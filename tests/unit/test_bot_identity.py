"""Two connectors must never be one bot account (§4.5).

The rule is checked at runtime because config cannot decide it: Mattermost token auth
leaves `username` empty, and two different tokens can authenticate the same account.
These tests cover the rule itself, the normalisation in front of it, and the guarantee
that every connector in the tree answers the identity question deliberately.
"""

import inspect
import pkgutil
import unittest
from pathlib import Path

from gateway.core.bot_identity import (
    BotIdentity,
    ConnectorIdentity,
    ConnectorIdentityError,
    DmClaim,
    canonical_origin,
    dm_claims,
    find_identity_conflicts,
)
from gateway.core.connector import Connector


class TestCanonicalOrigin(unittest.TestCase):
    """A comparison is only as good as the normalisation in front of it."""

    def test_the_spellings_of_one_server_converge(self):
        forms = [
            "https://mm.example.com",
            "https://mm.example.com/",
            "https://mm.example.com:443",
            "https://MM.Example.COM",
        ]
        self.assertEqual(
            len({canonical_origin(f) for f in forms}),
            1,
            "these are one server; comparing raw strings would call them five accounts",
        )

    def test_a_non_default_port_is_kept(self):
        self.assertNotEqual(
            canonical_origin("https://rc.example.com:3000"),
            canonical_origin("https://rc.example.com"),
            "a different port is a different server, not a spelling",
        )

    def test_the_scheme_distinguishes(self):
        self.assertNotEqual(
            canonical_origin("http://rc.example.com"),
            canonical_origin("https://rc.example.com"),
        )

    def test_a_unicode_host_matches_its_idna_form(self):
        """One DNS name written two ways. The weakest case in this function by reach —
        kept because it is four guarded lines and both spellings do resolve to one
        server, so the failure is a missed duplicate: two agents in one room."""
        self.assertEqual(
            canonical_origin("https://bücher.example"),
            canonical_origin("https://xn--bcher-kva.example"),
        )

    def test_an_ascii_host_never_touches_the_idna_codec(self):
        """The guard matters: Python's idna codec rejects empty and over-long labels
        that resolve perfectly well, so the ordinary path must not go through it."""
        self.assertEqual(
            canonical_origin("https://a.-weird-.example"), "https://a.-weird-.example")

    def test_a_terminal_dns_root_dot_is_dropped(self):
        """`chat.example.com.` and `chat.example.com` resolve to one server, so keeping
        the dot splits one account into two keys — a *missed* duplicate, which here means
        two agents answering in the same room."""
        self.assertEqual(
            canonical_origin("http://chat.example.com."),
            canonical_origin("http://chat.example.com"),
        )

    def test_equivalent_ipv6_spellings_are_one_origin(self):
        """`2001:0db8:0:0:0:0:0:1` and `2001:db8::1` are one address. Comparing the text
        would miss a duplicate — two agents in a room — which is the opposite failure
        from the bracket case above, in the same two lines of code."""
        self.assertEqual(
            canonical_origin("https://[2001:0db8:0:0:0:0:0:1]"),
            canonical_origin("https://[2001:db8::1]"),
        )

    def test_it_is_total(self):
        """No string an operator can type may raise out of here.

        Guarded as a property rather than one case per spelling, because the cases kept
        arriving one at a time: first a non-numeric port (`urlsplit().port` raises), then
        a bracketed non-IP host — which `urlsplit` itself rejects, so the per-field guard
        written for the first one did not cover the second. This function is called from
        `coop config validate`, where a traceback replaces the attributed bad-URL finding
        the operator needs, so totality is the requirement and not the individual fixes.
        """
        hostile = [
            "", "://", "https://[", "https://[not:an:address]", "https://]bad[",
            "https://chat.example.com:notaport", "http://x:99999", "http://x:-1",
            "https://a b c", "not a url at all", "https://ex.com:", "//", "://:",
            "https://[::1", "https://user:pw@host:80/path?q=1#f", "\\", "https://.",
        ]
        for value in hostile:
            with self.subTest(url=value):
                result = canonical_origin(value)
                self.assertIsInstance(result, str)
                self.assertEqual(
                    result, canonical_origin(value), "must be deterministic")

    def test_a_deployment_path_is_kept(self):
        """The opposite failure direction, and the worse one: two deployments under one
        host are two servers (both clients build their URLs on the prefix), and
        collapsing them would refuse a valid pair rather than miss an invalid one."""
        self.assertNotEqual(
            canonical_origin("https://host.example/rc-one"),
            canonical_origin("https://host.example/rc-two"),
        )

    def test_an_ipv6_host_keeps_its_brackets(self):
        """Without them, an address-with-port and a longer address render identically."""
        self.assertNotEqual(
            canonical_origin("https://[::1]:8443"),
            canonical_origin("https://[::1:8443]"),
        )

    def test_an_unparseable_port_does_not_raise(self):
        """Reached from `coop config validate`, where a traceback replaces the attributed
        bad-URL finding the operator actually needs."""
        self.assertEqual(
            canonical_origin("https://chat.example.com:notaport"),
            canonical_origin("https://chat.example.com:notaport"),
        )

    def test_a_bare_host_is_accepted(self):
        """`urlsplit` reads a schemeless string as a path, which would make every bare
        host normalise to the same empty origin — every such connector a duplicate."""
        self.assertEqual(canonical_origin("rc.example.com"), "https://rc.example.com")


