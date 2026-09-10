"""Inbound message normalization for Mattermost.

Converts a decoded WS posted-event (post dict + mentions user-id list) into
normalized IncomingMessage objects. All Mattermost-specific field names
(user_id, channel_id, root_id, file_ids, create_at, etc.) are handled here
and nowhere else in the codebase.

Mirrors gateway/connectors/rocketchat/normalize.py's filter/normalize split,
adapted for two structural differences confirmed against a live server:
  - Own-message and sender identity are by user ID, not username — the
    connector must resolve sender_username (async, via REST) before calling
    filter_mm_message, unlike RC where the username is already inline in the
    DDP doc.
  - `mentions` is a list of user IDs, not usernames — resolved to usernames
    here (for IncomingMessage.mentions / the to: field) via the REST client's
    cached resolve_username(), same trust model as RC's mentions[] usernames
    (server-controlled, not user-input).

Attachment downloads (via _download_attachments below) fetch file metadata
with rest.get_file_info() before downloading, since Mattermost's post.file_ids
only carries bare IDs (unlike RC, which embeds name/size/type inline).
"""

from __future__ import annotations

import asyncio
import functools
import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from ...core.adapter_utils import ts_to_float as _ts_to_float
from ...core.connector import Attachment, IncomingMessage, Room, User, UserRole
from ...core.sender_policy import sender_allowed  # noqa: F401 — re-exported for

# this module's existing importers; the rule itself lives in core now that both
# connectors ask it.
from .mentions import text_has_room_wide_mention

if TYPE_CHECKING:
    from .agent_chain import TurnStore
    from .config import MattermostConfig
    from .rest import MattermostREST

logger = logging.getLogger("coop.connectors.mattermost.normalize")


def bare_handle(display: str) -> str:
    """A Mattermost display handle with its leading `@` removed, if it has one.

    A **DM's** `channel_display_name` on the websocket event is the counterpart
    `@`-prefixed — `@alice`, not `alice`. Rocket.Chat supplies bare usernames,
    so carrying the prefix through left the same person addressable by two
    different watcher handles depending on the platform, and the Mattermost one
    needed percent-encoding to type: `mm:dm:%40alice` against `rc:dm:alice`
    (`@` is outside `_LABEL_SAFE`).

    **A group DM's is not prefixed.** Verified on 11.7.0 against a live server,
    because assuming otherwise is what put a fictional value into this
    function's first set of tests: a DM event carries `'@mmadmin'` while a group
    DM event carries `'acg_bot, mmadmin, probe-extra'` (§6.4). So this is
    applied to both kinds and is simply a no-op for one of them — two branches
    treating one wire field by different rules is what let the DM prefix go
    unnoticed in the first place.

    Exactly one leading `@` is removed and nothing else is touched, which is
    lossless for a username: Mattermost restricts those to letters, digits and
    `.-_`, so a name cannot itself begin with `@`.

    **The prefix is unconditional, not a display preference.** Worth recording
    because it was the one assumption here nothing in this repo could settle:
    mattermost-server builds the event's field with
    `GetChannelName(model.ShowUsername, "")` in `notification.go`, hardcoding
    the format — `GetDisplayNameWithPrefix` only drops the `@` under
    `ShowNicknameFullName`/`ShowFullName`, which that call never passes. So a
    server whose `TeammateNameDisplay` is set to full name still sends
    `@alice` on the wire, and the strip is not silently doing nothing there.

    Idempotence is a property of the implementation, not a requirement any
    caller currently exercises — and the earlier claim that the membership-add
    path "supplies a bare name" was wrong. That path reads REST's
    `display_name`, which for a direct channel is the **empty string** (see
    `rest.py`'s `channel_member_usernames` docstring), so it never reaches here
    with a name at all.
    """
    return display[1:] if display.startswith("@") else display


@functools.lru_cache(maxsize=8)
def _leading_mention_pattern(bot_username: str) -> re.Pattern[str]:
    """Match a leading bot mention prefix at the start of a message.

    Same shape as RC's _leading_mention_pattern — cached per bot_username to
    avoid redundant regex compilation.
    """
    return re.compile(rf"^\s*@{re.escape(bot_username)}(?:\s+|[:,-]\s*)?")


