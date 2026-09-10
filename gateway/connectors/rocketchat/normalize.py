"""Inbound message normalization for Rocket.Chat.

Converts raw DDP message documents into normalized IncomingMessage objects.
All RC-specific field names (u.username, mentions[], attachments[].title_link,
files[], ts.$date, etc.) are handled here and nowhere else in the codebase.

Inspired by OpenClaw's channels/plugins/normalize/ pattern: inbound
normalization and outbound delivery are kept as separate, focused modules.
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
from ...core.sender_policy import sender_allowed
from .mentions import is_room_wide_mention

if TYPE_CHECKING:
    from .agent_chain import TurnStore
    from .config import RocketChatConfig
    from .rest import RocketChatREST

logger = logging.getLogger("coop.connectors.rocketchat.normalize")


@functools.lru_cache(maxsize=8)
def _mention_pattern(bot_username: str) -> re.Pattern[str]:
    """Match an explicit standalone @mention of the bot username.

    Cached per bot_username: re.compile is called once per unique username
    rather than on every message, avoiding redundant regex compilation overhead
    in high-throughput rooms.
    """
    return re.compile(rf"(?<![\w@])@{re.escape(bot_username)}(?![\w.-])")


@functools.lru_cache(maxsize=8)
def _leading_mention_pattern(bot_username: str) -> re.Pattern[str]:
    """Match a leading bot mention prefix at the start of a message.

    Cached per bot_username for the same reason as _mention_pattern.
    """
    return re.compile(rf"^\s*@{re.escape(bot_username)}(?:\s+|[:,-]\s*)?")


# ---------------------------------------------------------------------------
# Filter: decide whether an inbound DDP doc should be processed
# ---------------------------------------------------------------------------


@dataclass
class FilterResult:
    accepted: bool
    sender: str = ""
    msg_ts: str = ""
    reason: str = ""  # debug only
    is_agent_chain: bool = False   # True when sender is a known ACG agent
    agent_chain_turn: int = 0      # current turn (1-based, after increment)
    # Names the increment rather than counting it. `agent_chain_turn` is the live count
    # and moves in both directions, so it cannot identify a delivery; releasing a turn
    # needs this. Zero when no turn was taken.
    agent_chain_token: int = 0
    agent_chain_max_turns: int = 5  # from config


def filter_rc_message(
    doc: dict,
    config: "RocketChatConfig",
    room_type: str,
    last_processed_ts: str | None,
    turn_store: "TurnStore | None" = None,
    bot_user_id: str = "",
    bot_username: str = "",
) -> FilterResult:
    """Decide whether a raw RC DDP message document should be processed.

    Applies (in order):
      1. Skip messages from the bot itself.
      2. Sender filter (allow-list or open mode, agents always pass).
      3. For non-DM rooms: require explicit @mention of the bot
         (skipped for agent senders and when require_mention=False).
      4. Timestamp deduplication: skip messages already processed.
      5. Agent chain turn budget check (agents only) / counter reset (humans).
         State mutation runs after dedup so replayed messages never corrupt
         turn counters.

    Returns a FilterResult describing the outcome.
    """
    # 0. Skip messages carrying a type letter.
    #
    # Rocket.Chat marks system events with `t` on the message doc — `uj` joined, `au`
    # added, `ru` removed, `room_changed_topic` — and delivers them over DDP like any
    # other message. Only the REST history path filtered them, so they reached the agent
    # as turns.
    #
    # **And they do not look empty.** RC puts the payload in `msg`: the joining user's
    # name for `uj`, the added user's for `au`, the new topic for `room_changed_topic`.
    # The history filter is the evidence — `not m.get("t") and m.get("msg")` in `rest.py`
    # tests both, and the first clause would be redundant if a system message always had
    # an empty body. So in a DM or a listen-all room the agent was answering a message
    # whose text was `glin`, with nothing to mark it as machinery.
    #
    # The letter is not exclusively for system events: `e2e` marks an
    # end-to-end-encrypted body (every user message in such a room carries it, and this
    # turns unreadable ciphertext into a clean drop — a bot cannot complete the key
    # exchange anyway), and `discussion-created` / `message_pinned` are user actions.
    # Dropping all of them as *turns* is the intent: none is someone talking to the
    # agent. The letter is named in the reason so a vanished message is traceable rather
    # than mysterious.
    #
    # One deliberate side effect, stated because nothing else would say it: this runs
    # before the agent-chain step, so a system message no longer resets the chain's turn
    # budget. A human joining a room used to hand two mid-chain agents a fresh five turns.
    # A join is not a human turn, so not resetting is the better answer — but it is a
    # change, not a no-op.
    #
    # Mattermost has gated this on the live path all along (`post.get("type")`).
    message_type = doc.get("t")
    if message_type:
        return FilterResult(
            accepted=False, reason=f"system message (t={message_type})"
        )

    sender = doc.get("u", {}).get("username", "")
    # The canonical spelling once the connector has logged in (#112); the
    # configured one only before that, or in tests that pass no better.
    own_username = bot_username or config.username

    # 1. Skip own messages — by id first, name only as a fallback for frames carrying no
    #    id. Rocket.Chat's login is not spelling-exact: probed against 6.12, an account
    #    whose canonical username is `ProbeBot9207` logs in as `probebot9207` or by email
    #    address, and `me.username` in the response is still `ProbeBot9207`. The connector
    #    stores what the operator configured, so `config.username` is whatever they typed
    #    and `u.username` on every frame is the canonical spelling.
    #
    #    A name-only test therefore fails to recognise the bot's own posts under an
    #    ordinary configuration. With `filter_sender` open and a DM — which skips the
    #    mention gate entirely — the bot's own reply reads as user activity, is dispatched,
    #    and the answer to it arrives as the next own message: an unbounded self-reply
    #    loop, with no turn budget in the way because the bot is not in `agent_usernames`.
    #
    #    `_on_unrouted_message` already tests by id; this is the same rule on the tracked
    #    path, which is the one that carries the traffic. Mattermost's filter has taken a
    #    `bot_user_id` for exactly this all along.
    if bot_user_id and doc.get("u", {}).get("_id") == bot_user_id:
        return FilterResult(accepted=False, reason="own message")
    if sender == own_username:
        return FilterResult(accepted=False, reason="own message")

    # Determine if sender is a known agent
    is_agent = sender in config.agent_chain.agent_usernames

    # 2. Sender filter — the shared rule, not an inline copy (#115): this was
    # the fourth site of "may this sender start a turn", and the module's own
    # docstring argues a second copy is one too many. The `is_agent` bypass
    # this copy carried is inside `sender_allowed` already.
    if not sender_allowed(config, sender):
        return FilterResult(accepted=False, sender=sender, reason="sender not in allow-list")
    # open mode passes everyone; role resolved later in normalize

    # 3. For channels/groups: require @mention (unless listen-all mode or agent sender)
    if config.require_mention and not is_agent and room_type != "dm":
        mentions = doc.get("mentions", [])
        bot_mentioned = any(m.get("username") == own_username for m in mentions)
        room_wide_mentioned = any(
            is_room_wide_mention(m.get("username", "")) for m in mentions
        )
        if not bot_mentioned and not room_wide_mentioned:
            msg_text = doc.get("msg", "")
            attach_descs = " ".join(
                a.get("description", "") for a in doc.get("attachments", [])
            )
            searchable = (msg_text + " " + attach_descs).strip()
            if not _mention_pattern(own_username).search(searchable):
                return FilterResult(
                    accepted=False, sender=sender, reason="bot not mentioned"
                )
    # Note: agent senders bypass @mention requirement (listen-all for agents)
    # Note: listen-all mode (require_mention=False) skips this for all senders

    # 4. Timestamp deduplication — run BEFORE any state mutation so replayed
    #    or reconnect-duplicated messages never corrupt turn counters.
    msg_ts = extract_ts(doc)
    msg_ts_f = _ts_to_float(msg_ts)
    last_ts_f = _ts_to_float(last_processed_ts)
    if msg_ts_f is not None and last_ts_f is not None and msg_ts_f <= last_ts_f:
        return FilterResult(
            accepted=False,
            sender=sender,
            msg_ts=msg_ts,
            reason=f"already processed (ts={msg_ts})",
        )

    # 5. Agent chain turn budget (only for agent senders) — state mutation only
    #    after dedup confirms this is a fresh, previously-unseen message.
    agent_chain_turn = 0
    agent_chain_token = 0
    if is_agent and turn_store is not None:
        allowed, agent_chain_turn, agent_chain_token = turn_store.check_and_increment(
            room_id=doc.get("rid", ""),
            thread_id=doc.get("tmid") or None,
            sender=sender,
            max_turns=config.agent_chain.max_turns,
        )
        if not allowed:
            # Do NOT reset the counter here — resetting on drop would allow the
            # loop to restart immediately on the next message.  Instead, the
            # counter stays at max_turns and every subsequent message from this
            # sender is force-dropped until a human message (reset_all) or TTL
            # expiry clears the budget.
            logger.info(
                "Agent chain turn limit reached for sender=%s (max=%d) — dropping",
                sender,
                config.agent_chain.max_turns,
            )
            return FilterResult(accepted=False, sender=sender, reason="agent chain turn limit reached")
    elif not is_agent and turn_store is not None:
        # Human message: reset all agent chain counters for this context
        turn_store.reset_all(
            room_id=doc.get("rid", ""),
            thread_id=doc.get("tmid") or None,
        )

    return FilterResult(
        accepted=True,
        sender=sender,
        msg_ts=msg_ts,
        is_agent_chain=is_agent,
        agent_chain_turn=agent_chain_turn,
        agent_chain_token=agent_chain_token,
        agent_chain_max_turns=config.agent_chain.max_turns,
    )


# ---------------------------------------------------------------------------
# Normalize: convert an accepted DDP doc into IncomingMessage
# ---------------------------------------------------------------------------


async def normalize_rc_message(
    doc: dict,
    room: Room,
    sender_username: str,
    msg_ts: str,
    config: "RocketChatConfig",
    rest: "RocketChatREST",
    cache_dir: Path,
    is_agent_chain: bool = False,
    agent_chain_turn: int = 0,
    agent_chain_max_turns: int = 5,
) -> IncomingMessage:
    """Convert an accepted RC DDP message document into a normalized IncomingMessage.

    Caller is responsible for running filter_rc_message() first; this function
    assumes the message has already passed all filters.

    Args:
        doc                  : Raw RC DDP message document.
        room                 : Resolved Room (id, name, type already known).
        sender_username      : Extracted from doc["u"]["username"] by filter step.
        msg_ts               : Extracted timestamp string (from filter step).
        config               : RocketChatConfig (for role resolution, attachment settings).
        rest                 : RocketChatREST (for authenticated attachment downloads).
        cache_dir            : Absolute directory path for downloaded attachments.
                               Caller ensures this is unique per watcher.
        is_agent_chain       : True when the sender is a known ACG agent.
        agent_chain_turn     : 1-based current turn number (after increment).
        agent_chain_max_turns: Configured turn budget ceiling.
    """
    role = UserRole(config.role_of(sender_username))
    sender = User(
        id=doc.get("u", {}).get("_id", sender_username),
        username=sender_username,
        display_name=doc.get("u", {}).get("name", sender_username),
    )

    text = _extract_text(doc, room.type, rest.bot_username or config.username)
    attachments, warnings = await _download_attachments(doc, config, rest, cache_dir)
    thread_id: str | None = doc.get("tmid") or None

    # Extract mentioned usernames from RC's mentions[] metadata.
    # This is server-controlled (injected by RC before delivery), not user-input.
    # Username sanitization happens in format_prompt_prefix before any use in
    # the trusted header; here we store the raw value for flexibility.
    mentions = [
        m.get("username", "")
        for m in doc.get("mentions", [])
        if m.get("username")
    ]

    msg = IncomingMessage(
        id=doc.get("_id", msg_ts),
        timestamp=msg_ts,
        room=room,
        sender=sender,
        role=role,
        text=text,
        attachments=attachments,
        warnings=warnings,
        thread_id=thread_id,
        mentions=mentions,
        raw=doc,
    )

    # Store agent chain context for the core layer to use
    msg.extra_context["is_agent_chain"] = is_agent_chain
    msg.extra_context["agent_chain_turn"] = agent_chain_turn
    msg.extra_context["agent_chain_max_turns"] = agent_chain_max_turns

    return msg


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def extract_ts(doc: dict) -> str:
    """Extract a sortable timestamp string from a DDP message document.

    RC timestamps are Unix-epoch milliseconds (numeric or inside ``{"$date": N}``).
    This function always returns a *string* for backwards compatibility with the
    dedup watermark stored in _RoomSubscription, but the comparison in
    ``filter_rc_message()`` now uses ``_ts_to_float()`` for numeric ordering.
    """
    ts = doc.get("ts", "")
    if isinstance(ts, dict):
        ts = ts.get("$date", "")
    return str(ts)



def _extract_text(doc: dict, room_type: str, bot_username: str) -> str:
    """Extract and clean message text from a DDP document.

    For DMs: return the raw text as-is (no @mention prefix to strip).
    For channels/groups: strip the leading @botname mention.

    If the main message body is empty (e.g. file-upload-only message), fall
    back to the first non-empty attachment description.  RC puts upload captions
    in attachments[].description rather than in the msg field.
    """
    raw_text = doc.get("msg", "")

    if room_type == "dm":
        text = raw_text.strip()
        if not text:
            text = _first_attachment_desc(
                doc, strip_mention=False, bot_username=bot_username
            )
    else:
        text = _leading_mention_pattern(bot_username).sub("", raw_text, count=1).strip()
        if not text:
            text = _first_attachment_desc(
                doc, strip_mention=True, bot_username=bot_username
            )

    return text or "(empty message)"


def _first_attachment_desc(doc: dict, strip_mention: bool, bot_username: str) -> str:
    """Return the first non-empty attachment description, optionally stripping @mention."""
    for att in doc.get("attachments", []):
        desc = att.get("description", "").strip()
        if strip_mention:
            desc = _leading_mention_pattern(bot_username).sub("", desc, count=1).strip()
        if desc:
            return desc
    return ""


async def _download_attachments(
    doc: dict,
    config: "RocketChatConfig",
    rest: "RocketChatREST",
    cache_dir: Path,
) -> tuple[list[Attachment], list[str]]:
    """Download all file attachments in a DDP document to cache_dir.

    Returns:
        A tuple of (successful_attachments, warnings).  Warnings are
        human-readable strings describing files that failed to download
        (too large, timed out, error).  These are injected into the agent
        prompt so the agent can inform the user.
    """
    rc_files = doc.get("files", [])
    rc_attachments = doc.get("attachments", [])
    if not rc_files:
        return [], []

    attach_cfg = config.attachments
    await asyncio.to_thread(cache_dir.mkdir, parents=True, exist_ok=True)
    max_bytes = (
        int(attach_cfg.max_file_size_mb * 1024 * 1024)
        if attach_cfg.max_file_size_mb > 0
        else 0
    )
    warnings: list[str] = []

    # Download attachments with bounded concurrency (max 4 parallel downloads)
    sem = asyncio.Semaphore(4)

    async def _download_one(idx: int, file_info: dict) -> Attachment | None:
        file_id = file_info.get("_id", "")
        original_name = file_info.get("name", f"attachment_{idx}")
        file_size = file_info.get("size", 0)
        mime_type = file_info.get("type", "")

        # Resolve download URL: prefer title_link from the matching attachments[] entry.
        # Match by file_id first (stable), then by original filename, and fall back
        # to positional index only as a last resort — RC does not guarantee strict
        # positional alignment between files[] and attachments[].
        title_link = _find_title_link(rc_attachments, file_id, original_name, idx)
        if not title_link:
            title_link = f"/file-upload/{file_id}/{original_name}"

        # Build dest path using file_id as the unique key.
        # Both file_id and original_name are sanitized to prevent path traversal
        # (e.g. a malicious server sending file._id = "../../evil" must not
        # escape the cache directory).
        safe_file_id = (
            re.sub(r"[^\w.\-]", "_", file_id) if file_id else f"attachment_{idx}"
        )
        safe_name = re.sub(r"[^\w.\-]", "_", original_name)
        dest_path = cache_dir / f"{safe_file_id}_{safe_name}"

        # Defense-in-depth: verify the resolved destination is still under
        # cache_dir after Path normalization.  The regex sanitization above
        # blocks all ASCII traversal characters, but Unicode normalization or
        # exotic filesystem path handling could theoretically produce a resolved
        # path that escapes the intended directory.  Blocking here ensures no
        # file is ever written outside cache_dir regardless of the input.
        try:
            dest_path.resolve().relative_to(cache_dir.resolve())
        except ValueError:
            logger.error(
                "Attachment path traversal blocked: '%s' resolves outside cache_dir %s",
                f"{safe_file_id}_{safe_name}",
                cache_dir,
            )
            return None

        # Size check
        if max_bytes and file_size > max_bytes:
            size_mb = file_size / 1024 / 1024
            logger.warning(
                "Skipping attachment '%s': %.1f MB exceeds limit %.1f MB",
                original_name,
                size_mb,
                attach_cfg.max_file_size_mb,
            )
            warnings.append(
                f"[⚠️ Attachment '{original_name}' skipped — "
                f"{size_mb:.1f} MB exceeds {attach_cfg.max_file_size_mb:.0f} MB limit]"
            )
            return None

        # Download with timeout and bounded concurrency.
        # Use an atomic write pattern: download to a .tmp path first, then
        # rename on success. This prevents partial/corrupt files from being
        # cached and reused on subsequent messages if the download fails.
        tmp_path = dest_path.with_suffix(dest_path.suffix + ".tmp")
        async with sem:
            result: Attachment | None = None
            try:
                try:
                    await asyncio.wait_for(
                        rest.download_file(title_link, str(tmp_path)),
                        timeout=attach_cfg.download_timeout,
                    )
                    await asyncio.to_thread(tmp_path.rename, dest_path)
                    logger.info(
                        "Downloaded attachment '%s' -> %s", original_name, dest_path
                    )
                    result = Attachment(
                        original_name=original_name,
                        local_path=str(dest_path),
                        mime_type=mime_type,
                        size_bytes=file_size,
                    )

                except asyncio.TimeoutError:
                    logger.warning(
                        "Download timed out for attachment '%s' (limit %ds)",
                        original_name,
                        attach_cfg.download_timeout,
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
                # Always clean up the .tmp file on every exit path — including
                # asyncio.CancelledError (a BaseException, not caught by
                # `except Exception` above).  A synchronous unlink is used
                # intentionally: `await asyncio.to_thread(...)` would itself
                # be cancelled if the enclosing task is being cancelled,
                # defeating the purpose of this cleanup guard.
                # After a successful rename(), tmp_path no longer exists and
                # unlink(missing_ok=True) is a safe no-op.
                tmp_path.unlink(missing_ok=True)
        return result

    downloaded = await asyncio.gather(
        *[_download_one(i, f) for i, f in enumerate(rc_files)]
    )
    return [att for att in downloaded if att is not None], warnings


def _find_title_link(
    rc_attachments: list[dict],
    file_id: str,
    original_name: str,
    fallback_idx: int,
) -> str:
    """Find the download title_link for a file from the attachments[] array.

    RC's ``files[]`` and ``attachments[]`` are not guaranteed to maintain strict
    positional alignment.  This function prefers stable-ID matching over index:

      1. Match by ``title_link`` containing the ``file_id`` (most reliable).
      2. Match by ``title`` equal to ``original_name``.
      3. Fall back to positional index (original behaviour).

    Returns the ``title_link`` string, or ``""`` if nothing matched.
    """
    # 1. Match by file_id in title_link path (e.g. "/file-upload/<file_id>/...")
    if file_id:
        for att in rc_attachments:
            tl = att.get("title_link", "")
            if tl and file_id in tl:
                return tl

    # 2. Match by filename in title field
    if original_name:
        for att in rc_attachments:
            if att.get("title") == original_name:
                tl = att.get("title_link", "")
                if tl:
                    return tl

    # 3. Positional fallback (original behaviour)
    if fallback_idx < len(rc_attachments):
        return rc_attachments[fallback_idx].get("title_link", "")

    return ""
