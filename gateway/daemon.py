"""Daemon lifecycle: fork, PID file, signal handling."""

import asyncio
import logging
import os
import signal
import sys
import time
from pathlib import Path

from .config import GatewayConfig
from .runtime_lock import LOCK_FILE as PID_FILE  # noqa: F401 — re-exported for cli.py
from .runtime_lock import RUNTIME_DIR, locked_pid
from .runtime_lock import acquire as _lock_acquire
from .runtime_lock import release as _lock_release
from .service import GatewayService, sanitize_pipe_message

logger = logging.getLogger("coop.daemon")

# RUNTIME_DIR comes from gateway.paths (via runtime_lock), the one definition.
# PID_FILE imported from runtime_lock (shared with control.py to break circular import)
LOG_FILE = RUNTIME_DIR / "gateway.log"


def _harden_config_permissions(config_path: str) -> None:
    """Chmod config.yaml to 0600, unconditionally.

    config.yaml holds plaintext secrets, and the documented guarantee is that
    `coop start` protects it whether or not anything else ever did. Extracted
    as its own function so it's unit-testable without going through
    `os.fork()` (this whole module's docstring already notes `start_daemon()`
    itself isn't unit-testable that way).
    """
    Path(config_path).chmod(0o600)


def _setup_logging() -> None:
    """Configure file-based logging for the daemon."""
    RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
        handlers=[
            logging.FileHandler(str(LOG_FILE), encoding="utf-8"),
        ],
    )


def is_running() -> tuple[bool, int | None]:
    """Check if daemon is running. Returns (is_running, pid)."""
    pid = locked_pid()
    if pid is None:
        # Stale or absent lock file — clean up so future checks don't read it.
        # Use _lock_release() (not PID_FILE.unlink()) so the call goes through
        # runtime_lock.LOCK_FILE, which is patchable in tests.
        _lock_release()
        return False, None
    return True, pid


def _wait_for_startup_signal(read_fd: int) -> None:
    """Block reading from the startup pipe until the daemon closes it.

    Protocol written by the daemon:
      - Zero or more ``info:<message>\\n`` lines for informational notices
        that happened during startup but didn't fail anything — always
        shown, success or degraded.
      - Zero or more ``error:<message>\\n`` lines for non-fatal startup failures.
      - A final ``ok\\n`` line to confirm the startup sequence completed.

    If the pipe closes without an ``ok`` line the daemon crashed before completing
    startup (e.g. config error, unhandled exception).  In that case we
    report failure and exit 1; the specific reason lives only
    in the log (matching how every other fatal startup failure already
    behaves here — config load, lock acquisition, etc.).

    This function never returns — it always calls sys.exit().
    """
    data = b""
    try:
        while chunk := os.read(read_fd, 4096):
            data += chunk
    except OSError:
        pass
    finally:
        try:
            os.close(read_fd)
        except OSError:
            pass

    lines = data.decode(errors="replace").splitlines()
    infos = [line[len("info:"):].strip() for line in lines if line.startswith("info:")]
    errors = [line[len("error:"):].strip() for line in lines if line.startswith("error:")]
    has_ok = any(line.strip() == "ok" for line in lines)

    if not has_ok:
        print(
            f"Gateway failed to start — check logs at {LOG_FILE}",
            file=sys.stderr,
        )
        sys.exit(1)

    for msg in infos:
        print(f"  ℹ {msg}")

    if errors:
        print("Gateway started with warnings:", file=sys.stderr)
        for e in errors:
            print(f"  ⚠ {e}", file=sys.stderr)
        print(
            f"Some components may be degraded. Check {LOG_FILE} for details.",
            file=sys.stderr,
        )
        print("Gateway started (degraded).")
        sys.exit(0)

    print("Gateway started successfully.")
    sys.exit(0)


