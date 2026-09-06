"""Control socket server: routes CLI commands to SessionManagers.

Extracted from GatewayService so the top-level orchestrator stays focused on
process-level lifecycle.  This module owns:

  - The Unix domain socket server lifecycle.
  - JSON framing (read request line → dispatch → write response line).
  - Command routing by connector name (aggregate ``list``, explicit targeting,
    ambiguity guard for multi-connector deployments).
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import TYPE_CHECKING

from . import runtime_lock
from .core.connector import Room
from .core.tz_utils import local_iana_timezone as _server_local_timezone
from .core.watcher_manager import config_from_record
from .runtime_lock import RUNTIME_DIR

if TYPE_CHECKING:
    from .core.job_store import JobStore
    from .service import ConnectorEntry, GatewayService

logger = logging.getLogger("agent-chat-gateway.control")


def _to_epoch_ms(dt) -> str | None:
    """An operator's ISO timestamp as the internal representation (§5.2).

    A naive datetime is read as local time, which is what an operator who
    omitted the offset meant — the alternative, UTC, would silently shift the
    window by the local offset.
    """
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.astimezone()
    return str(int(dt.timestamp() * 1000))

CONTROL_SOCK = RUNTIME_DIR / "control.sock"

# The operator verbs that change a watcher's lifecycle — refused while a
# reload applies, and against a degraded connector (#144).
LIFECYCLE_VERBS = ("pause", "resume", "reset", "expire")


def _degraded_error(entry: "ConnectorEntry") -> dict | None:
    """The refusal for a command aimed at a connector a reload left degraded (#144).

    Its manager is shut down, its connector never (re)connected; a verb would
    be told "the gateway is shutting down" and a send would hit an
    unauthenticated client. `list` is not refused — the records are real.
    """
    degraded = getattr(entry, "degraded", "")
    if not (isinstance(degraded, str) and degraded):
        return None
    return {"ok": False, "error": (
        f"Connector '{entry.name}' is degraded: {degraded}. Fix the cause and run "
        f"'agent-chat-gateway config reload' to bring it back.")}


class ControlServer:
    """Unix socket server for CLI command routing.

    Usage::

        server = ControlServer(entries)
        await server.start()

        # ... gateway runs ...

        await server.stop()
    """

    def __init__(
        self,
        entries: "list[ConnectorEntry]",
        job_store: "JobStore | None" = None,
        service: "GatewayService | None" = None,
    ) -> None:
        self._entries = entries
        self._job_store = job_store
        # The service, for the commands that are about the daemon rather than
        # one connector: `config-reload`, `config-show` (#144). None
        # for a server built without one (tests); those commands then refuse.
        self._service = service
        self._server: asyncio.Server | None = None

    async def start(self) -> None:
        """Bind the Unix domain socket and start accepting connections."""
        RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
        if CONTROL_SOCK.exists():
            # Attempt a quick probe connection before removing the socket file.
            # If the probe succeeds, another live gateway instance is already
            # bound — refuse to start rather than silently hijacking the socket.
            # If the probe fails (ConnectionRefusedError / OSError), the socket
            # is a stale leftover from a previous crash and is safe to remove.
            _probe_writer = None
            try:
                reader, _probe_writer = await asyncio.wait_for(
                    asyncio.open_unix_connection(str(CONTROL_SOCK)),
                    timeout=2.0,
                )
            except asyncio.TimeoutError:
                # IMPORTANT: asyncio.TimeoutError is TimeoutError which is a
                # subclass of OSError (Python 3.3+), so this clause MUST come
                # before the OSError clause below — otherwise OSError catches it
                # first and the PID-lock check is never reached.
                #
                # Socket exists but the connection timed out.  This could mean
                # the instance is alive but overloaded.  Check the PID lock
                # before treating the socket as stale — unlinking a live socket
                # would allow two gateway instances to run simultaneously (split-brain).
                pid = runtime_lock.locked_pid()
                if pid is not None:
                    raise RuntimeError(
                        f"Another gateway instance may be running (pid={pid}, "
                        f"socket {CONTROL_SOCK} timed out). "
                        f"Stop it first, or remove the socket manually to force-start."
                    )
                # No live PID owner — the socket is stale despite the timeout.
                CONTROL_SOCK.unlink()
            except (ConnectionRefusedError, OSError):
                # Socket refused or OS error — clearly stale, safe to replace.
                CONTROL_SOCK.unlink()
            else:
                # Probe succeeded — a live gateway instance is already bound.
                # Close the probe writer OUTSIDE the try/except so that any
                # OSError from writer cleanup cannot be caught by the OSError
                # clause above (which would mis-classify the socket as stale
                # and unlink it, allowing two instances to run simultaneously).
                if _probe_writer is not None:
                    try:
                        _probe_writer.close()
                        await _probe_writer.wait_closed()
                    except OSError:
                        pass
                raise RuntimeError(
                    f"Another gateway instance is already running "
                    f"(socket {CONTROL_SOCK} is live). "
                    f"Stop it first, or remove the socket manually to force-start."
                )

        self._server = await asyncio.start_unix_server(
            self._handle_client, path=str(CONTROL_SOCK)
        )
        # Restrict socket access to the owner only.  asyncio.start_unix_server
        # creates the socket with the process umask (typically 0o666 & ~umask),
        # which may allow group/world read-write.  chmod 0o600 ensures only the
        # owner can connect, providing OS-level access control for all commands
        # (pause, resume, reset, send) routed through this socket.
        try:
            import os as _os
            _os.chmod(str(CONTROL_SOCK), 0o600)
        except OSError as exc:
            logger.warning("Could not set control socket permissions: %s", exc)
        logger.info("Control socket listening at %s", CONTROL_SOCK)

    async def stop(self) -> None:
        """Close the server and remove the socket file."""
        if self._server:
            self._server.close()
            await self._server.wait_closed()
        CONTROL_SOCK.unlink(missing_ok=True)

    # ── Client handling ───────────────────────────────────────────────────────

    async def _handle_client(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        """Handle a single CLI connection: read JSON request, dispatch, respond."""
        try:
            # Apply a read timeout so a client that connects but never sends data
            # (e.g., killed mid-send) cannot hold the handler open indefinitely.
            data = await asyncio.wait_for(reader.readline(), timeout=30.0)
            if not data:
                return
            request = json.loads(data.decode())
            response = await self.dispatch_command(request)
            writer.write(json.dumps(response).encode() + b"\n")
            # Drain with a timeout so a client that receives the response but
            # then dies before consuming it (e.g. SIGKILL mid-read) cannot
            # block this handler indefinitely, leaking a file descriptor and
            # a coroutine slot in the event loop.
            await asyncio.wait_for(writer.drain(), timeout=10.0)
        except Exception as e:
            try:
                writer.write(json.dumps({"ok": False, "error": str(e)}).encode() + b"\n")
                await asyncio.wait_for(writer.drain(), timeout=10.0)
            except Exception:
                pass
        finally:
            writer.close()
            try:
                # Guard against a crashed client that never acknowledges the
                # close — the default TCP keepalive timeout is ~2 hours, so
                # without this limit the handler coroutine would leak for hours
                # holding an open file descriptor.
                await asyncio.wait_for(writer.wait_closed(), timeout=5.0)
            except (asyncio.TimeoutError, Exception):
                pass

    # ── Command routing ───────────────────────────────────────────────────────

    async def dispatch_command(self, request: dict) -> dict:
        """Route a CLI command to the appropriate SessionManager.

        Uses the 'connector' field in the request to select the target entry.
        Special case: 'list' without a connector name aggregates watchers
        across ALL connectors, annotating each entry with its connector name.
        Special case: 'reset' without a connector name auto-resolves the entry
        by searching all connectors for the named watcher (watcher names are
        globally unique, enforced at config load time).
        """
        cmd = request.get("cmd")
        connector_name = request.get("connector")

        # The daemon-level commands (#144): about the active configuration,
        # not any one connector.
        if cmd in ("config-reload", "config-show"):
            return await self._handle_service_command(cmd, request)

        # A lifecycle verb cannot race a reload's teardown: refused for the
        # whole apply window, with the reason, rather than reaching a manager
        # that is being shut down or has not started yet (#144, story 15).
        if (cmd in LIFECYCLE_VERBS
                and self._service is not None and self._service.reloading):
            from .service import RELOAD_IN_PROGRESS
            return {"ok": False, "error": f"Cannot {cmd}: {RELOAD_IN_PROGRESS}"}

        # list without a specific connector → aggregate across all entries
        if cmd == "list" and not connector_name:
            all_watchers: list = []
            errors: list = []
            for entry in self._entries:
                try:
                    result = await entry.session_manager.dispatch_command(request)
                except Exception as e:
                    # An unexpected exception from one connector must not
                    # abort the entire list — other connectors' watchers are
                    # still valid.  Capture the error with connector attribution
                    # so the caller can see which connector is broken.
                    logger.error(
                        "dispatch_command('list') raised for connector '%s': %s",
                        entry.name,
                        e,
                    )
                    errors.append({"connector": entry.name, "error": str(e)})
                    continue
                if result.get("ok"):
                    all_watchers.extend(result.get("data", []))
                else:
                    errors.append({
                        "connector": entry.name,
                        "error": result.get("error", "unknown error"),
                    })
            return {
                "ok": len(errors) == 0,
                "data": all_watchers,
                **({"errors": errors} if errors else {}),
            }

        # send: route directly to a connector's send_to_room method
        if cmd == "send":
            return await self._handle_send(request, connector_name)

        # fetch-history: on-demand channel history fetch for agent mid-session use
        if cmd == "fetch-history":
            return await self._handle_fetch_history(request)

        # instructions: static bundled docs for lazy instruction loading
        if cmd == "instructions":
            return self._handle_instructions(request)

        # schedule-migrate is the one that has to await: it asks connectors to
        # resolve room names. Handled here rather than inside the sync
        # `_handle_schedule` below, whose whole point is that it needs no await.
        if cmd == "schedule-migrate":
            return await self._handle_schedule_migrate()

        # schedule-*: managed by JobStore (no connector routing needed)
        if cmd and cmd.startswith("schedule-"):
            return self._handle_schedule(cmd, request)

        # pause/resume/reset/expire: auto-resolve connector from watcher name (watcher
        # names are globally unique across all connectors, so no --connector is needed).
        if cmd in LIFECYCLE_VERBS and not connector_name:
            watcher_name = request.get("watcher_name", "")
            entry = self._find_entry_for_watcher(watcher_name)
            if isinstance(entry, dict):
                return entry  # error response (unknown watcher)
            return _degraded_error(entry) or await entry.session_manager.dispatch_command(request)

        # All other commands: route to a specific entry
        entry = self._resolve_entry(connector_name)
        if isinstance(entry, dict):
            return entry  # error response
        if cmd != "list":
            refused = _degraded_error(entry)
            if refused:
                return refused

        return await entry.session_manager.dispatch_command(request)

    async def _handle_service_command(self, cmd: str, request: dict) -> dict:
        """`config-reload` and `config-show` — routed to the service.

        `config-show` answers `status` too: with `include_config: false` it
        returns the digest, load time and degraded sections without the dump.
        """
        if self._service is None:
            return {"ok": False, "error": f"'{cmd}' is not available on this control server"}
        if cmd == "config-reload":
            dry_run = request.get("dry_run", False)
            if not isinstance(dry_run, bool):
                return {"ok": False, "error": "'dry_run' must be a boolean"}
            config_path = request.get("config_path")
            if config_path is not None and not isinstance(config_path, str):
                return {"ok": False, "error": "'config_path' must be a string"}
            try:
                return await self._service.reload_config(dry_run=dry_run, config_path=config_path)
            except Exception as exc:
                logger.exception("config reload failed")
                return {"ok": False, "error": f"config reload failed: {exc}"}
        include = request.get("include_config", True)
        if not isinstance(include, bool):
            return {"ok": False, "error": "'include_config' must be a boolean"}
        return self._service.describe_config(include_config=include)

    def _handle_instructions(self, request: dict) -> dict:
        """Return a bundled instruction document by name."""
        from .instructions import read_instruction

        name = request.get("name")
        if not isinstance(name, str) or not name:
            return {"ok": False, "error": "Missing 'name' field in instructions command"}
        try:
            return {"ok": True, "name": name, "text": read_instruction(name)}
        except (OSError, ValueError) as exc:
            return {"ok": False, "error": str(exc)}

    def _resolve_entry(
        self, connector_name: str | None
    ) -> "ConnectorEntry | dict":
        """Resolve a connector name to a ConnectorEntry, or return an error dict."""
        if connector_name:
            entry = next((e for e in self._entries if e.name == connector_name), None)
            if entry is None:
                return {"ok": False, "error": f"Unknown connector: {connector_name!r}"}
            return entry

        if not self._entries:
            return {"ok": False, "error": "No connectors configured"}
        # Require explicit connector selection when multiple are configured.
        if len(self._entries) > 1:
            names = ", ".join(f"'{e.name}'" for e in self._entries)
            return {
                "ok": False,
                "error": (
                    f"Multiple connectors configured ({names}). "
                    f"Please specify --connector <name>."
                ),
            }
        return self._entries[0]

    def _find_entry_for_watcher(self, watcher_name: str) -> "ConnectorEntry | dict":
        """Find the ConnectorEntry that owns the named watcher.

        Watcher names are globally unique because the handle is injective by
        construction (Codex round 8 found the old claim false): `:` joins
        connector and room label, a connector name may not contain `:`
        (config load refuses it), and the label encoder percent-encodes `:`
        out of room and user names — so no two (connector, room) pairs can
        derive one handle, and searching all entries by name is unambiguous.
        Returns an error dict if no entry owns the watcher.
        """
        if not watcher_name:
            return {"ok": False, "error": "Missing 'watcher_name'"}
        # The persisted record is the only watcher identity left (§2.8) —
        # the config lookup died with the static shape.
        for entry in self._entries:
            if entry.session_manager.get_watcher_state(watcher_name) is not None:
                return entry
        return {"ok": False, "error": f"Unknown watcher: {watcher_name!r}"}

    def _handle_schedule(self, cmd: str, request: dict) -> dict:
        """Route schedule-* commands to the JobStore.

        All sub-handlers are synchronous (they call JobStore.save() which is
        synchronous file I/O).  This method is therefore a plain def — not async —
        to make the call-site ``return self._handle_schedule(cmd, request)`` in
        ``dispatch_command`` accurate and avoid the misleading impression that there
        is any I/O awaiting happening here.
        """
        if self._job_store is None:
            return {"ok": False, "error": "Scheduler is not enabled (JobStore not configured)"}

        if cmd == "schedule-create":
            return self._handle_schedule_create(request)
        if cmd == "schedule-list":
            return self._handle_schedule_list(request)
        if cmd == "schedule-delete":
            return self._handle_schedule_delete(request)
        if cmd == "schedule-pause":
            return self._handle_schedule_pause(request)
        if cmd == "schedule-resume":
            return self._handle_schedule_resume(request)
        return {"ok": False, "error": f"Unknown schedule command: {cmd!r}"}

    async def _handle_schedule_migrate(self) -> dict:
        """`schedule migrate`: bring jobs.json up to the current schema.

        Runs in the daemon because resolving a room name needs the connectors.
        Reports every job it looked at, changed or not — the output IS the record
        of what happened, which is the reason this is a command rather than
        something done invisibly at fire time.
        """
        if self._job_store is None:
            return {
                "ok": False,
                "error": "Scheduler is not enabled (JobStore not configured)",
            }
        from .core.job_migrate import migrate

        try:
            report = await migrate(self._job_store, self._entries)
        except ValueError as exc:
            # The file is newer than this code — there is nothing safe to do.
            return {"ok": False, "error": str(exc)}
        except Exception as exc:
            logger.exception("schedule migrate failed")
            return {"ok": False, "error": f"Migration failed: {exc}"}
        return {"ok": True, **report.to_dict()}

    def _handle_schedule_create(self, request: dict) -> dict:
        from datetime import UTC, datetime

        from .core.scheduler import compute_next_run
        from .schedule_types import JobStatus, ScheduledJob

        watcher = (request.get("watcher") or "").strip()
        message = request.get("message", "")
        cron = request.get("cron", "")
        times = request.get("times", 0)

        if not watcher:
            return {"ok": False, "error": "Missing 'watcher' field"}

        # Resolve the connector early — we need its timezone as the default
        # fallback when --tz is not supplied by the caller.
        entry = self._find_entry_for_watcher(watcher)
        if isinstance(entry, dict):
            available = self._list_all_watcher_names()
            hint = f" Available watchers: {available}" if available else ""
            return {"ok": False, "error": f"Watcher {watcher!r} not found in any connector.{hint}"}

        connector_tz = entry.connector.timezone  # "" → _server_local_timezone() inside connector
        timezone = request.get("timezone") or connector_tz or _server_local_timezone()

        if not isinstance(message, str):
            return {"ok": False, "error": "'message' must be a string"}
        if not message:
            return {"ok": False, "error": "Missing 'message' field"}
        if len(message) > 4096:
            return {"ok": False, "error": "'message' must be at most 4096 characters"}
        if not cron:
            return {"ok": False, "error": "Missing 'cron' field"}
        if isinstance(times, bool) or not isinstance(times, int) or times < 0:
            return {"ok": False, "error": "'times' must be a non-negative integer (0 = forever)"}

        # Validate timezone string (M6): reject invalid IANA names at creation time
        # so the job is never stored with a timezone that compute_next_run silently
        # falls back from, emitting a spurious warning on every tick.
        try:
            import zoneinfo as _zi
            _zi.ZoneInfo(timezone)
        except (_zi.ZoneInfoNotFoundError, KeyError):
            return {"ok": False, "error": f"Unknown timezone {timezone!r}. Use an IANA name (e.g. 'America/Los_Angeles', 'UTC')."}
        except Exception as e:
            return {"ok": False, "error": f"Failed to validate timezone {timezone!r}: {e}"}

        # Validate cron expression
        try:
            from croniter import croniter  # type: ignore[import-untyped]
            if not croniter.is_valid(cron):
                return {"ok": False, "error": f"Invalid cron expression: {cron!r}"}
        except Exception as e:
            return {"ok": False, "error": f"Failed to validate cron expression: {e}"}

        now = datetime.now(UTC)
        next_run_override = request.get("next_run")
        if next_run_override is not None:
            # Validate: must be a parseable, timezone-aware ISO 8601 string.
            # An untrusted or malformed value stored verbatim would cause the
            # scheduler to skip the job (ValueError on parse) or fire it
            # immediately on every tick (past timestamp).
            try:
                nr_dt = datetime.fromisoformat(next_run_override)
            except ValueError:
                return {
                    "ok": False,
                    "error": (
                        f"Invalid 'next_run' value {next_run_override!r}: "
                        "must be an ISO 8601 datetime string "
                        "(e.g. '2026-04-10T15:30:00+00:00')"
                    ),
                }
            if nr_dt.tzinfo is None:
                return {
                    "ok": False,
                    "error": (
                        f"'next_run' value {next_run_override!r} is missing timezone info. "
                        "Use UTC offset (e.g. '+00:00') or 'Z'."
                    ),
                }
            # C3: reject past timestamps — a past next_run causes the scheduler
            # to fire the job on the very next tick, bypassing the intended schedule.
            if nr_dt < now:
                return {
                    "ok": False,
                    "error": (
                        f"'next_run' value {next_run_override!r} is in the past. "
                        "Provide a future datetime."
                    ),
                }
            next_run = next_run_override
        else:
            try:
                next_run = compute_next_run(cron, timezone, after=now)
            except Exception as e:
                return {"ok": False, "error": f"Failed to compute next run time: {e}"}

        # The record is already in hand — `_find_entry_for_watcher` above refuses
        # the create unless one exists — so the room's identity costs nothing to
        # capture here, and capturing it is what lets the job outlive the record.
        #
        # Refused rather than defaulted to `""`. A job with no room id cannot
        # bring its watcher back once the record is reclaimed — it fails at every
        # slot — and since the daemon now warns at startup about exactly that,
        # creating one would raise a warning the operator can never clear by
        # migrating, because there is nothing wrong with the FILE. The empty case
        # means the watcher's own record is missing its room, which is a broken
        # record, not a job problem: say so instead of persisting the consequence.
        record = entry.session_manager.get_watcher_state(watcher)
        if record is None or not record.room_id:
            return {
                "ok": False,
                "error": (
                    f"Watcher {watcher!r} has no recorded room, so a job created "
                    f"against it could never deliver. Send a message in the room "
                    f"to rebuild the record, then create the job."
                ),
            }
        job = ScheduledJob(
            watcher=watcher,
            connector=entry.name,
            room_id=record.room_id,
            message=message,
            cron=cron,
            timezone=timezone,
            times=times,
            status=JobStatus.ACTIVE,
            created_at=now.isoformat(),
            next_run=next_run,
        )
        try:
            self._job_store.add(job)
        except Exception as e:
            return {"ok": False, "error": f"Failed to save job: {e}"}
        return {"ok": True, "job_id": job.id, "next_run": next_run}

    def _handle_schedule_list(self, request: dict) -> dict:
        connector = request.get("connector")
        include_completed = request.get("include_completed", False)
        try:
            jobs = self._job_store.list_jobs(connector=connector, include_completed=include_completed)
            rows = [j.to_dict() for j in jobs]
            # The WATCHER column is the handle as it is NOW, derived from the
            # room the job records, not the spelling stored at creation: a
            # handle follows a room rename (§2.3), and the operator must be
            # able to type what this column shows into `pause`/`expire`.
            by_name = {e.name: e for e in self._entries}
            for row in rows:
                entry = by_name.get(row.get("connector") or "")
                if entry is None or not row.get("room_id"):
                    continue
                record = entry.session_manager.record_for_room(row["room_id"])
                if record is not None and record.watcher_name:
                    row["watcher"] = record.watcher_name
            return {"ok": True, "jobs": rows}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def _handle_schedule_delete(self, request: dict) -> dict:
        job_id = request.get("job_id", "")
        if not job_id:
            return {"ok": False, "error": "Missing 'job_id' field"}
        removed = self._job_store.remove(job_id)
        if not removed:
            return {"ok": False, "error": f"Job {job_id!r} not found"}
        return {"ok": True}

    def _handle_schedule_pause(self, request: dict) -> dict:
        from .schedule_types import JobStatus
        job_id = request.get("job_id", "")
        if not job_id:
            return {"ok": False, "error": "Missing 'job_id' field"}
        job = self._job_store.get(job_id)
        if not job:
            return {"ok": False, "error": f"Job {job_id!r} not found"}
        if job.status == JobStatus.COMPLETED:
            return {"ok": False, "error": f"Job {job_id!r} is already completed"}
        if job.status == JobStatus.CANCELLED:
            return {"ok": False, "error": f"Job {job_id!r} is cancelled — 'schedule resume' restores it"}
        if job.status == JobStatus.PAUSED:
            return {"ok": True}  # idempotent: already paused
        job.status = JobStatus.PAUSED
        try:
            self._job_store.update(job)
        except Exception as e:
            return {"ok": False, "error": str(e)}
        return {"ok": True}

    def _handle_schedule_resume(self, request: dict) -> dict:
        from datetime import UTC, datetime

        from .core.scheduler import compute_next_run
        from .schedule_types import JobStatus
        job_id = request.get("job_id", "")
        if not job_id:
            return {"ok": False, "error": "Missing 'job_id' field"}
        job = self._job_store.get(job_id)
        if not job:
            return {"ok": False, "error": f"Job {job_id!r} not found"}
        if job.status == JobStatus.COMPLETED:
            return {"ok": False, "error": f"Job {job_id!r} is already completed and cannot be resumed"}
        if job.status == JobStatus.ACTIVE:
            return {"ok": True, "next_run": job.next_run}  # idempotent: already active
        # Compute next_run BEFORE mutating status so that a bad cron expression
        # leaves the job in its current (paused or cancelled) state rather than half-resuming.
        try:
            next_run = compute_next_run(job.cron, job.timezone, after=datetime.now(UTC))
        except Exception as e:
            return {"ok": False, "error": f"Failed to compute next_run: {e}"}
        job.status = JobStatus.ACTIVE
        job.next_run = next_run
        # Resume is also the restore for a job the gateway cancelled: the
        # record was kept for exactly this. The cancellation's own fields go
        # with the status, so a restored job does not read as both.
        job.cancelled_at = None
        job.cancel_reason = ""
        try:
            self._job_store.update(job)
        except Exception as e:
            return {"ok": False, "error": str(e)}
        return {"ok": True, "next_run": job.next_run}

    def _list_all_watcher_names(self) -> str:
        """Return a comma-separated string of all configured watcher names."""
        names: list[str] = []
        seen: set[str] = set()
        for entry in self._entries:
            for name in entry.session_manager.get_all_watcher_names():
                if name not in seen:
                    names.append(name)
                    seen.add(name)
        return ", ".join(f"{n!r}" for n in sorted(names))

    def _entries_serving_room(self, room: str) -> list:
        """The connectors whose lifecycle has a record for `room`.

        `room` is whatever the caller typed: a room id, the room's name, or a
        watcher handle. Records are the deterministic answer — a room the
        gateway serves has one — and the lookup is in memory; no connector is
        asked anything. Issue #136: `send` with no `--connector` on a
        multi-connector gateway was refused before the room was looked at, and
        the agent (whose own room always has a record) had to recover by
        guessing from its identity header.
        """
        from .core.state import StateFilter

        serving = []
        for entry in self._entries:
            sm = entry.session_manager
            if sm.record_for_room(room) is not None or sm.resolve_handle(room):
                serving.append(entry)
                continue
            if any(row.get("room_name") == room
                   for row in sm.list_watchers(StateFilter.ALL)):
                serving.append(entry)
        return serving

    async def _handle_send(self, request: dict, connector_name: str | None) -> dict:
        """Handle the 'send' command: route to a connector's send_to_room method.

        With no `--connector` on a multi-connector gateway, the room decides:
        exactly one connector with a record for it is used; none or several fall
        back to the explicit-connector error (#136).
        """
        room = request.get("room", "")
        if not connector_name and len(self._entries) > 1 and room:
            serving = self._entries_serving_room(room)
            if len(serving) == 1:
                connector_name = serving[0].name
            elif len(serving) > 1:
                names = ", ".join(f"'{e.name}'" for e in serving)
                return {"ok": False, "error": (
                    f"Room {room!r} is served by more than one connector ({names}). "
                    f"Please specify --connector <name>.")}
        entry = self._resolve_entry(connector_name)
        if isinstance(entry, dict):
            return entry  # error response
        refused = _degraded_error(entry)
        if refused:
            return refused

        text = request.get("text", "")
        attachment_path = request.get("attachment_path")

        if not room:
            return {"ok": False, "error": "Missing 'room' field in send command"}
        if not text and not attachment_path:
            return {"ok": False, "error": "Nothing to send: provide 'text' or 'attachment_path'"}

        try:
            await entry.connector.send_to_room(room, text, attachment_path=attachment_path)
            return {"ok": True}
        except Exception as e:
            logger.error("send_to_room failed for connector '%s': %s", entry.name, e)
            return {"ok": False, "error": str(e)}

    async def _handle_fetch_history(self, request: dict) -> dict:
        """Handle the 'fetch-history' command: on-demand channel history for agents.

        Resolves the watcher name to its connector + room, applies the
        max_fetch_count cap, fetches filtered history, and returns a formatted
        block string the agent can read directly from Bash stdout.

        Security: --watcher is honor-system in v1 (see issue #34 for token auth).
        Content security is enforced by the connector's allowlist filter — same
        boundary as live message processing.
        """
        from datetime import datetime as _datetime

        from .core.history_context import format_history_context

        watcher_name = request.get("watcher", "")
        if not watcher_name:
            return {"ok": False, "error": "Missing 'watcher' field in fetch-history command"}

        entry = self._find_entry_for_watcher(watcher_name)
        if isinstance(entry, dict):
            return entry  # error: unknown watcher
        refused = _degraded_error(entry)
        if refused:
            return refused

        if not entry.connector.supports_history():
            return {
                "ok": False,
                "error": (
                    f"Connector '{entry.name}' does not support history fetch. "
                    f"fetch-history is only available for connectors that implement "
                    f"fetch_room_history() (e.g. Rocket.Chat)."
                ),
            }

        state = entry.session_manager.get_watcher_state(watcher_name)
        if state is None or not state.room_id:
            return {"ok": False, "error": f"No watcher record found for '{watcher_name}'"}
        wc = config_from_record(state)
        if wc is None:
            return {"ok": False, "error": f"Watcher record for '{watcher_name}' carries no config"}

        # Validate and parse count.
        raw_count = request.get("count", 50)
        try:
            count = int(raw_count)
        except (TypeError, ValueError):
            return {"ok": False, "error": f"'count' must be a positive integer, got {raw_count!r}"}
        if count <= 0:
            return {"ok": False, "error": f"'count' must be > 0, got {count}"}

        # Validate and parse verbatim.
        raw_verbatim = request.get("verbatim", 15)
        try:
            verbatim = int(raw_verbatim)
        except (TypeError, ValueError):
            return {"ok": False, "error": f"'verbatim' must be a non-negative integer, got {raw_verbatim!r}"}
        if verbatim < 0:
            return {"ok": False, "error": f"'verbatim' must be >= 0, got {verbatim}"}

        # Apply server-side cap to protect against context window overload.
        max_count = wc.history_handoff.max_fetch_count
        clamped = count > max_count
        if clamped:
            count = max_count

        # Validate before_ts and after_ts format if provided.
        # Reuse _datetime (already imported above) — no second alias needed.
        before_ts = request.get("before_ts") or None
        before_dt = None
        if before_ts:
            try:
                before_dt = _datetime.fromisoformat(before_ts)
            except ValueError:
                return {
                    "ok": False,
                    "error": (
                        f"Invalid 'before_ts' value {before_ts!r}: "
                        "must be an ISO 8601 timestamp (e.g. '2026-05-10T10:00:00+08:00')"
                    ),
                }

        after_ts = request.get("after_ts") or None
        after_dt = None
        if after_ts:
            try:
                after_dt = _datetime.fromisoformat(after_ts)
            except ValueError:
                return {
                    "ok": False,
                    "error": (
                        f"Invalid 'after_ts' value {after_ts!r}: "
                        "must be an ISO 8601 timestamp (e.g. '2026-05-10T10:00:00+08:00')"
                    ),
                }

        # Cross-validate: after_ts must be strictly earlier than before_ts.
        # Wrap in try/except to handle mixed tz-aware/naive comparison gracefully.
        if after_dt and before_dt:
            try:
                if after_dt >= before_dt:
                    return {
                        "ok": False,
                        "error": (
                            f"'after_ts' ({after_ts!r}) must be earlier than "
                            f"'before_ts' ({before_ts!r})"
                        ),
                    }
            except TypeError:
                pass  # mixed tz-aware/naive: skip comparison, connector will handle

        try:
            # By id, never by name (§2.8): the record's `room` is a
            # description — a group DM's description resolves to nothing.
            room = Room(
                id=state.room_id,
                name=state.room_name or watcher_name,
                type=state.room_kind or state.room_type or "channel",
            )
            # Converted here, at the one boundary where a human types a
            # timestamp: connector bounds are epoch milliseconds like every
            # other timestamp inside ACG (§5.2). Both values are already
            # parsed above, so this reuses the datetimes rather than the
            # strings.
            msgs = await entry.connector.fetch_room_history(
                room,
                count,
                before_ts=_to_epoch_ms(before_dt),
                after_ts=_to_epoch_ms(after_dt),
            )
        except Exception as e:
            logger.error("fetch_room_history failed for watcher '%s': %s", watcher_name, e)
            return {"ok": False, "error": str(e)}

        fetched_at = _datetime.now().astimezone().isoformat(timespec="seconds")
        text = format_history_context(
            msgs,
            verbatim_tail=verbatim,
            fetched_at=fetched_at,
            on_demand=True,
        ) or ""

        if clamped:
            warning = (
                f"\n\n[fetch-history] Note: --count was clamped from {raw_count} "
                f"to {max_count} (max_fetch_count limit). Use a smaller --count or adjust "
                f"history_handoff.max_fetch_count in config."
            )
            text = text + warning if text else warning.strip()

        return {"ok": True, "history": text}
