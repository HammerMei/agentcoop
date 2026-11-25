"""The doorbell API as a :class:`~coop.client.Doorbell` over HTTP (design §3.2). The
gateway's answers are decoded with the same wire module the gateway encodes with;
its error statuses become the exceptions in :mod:`coop.errors`, so an agent written
against the in-process doorbell runs unchanged here.

A ``send`` is never retried (design §3.3): a timeout raises :class:`LostResponse`
and the agent checks the room before posting again.
"""

from __future__ import annotations

import asyncio
import re
import ssl
from urllib.parse import urlsplit

import httpx

from ..errors import (
    CertificateMismatch,
    DoorbellError,
    InvalidReply,
    LostResponse,
    NoLiveLease,
    RefusedSend,
)
from ..fingerprint import fingerprint, same_fingerprint
from ..room.model import Post, Reply, Sent, Wake
from ..room.platform import RefusedPost
from ..wire import decode_post, decode_wake, encode_reply

# The long-poll wait a client asks for when none is given, plus the slack the HTTP
# timeout allows over it.
_FILENAME = re.compile(r'filename="([^"]*)"')
DEFAULT_WAIT = 30.0
TIMEOUT_SLACK = 10.0


def pinned_context(address: str, expected: str, timeout: float = DEFAULT_WAIT) -> ssl.SSLContext:
    """A context that trusts only the certificate ``address`` presents, after checking
    its fingerprint against ``expected``. Raises :class:`CertificateMismatch`."""
    parts = urlsplit(address)
    pem = ssl.get_server_certificate((parts.hostname or "", parts.port or 443), timeout=timeout)
    presented = fingerprint(pem)
    if not same_fingerprint(presented, expected):
        raise CertificateMismatch(expected, presented)
    context = ssl.create_default_context(cadata=pem)
    context.check_hostname = False  # the pin is the identity; the name on a self-signed cert is not
    return context


class HttpDoorbell:
    """``fingerprint`` pins the gateway's self-signed certificate (design §3.2): the
    client fetches the certificate the address presents, refuses it when its SHA-256
    fingerprint differs, and otherwise trusts exactly that certificate. Without one,
    an ``https`` address is verified against the system's CAs as usual."""

    def __init__(self, address: str, token: str, fingerprint: str | None = None, timeout: float = 30.0) -> None:
        self.address = address.rstrip("/")
        self.timeout = timeout
        self._headers = {"Authorization": f"Bearer {token}"}
        self._pin = fingerprint if urlsplit(self.address).scheme == "https" else None
        self._client: httpx.AsyncClient | None = None

    async def _http(self) -> httpx.AsyncClient:
        """The HTTP client, built on first use: pinning fetches the gateway's certificate,
        a blocking handshake that runs off the event loop."""
        if self._client is None:
            verify: ssl.SSLContext | bool = True
            if self._pin is not None:
                verify = await asyncio.to_thread(pinned_context, self.address, self._pin, self.timeout)
            self._client = httpx.AsyncClient(base_url=self.address, headers=self._headers, verify=verify,
                                             timeout=self.timeout)
        return self._client

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()

    async def __aenter__(self) -> HttpDoorbell:
        return self

    async def __aexit__(self, *exc) -> None:
        await self.close()

    # ── the doorbell ────────────────────────────────────────────────────────

    async def poll(self, wait: float = 0) -> Wake | None:
        r = await (await self._http()).get("/v1/agents/me/wake", params={"wait": wait}, timeout=wait + TIMEOUT_SLACK)
        doc = self._answer(r)["wake"]
        return None if doc is None else decode_wake(doc)

    async def read(self, wake_id: str) -> tuple[Wake, list[Post]]:
        r = await (await self._http()).get("/v1/agents/me/posts", params={"wake": wake_id})
        doc = self._answer(r)
        return decode_wake(doc["wake"]), [decode_post(p) for p in doc["posts"]]

    async def read_room(self, room: str, after: str | None = None) -> list[Post]:
        params = {"room": room} if after is None else {"room": room, "after": after}
        r = await (await self._http()).get("/v1/agents/me/posts", params=params)
        return [decode_post(p) for p in self._answer(r)["posts"]]

    async def send(self, reply: Reply, room: str | None = None) -> Sent:
        body = encode_reply(reply)
        if room is not None:
            body["room"] = room
        try:
            r = await (await self._http()).post("/v1/agents/me/posts", json=body)
        except (httpx.TimeoutException, httpx.ReadError, httpx.RemoteProtocolError):
            # the request may have reached the gateway; a connection that never opened
            # (ConnectError) did not, and propagates as the ordinary failure it is
            raise LostResponse() from None
        if r.status_code == 422:
            raise RefusedSend(r.json().get("reasons", r.json().get("error", "")))
        if r.status_code == 504:
            # the gateway reached the platform and got no answer: the same unknown as a timeout
            raise LostResponse()
        if r.status_code == 502:
            # the gateway filled in a wake's thread the reply left out (design §3.3)
            raise RefusedPost(r.json().get("thread") or reply.thread)
        answer = self._answer(r)
        doc = answer["post"]
        return Sent(None if doc is None else decode_post(doc), tuple(answer.get("warnings", ())))

    async def done(self, wake_id: str, answered: tuple[str, ...] = (), failed: str | None = None) -> bool:
        r = await (await self._http()).post("/v1/agents/me/done",
                                    json={"wake_id": wake_id, "answered": list(answered), "failed": failed})
        return bool(self._answer(r)["done"])

    async def bio(self, text: str) -> int:
        r = await (await self._http()).post("/v1/agents/me/bio", json={"text": text})
        return int(self._answer(r)["version"])

    async def attachment(self, handle: str) -> tuple[str, bytes]:
        """The file's name and bytes, by the handle a read printed."""
        r = await (await self._http()).get(f"/v1/agents/me/attachments/{handle}")
        if r.status_code != 200:
            self._answer(r)
        found = _FILENAME.search(r.headers.get("Content-Disposition", ""))
        return (found.group(1) if found else handle), r.content

    async def rules(self) -> tuple[int, str]:
        r = await (await self._http()).get("/v1/rules")
        doc = self._answer(r)
        return int(doc["version"]), str(doc["text"])

    # ── errors ──────────────────────────────────────────────────────────────

    @staticmethod
    def _answer(r: httpx.Response) -> dict:
        if r.status_code == 200:
            return r.json()
        try:
            message = r.json().get("error", r.text)
        except ValueError:
            message = r.text
        if r.status_code == 409:
            raise NoLiveLease(message)
        if r.status_code == 400:
            raise InvalidReply(message)
        raise DoorbellError(r.status_code, message)
