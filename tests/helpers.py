"""Shared test helpers.

**Fixtures live here by default** (see `CLAUDE.md`, *Test Fixtures Are Shared By
Default*). A local copy in one suite is the exception and needs a reason in the
same breath — "it is small" is not one, because small fixtures duplicate just as
expensively. Adding one attribute to `WatcherLifecycle` once broke nineteen
tests at a stroke, every one of them a hand-built object missing a field no real
instance can lack.

The builders below run the **real constructors** and take keyword overrides for
the collaborators a test needs to substitute. That is the point: an object built
through `__init__` cannot be in a state no code path can produce, so it fails
where it is wrong rather than three layers away.
"""

from __future__ import annotations

import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from gateway.agents import AgentBackend
from gateway.agents.response import AgentResponse
from gateway.config import AgentConfig, WatcherConfig
from gateway.core.config import CoreConfig
from gateway.core.connector import Room
from gateway.core.session_manager import SessionManager
from gateway.core.watcher_manager import RoomRef
from gateway.core.watcher_rule import RoomKind

# Patch load_state/save_state globally so tests never touch live state files.
_patch_load_state = patch("gateway.core.state_store.load_state", return_value=[])
_patch_save_state = patch("gateway.core.state_store.save_state")


class IsolatedTestCase(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        _patch_load_state.start()
        _patch_save_state.start()
        self.addCleanup(_patch_load_state.stop)
        self.addCleanup(_patch_save_state.stop)


class MockAgentBackend(AgentBackend):
    """The shared agent double: canned responses, recorded calls, optional error.

    A superset of the three near-identical copies it replaces, so no caller
    loses anything::

        agent = MockAgentBackend(responses=["Hello!", "World!"])
        agent.side_effect = asyncio.TimeoutError   # make send() raise
        agent.sent_messages[0]["prompt"]
        agent.created_sessions[0]["working_directory"]
    """

    def __init__(
        self, responses=None, default_response: str = "mock reply",
        id_prefix: str = "mock-session",
    ) -> None:
        self._responses = list(responses or [])
        self._default_response = default_response
        # Session ids are `<id_prefix>-NNNN`. Two backends built in one test
        # (a reload rebuilds an agent) would otherwise mint the same ids and
        # read as "the same session" to code comparing them.
        self._id_prefix = id_prefix
        self.side_effect: type[Exception] | None = None

        # Captured call records for assertions.
        self.created_sessions: list[dict] = []
        self.sent_messages: list[dict] = []
        self.deleted_sessions: list[str] = []

        self._session_counter = 0

    async def create_session(
        self, working_directory, extra_args=None, session_title=None
    ) -> str:
        self._session_counter += 1
        session_id = f"{self._id_prefix}-{self._session_counter:04d}"
        self.created_sessions.append(
            {
                "session_id": session_id,
                "working_directory": working_directory,
                "extra_args": extra_args,
                "session_title": session_title,
            }
        )
        return session_id

    async def send(
        self, session_id, prompt, working_directory, timeout,
        attachments=None, env=None, append_system_prompt_file=None,
    ) -> AgentResponse:
        self.sent_messages.append(
            {
                "session_id": session_id,
                "prompt": prompt,
                "working_directory": working_directory,
                "timeout": timeout,
                "attachments": attachments,
                "env": env,
            }
        )
        if self.side_effect is not None:
            raise self.side_effect()
        text = self._responses.pop(0) if self._responses else self._default_response
        return AgentResponse(text=text)

    async def ensure_durable_instructions(self, *a, **kw):
        """Skip the default send()-based fallback, so watcher startup does not
        consume a canned response. A test exercising context injection should
        override this rather than rely on it (see
        tests/integration/test_injected_context_builder.py)."""
        return None


class CleanupTrackingAgent(MockAgentBackend):
    """`MockAgentBackend` that also *confirms* session deletion.

    Kept separate on purpose. `AgentBackend.delete_session` returns `False` by
    default — "deletion is unsupported or could not be confirmed" — and startup
    rollback branches on that: an unconfirmed delete keeps the session and its
    injection flag for the next attempt. Folding confirmation into the generic
    double would quietly move every rollback test onto the other branch while
    still passing, which is exactly the "reuse must not fuse two independent
    things" case in CLAUDE.md.
    """

    async def delete_session(self, session_id: str) -> bool:
        self.deleted_sessions.append(session_id)
        return True


def make_rc_config(
    server_url: str = "http://chat.example.com",
    username: str = "bot",
    password: str = "pw",
    name: str = "rc",
    owners=None,
    **overrides,
):
    """A minimal `RocketChatConfig` — lifted from `test_connector.py` when the
    wake-path suite became its second consumer."""
    from gateway.config import AttachmentConfig
    from gateway.connectors.rocketchat.config import RocketChatConfig

    return RocketChatConfig(
        server_url=server_url,
        username=username,
        password=password,
        name=name,
        owners=owners or ["alice"],
        attachments=AttachmentConfig(cache_dir_global="/tmp/rc-cache"),
        **overrides,
    )


def make_watcher(room="script", name=None, connector="script", agent="default", **kw):
    """A `WatcherConfig` with the fields most tests do not care about filled in."""
    return WatcherConfig(
        name=name or room, connector=connector, room=room, agent=agent, **kw
    )


async def start_watcher(lifecycle, wc, state=None, **kw):
    """Start a watcher from a `WatcherConfig`, for tests of the start machinery.

    The port of the deleted `_start_watcher`: resolve the configured room
    reference through the connector, then enter the one start path
    (`start_watcher_in_room`). Production has no name-resolving start left —
    the manager's create/recreate and `_resume_record` all arrive holding a
    room — but the start machinery's own behaviours (session provisioning,
    identity comparison, history handoff, rollback) are start-path
    properties, and these suites exercise them without needing the routing
    stack above.
    """
    room = await lifecycle._connector.resolve_room(wc.room)
    await lifecycle.start_watcher_in_room(wc, state, room, **kw)


def make_rule(room="script", name=None, connector="default", agent="default", **kw):
    """A `WatcherRule` naming one literal room — the post-cutover analogue of
    `make_watcher` for suites that start watchers at boot: on an eager
    connector (Script) the eager-start loop materializes it at `run_once`,
    creating a watcher named `<connector>-<room>` (the derived label)."""
    from gateway.core.room_pattern import RoomPattern
    from gateway.core.watcher_rule import RoomMatcher, WatcherRule

    return WatcherRule(
        name=name or f"rule-{room}", connector=connector, agent=agent,
        rooms=RoomMatcher(include=(RoomPattern(room),)), **kw,
    )


def make_rule_derived_record(
    name="w1",
    *,
    room_id=None,
    idle_days=15,
    expire_days=15,
    connector="rc",
    agent="default",
    session_id="sess-1",
    **kw,
):
    """A `WatcherState` as the watcher manager persists it: frozen rule AND
    materialized config, so `config_from_record` accepts it. Added when the
    membership-events suite became the second place to need one (the idle-sweep
    suite's local builder predates the config snapshot and pins TTL arithmetic,
    which never reads it)."""
    from gateway.core.state import WatcherState, backend_identity

    defaults = {
        "watcher_name": name,
        "session_id": session_id,
        "room_id": room_id or f"room-{name}",
        "room_type": "channel",
        "connector": connector,
        "agent": agent,
        "rule_name": "eng",
        "rule": {"session_idle_days": idle_days, "session_expire_days": expire_days},
        "config": {"name": name, "connector": connector, "room": "", "agent": agent},
        # Faithful to what a real start writes (round 19): a record WITH a
        # session always carries the backend identity it was minted against —
        # an empty one now reads "unverifiable" at reclaim and skips the
        # agent-bound cleanup, which is the corruption case, not the default.
        "backend_identity": backend_identity(
            AgentConfig().type, AgentConfig().working_directory),
    }
    defaults.update(kw)
    return WatcherState(**defaults)


def patch_persisted(records):
    """Patch the state store to hand back `records` at the next load.

    A context manager; the module-level `IsolatedTestCase` patch returns `[]`,
    this one returns something — what a booted manager hydrates."""
    return patch("gateway.core.state_store.load_state", return_value=list(records))


def isolate_runtime_dir(testcase):
    """Give a test its own `RUNTIME_DIR` under a temp dir; returns `(tmp, runtime)`.

    Cleaned up with the test. For tests that build a real `GatewayService` or
    touch `state.*.json` files on disk."""
    import tempfile
    from pathlib import Path

    import gateway.core.state as state_mod

    holder = tempfile.TemporaryDirectory()
    testcase.addCleanup(holder.cleanup)
    tmp = Path(holder.name)
    runtime = tmp / "runtime"
    runtime.mkdir()
    patcher = patch.object(state_mod, "RUNTIME_DIR", runtime)
    patcher.start()
    testcase.addCleanup(patcher.stop)
    return tmp, runtime


def gateway_config_text(
    *, connectors=("script",), agents=None, rules=None, working_directory=".", extra="",
) -> str:
    """The YAML of a minimal `config.yaml`, parameterized — the one hand-built shape.

    Script connectors only (constructible with no network or subprocess). `agents`
    is `{name: {field: value}}` (default: one claude agent named `default`);
    `rules` is a list of `{name, agent, connector, rooms, ...}` dicts (default:
    rule `w1` on room `script` of the first connector). `extra` is appended
    verbatim, for top-level keys such as `max_queue_depth`.
    """
    import yaml

    agents = agents if agents is not None else {
        "default": {"type": "claude", "working_directory": str(working_directory)}}
    rules = rules if rules is not None else [{
        "name": "w1", "agent": "default", "connector": connectors[0],
        "rooms": {"include": ["script"]}}]
    doc = {
        "connectors": [{"name": name, "type": "script"} for name in connectors],
        "agents": agents,
        "watcher_rules": rules,
    }
    return yaml.safe_dump(doc, sort_keys=False) + extra


def write_gateway_config(tmp, connector_name="script", *, working_directory=None, text=None):
    """Write and load a minimal `config.yaml` under `tmp` — the one hand-built config.

    One script connector, one claude agent, one rule (`gateway_config_text`), or
    `text` verbatim when a test needs another shape."""
    from gateway.config import GatewayConfig

    path = tmp / "config.yaml"
    path.write_text(text if text is not None else gateway_config_text(
        connectors=(connector_name,), working_directory=working_directory or tmp))
    return GatewayConfig.from_file(str(path))


async def boot_gateway_service(testcase, tmp, runtime, config, *, config_path=None):
    """Boot a real `GatewayService` on `config` and return it once its control
    socket is up; torn down with the test.

    Every agent backend is a `MockAgentBackend` (`_build_agent_backend` is
    patched for the test's whole life, so a reload rebuilding an agent gets a
    mock too); the control socket and `jobs.json` live under `runtime`. The
    caller has already isolated `RUNTIME_DIR` (`isolate_runtime_dir`). This is
    the seam the reload tests drive: `service._control.dispatch_command`.
    """
    import asyncio

    from gateway.core.job_store import JobStore
    from gateway.service import GatewayService

    built: list[int] = []

    def _backend(cfg):
        # Distinct ids per backend generation (see `MockAgentBackend.id_prefix`).
        built.append(len(built))
        return MockAgentBackend(id_prefix=f"mock-{len(built)}")

    patches = [
        patch("gateway.service._build_agent_backend", side_effect=_backend),
        # A stop that fails is retried a few seconds apart; tests need not wait.
        patch("gateway.core.retry_stop.STOP_RETRY_DELAY", 0.0),
        patch("gateway.control.CONTROL_SOCK", runtime / "control.sock"),
        patch("gateway.service.JobStore", side_effect=lambda: JobStore(runtime / "jobs.json")),
    ]
    for p in patches:
        p.start()
        testcase.addCleanup(p.stop)

    service = GatewayService(config, config_path=str(config_path or tmp / "config.yaml"))
    task = asyncio.create_task(service.run())

    async def _teardown():
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):
            pass

    testcase.addAsyncCleanup(_teardown)
    for _ in range(200):
        if service._control._server is not None:
            return service
        if task.done():
            task.result()  # raises the boot failure
        await asyncio.sleep(0.02)
    raise AssertionError("gateway service did not come up")