def _entry(name, user_id="u1", origin="https://s", scope="", dms=None,
           platform="rocketchat"):
    return ConnectorIdentity(
        name, BotIdentity(platform, origin, user_id, scope), dms or DmClaim())


class TestIdentityConflicts(unittest.TestCase):
    def test_distinct_accounts_are_fine(self):
        self.assertEqual(
            find_identity_conflicts([_entry("a", "u1"), _entry("b", "u2")]), [])

    def test_the_same_account_twice_is_rejected(self):
        conflicts = find_identity_conflicts([_entry("a"), _entry("b")])
        self.assertEqual(len(conflicts), 1)
        self.assertIn("'a'", conflicts[0])
        self.assertIn("'b'", conflicts[0])

    def test_the_same_account_on_different_servers_is_not_a_conflict(self):
        """Platform user ids are not globally unique — two servers can issue the same
        one, and treating that as a collision would reject an unrelated pair."""
        self.assertEqual(
            find_identity_conflicts(
                [_entry("a", origin="https://s1"), _entry("b", origin="https://s2")]),
            [],
        )

    def test_different_teams_on_one_account_are_allowed(self):
        """The Mattermost exception: the socket spans every team, so each connector
        discards other teams' events and channels stay apart."""
        self.assertEqual(
            find_identity_conflicts(
                [_entry("a", scope="team1"), _entry("b", scope="team2")]),
            [],
        )

    def test_the_same_team_twice_is_still_a_conflict(self):
        self.assertEqual(
            len(find_identity_conflicts(
                [_entry("a", scope="team1"), _entry("b", scope="team1")])),
            1,
        )

    def test_one_scoped_and_one_unscoped_is_a_conflict(self):
        """Fail-closed on a mixture: an empty scope means "nothing separates me", so a
        Rocket.Chat connector and a Mattermost one on the same account still collide —
        the exception requires *every* member to be separated, not just one."""
        self.assertEqual(
            len(find_identity_conflicts([_entry("a", scope="team1"), _entry("b")])), 1)

    def test_two_one_to_one_claims_on_one_account_are_rejected_across_teams(self):
        """The condition on the exception. A DM has no team, so the team gate cannot
        separate it and the platform delivers it to every open connection."""
        conflicts = find_identity_conflicts([
            _entry("a", scope="team1", dms=DmClaim(direct=True)),
            _entry("b", scope="team2", dms=DmClaim(direct=True)),
        ])
        self.assertEqual(len(conflicts), 1)
        self.assertIn("direct message", conflicts[0].lower())

    def test_one_dm_owner_across_teams_is_allowed(self):
        self.assertEqual(
            find_identity_conflicts([
                _entry("a", scope="team1", dms=DmClaim(direct=True)),
                _entry("b", scope="team2"),
            ]),
            [],
        )

    def test_disjoint_static_dms_across_teams_are_allowed(self):
        """A static watcher claims one resolved channel, not the stream: `subscribe_room`
        registers that channel and the handler ignores every other, so these two cannot
        both answer one message. Rejecting them refuses a configuration that works —
        the expensive direction for a check whose answer is "do not start"."""
        self.assertEqual(
            find_identity_conflicts([
                _entry("a", scope="team1", dms=DmClaim(rooms=frozenset({"@alice"}))),
                _entry("b", scope="team2", dms=DmClaim(rooms=frozenset({"@bob"}))),
            ]),
            [],
        )

    def test_the_same_static_dm_on_both_is_rejected(self):
        conflicts = find_identity_conflicts([
            _entry("a", scope="team1", dms=DmClaim(rooms=frozenset({"@alice"}))),
            _entry("b", scope="team2", dms=DmClaim(rooms=frozenset({"@alice", "@bob"}))),
        ])
        self.assertEqual(len(conflicts), 1)
        self.assertIn("@alice", conflicts[0])
        self.assertNotIn("@bob", conflicts[0], "only the overlap is the problem")

    def test_a_whole_stream_claim_swallows_another_connector_static_dm(self):
        """`direct: true` takes every DM the account receives, including the one a
        static watcher on the other connector is waiting for."""
        self.assertEqual(
            len(find_identity_conflicts([
                _entry("a", scope="team1", dms=DmClaim(direct=True)),
                _entry("b", scope="team2", dms=DmClaim(rooms=frozenset({"@alice"}))),
            ])),
            1,
        )

    def test_two_platforms_at_one_origin_are_not_one_account(self):
        """User ids live in per-platform id spaces. A Rocket.Chat and a Mattermost
        deployment reachable at one origin are separate authentication realms, and an id
        — or a conventional username like `bot` at validation time — colliding across
        them is a coincidence. Two connectors of different types can never be one
        account, so this must not refuse them."""
        self.assertEqual(
            find_identity_conflicts([
                _entry("rc", platform="rocketchat"),
                _entry("mm", platform="mattermost", scope="team1"),
            ]),
            [],
        )

    def test_every_conflict_is_reported_not_just_the_first(self):
        """Three colliding pairs should not need three restarts to discover."""
        conflicts = find_identity_conflicts([
            _entry("a", "u1"), _entry("b", "u1"),
            _entry("c", "u2"), _entry("d", "u2"),
        ])
        self.assertEqual(len(conflicts), 2)