@functools.lru_cache(maxsize=8)
def _mention_pattern(bot_username: str) -> re.Pattern[str]:
    """Match a standalone @mention of the bot username anywhere in text."""
    return re.compile(rf"(?<![\w@])@{re.escape(bot_username)}(?![\w.-])")


def text_mentions_bot(text: str, bot_username: str) -> bool:
    """Text-based bot-mention check — used ONLY for REST-history replay.

    Live dispatch uses the WS event's server-computed `mentions` user-id
    array instead (see filter_mm_message) — more robust, already trusted,
    and confirmed against a live server. This text-based fallback exists
    because Mattermost's REST channel-history API returns bare Post objects
    with no mention data at all: the `mentions` field is a WS-notification-
    time computation (who should be notified), not part of the Post schema,
    so replayed messages have no ID-based signal to check against.

    Known limitation: this only detects a mention of the bot itself, not
    other users/agents mentioned in the same message — so
    IncomingMessage.mentions / the to: field will be less complete for
    replayed messages than for live ones.
    """
    if not bot_username:
        return False
    return bool(_mention_pattern(bot_username).search(text))


# ---------------------------------------------------------------------------
# Filter: decide whether an inbound posted-event should be processed
# ---------------------------------------------------------------------------


@dataclass
class FilterResult:
    accepted: bool
    sender: str = ""
    msg_ts: str = ""
    reason: str = ""  # debug only
    is_agent_chain: bool = False   # True when sender is a known AgentCoop agent
    agent_chain_turn: int = 0      # current turn (1-based, after increment)
    # Names the increment rather than counting it — the same field, for the same reason,
    # as the Rocket.Chat result. `agent_chain_turn` is the live count and moves in both
    # directions, so it cannot identify a delivery. Zero when no turn was taken.
    agent_chain_token: int = 0
    agent_chain_max_turns: int = 5  # from config


def filter_mm_message(
    post: dict,
    mentions: list[str],
    sender_username: str,
    config: "MattermostConfig",
    room_type: str,
    last_processed_ts: str | None,
    bot_user_id: str,
    turn_store: "TurnStore | None" = None,
) -> FilterResult:
    """Decide whether a decoded Mattermost post should be processed.

    Applies (in order):
      0. Skip system messages (type field non-empty, e.g. "added to channel").
      1. Skip messages from the bot itself (by user ID).
      2. Sender filter (allow-list or open mode, agents always pass).
      3. For non-DM channels: require explicit @mention of the bot, checked
         against the server-provided mentions user-id list (not a text
         regex — more robust and already trusted), plus a text-based check
         for special @channel/@all/@here keywords which never appear in the
         id-based mentions list.
      4. Timestamp deduplication: skip messages already processed.
      5. Agent chain turn budget check (agents only) / counter reset (humans).

    Args:
        sender_username: Already resolved by the caller (async, via REST)
                          from post["user_id"] — this function stays
                          synchronous, matching filter_rc_message's shape.
    """
    if post.get("type"):
        return FilterResult(accepted=False, reason="system message")

    sender_id = post.get("user_id", "")

    # 1. Skip own messages — by ID, since Mattermost identifies senders by ID.
    if sender_id == bot_user_id:
        return FilterResult(accepted=False, reason="own message")

    is_agent = sender_username in config.agent_chain.agent_usernames

    # 2. Sender filter
    if not sender_allowed(config, sender_username):
        return FilterResult(
            accepted=False, sender=sender_username, reason="sender not in allow-list"
        )

    # 3. For channels: require @mention (unless listen-all mode or agent sender)
    if config.require_mention and not is_agent and room_type != "dm":
        bot_mentioned = bot_user_id in mentions  # trusted: server-computed ID array
        # room_wide_mentioned is a text-regex check, NOT a trusted server
        # signal like bot_mentioned above — Mattermost gives no ID-based
        # signal for @channel/@all/@here at all, so an already-allow-listed
        # sender can spoof this text to bypass the mention gate. Accepted
        # platform limitation — see mentions.py's SECURITY NOTE for the full
        # tradeoff and why no better fix exists.
        room_wide_mentioned = text_has_room_wide_mention(post.get("message", ""))
        if not bot_mentioned and not room_wide_mentioned:
            return FilterResult(
                accepted=False, sender=sender_username, reason="bot not mentioned"
            )

    # 4. Timestamp deduplication
    msg_ts = str(post.get("create_at", ""))
    msg_ts_f = _ts_to_float(msg_ts)
    last_ts_f = _ts_to_float(last_processed_ts)
    if msg_ts_f is not None and last_ts_f is not None and msg_ts_f <= last_ts_f:
        return FilterResult(
            accepted=False,
            sender=sender_username,
            msg_ts=msg_ts,
            reason=f"already processed (ts={msg_ts})",
        )

    # 5. Agent chain turn budget
    agent_chain_turn = 0
    agent_chain_token = 0
    if is_agent and turn_store is not None:
        allowed, agent_chain_turn, agent_chain_token = turn_store.check_and_increment(
            room_id=post.get("channel_id", ""),
            thread_id=post.get("root_id") or None,
            sender=sender_username,
            max_turns=config.agent_chain.max_turns,
        )
        if not allowed:
            logger.info(
                "Agent chain turn limit reached for sender=%s (max=%d) — dropping",
                sender_username,
                config.agent_chain.max_turns,
            )
            return FilterResult(
                accepted=False, sender=sender_username, reason="agent chain turn limit reached"
            )
    elif not is_agent and turn_store is not None:
        turn_store.reset_all(
            room_id=post.get("channel_id", ""),
            thread_id=post.get("root_id") or None,
        )

    return FilterResult(
        accepted=True,
        sender=sender_username,
        msg_ts=msg_ts,
        is_agent_chain=is_agent,
        agent_chain_turn=agent_chain_turn,
        agent_chain_token=agent_chain_token,
        agent_chain_max_turns=config.agent_chain.max_turns,
    )


