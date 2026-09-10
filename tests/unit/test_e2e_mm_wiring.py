"""The Mattermost E2E suite's assumptions about its own config, pinned here.

Everything asserted below is something `tests/e2e/test_mm_*.py` depends on and
cannot check itself, because checking needs the stack it is describing. The
same reasoning as `test_e2e_watcher_names.py`, which exists because the
schedule E2E test was silently passing rule names where watcher handles were
required — caught by hand after a cutover, not by a test.

The load-bearing one is `TestTheGlobStillClaimsTheOutsideChannel`. The
membership test's whole value rests on the rule matching a channel the bot has
not joined, so that the absence of a reply can only be explained by the
absence of an event. Narrow `acg-e2e-mm-*` to the member channel and that test
keeps passing while testing nothing at all — the worst failure mode a test
can have, and one no E2E run would report.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

import yaml

from gateway.connectors.mattermost.normalize import bare_handle
from gateway.core.watcher_manager import RoomRef, watcher_label
from gateway.core.watcher_rule import RoomKind, RuleMatch

_REPO = Path(__file__).resolve().parents[2]
_E2E = _REPO / "tests" / "e2e"
_E2E_CONFIG = _E2E / "acg-config" / "config.yaml"

# The E2E helpers import each other as top-level modules.
sys.path.insert(0, str(_E2E))
import acg_container  # noqa: E402
import gateway_log  # noqa: E402
import mm_setup  # noqa: E402

MM_CHANNEL_RULE = "e2e-mm-channel"
MM_DM_RULE = "e2e-mm-dm"


def _config() -> dict:
    return yaml.safe_load(_E2E_CONFIG.read_text())



class TestTheMMConnectorIsDeclaredUnderTheNameTheTestsUse(unittest.TestCase):
    def test_the_conftest_connector_name_is_declared_in_the_config(self):
        declared = {
            c["name"]: c.get("type")
            for c in (_config().get("connectors") or [])
            if isinstance(c, dict) and isinstance(c.get("name"), str)
        }
        name = acg_container.MM_CONNECTOR_NAME
        self.assertIn(name, declared)
        self.assertEqual(
            "mattermost",
            declared[name],
            f"connector {name!r} is not a Mattermost connector — the MM tests "
            "build handles with this name and would silently target the wrong "
            "platform",
        )

    def test_both_mm_rules_are_on_that_connector(self):
        """Via the template, not inline — `e2e-default` pins `rc-e2e`, so a
        rule that forgot `inherits: e2e-mm-default` would land on Rocket.Chat
        and match nothing."""
        by_name = {r.name: r for r in _loaded_config().watcher_rules}
        name = acg_container.MM_CONNECTOR_NAME
        for rule in (MM_CHANNEL_RULE, MM_DM_RULE):
            with self.subTest(rule=rule):
                self.assertIn(rule, by_name)
                self.assertEqual(name, by_name[rule].connector)


def _loaded_config():
    """The E2E config through the real loader.

    `working_directory` points inside the container, so a straight load fails
    on this machine for a reason that has nothing to do with what is being
    tested. Rewriting it to a path that exists everywhere keeps the assertions
    about rules rather than about the host.
    """
    import tempfile

    from gateway.config import GatewayConfig

    patched = _E2E_CONFIG.read_text().replace(
        "/root/.agentcoop/work", tempfile.gettempdir()
    )
    # Cleaned up: `mkdtemp` here leaked one directory per call, four per unit
    # run, forever. The loader only needs the file to exist while it reads.
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "config.yaml"
        path.write_text(patched)
        return GatewayConfig.from_file(str(path))


class TestTheGlobStillClaimsTheOutsideChannel(unittest.TestCase):
    """`tests/e2e/test_mm_membership_delivery.py` is only a test while this
    holds."""

    def setUp(self):
        self.rule = {r.name: r for r in _loaded_config().watcher_rules}[MM_CHANNEL_RULE]

    def test_it_claims_the_member_channel(self):
        self.assertEqual(
            RuleMatch.CLAIMED,
            self.rule.match(mm_setup.MEMBER_CHANNEL, RoomKind.CHANNEL),
        )

    def test_it_also_claims_the_channel_the_bot_never_joins(self):
        self.assertEqual(
            RuleMatch.CLAIMED,
            self.rule.match(mm_setup.OUTSIDE_CHANNEL, RoomKind.CHANNEL),
            f"the rule no longer matches {mm_setup.OUTSIDE_CHANNEL!r}. "
            "test_mm_membership_delivery.py would still pass, and would prove "
            "nothing: a reply is absent because the rule declined, not because "
            "Mattermost delivered no event. Either widen the glob back or "
            "delete that test — do not leave it passing.",
        )

    def test_it_does_not_reach_the_rocket_chat_channels(self):
        """Both platforms live in one config; the MM glob must not shadow RC
        room names of a similar shape."""
        for rc_room in ("acg-e2e-claude", "acg-e2e-permission", "acg-e2e-outside"):
            with self.subTest(room=rc_room):
                self.assertEqual(
                    RuleMatch.NO_MATCH, self.rule.match(rc_room, RoomKind.CHANNEL)
                )


class TestTheHandleFormatsTheMMTestsAssertOn(unittest.TestCase):
    """`test_mm_membership_delivery.py` builds `<connector>:<channel>` by hand
    to check `list --all`, the same coupling `test_e2e_watcher_names.py` pins
    for the schedule test.

    The connector name comes from conftest rather than a literal: hardcoding
    `"mm-e2e"` here would leave this class green while pinning a form the
    renamed connector no longer produces — which is the failure mode the class
    exists to prevent, one level up.
    """

    def setUp(self):
        self.connector = acg_container.MM_CONNECTOR_NAME

    def test_a_channel_handle_is_connector_colon_the_channel_slug(self):
        """The slug, not the display name: the MM connector fills RoomRef.name
        from the event's `channel_name`."""
        self.assertEqual(
            f"{self.connector}:{mm_setup.MEMBER_CHANNEL}",
            watcher_label(
                self.connector,
                RoomRef(
                    id="whatever",
                    kind=RoomKind.CHANNEL,
                    name=mm_setup.MEMBER_CHANNEL,
                    participants=(),
                ),
            ),
        )

    def test_a_dm_handle_is_bare_because_the_wire_prefix_is_stripped(self):
        """Pins the whole chain from wire value to handle, not `watcher_label`
        in isolation — which is how this test was wrong twice over.

        Mattermost sends a DM's counterpart `@`-prefixed on the websocket
        event. First this test fed `watcher_label` a bare username, because
        that is what Rocket.Chat supplies, and passed while describing a form
        that never occurred. A live `list --all` then showed the truth:

            rc-e2e:dm:test_user       ...  test_user
            mm-e2e:dm:%40test_user    ...  @test_user

        `%40` because `@` is outside `_LABEL_SAFE`, whose percent-encoding is
        what makes the `dm:` prefix unforgeable — correct downstream of a wrong
        input. The connector now strips the prefix at the source, so one person
        has one handle on both platforms.

        Scope, stated honestly because an earlier version of this docstring
        overclaimed: this pins `bare_handle` composed with `watcher_label`, so
        it does NOT catch the connector forgetting to CALL `bare_handle` — it
        never invokes the connector. That mutation is covered next door, by
        `test_mattermost_connector.py`'s DM case driving `_on_posted_event`
        with the real prefixed wire value. The two together close the chain;
        neither does alone.
        """
        wire_value = f"@{mm_setup.TEST_USER_USERNAME}"
        self.assertEqual(
            f"{self.connector}:dm:{mm_setup.TEST_USER_USERNAME}",
            watcher_label(
                self.connector,
                RoomRef(
                    id="whatever",
                    kind=RoomKind.DM,
                    name="",
                    participants=(bare_handle(wire_value),),
                ),
            ),
        )

    def test_without_the_strip_the_handle_needs_percent_encoding(self):
        """The cost of regressing the line above, stated so it is concrete: an
        operator would have to type `%40` to address a Mattermost DM."""
        self.assertEqual(
            f"{self.connector}:dm:%40test_user",
            watcher_label(
                self.connector,
                RoomRef(id="x", kind=RoomKind.DM, name="", participants=("@test_user",)),
            ),
        )


