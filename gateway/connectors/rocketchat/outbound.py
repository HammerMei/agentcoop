"""Outbound message delivery helpers for Rocket.Chat.

Thin wrappers around RocketChatREST that handle text chunking and media
upload, keeping delivery logic separate from the connector orchestration.

Inspired by OpenClaw's channels/plugins/outbound/ pattern.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .rest import RocketChatREST

logger = logging.getLogger("coop.connectors.rocketchat.outbound")

# RC has a generous practical limit, but very long messages can be rejected.
# We leave chunking disabled by default (None) and let the connector's
# text_chunk_limit property surface this to SessionManager if needed.
_DEFAULT_CHUNK_LIMIT: int | None = None

# Retry policy for post_message failures (e.g. transient RC REST errors).
# Three attempts total: immediate, +1 s, +3 s.
_MAX_RETRIES: int = 3
_RETRY_DELAYS: tuple[float, ...] = (1.0, 3.0)  # delays *between* attempts


async def _post_with_retry(
    rest: "RocketChatREST",
    room_id: str,
    text: str,
    tmid: str | None,
) -> None:
    """Call rest.post_message with up to _MAX_RETRIES attempts.

    Raises the last exception if all attempts fail.
    """
    last_err: BaseException | None = None
    for attempt in range(_MAX_RETRIES):
        try:
            await rest.post_message(room_id, text, tmid=tmid)
            return
        except Exception as exc:
            last_err = exc
            if attempt < _MAX_RETRIES - 1:
                delay = _RETRY_DELAYS[attempt]
                logger.warning(
                    "post_message attempt %d/%d failed (%s) — retrying in %.0fs",
                    attempt + 1,
                    _MAX_RETRIES,
                    exc,
                    delay,
                )
                await asyncio.sleep(delay)

    logger.error(
        "post_message failed after %d attempts for room %s: %s",
        _MAX_RETRIES,
        room_id,
        last_err,
    )
    raise last_err  # type: ignore[misc]


async def send_text(
    rest: "RocketChatREST",
    room_id: str,
    text: str,
    chunk_limit: int | None = _DEFAULT_CHUNK_LIMIT,
    tmid: str | None = None,
) -> None:
    """Send a text message to a Rocket.Chat room, splitting if needed.

    Each chunk is delivered via :func:`_post_with_retry`, which retries up to
    ``_MAX_RETRIES`` times on transient REST failures before giving up and
    re-raising the last exception.

    Args:
        rest        : Authenticated RocketChatREST client.
        room_id     : Opaque RC room ID (not the room name).
        text        : Message body to send.
        chunk_limit : If set, split text into chunks of this many characters.
                      None means send as a single message.
        tmid        : Thread root message ID.  All chunks are posted into the
                      same thread when set.
    """
    if not chunk_limit or len(text) <= chunk_limit:
        await _post_with_retry(rest, room_id, text, tmid)
        return

    # Split on newlines where possible to avoid cutting mid-word
    chunks = _split_text(text, chunk_limit)
    for chunk in chunks:
        await _post_with_retry(rest, room_id, chunk, tmid)
        logger.debug("Sent chunk (%d chars) to room %s", len(chunk), room_id)


async def send_media(
    rest: "RocketChatREST",
    room_id: str,
    file_path: str,
    caption: str = "",
) -> None:
    """Upload a local file to a Rocket.Chat room.

    Args:
        rest      : Authenticated RocketChatREST client.
        room_id   : Opaque RC room ID.
        file_path : Absolute local path of the file to upload.
        caption   : Optional message caption shown alongside the file.
    """
    await rest.upload_file(room_id, file_path, caption)


def _split_text(text: str, limit: int) -> list[str]:
    """Split text into chunks of at most `limit` characters.

    Prefers splitting on newline boundaries; falls back to hard-cutting at
    `limit` if no suitable boundary exists within the window.
    """
    chunks: list[str] = []
    while len(text) > limit:
        # Try to find a newline within the last 20% of the window
        split_at = text.rfind("\n", limit - limit // 5, limit)
        if split_at == -1:
            split_at = limit
        chunks.append(text[:split_at].rstrip())
        text = text[split_at:].lstrip()
    if text:
        chunks.append(text)
    return chunks
