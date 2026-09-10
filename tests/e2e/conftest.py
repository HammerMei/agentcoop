"""pytest fixtures for E2E tests.

Session-scoped:
    rc_setup     — runs setup.py; verifies RC is reachable
    mm_setup     — runs mm_setup.py; verifies Mattermost is reachable
    acg          — waits for AgentCoop Docker container to be ready
    mm_connected — asserts the mm-e2e CONNECTOR itself came up inside AgentCoop

Also session-scoped (all of them, to stay under Rocket.Chat's login rate
limit and to avoid re-bootstrapping per test):
    test_client    — RCClient logged in as test_user
    admin_client   — RCClient logged in as admin
    e2e_room       — parameterized: "dm" (→ OpenCode) or "channel" (→ Claude)
    mm_test_client — MMClient logged in as the human test user
    mm_admin_client— MMClient logged in as the system admin
    mm_bot_client  — MMClient logged in as the bot (see its docstring for why)
    mm_room        — parameterized: "dm" or "channel", both → Claude

Both platforms live in one AgentCoop container with one connector each, and MM's
boot failures split in a way worth knowing before reading a red suite:

* An unresolvable **team** or an unusable identity is *fatal to the whole
  gateway* — `resolve_team` raises out of `connect()`, and the daemon treats a
  connect-phase failure as fatal and exits 1. So this one shows up as the
  `acg` fixture failing for every test, RC's included, not as an MM-specific
  failure. Sharing one container buys a shared blast radius.
* Anything after that — a socket that opens and then drops — leaves the
  gateway up and answering, and an MM test's only symptom is a 120-second
  timeout indistinguishable from a slow agent.

`mm_connected` does NOT cover that second class, and its own docstring says
so: the lines it reads are one-time events and nothing removes them. What it
covers is a boot-scoped ABSENCE — the connector missing or renamed in the
config the container is running, or a gateway that is not running at all.
Same taste as `acg_container.container_missing`: name the real failure at second zero
instead of inferring it from a wait that never ends.
"""
from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import pytest

# Allow importing rc_client and setup from the same e2e directory
sys.path.insert(0, str(Path(__file__).parent))
import mm_setup as _mm
from acg_container import (
    CONTAINER as COOP_CONTAINER,
)
from acg_container import (
    MM_CONNECTOR_NAME,
    container_missing,
    gateway_pid,
    read_gateway_log,
)
from gateway_log import CONNECTORS_READY_MARKER, current_boot
from mm_client import MMClient
from rc_client import RCClient
from setup import RC_URL
from setup import setup as _run_setup

# ── Constants ─────────────────────────────────────────────────────────────────

E2E_DIR = Path(__file__).parent
COMPOSE_FILE = str(E2E_DIR / "docker-compose.yml")
BOT_USERNAME = "acg_bot"
# Seconds to wait for the daemon to answer `status`. It does NOT cover the
# agent warm-up, which runs afterwards on its own 120s-per-agent budget in
# `_warmup_agents` — the comment here used to claim it did.
COOP_READY_TIMEOUT = 180
COOP_READY_INTERVAL = 5

# Short, and it has to stay short: this fixture runs inside the per-test
# pytest-timeout budget (--timeout=180 in the Makefile and both workflows),
# alongside a 120s poll_for_message in the first MM test. The connectors settle
# during startup, so if the gateway answers at all this is a matter of the log
# file appearing, not of waiting for work.
MM_CONNECTED_TIMEOUT = 30


# ── Session fixtures ──────────────────────────────────────────────────────────


@pytest.fixture(scope="session")
def rc_setup() -> dict[str, Any]:
    """Run E2E setup and return config dict.

    Expects RC to already be running (started by Makefile / CI).
    If RC is not reachable, the fixture fails with a clear message.
    """
    rc_url = os.environ.get("E2E_RC_URL", RC_URL)
    try:
        return _run_setup(rc_url)
    except RuntimeError as exc:
        pytest.fail(
            f"E2E setup failed — is RC running?\n"
            f"  Start with: make e2e-up\n"
            f"  Error: {exc}"
        )