def make_record_from_rule(rule, room, *, session_id="sess-1", connector=None,
                          now="2026-09-01T00:00:00-07:00", **overrides):
    """A `WatcherState` written the way creation writes it.

    `materialize` the rule for the room, then `creation_provenance` for the
    frozen fields.

    Unlike `make_rule_derived_record`, whose `rule` snapshot is a two-key stub,
    this record carries the real snapshot — what reconciliation compares
    against the current rules — so a test can change one rule field and see
    exactly that field's consequence. `overrides` land last (e.g.
    `dropped_at=...` for a dormant record, `paused=True`).
    """
    from gateway.core.state import WatcherState, backend_identity
    from gateway.core.watcher_manager import creation_provenance, materialize

    wc = materialize(rule, room)
    # A started watcher records the backend it provisioned its session against;
    # a record without it never reuses its session (`_provision_session`).
    # `make_core_config`'s agents are default `AgentConfig()`s, so that is the
    # identity a real start would have written here.
    default_identity = backend_identity(AgentConfig().type, AgentConfig().working_directory)
    provenance = creation_provenance(
        wc, rule, room,
        connector_name=connector or rule.connector,
        agent_name=rule.agent,
        now=now,
    )
    fields = dict(
        watcher_name=wc.name,
        session_id=session_id,
        room_id=room.id,
        room_name=room.name,
        backend_identity=default_identity,
        **provenance,
    )
    fields.update(overrides)
    return WatcherState(**fields)


