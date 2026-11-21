"""Errors an agent sees on the doorbell (design §3.3, §3.5). They are raised by the
room core and carried to the agent by whichever client it uses: in process, as these
exceptions; over HTTP, as the status and reason the API answers with."""

from __future__ import annotations


class NoLiveLease(Exception):
    """The wake id names no wake this bot holds a live lease on."""


class InvalidReply(Exception):
    """A send with nothing postable (a structural problem): a usage error, nothing is
    posted and nothing is counted."""


class RefusedSend(Exception):
    """The gateway refused the send; ``reasons`` says why (design §3.5)."""

    def __init__(self, reasons: str) -> None:
        super().__init__(reasons)
        self.reasons = reasons