@pytest.fixture(scope="session")
def acg(rc_setup: dict[str, Any]) -> None:
    """Wait for the AgentCoop Docker container to be ready, then warm up agents.

    Expects the container to already be started (by Makefile / CI).
    Polls `docker exec acg-e2e coop status` until it succeeds
    or times out.

    After AgentCoop reports ready, sends a warm-up ping to both the DM (OpenCode)
    and the team channel (Claude Code).  OpenCode starts its subprocess lazily
    on the first request, so without this warm-up the first real test can time
    out waiting for the cold-start initialisation to complete.
    """
    print(f"\n[acg] Waiting for AgentCoop container '{COOP_CONTAINER}' ...", flush=True)
    _wait_for_acg(timeout=COOP_READY_TIMEOUT, interval=COOP_READY_INTERVAL)
    print("[acg] AgentCoop is ready.", flush=True)

    # ── Warm-up: trigger both agents so their subprocesses are initialised ────
    _warmup_agents(rc_setup)

    yield
    # Do NOT stop the container here — Makefile / CI handles lifecycle.
    # This lets tests be re-run quickly without restarting AgentCoop.


def _warmup_agents(rc_setup: dict[str, Any]) -> None:
    """Send a warm-up ping to DM (OpenCode) and channel (Claude Code).

    OpenCode initialises its subprocess lazily on the first message, which can
    take 60–90 s.  This function fires a simple 'pong' request at both agents
    and waits up to 120 s for each response — ensuring they are fully warmed up
    before the test suite starts.  Failures here are non-fatal (a warning is
    printed) so that individual tests can still provide actionable failure info.
    """
    rc_url = rc_setup["rc_url"]
    warmup_timeout = 120

    with RCClient(rc_url) as c:
        c.login(rc_setup["test_user_username"], rc_setup["test_user_password"])

        # ── DM → OpenCode ─────────────────────────────────────────────────────
        try:
            print("[acg] Warming up OpenCode (DM) ...", flush=True)
            dm_room_id = c.get_dm_room_id(BOT_USERNAME)
            before_ts = int(time.time() * 1000)
            c.post_message(dm_room_id, "respond with exactly the single word 'ready'")
            c.poll_for_message(
                dm_room_id,
                before_ts,
                predicate=lambda m: m["u"]["username"] == BOT_USERNAME,
                timeout=warmup_timeout,
                room_type="dm",
            )
            print("[acg] OpenCode (DM) warm-up done.", flush=True)
        except Exception as exc:
            print(f"[acg] WARNING: OpenCode warm-up failed: {exc}", flush=True)

        # ── Channel → Claude Code ──────────────────────────────────────────────
        try:
            print("[acg] Warming up Claude Code (channel) ...", flush=True)
            ch = c.get_channel(rc_setup["claude_channel"])
            if ch:
                before_ts = int(time.time() * 1000)
                c.post_message(
                    ch["_id"],
                    f"@{BOT_USERNAME} respond with exactly the single word 'ready'",
                )
                c.poll_for_message(
                    ch["_id"],
                    before_ts,
                    predicate=lambda m: m["u"]["username"] == BOT_USERNAME,
                    timeout=warmup_timeout,
                    room_type="channel",
                )
                print("[acg] Claude Code (channel) warm-up done.", flush=True)
        except Exception as exc:
            print(f"[acg] WARNING: Claude Code warm-up failed: {exc}", flush=True)