class TestEveryConnectorAnswersDeliberately(unittest.TestCase):
    """The base returns None, so a new connector inherits "no account" by omission.

    That is the silent failure this whole check exists to prevent, one level up: a
    platform connector that forgot to override would simply never be compared against
    anything. Enumerating the package makes the omission fail here instead — the same
    treatment used for the state schema's fields, and for the same reason: a rule that
    depends on someone remembering is not a rule.
    """

    # The test: does this connector authenticate as an account on a server that
    # another connector could also authenticate as? Both of these listen locally and
    # log in to nothing, so two of them cannot become one account — they would collide
    # on a port or a pipe, loudly, which is a different failure with its own message.
    # A platform connector belongs here only if that question is genuinely "no".
    ACCOUNTLESS = {
        "ScriptConnector",  # stdin/stdout
        "VoiceConnector",   # local HTTP endpoint, no login
    }

    def _connector_classes(self):
        import gateway.connectors as pkg

        found = {}
        for mod in pkgutil.walk_packages(pkg.__path__, f"{pkg.__name__}."):
            try:
                module = __import__(mod.name, fromlist=["_"])
            except Exception:
                continue  # an optional dependency missing is not this test's business
            for name, obj in vars(module).items():
                if (
                    inspect.isclass(obj)
                    and issubclass(obj, Connector)
                    and obj is not Connector
                    and obj.__module__ == mod.name
                ):
                    found[name] = obj
        return found

    def test_no_connector_lives_outside_the_package_this_walks(self):
        """The walk above is scoped to `gateway.connectors`, and that scope is an
        assumption — the same shape as anchoring a sweep on a hand-picked directory and
        calling it exhaustive. This checks the assumption instead of trusting it: an
        `ast` pass over the whole `gateway/` tree, needing no imports, so a connector
        defined somewhere else fails here rather than silently inheriting "no account".
        """
        import ast

        import gateway

        # Anchored to the package's own location, not the working directory: a relative
        # "gateway" path would find nothing when pytest runs from elsewhere, and the
        # sweep would report a clean tree because it had read no files.
        root = Path(gateway.__file__).parent
        found = []
        for path in root.rglob("*.py"):
            for node in ast.walk(ast.parse(path.read_text())):
                if not isinstance(node, ast.ClassDef) or node.name == "Connector":
                    continue
                for base in node.bases:
                    name = getattr(base, "id", None) or getattr(base, "attr", None)
                    if name and "Connector" in name:
                        found.append((str(path), node.name))

        connectors_dir = str(root / "connectors")
        outside = sorted(
            f"{p}:{n}" for p, n in found if not p.startswith(connectors_dir))
        self.assertEqual(
            outside, [],
            "a Connector subclass outside gateway/connectors/ is invisible to the walk "
            "below, so it would never be asked for a bot identity — move it into the "
            "package, or widen the walk",
        )
        self.assertTrue(found, "the ast sweep found no connectors at all; it is broken")

    def test_the_sweep_finds_the_known_connectors(self):
        """Guards the enumeration itself: a walk that silently found nothing would make
        every assertion below vacuously true."""
        names = set(self._connector_classes())
        self.assertIn("RocketChatConnector", names)
        self.assertIn("MattermostConnector", names)

    def test_each_connector_overrides_identity_or_is_declared_accountless(self):
        for name, cls in sorted(self._connector_classes().items()):
            with self.subTest(connector=name):
                overrides = cls.bot_identity is not Connector.bot_identity
                self.assertTrue(
                    overrides or name in self.ACCOUNTLESS,
                    f"{name} neither reports a bot identity nor is listed as "
                    f"accountless, so two of them on one account would go unnoticed",
                )


