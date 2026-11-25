"""``coop agent run``: the utility for CLI agents (design §3.4). One process per
agent on the agent's host, a doorbell client that, on each wake, runs one command,
waits for it to exit, and sends ``done --failed`` if the agent did not.

State lives under one directory: ``sessions.json`` (the session or thread id per
room) and ``rules.md`` (the room rules at the version the last wake carried).
"""

from __future__ import annotations

import asyncio
import json
import os
import signal
import sys
from dataclasses import dataclass, field
from pathlib import Path

import httpx

from ..errors import DoorbellError
from ..room.model import Wake
from .adapters import Adapter, Turn
from .client import HttpDoorbell

TERMINATE_GRACE_SECONDS = 5.0  # between SIGTERM and SIGKILL to the command's process group
LEASE_MARGIN_SECONDS = 1.0  # the command is stopped this long before the lease ends, so the done lands under it
RETRY_SECONDS = (2.0, 4.0, 8.0, 16.0, 32.0, 60.0)  # the gateway unreachable: wait, poll again
STOPPED = "utility stopped"


@dataclass
class Runner:
    doorbell: HttpDoorbell
    adapter: Adapter
    state_dir: Path
    workdir: Path
    # What the child needs: the connection as ``COOP_*`` and a directory with ``coop``
    # on ``PATH``, so the bare ``coop read --wake …`` the wake text names works.
    env: dict[str, str] = field(default_factory=dict)
    rules_version: int = -1
    sessions: dict[str, str] = field(default_factory=dict)
    stopping: bool = False
    _poll: asyncio.Task | None = field(default=None, repr=False)
    _process: asyncio.subprocess.Process | None = field(default=None, repr=False)
    _ended_by_stop: bool = field(default=False, repr=False)

    def __post_init__(self) -> None:
        self.state_dir.mkdir(parents=True, exist_ok=True)
        path = self.state_dir / "sessions.json"
        if path.exists():
            self.sessions = {str(k): str(v) for k, v in json.loads(path.read_text()).items()}
        if (self.state_dir / "rules.md").exists() and (self.state_dir / "rules.version").exists():
            self.rules_version = int((self.state_dir / "rules.version").read_text().strip() or -1)

    @property
    def rules(self) -> Path:
        return self.state_dir / "rules.md"

    # ── the loop ────────────────────────────────────────────────────────────

    async def serve(self, wait: float = 30.0) -> int:
        """Long-poll until stopped; one wake at a time. Returns the exit code: 0 when
        stopped, 1 when the token is refused."""
        retry = 0
        while not self.stopping:
            self._poll = asyncio.ensure_future(self.doorbell.poll(wait))
            try:
                wake = await self._poll
            except asyncio.CancelledError:
                if not self.stopping:
                    raise
                break
            except DoorbellError as exc:
                if exc.status == 401:
                    print(f"[ERROR] the gateway refuses the token: {exc.message}", file=sys.stderr)
                    return 1
                retry = await self._wait_out(exc, retry)
                continue
            except (httpx.HTTPError, OSError) as exc:  # the gateway unreachable, a reset, a timeout
                retry = await self._wait_out(exc, retry)
                continue
            finally:
                self._poll = None
            retry = 0
            if wake is not None and not self.stopping:
                await self.take(wake)
        return 0

    async def _wait_out(self, exc: Exception, retry: int) -> int:
        seconds = RETRY_SECONDS[min(retry, len(RETRY_SECONDS) - 1)]
        print(f"[WARNING] the gateway did not answer the poll ({exc}); polling again in {seconds:.0f} s", file=sys.stderr)
        try:
            await asyncio.sleep(seconds)
        except asyncio.CancelledError:
            if not self.stopping:
                raise
        return retry + 1

    def stop(self) -> None:
        """SIGTERM/SIGINT: the poll in flight is cancelled, the command's group ended, and
        its wake fails with ``utility stopped``."""
        self.stopping = True
        if self._poll is not None:
            self._poll.cancel()
        if self._process is not None:
            self._ended_by_stop = True
            asyncio.ensure_future(_end_group(self._process))

    async def take(self, wake: Wake) -> None:
        """One wake: the rules current, the session chosen, the command run to its
        exit or the lease's end, then ``done --failed`` unless the agent closed it."""
        try:
            await self._refresh_rules(wake)
        except (DoorbellError, httpx.HTTPError, OSError) as exc:
            if isinstance(exc, DoorbellError) and exc.status == 401:
                raise
            await self._done(wake, f"rules could not be fetched: {exc}")
            return
        session, first = self._session_for(wake)
        turn = Turn(wake_text(wake), self.rules, session, first)
        try:
            command = self.adapter.command(turn)
        except OSError as exc:
            self._forget_unused(wake, first)
            await self._done(wake, f"command could not be built: {exc}")
            return
        reason, stdout = await self._run(wake, command)
        self._after(wake, session, first, reason, stdout)
        await self._done(wake, reason)

    # ── the parts ───────────────────────────────────────────────────────────

    async def _refresh_rules(self, wake: Wake) -> None:
        if wake.rules_version <= self.rules_version and self.rules.exists():
            return
        version, text = await self.doorbell.rules()
        _atomic_write(self.rules, text)
        _atomic_write(self.state_dir / "rules.version", f"{version}\n")
        self.rules_version = version

    def _session_for(self, wake: Wake) -> tuple[str | None, bool]:
        """The room's session, or a new one: on a ``reset`` wake, or when none is held.
        A CLI that names its own session gets ``None`` on the first turn."""
        if wake.reset and wake.room in self.sessions:
            self.sessions.pop(wake.room)  # gone now: a reset whose turn fails must not resume the old one
            self._save_sessions()
        held = self.sessions.get(wake.room)
        if held is not None:
            return held, False
        if self.adapter.new_session is None:
            return None, True
        chosen = self.adapter.new_session()
        self.sessions[wake.room] = chosen  # kept now: a first turn the lease ends still has its session
        self._save_sessions()
        return chosen, True

    async def _run(self, wake: Wake, command: list[str]) -> tuple[str, str]:
        """Run the command in its own process group until it exits or the lease ends.
        Returns the failure reason (the agent may have closed the wake itself; the
        gateway's answer to ``done`` says) and the command's stdout."""
        try:
            process = await asyncio.create_subprocess_exec(
                *command, cwd=self.workdir, env={**os.environ, **self.env}, stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, start_new_session=True)
        except OSError as exc:
            return f"command failed to start: {exc}", ""
        self._process, self._ended_by_stop = process, False
        timeout = None if wake.lease_expires_at is None else max(0.0, wake.lease_expires_at - LEASE_MARGIN_SECONDS - _now())
        # never cancel ``communicate()``: a cancelled read drops what the command printed
        reading = asyncio.ensure_future(process.communicate())
        done, _ = await asyncio.wait({reading}, timeout=timeout)
        reason = None
        if not done:
            reason = "lease expired"
            await _end_group(process)
            done, _ = await asyncio.wait({reading}, timeout=TERMINATE_GRACE_SECONDS)
        self._process = None
        if not done:  # a survivor holds the pipes: the group was killed, the output is unreadable
            reading.cancel()
            print(f"[WARNING] wake {wake.wake_id}: the command's output could not be read after the kill", file=sys.stderr)
            return STOPPED if self._ended_by_stop else reason or "lease expired", ""
        out, err = reading.result()
        stdout = out.decode(errors="replace")
        if err.strip():
            print(f"[{self.adapter.kind}] stderr for {wake.wake_id}:\n{err.decode(errors='replace').rstrip()}", file=sys.stderr)
        if self._ended_by_stop:
            return STOPPED, stdout
        if reason is not None:
            return reason, stdout
        return f"no done from agent (exit {process.returncode})", stdout

    def _after(self, wake: Wake, session: str | None, first: bool, reason: str, stdout: str) -> None:
        """Keep the session the output names; drop one that did not resume."""
        named = self.adapter.session_in(stdout)
        if named is not None and named != session:
            self.sessions[wake.room] = named  # the CLI named it (Codex), or renamed ours
            self._save_sessions()
            return
        if first and reason.startswith("command"):
            self._forget_unused(wake, first)
            return
        failed_resume = (not first and named is None and reason.startswith("no done from agent (exit")
                         and not reason.endswith("(exit 0)") and not self.adapter.resume_creates)
        if failed_resume:
            print(f"[WARNING] {self.adapter.kind} session {session} for {wake.room} did not resume; dropped — the next "
                  f"wake starts a new session, or run coop reset <agent> {wake.room} to reset the room too",
                  file=sys.stderr)
            self.sessions.pop(wake.room, None)
            self._save_sessions()

    def _forget_unused(self, wake: Wake, first: bool) -> None:
        """A session chosen for a first turn that never ran: not kept, so the next wake
        is a first turn again rather than a resume of nothing."""
        if first and self.sessions.pop(wake.room, None) is not None:
            self._save_sessions()

    async def _done(self, wake: Wake, reason: str) -> None:
        """``done --failed`` unless the agent closed the wake: the gateway's answer is
        the one place that knows (a closed wake answers false)."""
        try:
            closed = await self.doorbell.done(wake.wake_id, failed=reason)
        except DoorbellError as exc:
            if exc.status == 401:
                raise
            print(f"[WARNING] done for {wake.wake_id} was refused ({exc}); the gateway's lease expiry fails the turn",
                  file=sys.stderr)
            return
        except (httpx.HTTPError, OSError) as exc:
            print(f"[WARNING] done for {wake.wake_id} got no answer ({exc}); the next wake's read shows what happened",
                  file=sys.stderr)
            return
        # a stale done after an exit is the normal end of a turn the agent closed; a lease
        # the utility ended, or a command that never ran, is said whatever the gateway answers
        if closed or not reason.startswith("no done from agent"):
            print(f"[WARNING] wake {wake.wake_id}: {reason}", file=sys.stderr)

    def _save_sessions(self) -> None:
        _atomic_write(self.state_dir / "sessions.json", json.dumps(self.sessions, indent=1, sort_keys=True) + "\n")


def wake_text(wake: Wake) -> str:
    """The short prompt a wake becomes (design §3.4)."""
    return (f"You are {wake.bot} in room {wake.room}; wake {wake.wake_id} ({wake.source}, thread {wake.starting_thread}). "
            f"Run `coop read --wake {wake.wake_id}`, reply with `coop send --wake {wake.wake_id} …`, "
            f"then `coop done --wake {wake.wake_id}`.")


async def _end_group(process: asyncio.subprocess.Process) -> None:
    """Terminate the command's process group, then kill it: a CLI spawns tools, and a
    tool left running could send after the lease. The kill goes to the group even
    when the command itself has exited, for the tool that outlived it."""
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    try:
        await asyncio.wait_for(process.wait(), TERMINATE_GRACE_SECONDS)
    except TimeoutError:
        pass
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass


def _atomic_write(path: Path, text: str) -> None:
    temp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temp.write_text(text)
    os.replace(temp, path)


def _now() -> float:
    import time

    return time.time()
