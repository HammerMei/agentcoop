"""Reading AgentCoop's own log file — the parts worth testing without a stack.

This module exists because of an import collision, and the collision is the
reason it is a plain module rather than more code in `conftest.py`.

`tests/conftest.py` and `tests/e2e/conftest.py` are both importable as the
top-level name `conftest`. Whichever lands in `sys.modules["conftest"]` first
wins for the rest of the process, so a unit test doing `import conftest` to
reach an E2E helper got the wrong file depending on collection order — passing
when run alone and failing in the full suite, or the reverse. A pytest conftest
is a plugin, not a library; anything a unit test needs to import belongs
somewhere with a name of its own.
"""

from __future__ import annotations

# Logged by GatewayService once per boot, AFTER both settle phases — and a
# connector that fails to connect is fatal to the daemon, so this line
# appearing means every connector it lists connected. Unlike the Mattermost
# connector's own "MattermostConnector connected to", it NAMES them, which is
# what lets a caller detect a connector missing or renamed in config.
CONNECTORS_READY_MARKER = "GatewayService running with connector(s):"

# The daemon's first log line of a boot. Formatted with the pid so the anchor
# is specific: the bare prefix is user-controllable, because every inbound
# message's first 120 characters are logged, and `rfind` takes the LAST hit —
# so a chat message reading "Daemon started (pid=1)" would otherwise become
# the anchor and hide the real boot.
BOOT_ANCHOR_FMT = "Daemon started (pid={pid})"


def current_boot(log: str, pid: int) -> str:
    """The tail of `gateway.log` written by the daemon running as `pid`.

    Anchored on the daemon's own first line, because the markers a caller
    looks for are logged later in the same boot — anchoring on a service line
    would cut off the very text being searched for.

    Returns the whole log when the anchor is absent, which happens on a log
    format change. That is the safe direction: treating a missing anchor as an
    empty boot would turn a reworded log line into a failure claiming the
    connector never connected. `tests/unit/test_e2e_mm_wiring.py` checks the
    format string against the daemon's own logging call, so the fallback
    cannot quietly become the normal path.
    """
    index = log.rfind(BOOT_ANCHOR_FMT.format(pid=pid))
    return log if index < 0 else log[index:]