if __name__ == "__main__":
    unittest.main()


class TestDeclaredAccountsAtConfigLoad(unittest.TestCase):
    """The early half: catch the obvious case before the operator restarts the daemon.

    Deliberately weaker than the runtime barrier and in the safe direction — every
    blind spot is a missed duplicate the barrier still catches, never a rejection of a
    configuration that would have worked.
    """

    def _validate(self, yaml_text):
        import tempfile
        from pathlib import Path as P

        from gateway.config_validate import validate_config

        with tempfile.TemporaryDirectory() as d:
            path = P(d) / "config.yaml"
            path.write_text(yaml_text)
            return validate_config(str(path))

    def _yaml(self, second_username="acg-bot"):
        # The second URL differs by an explicit default port, NOT a trailing slash:
        # the RC config parser already `rstrip("/")`s, so a trailing-slash pair would
        # compare equal with no normalisation of ours involved — the test would pass
        # while proving nothing. Found by disabling `canonical_origin` and watching
        # this test stay green.
        return f"""
connectors:
  - name: rc-one
    type: rocketchat
    server:
      url: https://chat.example.com
      username: acg-bot
      password: secret
  - name: rc-two
    type: rocketchat
    server:
      url: https://chat.example.com:443
      username: {second_username}
      password: secret
agents:
  default:
    type: claude
    working_directory: /tmp
watcher_rules: []
"""

    def test_two_connectors_declaring_one_account_are_rejected(self):
        result = self._validate(self._yaml())
        self.assertTrue(
            any("same bot account" in e for e in result.errors),
            f"expected a duplicate-account error, got {result.errors}",
        )

    def test_a_different_spelling_of_one_server_does_not_hide_it(self):
        """The two URLs above are one server written two ways. Without normalisation
        this check reports nothing and looks exactly like a clean config."""
        self.assertTrue(any("same bot account" in e for e in self._validate(self._yaml()).errors))

    def test_distinct_usernames_pass(self):
        result = self._validate(self._yaml(second_username="other-bot"))
        self.assertFalse(
            [e for e in result.errors if "same bot account" in e],
            "two different accounts on one server are the supported setup",
        )


class TestTheRealConnectorsReportTheirIdentity(unittest.TestCase):
    """The enumeration test proves RC and MM *override* the method; this proves the
    overrides work. Renaming `_rest.user_id` or `_rest.team_id` would otherwise pass the
    entire suite and fail at boot, where the only symptom is a refusal to start.

    `RocketChatConnector.connect()` awaits `self._rest.login()` before anything else
    (connector.py), and Mattermost's awaits `get_me()` then `resolve_team()`, so both
    ids are populated by the time the barrier reads them.
    """

    def _rc(self, user_id, url="https://chat.example.com/"):
        from unittest.mock import MagicMock as M

        from gateway.connectors.rocketchat.connector import RocketChatConnector

        c = RocketChatConnector.__new__(RocketChatConnector)
        c._rest = M(user_id=user_id)
        c._config = M(server_url=url)
        return c

    def _mm(self, user_id, team_id, url="https://mm.example.com"):
        from unittest.mock import MagicMock as M

        from gateway.connectors.mattermost.connector import MattermostConnector

        c = MattermostConnector.__new__(MattermostConnector)
        c._rest = M(bot_user_id=user_id, team_id=team_id)
        c._config = M(server_url=url, team="eng")
        return c

    def test_rocketchat_reports_the_login_id_under_a_canonical_origin(self):
        identity = self._rc("rc-user-1").bot_identity()
        self.assertEqual(identity.platform, "rocketchat")
        self.assertEqual(identity.user_id, "rc-user-1")
        self.assertEqual(identity.origin, "https://chat.example.com")
        self.assertEqual(identity.scope, "", "Rocket.Chat has no team to scope by")

    def test_rocketchat_refuses_when_it_has_no_id(self):
        with self.assertRaises(ConnectorIdentityError):
            self._rc("").bot_identity()

    def test_mattermost_scopes_by_the_resolved_team_id(self):
        """Not the configured `team:` string — that field takes a name *or* an id, so
        two connectors on one team can spell it two ways and compare as different,
        breaking the one case the exception is meant to make safe."""
        identity = self._mm("mm-user-1", "team-id-abc").bot_identity()
        self.assertEqual(identity.platform, "mattermost")
        self.assertEqual(identity.user_id, "mm-user-1")
        self.assertEqual(identity.scope, "team-id-abc")
        self.assertNotEqual(identity.scope, "eng", "must not be the configured name")

    def test_mattermost_refuses_without_a_user_id(self):
        with self.assertRaises(ConnectorIdentityError):
            self._mm("", "team-id-abc").bot_identity()

    def test_mattermost_refuses_without_a_resolved_team(self):
        """An empty scope would claim "nothing separates me from another connector on
        this account", which is not an answer Mattermost is allowed to give."""
        with self.assertRaises(ConnectorIdentityError):
            self._mm("mm-user-1", "").bot_identity()


