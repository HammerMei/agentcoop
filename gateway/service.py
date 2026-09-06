"""Gateway service: wires multiple Connectors + SessionManagers together.

This module is the top-level orchestration layer:
  - One ConnectorEntry per connector defined in config
  - Each ConnectorEntry has its own SessionManager with isolated state
  - A single unified control socket routes CLI commands to the right manager

Startup order
-------------
1-2. AgentRuntimeManager.start_all() — start all backends and permission brokers
     (ordering handled internally: backends first, then brokers)
3.   run_once()  — connect connectors and resume sessions

daemon.py and cli.py interface is unchanged:
    service = GatewayService(config)
    await service.run()
"""

import asyncio
import logging
import os
from dataclasses import dataclass

from .agents import AgentBackend, GatewayBrokerConfig, check_backend_signatures
from .agents.claude import ClaudeBackend
from .agents.opencode import OpenCodeBackend
from .config import AgentConfig, ConnectorConfig, GatewayConfig
from .config_diff import ConfigDiff, config_digest, diff_configs, redacted_config
from .config_validate import finding_to_dict, validate_config
from .connectors import connector_factory
from .control import ControlServer
from .core.bot_identity import (
    ConnectorIdentity,
    ConnectorIdentityError,
    DmClaim,
    DuplicateBotIdentityError,
    dm_claims,
    find_identity_conflicts,
    fold_record_dm_claims,
)
from .core.config import CoreConfig
from .core.connector import Connector
from .core.expiry_task import run_expiry_task
from .core.job_store import JobStore
from .core.permission import (
    ConnectorPermissionNotifier,
    PermissionBroker,
    PermissionRegistry,
)
from .core.reconcile import orphan_decisions
from .core.retry_stop import STOP_ATTEMPTS, stop_with_retries
from .core.scheduler import JobScheduler
from .core.session_manager import JOBS_CANCELLED_CONNECTOR_REMOVED, SessionManager
from .core.session_maps import SessionMaps
from .core.session_release import log_session_released
from .core.state import (
    check_session_uniqueness,
    check_state_formats,
    load_state,
    now_iso,
)
from .reload_plan import (
    SCOPE_REVALIDATION_NOTE,
    Degraded,
    ReloadPlan,
    connector_removed_changes,
    orphan_removals,
    plan_connector_records,
    plan_persisted_records,
)

logger = logging.getLogger("agent-chat-gateway.service")

# What a lifecycle verb or a room wake is told while a reload applies (#144).
RELOAD_IN_PROGRESS = "a config reload is in progress — retry when it finishes"


def _build_agent_backend(agent_cfg: AgentConfig) -> AgentBackend:
    """Instantiate the correct AgentBackend from an AgentConfig."""
    if not agent_cfg.permissions.enabled and agent_cfg.permissions.skip_owner_approval:
        logger.warning(
            "Agent '%s': permissions.skip_owner_approval=true has no effect because "
            "permissions.enabled=false — the permission broker is disabled entirely. "
            "Set permissions.enabled=true to activate skip_owner_approval.",
            agent_cfg.name,
        )
    broker_config = (
        GatewayBrokerConfig(
            owner_allowed_tools=agent_cfg.effective_owner_allowed_tools(),
            guest_allowed_tools=agent_cfg.effective_guest_allowed_tools(),
            timeout=agent_cfg.permissions.timeout,
            skip_owner_approval=agent_cfg.permissions.skip_owner_approval,
        )
        if agent_cfg.permissions.enabled
        else None
    )

    if agent_cfg.type == "claude":
        return ClaudeBackend(
            command=agent_cfg.command,
            new_session_args=agent_cfg.new_session_args,
            timeout=agent_cfg.timeout,
            broker_config=broker_config,
        )
    if agent_cfg.type == "opencode":
        # sidecar_env is intentionally hardcoded: the opencode sidecar process
        # always runs as "owner" because it is the gateway's own agent backend.
        # Per-message guest enforcement (tool allow-lists, permission prompts)
        # is handled by the PermissionBroker, not by environment variables.
        return OpenCodeBackend(
            command=agent_cfg.command,
            new_session_args=agent_cfg.new_session_args,
            timeout=agent_cfg.timeout,
            sidecar_env={"ACG_ROLE": "owner"},
            sidecar_cwd=agent_cfg.working_directory or None,
            broker_config=broker_config,
        )
    raise ValueError(
        f"Unknown agent type: {agent_cfg.type!r} (supported: 'claude', 'opencode')"
    )


class AgentRuntimeManager:
    """Manages per-agent backend + permission broker lifecycle.

    Encapsulates the startup ordering constraint (backends first, then brokers)
    and failure tracking so that :class:`GatewayService` never needs to know
    about internal sequencing or which agents are permission-enabled.
    """

    def __init__(self, agents: dict[str, AgentBackend]) -> None:
        self._agents = agents
        self._brokers: dict[str, PermissionBroker] = {}
        self._unavailable: set[str] = set()
        # Backends and brokers a reload replaced but could not stop (#144):
        # `(agent, kind, object, error)`. Tracked so the final shutdown tries
        # them again and `status` names them; nothing else is done to them.
        self._leftovers: list[tuple[str, str, object, str]] = []

    async def start_all(
        self,
        registry: PermissionRegistry,
        notifier: "ConnectorPermissionNotifier",
        maps: SessionMaps,
    ) -> list[str]:
        """Start all agent backends and their permission brokers.

        Ordering is handled internally:
          1. Start backends (e.g. ``opencode serve``).
          2. Start permission brokers — only for backends that succeeded.
             Broker creation uses the backend's resolved URL, so it must follow
             backend startup.

        Returns:
            List of human-readable error strings for any agent that failed.
        """
        return await self.start_some(set(self._agents), registry, notifier, maps)

    async def start_some(
        self,
        names: set[str],
        registry: PermissionRegistry,
        notifier: "ConnectorPermissionNotifier",
        maps: SessionMaps,
    ) -> list[str]:
        """`start_all` for a subset — what `config reload` starts after it has
        stopped and rebuilt the agents whose definition changed (#144). The
        unavailable set is updated for exactly these names: a failure adds,
        a success removes."""
        errors: list[str] = []
        failed_backends: set[str] = set()
        starting = {name: self._agents[name] for name in names if name in self._agents}

        # Phase 1: start backends
        async def _start_backend(
            name: str, backend: AgentBackend
        ) -> tuple[str, Exception | None]:
            try:
                await backend.start()
                return name, None
            except Exception as e:
                return name, e

        backend_results = await asyncio.gather(
            *[_start_backend(name, backend) for name, backend in starting.items()]
        )
        for name, err in backend_results:
            if err is not None:
                msg = f"Agent '{name}': backend failed to start — agent will be unavailable: {err}"
                logger.error(msg)
                errors.append(msg)
                failed_backends.add(name)

        # Phase 2: start permission brokers (skip agents with failed backends)
        failed_broker_agents: set[str] = set()
        for name in failed_backends:
            logger.debug("Agent '%s': skipping broker — backend failed to start", name)

        async def _start_broker(
            name: str, backend: AgentBackend
        ) -> tuple[str, PermissionBroker | None, Exception | None]:
            try:
                broker = backend.create_gateway_broker(
                    registry=registry,
                    notifier=notifier,
                    session_room_map=maps.room_view,
                    session_role_map=maps.role_view,
                    session_permission_thread_map=maps.permission_thread_view,
                )
                if broker is None:
                    return name, None, None
                await broker.start()
                return name, broker, None
            except Exception as e:
                return name, None, e

        broker_results = await asyncio.gather(
            *[
                _start_broker(name, backend)
                for name, backend in starting.items()
                if name not in failed_backends
            ]
        )
        for name, broker, err in broker_results:
            if err is None:
                if broker is None:
                    continue
                self._brokers[name] = broker
                logger.info("Agent '%s': permission broker started", name)
            else:
                msg = f"Agent '{name}': permission broker failed to start: {err}"
                logger.error(msg)
                errors.append(msg)
                failed_broker_agents.add(name)

        for name in failed_broker_agents:
            backend = self._agents.get(name)
            if not backend:
                continue
            try:
                await backend.stop()
            except Exception as e:
                logger.error(
                    "Agent '%s': backend stop after broker failure also failed: %s",
                    name,
                    e,
                )

        self._unavailable = (
            (self._unavailable - set(starting)) | failed_backends | failed_broker_agents
        )
        return errors

    async def stop_all(self) -> None:
        """Stop all brokers and backends (reverse of start order), then try the
        leftovers of earlier reloads once more. Best-effort: shutdown logs what
        would not stop and carries on."""
        await self.stop_some(set(self._agents))
        await self.retry_leftovers()

    async def stop_some(self, names: set[str]) -> dict[str, str]:
        """Stop the brokers and backends of `names` (reverse of start order).

        Each stop is retried (`stop_with_retries`); one that still raises is
        kept in `leftovers` — tracked, tried again at shutdown, named by
        `status` — and reported by agent name in the return value. A broker
        that would not stop counts as that agent failing, exactly like its
        backend: the reload replaces the agent regardless and says so; it
        does not roll back or try another way to kill anything.
        """
        failed: dict[str, str] = {}
        for name in [n for n in names if n in self._brokers]:
            broker = self._brokers.pop(name)
            err = await stop_with_retries(f"agent '{name}' permission broker", broker.stop)
            if err is not None:
                self._leftovers.append((name, "permission broker", broker, str(err)))
                failed[name] = (f"its permission broker did not stop after {STOP_ATTEMPTS} "
                                f"attempts: {err}")

        stopping = [(name, self._agents[name]) for name in names if name in self._agents]
        results = await asyncio.gather(*[
            stop_with_retries(f"agent '{name}' backend", backend.stop)
            for name, backend in stopping])
        for (name, backend), err in zip(stopping, results, strict=True):
            if err is not None:
                self._leftovers.append((name, "backend", backend, str(err)))
                msg = f"its backend did not stop after {STOP_ATTEMPTS} attempts: {err}"
                failed[name] = f"{failed[name]}; {msg}" if name in failed else msg
        return failed

    @property
    def leftovers(self) -> list[tuple[str, str, str]]:
        """`(agent, kind, error)` for every backend or broker still not stopped."""
        return [(name, kind, err) for name, kind, _obj, err in self._leftovers]

    async def retry_leftovers(self) -> None:
        """Try once more to stop what earlier reloads could not; drop what stops."""
        still: list[tuple[str, str, object, str]] = []
        for name, kind, obj, err in self._leftovers:
            again = await stop_with_retries(f"agent '{name}' leftover {kind}", obj.stop)
            if again is not None:
                still.append((name, kind, obj, str(again)))
        self._leftovers = still

    def replace(self, name: str, backend: AgentBackend | None) -> None:
        """Install a rebuilt backend under `name`, or drop the agent (`None`).

        Writes the dict the session managers hold — every lifecycle sees the
        new object on its next start. The caller stops the old one first
        (`stop_some`) and starts the new one after (`start_some`).
        """
        if backend is None:
            self._agents.pop(name, None)
            self._unavailable.discard(name)
        else:
            self._agents[name] = backend

    @property
    def unavailable_agents(self) -> set[str]:
        """Agent names that failed to start (backend or broker)."""
        return self._unavailable

    @property
    def has_active_brokers(self) -> bool:
        """True if at least one permission broker was started successfully."""
        return bool(self._brokers)