class TestGetPostsAsksForAnInclusiveWindow(unittest.TestCase):
    """Mattermost's `since` is exclusive; the client's contract is inclusive.

    Worth a guard rather than a comment because of how it fails. Measured on
    11.7.0, `int(time.time() * 1000)` followed immediately by a post landed in
    the SAME millisecond on 6 of 6 attempts — so an exclusive boundary drops
    the boundary post essentially always, not rarely. In `poll_for_message`
    that only skews a count, but `test_mm_membership_delivery.py` asks "did
    the bot post in the channel it never joined, since this moment?" and a
    dropped boundary post turns that into a pass while the forbidden thing
    happened. Silently, and in the direction that reports success.
    """

    def _captured_since(self, since_ms: int | None) -> str | None:
        import httpx
        from mm_client import MMClient

        seen: list[httpx.URL] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request.url)
            return httpx.Response(200, json={"order": [], "posts": {}})

        with MMClient("http://mm.invalid") as client:
            # The real client built by __init__ is replaced, so close it rather
            # than orphan it; the `with` also covers a raise from get_posts or
            # from the assertion below, which a bare close() at the end did not.
            client._client.close()
            client._client = httpx.Client(
                base_url="http://mm.invalid/api/v4",
                transport=httpx.MockTransport(handler),
            )
            client.get_posts("chan-id", since_ms=since_ms)
        self.assertEqual(1, len(seen))
        return seen[0].params.get("since")

    def test_the_requested_since_is_one_millisecond_earlier(self):
        self.assertEqual(
            "999",
            self._captured_since(1000),
            "get_posts must ask for since-1: Mattermost selects posts modified "
            "AFTER the value, so passing the caller's timestamp straight "
            "through hides a post created in that same millisecond",
        )

    def test_no_since_parameter_when_the_caller_gives_none(self):
        self.assertIsNone(self._captured_since(None))