def _wait_for_acg(timeout: float, interval: float) -> None:
    """Poll docker exec until coop status returns 0.

    "Not there at all" and "there but still starting" get different
    treatment. Waiting the full timeout for a container that does not exist
    is pure loss — and worse than loss, because the eventual failure reads
    like a readiness problem when the real answer is "you never brought the
    stack up". That cost a real debugging session: the suite was run against
    a stack where only MongoDB and Rocket.Chat had been started, so this
    polled a nonexistent container for three minutes and then errored every
    single test.
    """
    if container_missing():
        pytest.fail(
            f"Container '{COOP_CONTAINER}' does not exist — the stack is not up.\n"
            "Run 'make e2e-up' first (it starts MongoDB + Rocket.Chat and "
            "Postgres + Mattermost, bootstraps the accounts on BOTH, then "
            "starts AgentCoop). 'make e2e-test' only runs the suite; it does not "
            "bring anything up.\n"
            "If e2e-up itself failed partway, 'make e2e-dump' writes the full "
            "container logs to ./e2e-logs."
        )

    deadline = time.monotonic() + timeout
    last_output = ""
    while time.monotonic() < deadline:
        result = subprocess.run(
            ["docker", "exec", COOP_CONTAINER, "coop", "status"],
            capture_output=True,
            text=True,
        )
        if result.returncode == 0:
            return
        last_output = (result.stdout + result.stderr).strip()
        # A container that vanishes mid-wait (crash-looping, or torn down)
        # is the same actionable case as never having existed.
        if container_missing():
            pytest.fail(
                f"Container '{COOP_CONTAINER}' disappeared while waiting for it "
                "to become ready — it likely crashed on startup. "
                "'make e2e-dump' writes the full logs to ./e2e-logs."
            )
        time.sleep(interval)

    # On timeout, dump AgentCoop logs for debugging
    logs = subprocess.run(
        ["docker", "logs", "--tail", "50", COOP_CONTAINER],
        capture_output=True,
        text=True,
    ).stdout
    pytest.fail(
        f"AgentCoop did not become ready within {timeout}s.\n"
        f"Last status output: {last_output}\n"
        f"Container logs (last 50 lines):\n{logs}"
    )


# ── Mattermost fixtures ───────────────────────────────────────────────────────


@pytest.fixture(scope="session")
def mm_setup() -> dict[str, Any]:
    """Bootstrap Mattermost and return the config dict.

    Expects the container to already be running (started by Makefile / CI).
    Re-running is harmless: `mm_setup.setup()` is idempotent.
    """
    mm_url = os.environ.get("E2E_MM_URL", _mm.MM_URL)
    try:
        return _mm.setup(mm_url)
    except Exception as exc:
        pytest.fail(
            f"Mattermost E2E setup failed — is Mattermost running?\n"
            f"  Start with: make e2e-up\n"
            f"  Error: {type(exc).__name__}: {exc}"
        )


@pytest.fixture(scope="session")
def mm_connected(acg: None, mm_setup: dict[str, Any]) -> None:
    """Fail fast unless the mm-e2e connector came up in THIS boot of AgentCoop.

    Rewritten rather than patched a third time. Two rounds of review found a
    defect in each previous version — a stale marker accepted from an earlier
    boot, then a boot anchor a chat message could poison — and the reason was
    the instrument, not the details: it grepped for
    `MattermostConnector connected to`, which **does not name the connector**,
    so the one thing this fixture uniquely exists to catch (mm-e2e absent or
    renamed in config, with the gateway happily serving the other platform) was
    the one thing it could not see.

    Two observables replace it, and both are read from the gateway rather than
    inferred:

    * **The pid**, from `status`' text. Not its exit code, which is 0 even for
      "Gateway: not running" — that is why `_wait_for_acg` admits a dead
      daemon, and why the previous version of this fixture ended up blaming
      Mattermost for a stack where nothing was running.
    * **`GatewayService running with connector(s): …`**, scoped to the boot
      whose banner carries that same pid. It is logged after both settle
      phases, and a connector that fails to connect is fatal to the daemon, so
      the line appearing at all means every connector in it connected. It lists
      the configured names, so `mm-e2e` being in it is exactly the assertion
      wanted.

    Scoping by pid rather than by the generic `Daemon started (` string is what
    closes the poisoning hole: every inbound message's first 120 characters are
    logged (`message_processor`), so a user posting `Daemon started (pid=1)` in
    any watched room could otherwise become the last anchor in the file and
    hide the current boot entirely.

    What this still cannot catch, stated so no one has to rediscover it: a
    socket that opened and later dropped. Those lines are a one-time event and
    nothing removes them. The gateway being up and the connector being listed
    is the whole of the claim.
    """
    pid = gateway_pid()
    if pid is None:
        pytest.fail(
            "The gateway is not running inside "
            f"'{COOP_CONTAINER}' — this is not a Mattermost problem.\n"
            "'coop status' exits 0 even when it reports 'not "
            "running', so the readiness wait upstream does not catch it. "
            f"'docker logs {COOP_CONTAINER}' will show a startup that never "
            "finished; 'make e2e-dump' collects everything."
        )

    deadline = time.monotonic() + MM_CONNECTED_TIMEOUT
    boot = ""
    while time.monotonic() < deadline:
        boot = current_boot(read_gateway_log(), pid)
        for line in boot.splitlines():
            if CONNECTORS_READY_MARKER in line and MM_CONNECTOR_NAME in line:
                return
        time.sleep(2)

    ready = [ln for ln in boot.splitlines() if CONNECTORS_READY_MARKER in ln]
    mm_lines = [
        ln for ln in boot.splitlines()
        if "attermost" in ln or MM_CONNECTOR_NAME in ln
    ]
    pytest.fail(
        f"The gateway is running (pid={pid}) but '{MM_CONNECTOR_NAME}' is not "
        f"among its connectors within {MM_CONNECTED_TIMEOUT}s.\n"
        + (
            f"It reports: {ready[-1].strip()!r} — so the connector is missing "
            "from the config the container is running, or named something "
            "else. Check tests/e2e/acg-config/config.yaml against "
            "conftest.MM_CONNECTOR_NAME.\n"
            if ready
            else "It has not reported its connectors at all, so startup is "
            "still in progress or wedged. Most likely the MM bootstrap did not "
            "run before AgentCoop started — 'make e2e-up' does both in order; "
            "starting the container by hand does not.\n"
        )
        + "Mattermost-related log lines from this boot:\n  "
        + ("\n  ".join(mm_lines[-25:]) if mm_lines else "(none at all)")
    )


