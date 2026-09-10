"""One bot account, one connector — stated once, checked in two places (§4.5).

Under subscribe-all, a connector receives everything its account can see, so two
connectors sharing an account receive identical streams and every room matching rules
on both gets two agents answering. The `(connector, room_id)` watcher key cannot see
it: the records differ in their connector component and each connector writes its own
state file.

**Config cannot decide this.** Mattermost supports token-only auth, where `username` is
empty, and two *different* tokens can authenticate the *same* account — so comparing
config fields misses precisely the case the rule exists to catch, and comparing tokens
misses it too. The enforcement point is therefore runtime, after each connector has
authenticated and can report the identity the *platform* gave it.

The load-time check in `gateway/config.py` is an optimisation on top of this, not a
second rule: it compares declared credentials so an obvious mistake fails at
`config validate` rather than at startup. Both call `find_identity_conflicts()` so the
rule has one statement. Where the two disagree, the runtime one is right.
"""

from __future__ import annotations

from dataclasses import dataclass
from ipaddress import ip_address
from urllib.parse import urlsplit

_DEFAULT_PORTS = {"http": 80, "https": 443, "ws": 80, "wss": 443}


class ConnectorIdentityError(Exception):
    """A connector could not establish who it is authenticated as.

    Fail-closed on purpose: a connector that cannot answer this question cannot be
    checked against the others, and starting it unvalidated is the situation §4.5
    exists to prevent. Raised by a connector's own `bot_identity()`.
    """


class DuplicateBotIdentityError(Exception):
    """Two connectors are the same bot account on the same server."""


def canonical_origin(url: str) -> str:
    """`scheme://host[:port][/path]` — comparable, from a URL a human typed.

    Two connectors pointing at one server routinely spell it differently:
    `https://mm.example.com`, the same with a trailing slash, and
    `https://mm.example.com:443` are one origin, and comparing the raw strings would
    call them three accounts and let the duplicate through. The same lesson as
    canonicalizing a working directory before comparing session identity — a
    comparison is only as good as the normalisation in front of it.

    Normalisation has a second failure direction, and it is the worse one: collapsing
    things that are genuinely different produces a *false* duplicate, and this check
    responds to a duplicate by refusing to start.

    * **The path is kept.** Two deployments can share a host under different prefixes
      (`https://host/rc-one`, `https://host/rc-two`) — the REST and WebSocket clients
      both build their URLs on that prefix, so those are two servers. Dropping it would
      refuse a valid pair.
    * **An IP literal is reduced to its canonical form, and an IPv6 host keeps its
      brackets.** Without them `https://[::1]:8443` and
      `https://[::1:8443]` both render as `https://::1:8443`, one address-and-port and
      one address, indistinguishable. Compression matters for the opposite reason:
      `[2001:0db8:0:0:0:0:0:1]` and `[2001:db8::1]` are one address, and comparing the
      text would miss a duplicate rather than invent one. Rare, and kept anyway because
      it is six self-contained lines: a literal in `server.url` means a deployment with
      no usable DNS, which is unusual at the scale where IPv6 is forced but not
      impossible — and both failure directions are silent.
    * **A Unicode host is encoded to its IDNA form.** `bücher.example` and
      `xn--bcher-kva.example` are one name; comparing the text misses the duplicate.
    * **A terminal DNS root dot is dropped.** `chat.example.com.` and `chat.example.com`
      resolve to one server, so keeping the dot would split one account into two keys —
      a missed duplicate, which here means two agents in the same room.
    * **An unparseable port is left alone rather than normalised.** `urlsplit().port`
      raises on a non-numeric or out-of-range port, and this function is reached from
      `coop config validate`, where a traceback replaces the attributed bad-URL finding
      the operator needs. The malformed string becomes its own origin: it compares
      equal only to an identical mistake, which is the harmless answer.

    The default port for the scheme is dropped, so the explicit and implicit spellings
    converge. Query and credentials are discarded: they address a request, not a server.
    """
    try:
        parsed = urlsplit(url if "//" in url else f"//{url}", scheme="https")
        scheme = (parsed.scheme or "https").lower()
        host = (parsed.hostname or "").lower().rstrip(".")
    except ValueError:
        # `urlsplit` itself rejects a bracketed host that is not an IP literal, so the
        # earlier per-field guard was not enough: this function is called from
        # `coop config validate`, and *any* string an operator can type must come back as
        # a value rather than a traceback. Unparseable text is its own origin — it
        # matches only an identical mistake.
        return url.strip().lower().rstrip("/")
    return _format_origin(scheme, host, parsed)