class TestWhoOwnsTheDmStream(unittest.TestCase):
    """A rule's DM opt-ins are the only claim shape left: `direct` and
    `group_direct` each claim a whole DM class — a DM has no room name for a
    pattern to match. The per-room claim (`DmClaim.rooms`) survives as a type
    for §2.7's reserved object form, so `overlaps` keeps understanding it,
    but nothing produces one from config today."""

    def _rule(self, connector, direct=False, group_direct=False):
        from unittest.mock import MagicMock as M

        return M(connector=connector, rooms=M(direct=direct, group_direct=group_direct))

    def test_a_rule_opting_in_claims_that_class_only(self):
        """The two DM kinds are separate in `RoomMatcher.match()`, so a rule taking one
        must not be recorded as taking the other."""
        one_to_one = dm_claims([self._rule("mm-eng", direct=True)])["mm-eng"]
        self.assertTrue(one_to_one.direct)
        self.assertFalse(one_to_one.group_direct)

        group = dm_claims([self._rule("mm-eng", group_direct=True)])["mm-eng"]
        self.assertTrue(group.group_direct)
        self.assertFalse(group.direct)

    def test_a_rule_with_no_dm_opt_in_claims_nothing(self):
        self.assertEqual(dm_claims([self._rule("mm-eng")]), {})

    def test_the_two_dm_classes_do_not_collide(self):
        """A connector on 1:1 DMs and one on group DMs answer different conversations;
        rejecting them refuses a working pairing."""
        self.assertFalse(
            DmClaim(direct=True).overlaps(DmClaim(group_direct=True)))
        self.assertTrue(
            DmClaim(group_direct=True).overlaps(DmClaim(group_direct=True)))

    def test_two_whole_stream_claims_overlap(self):
        """Two connectors both opting into 1:1 DMs would both answer every DM
        on the account — exactly what the identity barrier must see."""
        a = dm_claims([self._rule("mm-eng", direct=True)])["mm-eng"]
        b = dm_claims([self._rule("mm-sales", direct=True)])["mm-sales"]
        self.assertTrue(a.overlaps(b))

    def test_a_reserved_per_room_claim_still_overlaps_the_stream(self):
        """`DmClaim.rooms` is the reserved object form's shape (§2.7): nothing
        produces it from config today, but `overlaps` must keep understanding
        it so reviving per-DM include lists later extends this rather than
        rewriting it."""
        named = DmClaim(rooms=frozenset({"@alice"}))
        self.assertTrue(named.overlaps(DmClaim(direct=True)))
        self.assertFalse(named.overlaps(DmClaim(group_direct=True)))

    def test_a_connector_claiming_nothing_never_overlaps(self):
        self.assertFalse(DmClaim().overlaps(DmClaim(direct=True)))
        self.assertFalse(DmClaim(direct=True).overlaps(DmClaim()))