def make_core_config(timeout: int = 10, agents=None, **kw):
    """A `CoreConfig` with one agent, which is what most tests need."""
    return CoreConfig(
        agents=agents or {"default": AgentConfig(timeout=timeout)},
        **kw,
    )


def make_manager(
    connector=None,
    agent=None,
    *,
    timeout: int = 10,
    permission_registry=None,
    state_name: str = "default",
    agents=None,
    # The name the single default agent is registered under. Not a
    # `default_agent:` fallback — that config key is gone; this only decides the
    # key in the `agents` dict a rule would have to name explicitly.
    agent_name: str = "default",
    config=None,
    **kw,
) -> SessionManager:
    """A `SessionManager` wired to one connector and one agent.

    Replaces three near-identical copies that differed only by which of
    `timeout`, `permission_registry` and `state_name` they exposed.
    """
    from gateway.connectors.script import ScriptConnector

    connector = ScriptConnector() if connector is None else connector
    agent = MockAgentBackend() if agent is None else agent
    return SessionManager(
        connector,
        agents or {agent_name: agent},
        config or make_core_config(timeout=timeout),
        state_name=state_name,
        permission_registry=permission_registry,
        **kw,
    )


async def settle_routing_tasks(connector):
    """Wait until every spawned routing episode has finished AND been discarded.

    Not a bare `while set: gather` — that livelocks: awaiting an already-done
    task returns **without yielding**, and the `discard` runs in a done-callback
    that needs loop time it then never gets. The whole test spins as one giant
    callback (caught on Python 3.13, where the runner's debug mode reported a
    single 120-second callback; 3.11 only ever dodged it by timing). The
    `sleep(0)` is the yield that lets the callbacks drain the set.
    """
    import asyncio

    while connector._routing_tasks:
        await asyncio.gather(*connector._routing_tasks)
        await asyncio.sleep(0)