def _format_origin(scheme: str, host: str, parsed) -> str:
    """The comparable form, once the URL has parsed."""
    if not host.isascii():
        # `bücher.example` and `xn--bcher-kva.example` are one DNS name. Guarded on
        # non-ASCII so the ordinary path never touches Python's quirky idna codec,
        # which rejects empty and over-long labels that resolve perfectly well.
        try:
            host = host.encode("idna").decode("ascii")
        except UnicodeError:
            pass  # not encodable: compare it as written
    try:
        # `2001:0db8:0:0:0:0:0:1` and `2001:db8::1` are one address written two ways,
        # and a textual comparison calls them two servers — a missed duplicate.
        host = ip_address(host).compressed
    except ValueError:
        pass  # a name, or a malformed literal: compare it as written
    if ":" in host:  # IPv6 literal — brackets are what make host and port separable
        host = f"[{host}]"
    try:
        port = parsed.port
    except ValueError:
        # Not a number, or outside 1-65535. Nothing here can repair it, and raising
        # would crash a validation run that exists to report exactly this kind of typo.
        return f"{scheme}://{parsed.netloc.lower()}{parsed.path.rstrip('/')}"
    if port is not None and _DEFAULT_PORTS.get(scheme) == port:
        port = None
    authority = f"{host}:{port}" if port else host
    return f"{scheme}://{authority}{parsed.path.rstrip('/')}"


@dataclass(frozen=True)
class BotIdentity:
    """Who a connector is authenticated as, as the *platform* reports it.

    `scope` is Mattermost's resolved team id, and is the one exception to "one account,
    one connector": the socket spans every team the account belongs to, so two
    connectors on one account are safe for *channels* provided each discards events for
    other teams. It must be the **resolved** team id, never the configured `team:`
    string — that field accepts a name or an id, so two connectors on one team can spell
    it two ways and compare as different.

    An empty `scope` means the connector has no sub-scope to separate it from another on
    the same account, which is Rocket.Chat's situation: no team concept, so two
    connectors on one account duplicate everything.

    `platform` is part of the key because user ids live in per-platform id spaces. A
    Rocket.Chat and a Mattermost deployment reachable at one origin are two independent
    authentication realms, and an id — or, at config-validation time, a conventional
    username like `bot` — colliding across them is a coincidence, not one account. Two
    connectors of different types can never be the same account, so keeping the platform
    in the key costs nothing and removes a class of false refusals.
    """

    platform: str
    origin: str
    user_id: str
    scope: str = ""


