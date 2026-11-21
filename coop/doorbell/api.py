"""The doorbell API (design §3.2): the :class:`~coop.room.gateway.Gateway` behind an
aiohttp application, one bearer token per agent.

The endpoints and what each answers:

```
GET  /v1/agents/me/wake?wait=S          {"wake": <wake> | null}; waits up to S seconds
GET  /v1/agents/me/posts?wake=W         {"wake": <wake>, "posts": [<post>]}
GET  /v1/agents/me/posts?room=R         {"posts": [<post>]}
POST /v1/agents/me/posts                {"post": <post> | null}; body is a reply, plus "room"
POST /v1/agents/me/done                 {"done": true | false}; body {wake_id, answered, failed}
POST /v1/agents/me/bio                  {"version": N}; body {text}
GET  /v1/rules                          {"version": N, "text": "..."}
GET  /v1/agents/me/attachments/{id}     the file, by the handle a read printed
```

Errors answer ``{"error": "..."}``: 401 for a missing or unknown token, 403 for a room
whose roster does not list the agent, 404 for a room the gateway does not watch, 409
for a wake id with no live lease, 400 for a reply with nothing postable, 422 for a
send the gateway refused (``reasons`` beside ``error``), 502 for a post the platform
refused. The token is compared in constant time and never logged.
"""

from __future__ import annotations

import asyncio
import hmac
import re
from dataclasses import dataclass, field
from pathlib import Path

from aiohttp import web

from ..errors import InvalidReply, NoLiveLease, RefusedSend, RoomUnresolved
from ..room.gateway import Gateway
from ..room.model import Post, Wake
from ..room.platform import RefusedPost
from ..wire import decode_reply, encode_post, encode_wake

DEFAULT_WAIT = 30.0
MAX_WAIT = 120.0
_HEADER_UNSAFE = re.compile(r'[\x00-\x1f\x7f"]')
DOORBELL = web.AppKey("doorbell", "_Doorbell")
AGENT = web.RequestKey("agent", str)


@dataclass
class _Doorbell:
    gateway: Gateway
    tokens: dict[str, str]  # token -> agent
    rules_text: str
    # One persistent listener per agent, set by every wake the gateway issues for it;
    # a long-poll waits on it (``Gateway.subscribe`` has no unsubscribe, so one per
    # request would leak).
    signals: dict[str, asyncio.Event] = field(default_factory=dict)

    def agent_of(self, token: str) -> str | None:
        found = None
        for candidate, agent in self.tokens.items():
            # every entry is compared, so the time taken does not say which one matched
            if hmac.compare_digest(candidate.encode(), token.encode()):
                found = agent
        return found

    def signal(self, agent: str) -> asyncio.Event:
        event = self.signals.get(agent)
        if event is None:
            event = self.signals[agent] = asyncio.Event()

            async def listener(wake: Wake) -> None:
                event.set()

            self.gateway.subscribe(agent, listener)
        return event


RULES = Path(__file__).resolve().parent.parent / "rules.md"


def build_app(gateway: Gateway, tokens: dict[str, str], rules_text: str | None = None) -> web.Application:
    """``tokens`` maps each bearer token to the agent it names. ``rules_text`` is the
    room-rules document (design §3.3); the package's ``rules.md`` unless given."""
    doorbell = _Doorbell(gateway, dict(tokens), RULES.read_text() if rules_text is None else rules_text)
    app = web.Application(middlewares=[_authenticate])
    app[DOORBELL] = doorbell
    app.router.add_get("/v1/agents/me/wake", wake)
    app.router.add_get("/v1/agents/me/posts", read)
    app.router.add_post("/v1/agents/me/posts", send)
    app.router.add_post("/v1/agents/me/done", done)
    app.router.add_post("/v1/agents/me/bio", bio)
    app.router.add_get("/v1/rules", rules)
    app.router.add_get("/v1/agents/me/attachments/{handle}", attachment)
    return app