def make_bare_gateway_service(**attrs):
    """A `GatewayService` built without `__init__`, every collaborator a double.

    For tests that drive `run()`/`shutdown()` against mocked phases (the
    handshake pipe, the identity barrier's ordering). Sets every attribute
    `__init__` sets — a hand-built subset describes an object no code path
    produces, and the first method to read an unset field fails several layers
    from the cause; two copies of this fixture once broke together when
    `_agent_errors` arrived. `attrs` override any of them.
    """
    import asyncio
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from gateway.service import GatewayService

    svc = GatewayService.__new__(GatewayService)
    svc._config = None
    svc._config_path = None
    svc._config_digest = ""
    svc._config_loaded_at = ""
    svc._reload_lock = asyncio.Lock()
    svc._reloading = False
    svc._shutdown_holds_reload_lock = False
    svc._notifier = None
    svc._agent_errors = {}
    svc._core_config = MagicMock()
    svc._registry = MagicMock()
    svc._maps = SimpleNamespace(connector_view={})
    svc._expiry_task = None
    svc._scheduler_task = None
    svc._agents = {}
    svc._runtime_manager = MagicMock()
    svc._runtime_manager.start_all = AsyncMock(return_value=[])
    svc._runtime_manager.has_active_brokers = False
    svc._runtime_manager.unavailable_agents = set()
    svc._runtime_manager.stop_all = AsyncMock()
    svc._runtime_manager.retry_leftovers = AsyncMock()
    svc._runtime_manager.leftovers = []
    svc._leftover_entries = []
    svc._dm_claims = {}
    svc._entries = []
    svc._job_store = None
    svc._session_managers = {}
    svc._job_scheduler = MagicMock()
    svc._job_scheduler.fire_lock = asyncio.Lock()
    svc._control = MagicMock()
    svc._control.start = AsyncMock()
    svc._control.stop = AsyncMock()
    for name, value in attrs.items():
        setattr(svc, name, value)
    return svc