@dataclass(frozen=True)
class DmClaim:
    """How much of an account's direct-message traffic one connector answers.

    Three claims, not one flag, because collapsing them refuses configurations that
    work — and this check answers a collision by refusing to start:

    * `direct` — every 1:1 DM the account receives (`rooms.direct` on a rule).
    * `group_direct` — every group DM (`rooms.group_direct`). A separate class in
      `RoomMatcher.match()`, so a connector claiming only this cannot collide with one
      claiming only 1:1 DMs.
    * `rooms` — the specific conversations named by **static** watchers. Such a watcher
      claims exactly one resolved channel: `subscribe_room()` registers that channel and
      `_on_posted_event()` ignores every other, so two connectors watching `@alice` and
      `@bob` cannot both answer one message.
    """

    direct: bool = False        # every 1:1 DM
    group_direct: bool = False  # every group DM
    rooms: frozenset[str] = frozenset()  # named 1:1 conversations
    # Specific group-DM conversations, claimed by persisted records (Codex
    # round 6): sticky binding keeps a record answering its room after the
    # rule that created it is gone, so a record is a claim exactly the way a
    # rule is. Nothing rule-shaped produces one — group_direct claims the
    # class — so this is populated only from state files.
    group_rooms: frozenset[str] = frozenset()
    # Provenance for the refusal message, never part of the overlap logic:
    # the watchers whose persisted records contributed to `rooms`/
    # `group_rooms`. An operator who already deleted the rule cannot fix a
    # refusal that cites rules — the exit is `expire <name>` or the TTL.
    record_watchers: frozenset[str] = frozenset()

    def overlaps(self, other: "DmClaim") -> bool:
        """Whether both connectors could answer the same direct message.

        1:1 and group DMs are separate classes, not one stream: `RoomMatcher.match()`
        gates `RoomKind.DM` on `direct` and `RoomKind.GROUP_DM` on `group_direct`
        independently, so a connector claiming only one of them cannot collide with a
        connector claiming only the other. Collapsing them refused that pairing.

        A named room is a 1:1 conversation — `@someone` addresses one person, and both
        connectors resolve it through their direct-channel endpoint — so it overlaps a
        1:1 claim but never a group-DM one. A named GROUP room (a persisted
        group-DM record) symmetrically overlaps the group class and other
        named group rooms, never the 1:1 side.
        """
        mine_1to1 = self.direct or bool(self.rooms)
        theirs_1to1 = other.direct or bool(other.rooms)
        if self.direct and theirs_1to1:
            return True
        if other.direct and mine_1to1:
            return True
        mine_group = self.group_direct or bool(self.group_rooms)
        theirs_group = other.group_direct or bool(other.group_rooms)
        if self.group_direct and theirs_group:
            return True
        if other.group_direct and mine_group:
            return True
        if self.group_rooms & other.group_rooms:
            return True
        return bool(self.rooms & other.rooms)


@dataclass(frozen=True)
class ConnectorIdentity:
    """One connector's answer, plus what it claims of the account's DM traffic."""

    connector_name: str
    identity: BotIdentity
    dms: DmClaim = DmClaim()


def dm_claims(watcher_rules) -> dict[str, DmClaim]:
    """What each connector claims of its account's direct messages.

    Post-cutover a rule's DM opt-ins are the only claim shape: `direct` and
    `group_direct` each claim a whole DM class, because a DM has no room name
    for a pattern to match. `DmClaim.rooms` — the per-room claim the static
    `@someone` watchers used to make — stays as a *type* for §2.7's reserved
    object form (`direct: {include: [...]}`), but nothing produces one today,
    so two connectors that both opt into a class simply overlap.

    Takes the list rather than a `GatewayConfig` because this module is in
    `gateway.core`, which the config layer imports and must not import back.
    """
    direct = {r.connector for r in watcher_rules if r.rooms.direct}
    group = {r.connector for r in watcher_rules if r.rooms.group_direct}

    return {
        name: DmClaim(
            direct=name in direct,
            group_direct=name in group,
        )
        for name in direct | group
    }


def fold_record_dm_claims(claim: DmClaim, records) -> DmClaim:
    """The claim above, widened by what a connector's persisted records still
    answer (Codex round 6).

    Sticky binding (§2.4) keeps a rule-derived DM record answering its room
    after the rule that created it is deleted — so a rule-only claim misses
    exactly the records that outlive their rules, and two connectors sharing
    an account can both answer one private conversation. Paused records are
    included: `resume` revives them, so they still claim. Static-era records
    (no rule_name) are pruned at the next boot and claim nothing.
    """
    rooms = set(claim.rooms)
    group_rooms = set(claim.group_rooms)
    watchers = set(claim.record_watchers)
    for record in records:
        # The same rule_name-or-config eligibility as hydration, boot
        # recovery and the prune (Codex rounds 22-24): a DM record whose
        # rule_name alone was damaged is preserved and recreated, so it
        # still answers its conversation — excluding it from the claim let
        # two connectors sharing an account both answer that DM.
        if not (record.rule_name or record.config) or not record.room_id:
            continue
        # A damaged room_kind falls back to room_type (Codex round 26):
        # the record is preserved and can still answer its DM — a live wake
        # supplies the kind — so an unknown kind must not read as no claim.
        # The fallback is conservative: a bare "dm" room_type with no finer
        # kind claims BOTH classes rather than neither.
        kind = record.room_kind or ""
        if kind not in ("dm", "group_dm") and getattr(
                record, "room_type", "") == "dm":
            rooms.add(record.room_id)
            group_rooms.add(record.room_id)
            watchers.add(record.watcher_name)
        elif kind == "dm":
            rooms.add(record.room_id)
            watchers.add(record.watcher_name)
        elif kind == "group_dm":
            group_rooms.add(record.room_id)
            watchers.add(record.watcher_name)
    return DmClaim(
        direct=claim.direct,
        group_direct=claim.group_direct,
        rooms=frozenset(rooms),
        group_rooms=frozenset(group_rooms),
        record_watchers=frozenset(watchers),
    )


