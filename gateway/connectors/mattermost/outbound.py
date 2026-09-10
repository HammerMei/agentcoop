"""Outbound message delivery helpers for Mattermost.

Thin wrappers around MattermostREST that handle text chunking and media
upload, keeping delivery logic separate from the connector orchestration —
same split as gateway/connectors/rocketchat/outbound.py.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .rest import MattermostREST

logger = logging.getLogger("coop.connectors.mattermost.outbound")

# Mattermost's default per-post character limit (ServiceSettings.MaxPostSize)
# is 16383. Surfaced via the connector's text_chunk_limit property; kept here
# only as the outbound module's own default when called without one.
_DEFAULT_CHUNK_LIMIT: int | None = None

# Retry policy for post_message failures (e.g. transient REST errors) — same
# shape as RC's outbound.py.
_MAX_RETRIES: int = 3
_RETRY_DELAYS: tuple[float, ...] = (1.0, 3.0)  # delays *between* attempts


async def _post_with_retry(
    rest: "MattermostREST",
    channel_id: str,
    text: str,
    root_id: str | None,
) -> None:
    """Call rest.post_message with up to _MAX_RETRIES attempts.

    Raises the last exception if all attempts fail.
    """
    last_err: BaseException | None = None
    for attempt in range(_MAX_RETRIES):
        try:
            await rest.post_message(channel_id, text, root_id=root_id)
            return
        except Exception as exc:
            last_err = exc
            if attempt < _MAX_RETRIES - 1:
                delay = _RETRY_DELAYS[attempt]
                logger.warning(
                    "post_message attempt %d/%d failed (%s) — retrying in %.0fs",
                    attempt + 1, _MAX_RETRIES, exc, delay,
                )
                await asyncio.sleep(delay)

    logger.error(
        "post_message failed after %d attempts for channel %s: %s",
        _MAX_RETRIES, channel_id, last_err,
    )
    raise last_err  # type: ignore[misc]


async def send_text(
    rest: "MattermostREST",
    channel_id: str,
    text: str,
    chunk_limit: int | None = _DEFAULT_CHUNK_LIMIT,
    root_id: str | None = None,
) -> None:
    """Send a text message to a Mattermost channel, splitting if needed.

    Args:
        rest        : Authenticated MattermostREST client.
        channel_id  : Opaque Mattermost channel ID.
        text        : Message body to send.
        chunk_limit : If set, split text into chunks of this many characters.
                      None means send as a single message.
        root_id     : Thread root post ID. All chunks are posted into the
                      same thread when set.
    """
    if not chunk_limit or len(text) <= chunk_limit:
        await _post_with_retry(rest, channel_id, text, root_id)
        return

    chunks = _split_text(text, chunk_limit)
    for chunk in chunks:
        await _post_with_retry(rest, channel_id, chunk, root_id)
        logger.debug("Sent chunk (%d chars) to channel %s", len(chunk), channel_id)


async def send_media(
    rest: "MattermostREST",
    channel_id: str,
    file_path: str,
    caption: str = "",
) -> None:
    """Upload a local file to a Mattermost channel with an optional caption.

    Mattermost separates upload from posting (unlike RC's single-call
    rooms.upload): upload_file() returns file_ids, then post_message()
    attaches them to a new post.
    """
    file_ids = await rest.upload_file(channel_id, file_path)
    await rest.post_message(channel_id, caption, file_ids=file_ids)


def _split_text(text: str, limit: int) -> list[str]:
    """Split text into chunks of at most `limit` characters.

    Same newline-boundary-preferring strategy as RC's outbound.py.
    """
    chunks: list[str] = []
    while len(text) > limit:
        split_at = text.rfind("\n", limit - limit // 5, limit)
        if split_at == -1:
            split_at = limit
        chunks.append(text[:split_at].rstrip())
        text = text[split_at:].lstrip()
    if text:
        chunks.append(text)
    return chunks
