"""Talking to the running AgentCoop container — the docker-facing half of the E2E rig.

Split out of `conftest.py` for the same reason `gateway_log.py` was: a pytest
conftest is a plugin, not a library. Both `tests/conftest.py` and
`tests/e2e/conftest.py` are importable as the top-level name `conftest`, so
`from conftest import ...` resolves to whichever landed in
`sys.modules["conftest"]` first — and even when it resolves correctly it builds
a SECOND module object, whose body re-executes, beside the one pytest
registered. Constants and stateless functions survive that; the day this file
holds a cache or a client, the fixture and the test would be looking at
different ones.

`gateway_log.py` keeps the pure string work, testable with no stack at all.
This module is the side that needs docker.
"""

from __future__ import annotations

import re
import subprocess

CONTAINER = "acg-e2e"
GATEWAY_LOG = "/root/.agentcoop/gateway.log"

# Must match the connector name in tests/e2e/acg-config/config.yaml. Pinned by
# tests/unit/test_e2e_mm_wiring.py so a rename fails without Docker.
MM_CONNECTOR_NAME = "mm-e2e"


def _exec(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["docker", "exec", CONTAINER, *args], capture_output=True, text=True
    )


def container_missing() -> bool:
    """Is there no container by this name at all (as opposed to one that is
    merely not ready yet)?"""
    result = subprocess.run(
        ["docker", "container", "inspect", CONTAINER], capture_output=True, text=True
    )
    return result.returncode != 0


def read_gateway_log() -> str:
    """The gateway's own log, or "" if it cannot be read.

    Callers that make a decision on the CONTENT must treat "" as unknown rather
    than as absence — see `watcher_list`'s docstring for what that cost once.
    """
    return _exec("sh", "-c", f"cat {GATEWAY_LOG}").stdout


def gateway_pid() -> int | None:
    """The running daemon's pid, or None if it is not running.

    Read from `status`' TEXT, not its exit code: `coop status`
    exits 0 while printing "Gateway: not running" (issue #134), which is why a
    readiness check gated on the returncode admits a dead daemon.
    """
    match = re.search(r"running \(pid=(\d+)\)", _exec("coop", "status").stdout)
    return int(match.group(1)) if match else None


def watcher_list() -> str:
    """`coop list --all` as seen inside the container.

    `--all` matters: idle watchers are hidden by default, so a plain `list` can
    report "No watchers" for a room that has one.

    **Raises when the command fails**, rather than returning its error text.
    Callers assert a handle is ABSENT from this output, and error text contains
    no handles — so a failed `docker exec` used to make those assertions pass
    for a reason unrelated to what they check, which is the one direction a
    guard must never fail in.
    """
    result = _exec("coop", "list", "--all")
    if result.returncode != 0:
        raise RuntimeError(
            f"`list --all` failed inside {CONTAINER} (exit {result.returncode}). "
            "Treating this as an empty watcher list would make an absence "
            f"assertion pass for the wrong reason.\nstdout: {result.stdout!r}\n"
            f"stderr: {result.stderr!r}"
        )
    return result.stdout
