"""Attachment limits and the bounded transfer (design §3.3).

A file crosses the gateway as a stream of chunks in either direction: an agent's
``send --attach`` streams up to the platform, ``coop attachment`` streams down from
it, and the gateway holds nothing but the chunk in flight. :class:`Limits` is the
``gateway.attachments`` section; :func:`bounded` applies it to one transfer.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass

MB = 1024 * 1024


@dataclass(frozen=True)
class Limits:
    """``0`` for a size or a count leaves the platform's own limit in force; set one
    only to be stricter than the platform."""

    max_size_mb: float = 0
    max_per_post: int = 0
    transfer_timeout_seconds: float = 120

    @property
    def max_bytes(self) -> int:
        return int(self.max_size_mb * MB)

    def over_size(self, name: str, size: int) -> str | None:
        """The refusal for a file of ``size`` bytes, or ``None``."""
        if self.max_bytes and size > self.max_bytes:
            return (f"attachment {name} is {size / MB:.1f} MB, over the {self.max_size_mb:g} MB limit "
                    "(gateway.attachments.max_size_mb)")
        return None

    def over_count(self, count: int) -> str | None:
        if self.max_per_post and count > self.max_per_post:
            return (f"{count} attachments on one post, over the limit of {self.max_per_post} "
                    "(gateway.attachments.max_per_post)")
        return None


class AttachmentRefused(Exception):
    """A file the gateway's limits refuse, before or during its transfer; the doorbell
    answers 413 and nothing was posted. ``name`` is the file's, or its handle."""

    def __init__(self, reason: str, name: str = "") -> None:
        super().__init__(reason)
        self.name = name


class TransferTimeout(Exception):
    """One file's transfer ran past ``transfer_timeout_seconds``."""

    def __init__(self, name: str, seconds: float) -> None:
        super().__init__(f"the transfer of {name} ran past {seconds:g} s (gateway.attachments.transfer_timeout_seconds)")
        self.name = name


async def bounded(chunks: AsyncIterator[bytes], name: str, limits: Limits, *, expected: int | None = None,
                  clock=None) -> AsyncIterator[bytes]:
    """``chunks`` with the limits applied: the whole transfer within
    ``transfer_timeout_seconds``, and the bytes within ``max_size_mb`` and, when the
    sender declared a size, within that. A chunk that crosses a limit raises before it
    is yielded, so the receiver never sees more than the limit allows."""
    loop = asyncio.get_running_loop()
    now = loop.time if clock is None else clock
    deadline = now() + limits.transfer_timeout_seconds
    total = 0
    while True:
        remaining = deadline - now()
        if remaining <= 0:
            raise TransferTimeout(name, limits.transfer_timeout_seconds)
        try:
            chunk = await asyncio.wait_for(anext(chunks), remaining)
        except StopAsyncIteration:
            return
        except TimeoutError:
            raise TransferTimeout(name, limits.transfer_timeout_seconds) from None
        total += len(chunk)
        problem = limits.over_size(name, total)
        if problem is not None:
            raise AttachmentRefused(problem, name)
        if expected is not None and total > expected:
            raise AttachmentRefused(f"attachment {name} is longer than the {expected} bytes declared", name)
        yield chunk