def start_daemon(config_path: str) -> None:
    """Daemonize and run the gateway service.

    Blocks the parent process until the daemon has completed its full startup
    sequence (sidecars healthy, brokers running, all watchers started).  Prints
    a clear success/failure message and exits with the appropriate code.
    """
    running, pid = is_running()
    if running:
        print(f"Gateway already running (pid={pid})")
        sys.exit(1)

    # Resolve paths before forking
    # Absolute, not resolved: the service re-reads this path on `config reload`,
    # and a repointed `config.yaml` symlink must be followed then, not frozen now.
    config_path = os.path.abspath(config_path)
    RUNTIME_DIR.mkdir(parents=True, exist_ok=True)

    # Startup handshake pipe:
    #   read_fd  — parent blocks here until daemon signals startup complete
    #   write_fd — daemon writes "error:<msg>\n"* + "ok\n" after startup, then closes
    read_fd, write_fd = os.pipe()

    # ── First fork ────────────────────────────────────────────────────────────
    pid = os.fork()
    if pid > 0:
        # Parent: close the write end and wait for the daemon to report startup.
        os.close(write_fd)
        _wait_for_startup_signal(read_fd)  # blocks; never returns

    # ── First child ───────────────────────────────────────────────────────────
    os.setsid()

    # ── Second fork (prevent controlling terminal reacquisition) ─────────────
    pid = os.fork()
    if pid > 0:
        # Intermediate process: close both ends and exit.
        # Must close write_fd explicitly so the parent only unblocks when the
        # actual daemon (second child) closes its copy.
        os.close(write_fd)
        os.close(read_fd)
        sys.exit(0)

    # ── Daemon process (second child) ─────────────────────────────────────────
    # Close the read end — daemon only writes.
    os.close(read_fd)

    # Redirect stdio → /dev/null for stdin, log file for stdout/stderr
    sys.stdout.flush()
    sys.stderr.flush()
    devnull = open(os.devnull, "r")
    os.dup2(devnull.fileno(), sys.stdin.fileno())
    devnull.close()  # FD duplicated into stdin; close the original to avoid leak
    log_fd = open(str(LOG_FILE), "a")
    os.dup2(log_fd.fileno(), sys.stdout.fileno())
    os.dup2(log_fd.fileno(), sys.stderr.fileno())
    log_fd.close()  # FD duplicated into stdout/stderr; close the original to avoid leak

    # Setup logging FIRST — must happen before any operation that could fail,
    # so errors are captured in the log file AND the parent receives a proper
    # error message instead of blocking forever on the startup pipe.
    _setup_logging()
    logger.info("Daemon started (pid=%d)", os.getpid())

    # Acquire the runtime lock (writes PID file atomically).
    # Must run AFTER _setup_logging() so any failure is both logged and
    # signalled to the parent before the process exits.
    try:
        _lock_acquire()
    except Exception as e:
        logger.error("Failed to acquire runtime lock: %s", e)
        try:
            os.write(
                write_fd,
                f"error:Failed to acquire runtime lock: {sanitize_pipe_message(str(e))}\n".encode(),
            )
            os.close(write_fd)
        except OSError:
            pass
        sys.exit(1)

    # config.yaml holds plaintext secrets — chmod it 0600 on every start.
    # Best-effort: a read-only bind mount or a config.yaml owned by a
    # different uid than the daemon process (both realistic in Docker) makes
    # chmod() raise PermissionError even though the file is perfectly
    # loadable; that must not block startup, just warn. A missing file is
    # left for from_file() below to report as the config-load failure it is.
    try:
        _harden_config_permissions(config_path)
    except FileNotFoundError:
        pass  # from_file() reports the missing config below
    except OSError as e:
        logger.warning("Could not chmod config.yaml to 0600: %s", e)

    # Load config — failure is fatal; signal the parent before exiting
    try:
        config = GatewayConfig.from_file(config_path)
    except Exception as e:
        logger.error("Failed to load config: %s", e)
        try:
            os.write(
                write_fd,
                f"error:Config load failed: {sanitize_pipe_message(str(e))}\n".encode(),
            )
        except OSError:
            pass
        try:
            os.close(write_fd)
        except OSError:
            pass
        _cleanup()
        sys.exit(1)

    # Run the service — startup_fd is passed through so service.run() signals
    # the parent once the full startup sequence completes.
    service = GatewayService(config, config_path=config_path)

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    main_task = loop.create_task(service.run(startup_fd=write_fd))

    # Handle SIGTERM for graceful shutdown
    def handle_sigterm(signum, frame):
        logger.info("Received SIGTERM, shutting down...")
        main_task.cancel()

    signal.signal(signal.SIGTERM, handle_sigterm)
    signal.signal(signal.SIGINT, handle_sigterm)

    try:
        loop.run_until_complete(main_task)
    except asyncio.CancelledError:
        # service.run() catches CancelledError internally and calls shutdown()
        # in its own finally block, so no explicit shutdown call is needed here.
        # Calling service.shutdown() again would risk a double-shutdown.
        pass
    except Exception as e:
        logger.error("Service crashed: %s", e)
        # Signal failure to parent if startup hasn't completed yet (write_fd still open)
        try:
            os.write(
                write_fd,
                f"error:Service crashed during startup: "
                f"{sanitize_pipe_message(str(e))}\n".encode(),
            )
            os.close(write_fd)
        except OSError:
            pass  # already closed — startup signal was already sent
    finally:
        _cleanup()
        loop.close()
        logger.info("Daemon exited")


def stop_daemon() -> None:
    """Stop the running daemon."""
    running, pid = is_running()
    if not running:
        print("Gateway is not running")
        return

    print(f"Stopping gateway (pid={pid})...")
    try:
        os.kill(pid, signal.SIGTERM)
        # Wait for the gateway to complete its graceful shutdown before resorting
        # to SIGKILL.  The shutdown sequence includes:
        #   - stopping session managers (processor drain: up to 30s, now parallel)
        #   - stopping agent backends (opencode: SIGTERM wait 5s + SIGKILL wait 5s)
        # With parallel processor drain the worst case is ~50s (30s drain + 20s
        # backend stop).  90s gives a comfortable margin.
        # History: the original 6s window caused opencode serve to be left as an
        # orphan on restart; extended to 30s then 90s as drain time grew.
        for _ in range(450):  # 450 × 0.2s = 90s grace period
            try:
                os.kill(pid, 0)
                time.sleep(0.2)
            except ProcessLookupError:
                break
        else:
            # Force kill if still alive after 30s grace period
            print("Force killing...")
            os.kill(pid, signal.SIGKILL)
    except ProcessLookupError:
        pass

    # Only release the lock if it still belongs to the process we killed.
    # A rapid restart (e.g. by a process supervisor) could have already written
    # its own PID to the file before we get here.  Releasing an alien PID file
    # would cause the new daemon's next is_running() check to return False,
    # allowing yet another instance to start — producing split-brain.
    current_pid = locked_pid()
    if current_pid == pid:
        _lock_release()

    print("Gateway stopped")


def _cleanup() -> None:
    """Release the runtime lock (removes PID file)."""
    _lock_release()