def make_connector_config(name="rc", type="rocketchat", **server):
    """A `ConnectorConfig` with a `server:` block — `server` keys land in `raw`."""
    from gateway.config import ConnectorConfig

    return ConnectorConfig(name=name, type=type,
                           raw={"server": {"url": "https://rc.example", **server}})


def make_gateway_config(connectors=None, agents=None, rules=None, **kw):
    """A `GatewayConfig` through the real constructor: one rocketchat connector
    `rc`, one agent `a`, one rule `eng` on room `eng` unless overridden."""
    from gateway.config import GatewayConfig

    return GatewayConfig(
        connectors=connectors if connectors is not None else [make_connector_config()],
        agents=agents if agents is not None else {"a": AgentConfig(name="a")},
        watcher_rules=rules if rules is not None else [
            make_rule(room="eng", name="eng", connector="rc", agent="a")],
        **kw,
    )


def make_bare_session_manager(**attrs):
    """A `SessionManager` built without `__init__`, every collaborator a double.

    For method-level tests that pin one SessionManager method against mocks —
    the real constructor builds real collaborators, which is a different kind
    of test (`make_manager`). Sets every attribute `__init__` sets, because a
    hand-built subset describes an object no code path produces, and the first
    method to read an unset field fails several layers from the cause — the
    `WatcherLifecycle.__new__` lesson, re-learned on SessionManager when
    `_sweep` arrived and seven tests across two files broke at once.

    A drift test (`test_session_manager_commands`) compares this against a
    real instance, so the next `__init__` field fails there, once, with a
    message that says what to do.
    """
    from unittest.mock import AsyncMock

    from gateway.core.session_manager import SessionManager

    mgr = SessionManager.__new__(SessionManager)
    mgr._connector = MagicMock()
    mgr._connector.connect = AsyncMock()
    mgr._connector.start_inbound = AsyncMock()
    mgr._connector.disconnect = AsyncMock()
    mgr._connector_name = "default"
    mgr._dispatcher = MagicMock()
    mgr._injector = MagicMock()
    mgr._state_store = MagicMock()
    mgr._lifecycle = MagicMock()
    mgr._lifecycle.sync_watchers = AsyncMock(return_value=[])
    mgr._lifecycle.stop_all = AsyncMock()
    mgr._deferred_membership = []
    mgr._quiesced = False
    mgr._lifecycle.drain_verbs = AsyncMock()
    mgr._lifecycle.pause_watcher = AsyncMock()
    mgr._lifecycle.resume_watcher = AsyncMock()
    mgr._lifecycle.reset_watcher = AsyncMock()
    # No rules → no creation path and no sweep; tests that exercise either
    # override these two together, the way __init__ gates them together.
    mgr._watcher_manager = None
    mgr._sweep = None
    mgr._cancel_jobs = None
    mgr._watcher_rules = []
    mgr._records_settled = False
    for name, value in attrs.items():
        setattr(mgr, name, value)
    return mgr