# ---------------------------------------------------------------------------
# Normalize: convert an accepted post into IncomingMessage
# ---------------------------------------------------------------------------


async def normalize_mm_message(
    post: dict,
    mentions: list[str],
    room: Room,
    sender_username: str,
    sender_id: str,
    msg_ts: str,
    config: "MattermostConfig",
    rest: "MattermostREST",
    cache_dir: Path,
    is_agent_chain: bool = False,
    agent_chain_turn: int = 0,
    agent_chain_max_turns: int = 5,
) -> IncomingMessage:
    """Convert an accepted Mattermost post into a normalized IncomingMessage.

    Caller is responsible for running filter_mm_message() first; this
    function assumes the message has already passed all filters.
    """
    role = UserRole(config.role_of(sender_username))
    sender = User(id=sender_id, username=sender_username, display_name=sender_username)

    bot_username = rest.bot_username or ""
    text = _extract_text(post, room.type, bot_username)
    attachments, warnings = await _download_attachments(post, config, rest, cache_dir)
    thread_id: str | None = post.get("root_id") or None

    # Resolve mentioned user IDs to usernames — server-controlled ID list
    # (from the trusted WS event), not user-input, same trust model as RC's
    # mentions[] usernames.
    mention_usernames: list[str] = []
    for uid in mentions:
        try:
            mention_usernames.append(await rest.resolve_username(uid))
        except Exception:
            logger.warning("Failed to resolve mentioned user id %s", uid)
    if text_has_room_wide_mention(post.get("message", "")):
        mention_usernames.append("all")

    msg = IncomingMessage(
        id=post.get("id", msg_ts),
        timestamp=msg_ts,
        room=room,
        sender=sender,
        role=role,
        text=text,
        attachments=attachments,
        warnings=warnings,
        thread_id=thread_id,
        mentions=mention_usernames,
        raw=post,
    )

    msg.extra_context["is_agent_chain"] = is_agent_chain
    msg.extra_context["agent_chain_turn"] = agent_chain_turn
    msg.extra_context["agent_chain_max_turns"] = agent_chain_max_turns

    return msg


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _extract_text(post: dict, room_type: str, bot_username: str) -> str:
    """Extract and clean message text from a decoded post.

    For DMs: return the raw text as-is (no @mention prefix to strip).
    For channels: strip the leading @botname mention.

    Unlike RC, Mattermost has no upload-only-message quirk where captions
    live in a separate attachments[].description field — file captions are
    always in post["message"] directly, so no fallback extraction is needed.
    """
    raw_text = post.get("message", "")
    if room_type == "dm" or not bot_username:
        text = raw_text.strip()
    else:
        text = _leading_mention_pattern(bot_username).sub("", raw_text, count=1).strip()
    return text or "(empty message)"