# The other half of the membership setup — the human in that channel, the bot
# out of it — is deliberately NOT pinned here. Asserting it would mean matching
# mm_setup.py's source text, which breaks on a reformat and passes on a rename;
# and the property is already asserted where it can be OBSERVED rather than
# read, in test_mm_membership_delivery.py's preconditions, against the live
# server and with a message naming the fix.


class TestBootScopingOfTheReadyMarker(unittest.TestCase):
    """`mm_connected` must read only the CURRENT boot, and the anchor must be
    specific enough that chat traffic cannot forge it.

    Two review rounds each found a defect in the previous version of this, so
    the risks are named rather than implied:

    * the daemon appends to `gateway.log` and the runtime dir is not a volume,
      so under `restart: unless-stopped` the file holds every boot — an
      unscoped search answers "did this ever connect", which passes for a
      gateway that came back broken;
    * every inbound message's first 120 characters are logged, so anchoring on
      the bare `Daemon started (` prefix let a chat message become the last
      anchor and hide the real boot. The pid closes that.

    The anchor format is checked against the DAEMON's own logging call below,
    so a reworded log line fails here rather than degrading silently.
    """

    # Imported at module scope below, from `gateway_log` rather than from
    # `conftest`. Importing the E2E conftest by that name is what made these
    # three tests order-dependent: `tests/conftest.py` is importable under the
    # same top-level name, whichever reaches `sys.modules["conftest"]` first
    # wins for the process, and the loser's attributes simply are not there.
    # They passed alone and failed in the full suite.

    def _log(self, pid: int, tail: str) -> str:
        return (
            f"01-01 00:00:00 [daemon] INFO: Daemon started (pid={pid})\n" + tail
        )

    def test_the_anchor_format_matches_the_daemon_call_that_emits_it(self):
        """The one assertion that is NOT built from the constant.

        Everything else here composes `_BOOT_ANCHOR_FMT` with itself, so a
        rename of the daemon's log text would move input and expectation
        together and go unnoticed — while `_current_boot` silently fell back to
        the whole log, i.e. back to the bug this class exists to prevent. Tie
        the constant to its producer instead.
        """
        source = (_REPO / "gateway" / "daemon.py").read_text()
        rendered = gateway_log.BOOT_ANCHOR_FMT.format(pid="%d")
        self.assertIn(
            rendered,
            source,
            "conftest._BOOT_ANCHOR_FMT no longer matches the string "
            "gateway/daemon.py logs at startup, so mm_connected's boot "
            "scoping silently degrades to searching the whole log",
        )

    def test_an_earlier_boots_marker_is_excluded(self):
        log = (
            self._log(1, "01-01 00:00:01 [service] INFO: ready connector(s): mm-e2e\n")
            + self._log(2, "01-02 00:00:01 [mattermost] ERROR: websocket refused\n")
        )
        self.assertNotIn("mm-e2e", gateway_log.current_boot(log, 2))

    def test_the_current_boots_marker_is_kept(self):
        marker = gateway_log.CONNECTORS_READY_MARKER
        log = self._log(7, f"01-02 00:00:01 [service] INFO: {marker} rc-e2e, mm-e2e\n")
        self.assertIn(marker, gateway_log.current_boot(log, 7))

    def test_a_chat_message_quoting_the_prefix_cannot_hide_the_boot(self):
        """The poisoning case. Inbound message text is logged, so this line is
        something a user can put in the file — after the real banner."""
        marker = gateway_log.CONNECTORS_READY_MARKER
        log = (
            self._log(263, f"01-02 00:00:01 [service] INFO: {marker} rc-e2e, mm-e2e\n")
            + "01-02 00:00:02 [processor] INFO: Processing [general] Daemon started (pid=1)\n"
        )
        self.assertIn(
            marker,
            gateway_log.current_boot(log, 263),
            "a chat message quoting the boot banner became the anchor, hiding "
            "the current boot — the pid in the anchor is what prevents this",
        )

    def test_a_log_without_the_anchor_is_used_whole(self):
        """Documented fallback: a format change must not become a failure
        claiming the connector never connected. The test above is what makes
        this fallback safe to keep."""
        marker = gateway_log.CONNECTORS_READY_MARKER
        log = f"no anchor here\n[service] INFO: {marker} mm-e2e\n"
        self.assertIn(marker, gateway_log.current_boot(log, 99))


if __name__ == "__main__":
    unittest.main()