@web.middleware
async def _authenticate(request: web.Request, handler):
    header = request.headers.get("Authorization", "")
    scheme, _, token = header.partition(" ")
    agent = request.app[DOORBELL].agent_of(token) if scheme == "Bearer" and token else None
    if agent is None:
        return _error(401, "unknown or missing bearer token")
    request[AGENT] = agent
    return await handler(request)


def _error(status: int, message: str, **more) -> web.Response:
    return web.json_response({"error": message, **more}, status=status)


def _posts(gateway: Gateway, room: str, posts: list[Post]) -> list[dict]:
    return [encode_post(gateway.deliverable(room, p)) for p in posts]


# ── handlers ────────────────────────────────────────────────────────────────


async def wake(request: web.Request) -> web.Response:
    """Long-poll the agent's next wake. The gateway stamps ``last_seen`` at arrival,
    the lease starts at the poll that picks the wake up (design §3.2)."""
    doorbell: _Doorbell = request.app[DOORBELL]
    agent = request[AGENT]
    try:
        wait = min(float(request.query.get("wait", DEFAULT_WAIT)), MAX_WAIT)
    except ValueError:
        return _error(400, "wait: a number of seconds")
    signal = doorbell.signal(agent)
    signal.clear()
    found = doorbell.gateway.poll(agent)
    if found is None and wait > 0:
        try:
            await asyncio.wait_for(signal.wait(), wait)
        except TimeoutError:
            pass
        if _client_gone(request):
            # the agent left while we waited: picking the wake up now would start a
            # lease nobody holds; it stays pending for the agent's next poll
            return web.json_response({"wake": None})
        found = doorbell.gateway.poll(agent)
    return web.json_response({"wake": None if found is None else encode_wake(found)})


def _client_gone(request: web.Request) -> bool:
    transport = request.transport
    return transport is None or transport.is_closing()


async def read(request: web.Request) -> web.Response:
    doorbell: _Doorbell = request.app[DOORBELL]
    gateway, agent = doorbell.gateway, request[AGENT]
    wake_id, room = request.query.get("wake"), request.query.get("room")
    if (wake_id is None) == (room is None):
        return _error(400, "read names a wake or a room, not both")
    if wake_id is not None:
        try:
            found, posts = gateway.read(agent, wake_id)
        except NoLiveLease as exc:
            return _error(409, str(exc))
        return web.json_response({"wake": encode_wake(found), "posts": _posts(gateway, found.room, posts)})
    refused = _room_refusal(gateway, agent, room)
    if refused is not None:
        return refused
    return web.json_response({"posts": _posts(gateway, room, gateway.read_room(agent, room))})


async def send(request: web.Request) -> web.Response:
    doorbell: _Doorbell = request.app[DOORBELL]
    gateway, agent = doorbell.gateway, request[AGENT]
    body = await _json(request)
    if body is None:
        return _error(400, "send: a JSON reply")
    problem = _typed(body, thread=str, to=list, text=str, control=dict, wake_id=str, room=str) or _control_typed(body)
    if problem is not None:
        return _error(400, f"send: {problem}")
    refused = _default_thread(gateway, agent, body)
    if refused is None and body.get("room") is not None:
        refused = _room_refusal(gateway, agent, body["room"])
    if refused is not None:
        return refused
    reply, room = decode_reply(body), body.get("room")
    try:
        core = gateway.room_for(agent, reply, room)
        post = await core.send(agent, reply)
    except (NoLiveLease, RoomUnresolved, InvalidReply, RefusedSend, RefusedPost) as exc:
        return _send_refusal(exc)
    return web.json_response({"post": None if post is None else encode_post(gateway.deliverable(core.room, post))})


def _default_thread(gateway: Gateway, agent: str, body: dict) -> web.Response | None:
    """A wake's send with no thread goes to the wake's starting thread; once the lease
    is gone the gateway no longer knows it, so a late send names its thread."""
    if body.get("thread") is not None:
        return None
    if not isinstance(body.get("wake_id"), str):
        return _error(400, "send: an unsolicited send names its room or thread")
    try:
        body["thread"] = gateway.starting_thread(agent, body["wake_id"])
    except NoLiveLease as exc:
        return _error(409, f"{exc}; pass the thread with a late send")
    return None