@dataclass
class ConnectorEntry:
    """A single connector instance paired with its dedicated SessionManager.

    `degraded` is set by `config reload` on an entry it could not bring back
    (#144): the connector failed to connect or to start its watchers. The
    entry stays in the list — `list` still answers for its records and
    `status` shows the error — with no processors and no automatic retry;
    the operator fixes the file and reloads again. Boot never sets it: a
    connector that cannot connect at boot stops the boot.
    """

    name: str
    connector: Connector
    session_manager: SessionManager
    degraded: str = ""


class GatewayService:
    """Top-level orchestrator: manages one ConnectorEntry per configured connector.

    Each connector gets its own SessionManager with isolated state
    (state.{name}.json).  A single unified control socket routes CLI
    commands to the correct manager by connector name.

    External interface (used by daemon.py) is unchanged:
        service = GatewayService(config)
        await service.run()
    """

    def __init__(self, config: GatewayConfig, config_path: str | None = None) -> None:
        # The ACTIVE configuration (#144): what the daemon is running, kept so
        # `config reload` diffs the file against it rather than reconstructing
        # it from runtime state, and so `status`/`config show` can name it.
        # `config_path` is where a reload re-reads from; None (tests, scripts)
        # means reload has nothing to read and refuses.
        self._config = config
        # Absolute, NOT resolved: a deployment that repoints a `config.yaml`
        # symlink expects the next reload to read the new target, and the CLI
        # to be accepted with the path it has always passed.
        self._config_path = os.path.abspath(config_path) if config_path else None
        self._config_digest = config_digest(config)
        self._config_loaded_at = now_iso()
        self._reload_lock = asyncio.Lock()
        self._reloading = False
        self._shutdown_holds_reload_lock = False
        self._notifier: ConnectorPermissionNotifier | None = None
        # Why each unavailable agent is unavailable — the start error, kept so
        # `status` can say more than "failed to start".
        self._agent_errors: dict[str, str] = {}
        # Connector instances a reload replaced but could not shut down (#144):
        # `(entry, error)`. Only ever DISCONNECTED again — at the next reload
        # and at shutdown — never saved again: their records belong to the
        # replacement now. `status` names them until they go.
        self._leftover_entries: list[tuple[ConnectorEntry, str]] = []
        # Preflight — and settle — the persisted state BEFORE building anything.
        # Two read-only checks (they raise) around one write (the orphan sweep
        # removes files no configured connector owns; see below for why it must
        # come between them). A state file this
        # build cannot read holds real sessions, and every path that would notice it
        # later is per-connector — so a file belonging to a connector no longer in
        # config.yaml would never be opened, and the daemon would start successfully
        # while abandoning every session in it. Raising here is the whole point of the
        # version marker: an unreadable file must stop the boot, not be discovered as
        # an absence. See gateway/core/state.py.
        check_state_formats()
        # Then let go of the files no configured connector owns (#143) — BEFORE the
        # uniqueness preflight below scans every file: an orphan left by renaming a
        # connector (state copied, old file kept) shares its session ids with the new
        # file, and a preflight that still read the orphan would refuse the boot for
        # records this sweep is about to release.
        self._reclaim_orphaned_state_files({c.name for c in config.connectors})
        # Before anything is built: a state file binding one session to two rooms is a
        # cross-room leak waiting for both watchers to start (§4.1). The runtime check
        # in `bind_session` catches it too, but only once one of them is already
        # answering, and which one wins would depend on start order.
        check_session_uniqueness()

        core_config = CoreConfig.from_gateway_config(config)
        # Held by every session manager and processor, and updated IN PLACE by a
        # reload: `max_queue_depth`, the agent and connector config maps.
        self._core_config = core_config

        # Shared permission registry (one per gateway instance)
        self._registry = PermissionRegistry()
        # Shared mutable maps between SessionManagers, brokers, and processors
        self._maps = SessionMaps()
        # Expiry background task handle
        self._expiry_task: asyncio.Task | None = None
        # Scheduler task handle
        self._scheduler_task: asyncio.Task | None = None

        # Build agents — runtime manager handles backend + broker lifecycle
        agents: dict[str, AgentBackend] = {
            name: _build_agent_backend(agent_cfg)
            for name, agent_cfg in config.agents.items()
        }
        # Fail here rather than at the first watcher start: a backend implementing the
        # pre-rename signature would otherwise raise a bare TypeError deep in the
        # lifecycle, which rolls the startup back and says nothing about what to change.
        check_backend_signatures(agents)

        # The ONE agents dict: the runtime manager, every session manager and
        # every lifecycle hold this same object, so a reload's replacement of
        # one backend is seen everywhere on the next start.
        self._agents = agents
        self._runtime_manager = AgentRuntimeManager(agents)

        # What each connector claims of its account's direct messages — a rule opting
        # in takes the whole stream, a static `@someone` watcher takes one channel.
        # Read once here rather than holding the whole config: the identity barrier
        # needs only this, and config is immutable after load. A DM has no team, so it
        # is the one thing the Mattermost different-teams exception cannot keep apart
        # (§4.5).
        self._dm_claims: dict[str, DmClaim] = dm_claims(config.watcher_rules)
        self._entries: list[ConnectorEntry] = [
            self._build_entry(cc, connector_factory(cc), config) for cc in config.connectors
        ]

        # Build JobStore + JobScheduler. The scheduler's mapping is THIS dict,
        # kept in step with `_entries` by `_install_entries` — a reload swaps
        # a connector's manager in both places at once.
        self._job_store = JobStore()
        self._session_managers = {e.name: e.session_manager for e in self._entries}
        self._job_scheduler = JobScheduler(
            store=self._job_store,
            session_managers=self._session_managers,
            completed_job_ttl_days=config.scheduler.completed_job_ttl_days,
        )

        self._control = ControlServer(
            self._entries,
            job_store=self._job_store,
            service=self,
        )

    def _build_entry(
        self, cc: ConnectorConfig, connector: Connector, config: GatewayConfig
    ) -> ConnectorEntry:
        """One connector and its session manager, from the config entry.

        Boot builds every entry this way; a reload builds the added and the
        restarted ones. The connector is passed in rather than built here so a
        reload can construct every new connector BEFORE it stops anything —
        a factory that raises must leave the running fleet untouched.
        """
        sm = SessionManager(
            connector=connector,
            agents=self._agents,
            config=self._core_config,
            state_name=cc.name,
            permission_registry=self._registry,
            session_maps=self._maps,
            # Rules give the manager runtime effect (§2.8). Filtered like the
            # watcher configs: a rule binds to one connector by name, and the
            # manager keys its matches on that same name.
            watcher_rules=config.rules_for(cc.name),
            # The membership-remove handler's job cancellation (§2.7).
            # The expiry-exemption oracle that used to sit here is gone with
            # the exemption itself: a job records the room it targets and can
            # resurrect it, so there is no record to protect on its behalf.
            cancel_jobs=(
                lambda room_id, legacy_handle, *, reason, advice, _cn=cc.name:
                    self._cancel_jobs_for(
                        _cn, room_id, legacy_handle=legacy_handle,
                        reason=reason, advice=advice)
            ),
        )
        return ConnectorEntry(name=cc.name, connector=connector, session_manager=sm)

    def _install_entries(self, entries: list[ConnectorEntry]) -> None:
        """Make `entries` the fleet, in place — the control server and the
        scheduler hold the list and the dict, not copies."""
        self._entries[:] = entries
        self._session_managers.clear()
        self._session_managers.update({e.name: e.session_manager for e in entries})

    @property
    def reloading(self) -> bool:
        """Whether a `config reload` is applying right now (#144)."""
        return self._reloading

    def _cancel_jobs_for(
        self, connector_name: str, room_id: str, *, legacy_handle: str,
        reason: str, advice: str,
    ) -> None:
        """Cancel every scheduled job targeting a reclaimed room (§2.7).

        Room id first and required; `legacy_handle` is keyword-only and named
        for what it is — the ONLY thing a job written before schema 2 has to be
        matched by. Nothing else here reads a handle (§2.8).

        Fired by the membership-remove handler after `reclaim_room` succeeds:
        the room can never receive another message, so a job left in the store
        would fire forever at nothing. Each cancellation is logged as an audit
        line with the reason — a job disappearing from `schedule list` must be
        explainable. Best-effort like the oracle: a store that cannot answer
        leaves the jobs, and each fires audibly against the missing room
        rather than being silently lost here.
        """
        try:
            configured = {e.name for e in self._entries}

            def _owner_of(job) -> str:
                """Which connector would DELIVER this job.

                The same order `JobScheduler._resolve_target` uses, and
                deliberately so: cancellation must claim exactly the jobs this
                connector would have fired. `job.connector` when it names a
                configured connector; otherwise the handle's prefix, the only
                other thing in a job that names one.
                """
                if job.connector in configured:
                    return job.connector
                return job.watcher.partition(":")[0]

            def _claims_this_room(job) -> bool:
                """Is this job THIS connector's job for THIS room?

                Both halves, because cancellation is destructive and
                unappealable. A room id does NOT establish ownership: ids are
                per-server, not per-connector, and the canonical multi-agent
                setup is one account per agent in the same rooms, so several
                connectors' jobs legitimately carry one room id.

                An earlier version admitted any job whose connector was not
                configured (`or j.connector not in configured`), on the argument
                that such a job is deliverable here through the fallback scan and
                so must be cancellable here. The argument was right and the
                clause too broad: a job belonging to a connector renamed away in
                `config.yaml` was deleted by a DIFFERENT connector's membership
                event, under an audit line saying the bot had been removed from
                the room — it had not been removed from that agent's account.
                Asking who would deliver it keeps the intent and drops the reach.
                """
                if _owner_of(job) != connector_name:
                    return False
                if job.room_id:
                    # A handle can have been taken over by a different room, and
                    # cancelling by it would delete a live room's jobs while
                    # leaving this room's own firing at a room the bot has left.
                    # Both directions were reachable.
                    return job.room_id == room_id
                # Pre-schema-2: the handle is the only key there is.
                return job.watcher == legacy_handle

            doomed = [j for j in self._job_store.list_jobs()
                      if _claims_this_room(j)]
            for job in doomed:
                self._job_store.cancel(job.id, reason=reason)
                logger.warning(
                    "AUDIT: cancelled scheduled job %s (watcher '%s', room %s, "
                    "connector '%s') — %s. The record is kept; %s",
                    job.id, job.watcher, room_id, job.connector, reason,
                    advice.format(job_id=job.id),
                )
        except Exception as e:
            logger.warning(
                "Could not cancel scheduled jobs for reclaimed room %s: %s",
                room_id, e,
            )

    async def _settle(
        self,
        coros: list,
        *,
        phase: str,
        startup_errors: list[str],
    ) -> None:
        """Await every coroutine, then raise the first failure — never before.

        `return_exceptions=True` is required, and the reason is a race this code was
        bitten by: without it the FIRST failure (a bad URL failing DNS almost instantly)
        propagates immediately WITHOUT cancelling the still-in-flight calls for the
        other connectors (a real login plus handshake is much slower). Those keep
        running as orphaned tasks, while the caller's `except Exception` routes into
        `shutdown()` -> `session_manager.shutdown()` -> `save_state()` for EVERY entry —
        including one whose startup had not finished populating its watcher states,
        overwriting that connector's state file with a partial dict and wiping real
        session ids for a connector that was never part of the failure.

        Awaiting every result before deciding closes that race. Extracted because
        startup now settles twice, and two copies of this reasoning would be one edit
        away from one of them losing it.
        """
        results = await asyncio.gather(*coros, return_exceptions=True)
        first_exception: BaseException | None = None
        for entry, result in zip(self._entries, results, strict=True):
            if isinstance(result, BaseException):
                logger.error(
                    "SessionManager for connector '%s' failed to %s during startup: %s",
                    entry.name,
                    phase,
                    result,
                )
                startup_errors.append(
                    f"Connector '{entry.name}' failed to {phase}: {result}"
                )
                if first_exception is None:
                    first_exception = result
            elif isinstance(result, list):
                startup_errors.extend(result)
        if first_exception is not None:
            raise first_exception

    def _check_bot_identities(self, entries: "list[ConnectorEntry] | None" = None) -> None:
        """Refuse to go further if two connectors are one bot account (§4.5).

        Runs between authentication and subscription: earlier there is no identity to
        read, later the damage is already possible. A connector that cannot answer
        raises `ConnectorIdentityError` out of here, which is the fail-closed half.

        Rejects the whole startup rather than disabling the offending connector: which
        one is "offending" depends only on config order, and this project's other
        preflights (`check_state_formats`, `check_backend_signatures`) refuse loudly
        rather than degrade quietly — a daemon that silently drops a connector looks
        healthy while half its rooms go unanswered.
        """
        conflicts = find_identity_conflicts(self._identities(
            self._entries if entries is None else entries, self._dm_claims, fold_records=True))
        if conflicts:
            raise DuplicateBotIdentityError("\n".join(conflicts))

    def _identities(
        self, entries: "list[ConnectorEntry]", claims: dict[str, DmClaim], *, fold_records: bool,
    ) -> list[ConnectorIdentity]:
        """Each connector's account and DM claim, as the barrier compares them."""
        identities: list[ConnectorIdentity] = []
        for e in entries:
            identity = e.connector.bot_identity()
            if identity is None:
                continue  # declares no shared account to collide over
            claim = claims.get(e.name, DmClaim())
            if not fold_records:
                identities.append(ConnectorIdentity(
                    connector_name=e.name, identity=identity, dms=claim))
                continue
            # Widened by the connector's persisted DM records (Codex round
            # 6): sticky binding keeps a record answering its room after its
            # rule is deleted, so a rule-only claim misses exactly the
            # records that outlive their rules — and two connectors sharing
            # an account would both answer one private conversation. Read
            # best-effort: an unreadable file was already refused loudly by
            # check_state_formats, so nothing here needs a second refusal.
            try:
                claim = fold_record_dm_claims(claim, load_state(e.name))
            except Exception:
                logger.debug(
                    "Could not fold persisted DM claims for connector '%s' — "
                    "using the rule-derived claim alone", e.name, exc_info=True,
                )
            identities.append(
                ConnectorIdentity(
                    connector_name=e.name,
                    identity=identity,
                    dms=claim,
                )
            )
        return identities

    def _reclaim_orphaned_state_files(self, configured: set[str]) -> None:
        """Remove state files of connectors that are no longer configured (#143).

        Nothing opens `state.<name>.json` for a connector `config.yaml` no longer
        names — `config validate` only warns about it — so its records, and the
        sessions they point at, were abandoned silently. Boot now reconciles
        them the way it reconciles every other record: one AUDIT line per
        record with the full session id (the backend session cannot be deleted;
        there is no connector or agent context left to do it with), then the
        file is removed. Enumerates files on disk, as the validator does, so a
        renamed connector is found by its old file and not by config.
        """
        # The decision is `orphan_decisions`' (format already preflighted in
        # __init__); this method only carries it out, so `config validate` can
        # predict the same outcome without a second copy of the rule.
        for decision in orphan_decisions(configured):
            path, name, records = decision.path, decision.connector, decision.records
            if decision.keep_reason:
                logger.warning(
                    "Orphaned state file %s kept: %s — fix or delete the file by "
                    "hand (connector '%s' is no longer configured)",
                    path.name, decision.keep_reason, name,
                )
                continue
            try:
                path.unlink()
            except OSError as exc:
                # Not released: the file, and the records in it, are still
                # there and the next start finds them again. No AUDIT line —
                # that would announce a release that did not happen, twice.
                logger.warning(
                    "Could not remove orphaned state file %s (%d record(s) of "
                    "connector '%s' remain until the next start): %s",
                    path, len(records), name, exc,
                )
                continue
            for record in records:
                log_session_released(
                    logger,
                    connector=name,
                    room_id=record.room_id,
                    watcher=record.watcher_name,
                    agent=record.agent,
                    identity=record.backend_identity,
                    session_id=record.session_id,
                    reason="connector-removed",
                )
            logger.warning(
                "Removed state file %s — connector '%s' is no longer configured; "
                "its %d record(s) are logged above", path.name, name, len(records),
            )

    async def run(self, startup_fd: int = -1) -> None:
        """Connect all connectors, start unified control socket, block until cancelled.

        Args:
            startup_fd: Write end of the daemon startup handshake pipe.  When >= 0
                the method writes startup results (zero or more ``error:<msg>\\n``
                lines followed by ``ok\\n``) and closes the fd after the full startup
                sequence completes.  Pass -1 (default) to skip signalling — used in
                tests and scripts that call run() directly.
        """
        startup_errors: list[str] = []
        startup_signaled = False

        try:
            # 1-2. Start all agent backends and permission brokers.  The runtime
            #      manager handles ordering (backends first, then brokers) and
            #      failure isolation internally.
            notifier = ConnectorPermissionNotifier(self._maps.connector_view)
            self._notifier = notifier
            runtime_errors = await self._runtime_manager.start_all(
                registry=self._registry,
                notifier=notifier,
                maps=self._maps,
            )
            startup_errors.extend(runtime_errors)
            self._note_agent_errors(runtime_errors)

            # Start the permission expiry background task if any brokers are active.
            if self._runtime_manager.has_active_brokers:
                self._expiry_task = asyncio.create_task(
                    run_expiry_task(self._registry, notifier),
                    name="permission-expiry",
                )

            # The job store LOADS before any connector goes live (Codex round
            # 8): a membership removal arriving between start_inbound and the
            # load reclaimed the record and then failed its job cancellation
            # on the store's not-loaded error — and with the record gone,
            # nothing could ever rediscover those jobs. Loading is a cheap
            # file read; only the SCHEDULER must start after run_once (its
            # catch-up injections need processors), and it still does, below.
            if getattr(self, "_job_store", None) is not None:
                self._job_store.load()

            # 3. run_once() connects each SessionManager without blocking — the daemon
            #    loop below keeps the process alive.  We intentionally avoid sm.run()
            #    so that only the GatewayService owns the control socket.
            #
            # return_exceptions=True is required here — without it, the FIRST
            # SessionManager.run_once() to raise (e.g. a newly added connector
            # with a bad URL failing DNS/connect almost instantly) makes
            # gather() propagate immediately WITHOUT cancelling the other,
            # still-in-flight run_once() calls (a real RC/Mattermost login +
            # DDP handshake is much slower). Those keep running as orphaned
            # background tasks. The `except Exception` below used to route
            # straight into `shutdown()` -> `session_manager.shutdown()` ->
            # `save_state()` for EVERY entry, including the one whose
            # run_once() hadn't finished populating its watcher states yet —
            # unconditionally overwriting that connector's state.<name>.json
            # with an empty/partial dict and silently wiping out real
            # session IDs for a connector that was never actually part of
            # the failure. Awaiting every result here (success or exception)
            # before deciding what to do next closes that race: by the time
            # any exception is re-raised, no run_once() call is still
            # in-flight, so shutdown() can no longer race one.
            #
            # The two phases are separated by an identity barrier rather than merged:
            # two connectors on one bot account must be refused before EITHER
            # subscribes (§4.5), and a SessionManager owns exactly one connector, so
            # only this loop can see the collision. Fanning `run_once()` out would let
            # connector A finish subscribing while B was still logging in, and the
            # check would then run after the duplicate had started answering.
            # Settle the persisted records first (#143): hydrate and reconcile
            # each connector's fleet against the current rules BEFORE anything
            # consumes it. The identity barrier below folds persisted DM
            # records into each connector's claim; a record a deleted rule
            # left behind must be gone by then, or a legitimate two-team setup
            # is refused for a conversation nothing will ever answer again.
            await self._settle(
                [
                    e.session_manager.settle_records(
                        unavailable_agents=self._runtime_manager.unavailable_agents,
                    )
                    for e in self._entries
                ],
                phase="settle",
                startup_errors=startup_errors,
            )
            await self._settle(
                [e.session_manager.connect_only() for e in self._entries],
                phase="connect",
                startup_errors=startup_errors,
            )
            self._check_bot_identities()
            await self._settle(
                [
                    e.session_manager.sync_only(
                        unavailable_agents=self._runtime_manager.unavailable_agents,
                    )
                    for e in self._entries
                ],
                phase="start",
                startup_errors=startup_errors,
            )

            # Start the job scheduler AFTER connectors are connected and
            # watchers are up.  Starting it before run_once() would cause
            # catch-up messages to be dropped (processors not yet started).
            # The store itself loaded BEFORE run_once — see above.
            self._start_scheduler()

            await self._control.start()
            names = ", ".join(e.name for e in self._entries)
            logger.info("GatewayService running with connector(s): %s", names)

            # Signal startup complete to the parent process (daemon handshake).
            if startup_fd >= 0:
                _write_startup_signal(startup_fd, startup_errors)
                startup_signaled = True

            try:
                while True:
                    await asyncio.sleep(3600)
            except asyncio.CancelledError:
                pass
        except Exception as e:
            if startup_fd >= 0 and not startup_signaled:
                # fatal=True: startup failed — do NOT emit "ok" so the parent
                # correctly reports failure and exits 1 instead of "degraded".
                _write_startup_signal(
                    startup_fd,
                    startup_errors + [f"startup failed: {e}"],
                    fatal=True,
                )
                startup_signaled = True
            raise
        finally:
            await self.shutdown()
            # Ensure the parent process never blocks forever on os.read(read_fd)
            # if startup was interrupted mid-flight by CancelledError (which is
            # NOT caught by `except Exception` above).  Without this, SIGTERM
            # arriving before startup_signaled=True leaves the write-fd open and
            # the parent hangs indefinitely waiting for EOF.
            if startup_fd >= 0 and not startup_signaled:
                # fatal=True: startup was cancelled before completing — no "ok".
                _write_startup_signal(
                    startup_fd,
                    startup_errors + ["startup cancelled"],
                    fatal=True,
                )
                startup_signaled = True

    async def shutdown(self) -> None:
        """Graceful shutdown — called by daemon.py on SIGTERM/crash.

        Shutdown order:
          1. Stop the control socket FIRST so no new lifecycle commands
             (pause/resume/reset) can arrive while session managers are
             tearing down.  A command reaching an already-shut-down
             WatcherLifecycle would produce confusing errors.
          2. Cancel the job scheduler BEFORE session managers shut down so
             that an in-progress fire cannot race a draining queue.
          3. Shut down session managers (drain processors, cancel permissions).
          4. Stop agent runtime (brokers, backends).
          5. Cancel the permission expiry task.
        """
        logger.info("GatewayService shutting down")
        # Step 1: close the control socket so no new commands arrive during teardown.
        try:
            await self._control.stop()
        except Exception as e:
            logger.error("Error stopping control server: %s", e)
        # Step 1b: a `config reload` already applying finishes first (#144).
        # `ControlServer.stop` closes the listener but does not await the
        # handler in flight, and that handler is stopping, replacing and
        # starting the very entries the steps below tear down. Taken and never
        # released — nothing after this may reload.
        if not self._shutdown_holds_reload_lock:
            await self._reload_lock.acquire()
            self._shutdown_holds_reload_lock = True
        # Step 2: cancel the job scheduler before session managers stop.
        # This prevents a scheduler tick from trying to inject into a processor
        # that is in the middle of draining its queue.
        await self._stop_scheduler()
        # Step 3: shut down session managers.
        sm_results = await asyncio.gather(
            *[e.session_manager.shutdown() for e in self._entries],
            return_exceptions=True,
        )
        for entry, result in zip(self._entries, sm_results, strict=False):
            if isinstance(result, Exception):
                logger.error(
                    "Error shutting down session manager for connector '%s': %s",
                    entry.name,
                    result,
                )
        # Step 3b: the connector instances earlier reloads could not shut down —
        # disconnected once more, never saved (their records are the
        # replacement's now).
        for entry, _err in self._leftover_entries:
            await stop_with_retries(f"leftover connector '{entry.name}'",
                                    entry.connector.disconnect)
        # Step 4: stop agent runtime (brokers, backends).
        try:
            await self._runtime_manager.stop_all()
        except Exception as e:
            logger.error("Error stopping agent runtime manager: %s", e)
        # Step 5: cancel the permission expiry task.
        if self._expiry_task:
            self._expiry_task.cancel()
            try:
                await self._expiry_task
            except asyncio.CancelledError:
                pass
            except Exception as e:
                logger.error("Error stopping permission expiry task: %s", e)
            finally:
                self._expiry_task = None
        logger.info("GatewayService shut down")

    def _start_scheduler(self) -> None:
        """Start the job scheduler — after connectors are connected and
        watchers are up, so its catch-up injections find processors."""
        if getattr(self, "_job_store", None) is not None and self._scheduler_task is None:
            self._scheduler_task = asyncio.create_task(
                self._job_scheduler.run(),
                name="job-scheduler",
            )

    async def _stop_scheduler(self) -> None:
        if getattr(self, "_scheduler_task", None):
            self._scheduler_task.cancel()
            try:
                await self._scheduler_task
            except asyncio.CancelledError:
                pass
            except Exception as e:
                logger.error("Error stopping job scheduler task: %s", e)
            finally:
                self._scheduler_task = None  # type: ignore[assignment]

    # ── Config reload (#144) ─────────────────────────────────────────────────

    def describe_config(self, *, include_config: bool = False) -> dict:
        """The active configuration as `status` and `config show` report it."""
        out = {
            "ok": True,
            "config_path": self._config_path,
            "digest": self._config_digest,
            "loaded_at": self._config_loaded_at,
            "reloading": self._reloading,
            "degraded": [
                Degraded("connector", e.name, e.degraded).to_dict()
                for e in self._entries if e.degraded
            ] + [
                Degraded("connector", e.name,
                         f"a previous instance did not shut down ({err}); it is disconnected "
                         f"again at the next reload and at shutdown — check the process now"
                         ).to_dict()
                for e, err in self._leftover_entries
            ] + [
                Degraded("agent", name, self._agent_errors.get(name, "failed to start")).to_dict()
                for name in sorted(self._runtime_manager.unavailable_agents)
            ] + [
                Degraded("agent", name,
                         f"a previous {kind} did not stop ({err}); it is stopped again at the "
                         f"next reload and at shutdown — check the process now").to_dict()
                for name, kind, err in self._runtime_manager.leftovers
            ],
        }
        if include_config:
            out["config"] = redacted_config(self._config)
        return out

    def _note_agent_errors(self, errors: list[str]) -> None:
        """Keep each unavailable agent's start error, by name, for `status`."""
        for name in self._runtime_manager.unavailable_agents:
            for err in errors:
                if f"'{name}'" in err:
                    self._agent_errors[name] = err
        for name in list(self._agent_errors):
            if name not in self._runtime_manager.unavailable_agents:
                del self._agent_errors[name]

    async def reload_config(self, *, dry_run: bool, config_path: str | None = None) -> dict:
        """`config reload`: validate the file, plan, and unless `dry_run` apply.

        One request, one response. The file is read once, here; the plan is
        computed against the running fleet; a dry run returns it; an apply
        prints it to the log, executes it and returns it with what happened.
        Refusals — invalid file, a reload already applying, a path other than
        the daemon's — return `ok: False` and change nothing.
        """
        if self._config_path is None:
            return ReloadPlan.refused(
                "this daemon was not started from a config file — nothing to reload",
                dry_run=dry_run).to_dict()
        if config_path and os.path.abspath(config_path) != self._config_path:
            return ReloadPlan.refused(
                f"the daemon runs {self._config_path}, not {config_path} — pass that "
                f"path, or restart the daemon on the new one", dry_run=dry_run).to_dict()
        if self._reload_lock.locked():
            return ReloadPlan.refused(
                "a config reload is already in progress — wait for it to finish",
                dry_run=dry_run).to_dict()
        async with self._reload_lock:
            result = validate_config(self._config_path)
            findings = [finding_to_dict(f) for f in result.findings if f.severity != "lint"]
            if not result.ok or result.config is None:
                return ReloadPlan.refused(
                    f"{self._config_path}: {len(result.errors)} error(s) — nothing changed",
                    dry_run=dry_run, findings=findings).to_dict()
            candidate = result.config
            diff = diff_configs(self._config, candidate)
            await self._retry_leftovers()
            conflict = self._kept_identity_conflict(diff, candidate)
            if conflict:
                return ReloadPlan.refused(conflict, dry_run=dry_run, findings=findings).to_dict()
            self._retry_degraded(diff, candidate)
            try:
                plan = self._plan_reload(diff, candidate, findings, dry_run=dry_run)
            except Exception as e:
                logger.exception("config reload: could not plan")
                return ReloadPlan.refused(
                    f"could not plan the reload — nothing changed: {e}",
                    dry_run=dry_run, findings=findings).to_dict()
            if dry_run or not plan.has_changes:
                return plan.to_dict()
            logger.info("config reload: applying\n%s", plan.render())
            try:
                await self._apply_reload(diff, candidate, plan)
            except _ReloadRefused as e:
                return ReloadPlan.refused(str(e), dry_run=dry_run, findings=findings).to_dict()
            plan.applied = True
            logger.info("config reload: %s", plan.render().splitlines()[-1])
            return plan.to_dict()

    async def _retry_leftovers(self) -> None:
        """Once more, at every reload: disconnect the connector instances and
        stop the backends earlier reloads could not. What stops leaves the
        degraded list; what does not stays there, and the operator is told
        again. No other means are tried."""
        still: list[tuple[ConnectorEntry, str]] = []
        for entry, err in self._leftover_entries:
            again = await stop_with_retries(f"leftover connector '{entry.name}'",
                                            entry.connector.disconnect)
            if again is not None:
                still.append((entry, str(again)))
        self._leftover_entries = still
        await self._runtime_manager.retry_leftovers()

    def _kept_identity_conflict(self, diff: ConfigDiff, candidate: GatewayConfig) -> str | None:
        """Would the candidate's rules make two KEPT connectors claim one account's DMs?

        The identity barrier runs when a connector connects, so a rule-only
        reload — nothing connects — would otherwise let two Mattermost
        connectors on one bot account, safe while their teams kept them apart,
        both opt into `direct:` and answer the same private conversation. The
        check is the barrier's own, over the connectors that stay up and the
        candidate's rule-derived claims, and it REFUSES before anything is
        touched: a refusal here is cheap, a duplicate answer is not. Persisted
        DM records are not folded in — the ones a deleted rule leaves are
        expired by this very reload, which is what boot's settle-then-barrier
        order relies on too.
        """
        if not diff.rules_changed:
            return None
        names = {c.name for c in candidate.connectors}
        kept = [e for e in self._entries
                if not e.degraded and e.name in names and e.name not in diff.connectors.changed]
        try:
            conflicts = find_identity_conflicts(
                self._identities(kept, dm_claims(candidate.watcher_rules), fold_records=False))
        except ConnectorIdentityError as exc:
            return f"could not check bot identities for the rule change — nothing changed: {exc}"
        if conflicts:
            return ("the rule change would make connectors share a bot account's direct "
                    "messages — nothing changed:\n" + "\n".join(conflicts))
        return None

    def _retry_degraded(self, diff: ConfigDiff, candidate: GatewayConfig) -> None:
        """Fold the degraded sections into the diff as changed, so a reload
        retries them even when their own entry did not change.

        "Fix the file and reload again" must also cover a fix that is not in
        the file — a server that is reachable again, a sidecar binary put
        back. Without this an unchanged degraded connector would stay down
        until a full restart. Silent on purpose (owner, 2026-09-05): the plan
        shows the restart like any other, and if the section is STILL failing
        the Degraded block says so — a section that came back needs no history.
        """
        names = {c.name for c in candidate.connectors}
        for e in self._entries:
            if (e.degraded and e.name in names
                    and e.name not in diff.connectors.changed
                    and e.name not in diff.connectors.added):
                diff.connectors.changed.append(e.name)
        for name in sorted(self._runtime_manager.unavailable_agents):
            if name in candidate.agents and name not in diff.agents.changed:
                diff.agents.changed.append(name)

    def _plan_reload(
        self, diff: ConfigDiff, candidate: GatewayConfig, findings: list[dict], *, dry_run: bool
    ) -> ReloadPlan:
        """What this reload will do, record by record, from the running fleet."""
        plan = ReloadPlan(dry_run=dry_run, findings=findings, digest=config_digest(candidate))
        plan.take_diff(diff)
        if any(v.path == "max_queue_depth" for v in diff.values):
            # Honest about what a value swap reaches: a processor's queue is
            # sized when it is built, and this reload deliberately restarts none
            # of them for a tuning value.
            plan.notes.append(
                "max_queue_depth applies to watchers started from now on; a resident "
                "watcher keeps its current queue size until it next restarts")
        removed = set(diff.connectors.removed)
        restarted = set(diff.connectors.changed)
        restarted_agents = set(diff.agents.changed)
        candidate_names = {c.name for c in candidate.connectors}
        for e in self._entries:
            if e.name in diff.connectors.added:
                continue  # a failed apply's leftover — planned from its file below
            records = e.session_manager.records()
            if e.name in removed or e.name not in candidate_names:
                if e.name not in plan.connectors.removed:
                    plan.connectors.removed.append(e.name)  # a leftover neither config names
                plan.watchers.extend(connector_removed_changes(e.name, records))
                continue
            plan.watchers.extend(plan_connector_records(
                e.name, records, candidate.rules_for(e.name),
                resident=e.session_manager.resident_rooms(),
                restart_all=e.name in restarted,
                restarted_agents=restarted_agents,
            ))
            if e.name in restarted and records:
                plan.notes.append(SCOPE_REVALIDATION_NOTE.format(
                    connector=e.name, count=len(records)))
        # An added connector hydrates whatever state file already carries its
        # name (a connector renamed back, say); those records reconcile too.
        for name in diff.connectors.added:
            plan.watchers.extend(plan_persisted_records(name, candidate))
        # Files on disk no candidate connector owns — the same sweep boot runs.
        # A removed entry's own file is planned above from its live records.
        names, changes, notes = orphan_removals(
            candidate_names, skip={e.name for e in self._entries})
        plan.connectors.removed.extend(n for n in names if n not in plan.connectors.removed)
        plan.watchers.extend(changes)
        plan.notes.extend(notes)
        return plan

    async def _apply_reload(
        self, diff: ConfigDiff, candidate: GatewayConfig, plan: ReloadPlan
    ) -> None:
        """Execute a reload plan: one stop pass, one start pass, then reconcile.

        Order, and why:

        0. Everything new is CONSTRUCTED first — backends, connectors. A factory
           that raises refuses the whole reload with nothing touched. That is
           the LAST refusal: from here on, whatever fails degrades its section
           and is reported; nothing is rolled back and nothing is killed by
           another route (owner, 2026-09-05 — a rollback can fail too, and
           even one that succeeds has not reloaded the config).
        1. The scheduler is paused (as shutdown does) so no fire lands in the
           window; a job due meanwhile fires on the restart's catch-up.
        2. Every kept manager is quiesced — no new wake, join or verb — so no
           watcher can start against a backend about to be stopped.
        3. Stop pass, in shutdown order: a removed connector's records are
           reclaimed through the shared tail while everything is alive; going
           managers shut down; the processors of every changed or removed
           agent are drained, while their backends are still alive; those
           agents stop. Every stop is retried a few times; one that still will
           not stop is kept as a LEFTOVER (tried again at the next reload and
           at shutdown, named by `status`), the replacement proceeds, and the
           plan carries a degraded finding saying so.
        4. Rebuild: the shared agents dict and the core config are updated in
           place; new entries replace old ones in candidate order; kept
           managers take the candidate's rules.
        5. Start pass, in boot order: agents; kept managers re-arm and
           reconcile (before the identity barrier, as boot settles before it);
           each new connector settles its records, connects, passes the
           barrier and syncs. A failure leaves a DEGRADED entry, never a
           half-started one.
        6. Kept managers start what step 3 stopped and every was-active room
           of a changed agent. Then the scheduler, the active config, the
           digest.
        """
        removed = set(diff.connectors.removed)
        restarted = set(diff.connectors.changed)
        added = set(diff.connectors.added)
        changed_agents = set(diff.agents.changed)
        added_agents = set(diff.agents.added)
        removed_agents = set(diff.agents.removed)

        # 0. Construct before destroying.
        try:
            new_backends = {
                name: _build_agent_backend(candidate.agents[name])
                for name in sorted(changed_agents | added_agents)
            }
            check_backend_signatures(new_backends)
            new_connectors = {
                cc.name: connector_factory(cc)
                for cc in candidate.connectors if cc.name in (restarted | added)
            }
        except Exception as e:
            raise _ReloadRefused(f"could not build the new configuration — nothing changed: {e}")

        self._reloading = True
        kept: list[ConnectorEntry] = []
        started: set[str] = set()
        try:
            # 1–2. Pause the scheduler, still the kept managers. An entry that
            #      already exists under an ADDED name is one a failed apply left
            #      behind (a degraded placeholder, or a half-started connector):
            #      it goes the way a restarted one does, so its replacement is
            #      never started beside it. An entry neither config names — a
            #      leftover once the file was put back — is a removal.
            await self._stop_scheduler()
            live = {e.name for e in self._entries}
            removed |= live - {c.name for c in candidate.connectors} - removed
            replacing = removed | restarted | (added & live)
            kept = [e for e in self._entries if e.name not in replacing]
            for e in kept:
                await e.session_manager.quiesce(RELOAD_IN_PROGRESS)

            # 3. Stop pass.
            going = [e for e in self._entries if e.name in replacing]
            #    A removed connector's records first, through the shared
            #    reclamation tail — session deleted where the backend can,
            #    prompt file and attachment workspace removed, jobs cancelled,
            #    one AUDIT line each — while its manager and the backends are
            #    still alive. Boot's orphan sweep cannot do this for a file
            #    whose connector is gone; here nothing is gone yet.
            for e in going:
                if e.name in removed:
                    await e.session_manager.reclaim_all(
                        reason="connector-removed",  # boot's sweep spells it so; one grep
                        jobs=JOBS_CANCELLED_CONNECTOR_REMOVED)
            #    Then the going managers shut down. One that will not — after
            #    the retries — is a leftover: still tracked, disconnected again
            #    later, never saved again; its replacement goes ahead.
            results = await asyncio.gather(*[
                stop_with_retries(f"connector '{e.name}'", e.session_manager.shutdown)
                for e in going])
            for e, err in zip(going, results, strict=True):
                if err is not None:
                    self._leftover_entries.append((e, str(err)))
                    plan.degraded.append(Degraded(
                        "connector", e.name,
                        f"its previous instance did not shut down after {STOP_ATTEMPTS} "
                        f"attempts ({err}); the new configuration was applied beside it and "
                        f"it is disconnected again at the next reload and at shutdown — it "
                        f"may still hold the account's connection: check the process now"))
            #    Then the processors of every agent that stops or goes, while
            #    its backend is still alive: a processor's stop drains its queue
            #    by processing it, and against a stopped sidecar every drained
            #    message would fail into the room instead. A removed agent's
            #    processors are here too — the reconciliation below stops them
            #    (their records expire or move) and would otherwise drain them
            #    against a backend already gone.
            stopping_agents = changed_agents | removed_agents
            stopped_rooms: dict[str, list[str]] = {}
            if stopping_agents:
                drained = await asyncio.gather(
                    *[e.session_manager.stop_watchers_on_agents(stopping_agents) for e in kept])
                for e, (rooms, failures) in zip(kept, drained, strict=True):
                    stopped_rooms[e.name] = rooms
                    self._report_room_failures(plan, e.name, failures)
            #    Then those agents' backends and brokers. One that will not stop
            #    is a leftover — the agent is replaced regardless and reported.
            not_stopped = await self._runtime_manager.stop_some(stopping_agents)
            for name, err in sorted(not_stopped.items()):
                plan.degraded.append(Degraded(
                    "agent", name,
                    f"{err}; the new backend was started beside the old one, which is "
                    f"stopped again at the next reload and at shutdown — check the "
                    f"process now"))
            self._install_entries(kept)
            self._reclaim_orphaned_state_files({c.name for c in candidate.connectors})

            # 4. Rebuild.
            for name in removed_agents:
                self._runtime_manager.replace(name, None)
            for name, backend in new_backends.items():
                self._runtime_manager.replace(name, backend)
            self._core_config.agents = candidate.agents
            self._core_config.connector_configs = {c.name: c for c in candidate.connectors}
            self._core_config.max_queue_depth = candidate.max_queue_depth
            self._dm_claims = dm_claims(candidate.watcher_rules)
            by_name = {e.name: e for e in kept}
            fleet: list[ConnectorEntry] = []
            for cc in candidate.connectors:
                if cc.name in new_connectors:
                    fleet.append(self._build_entry(cc, new_connectors[cc.name], candidate))
                else:
                    fleet.append(by_name[cc.name])
            self._install_entries(fleet)
            for e in kept:
                e.session_manager.replace_rules(candidate.rules_for(e.name))

            # 5. Start pass — agents first.
            if new_backends:
                notifier = self._notifier or ConnectorPermissionNotifier(self._maps.connector_view)
                errors = await self._runtime_manager.start_some(
                    set(new_backends), self._registry, notifier, self._maps)
                self._note_agent_errors(errors)
                for name in sorted(set(new_backends) & self._runtime_manager.unavailable_agents):
                    plan.degraded.append(Degraded(
                        "agent", name, self._agent_errors.get(name, "failed to start")))
                if self._runtime_manager.has_active_brokers and self._expiry_task is None:
                    self._expiry_task = asyncio.create_task(
                        run_expiry_task(self._registry, notifier), name="permission-expiry")
            # The kept lifecycles judge starts by a set boot wrote; a reload that
            # changed which agents are up must rewrite it before anything starts.
            for e in kept:
                e.session_manager.set_unavailable_agents(self._runtime_manager.unavailable_agents)

            #    Kept managers re-arm and reconcile BEFORE the new connectors
            #    pass the identity barrier — boot's own order (settle, connect,
            #    identity). The barrier folds each connector's persisted DM
            #    records into its claim, and a DM record a deleted rule left
            #    behind must be gone by then, or a legitimate second connector
            #    on the same account is refused for a room nothing answers.
            for e in kept:
                await e.session_manager.rearm()
            restarted_rooms: dict[str, set[str]] = {}
            if diff.rules_changed:
                for e in kept:
                    try:
                        rooms, failures = await e.session_manager.reconcile_live()
                        restarted_rooms[e.name] = set(rooms)
                        self._report_room_failures(plan, e.name, failures)
                    except Exception as exc:
                        # On the entry, not only in the response: `status` must
                        # show it, and the next reload must retry it (as a whole
                        # restart, which reconciles from settle_records) even
                        # when the file did not change again.
                        logger.exception("config reload: reconciliation on connector '%s' failed",
                                         e.name)
                        e.degraded = f"reconciliation failed: {exc}"
                        plan.degraded.append(Degraded("connector", e.name, e.degraded))

            starting = [e for e in fleet if e.name in new_connectors]
            started |= await self._start_entries(starting, plan)

            # 6. Start what step 3 stopped — wherever its record now points —
            #    and every was-active room of an agent that changed.
            for e in kept:
                if e.degraded:
                    continue
                self._report_room_failures(
                    plan, e.name,
                    await e.session_manager.start_watchers_on_agents(
                        changed_agents, rooms=stopped_rooms.get(e.name, []),
                        exclude=restarted_rooms.get(e.name, set())))
            self._job_scheduler.completed_job_ttl_days = candidate.scheduler.completed_job_ttl_days
            self._start_scheduler()
            self._config = candidate
            self._config_digest = plan.digest
            self._config_loaded_at = now_iso()
        except BaseException as exc:
            # A DEFECT in the apply (nothing above raises by design) must not
            # leave the daemon wedged or lying. What is running keeps running
            # (kept managers re-armed, scheduler back); every connector the
            # candidate names — and every one this apply was tearing down —
            # has an entry, the ones it lost marked degraded with the error;
            # and the PREVIOUS config stays active, so `config show` says the
            # file is not applied and the next reload re-diffs everything. The
            # error itself goes back to the operator.
            logger.exception("config reload: apply failed part-way — re-arming what is running")
            await self._settle_after_failed_apply(
                candidate, kept, started, new_connectors, plan, exc)
            raise
        finally:
            self._reloading = False

    @staticmethod
    def _report_room_failures(plan: ReloadPlan, connector: str, failures: list[str]) -> None:
        """One room did not come back (or did not stop); the connector runs on.

        Every per-watcher failure on the apply path — a processor that would
        not stop before its agent changed, a re-materialized one that would
        not restart, a room of a changed agent, an eager room a new rule
        names, a new connector's own start errors — lands here and nowhere
        else: as a degraded finding on the plan (exit 2) that names the
        remedy, WITHOUT marking the connector degraded (its other rooms are
        answering, and a degraded entry refuses every verb). `list` shows the
        room failed; `resume`, or the room's next message, brings it back.
        """
        for msg in failures:
            logger.error("config reload: %s", msg)
            plan.degraded.append(Degraded(
                "connector", connector,
                f"{msg} — the connector runs on; the room shows as failed in 'list': "
                f"'resume' it, or send a message in the room"))

    async def _settle_after_failed_apply(
        self, candidate: GatewayConfig, kept: "list[ConnectorEntry]", started: set[str],
        new_connectors: dict[str, Connector], plan: ReloadPlan, exc: BaseException,
    ) -> None:
        """Leave a consistent fleet behind an apply that RAISED (a defect).

        Every existing entry stays tracked (the final shutdown must visit a
        connector that was mid-teardown); every candidate connector without an
        entry gets a degraded placeholder. Every entry except the ones known to
        have started cleanly is degraded with the error — the KEPT ones too,
        because the apply may have swapped their rules or the shared core
        config before it raised, and nothing short of a restart says which:
        `status` shows them, and the next reload — even of the file put back —
        restarts them whole. The active config is NOT advanced.
        """
        by_name = {e.name: e for e in self._entries}
        untouched = set(started)
        fleet: list[ConnectorEntry] = []
        for cc in candidate.connectors:
            entry = by_name.pop(cc.name, None)
            if entry is None:
                connector = new_connectors.get(cc.name) or connector_factory(cc)
                entry = self._build_entry(cc, connector, candidate)
            fleet.append(entry)
        fleet.extend(by_name.values())  # tracked still — being removed, not yet gone
        for entry in fleet:
            if entry.name not in untouched and not entry.degraded:
                entry.degraded = f"reload failed before this connector was settled: {exc}"
                plan.degraded.append(Degraded("connector", entry.name, entry.degraded))
        self._install_entries(fleet)
        for e in kept:
            try:
                await e.session_manager.rearm()
            except Exception:
                logger.exception("config reload: could not re-arm connector '%s'", e.name)
        self._start_scheduler()

    async def _start_entries(self, entries: "list[ConnectorEntry]", plan: ReloadPlan) -> set[str]:
        """Boot order for a reload's new connectors, one degraded entry per
        failure. Returns the names that came up."""
        unavailable = self._runtime_manager.unavailable_agents
        connected: list[ConnectorEntry] = []
        started: set[str] = set()

        async def _settle_and_connect(e: ConnectorEntry) -> None:
            await e.session_manager.settle_records(unavailable_agents=unavailable)
            await e.session_manager.connect_only()

        # Concurrently, as boot's `_settle` phases are: several slow logins
        # must not add up inside the operator's one request.
        results = await asyncio.gather(
            *[_settle_and_connect(e) for e in entries], return_exceptions=True)
        for e, result in zip(entries, results, strict=True):
            if isinstance(result, BaseException):
                await self._degrade(e, f"failed to connect: {result}", plan)
            else:
                connected.append(e)
        # The identity barrier, one new connector at a time: boot fails fast on a
        # conflict, a reload cannot take the running connectors down for one, so
        # the new connector whose ADDITION creates the conflict is the one refused
        # — for a conflict, and for an identity nothing could read — and the
        # others still come up.
        accepted = [e for e in self._entries if not e.degraded and e not in connected]
        passing: list[ConnectorEntry] = []
        for e in connected:
            try:
                self._check_bot_identities(accepted + [e])
            except (DuplicateBotIdentityError, ConnectorIdentityError) as exc:
                await self._degrade(e, f"bot identity check failed: {exc}", plan)
                continue
            accepted.append(e)
            passing.append(e)
        results = await asyncio.gather(
            *[e.session_manager.sync_only(unavailable_agents=unavailable) for e in passing],
            return_exceptions=True)
        for e, result in zip(passing, results, strict=True):
            if isinstance(result, BaseException):
                await self._degrade(e, f"failed to start: {result}", plan)
                continue
            # The connector is up; `sync_only` returns the rooms that are not.
            self._report_room_failures(plan, e.name, result)
            started.add(e.name)
        return started

    async def _degrade(self, entry: ConnectorEntry, error: str, plan: ReloadPlan) -> None:
        logger.error("config reload: connector '%s' is degraded — %s", entry.name, error)
        entry.degraded = error
        plan.degraded.append(Degraded("connector", entry.name, error))
        try:
            await entry.session_manager.shutdown()
        except Exception as exc:
            logger.error("config reload: degraded connector '%s' did not shut down cleanly: %s",
                         entry.name, exc)

    # Control socket has been extracted to gateway.control.ControlServer.
    # Backend + broker lifecycle has been extracted to AgentRuntimeManager.


