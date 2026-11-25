"""The doorbell API as a :class:`~coop.client.Doorbell` over HTTP (design §3.2). The
gateway's answers are decoded with the same wire module the gateway encodes with;
its error statuses become the exceptions in :mod:`coop.errors`, so an agent written
against the in-process doorbell runs unchanged here.

A ``send`` is never retried (design §3.3): a timeout raises :class:`LostResponse`
and the agent checks the room before posting again.
"""

from __future__ import annotations

import asyncio
import json
import re
import secrets
import ssl
from collections.abc import AsyncIterator, Callable
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
from ..room.attachments import AttachmentRefused
from ..room.model import Post, Reply, Sent, Upload, Wake
from ..room.platform import RefusedAttachment, RefusedPost
from ..wire import decode_post, decode_wake, encode_reply

# The long-poll wait a client asks for when none is given, plus the slack the HTTP
# timeout allows over it.
_FILENAME = re.compile(r'filename="([^"]*)"')
_FILENAME_UNSAFE = re.compile(r'[\x00-\x1f\x7f"\\]')
DEFAULT_WAIT = 30.0
TIMEOUT_SLACK = 10.0
# How long a request that carries a file may take to answer, and how long a download
# may stall between chunks: apart from the request timeout, which is sized for JSON.
DEFAULT_TRANSFER_TIMEOUT = 120.0


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

    def __init__(self, address: str, token: str, fingerprint: str | None = None, timeout: float = 30.0,
                 transfer_timeout: float = DEFAULT_TRANSFER_TIMEOUT) -> None:
        self.address = address.rstrip("/")
        self.timeout = timeout
        self.transfer_timeout = transfer_timeout
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
            if reply.files:
                # the reply first, then each file streamed in turn, so the gateway checks
                # the rules before a byte of a file is read (design §3.3)
                boundary = secrets.token_hex(16)
                r = await (await self._http()).post(
                    "/v1/agents/me/posts", content=_multipart(boundary, body, reply.files),
                    headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
                    timeout=httpx.Timeout(self.timeout, read=self.transfer_timeout, write=self.transfer_timeout))
            else:
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
        if r.status_code in (413, 408):
            # a file over the limits, or one whose transfer ran out of time: nothing was posted
            raise AttachmentRefused(r.json().get("error", ""))
        if r.status_code == 502 and r.json().get("file") is not None:
            # the platform refused a file: nothing was posted, the thread is open
            raise RefusedAttachment(r.json()["file"], r.json().get("error", ""))
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

    async def download(self, handle: str, write: Callable[[bytes], None]) -> str:
        """Stream the file a read printed ``handle`` for to ``write``, chunk by chunk,
        and return its name. A transfer the gateway cut short raises
        ``httpx.RemoteProtocolError`` from the chunk it broke at: the caller discards
        what it wrote."""
        timeout = httpx.Timeout(self.timeout, read=self.transfer_timeout)
        async with (await self._http()).stream("GET", f"/v1/agents/me/attachments/{handle}", timeout=timeout) as r:
            if r.status_code != 200:
                await r.aread()
                if r.status_code in (413, 408):
                    raise AttachmentRefused(r.json().get("error", ""))
                self._answer(r)
            found = _FILENAME.search(r.headers.get("Content-Disposition", ""))
            async for chunk in r.aiter_bytes():
                write(chunk)
        return found.group(1) if found else handle

    async def attachment(self, handle: str) -> tuple[str, bytes]:
        """The file's name and bytes, by the handle a read printed; :meth:`download`
        streams instead of holding the file."""
        parts: list[bytes] = []
        name = await self.download(handle, parts.append)
        return name, b"".join(parts)

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


async def _multipart(boundary: str, body: dict, files: tuple[Upload, ...]) -> AsyncIterator[bytes]:
    """A multipart body the doorbell reads part by part: the reply as JSON, then each
    file's chunks as they come, nothing held."""
    yield (f'--{boundary}\r\nContent-Disposition: form-data; name="reply"\r\n'
           f"Content-Type: application/json\r\n\r\n").encode() + json.dumps(body).encode() + b"\r\n"
    for upload in files:
        name = _FILENAME_UNSAFE.sub("", upload.name)
        yield (f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="{name}"\r\n'
               f"Content-Type: {upload.content_type or 'application/octet-stream'}\r\n\r\n").encode()
        async for chunk in upload.chunks:
            yield chunk
        yield b"\r\n"
    yield f"--{boundary}--\r\n".encode()