def find_identity_conflicts(entries: list[ConnectorIdentity]) -> list[str]:
    """Every reason these connectors cannot run together, as operator-facing lines.

    Returns all conflicts rather than the first: a config with three colliding
    connectors should not need three restarts to learn that.

    The rule, applied per `(origin, user_id)` group:

    * a group of one is always fine;
    * a group of more than one is a conflict **unless** every member has a distinct,
      non-empty `scope` — the Mattermost different-teams exception;
    * within an excepted group, no two connectors may claim **overlapping** direct
      messages. A DM has no team, so the team gate cannot separate it and the platform
      delivers it to every socket the account has open — but a static watcher claims one
      channel rather than the stream, so two connectors on `@alice` and `@bob` are fine
      and only an actual overlap is a conflict.
    """
    conflicts: list[str] = []
    groups: dict[tuple[str, str, str], list[ConnectorIdentity]] = {}
    for e in entries:
        key = (e.identity.platform, e.identity.origin, e.identity.user_id)
        groups.setdefault(key, []).append(e)

    for (_platform, origin, user_id), group in sorted(groups.items()):
        if len(group) < 2:
            continue
        names = ", ".join(sorted(f"'{e.connector_name}'" for e in group))
        scopes = [e.identity.scope for e in group]
        if any(not s for s in scopes) or len(set(scopes)) != len(scopes):
            conflicts.append(
                f"Connectors {names} authenticate as the same bot account "
                f"({user_id} on {origin}). Two connectors sharing an account receive "
                f"identical message streams, so every room matching rules on both gets "
                f"two agents answering it. Give each connector its own bot account, or "
                f"— on Mattermost only — scope them to different teams."
            )
            continue
        for i, a in enumerate(group):
            for b in group[i + 1:]:
                if not a.dms.overlaps(b.dms):
                    continue
                pair = ", ".join(
                    sorted(f"'{e.connector_name}'" for e in (a, b)))
                shared = sorted((a.dms.rooms & b.dms.rooms)
                                | (a.dms.group_rooms & b.dms.group_rooms))
                if shared:
                    detail = f"both watch {', '.join(shared)}"
                elif a.dms.direct or b.dms.direct:
                    detail = "one of them takes every 1:1 direct message"
                else:
                    detail = "both take every group direct message"
                # A claim from a persisted record needs its own exit named:
                # the operator may have already deleted the rule, so a
                # refusal citing rules would be unfixable from their side.
                record_note = ""
                holders = sorted(
                    f"'{w}' (connector '{e.connector_name}')"
                    for e in (a, b) for w in e.dms.record_watchers)
                if holders:
                    record_note = (
                        f" Part of this claim comes from persisted watcher "
                        f"record(s) {', '.join(holders)}, which sticky "
                        f"binding keeps answering their rooms even though no "
                        f"current rule names them — release them with "
                        f"'expire <name>' or wait out their session TTL."
                    )
                conflicts.append(
                    f"Connectors {pair} share the bot account {user_id} on {origin} and "
                    f"their direct-message coverage overlaps — {detail}. Different teams "
                    f"keep their channels apart, but a DM has no team and reaches every "
                    f"connection the account has open, so both would answer it. Leave "
                    f"the overlapping direct messages to one of them."
                    f"{record_note}"
                )
    return conflicts