# ── Module-level helpers ───────────────────────────────────────────────────────


class _ReloadRefused(Exception):
    """A reload that could not start — nothing was changed."""


def sanitize_pipe_message(message: str) -> str:
    """Strip embedded newlines from a message before writing it into the
    daemon startup handshake pipe's line-oriented `info:`/`error:`/`ok`
    protocol — an embedded newline would split one message into multiple,
    unparseable protocol lines. Shared by `_write_startup_signal()` below
    AND `gateway/daemon.py`'s own pipe writes (lock-acquire/config-
    migration/config-load/service-crash failures) — code-review finding:
    those two files used to each keep an independent inline copy of this
    exact sanitization, applied inconsistently (only 2 of daemon.py's 5
    write sites), so this is now the one place it's defined."""
    return message.replace("\n", " ").replace("\r", " ")


def _write_startup_signal(fd: int, errors: list[str], *, fatal: bool = False) -> None:
    """Write startup result to the daemon handshake pipe and close it.

    Protocol:
      - Zero or more ``error:<message>\\n`` lines for startup failures.
      - A final ``ok\\n`` line IFF startup completed (possibly degraded).

    When ``fatal=True`` the ``ok`` line is intentionally omitted so the
    parent process sees no ``ok`` and correctly reports failure + exits 1.
    Emitting ``ok`` after a fatal error would cause the parent to report
    "degraded startup" even though the daemon has already crashed.

    The parent process reads until EOF, then checks for error lines and the
    presence of the ``ok`` marker.
    """
    try:
        sanitized = [sanitize_pipe_message(e) for e in errors]
        payload = "".join(f"error:{e}\n" for e in sanitized)
        if not fatal:
            payload += "ok\n"
        os.write(fd, payload.encode())
    except OSError as exc:
        # Log but do not re-raise — the finally block closes the fd, which
        # sends EOF to the parent so it can unblock.  The parent will see no
        # 'ok' line and report failure, which is the right outcome when we
        # cannot write the startup signal.
        import logging as _logging
        _logging.getLogger("agent-chat-gateway.service").warning(
            "Failed to write startup signal to handshake pipe (fd=%d): %s", fd, exc
        )
    finally:
        try:
            os.close(fd)
        except OSError:
            pass