@pytest.fixture(scope="session")
def mm_test_client(mm_setup: dict[str, Any]) -> MMClient:
    """MM client logged in as the human test user."""
    c = MMClient(mm_setup["mm_url"])
    try:
        c.login(mm_setup["test_user_username"], mm_setup["test_user_password"])
    except Exception:
        # login() issues a POST, so the connection pool is live by the
        # time any of its failure points can fire — and a generator that
        # raises before `yield` never runs its teardown. Reachable on a
        # warm platform volume where the account exists with a different
        # password, which is exactly when a leaked socket is least
        # welcome.
        c.close()
        raise
    try:
        yield c
    finally:
        c.close()


@pytest.fixture(scope="session")
def mm_admin_client(mm_setup: dict[str, Any]) -> MMClient:
    """MM client logged in as the system admin.

    Membership questions need this one: a non-member cannot read even its own
    membership row (403), so establishing that the bot is NOT in a channel
    requires admin eyes — see MMClient.is_channel_member.
    """
    c = MMClient(mm_setup["mm_url"])
    try:
        c.login(mm_setup["admin_username"], mm_setup["admin_password"])
    except Exception:
        # login() issues a POST, so the connection pool is live by the
        # time any of its failure points can fire — and a generator that
        # raises before `yield` never runs its teardown. Reachable on a
        # warm platform volume where the account exists with a different
        # password, which is exactly when a leaked socket is least
        # welcome.
        c.close()
        raise
    try:
        yield c
    finally:
        c.close()


@pytest.fixture(scope="session")
def mm_bot_client(mm_setup: dict[str, Any]) -> MMClient:
    """MM client logged in AS THE BOT — a second session for the same account
    the connector uses.

    A test logging in as the bot is unusual enough to justify: design §6.2
    insists that readability be established with the *probe's own* token,
    because an admin-token read proves nothing about what the bot can see, and
    "no event" would then be indistinguishable from "no access". This is the
    only way to ask that question.

    Safe to do concurrently with the connector, and verified against the live
    server rather than assumed: Mattermost keeps concurrent sessions per
    account (§6.3 observed a DM delivered to two sockets of one account), and
    this client opens no websocket, so it consumes no events the connector
    needs.
    """
    c = MMClient(mm_setup["mm_url"])
    try:
        c.login(mm_setup["bot_username"], mm_setup["bot_password"])
    except Exception:
        # login() issues a POST, so the connection pool is live by the
        # time any of its failure points can fire — and a generator that
        # raises before `yield` never runs its teardown. Reachable on a
        # warm platform volume where the account exists with a different
        # password, which is exactly when a leaked socket is least
        # welcome.
        c.close()
        raise
    try:
        yield c
    finally:
        c.close()