class TestPersistedRecordsAreClaims(unittest.TestCase):
    """Codex round 6 (P1): sticky binding keeps a rule-derived DM record
    answering its room after the rule that created it is deleted — so a
    rule-only claim misses exactly the records that outlive their rules, and
    two connectors sharing one account both answer one private conversation.
    Persisted records are folded into the claim before the overlap check."""

    def _record(self, kind, room_id, name="w1", rule_name="eng"):
        from unittest.mock import MagicMock as M

        return M(rule_name=rule_name, room_id=room_id, room_kind=kind,
                 watcher_name=name)

    def test_a_dm_record_claims_its_room(self):
        from gateway.core.bot_identity import fold_record_dm_claims

        claim = fold_record_dm_claims(
            DmClaim(), [self._record("dm", "d1", name="mm-dm-alice")])
        self.assertIn("d1", claim.rooms)
        self.assertIn("mm-dm-alice", claim.record_watchers)
        self.assertTrue(claim.overlaps(DmClaim(direct=True)),
                        "the sticky record collides with a whole-stream "
                        "claim on the other connector")

    def test_a_group_dm_record_claims_the_group_side_only(self):
        from gateway.core.bot_identity import fold_record_dm_claims

        claim = fold_record_dm_claims(
            DmClaim(), [self._record("group_dm", "g1")])
        self.assertIn("g1", claim.group_rooms)
        self.assertTrue(claim.overlaps(DmClaim(group_direct=True)))
        self.assertFalse(claim.overlaps(DmClaim(direct=True)),
                         "a group-DM record never collides with the 1:1 side")

    def test_two_records_on_the_same_room_overlap(self):
        from gateway.core.bot_identity import fold_record_dm_claims

        a = fold_record_dm_claims(DmClaim(), [self._record("group_dm", "g1")])
        b = fold_record_dm_claims(DmClaim(), [self._record("group_dm", "g1")])
        self.assertTrue(a.overlaps(b))

    def test_static_era_and_channel_records_claim_nothing(self):
        from unittest.mock import MagicMock as M

        from gateway.core.bot_identity import fold_record_dm_claims

        static = M(rule_name="", config={}, room_id="d1", room_kind="dm",
                   watcher_name="w-static")
        claim = fold_record_dm_claims(DmClaim(), [
            static,                                   # truly static: BOTH empty
            self._record("channel", "c1"),            # not a DM at all
        ])
        self.assertEqual(claim.rooms, frozenset())
        self.assertEqual(claim.group_rooms, frozenset())

    def test_a_damaged_room_kind_still_claims_via_room_type(self):
        """Codex round 26: a preserved record whose room_kind was corrupted
        can still answer its DM (the live wake supplies the kind), so an
        unknown kind must not read as no claim — the surviving room_type
        claims BOTH DM classes, conservatively."""
        from unittest.mock import MagicMock as M

        from gateway.core.bot_identity import fold_record_dm_claims

        damaged = M(rule_name="eng", config={"name": "w1"}, room_id="d1",
                    room_kind="", room_type="dm", watcher_name="w1")
        claim = fold_record_dm_claims(DmClaim(), [damaged])
        self.assertIn("d1", claim.rooms)
        self.assertIn("d1", claim.group_rooms)
        self.assertTrue(claim.overlaps(DmClaim(direct=True)))
        self.assertTrue(claim.overlaps(DmClaim(group_direct=True)))

    def test_a_config_only_dm_record_still_claims(self):
        """Codex round 24, the both-fields eligibility's last consumer: a DM
        record whose rule_name alone was damaged is preserved and recreated,
        so it still answers its conversation — the identity barrier must see
        its claim, or two connectors sharing an account both answer it."""
        from unittest.mock import MagicMock as M

        from gateway.core.bot_identity import fold_record_dm_claims

        damaged = M(rule_name="", config={"name": "w1", "agent": "a"},
                    room_id="d1", room_kind="dm", watcher_name="w1")
        claim = fold_record_dm_claims(DmClaim(), [damaged])
        self.assertIn("d1", claim.rooms)
        self.assertTrue(claim.overlaps(DmClaim(direct=True)))

    def test_the_refusal_names_the_record_and_the_exit(self):
        """The operator already deleted the rule — a refusal citing rules is
        unfixable from their side. The message must name the persisted
        watcher and the release path."""
        from gateway.core.bot_identity import fold_record_dm_claims

        a_claim = fold_record_dm_claims(
            DmClaim(), [self._record("dm", "d1", name="mm-dm-alice")])
        conflicts = find_identity_conflicts([
            _entry("a", scope="team1", dms=a_claim),
            _entry("b", scope="team2", dms=DmClaim(direct=True)),
        ])
        self.assertEqual(len(conflicts), 1)
        self.assertIn("mm-dm-alice", conflicts[0])
        self.assertIn("expire", conflicts[0])

class TestStaticDmWatchersReachTheCheck(unittest.TestCase):
    """The wiring, not the helper: `GatewayService` must derive its DM owners this way.

    Two Mattermost connectors on one account and different teams are allowed — except
    when both handle DMs, because a DM channel is account-level and both would answer.
    With static watchers being the only form that runs, this is the path that matters.
    """

    def _config(self, root, second_rooms_block):
        import textwrap

        path = root / "config.yaml"
        path.write_text(textwrap.dedent(f"""
            connectors:
              - name: mm-eng
                type: mattermost
                server:
                  url: https://mm.example.com
                  team: eng
                  username: acg-bot
                  password: secret
              - name: mm-sales
                type: mattermost
                server:
                  url: https://mm.example.com
                  team: sales
                  username: acg-bot
                  password: secret
            agents:
              default:
                type: claude
                working_directory: {root}
            watcher_rules:
              - name: w1
                agent: default
                connector: mm-eng
                rooms:
                  direct: true
              - name: w2
                agent: default
                connector: mm-sales
                rooms:
{second_rooms_block}
        """))
        return path

    def _service_dm_owners(self, second_rooms_block):
        import tempfile
        from pathlib import Path as P
        from unittest.mock import patch

        from gateway.config import GatewayConfig
        from gateway.core import state as state_mod
        from gateway.service import GatewayService

        with tempfile.TemporaryDirectory() as d:
            root = P(d)
            cfg = GatewayConfig.from_file(str(self._config(root, second_rooms_block)))
            with patch.object(state_mod, "RUNTIME_DIR", root / "runtime"):
                service = GatewayService(cfg)
            return service._dm_claims

    def test_rule_dm_opt_ins_become_stream_claims(self):
        """A rule's `direct: true` claims the whole 1:1 stream — the per-room
        claim died with the static shape, because DMs have no name for a rule
        pattern to match. Two connectors both opting in therefore overlap,
        which is exactly what the identity barrier must see."""
        claims = self._service_dm_owners("                  direct: true")
        self.assertTrue(claims["mm-eng"].direct)
        self.assertTrue(claims["mm-sales"].direct)
        self.assertTrue(
            claims["mm-eng"].overlaps(claims["mm-sales"]),
            "two whole-stream claims on one account overlap",
        )

    def test_a_channel_rule_claims_nothing(self):
        """Otherwise the test above would pass against a map that always holds both."""
        self.assertNotIn(
            "mm-sales",
            self._service_dm_owners("                  include: [incidents]"),
        )


