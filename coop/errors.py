"""Errors an agent sees on the doorbell (design §3.3, §3.5). They are raised by the
room core and carried to the agent by whichever client it uses: in process, as these
exceptions; over HTTP, as the status and reason the API answers with."""

from __future__ import annotations


class RoomUnresolved(Exception):
    """A send or read named no room the gateway could settle on: a room it does not
    watch, a thread in none of the agent's rooms, or a thread in several of them."""


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


class DoorbellError(Exception):
    """The doorbell API answered an error the agent cannot act on by changing the
    reply: an unknown token (401), a room whose roster does not list the agent (403),
    a room the gateway does not watch (404), or anything unexpected."""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(f"{status}: {message}")
        self.status = status
        self.message = message


class CertificateMismatch(Exception):
    """The gateway presented a certificate whose fingerprint is not the pinned one."""

    def __init__(self, expected: str, presented: str) -> None:
        super().__init__(f"the gateway's certificate fingerprint {presented} is not the pinned {expected}")
        self.expected = expected
        self.presented = presented


class LostResponse(Exception):
    """A send the gateway did not answer in time (design §3.3): the reply may have
    been posted. The agent reads the room and posts again only if it is not there."""

    TEXT = ("the reply may have been posted: the gateway did not answer in time. "
            "Run `coop read` and post again only if the reply is not in the room.")

    def __init__(self) -> None:
        super().__init__(self.TEXT)
