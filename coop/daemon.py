"""The daemon process: one per runtime directory, held by a pid file; started in the
background by ``coop start`` with a pipe that carries the startup outcome back to
the operator, or in the foreground for a terminal. The pid lock, the double fork
and the startup pipe are the frozen gateway's, cut to what this daemon needs.
"""

from __future__ import annotations

import asyncio
import logging
import os
import signal
import sys
import time
from pathlib import Path

from . import paths
from .config import ConfigError, Server, load
from .connectors.base import Connector, OnEvent
from .control import ControlServer
from .room.trace import Trace, TraceEvent
from .service import Service

logger = logging.getLogger(__name__)


# ── the pid lock ────────────────────────────────────────────────────────────


def acquire_lock(pid_file: Path = paths.PID_FILE) -> None:
    """Create the pid file, or fail when a live daemon holds it. A file left by a daemon
    that is gone is replaced once. ``RuntimeError`` names the live holder. This is a pid
    file, not a lock: two starts in the same few milliseconds can both pass (the stale
    check and the create are two steps, as is ``is_running``'s cleanup); the loser then
    fails on the control socket. Closing that window means an ``flock`` held for the
    daemon's life, which is not worth its state at the rate two starts coincide."""
    pid_file.parent.mkdir(parents=True, exist_ok=True)
    for attempt in range(2):
        try:
            fd = os.open(pid_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
        except FileExistsError:
            holder = locked_pid(pid_file)
            if holder is not None:
                raise RuntimeError(f"coop daemon already running (pid={holder})") from None
            if attempt == 0:
                pid_file.unlink(missing_ok=True)  # stale: the next attempt creates it
                continue
            raise
        with os.fdopen(fd, "w") as f:
            f.write(str(os.getpid()))
        return
    raise RuntimeError(f"could not take the pid file {pid_file}")


def release_lock(pid_file: Path = paths.PID_FILE) -> None:
    """Remove the pid file when it is ours, or stale; never another live daemon's."""
    try:
        holder = int(pid_file.read_text().strip())
    except (OSError, ValueError):
        pid_file.unlink(missing_ok=True)
        return
    if holder == os.getpid() or locked_pid(pid_file) is None:
        pid_file.unlink(missing_ok=True)


def locked_pid(pid_file: Path = paths.PID_FILE) -> int | None:
    """The pid of the live daemon holding the lock, or none: a missing file, an
    unreadable one, or a pid that is no longer a process."""
    try:
        pid = int(pid_file.read_text().strip())
    except (OSError, ValueError):
        return None
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return None
    except PermissionError:
        return pid
    return pid


def is_running(pid_file: Path = paths.PID_FILE) -> tuple[bool, int | None]:
    pid = locked_pid(pid_file)
    if pid is None:
        release_lock(pid_file)  # stale or absent
        return False, None
    return True, pid


# ── the connectors ──────────────────────────────────────────────────────────


# What the room core does, one log line per event (``coop/room/trace.py`` lists the
# kinds); measure mode's outcomes are its ``rule`` events (design §1.1).
trace_logger = logging.getLogger("coop.trace")
TRACE_LEVELS = {"sweep": logging.DEBUG}  # a timer's tick per bot and room: noise at INFO


def log_trace(event: TraceEvent) -> None:
    trace_logger.log(TRACE_LEVELS.get(event.kind, logging.INFO), "%s", event.describe())


def daemon_trace() -> Trace:
    """The daemon's trace: every event is logged and none is kept in memory."""
    return Trace(sink=log_trace, keep=False)


def make_connector(server: Server, on_event: OnEvent) -> Connector:
    if server.type == "mattermost":
        from .connectors.mattermost.connector import MattermostConnector

        return MattermostConnector(server, on_event)
    raise ConfigError(f"servers.{server.name}: no connector for type {server.type!r} in this increment")


# ── running ─────────────────────────────────────────────────────────────────


async def run(config_path: Path, startup_fd: int = -1, pid_file: Path = paths.PID_FILE,
              socket_path: Path = paths.SOCKET_FILE, state_dir: Path = paths.STATE_DIR) -> int:
    """The daemon's life: start, serve until stopped, stop. Returns the exit code.
    ``startup_fd`` is the pipe to the operator's ``coop start``, written once."""
    stopping = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stopping.set)
    service: Service | None = None
    control: ControlServer | None = None
    try:
        config = load(config_path)
        service = Service(config, state_dir, make_connector, trace=daemon_trace())
        await service.start()
        control = ControlServer(service, socket_path, config_path, stopping.set, lambda: locked_pid(pid_file))
        await control.start()
    except Exception as exc:  # noqa: BLE001 - the one place that turns any startup failure into the pipe
        logger.exception("startup failed")
        _startup_report(startup_fd, [f"error:{exc}"], ok=False)
        if control is not None:
            await control.stop()
        if service is not None:
            await service.stop()
        return 1
    _startup_report(startup_fd, [f"info:{n}" for n in service.notes], ok=True)
    logger.info("coop daemon running (pid %s)", os.getpid())
    await stopping.wait()
    logger.info("stopping")
    await control.stop()
    await service.stop()
    return 0