class TestInboundStartsAfterWatchersExist(unittest.IsolatedAsyncioTestCase):
    """A connector that reads before its rooms are known loses those messages.

    Mattermost's socket delivers every channel the account can see, and the handler
    returns early for a channel with no state. Nothing replays them: the watermark
    restore only covers channels that already exist. So "not subscribed yet" becomes
    "permanently lost" — a window that already spanned each connector's own watcher
    restore, and that the identity barrier widens by every other connector's login.
    """

    async def test_sync_only_starts_the_stream_after_restoring_watchers(self):
        from unittest.mock import AsyncMock, MagicMock, call

        from tests.helpers import make_bare_session_manager

        sm = make_bare_session_manager()
        order = MagicMock()
        sm._connector.start_inbound = AsyncMock(side_effect=lambda: order("inbound"))
        sm._lifecycle.sync_watchers = AsyncMock(side_effect=lambda **kw: order("sync") or [])

        await sm.sync_only()

        self.assertEqual(order.call_args_list, [call("sync"), call("inbound")])

    async def test_the_stream_starts_even_when_a_watcher_failed(self):
        """Per-watcher failures are reported, not a reason to leave the connector deaf
        for the rooms that did start."""
        from unittest.mock import AsyncMock

        from tests.helpers import make_bare_session_manager

        sm = make_bare_session_manager()
        sm._lifecycle.sync_watchers = AsyncMock(return_value=["w1 failed"])

        errors = await sm.sync_only()

        self.assertEqual(errors, ["w1 failed"])
        sm._connector.start_inbound.assert_awaited_once()

    async def test_mattermost_connect_opens_the_socket_without_reading_it(self):
        """Splitting at the listen loop, not at the socket: the socket being open is
        what lets the client buffer arrivals during the wait instead of missing them."""
        from unittest.mock import AsyncMock, MagicMock

        from gateway.connectors.mattermost.connector import MattermostConnector

        c = MattermostConnector.__new__(MattermostConnector)
        c._rest = MagicMock(
            authenticate=AsyncMock(), get_me=AsyncMock(), resolve_team=AsyncMock(),
            bot_username="bot")
        c._config = MagicMock(server_url="https://mm.example.com", team="eng")
        c._ws = MagicMock(connect=AsyncMock(), start=AsyncMock())

        await c.connect()
        c._ws.connect.assert_awaited_once()
        c._ws.start.assert_not_awaited()

        await c.start_inbound()
        c._ws.start.assert_awaited_once()

    async def test_the_base_connector_has_nothing_to_defer(self):
        """Rocket.Chat gates delivery per room, so it needs no carve-out — the default
        must be a no-op rather than an abstract method every connector has to answer."""
        from gateway.connectors.script.connector import ScriptConnector

        await ScriptConnector().start_inbound()


class TestTheDocumentedLifecycleWorks(unittest.IsolatedAsyncioTestCase):
    """Splitting startup changed a public contract, so the contract is executed here.

    `MattermostConnector`'s class docstring shows an embedding using the connector
    directly, and the base `connect()` docstring is what a new connector author reads.
    Deferring the listen loop made "connect, then subscribe, then receive" false for
    those callers — silently, since the handler simply never fires. This walks the
    documented order and requires delivery, so the docs and the code fail together.
    """

    async def test_connect_subscribe_start_inbound_delivers(self):
        from unittest.mock import AsyncMock, MagicMock

        from gateway.connectors.mattermost.connector import MattermostConnector

        started = {}

        connector = MattermostConnector.__new__(MattermostConnector)
        connector._rest = MagicMock(
            authenticate=AsyncMock(), get_me=AsyncMock(), resolve_team=AsyncMock(),
            bot_username="bot", bot_user_id="bot-id")
        connector._config = MagicMock(server_url="https://mm.example.com", team="eng")
        connector._ws = MagicMock(
            connect=AsyncMock(),
            start=AsyncMock(side_effect=lambda: started.setdefault("reading", True)),
        )
        connector._channels = {}
        connector._handler = MagicMock()

        await connector.connect()
        self.assertNotIn(
            "reading", started, "reading before any room is known loses those events")

        room = MagicMock(id="chan-1", name="town-square", type="channel")
        await connector.subscribe_room(room, watcher_id="w1")
        self.assertIn("chan-1", connector._channels)

        await connector.start_inbound()
        self.assertTrue(started["reading"], "the documented final step must start it")

    def test_the_usage_example_names_the_step(self):
        """The example is what an embedder copies; a stale one teaches a lifecycle that
        drops every message."""
        from gateway.connectors.mattermost.connector import MattermostConnector

        doc = MattermostConnector.__doc__ or ""
        self.assertIn("start_inbound()", doc)
        self.assertLess(
            doc.index("subscribe_room"), doc.index("start_inbound()"),
            "the example must subscribe before it starts reading",
        )