@pytest.fixture(scope="session", params=["dm", "channel"])
def mm_room(
    request: pytest.FixtureRequest,
    mm_setup: dict[str, Any],
    mm_test_client: MMClient,
) -> dict[str, Any]:
    """Parameterized Mattermost room: runs each test twice.

    Both rooms route to the Claude agent — unlike the RC fixture, which splits
    DM/channel across OpenCode and Claude. The MM coverage is a focused smoke
    plus the membership behaviour, and a second cold-starting runtime would
    buy nothing.

    `mention_prefix` is not cosmetic symmetry with the RC fixture: the MM
    connector's `require_mention` defaults to true and the gate exempts 1:1
    DMs only, so a channel message without the mention is filtered out and the
    test would time out with the agent never having been asked.
    """
    if request.param == "dm":
        bot = mm_test_client.get_user(mm_setup["bot_username"])
        assert bot, f"bot user {mm_setup['bot_username']!r} not found on Mattermost"
        return {
            "id": mm_test_client.get_dm_channel_id(bot["id"]),
            "type": "dm",
            "name": f"DM with {mm_setup['bot_username']}",
            "mention_prefix": "",
        }
    return {
        "id": mm_setup["member_channel_id"],
        "type": "channel",
        "name": mm_setup["member_channel"],
        "mention_prefix": f"@{mm_setup['bot_username']} ",
    }





# ── Rocket.Chat client fixtures ───────────────────────────────────────────────


@pytest.fixture(scope="session")
def test_client(rc_setup: dict[str, Any]) -> RCClient:
    """RC client logged in as test_user.

    Session-scoped to avoid RC's login rate limit (429) when running many tests.
    The httpx.Client maintains a persistent connection pool for the session.
    """
    c = RCClient(rc_setup["rc_url"])
    c.login(rc_setup["test_user_username"], rc_setup["test_user_password"])
    yield c
    c.close()


@pytest.fixture(scope="session")
def admin_client(rc_setup: dict[str, Any]) -> RCClient:
    """RC client logged in as admin.

    Session-scoped to avoid RC's login rate limit (429).
    """
    c = RCClient(rc_setup["rc_url"])
    c.login(rc_setup["admin_username"], rc_setup["admin_password"])
    yield c
    c.close()


@pytest.fixture(scope="session", params=["dm", "channel"])
def e2e_room(
    request: pytest.FixtureRequest,
    rc_setup: dict[str, Any],
    test_client: RCClient,
) -> dict[str, Any]:
    """Parameterized room fixture: runs each test twice.

    "dm"      → DM room between test_user and acg_bot → OpenCode agent
    "channel" → #acg-e2e-claude public channel        → Claude Code agent

    Returned dict:
        id:             RC room _id
        type:           "dm" or "channel"
        agent:          "opencode" or "claude"
        name:           human-readable label
        mention_prefix: "" for DM, "@acg_bot " for channel
                        (channel messages need @bot mention to be processed)
    """
    if request.param == "dm":
        room_id = test_client.get_dm_room_id(BOT_USERNAME)
        return {
            "id": room_id,
            "type": "dm",
            "agent": "opencode",
            "name": f"DM with {BOT_USERNAME}",
            "mention_prefix": "",
        }
    else:
        ch = test_client.get_channel(rc_setup["claude_channel"])
        if ch is None:
            pytest.fail(
                f"Channel '#{rc_setup['claude_channel']}' not found. "
                "Run 'make e2e-up' first."
            )
        return {
            "id": ch["_id"],
            "type": "channel",
            "agent": "claude",
            "name": rc_setup["claude_channel"],
            "mention_prefix": f"@{BOT_USERNAME} ",
        }