def _startup_report(fd: int, lines: list[str], ok: bool) -> None:
    if fd < 0:
        return
    text = "".join(line.replace("\n", " ") + "\n" for line in lines) + ("ok\n" if ok else "")
    try:
        os.write(fd, text.encode())
    finally:
        os.close(fd)


def run_foreground(config_path: Path) -> int:
    """``coop start --foreground``: the daemon in this process, logging to stderr."""
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    running, pid = is_running()
    if running:
        print(f"[ERROR] coop daemon already running (pid={pid})", file=sys.stderr)
        return 1
    try:
        acquire_lock()
    except RuntimeError as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        return 1
    try:
        return asyncio.run(run(config_path))
    finally:
        release_lock()


def start_daemon(config_path: Path, log_file: Path = paths.LOG_FILE) -> int:
    """``coop start``: fork the daemon off and wait for its startup outcome."""
    running, pid = is_running()
    if running:
        print(f"coop daemon already running (pid={pid})")
        return 0
    try:
        load(config_path)  # refuse here, where the operator can read the message
    except (ConfigError, OSError) as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        return 1
    read_fd, write_fd = os.pipe()
    if os.fork() > 0:
        os.close(write_fd)
        return _wait_for_startup(read_fd, log_file)
    # the child: detach, fork again, run
    os.close(read_fd)
    os.setsid()
    if os.fork() > 0:
        os._exit(0)
    log_file.parent.mkdir(parents=True, exist_ok=True)
    log = open(log_file, "ab", buffering=0)  # noqa: SIM115 - the daemon's stdio for its whole life
    os.dup2(log.fileno(), 1)
    os.dup2(log.fileno(), 2)
    devnull = os.open(os.devnull, os.O_RDONLY)
    os.dup2(devnull, 0)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s",
                        stream=sys.stderr, force=True)
    code = 1
    try:
        acquire_lock()
    except RuntimeError as exc:
        _startup_report(write_fd, [f"error:{exc}"], ok=False)
        os._exit(1)
    try:
        code = asyncio.run(run(config_path, startup_fd=write_fd))
    except Exception:  # noqa: BLE001
        logger.exception("daemon crashed")
    finally:
        release_lock()
        os._exit(code)


def _wait_for_startup(read_fd: int, log_file: Path) -> int:
    data = b""
    try:
        while chunk := os.read(read_fd, 4096):
            data += chunk
    except OSError:
        pass
    finally:
        os.close(read_fd)
    lines = data.decode(errors="replace").splitlines()
    for line in lines:
        if line.startswith("info:"):
            print(line[5:].strip())
        elif line.startswith("error:"):
            print(f"[ERROR] {line[6:].strip()}", file=sys.stderr)
    if not any(line.strip() == "ok" for line in lines):
        print(f"[ERROR] coop daemon failed to start; see {log_file}", file=sys.stderr)
        return 1
    print("coop daemon started.")
    return 0


def stop_daemon(pid_file: Path = paths.PID_FILE, timeout: float = 30.0) -> int:
    running, pid = is_running(pid_file)
    if not running:
        print("coop daemon is not running")
        return 0
    os.kill(pid, signal.SIGTERM)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if locked_pid(pid_file) is None:
            release_lock(pid_file)
            print("coop daemon stopped.")
            return 0
        time.sleep(0.2)
    print(f"[ERROR] coop daemon (pid={pid}) did not stop within {timeout:.0f}s", file=sys.stderr)
    return 1