async def _download_attachments(
    post: dict,
    config: "MattermostConfig",
    rest: "MattermostREST",
    cache_dir: Path,
) -> tuple[list[Attachment], list[str]]:
    """Download all file attachments referenced by a post to cache_dir.

    Args:
        post     : Decoded Mattermost post dict (from a WS posted event).
        config   : MattermostConfig — attachment size/timeout limits.
        rest     : Authenticated MattermostREST client.
        cache_dir: Absolute directory path for downloaded attachments.

    Returns:
        A tuple of (successful_attachments, warnings), same contract as
        RC's _download_attachments.
    """
    file_ids = post.get("file_ids") or []
    if not file_ids:
        return [], []

    attach_cfg = config.attachments
    await asyncio.to_thread(cache_dir.mkdir, parents=True, exist_ok=True)
    max_bytes = (
        int(attach_cfg.max_file_size_mb * 1024 * 1024)
        if attach_cfg.max_file_size_mb > 0
        else 0
    )
    warnings: list[str] = []
    sem = asyncio.Semaphore(4)

    async def _download_one(idx: int, file_id: str) -> Attachment | None:
        try:
            info = await rest.get_file_info(file_id)
        except Exception as e:
            logger.error("Failed to fetch file info for %s: %s", file_id, e)
            warnings.append("[⚠️ Attachment failed to download — file not available]")
            return None

        original_name = info.get("name", f"attachment_{idx}")
        file_size = info.get("size", 0)
        mime_type = info.get("mime_type", "")

        safe_file_id = re.sub(r"[^\w.\-]", "_", file_id) if file_id else f"attachment_{idx}"
        safe_name = re.sub(r"[^\w.\-]", "_", original_name)
        dest_path = cache_dir / f"{safe_file_id}_{safe_name}"

        # Defense-in-depth path-traversal guard, same as RC's normalize.py.
        try:
            dest_path.resolve().relative_to(cache_dir.resolve())
        except ValueError:
            logger.error(
                "Attachment path traversal blocked: '%s' resolves outside cache_dir %s",
                f"{safe_file_id}_{safe_name}",
                cache_dir,
            )
            return None

        if max_bytes and file_size > max_bytes:
            size_mb = file_size / 1024 / 1024
            logger.warning(
                "Skipping attachment '%s': %.1f MB exceeds limit %.1f MB",
                original_name, size_mb, attach_cfg.max_file_size_mb,
            )
            warnings.append(
                f"[⚠️ Attachment '{original_name}' skipped — "
                f"{size_mb:.1f} MB exceeds {attach_cfg.max_file_size_mb:.0f} MB limit]"
            )
            return None

        tmp_path = dest_path.with_suffix(dest_path.suffix + ".tmp")
        async with sem:
            result: Attachment | None = None
            try:
                try:
                    await asyncio.wait_for(
                        rest.download_file(file_id, str(tmp_path)),
                        timeout=attach_cfg.download_timeout,
                    )
                    await asyncio.to_thread(tmp_path.rename, dest_path)
                    logger.info("Downloaded attachment '%s' -> %s", original_name, dest_path)
                    result = Attachment(
                        original_name=original_name,
                        local_path=str(dest_path),
                        mime_type=mime_type,
                        size_bytes=file_size,
                    )
                except asyncio.TimeoutError:
                    logger.warning(
                        "Download timed out for attachment '%s' (limit %ds)",
                        original_name, attach_cfg.download_timeout,
                    )
                    warnings.append(
                        f"[⚠️ Attachment '{original_name}' failed to download (timed out) — file not available]"
                    )
                except Exception as e:
                    logger.error("Failed to download attachment '%s': %s", original_name, e)
                    warnings.append(
                        f"[⚠️ Attachment '{original_name}' failed to download — file not available]"
                    )
            finally:
                tmp_path.unlink(missing_ok=True)
        return result

    downloaded = await asyncio.gather(
        *[_download_one(i, fid) for i, fid in enumerate(file_ids)]
    )
    return [att for att in downloaded if att is not None], warnings
