"""The doorbell API as a :class:`~coop.client.Doorbell` over HTTP (design §3.2). The
gateway's answers are decoded with the same wire module the gateway encodes with;
its error statuses become the exceptions in :mod:`coop.errors`, so an agent written
against the in-process doorbell runs unchanged here.

A ``send`` is never retried (design §3.3): a timeout raises :class:`LostResponse`
and the agent checks the room before posting again.
"""

from __future__ import annotations

import ssl

import httpx

from ..errors import DoorbellError, InvalidReply, LostResponse, NoLiveLease, RefusedSend
from ..room.model import Post, Reply, Wake
from ..room.platform import RefusedPost
from ..wire import decode_post, decode_wake, encode_reply

# The long-poll wait a client asks for when none is given, plus the slack the HTTP
# timeout allows over it.
DEFAULT_WAIT = 30.0
TIMEOUT_SLACK = 10.0


class HttpDoorbell:
    def __init__(self, address: str, token: str, verify: ssl.SSLContext | bool = True,
                 timeout: float = 30.0) -> None:
        self.address = address.rstrip("/")
        self.timeout = timeout
        self._client = httpx.AsyncClient(base_url=self.address, headers={"Authorization": f"Bearer {token}"},
                                         verify=verify, timeout=timeout)

    async def close(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> HttpDoorbell:
        return self

    async def __aexit__(self, *exc) -> None:
        await self.close()

    # ── the doorbell ────────────────────────────────────────────────────────

    async def poll(self, wait: float = 0) -> Wake | None:
        r = await self._client.get("/v1/agents/me/wake", params={"wait": wait}, timeout=wait + TIMEOUT_SLACK)
        doc = self._answer(r)["wake"]
        return None if doc is None else decode_wake(doc)

    async def read(self, wake_id: str) -> tuple[Wake, list[Post]]:
        r = await self._client.get("/v1/agents/me/posts", params={"wake": wake_id})
        doc = self._answer(r)
        return decode_wake(doc["wake"]), [decode_post(p) for p in doc["posts"]]

    async def read_room(self, room: str) -> list[Post]:
        r = await self._client.get("/v1/agents/me/posts", params={"room": room})
        return [decode_post(p) for p in self._answer(r)["posts"]]

    async def send(self, reply: Reply, room: str | None = None) -> Post | None:
        body = encode_reply(reply)
        if room is not None:
            body["room"] = room
        try:
            r = await self._client.post("/v1/agents/me/posts", json=body)
        except httpx.TimeoutException:
            raise LostResponse() from None
        if r.status_code == 422:
            raise RefusedSend(r.json().get("reasons", r.json().get("error", "")))
        if r.status_code == 502:
            raise RefusedPost(reply.thread)
        doc = self._answer(r)["post"]
        return None if doc is None else decode_post(doc)

    async def done(self, wake_id: str, answered: tuple[str, ...] = (), failed: str | None = None) -> bool:
        r = await self._client.post("/v1/agents/me/done",
                                    json={"wake_id": wake_id, "answered": list(answered), "failed": failed})
        return bool(self._answer(r)["done"])

    async def bio(self, text: str) -> int:
        r = await self._client.post("/v1/agents/me/bio", json={"text": text})
        return int(self._answer(r)["version"])

    async def rules(self) -> tuple[int, str]:
        r = await self._client.get("/v1/rules")
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