def make_processor(agent=None, **overrides):
    """A real `MessageProcessor` with a double for every collaborator.

    Runs the real constructor and takes keyword overrides for whatever the
    test substitutes — the same shape as `make_lifecycle`, added when the
    idle-clock suite became the third file to need one.
    """
    from gateway.core.message_processor import MessageProcessor

    connector = MagicMock()
    connector.send_text = AsyncMock()
    connector.format_prompt_prefix = MagicMock(return_value="")
    connector.notify_typing = AsyncMock()
    defaults = {
        "session_id": "ses_001",
        "room": Room(id="room_1", name="test-room"),
        "working_directory": "/tmp",
        "watcher_id": "test-watcher",
        "connector": connector,
        "agent": agent or MockAgentBackend(),
        "config": make_core_config(),
        "agent_name": "default",
    }
    defaults.update(overrides)
    return MessageProcessor(**defaults)


def make_lifecycle(**overrides):
    """A real `WatcherLifecycle` with a double for every collaborator.

    Built through `__init__`, deliberately: the thirteen hand-rolled
    `WatcherLifecycle.__new__(...)` fixtures this replaces each assigned their
    own subset of attributes, so every new field broke all of them at once and
    each was free to omit one and produce a state no code path can reach.

    Pass a keyword for anything the test actually exercises; everything else is
    a `MagicMock` that will fail loudly if the test unexpectedly depends on it.
    """
    from gateway.core.watcher_lifecycle import WatcherLifecycle

    defaults = {
        "connector": MagicMock(),
        "agents": {},
        "config": make_core_config(),
        # `load()` must return a real empty mapping, not a MagicMock. A bare
        # mock's `.get(name)` is truthy, so `sync_watchers` reads every watcher
        # as paused, starts none of them, and returns no errors — a lifecycle
        # test built on that passes without exercising startup at all, which is
        # the precise failure this file exists to prevent.
        "state_store": MagicMock(load=MagicMock(return_value={})),
        "dispatcher": MagicMock(),
        "injector": MagicMock(),
        "permission_registry": None,
        "maps": MagicMock(),
    }
    defaults.update(overrides)
    return WatcherLifecycle(**defaults)


# ── Installing records into a WatcherLifecycle ────────────────────────────────
#
# `WatcherLifecycle` keys its records and processors by ROOM ID and finds them
# by name through an index. A test that wrote `lifecycle._states[name] = r`
# produced a state no code path can reach (a record under the wrong key, with
# no name index) — and there were nine of them across six files. These go
# through the lifecycle's own single write points instead.


def install_record(lifecycle, record, *, as_name=None, processor=None):
    """Install `record` the way the lifecycle does, optionally with a resident
    processor. `as_name` is the key the test used to write under — kept so a
    fixture whose key disagreed with the record's own name fails loudly here
    rather than silently testing a record nothing could find."""
    if as_name is not None and as_name != record.watcher_name:
        raise AssertionError(
            f"fixture installed {record.watcher_name!r} under key {as_name!r}")
    lifecycle._install(record)
    if processor is not None:
        lifecycle._set_processor(record.watcher_name, processor)
    return record


def register_processor(lifecycle, name, processor):
    """Make `processor` resident for the watcher `name` (record must exist)."""
    lifecycle._set_processor(name, processor)
    return processor


def pop_processor(lifecycle, name):
    return lifecycle._pop_processor(name)


def evict_record(lifecycle, name):
    """Drop the record and any processor under `name`, as a reclaim would."""
    lifecycle._pop_processor(name)
    return lifecycle._uninstall(name)


ENG_ROOM = RoomRef(id="eng-backend", kind=RoomKind.CHANNEL, name="eng-backend")