def _send_refusal(exc: Exception) -> web.Response:
    if isinstance(exc, NoLiveLease):
        return _error(409, f"{exc}; a late send names its room and thread")
    if isinstance(exc, RoomUnresolved):
        return _error(404, str(exc))
    if isinstance(exc, InvalidReply):
        return _error(400, str(exc))
    if isinstance(exc, RefusedSend):
        return _error(422, "refused", reasons=exc.reasons)
    return _error(502, str(exc), thread=getattr(exc, "thread", None))


async def done(request: web.Request) -> web.Response:
    doorbell: _Doorbell = request.app[DOORBELL]
    body = await _json(request)
    if body is None or not isinstance(body.get("wake_id"), str):
        return _error(400, "done: a JSON body with wake_id")
    problem = _typed(body, answered=list, failed=str)
    if problem is not None:
        return _error(400, f"done: {problem}")
    answered = tuple(body.get("answered") or ())
    failed = body.get("failed")
    ok = await doorbell.gateway.done(request[AGENT], body["wake_id"], answered, failed)
    return web.json_response({"done": ok})


async def bio(request: web.Request) -> web.Response:
    doorbell: _Doorbell = request.app[DOORBELL]
    body = await _json(request)
    if body is None or not isinstance(body.get("text"), str):
        return _error(400, "bio: a JSON body with text")
    return web.json_response({"version": doorbell.gateway.bio(request[AGENT], body["text"])})


async def rules(request: web.Request) -> web.Response:
    doorbell: _Doorbell = request.app[DOORBELL]
    return web.json_response({"version": doorbell.gateway.rules_version, "text": doorbell.rules_text})


async def attachment(request: web.Request) -> web.Response:
    doorbell: _Doorbell = request.app[DOORBELL]
    handle = request.match_info["handle"]
    try:
        found, data = await doorbell.gateway.fetch_attachment(request[AGENT], handle)
    except KeyError:
        return _error(404, f"no attachment {handle!r}: fetch by the handle a read printed")
    except PermissionError as exc:
        return _error(403, str(exc))
    name = _HEADER_UNSAFE.sub("", found.name)  # a control character or a quote would break the header
    return web.Response(body=data, content_type=found.content_type or "application/octet-stream",
                        headers={"Content-Disposition": f'attachment; filename="{name}"'})


# ── helpers ─────────────────────────────────────────────────────────────────


async def _json(request: web.Request) -> dict | None:
    try:
        body = await request.json()
    except ValueError:
        return None
    return body if isinstance(body, dict) else None


def _typed(body: dict, **types: type) -> str | None:
    """The first field whose value is present, not ``None`` and not of its type (a list
    must hold strings); ``None`` when every field is well typed."""
    for name, kind in types.items():
        value = body.get(name)
        if value is None:
            continue
        if not isinstance(value, kind) or (kind is list and not all(isinstance(v, str) for v in value)):
            return f"{name} must be {'a list of strings' if kind is list else 'a ' + kind.__name__}"
    return None


def _control_typed(body: dict) -> str | None:
    """A control's value is ``all`` or a list of names (section 5.3)."""
    for key, value in (body.get("control") or {}).items():
        if value != "all" and not (isinstance(value, list) and all(isinstance(n, str) for n in value)):
            return f"control {key} must be \"all\" or a list of strings"
    return None


def _room_refusal(gateway: Gateway, agent: str, room: str) -> web.Response | None:
    """404 for a room the gateway does not watch, 403 for one whose roster does not
    list the agent; ``None`` when the agent may act there."""
    if room not in gateway.rooms:
        return _error(404, f"no room {room!r}")
    if not gateway.hosts(agent, room):
        return _error(403, f"{agent} is not in the roster of room {room!r}")
    return None