class TestVoiceAcceptsOnlyWhenReady(unittest.IsolatedAsyncioTestCase):
    """An accountless inbound server has the same problem as a chat socket.

    The voice connector opens an HTTP listener, and a request arriving before its
    watcher's processor exists reaches an empty dispatcher and is told the gateway is
    busy — a wrong answer from a daemon that is merely still starting, and the identity
    barrier widens the window to every other connector's login.
    """

    async def test_connect_binds_without_accepting_and_start_inbound_accepts(self):
        import asyncio
        import socket

        from gateway.connectors.voice.config import VoiceConfig
        from gateway.connectors.voice.connector import VoiceConnector

        with socket.socket() as probe:  # a free port, released before the connector binds
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]

        connector = VoiceConnector(VoiceConfig(host="127.0.0.1", port=port, secret=""))
        await connector.connect()
        try:
            self.assertIsNotNone(connector._server)
            self.assertFalse(
                connector._server.is_serving(),
                "accepting here would answer requests no watcher can serve yet",
            )

            await connector.start_inbound()
            self.assertTrue(connector._server.is_serving())

            # And it really accepts, rather than merely reporting that it does.
            reader, writer = await asyncio.open_connection("127.0.0.1", port)
            writer.close()
            await writer.wait_closed()
        finally:
            await connector.disconnect()

    async def test_the_port_conflict_still_fails_at_connect(self):
        """Binding stays in `connect()` on purpose: a port already in use is a startup
        failure, and it is easiest to attribute at the moment the connector claims it."""
        import socket

        from gateway.connectors.voice.config import VoiceConfig
        from gateway.connectors.voice.connector import VoiceConnector

        with socket.socket() as taken:
            taken.bind(("127.0.0.1", 0))
            taken.listen(1)
            port = taken.getsockname()[1]

            connector = VoiceConnector(VoiceConfig(host="127.0.0.1", port=port, secret=""))
            with self.assertRaises(OSError):
                await connector.connect()


class TestDeferredInboundIsDocumented(unittest.TestCase):
    """A connector that defers accepting must say so where an embedder will read it.

    The failure is silent in the worst way: a direct embedding follows a docstring that
    ends at `connect()`, and its handler simply never fires. This was fixed once for
    Mattermost and missed for the voice connector in the same round — an instance patched
    instead of a shape — so it is enumerated rather than remembered.
    """

    def _deferring_connectors(self):
        import gateway.connectors as pkg
        from gateway.core.connector import Connector

        found = {}
        for mod in pkgutil.walk_packages(pkg.__path__, f"{pkg.__name__}."):
            try:
                module = __import__(mod.name, fromlist=["_"])
            except Exception:
                continue
            for name, obj in vars(module).items():
                if (
                    inspect.isclass(obj)
                    and issubclass(obj, Connector)
                    and obj.__module__ == mod.name
                    and obj.start_inbound is not Connector.start_inbound
                ):
                    found[name] = obj
        return found

    def test_the_sweep_finds_the_deferring_connectors(self):
        """Vacuously-true insurance: an empty sweep would pass every assertion below."""
        self.assertEqual(
            set(self._deferring_connectors()),
            {"MattermostConnector", "VoiceConnector", "RocketChatConnector"},
        )

    def test_each_one_documents_the_extra_phase(self):
        for name, cls in sorted(self._deferring_connectors().items()):
            with self.subTest(connector=name):
                doc = cls.__doc__ or ""
                self.assertIn(
                    "start_inbound()", doc,
                    f"{name} defers accepting input but its class docstring never says "
                    f"so, so a direct embedding silently receives nothing",
                )
                self.assertLess(
                    doc.index("connect()"), doc.index("start_inbound()"),
                    "the documented order must match the required order",
                )
