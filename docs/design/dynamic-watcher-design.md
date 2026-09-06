# Dynamic watcher design

Status: **design, not implemented.** Both connector assumptions have been
verified against live Rocket.Chat and Mattermost servers — see §6 for the
observed behaviour and what it settles.

---

## 1. Motivation

### 1.1 The current model

A watcher is a 1:1 binding between one room and one agent session, declared
in `config.yaml` and started at gateway boot:

```yaml
watcher_rules:
  - name: rc-nest
    connector: rc-home
    room: nest
    agent: claude-main
```

`WatcherConfig.room` is a single concrete room name. `sync_watchers()`
resolves each one and starts a processor for it. There is no runtime
add-watcher path.

### 1.2 Why it needs to change

- **Rooms are created faster than config is edited.** A team spins up an
  incident channel and adds the bot to it. Nothing happens until someone
  edits `config.yaml` and restarts the gateway.
- **Every room costs startup time whether or not it is used.** Boot starts
  every configured watcher unconditionally, including rooms nobody has
  messaged in months.
- **Nothing is ever reclaimed.** A watcher's session lives until an
  operator removes it from config. There is no idle or expiry notion, so
  resource cost grows monotonically with the number of rooms ever
  configured.

### 1.3 Requirements

- **R1** — An operator configures *how* to build a watcher, not *which
  rooms* to watch. Room names are never enumerated in config.
- **R2** — A watcher and its session are created on first activity in a
  matching room, with no restart and no config edit.
- **R3** — Idle watchers release their runtime cost but keep conversational
  continuity; genuinely dead ones release everything.
- **R4** — One session serves exactly one room, always. This is a
  confidentiality boundary, not a tidiness preference — see §4.1.
- **R5** — Different rooms can be served by different agents with different
  settings, chosen by rule rather than by enumeration.
- **R6** — One code path. A watcher's configuration must be derivable
  identically from the message path, the control path and the scheduler.

---

## 2. Design

### 2.1 Watcher rules

**The block is `watcher_rules:`, and the name is load-bearing.** It began as
`watchers:` — the same key the static shape used — so that one block could carry
both shapes while the cutover landed. Keeping it afterwards cost more than it
looked:

* it read as a list of watchers when an entry is not one. The config tool calls
  them rules, the parsed attribute is `GatewayConfig.watcher_rules`, and the
  sibling block is `watcher_templates:` — the config key was the only place
  still saying "watchers";
* the loader had to *guess* which shape an entry used, and it guessed by looking
  for a `rooms:` **mapping** on the raw entry. That guess ran BEFORE templates
  were merged, so an entry taking its rooms from a `watcher_templates:` entry
  did not read as a rule at all — which is why `rooms` was forbidden in a
  template. A restriction on a feature, to support a shape that no longer
  exists.

Renaming the block removed both. Every entry under `watcher_rules:` is a rule by
virtue of the block it is in, so the shape test is gone and `rooms` is
inheritable; a config still using `watchers:` is reported by the general
unknown-top-level-key check, which names `watcher_rules` as the likely
intention. No migration-specific code was added for it.

A `watcher_rules:` entry stops naming a room and starts describing how to build a
watcher for whatever rooms match it:

```yaml
watcher_rules:
  - name: eng                    # rule identity, operator-supplied, unique
    connector: mm-home
    agent: claude-eng
    rooms:
      include: ["eng-*", "incident-*"]
      except_for: ["eng-archive"]
    session_idle_days: 15        # default 15 — see §2.5
    session_expire_days: 15      # default 15, measured from the idle moment
    # plus every existing watcher parameter: context_inject_files,
    # history_handoff, notifications, inherits, …
```

**Multiple rules per connector are allowed. The first match in config order
wins.** Precedence is explicit and positional; there is no scoring or
specificity heuristic.

Consequences that must be implemented rather than assumed:

- Patterns are compiled and validated **at config load**. An invalid pattern
  must never surface on the message-delivery path, where no operator is
  present to fix it.
- **Config order is load-bearing.** Any tool that rewrites `config.yaml`
  must preserve watcher entry order; reordering silently re-routes rooms.
**The pattern language is globs, not regexes**, deliberately constrained so the
static checks below are decidable:

| | |
|---|---|
| syntax | `*` (any run, including empty), `?` (one character), `[…]` character class. Nothing else — no alternation, no quantifiers, no anchors |
| matched against | the room's **full** platform name, implicitly anchored at both ends |
| case | sensitive; both platforms' slugs are lowercase by construction |
| unicode | compared NFC-normalised, so a decomposed pattern matches a composed name |
| `except_for` | subtracts from **this rule's own `include`**, evaluated after it. A room it removes does **not** fall through to a later rule — the rule claimed it and then declined it |

That last row is a real decision rather than an implementation detail: fall-
through would make `except_for` a routing operator, and two rules could then
silently contend for the same room. Two consequences follow from it that are
easy to get backwards, so both are stated outright.

**The key is named `except_for`, not `exclude`, and the name is doing work.**
"Exclude" reads as absolute — *exclude this room from the bot* — whereas the
behaviour is relative: *of the rooms I include, not these*. English does not let
"except for" stand alone, so the word itself makes a reader look for the
`include` it subtracts from, which is exactly the relationship that has to be
understood to use it correctly.

**It only affects rooms the rule already includes.** A name no `include` pattern
matches is `NO_MATCH`, which falls through — so listing a room under `except_for`
without including it does **not** keep a later rule from claiming it. It looks
like protection and is a no-op. Because that reads as the opposite of what it
does, an `except_for` pattern that cannot overlap the rule's `include` union is a
hard error rather than a warning (below).

**Including a room and excluding it is how you block it entirely.** The rule
claims the room, declines it, and `DECLINED` does not fall through, so no later
rule sees it:

```yaml
watcher_rules:
  - name: never-here                  # a deny rule
    connector: mm-home
    rooms:
      include: ["tmp-*"]
      except_for: ["tmp-*"]
  - name: everything-else
    connector: mm-home
    rooms:
      include: ["*"]                  # never sees tmp-*
```

This is the only way the rule language expresses "no rule may claim this room",
which is why it is documented as an idiom rather than reported as a contradiction:
a warning here would fire on every legitimate deny rule, and nothing
distinguishes the idiom from a copy-paste error.

Given globs, the load-time checks split into three tiers by what is actually
decidable, rather than one promise that cannot be kept:

- **Hard errors**: a syntactically invalid pattern; an empty include list on a
  rule that is not DM-only; a duplicate rule name; an `except_for` pattern that
  cannot overlap the rule's own `include` union. That last one is an error rather
  than a warning because the config it describes is not merely useless — it reads
  as protecting a room while leaving it free for any later rule to claim, so
  accepting it quietly is how an operator ends up believing a room is off limits
  when it is not. Glob intersection is decidable for this syntax, so the check is
  exact.
- **Exact warnings**, decidable for globs: one rule fully shadowed by an
  earlier one (glob subsumption is decidable for this syntax), and a DM opt-in
  shadowed by an earlier rule that already claimed that class (§2.7). A rule can
  reach rooms in three independent ways — by name, and by each DM class — so a
  hybrid rule can lose one reach and stay live for the others; each dead reach is
  its own warning, since a rule whose DM opt-in is dead looks perfectly healthy
  from its patterns alone.

  **An earlier rule's blocking language is its `include`, not its `include`
  minus its `except_for`** — the one part of this that is easy to get backwards.
  Because `except_for` produces a decline that halts routing rather than falling
  through, a room the earlier rule *declines* never reaches a later rule either.
  An earlier rule therefore shadows everything its `include` matches, whether it
  goes on to claim or decline it, and its own `except_for` has no bearing on what
  it shadows. A deny rule shadows later rules for its rooms completely, which is
  exactly what it is for.
- **Observational only**: a rule that has matched zero rooms. This is *not*
  reported as a dead rule, because it is indistinguishable from a correct rule
  whose rooms have simply been quiet — `list` shows the count and lets the
  operator judge.

"A pattern matching nothing" is deliberately absent from the error tier: with
no room inventory at load time, it is not knowable, and claiming otherwise
would either produce false positives or require the room discovery this design
does without.
- `session_id` is **rejected on a rule** (§2.4).

### 2.2 Routing

Each connector subscribes to **all** messages for its platform user and
never unsubscribes for lifecycle reasons. Per inbound message:

```
message → connector (no lifecycle knowledge)
        → router: resolve a RoomRef (id, kind, name-or-participants)
        → first matching rule, in config order
            no match  → drop
            match     → manager.get_or_create(connector, room_ref)
                          → deliver
```

**Resolving the `RoomRef` is not always synchronous, and that shapes the
flow.** Mattermost supplies everything on the event — id, type, name, and for
a DM the counterpart via `channel_display_name` — so its resolution is pure
local work. Rocket.Chat supplies id and a type letter, but reports **both** DM
kinds as `d` (§6.4), so distinguishing a 1:1 from a group DM needs a
participant lookup over REST.

That matters because the kind is needed *before* rule matching: a rule with
`direct: true` and no `group_direct` must not match a group DM. So on
Rocket.Chat even the decision to **drop** a DM can require a network call,
which cannot sit on the semaphore-held handler path (§2.7). The ordering is
therefore:

1. Cheap synchronous rejects that need no room metadata at all — own-message
   echoes, system messages, the sender allow-list.
2. Classify. Free on Mattermost. On Rocket.Chat, if the room is type `d` and
   its kind is not already cached, hand off to the off-handler task with the
   message buffered, and classify there.
3. Match rules against the now-known kind, then create or drop.

**Cache the kind per room id, and it needs no invalidation.** A room's kind does
not change, and the obvious exception — a 1:1 DM that gains a member and becomes
a group DM — is not reachable on Rocket.Chat: the server refuses to add a
participant to a type-`d` room at all, and a DM with a different member set is a
*different room with its own id* (§6.4). The cache therefore cannot go stale, in
either direction.

This is worth stating explicitly because the alternative would have been
awkward: ordinary frames for a DM carry only type `d`, so nothing in the message
stream would signal that a cached kind had become wrong, and the one event that
would — a participant change — arrives as a system message, which step 1 above
rejects before classification ever runs. If a future server version does allow
in-place growth, that reject is the point an invalidation would have to hook
ahead of.

#### One dedup transaction, for every outcome

Because classification and creation can now both happen off the handler path,
message accounting needs a single state machine rather than a rule per branch.
Today's shape — register the id optimistically, roll back on a `False` return
but not on an exception — has no answer for "buffered, still classifying".

**Reserve → resolve → commit or abort:**

| Step | Effect |
|---|---|
| **Reserve** the message id, before any async work | A concurrent duplicate for the same room is recognised and discarded. The reservation is in-memory and does **not** advance the durable watermark. |
| **Resolve**: classify, match, create, enqueue | May await. May be interrupted. |
| **Commit** — on successful enqueue *or* a deliberate drop | Id becomes durably seen; watermark advances. A deliberate drop is a completed decision, so re-delivering it later would be wrong. |
| **Abort** — on any retryable failure | Reservation released, watermark unchanged, so redelivery or reconnect replay can retry. Creation failures are retryable; a rule miss is not. |

Terminal outcomes each need a defined effect, and each needs a test:

- rule matched, watcher created, message enqueued → **commit**
- no rule matched (including a DM whose kind opted out) → **commit**; the
  decision is final
- classification lookup failed (network) → **abort**; the kind is unknown, so
  the routing decision was never actually made
- creation raised → **abort**; the message must survive, and a brand-new room
  has an empty watermark so reconnect replay would otherwise skip it
- pre-creation buffer full → **abort**, with a room-visible "starting up"
  notice; silently committing would drop a message the user watched arrive
- duplicate arrives while the first copy is reserved → discard the duplicate,
  do not disturb the reservation
- process exits mid-resolve → nothing was committed, so the watermark still
  points before the message — but **today nothing redelivers it**, and this
  gap has to be closed for the abort guarantee to mean anything (below)

The distinction that makes this coherent: **a reservation prevents duplicate
*work*; only a commit prevents duplicate *delivery*.** The watermark is the
durable record and moves once, at commit.

##### Commits within a room must be ordered

The watermark is a single timestamp per room, not a set of seen ids, so it
cannot represent "committed the later message but not the earlier one". Once
classification and creation may await, two messages from one room can be
in flight together, and that representational limit becomes a lost message.
Two rules are needed, and **neither is sufficient alone**:

- **Resolution is serialized per room.** Messages for one room resolve in
  arrival order, so commits advance the watermark monotonically. This does not
  reintroduce the stall that moving creation off the handler path removed
  (§2.7 step 3): the connector-wide permit is released either way, and only
  that one room's later messages wait. It also matches what Mattermost's
  per-channel worker already provides today.

- **An abort halts that room's commit frontier.** Serialization alone still
  loses the message: if an earlier message aborts and the queue simply moves
  on, the next message's commit advances the watermark *past* the aborted one,
  and replay — which starts after the watermark — can never return to it. The
  abort guarantee "watermark unchanged" only holds if nothing else is allowed
  to move it. So an abort blocks later commits for that room: the aborted
  resolve is retried in place with bounded backoff, and if it keeps failing the
  room parks with its watermark still below the aborted message, leaving it
  redeliverable.

The failing case to test explicitly: two messages arrive for one new room, the
second resolves successfully while the first is still classifying, and the
first then aborts. The first message must still be redeliverable afterwards.

##### Abort is only retryable if something replays

An abort leaves the watermark below the message so it *can* be redelivered, but
nothing currently performs that redelivery after a restart. Both connectors
replay history from `_on_ws_reconnect()`, which fires when an
already-running connector's socket drops and reconnects; a process restart
takes the initial `connect()` path, which registers that callback and never
invokes it. So the window between the persisted watermark and the present is
not pulled at startup — the connector simply resumes from the live socket.

This is pre-existing rather than introduced here, but the transaction above
*depends* on it, so it becomes in-scope: **an explicit startup replay is
required.** Under lazy watchers it cannot be modelled on the reconnect path,
which iterates live subscriptions — at startup nothing is subscribed yet. It
has to iterate **persisted records**, replaying each from its own stored
watermark, before or as rooms are recreated.

One residual is accepted rather than fixed: a room that never produced a
persisted record has no watermark to replay from, so a crash between the
arrival of its very first message and that message's commit loses it. Making
even that recoverable would mean persisting a reservation before any work — a
disk write for every inbound message, including all the ones the rules are
about to discard — which is out of proportion to a crash-only,
first-message-of-a-never-seen-room window. It is called out here so the
guarantee is not read as stronger than it is.

This supersedes the "defer bookkeeping until accepted" phrasing in §2.7, which
described only the success path.

##### Replay ownership: one interval, one owner

Three mechanisms replay a room's history, and they were built one increment at
a time without ever being stated together. Three consecutive review rounds then
landed in the seams between them — each round moved a boundary, and the next
found a defect at its new position. This is that statement.

**Every replay is defined by the interval it owes, and every interval has
exactly one owner.**

| Owner | Interval | Names the window? | May discharge the boundary? |
|---|---|---|---|
| `_on_ws_reconnect` | the outage the socket just ended | no — reads the room's own marks | **yes**, it read the window those marks describe |
| `WatcherManager._recreate` | everything below the record's watermark, for the room it is recreating | yes (`after_ts=record.last_processed_ts`) | no |
| the drain of a routing episode | nothing — it *claims*, never replays | — | no |

Two consequences that are not obvious and were each got wrong once:

* **A caller that names a window may not discharge the room's boundary.** The
  boundary is set by the room's own hand-back accounting — "a message below
  here was refused and must come back" — and a replay that read a *different*
  window has no claim on it. Discharging it anyway loses the refused message
  permanently, because the live watermark has moved past it by then. The rule
  applies at **every** discharge site, of which there are two per connector
  (the empty page and the dispatched batch); guarding one and not the other
  reads as correct in a single file.

* **The startup replay owns no interval at all.** It exists to *notice* that a
  room has a gap and to trigger the recreation that covers it — the recreation
  is what replays. A room in the startup scan that is already resident got that
  way through `_recreate` (its record rules out `_create`, and a rule-derived
  record is not in `watcher_rules:` so nothing starts it), so its
  interval has already been replayed and a second pass is pure duplication:
  replay hands the filter its own boundary rather than the live watermark, so
  the ts filter suppresses nothing and only the bounded id window separates the
  two passes.

* **Boot resolves a room through the connector before recreating a watcher
  from its record.** A record's stored room fields say what the room was when
  the record was written, not whether this connector still serves it: a
  Mattermost connector whose `server.team` changed under an unchanged name, or
  an account removed from a room, leaves those fields intact. `room_ref_by_id`
  is the connector's own scope check and the wake path already goes through
  it; both boot recreation sites — the lifecycle evaluation and the startup
  replay — do the same, after asking `supports_room_lookup()`: the base
  `room_ref_by_id` answers `None` for a connector with no id lookup, which must
  not read as "gone" on a path that destroys the record, so such a connector
  keeps the pre-lookup behaviour. `None` (gone, another team, no longer a member)
  reclaims the record through the removal path's shared tail (jobs cancelled,
  same end state as the bot being removed) with the full session id in the
  log — reclamation calls the backend's `delete_session`, which some
  implementations honour (OpenCode deletes the session) and others do not
  support (Claude keeps it), so the logged id is a recovery path only where
  the backend keeps the session; a raise (could not ask) leaves the record
  alone for this boot, and its
  next live message resolves the room again. In the replay the lookup runs
  *before* the history probe: a room the bot was removed from makes the probe
  itself raise, and a probe failure is skipped as best-effort, so a check after
  it would never reach exactly the rooms it exists for. Cost: the connector's
  room lookup per record the boot would recreate — one subscription read on
  Rocket.Chat; on Mattermost the channel read plus the account-wide membership
  list, so roughly two serialized requests per record on top of the existing
  history probe. Dormant records — paused, or idle with no gap — are not
  resolved here; nothing recreates them at boot. A later wake resolves the room
  itself (`_resolve_room_for_wake`); `resume` does not yet — it still rebuilds
  the room from the record's stored fields, tracked as a separate gap (#145).

The claim the drain makes is the one piece that is deliberately *not* a replay:
the frames are handed to the room's worker immediately, and the claim only
covers the case where the scalar watermark has already moved past them (above).
It is discharged by whichever reconnect next reads its own window.

An unmatched message is dropped deliberately. The connector knows nothing
about watcher lifecycle, which is both the correct separation and the reason
idle becomes cheap: **no unsubscribe means the connector's per-room state —
the dedup watermark and the recent-message-id window — survives an idle drop
untouched**, and recreation needs no re-subscribe on either connector.

Two costs this accepts:

- Per-room structures now scale with *rooms the bot is in* rather than
  *rooms with watchers*. Mattermost spawns one task and one bounded queue
  per channel id on first sight and never reaps them; a reaping hook is
  required (§5.2).
- Work already done before routing can be decided — dedup registration,
  watermark advance, attachment download, capacity preflight — is paid for
  messages that will be dropped. That path needs auditing so dropping is
  cheap rather than merely silent.

### 2.3 Identity and keys

Three distinct keys, deliberately:

| Purpose | Key |
|---|---|
| Watcher instance, sticky binding, per-room lock | `(connector, room_id)` |
| Persisted state record | `(connector, room_id)` |
| Filesystem paths (system prompt, attachment workspace) | `hash(connector, room_id)` — never the raw id |
| Display and CLI | `<connector>:<room_label>` — the operator's name for a watcher, and a **fallback** resolution key where no room id is available (see §2.8) |

The last row used to read "cosmetic, never load-bearing". It is not, in two
respects, and calling it cosmetic taught six separate readers that reaching for
the handle was free:

1. it is the identity `schedule list`/`pause`/`delete` address, so it is
   load-bearing by design;
2. more importantly, for the whole first release of dynamic watchers **the
   runtime dictionaries in `WatcherLifecycle` — records and processors — were
   keyed by the handle**, not by `(connector, room_id)` as the first two rows
   of this table specify, and `record_for_room` was a linear scan. That made the
   handle the ergonomic lookup and the room id the awkward one — the wrong way
   round, and the single cause behind the recurring defect described in §2.8.

   **Re-keyed (owner, 2026-09-01).** `_states` and `_processors` are keyed by
   room id; a name resolves through one index (`_room_of`), and every write goes
   through `_install`/`_uninstall`/`_rename`, which keep the two in step and
   refuse a name that already names a different room. `record_for_room` is a dict get. The
   per-watcher **lock** stays keyed by name on purpose: it is a mutex, not an
   identity, and it is taken before a record exists (`WatcherManager._create`
   locks `wc.name`, then starts). The on-disk state file was never keyed — it is
   a list — so nothing migrated. `tests/unit/test_lifecycle_keyed_by_room.py`
   walks the dicts and fails on any key that is not its record's room id.

The `room_name` on the state record is a **human-readable description of the
room**, refreshed from inbound messages (`WatcherLifecycle.observe_room_name`,
reached from `SessionManager._on_inbound` on every claimed frame): the
platform's own name for a named room, and for the DM kinds the room's
description — the counterpart for a 1:1, the participant list for a group DM
(§2.4). **The handle follows it** (owner, 2026-09-02): when a frame shows a
named room's name has moved, the handle is re-derived through the same
`watcher_label` the creation used, the name index and the per-watcher lock move
with it, and an AUDIT line records the change. The handle is a function of the
room's current name, computed, never an identity — the state record and a job
persist it as display metadata, and nothing keys on it. Both connectors read the current name off the frame (Mattermost
`channel_name`, Rocket.Chat `roomName`); neither consumes a rename event, so
the first message after a rename is when it shows. Direct rooms have no platform name
to carry, and leaving the field empty for them made `list` show a blank column
for exactly the rooms an operator cannot otherwise tell apart, so the
description fills it instead. It is a display value and nothing keys on it.

**Boot and recreation resolve by `room_id`, never by the persisted name** — a
name freed by a rename can be reused by a different room, and resolving by name
would bind an existing session to the wrong one. This is what makes the field
safe to hold a description rather than a platform identifier.

Labels join with `:`, and the joiner is what makes the handle injective
(review of the implementation PR found the original `-` joiner was not:
connector `rc` + room `home-general` and connector `rc-home` + room `general`
both derived `rc-home-general`). Two rules hold the boundary: a connector name
may not contain `:` (refused at config load), and `:` is outside the label
encoder's safe set, so a literal `:` in a room or user name is percent-encoded
— the first `:` in a handle always ends the connector component, and the
`dm:`/`gdm:` kind prefixes cannot be forged by a room name. On Mattermost a
channel name is unique only within a team (§6.3), so the label half is unique
exactly because one connector serves one team; the room *name* is really
`(team, channel)` even though only the channel part appears in the label.

Because the label is cosmetic (below), a collision would produce two
identical-looking rows and nothing worse. The reason §4.5 still forbids a
connector spanning teams is not the label — it is that a shared bot account
duplicates *watchers*, which is a correctness problem independent of naming.

#### Filesystem paths key on room_id, not on the display name

Today the derived watcher name is also a path component in two places —
`RUNTIME_DIR/system-prompts/<name>.md` and
`{working_directory}/.acg-attachments/<name>` — which makes the display name
load-bearing and every change to it destructive: the old file and symlink are
orphaned, and a collision repoints one room's attachment path at another's
files.

**Both key on a derived digest, not on the raw `room_id`** (`paths.room_path_key`,
`paths.watcher_prompt_key` — done). The display name is then purely cosmetic: free to change, free to be ugly, free to be
absent. This costs a little debugging convenience — a directory listing no
longer reads as room names — which `list` offsets by showing both.

"Derived" rather than raw matters, because `room_id` is **external connector
data** and nothing constrains it to a single safe path segment. Today's two
platforms emit opaque alphanumeric ids, but a future connector — or a corrupted
state file — could supply `/`, `..`, a leading dash, or something absurdly long,
any of which escapes or collides inside the prompt and attachment roots. So the
path component is a fixed-width encoding of `(connector, room_id)` — a hash,
which is uniform, injective in practice, and safe by construction — with the
raw id kept only in state and in `list` output.

Two further requirements that follow from treating these as untrusted paths:
**validate containment** after constructing any such path (resolve it and
assert it is still under the intended root), and **define symlink handling
before deletion** during expiry, so reclaiming an attachment workspace cannot
follow a link out of the tree.

The containment requirement has a second implementation site the expiry
increment added: **the attachment cache directory itself** —
`attachment_cache_dir` on both connectors joins a *server-supplied* room id
under the cache base, and expiry deletes the directory it names. A
character-class sanitize alone is not the fence, because dots are legal
filename characters and `..` passes it; `resolve_under` is, and an id it
refuses means no attachment caching for that room rather than an escape.

The reason this is worth doing rather than tolerating is that *three separate
things* all want to change the display name, and only this decoupling makes
all three harmless at once: a channel rename, a group DM's membership
changing (§2.7), and a re-derivation from an improved sanitizer.

#### Name derivation changes to percent-encoding

The current sanitizer collapses anything outside `[A-Za-z0-9._-]` to `-` and
raises when the result is empty. Replace with: percent-encode anything outside
that set; never raise; if the result exceeds a length cap, truncate and append
a short `room_id` hash.

With paths keyed on `room_id` the length cap is no longer a correctness
requirement, but it stays as a sanity bound — a display name should not be
several hundred characters wide in a table.

#### What the label is, per room kind

A channel has a name; the DM kinds do not, and group DMs have nothing usable
at all (§6.4). The label is therefore derived per kind, and only the group case
gives up on readability:

| Room kind | Label | Stable? |
|---|---|---|
| channel / private group | the channel's URL name (Mattermost `name`, Rocket.Chat `name`) — never the display name, which admits any character and need not be unique (owner, 2026-09-03) | until its URL name is changed |
| 1:1 DM | `dm:<counterpart>` | until the counterpart is renamed — and see below |
| group DM | `gdm:<16 hex of a room_id hash>` | yes, by construction |

**A renamed counterpart is a known inconsistency, deliberately left.** This
table used to claim a username was stable. It is not — Rocket.Chat allows a
rename, the room id does not change with it, and the row now says so. A channel
rename is followed on the next frame (above); a username is not on the frame,
so it comes from an `im.members` lookup cached per room, and a DM's label keeps
the counterpart's name as of creation.

What that costs is smaller than it first looks, and the reason is worth stating
because it is the part a reader would get wrong. Nothing binds to the name: a
watcher is keyed `(connector, room_id)`, the state record caches the resolved
`room_id`, and `participants` is explicitly not part of any key (§6.4). Nor does
a restart re-derive the label — recreation reads the **materialized config
persisted in the state record** (§2.4), so a rename cannot split a watcher's
session, its watermark or its idle clock: the record is found by room id and
its handle is then refreshed from the next frame. For the DM kinds the stale
name is visible only where a name is *derived*: a watcher created after the
rename — first contact, or a recreation after expiry. Within one process the
cache can make even that fresh creation use the old name.

So the defect is a label that can lag, not an identity that can break — **and
that is a constraint on the creation path, not merely an observation about it.**
A recreation that re-derived the label from a fresh lookup, rather than reading
the config the state record already holds, would turn this into a rename
silently orphaning a session. Recreation reads the stored config.

The cache stays anyway, and the reason is worth stating rather than implying:
it is what stops a DM that **no rule claims** from calling `im.members` on
every message it ever receives, since an unclaimed room is offered to the router
again each time. Caching user ids instead would not avoid that lookup — the
label needs names, so ids would have to be resolved to names at the same point.

What the verified immutability of DM membership (§6.4) justifies is caching the
**kind**; the names are a snapshot and are documented here as one. For named
rooms the question "what does a rename do to a live watcher's identity" is
answered above: nothing — identity is the room id, and the handle is
recomputed. For DM labels it stays open, because no frame carries the
counterpart's current name.

**Group DMs deliberately do not encode their members in the label.** The
tempting alternative is Mattermost's `channel_display_name`, which is exactly
the member list — but it moves whenever membership does, it includes the bot's
own name, its ordering is unspecified (alphabetical in the one case observed,
but that is not a documented contract), and Rocket.Chat supplies no equivalent
at all, so the two platforms would label the same kind of room by different
rules.

A short hash of `room_id` sidesteps all of that: identical on both platforms,
stable for the life of the room, and short enough to type. The members are
better presented as a **column in `list`** than crammed into an identifier —
they are information about the room, not its name.

So `list` shows the label, the room id, and for DMs the participants:

```
NAME                     ROOM ID                     STATE   PARTICIPANTS
mm-eng:incident-42       r1o6c8a1k3d8icd931qq1n6g4y  active  —
mm-eng:dm:alice          iwihkhk9jpf3tngp14ushkx6pe  idle    @alice
mm-eng:gdm:a3f9c1b2d4e5f607  cib3hjsrgpydtf6tyac7frcu6o  active  @alice, @bob
```

The verbs take the handle. `list` shows the room id and the participants beside
it, so a group DM can be told apart even though its label is a digest, but
`pause`/`resume`/`reset`/`expire` do not accept a room id.

**The participants column is not decoration; it is how a group DM is
identified.** An opaque label is only acceptable because something else in the
same view answers "which group is this". Removing that column later as
redundant would leave group DMs genuinely unidentifiable, so it belongs in the
minimum `list` output rather than behind a verbose flag. A substring filter on
`list` (find the group containing `alice`) is the natural companion and cheap
to add.

**Second, independent route: ask the agent in the room.** The durable identity
header supplies `**Watcher name:**` to the agent on every turn, so an agent in
a group DM can simply be asked what its watcher name is. This is not a
workaround — it is the agent reading its own injected identity, and it stays
correct even if the label changes.

That route only works if the header is meaningful, which forces one detail:
**for a group DM, the header's room line carries the participants, not the
label.** A materialized config needs *something* in its `room` field and a
group DM has no name, so the choice is between the hash and the member list.
The member list is strictly better here — it is what makes the agent's own
sense of place accurate, and it is what makes the answer useful when an
operator asks. The hash remains the label; the header describes the room.

**The header should source that description from the state record, not the
frozen config; today it renders the frozen `config.room`** (the one rewrite is
`MessageProcessor.rename`, on a named-room rename). The materialized config is a
snapshot taken at creation (§2.4), so a
group DM whose membership later changes would keep announcing its original
members forever. `state.participants` is *intended* to be refreshed from
inbound messages, so reading the header from there keeps the agent's stated
sense of place true — which matters precisely because that header is also the
"ask the agent which watcher this is" affordance. The frozen copy in the
config stays as the drift baseline it exists to be; it is not what gets shown.

> **Refresh deferred** (issue #124): the first implementation freezes
> `state.participants` at creation too, because the message path carries no
> participant set to refresh from — adding that carrier is its own change.
> Until then the header and `list` show the creation-time members; nothing
> keys on participants (above), so the staleness is display-only.

One consequence worth stating: a Mattermost group DM's `channel_name` is itself
a stable hash, so it *could* serve as the label. It is not used, because it is
40 characters and Rocket.Chat has no counterpart — deriving the label from
`room_id` on both platforms keeps one rule instead of two.

Both changes above are forward-looking rather than fixes for a present fault.
Both connectors derive room names from platform *slugs*, and both slug
character sets already sit inside the safe set — Mattermost's are lowercase
alphanumeric plus `-`/`_`, Rocket.Chat's default validation is
`[0-9a-zA-Z-_.]+` — so on those two platforms the encoder is the identity
function. It is exercised today by voice rooms, whose names come from URL paths
(`/ask/a/b` → `a/b`); a collision truncates and digests rather than raising.

Note how much the decoupling reduces the stakes. Before it, a label collision
meant one session serving two rooms, a system prompt naming the wrong room,
and an attachment path resolving into another room's files. After it, the same
collision produces two identical-looking rows in `list` and nothing else —
the bindings, the paths and the sessions are all keyed on `room_id` and remain
distinct. That is the difference between a data-leak class and a cosmetic one,
which is why it is worth doing even though the trigger is not yet reachable.

### 2.4 Sticky binding and materialization

Once a watcher exists for `(connector, room_id)` it stays bound to that key
until it expires. **Between reconciliations, editing or deleting the rule that
created it does not rebind or destroy it**: recreation after an idle drop uses
the watcher's own persisted config, not the current rule. A **reconciliation**
— every boot, and every `config reload` — re-runs first-match over the current
ordered rule list for every record and brings the record up to date (see
"Reconciliation" below); until the next one, the record is authoritative.

**That persisted config is a materialized per-watcher config, not a copy of
the rule.** At creation the rule is copied and two fields are overwritten
before anything is persisted:

- `name` → the derived watcher label
- `room` → a **concrete room description**, never the pattern: the channel
  name for a channel, the counterpart for a 1:1 DM, and the participant list
  for a group DM, which has no name of its own (§2.3)

This matters because `WatcherConfig.room` is consumed as a concrete room in
at least five places, and the most damaging is the durable identity header
(`- **Room:** {wc.room}`), which is delivered as an appended system prompt
specifically so it survives compaction and is re-supplied on every turn. A
rule-shaped config would permanently tell an agent its room is `eng-*`. The
others: room resolution on the creation path and in `fetch-history`, the
backend session title, the reported room in `list`, and the scheduler's
label fallback. Worth an assertion and a round-trip test that a persisted
config's `room` never contains pattern metacharacters.

Note the split of duties this creates, deliberately: the **label** is a stable
handle for addressing a watcher, while `room` is a human-meaningful
*description* of where it lives. For a channel they coincide. For a group DM
they diverge — label `gdm:a3f9c1b2…`, room `@alice, @bob` — and the resolution
paths that consume `room` must therefore not treat it as a lookup key. Room
resolution already goes by `room_id` (§2.3), so the only requirement is that
nothing regresses to resolving by this field.

#### `WatcherConfig.session_id` is removed

A rule cannot carry a pinned session id: provisioning gives one absolute
priority and returns it unconditionally, so a rule holding one would hand
*every room it matches the same session* — violating R4 at config level rather
than through a race.

But once every watcher is rule-derived, nothing can populate the field at all,
so it is **removed entirely** rather than merely rejected. That deletes three
things: the field itself, the priority-1 branch in `_provision_session`, and
`reset_watcher`'s pinned-session handling. Leaving them would mean carrying
branches that nothing can reach and a future reader cannot tell are dead.

`session_id` in `config.yaml` fails to load as an unknown key — a removed field
does not earn its own rejection path; the replacement is documented here, not in
the error.
**The replacement is a handoff, not a pin**: have the agent summarise its
session to a file and read that file back in the next one. That is strictly
more robust than pinning — it survives the backend expiring the session, which
pinning never did (§3, backend retention).

Note precisely which id disappears. `WatcherConfig.session_id` — the
*config-pinned* one — is gone. `WatcherState.session_id` — the id ACG assigns
at provisioning and persists so a room can resume — is untouched and remains
central to the whole idle/expiry model (§2.5). Conflating the two would be an
easy and damaging mistake.

What sticky binding buys:

- A rule edit cannot take a room from a running or paused watcher, so no
  second watcher can appear in a room whose pause an operator set.
- Agent drift becomes detectable. Because the watcher's own config records
  its agent, editing a rule's `agent:` cannot silently re-point a dormant
  session at a different backend — which would otherwise hand a session id
  created by one backend to another, since provisioning returns the
  persisted id and agent resolution only warns on an unknown name.

#### Two records, not one

A watcher persists **both** forms, because they answer different questions:

| Field | Content | Used for |
|---|---|---|
| `config` | materialized: concrete `room`, derived `name` | recreating the watcher |
| `rule_name` | the rule's operator-supplied identity | finding "my rule" in a later config |
| `rule` | the rule **as resolved at creation**, unmaterialized | detecting that the rule has since changed |

The materialized config cannot serve as the drift baseline: its `name` and
`room` are overwritten by construction, so diffing it against a rule would
report those two fields as changed on every comparison.

**Store the rule in its resolved form, after `inherits:` has been applied.**
Template inheritance is flattened at parse time — a parsed rule carries no
`inherits` field — so the resolved form is what a parser naturally produces,
and storing it means **an edit to a watcher template is caught as drift for
free**. Storing the raw YAML entry instead would let template changes escape
detection entirely.

A content hash of the resolved rule is sufficient for "has anything
changed?" and is worth deriving for cheap equality checks, but the full
content is what allows showing an operator *what* changed, or applying a
change field-by-field.

This is what reconciliation reads. Two facts shaped it:

- **Content drift and ownership drift are different checks.** Under
  first-match precedence a rule inserted *above* mine begins winning for my
  room without any rule's content changing. Detecting that requires
  re-running the match against the current ordered rule list, not a diff.
  A rule-content diff alone would miss it — so reconciliation re-matches
  every record and uses the snapshot digest only to decide whether a
  same-named winner is a change at all.
- **The agent's own definition is outside the rule.** A rule names
  `agent: X`, but X's `working_directory`, backend and permission settings
  can all change without the rule changing. Reconciliation does not decide
  that: it rewrites the record's `agent`, and the backend-identity check at
  the next session provisioning decides whether the stored session is reused
  or abandoned with its id logged — one place deciding session reuse.

**Reconciliation** (`gateway/core/reconcile.py`, run by the session manager's
`settle_records` right after hydration at boot — **before the connector
connects and before the identity barrier**, so everything that consumes
records afterwards, the DM-claim check included, sees the fleet as the current
config describes it): for every rule-derived record, re-run first-match for
its room against the connector's current ordered rules. It does not need the
connector to be connected: the plan is pure, re-materialization is an in-memory
rewrite plus a save, and an expiry talks only to the agent backend (started
earlier) — its connector step, unsubscribing, is a no-op for a room nothing has
subscribed yet.

- The same rule wins and its snapshot digest is unchanged → **keep**.
- A rule wins whose name or content differs from what the record froze →
  **re-materialize**: the rule-derived frozen fields (`config`, `rule`,
  `rule_name`, `connector`, `agent`, `config_schema_version`) are rewritten
  from the winning rule through the same `materialize` creation uses. The
  record object stays: `room_kind`, `participants` and `created_at` describe
  the room and the birth, not the rule; session-scoped fields (`session_id`,
  `paused`, the watermark) and the lifecycle clocks are untouched. Same room,
  same session, same idle clock — a paused record is re-materialized paused.
- No rule wins → **expire**, through the removal path's shared tail (jobs
  cancelled, with a reason that says so), with the session id on the AUDIT
  line every released session gets (`session_release.py`). A rule cannot name
  an agent that does not exist — config loading refuses it — so a deleted
  agent reaches a record only through a rule edit, i.e. re-materialization.
- A record that cannot be re-matched honestly is **kept**, with the reason on
  a warning: no room kind recorded, a room kind this build does not know, a
  named room with no name (`unmatchable_reason`). The runtime degrades each
  of these to something workable; a decision that can expire must not.
- The keep decision also requires the record's `config_schema_version` to be
  current, so a build that changes what a config field means rewrites every
  record once, rule change or not — that is what the per-record marker is for.
- A state file whose connector is no longer in `config.yaml` is reconciled
  too: the file is removed, then one AUDIT line per record — announced only
  once the removal happened; a file with a record that did not parse is kept
  for repair by hand. Nothing would ever open it again.

Reconciliation matches the room **as the record describes it** — the name the
record carries, which the inbound stream refreshes on every frame
(`observe_room_name`). A room renamed while the daemon was down is therefore
matched under its old name at this boot; the first frame teaches the new name,
and the next reconciliation (the next boot, or a `config reload`) applies the
rules to it. That is the same "sticky between reconciliations" rule as for
rule edits, and it keeps this pass free of connector calls.

Boot has no operator to ask, so it applies the plan and logs it — one summary
line, plus one line per record that changed. The preview is `config reload
--dry-run`, which renders the same plan without acting (with the daemon
stopped it is computed from the state files and IS the next boot's plan —
`docs/design/config-reload-design.md`).
This is affordable because a lost session is recoverable in practice: the
platform keeps every message in the room, and history handoff re-feeds recent
context on the next session. Whether the *backend* still has the session is
backend-dependent (reclamation asks `delete_session`; OpenCode honours it,
Claude does not support it), which is why the id is always logged — together
with the backend identity it was provisioned against (`identity=`), which is
what says where to look for it; the record's current agent name may already
have moved on.

One part of that is **not** deferrable, because it is about resuming a session
rather than detecting drift. A record stores the agent *name* and a
`session_id`, and recreation resolves whatever `AgentConfig` that name means
now. If the backend type or working directory changed while the record was idle
— or across a restart — the stored id is replayed into a different backend and a
different session store. That either silently loses continuity or, worse,
matches an unrelated session that happens to carry the same id.

So the record must also persist the **resolved backend identity** it was
created against — backend type plus the working directory that scopes its
session store — and compare it before resuming. On a mismatch the stored
session is not reused: the watcher starts a fresh session, logged as an
explicit reason rather than appearing as inexplicable amnesia. Storing the name
alone is what makes sticky binding (§2.4) a weaker guarantee than it reads.

At scale the same rule content is duplicated across every room it matched.
For the expected range — tens to low hundreds of rooms — that is negligible
and simpler than the alternative. If it ever matters, normalize into a
rules table keyed by `(rule_name, content_hash)` with watchers referencing
it rather than embedding it.

### 2.5 Lifecycle

| State | In memory | On disk | Entered by |
|---|---|---|---|
| **active** | processor + session | record | first message in a matching room; or recreation from an idle record |
| **idle** | nothing | record, incl. session id, watermark, `dropped_at` | no activity for `session_idle_days`; or the bot being added to a matching room (§2.7) |
| **failed** | nothing | record, no `dropped_at` | a start that left a record behind and did not finish — see below for exactly which failures those are |
| **expired** | nothing | nothing (logged) | idle for `session_expire_days`; or the bot being removed from the room |
| **paused** | nothing | record, `paused=True` | operator only |

A watcher can therefore exist as a record before it has ever run — which is
what makes a newly-joined room listable and pausable before its first
message.

#### Defaults, and what a long outage does to them

`session_idle_days: 15` and `session_expire_days: 15` — a month from last
activity to reclamation. Chosen to sit in the range agent backends retain
sessions for anyway, so the expectation an operator already has is the one this
meets.

Both timers are wall-clock and know nothing about whether ACG was running, so a
daemon that is down for a week returns to find every record a week older. What
keeps that survivable is **where each timer starts**, and the two halves of the
fleet are protected differently.

**Expiry is measured from the moment a watcher becomes idle — not from its last
activity.** `dropped_at` is written when the sweep drops the watcher, so for a
watcher that was *active* when the daemon stopped, the expiry clock begins at
the first sweep after it starts and the outage is not counted at all.
`active → expired` therefore cannot happen through an outage of any length.

This requires one rule of the implementation, because the naive version breaks
it: **a sweep advances a watcher by at most one state.** Deriving the final
state from the timestamps and acting on it would take a watcher that was busy
right up to the shutdown from `active` straight to `expired` in a single pass,
deleting the session the `dropped_at` reset exists to protect. One step per
sweep costs nothing and is what makes the guarantee above true.

**A watcher that was already idle when the daemon stopped is not protected by
that**, and this is accepted rather than solved. Its `dropped_at` predates the
outage, and `idle → expired` is itself a single transition, so nothing but the
size of the window helps: an outage longer than `session_expire_days` puts its
session at risk. Three reasons this is a decision and not a gap:

* **The case is uncommon.** With the defaults it takes more than two weeks of
  continuous downtime, and a session nobody has touched in the month that
  precedes it is one it is reasonable to let go.
* **It has an operator remedy that needs no code.** A deployment that expects
  long outages raises `session_expire_days`; the setting is per rule, so it can
  be raised only where the sessions are worth keeping. Note the prerequisite:
  a watcher reads the **frozen** rule it was created against (below), so the
  raised value reaches existing watchers at the next reconciliation — a
  `config reload` or the next start — and watchers created after the change
  read it at once. That suits the case — an operator who knows their
  environment has long outages sets it up front — but it is prophylactic, not
  a repair applied after the fact.
* **The automatic version is not cheap.** Crediting the outage back means
  persisting a heartbeat and subtracting the downtime when a TTL is evaluated —
  and a single global downtime figure does not compose across repeated
  restarts, since it remembers only the last one. A correct version needs
  either a per-record accumulator or a boot-time mutation of
  `last_activity_at`, and the second makes the record claim activity at a
  moment there was none.

Support for the outage case can be added later if a deployment shows it is
needed. Nothing here is a one-way door: every §5.3 field defaults, so an
accumulator is additive, and a heartbeat is a new file rather than a schema
change.

#### The lifecycle timers read the frozen rule, always

A watcher evaluates its TTLs against the rule snapshot frozen at creation
(§2.4) — never against the current `config.yaml`. There is deliberately no
exception for "operational" fields:

**One rule with a carve-out is a rule applied inconsistently.** "Identity comes
from the frozen copy, policy from the current one" reads well and is a
maintenance trap: every reader then has to know which half it is holding, and
the answer for a room whose rule has since been *deleted* is a third case
again. A watcher that always reads its own snapshot needs none of that.

**It also gives the frozen copy a consumer.** §2.4 stores the resolved rule so
drift can be detected, and drift detection has had no caller: the snapshot has
been groundwork waiting for one. Updating a live watcher's rule becomes an
explicit step — diff the current rule against the frozen one and apply the
difference — rather than a value that silently changes meaning on the next
sweep. An operator can then see what a config change did, which the silent
version cannot offer.

So reconciliation (§2.4) owns rule updates — at boot, and at `config
reload` — and this section owns only the reading: **the sweep reads what the
record carries.** Between reconciliations a rule edit reaches an existing
watcher only by the blunt route — expire the watcher, let the next message
recreate it against the current rule.

**One ordering constraint is not optional, whatever the arithmetic does:** the
first expiry sweep must not run until the startup replay (§2.2) has finished.
Expiry is the destructive step — it deletes the record recreation reads from —
and the replay's whole job is reviving rooms that have messages waiting. A
sweep that runs first reclaims exactly the rooms the replay was about to wake,
irreversibly.

**A related question the lifecycle increment has to answer**, and it is not
settled by "boot recreates active records only" below: a record that was
`active` at shutdown has no `dropped_at`, so between the start and whatever
processes it, it is neither resident nor dropped — and `lifecycle_state` reads
that as **`failed`**, which is in the default `list` view. What it must not do
is leave `list` reporting a healthy fleet as broken after every restart.

#### `failed`: a record that wants to be resident and is not

A start writes its record partway through, before the room subscription and the
processor, so that a subscription failure does not lose the session id or the
injection flag. The consequence is a record that is **not paused, not dropped,
and not running**, and calling that `active` tells
an operator the opposite of what happened. This design previously noticed the
same problem from the other side, while arguing that `dropped_at` cannot be
inferred from `room_id`: *"the subscribe-failure rollback deliberately keeps a
record with `room_id` populated, so a start failure is indistinguishable from a
healthy record by that field."* It is a fifth state, and naming it is cheaper
than the operator confusion of not naming it.

**Which failures reach it, precisely.** "After the record is written" is *not*
the dividing line, and stating it that way was wrong: three of the four rollback
paths delete the record again, deliberately, so that a half-built one cannot be
resumed as though it were whole. Only these produce `failed`:

| Failure | Effect on the record |
|---|---|
| room subscription, or the room claim | kept on purpose — session id and injection flag survive the next attempt |
| session-map bind, context injection, attachment workspace | deleted by their own rollbacks |
| room resolution, session creation, an unavailable agent | never written |

**But the rollbacks only remove what *this* start added.** A watcher that ran on
an earlier boot has a record on disk, and `merged_view` keeps it, so the same
fault shows as `failed` where a record already existed and as no row at all on a
first-ever start. That is why the operator-facing rule has to be stated as the
invariant — **a row exists exactly when a record does** — rather than as a table
of failures: the table is true only of first starts, and an operator has no way
to know which they are looking at.

**It is derived, not stored.** The record says what the gateway *wants* — a
watcher should be resident for this room; residency says what *is*. `failed` is
simply the two disagreeing:

```
paused?        → paused      (an operator's explicit decision outranks everything, §4.4)
dropped_at?    → idle        (the manager released it on purpose)
not resident?  → failed      (wanted resident, is not)
otherwise      → active
```

A stored `failed` flag would need a writer *and* a clearer, and every path that
forgot the clearer would leave a record permanently lying about itself — the
failure mode this project has repeatedly shipped. A derived one is correct by
construction: the next successful start makes a processor exist, and the state
changes with no one having to remember to change it.

The order matters and is not arbitrary. `idle` is checked before residency
because an idle record is *supposed* to have no processor; reversing the two
would report every idle watcher as failed.

**Residency is an input, not a lookup.** The predicate takes the record and a
boolean, so it stays a pure function that a tool reading state files can call —
it simply passes `False` and gets the honest answer for a process that is not
running any of them.

**Residency has to include a transition in flight, and that is not a nicety.**
`pause` and `reset` both remove the processor *first* and settle the record
*last*, so between the two the record is not paused, not dropped and not
resident — indistinguishable from a failed start. `pause` holds that for the
drain; **`reset` holds it for a fresh session, a history fetch and a full model
turn**, bounded by the agent timeout and defaulting to minutes. Reporting
`failed` there would have the recovery verb accuse itself while working, and
send an operator to a startup log with nothing in it. So residency is *a
processor is registered **or** a lifecycle transition is in flight*, and the
per-watcher lock is exactly that span — including being released on failure, so
a genuinely failed start reports `failed` the moment it gives up. Erring toward
`active` for a few seconds is self-correcting; erring toward `failed` is not.

**The remaining window is unreachable.** Between process start and watcher
restore nothing is resident, so every record would read `failed`. `list`
arrives over the control socket, and the socket is opened after the restore
phase completes (verified: `GatewayService` runs `sync_only()` for every entry
and only then calls `ControlServer.start()`), so no caller can observe it.

##### Retry policy: every start, and no memory of having given up

A failed watcher is retried **on every daemon start**, which is what
`sync_watchers` already does — the record is on disk, boot reads it, and the
start is attempted again. If the underlying problem is unfixed it fails again
and the state reports `failed` again.

That repetition is the point. The alternative — remembering the failure and
suppressing future attempts — converts a problem the operator can fix into one
the system has silently decided to live with, and it needs a persisted marker
whose staleness is a second bug. Under rule-derived creation the same reasoning
gives the per-room park a deliberately short memory: a resolve that keeps
failing backs off and parks **in process only** (§2.7), and a restart clears the
park and tries again.

**One qualification, ruled during the idle-tick increment's review: a failed
record whose room has also been quiet past its idle TTL converts to `idle` at
boot instead of being retried.** The boot evaluation cannot tell was-active
from failed — the rollback table above is why no field distinguishes them —
and it judges both by the same clock. The conversion is deliberate rather than
an accident of that blindness: reviving a room nobody has spoken in for
`session_idle_days` just to have it sit resident is the resume cost the boot
evaluation exists to avoid paying, and the record's next message or scheduled
injection retries the start through the wake anyway. What it costs is
visibility — `idle` is outside the default `list` view, so a broken watcher
whose room went quiet stops being reported after the first restart past its
TTL. Accepted because the staleness bounds the harm: the failure is at least
`session_idle_days` old, nobody has been affected by it in all that time, and
the wake retries it the moment somebody would be. Retry-on-every-start remains
the rule for every failed record *inside* its idle TTL — the case where
someone is plausibly waiting.

##### `failed` is in the default `list` view

`StateFilter.OPERABLE` is `active | paused | failed`. Of the states an operator
can act on, this is the one they most need to see: it is the only one that
means something is wrong. Excluding it would reproduce, one level up, the defect
this whole section exists to remove — a broken watcher that looks fine.

##### What the verbs do to it

* `resume` and `reset` retry the start, which is the in-place recovery for a
  fault outside the gateway — a room that had gone, a server that was down.
  **Neither can recover an unavailable agent.** Availability is decided once, at
  boot, and both verbs refuse fail-closed on it rather than starting a watcher
  with no permission broker. Restarting the daemon is the only thing that
  re-evaluates it, and operator-facing text has to say so rather than offering
  `resume` as the general answer.
* `pause` mutes it, and the resulting `paused=True` is what stops boot from
  retrying — the honest way to say "stop trying this one for now", as against
  the system deciding that for itself.
* `get` (§2.8) treats it as it treats idle: a record that is not resident is
  recreated on demand. A start that keeps failing therefore surfaces as a
  failure to the caller rather than as a silently unrouted room.

This is also what lets *Boot is serial, deliberately* (below) settle its own
open question: a watcher failing at boot can simply be skipped, because the
state it leaves is now named rather than indistinguishable from a healthy one.

**Paused is a real state and is never reclaimed by a timer** — not idled, not
expired. Once config no longer names rooms, pause is the only durable way to
mute one, so letting an idle clock expire a paused record would erase an
explicit instruction. For the same reason, `reset` must not silently clear
`paused`.

The one thing that *does* reclaim a paused record is the platform reporting the
bot has been removed from the room — an authoritative fact rather than an
inference from inactivity (§2.7, §4.4).

**A room the gateway has never seen cannot be paused.** Pause acts on a
record, and an unobserved room has none — no id, no kind, nothing to key on.
The request is rejected with a message pointing at the rule's `except_for:` list,
which is where "never engage with this room" belongs: declarative, effective
before the first message rather than after it, and not dependent on the room
having been observed. (Today's behaviour is to fabricate an empty record for
such a name; §5.3 covers retiring those.)

**Resume returns a paused watcher to active and restarts its clock.**
`last_activity_at` is set at the moment of resume, so a watcher paused for
longer than `session_expire_days` does not expire the instant it comes
back — it gets a full idle period like any other active watcher. Stating it
because the alternative reading (pause accrues idle time invisibly, and
resume hands back something already due for reclamation) is a plausible
misimplementation of the same table.

**Idleness needs its own clock.** The existing `last_processed_ts` is a
message-dedup watermark with different semantics and cannot serve. A
gateway-owned `last_activity_at` is written by both the inbound path **and**
scheduled injection, or scheduled-only rooms idle out between fires. It must
also account for in-flight agent turns and outstanding permission requests,
so an idle drop cannot cancel an approval an operator is still reading.

**Boot recreates active records only**, which is what `dropped_at`
distinguishes — without it, boot cannot tell "was active, recreate" from
"was idle, leave dormant" and would recreate every room ever matched,
discarding the idle savings on every restart. `room_id` cannot substitute:
the subscribe-failure rollback deliberately keeps a record with `room_id`
populated, so a start failure is indistinguishable from a healthy record by
that field.

**Boot is serial, deliberately.** The cost asymmetry that makes this
acceptable: history handoff only runs for a newly created session, so
recreating a watcher that resumes an existing session skips both the history
fetch and the full model turn that delivers it.

| | new room (new session) | recreate (resume) |
|---|---|---|
| resolve room | 1 REST call | same |
| create session | 1–5s | resume, cheap |
| fetch history | 1 REST call | skipped |
| deliver history | **full model turn, 5–30s** | skipped |
| context, workspace, subscribe | ~1s | same |
| **total** | **~10–40s** | **~1–3s** |

Boot's common case is therefore the cheap one. Parallelising is a small
change when wanted — each start is independent and takes its own per-room
lock — which is the argument for deferring it rather than pre-building it.
One watcher failing **skips that watcher** and boot continues; the partial
state it leaves is no longer something to avoid, because it now has a name
(`failed`, above) and is visible in `list`.

**Expiry reclaims everything**, or it leaves bookkeeping behind: the state
record, the backend session, injector retry state, session maps, the
system-prompt file, the attachment symlink and the attachment cache
directory. A failed backend session delete logs once and accepts the leak
rather than refusing to expire.

### 2.6 Connector-declared capability

Each connector declares whether its transport delivers **unsolicited
inbound** messages. Idle eligibility, eager-versus-lazy creation and
black-hole behaviour all derive from that one property rather than from
per-connector branching.

| Connector | Unsolicited inbound | Creation | Idle / expiry | Membership events |
|---|---|---|---|---|
| Mattermost | yes | lazy, on first message | eligible | yes (§2.7) |
| RocketChat | yes, via subscribe-all | lazy, on first message | eligible | yes (§2.7) |
| Script | **no** | eager | **never** | n/a |
| Voice | **no** | eager | **never** | n/a |

Membership events are a separate, optional capability: they register a record
early but never start a watcher, so a connector without them behaves
identically once the first message arrives.

**Mattermost** already receives, on one socket, every channel the bot is a
member of — and *only* those, verified in §6.2. The connector currently
discards events for channels it has no state for; that discard becomes the
routing hook. Because delivery follows membership, no membership gate is
needed, and the event itself carries channel name, type and team id, so
routing needs no REST lookup for ordinary channels.

**RocketChat** can receive messages for rooms it has not per-room-subscribed
to, using `stream-room-messages` with the reserved room id
`__my_messages__`. Verified working in §6.1. The server emits every message
to such subscribers, filters per message via an access check, and attaches a
`roomParticipant` flag distinguishing membership from mere readability — here
the gate *is* required, precisely because subscribe-all also delivers public
channels the account can only read. The access object also carries room type
and name, so ordinary channels need no REST lookup either; direct messages
are the exception, since they have no name to carry. Reaching this requires
real work in the RC connector (§5.2) — most of all splitting a key space that
subscribe-all otherwise collapses onto a single entry.

**Script and Voice have no inbound stream to discover from.** Script's
messages arrive through direct injection that bypasses the connector
entirely; Voice's rooms arrive as HTTP path segments. Both therefore require
**literal** `rooms.include` entries — no patterns — enforced at config load
for any connector declaring no unsolicited inbound. This keeps eager
creation possible (a concrete room is known) and turns an otherwise silent
misconfiguration into a load error.

Voice's path segment is also its only agent selector, which is why multiple
rules per connector is load-bearing rather than a convenience: two rules
with literal room patterns on one voice connector serve two agents on one
port. Voice additionally needs default-deny on unknown rooms — a typo'd path
must not spawn a fresh context-less session — and its unmatched-room reply
must say no route is configured rather than reporting backpressure. *(Not yet
done: voice answers `_BUSY_REPLY` for an unrouted room; default-deny holds.)*

### 2.7 Creation path

Ordering, which is where the cost and the correctness both live:

1. **Cheap synchronous rejects, above the room-state lookup**: system
   messages, own-message echoes, and the sender allow-list. These filters are
   synchronous and need no room metadata; run them with no turn store so the
   precheck does not consume the agent-chain budget.

   The **mention gate is deliberately not in this step.** It is
   kind-dependent — `require_mention` does not apply to a 1:1 DM but *does*
   apply to a group DM (§6.4) — so running it before the kind is known would
   accept an unmentioned group-DM message as though the room were a 1:1, and
   the agent would answer every message in that group. The gate therefore runs
   **after classification**, uniformly on both connectors: the kind is free on
   Mattermost and only needs a lookup for Rocket.Chat type `d`, so this costs
   nothing on Mattermost and is the only correct order on Rocket.Chat.
   Deferring it does not spend the agent-chain budget either, since
   classification still precedes any model turn.

   **Plan of record (owner, 2026-09-02): an unmentioned message from an
   allowed sender still creates or wakes the room's watcher.** Under
   `require_mention: true` the message is then rejected by the tracked-path
   filter, but the processor has been started (and an idle record's
   `dropped_at` cleared), so ordinary chatter in a room the bot was added to
   keeps its watcher warm and defers its expiry. This is accepted, not an
   oversight: dynamic watchers exist to remove per-room configuration, not to
   save resources, and a warm watcher answers the first real mention without a
   start-up delay. A room where the bot is never addressed is a room the bot
   should not have been added to. Revisit only if a concrete deployment shows
   the cost matters.
2. **Membership and scope gate**: on RocketChat, the `roomParticipant` flag
   the server computes per message — required, since subscribe-all also
   delivers public channels the account can merely read. On Mattermost no
   membership gate is needed: delivery *is* the membership signal, verified
   rather than assumed (§6.2). Mattermost does need a **team** gate, since one
   connector serves one team while the socket spans every team the account
   belongs to — and that gate must pass rooms with **no** team (DMs) through
   rather than rejecting them, or enabling it silently disables DM support
   (§6.3).
3. **Creation runs off the connector's handler path**, with the triggering
   message held in a **bounded** per-room buffer and replayed into the new
   processor. Creation is expensive — room resolution, session creation, a
   history fetch, a full model turn, context injection, filesystem setup —
   and Mattermost holds a connector-wide permit for the whole handler call,
   so synchronous creation stalls delivery for every channel once enough
   rooms start at once.
4. **A per-room single-flight guard is required at this step**, because
   step 3 removes the serialisation that was implicitly providing one:
   Mattermost's per-channel worker drains sequentially today, so two
   back-to-back messages for one new room cannot both trigger creation.
   Once creation is not awaited, they can — and the failure is silent,
   because the dispatcher registers processors in a list and fans out to
   all of them, so two processors in one room means two agents answering
   every message. The lock is keyed like the watcher, taken by the manager,
   and covers the existence check and the creation together.
5. **Register the processor before any capacity preflight is evaluated.**
   The preflight reports "no capacity" when no processor exists, so
   evaluating it first would reject the first message from every new room —
   and post a "server busy" notice into the room from a completely idle
   gateway. The preflight should also distinguish *empty* from *full*, so a
   routing miss is never reported as backpressure.
6. **Run the trigger through the reserve/commit transaction in §2.2.** The
   creation path is the abort-heavy branch of it: ids are currently registered
   before the handler and rolled back only on a negative return, never on an
   exception, and since a brand-new room has an empty watermark, reconnect
   replay skips it too — so a creation that throws loses the message with
   nothing visible in the room.
7. **Cap concurrent creations.** Queue depth bounds messages per room;
   nothing bounds rooms being created at once. Over-cap triggers get an
   honest "starting up" response.

Because the trigger is held, its timestamp is known — so **history handoff
fetches messages strictly older than it**. Without that the newest-history
block contains the very message that triggered creation, which is then
delivered twice: once inside a history turn whose response is discarded, and
again as the live prompt. Buffering alone does not fix this.

**No online/offline notification is posted on any path**, including pause and
resume — the `online_notification`/`offline_notification` settings were removed
with the cutover. Otherwise every idle room would announce the agent offline
each idle period and online on each burst.

**DMs are opt-in per rule**, with 1:1 and group DMs distinguished — because
the two behave differently, not merely because they look different.
`require_mention` is skipped whenever a room's type is `dm`, so a group DM
misclassified as a plain DM makes the agent reply to **every** message from
**anyone** in that group chat. That is the cost of getting it wrong, and it is
why `group_direct` is its own opt-in.

Classifying them is asymmetric (§6.4). Mattermost marks a group DM
`channel_type: "G"`, distinct from `"D"`. Rocket.Chat reports **both** as
`roomType: "d"` with no participant information in the frame at all, so
honouring `group_direct` there needs a participant-count lookup the first time
a DM room is seen — cacheable permanently: a DM's member set cannot change
(§6.4), so a different member set is a different room id, never a stale cache.

Two things make DMs structurally unlike channels, both verified in §6.3:

- **A DM has no team**, so the Mattermost team gate cannot classify it.
- **A DM reaches every socket the bot account has open.** There is nothing to
  withhold at the transport: Mattermost has no per-room subscribe on the wire
  at all — `subscribe_room` is pure in-memory bookkeeping with zero I/O — and
  the server pushes account-level events to every session. So two connectors
  sharing an account *both* receive every DM, unavoidably, and the
  de-duplication has to happen in routing.

**DMs cannot be expressed as a name pattern.** Mattermost's DM
`channel_name` is the opaque `<userid>__<userid>` form and Rocket.Chat omits
the room name for DMs entirely, so no `include:` pattern can match one on
either platform. They need a distinct key:

```yaml
connectors:
  - name: mm-eng                     # same bot account, different teams
    type: mattermost
    server_url: https://mm.example.com
    team: eng
    username: acg-bot
  - name: mm-sales
    type: mattermost
    server_url: https://mm.example.com
    team: sales
    username: acg-bot

watcher_rules:
  - name: eng-channels
    connector: mm-eng
    agent: claude-eng
    rooms:
      include: ["eng-*", "incident-*"]
      except_for: ["eng-archive"]

  - name: direct-messages            # exactly one rule per bot account
    connector: mm-eng                # may opt into DMs
    agent: claude-assistant
    rooms:
      direct: true                   # 1:1 DMs. Default false.
      # group_direct: true           # separate opt-in — no stable identity

  - name: sales-channels
    connector: mm-sales
    agent: claude-sales
    rooms:
      include: ["sales-*"]
      # no `direct:` key, so mm-sales never routes a DM
```

**The opt-in is the gate.** Routing classifies the room first, then applies
the gate that fits it:

```
inbound message
├─ DM?  (channel type D/G, or Rocket.Chat room type d)
│    └─ any rule on THIS connector with rooms.direct / group_direct?
│         yes → first such rule
│         no  → drop
└─ team channel?
     └─ event's team == this connector's team?
          no  → drop
          yes → first rule whose include/except_for matches the channel name
```

Classifying before gating avoids a "an empty team id means pass" special
case, which is subtle and easy to regress.

So in the example above `mm-sales` receives every DM on its socket and drops
each one, because none of its rules opts in — no special-case code, just an
absence of a matching rule. The config-load check in §4.5 is therefore not
what makes the normal path correct; it exists to catch the *misconfiguration*
where an operator sets `direct: true` under both connectors of one account,
which routing alone cannot detect since each connector sees only its own
rules.

A DM-only connector is not expressible as an alternative: `team` is a
required field on a Mattermost connector, so DM ownership has to attach to one
of the team connectors.

**`direct` and `include` may be combined in one rule.** A room is either a DM
or a team channel, never both, so the two selectors address disjoint classes
and cannot produce a matching ambiguity. Two consequences follow:

- **Shadowing detection must treat `direct` and `group_direct` as claimable
  classes**, not only patterns (§2.1). Given an earlier rule with
  `direct: true`, a later rule's `direct: true` is dead — the DM class is
  already claimed — and that is exactly as invisible at runtime as a shadowed
  pattern.
- **The single-owner check counts any rule with DMs enabled**, wherever it
  appears (§4.5). Folding `direct: true` into a channel rule for convenience
  still consumes the account's one DM opt-in.

Separate rules are nevertheless the better practice, and the example above
uses them: a DM is one human, a channel is a group, and they usually want a
different agent, different history-handoff behaviour and different TTLs.
That is a recommendation, not a constraint.

#### The DM opt-in is all-or-nothing, and that is the intended scope

`direct: true` admits **every** DM the connector can see. There is no
per-counterpart selection in this design.

What that does *not* mean is "anyone may now talk to the agent". Two existing
connector-level mechanisms, easy to conflate, still apply:

| Mechanism | Config | Decides |
|---|---|---|
| **Admission** | `filter_sender` (defaults to **true**) gating on `allow_senders`, which is `owners + guests` | whether a message is processed at all |
| **Authorization** | `role_of(username)` → `owner` or `guest` | which tools the agent may run on that person's behalf |

So under default config a DM from someone in neither list is rejected at
message level with "sender not in allow-list", and never reaches routing.
Note the second mechanism's fallback: `role_of()` returns `guest` for an
unrecognised username rather than raising, which is what matters when
`filter_sender` is turned **off** — then everyone is admitted and everyone
unlisted is a guest.

The practical consequence: DM enablement is all-or-nothing at the *room*
level, while *who* may use it is already controlled per user by the sender
allow-list. What cannot be expressed today is routing different counterparts
to **different agents** — every admitted DM goes to the one rule that opted
in.

#### Future extension, if per-DM control is ever needed

The chosen shape is deliberately forward-compatible. `direct: true` is
equivalent to a wildcard, so granular control can be added later without
breaking any existing config:

```yaml
# today — all DMs the connector can see, one agent
rooms:
  direct: true

# possible later — equivalent to the above
rooms:
  direct: {include: ["*"]}

# possible later — different agents per counterpart, two rules
- name: dm-support
  rooms: {direct: {include: ["@alice", "@bob"]}}
  agent: claude-support
- name: dm-everyone-else
  rooms: {direct: {include: ["*"], except_for: ["@alice", "@bob"]}}
  agent: claude-general
```

`direct: true` would then be parsed as sugar for `{include: ["*"]}`, so the
boolean form stays valid indefinitely and no migration is needed.

Two things that extension would have to solve, recorded now so the cost is
known rather than discovered:

- **The identity source is asymmetric.** Mattermost supplies the counterpart
  free in `channel_display_name`, but Rocket.Chat's per-message access object
  omits the room name for DMs entirely (§6.1), so matching a pattern there
  needs a REST lookup or a derivation from the sender — and the sender of a DM
  is the counterpart *or* the bot itself, so it does not reliably identify the
  room.
- **A group DM has no name a pattern could usefully match.** Mattermost's
  `channel_display_name` is the member list, which moves as membership changes,
  and Rocket.Chat supplies nothing at all (§6.4). Patterns would work for 1:1
  before they work for `group_direct`, so the extension may well have to land
  in two stages.

Neither is a reason to avoid the extension; both are reasons it is more than a
parsing change, which is why it is not being done speculatively.

#### Membership events: register on join, do not start

Both discovering connectors can tell when the bot is added to a room —
Mattermost via a `user_added` event targeted at the new member, RocketChat
via a subscriptions-changed notification with an inserted action. Without
using it, a bot added to fifty rooms shows nothing in `list` until somebody
talks in each one, and an operator cannot pause a room before its first
message.

Using it to *start* a watcher is the wrong end of the trade, though: it pays
a session and a full history-handoff model turn for every room the bot is
added to, including ones never used. That is the eager cost this design
exists to avoid.

**So a membership-add event creates the record in `idle` state.** The rule is
matched, the watcher is materialized and persisted, and nothing is started.
The room becomes listable and addressable immediately; the first message
wakes it through the normal path. This reuses the existing idle state rather
than inventing a fourth one.

Reusing idle means inheriting its expiry: the registered record's
`dropped_at` is the join instant — being born idle, that *is* the moment it
became idle — so a room nobody ever speaks in is reclaimed
`session_expire_days` after the join. Accepted deliberately (owner,
2026-08-17), and the usual pattern makes it the uncommon case anyway: a bot
is normally added to a room *in order to* be spoken to, and the first message
makes the watcher active. A registration that expires never-spoken had no
session to lose — the record is the only thing reclaimed, and a later first
message recreates it at full fidelity through the same path it would have
taken anyway. Once woken, the join time never participates in any calculation
again; a later quiet spell restamps `dropped_at` and expiry measures from the
new stamp.

Three properties this must have:

- **It is a supplement, never a replacement.** Mattermost's websocket has no
  gap detection or replay on reconnect, so an add event that arrives during a
  disconnect is simply gone. Message-triggered creation stays the safety net,
  which means it must remain correct for a room with no record at all.
- **The rule is snapshotted at join time**, so a rule edited between join and
  first message does not apply to that room (§2.4, sticky binding). This is
  consistent but worth knowing.
- **Removal is symmetric, and it overrides pause.** A membership-remove event
  reclaims the record: there is no reason to keep a session for a room the bot
  can no longer read, and RocketChat's server tears down its own subscription
  anyway.

  This is deliberately **not** "subject to the paused-record rule" (§4.4),
  because that rule and this event answer different questions. Pause protects a
  record from *inactivity-driven* reclamation — the operator's "stay quiet here"
  must not be misread as "nobody needs this". Removal is not an inference from
  inactivity; it is the platform stating authoritatively that the room is gone.
  Honouring pause here would keep a session, a system-prompt file, an
  attachment directory and any pending jobs alive forever for a room that can
  never receive another message, and would leave `resume` able to "revive" a
  room the bot has no access to — which contradicts R3 outright.

  So: **revocation force-reclaims even a paused record**, logged as an audit
  event naming the room and noting that a pause was overridden, since that is
  the one case where an operator's explicit setting is discarded and it should
  never be silent. Pending jobs for the room are cancelled with a stated
  reason rather than left pointing at nothing — cancelled, not deleted: the job
  stays in `jobs.json` as `cancelled` with `cancelled_at`/`cancel_reason`, is
  purged after the completed-job TTL, and `schedule resume` restores it.

  Two consequences worth stating. Reclamation must be idempotent, because
  Mattermost's socket has no replay and a removal event can be *missed* —
  discovered later as a REST failure, which must reach the same end state
  rather than a different one. And re-adding the bot afterwards is simply a
  fresh start: a new record, a new session, no continuity with what was
  reclaimed. That is the correct outcome, not a regression.

  **"Discovered later as a REST failure" does not cover paused or idle
  records**, which is the one case that needs its own mechanism. That discovery
  depends on some future operation touching the room, and a paused record has
  no timer reclamation (§2.5) and receives no inbound messages — so nothing
  ever touches it. If the removal event was missed, the record, its session,
  its jobs, its prompt file and its attachment directory persist indefinitely,
  and `resume` keeps offering a room the bot cannot access — which is R3
  violated by a different route than the one pause was protecting against.

  So a **periodic membership reconciliation** is required for dormant records:
  re-check membership for paused and idle records on a slow tick and reclaim
  the ones that are gone. This is keyed on the connector declaring unsolicited
  inbound (§2.6), **not** on Mattermost specifically — Rocket.Chat has the same
  hole for a paused room, since its reconnect replay covers message history but
  not membership, and no message arrives to provoke a REST failure either. It
  is a slow tick: this is a correctness backstop for a missed event, not a
  primary path.

Connectors that declare no unsolicited inbound have no membership stream and
are unaffected.

### 2.8 The watcher manager

One class owns the lifecycle. Callers ask whether a watcher exists and get
one; they never drive creation, idling or expiry.

The block below is the *shape*, not the code's signatures. In the code, creation
and recreation live in `WatcherManager.get_or_create`; the verbs are
`SessionManager.pause_watcher`/`resume_watcher`/`reset_watcher`/`expire_watcher`
(by handle); `list` is `WatcherLifecycle.list_watchers`; and a residency probe does
exist — `WatcherLifecycle.processor_for_room` — which `inject_message` consults
before `get_or_create`.

```python
WatcherKey = tuple[str, str]          # (connector, room_id)

class RoomKind(Enum):
    CHANNEL = "channel"      # public channel
    GROUP = "group"          # private group
    DM = "dm"                # 1:1 direct message
    GROUP_DM = "group_dm"    # multi-party direct message

@dataclass(frozen=True)
class RoomRef:
    """Everything creation needs about a room, resolved once by the caller.

    `name` is the platform's own name and is empty for both DM kinds.
    `participants` is populated for the DM kinds and empty otherwise. Between
    them they supply the label (§2.3), the materialized config's room
    description (§2.4) and the state record's room_kind/participants (§5.3) —
    which is why this is a struct and not a name string.
    """
    id: str
    kind: RoomKind
    name: str = ""
    participants: tuple[str, ...] = ()

class WatcherManager:
    # resolution — the only place a display reference becomes a key
    def resolve(self, ref: str, connector: str | None = None) -> WatcherKey
        """Accepts a label, or a room name/id plus a connector. Raises on
        unknown or ambiguous input."""

    # the two ways to obtain a watcher
    async def get(self, key: WatcherKey) -> Watcher | None
        """A READY watcher. Recreates from the persisted record if the
        watcher is idle — callers never observe idleness. Returns None only
        when there is no record and no matching rule. Needs no RoomRef: a
        record already carries everything recreation requires."""
    async def get_or_create(self, connector: str, room: RoomRef) -> Watcher | None
        """As get(), and additionally creates a first-ever watcher from a
        matching rule. The message path. None when no rule matches.

        Takes a RoomRef rather than a key plus a name because creating a
        watcher requires the room's kind and participants, not just its id:
        the kind selects the label form and decides whether require_mention
        applies, and for a group DM the participants ARE the room's
        description. The key is (connector, room.id)."""

    # views and verbs
    def list(self, state: StateFilter = StateFilter.OPERABLE) -> list[WatcherView]
    async def pause(self, key: WatcherKey) -> None
    async def resume(self, key: WatcherKey) -> None
    async def reset(self, key: WatcherKey) -> None
    async def expire(self, key: WatcherKey) -> None
```

The asymmetry between the two getters is deliberate and worth stating: `get`
works from a key alone because a persisted record is self-sufficient, while
`get_or_create` needs a `RoomRef` because there is nothing persisted yet to
read the room's kind out of. That places the burden of resolving room metadata
on the routing layer, which is where the platform-specific knowledge already
lives (§2.2) — the manager stays connector-agnostic.

**Idleness is invisible to callers.** `get` returns a watcher that is ready
to use; if the record is idle it is recreated first. There is deliberately
no "is it resident?" variant on the routing path — a caller that had to
branch on state would be reimplementing the lifecycle, which is the thing
this class exists to own. Two consequences to accept:

- **`get` is async and can take seconds.** Recreating a watcher resumes a
  session and re-subscribes (~1–3s, §2.5). Every caller must tolerate that,
  including the control server and the scheduler.
- **`get` refuses to override an explicit pause** (§4.4). A paused watcher is
  not "idle with extra steps"; `get` on a paused record returns nothing
  usable rather than silently unpausing. Only `resume` clears it.

Recreation is not optional, because an inbound message cannot be the only
route back: scheduled work does not arrive through the inbound path at all.
Injection enqueues straight onto a processor, so an idled room has none,
injection fails, and the scheduler then advances `next_run` to avoid a retry
flood — the job is skipped silently, and skipped again every period,
forever. For Script this is the *only* path.

`list` enumerates **persisted records**, not live processors, or idle and
paused rooms become invisible to commands that can already act on them. It
takes a composable state filter, and the default is deliberately not "all":

```python
class StateFilter(Flag):
    ACTIVE = auto()
    IDLE   = auto()
    PAUSED = auto()
    FAILED = auto()
    OPERABLE = ACTIVE | PAUSED | FAILED   # the default
    ALL      = ACTIVE | IDLE | PAUSED | FAILED
```

**Default is active + paused + failed** — the watchers an operator is
realistically about to act on. A paused watcher belongs in the default view
precisely because it is waiting on a human decision, and a failed one because
it is the only state that means something is wrong (§2.5). Idle is
informational: the bot knows about the room, but nothing is running and nothing
is being withheld.

This matters more once membership events register joined rooms as idle
(§2.7): a bot in two hundred channels would otherwise have a `list` dominated
by rooms nobody has ever spoken in, burying the handful being worked on.
Idle is one flag away when the question is "what does the bot know about"
rather than "what is it doing".

**A scheduled job keys on `(connector, room_id)`, not on the label.** A watcher
name is free to change (§2.3), so keying on it means a rename orphans every job
on that room. The label is kept alongside as display metadata — it is what
`list`, `pause` and `expire` speak — and `room_id` is what a fire resolves
through.

### The routing rule, stated as a rule

Every site that takes a watcher identity **on behalf of a job** obeys this, and
it is written here because stating it once is what stops the seventh occurrence:

> A job is addressed by its `room_id`. The handle is consulted **only** when
> `room_id` is empty — a job written before schema 2 — and **never** once an id
> exists. The manager is `job.connector` and nothing else: a job whose connector
> is not configured is cancelled at its next slot, with an audit line, however
> many connectors hold its room. Occupancy
> is not ownership — under one-account-per-agent the sole remaining holder is by
> construction a different agent, and running the job there would execute it with
> that agent's backend, tools and account while the fire logged success.

The seam, so a reader can check it rather than rediscover it: a fire resolves
its job **once** in `JobScheduler._resolve_target`, which answers
`(manager, room_id)`; everything downstream — `SessionManager.inject_message`,
`SessionManager.notify_watcher_room`, the pause check, and
`GatewayService._cancel_jobs_for` — take a room id; `_cancel_jobs_for` alone
additionally takes a keyword-only `legacy_handle`, consulted only for a job with
no `room_id`. A job written before schema 2 has only its handle, and
`SessionManager.resolve_handle` is the one place on the runtime path that turns
one into a room id. `job_migrate._resolve_room_id` reads the handle too, by
design — recording the room is the migration's purpose.

The fence is mechanical: `tests/unit/test_by_name_lookups_are_fenced.py` walks
the runtime modules with `ast` and fails on any by-name lookup outside the
operator boundary (control-socket verbs, where a human typed the name) and
`resolve_handle`. Prose did not hold this line for a release; the signatures
and the test do.

**Why a rule and not six fixes.** The same defect — code reading the handle where
a room id was available — was found and fixed six times across four review
rounds, each time at a different site, each time correctly. It kept coming back
because §2.3's runtime dictionaries were keyed by the handle rather than by
`(connector, room_id)`, so the handle was the cheap lookup and the id the
awkward one. They are now keyed by room id (§2.3), which removes the gradient.
It does **not** make the defect unrepresentable — operators address watchers by
name, so a by-name API has to exist and a job path can still be written against
it — which is why this rule and `tests/unit/test_job_room_identity.py` remain
the thing that holds the line, with the re-keying making the right choice the
easy one rather than enforcing it.

**Two conditions on the resurrection promise**, because "a job brings its watcher
back" is true of neither an un-migrated job nor a connector that cannot look a
room up by id (`Connector.room_ref_by_id` answering `None` — every shipped
connector now overrides it, and a test over `SUPPORTED_CONNECTOR_TYPES` holds
that). Both are stated for operators in `docs/scheduling.md`; do not repeat the
promise anywhere without them.

Implemented (owner, 2026-09-01), after a first attempt was reverted for three
defects worth naming, because each is a way this can be got wrong again:

* the id must reach `jobs.json`. It did not, so the whole thing was a no-op
  after a restart — and the expiry exemption below had already been removed on
  the strength of it. `tests/unit/test_job_store_roundtrip.py` walks
  `dataclasses.fields()` so the next field cannot be dropped silently;
* the id must address the REPLY as well as the wake. Resolving the room by id
  and then re-reading the record by handle produced an empty room id for a
  watcher resurrected under a new label: the agent ran a full turn and the reply
  went nowhere, while the fire counted as a success;
* the id must be consulted BEFORE the handle. Consulted after, a job holding a
  correct id delivered into whichever room had taken its handle over.

`Connector.room_ref_by_id()` is the mechanism: the inverse of `resolve_room`, and
the direction that survives a rename. It returns a `RoomRef` rather than a `Room`
because creation needs the kind and, for the DM kinds, the participants. `None`
means answered-and-absent — no such room, another team, or this account is no
longer a member — and only a transport failure raises, so a caller can tell
"give up on this room" from "ask again later". Each connector answers through the
classifier its message path already uses, so the `d`-covers-both-DM-kinds problem
(§6.4) keeps one implementation. Rocket.Chat gets membership free (its
subscription record IS membership); Mattermost asks, because a channel the bot
was removed from still resolves by id there.

The wake stays where it was: `inject_message` resolves the room once — from the
record when one is bound to that room, from the connector by id when not — and
both the creation and the injected message's address come from that one
resolution. Pause, the creation cap and the rule match are all decided inside
`get_or_create`, so a job cannot reach a room a message could not, and pause
still outranks a schedule (§4.4). A room that cannot be resolved injects nothing,
rather than injecting a message with nowhere to reply.

**Old jobs migrate on an operator's command, not at fire time.**
`agent-chat-gateway schedule migrate` fills in `room_id` for jobs written before
schema 2. Deliberately not lazy: the step finds a job's room through its watcher
handle, and a handle only names the right room while nobody has renamed it — the
operator can choose a moment when that holds, right after an upgrade and before
touching room names. A job firing once a year would otherwise wait a year to
migrate, through a window in which anything could have happened (owner,
2026-09-01). The command is version-aware, so a deployment jumping two versions
runs both steps and one jumping one runs only the second; each step is also
idempotent, so a wrong version cannot corrupt anything. Nothing is guessed: a job
whose room cannot be identified is reported and left exactly as it was, and a
group DM — whose label is a digest of its room id, not a name — is told to be
recreated rather than resolved.

**A pending scheduled job earns a room no exemption from the sweep.** It used to
be exempt from expiry, never from idling, and the reason held while it did:
idling such a room is harmless because the job wakes it, but expiry deleted the
record the recreation read from. With the id on the job, an expired room is one a
job can bring back — a 9am job on an expired room recreates its watcher at 9am,
which is the feature working rather than a case to guard. The oracle that
answered the exemption, `GatewayService._has_pending_jobs`, is removed rather
than left accepted-and-ignored: an argument a caller still passes is an argument
it still believes in. The cancel-side rule it shared its fallback logic with is
still live, and still tested.

Expiry and membership removal now differ deliberately, and the difference is what
the two verbs MEAN. `expire` clears a session and reclaims a record; it does not
stop a rule watching a room (that is a rules edit, or removing the bot), so its
jobs are kept and one of them may bring the watcher back. Membership removal
cancels them, because the bot has left and the room can no longer answer.

**The operator's `expire` does not cancel a room's jobs** (owner, 2026-08-31).
It once did, borrowed from the membership-removal path, whose reason was that a
job left in the store would fire forever at a room that cannot answer. That is
true after a removal — the bot is gone from the room. It is false after an
expire: the room is still there, the bot is still in it, and a watcher handle is
a pure function of `(connector, room)` (§2.3), so the room's next message brings
the same handle back and the job resumes. Cancelling destroyed something
self-healing, and destroyed it silently, for an operator who asked about a
watcher and said nothing about their schedules.

A job DOES carry the room's identity, so an expire is not the end of it: the
next fire re-resolves the room by id and recreates the watcher through the
ordinary rule path. A fire that genuinely cannot — the room is gone, the
connector disowns it, or no rule claims it any more — fails audibly: logged,
`next_run` advanced, and a finite job's `run_count` deliberately not consumed.

There is no longer an asymmetry with the sweep to note. Both the timer and the
operator verb reclaim a job-bearing room, and in both cases a job can bring it
back.

One interaction worth stating: a room idle for a long time will have had its
session deleted by the agent backend regardless of what ACG persisted
(§3, backend retention). The recreation path handles that through the typed
session-not-found error — a new session is minted and handoff re-runs — which
is why that error, and not TTL arithmetic, is the load-bearing mechanism.

Configuration resolution is a **pure function of (rule, room)** reachable
identically from the message path, the control path and the scheduler (R6).
Today five divergent resolution predicates exist across the control server,
lifecycle, scheduler and session manager; the manager replaces all of them.

The shortcut to avoid: registering materialized configs into a mutable
process-wide config list. In-memory registrations vanish on restart while
on-disk records persist, and boot then eagerly starts every room ever seen.

---

## 3. Trade-offs

**Accepted:**

- **Rule edits reach existing watchers only at a reconciliation.** Between
  a boot and the next, a watcher keeps its materialized config; a rule edit
  cannot steal a running room or override a pause mid-flight. At boot (and at
  `config reload`) every record is reconciled against the current rules
  (§2.4): re-materialized where its rule changed or a different rule now wins
  its room, expired where no rule covers it. `config reload` runs that
  reconciliation on the live fleet and restarts only the connectors and
  agents whose definition changed — `docs/design/config-reload-design.md`
  records how, and what it does not guarantee. `expire` remains the blunt
  per-room route, at the cost of that room's conversational continuity.
- **`list` output becomes dynamic.** There is no longer a static set of
  watchers derivable from `config.yaml`; the answer to "what is being
  watched" is runtime state, so tooling must query the daemon. `agent-chat-gateway list`
  defaults to **active + paused + failed** — what an operator is about to act
  on — with `--all`, `--active`, `--idle`, `--paused` and `--failed` for the
  rest. Idle is excluded by default because with membership-event registration
  (§2.7) it is the largest and least actionable group; failed is included
  because it is the only state that means something is wrong (§2.5).
- **Idle and expiry are RocketChat and Mattermost only.** Script and Voice
  never reclaim, because neither has an inbound stream to wake from and
  neither supports history, so a fresh session would lose continuity with
  nothing to soften it.
- **First-match precedence is positional.** Reordering rules changes
  routing. This is simple and predictable, and it is the reason config
  order must be preserved by tooling.
- **Percent-encoded names are ugly** for rooms outside the ASCII-safe set.
  Correctness over aesthetics, and unreachable on current platforms.
- **A room's first message pays creation latency**, roughly 10–40s, most of
  it one model turn for history handoff. Acceptable in context: an agent
  turn on a large model can take comparable time anyway, so the first reply
  in a brand-new room being slower is not a distinct class of experience.
  Waking an idle room costs ~1–3s because the session resumes.
- **The connector processes messages for rooms no rule matches.** With
  subscribe-all, every message the bot can see reaches the gateway and is
  filtered, including rooms that will never have a watcher. The cost is
  per-message filter work plus one bounded queue and worker per room the bot
  is in (§2.2). It is accepted because the alternative — the connector
  consulting rules to decide what to subscribe to — puts lifecycle knowledge
  in the transport layer and reintroduces per-room subscribe management,
  which is the coupling this design removes. The pre-routing work audit in
  §2.2 exists to keep the dropped case cheap.
- **The STATIC-era upgrade is a clean break, not a migration.** Config, runtime
  state and scheduled jobs all change shape, and **none of them is converted**. A
  leftover old config field is a hard load error naming its replacement;
  legacy state files cause a refusal to start; jobs are re-created.

  This is about jobs keyed on a **static watcher name from config.yaml**: those
  names do not exist after the cutover and nothing can resolve them, so the guide
  has the operator delete and recreate every one. It is NOT the same population
  as a schema-1 `jobs.json`, whose jobs are already keyed on a derived handle —
  those DO convert, through `schedule migrate` (§ above). A deployment upgrading
  across the cutover does both: recreate the static-era jobs per the guide, then
  run `schedule migrate` for anything created since. The
  release ships one guide covering the procedure and stating the losses —
  chiefly that every room starts a fresh agent session, and that a paused
  room must be re-expressed as an `except_for:` entry or it becomes active
  (§5.3).

  Every alternative was considered and rejected for the same reason: each
  buys continuity on a single upgrade in exchange for permanent complexity.
  Accepting old config fields keeps two schemas in the loader forever;
  rewriting the operator's `config.yaml` is a surprising thing to do to a file
  a human owns; and a state converter cannot even work in principle, because
  a legacy record has no agent and the config that held it is gone by the time
  it would run.

  This is affordable **because adoption is early**, and the window closes. The
  same decision taken later, with real deployments, would be far more
  expensive — which is the argument for taking it now rather than deferring it
  behind a compatibility layer that would then be permanent.

**Rejected:**

- **Lazy-only, with no eager start.** Only Mattermost and RocketChat can
  discover rooms from inbound traffic. Removing eager start would mean
  Script and Voice watchers never start at all.
- **One rule per connector.** It cannot express Voice's one-port,
  multiple-agent form, and it forces either a second mechanism or a
  precedence policy anyway.
- **Deriving TTL from each agent backend's own session retention.** Clamping
  ACG's TTL to the backend's declared retention is one-sided and cannot help
  in the direction that matters: a backend configured to delete transcripts
  sooner than ACG expects will do so at *any* TTL value, while
  over-estimating merely wastes a recreate. The **typed session-not-found
  error** is the load-bearing mechanism instead — when a resume reports the
  session is gone, mint a new one and re-run handoff.

  Declared retention is still used, in two non-binding ways: as a hint for
  choosing defaults, and as a **config-load warning when a rule's
  `session_expire_days` exceeds the retention its agent's backend declares**.
  That turns a silently-degrading configuration into something the operator
  sees at the moment they write it, without making the loader enforce a limit
  it cannot actually guarantee. The warning must be worded as a likelihood
  rather than a certainty: a declared value is an assumption about the
  backend's own settings, not a contract. (Claude's default cleanup period is
  30 days but is user-adjustable and skipped in several documented paths;
  OpenCode has no automatic expiry at all, so no warning is possible there.)

---

## 4. Invariants the implementation must preserve

### 4.1 One session, one room

A session is bound to a room in three artifacts: the durable identity header
naming the room, re-supplied every turn; the transcript, which contains that
room's fetched history and every prior turn's server-injected
`[connector #room | from: user | role: …]` prefix; and the session-to-room
map, which is single-valued, so permission prompts for one room's tool calls
would surface in another. **A reused session is a cross-room data leak.**

**Two enforcement points are needed, in opposite directions.**

*One room, one processor.* Registration appended to a per-room list and dispatch
fanned out to every entry, so a duplicate degraded silently into two agents
answering every message. The index is now a single slot per room: a watcher
re-registering for a room it holds replaces its own processor, and a different
watcher raises. Implemented in `impl/uniqueness`.

*One session, one room.* That is the reverse direction, and reject-or-replace
does not cover it: it prevents two processors for one key, not one session bound
to two keys. Per-room locks do not help either — they serialise access to a
*room*, while the hazard is two different rooms resuming the same
`session_id`. And the session-to-room map is single-valued, so the second
binding silently overwrites the first instead of being detected.

Reachable how? A hand-edited or corrupted state file, or a defect in the
creation path itself. Not from legacy data — the clean break means no
pre-upgrade record survives to carry a stale binding in (§5.3). Rare either
way, but the consequence is the cross-room leak this invariant exists to
prevent, so it needs a positive check rather than an argument that it cannot
happen:

- Maintain a reverse index and **fail closed** if a second room attempts to bind
  an already-bound session.

  **Keyed by `session_id` alone, not by `(agent identity, session_id)`** as this
  section originally specified. The composite key is the honest identity of a
  session — ids are unique only within the store that issued them — but every
  routing map in `SessionMaps`, and every consumer of them, is keyed by the bare
  id. Permitting two bindings that those maps cannot represent moves the silent
  overwrite one level down instead of stopping it. The connector is compared
  alongside the room, because two connectors can resolve different watched rooms
  to one platform room id.
- Validate it across all persisted records at load, so a bad state file is
  caught before anything starts rather than on the unlucky second start.
- Check it atomically during provisioning, before either processor becomes
  visible to the dispatcher.
- Test: two records carrying the same `session_id`; assert the second watcher
  never starts and never receives a message.

### 4.2 State persistence must merge

State is currently written wholesale from the in-memory map, and boot seeds
that map only from configured watchers before saving unconditionally. With
rooms unenumerated, that combination erases every persisted record — session
ids, watermarks and paused flags — on the first boot, before any message
arrives. Six further save sites re-truncate on any later command.

Either every persisted record stays resident in the in-memory map, or the
save becomes a merge. The state file is the room registry under this design;
it cannot be a projection of config.

### 4.3 Watermark capture precedes unsubscribe

Processor teardown unsubscribes before reading the connector's watermark,
and unsubscribing at zero refcount discards the per-room state that read
depends on, so the persisted watermark silently stays stale. §2.2 removes
this from the idle path by never unsubscribing there, but it remains live on
pause, reset and shutdown. Fix with a reproducing test.

### 4.4 An explicit pause is never overridden by inference

Not by creation, not by wake, not by rule edits, and not by idle or expiry
timers. The common thread is that all of those are the gateway *inferring* that
a room no longer needs attention, and a pause is the operator having already
answered that question.

**The single exception is authoritative revocation**: the platform reporting
that the bot was removed from the room. That is not an inference — it is a fact
about access, and honouring a pause against it would preserve a session for a
room that can never receive another message (§2.7). Revocation force-reclaims,
and logs that it overrode a pause, because that is the one path where an
explicit operator setting is discarded and it must never be silent.

### 4.5 One bot account, one connector — and one owner for direct messages

Under subscribe-all, a connector receives everything its bot account can see.
Two connectors sharing an account therefore receive **identical** streams, and
every room matching rules on both gets two watchers — two agents in one room,
which is §4.1 again. The `(connector, room_id)` key cannot detect it: the
records differ in their connector component, and each connector writes a
separate state file.

**Enforcement is at runtime, after authentication — config alone cannot do
it.** Mattermost supports token-only auth, where `username` is empty, and two
*different* tokens can authenticate the *same* bot account. Comparing config
fields therefore misses precisely the case this invariant exists to catch, and
comparing token strings misses it too.

So each connector, once authenticated, reports a canonical
`(server origin, platform user id)` — the id from the platform's own
whoami-style call, not from config. A global registry rejects a duplicate
**before any subscription opens or any watcher is restored**, and fails closed:
a connector that cannot establish its own identity does not start, rather than
starting unvalidated.

Config load keeps a cheap early version of the same check for the case where a
stable identity *is* declared (username/password auth), because failing at load
is friendlier than failing at startup — but it is an optimisation, not the
enforcement point.

The rule, applied at that registry, is: reject two connectors sharing a
`(server origin, platform user id)` pair, with one exception and one condition:

- **Exception — Mattermost connectors scoped to different teams.** The socket
  spans every team the account belongs to, and two teams may hold channels of
  the same name (§6.3, §6.4), so each connector must discard events for teams
  other than its own. That team gate is what makes two connectors on one
  account safe for channels — which is why it is an invariant and not an
  optimisation.
- **Condition — at most one of them may handle direct messages.** A DM has no
  team, so the team gate cannot separate it, and it is delivered to every
  socket the account has open (§6.3). DM handling is opt-in per rule (§2.7), so
  config load must additionally reject more than one DM-enabled rule per bot
  account.

The general rule matters beyond Mattermost: Rocket.Chat has no team scoping to
fall back on, so two Rocket.Chat connectors sharing an account duplicate
*everything*, not just DMs.

---

## 5. What changes

### 5.1 Prerequisites

Independently shippable, and each is a separate change. The first two are
**live bugs today**, not merely groundwork.

1. **State save becomes a merge** (§4.2). Live: `sync_watchers` seeds the
   in-memory map only from configured watchers before saving unconditionally,
   so a watcher skipped because its agent was unavailable — a fail-closed
   guard, not a removal — has its session id, watermark and paused flag wiped.
   The same happens if its start raised. Only the "removed from config" case is
   intended, and that one is already warned about elsewhere. Fix by merging on
   save and making intentional pruning explicit, which turns silent loss into a
   record that outlives its config. Ships with a test that a record absent from
   config survives a save.
2. **Watermark capture before unsubscribe** (§4.3). Live, and silent: teardown
   unsubscribes before reading the watermark, and unsubscribing at the last
   watcher pops the very dict the read depends on, so the read returns nothing
   and the persisted value stays stale — messages are redelivered after a
   restart. Doubled, because the save path pulls from the same dict. Needs a
   reproducing test with a connector that models the pop; the existing tests
   cannot catch it, since they mock the getter or use a connector whose base
   implementation always returns nothing.
3. **Config-tool room merging must exclude roomless entries.** The merge-target
   search is blind to whether an entry has a room, so adding a room rewrites a
   roomless one in place. The write is not merely permitted but *actively*
   permitted: the save gate blocks only newly-introduced errors, and merging a
   room into a roomless entry **removes** its pre-existing error, so the
   difference is empty. Compounding it, such an entry is invisible in the TUI
   while remaining a live merge target. Must land before a roomless rule is
   expressible.
4. **De-duplicate the watcher field lists.** Not a collapse into one table —
   most of these lists are genuinely different concerns and merging them would
   be a regression. Only two are real duplication:
   - the template-forbidden-keys set exists in **four byte-identical copies**
     across the loader and the config tool, with a comment claiming tests keep
     them in sync and no such test existing;
   - the watcher template *field specs* and their *defaults* are one table
     split in two, with identical key sets.

   Left deliberately alone: the two shared-field sets differ by one key that a
   screen re-adds, and read from raw versus merged sources; known-fields
   (display) and template-fields (edit) differ in nesting granularity; and the
   shared-field and required-field sets are disjoint concerns.

   The genuine gap — the JSON schema is a superset that the config tool's lists
   do not track, and the user guide's field table is already stale — is fixed
   with the config-tool work (§5.6), not here. The schema is about to change
   shape, so a sync test written now would encode the wrong target.

### 5.2 Connector interface

#### Timestamps: one representation inside, ISO only where a human reads it

Two representations are in play and nothing said which went where, so a value
that looked right crossed an interface that wanted the other one. The rule:

> **Every timestamp crossing an ACG interface is epoch milliseconds as a
> string.** ISO-8601 appears in exactly two places, both of them edges: what
> the control socket accepts from an operator, and what `fetch_room_history`
> returns *inside its message dicts*, which an agent reads.

This is not a new convention — it is the one the system already followed almost
everywhere. `get_last_processed_ts`, `update_last_processed_ts`,
`probe_missed_since`, `replay_room_since`, both connectors' message-timestamp
extraction, `WatcherState.last_processed_ts`, `claim_boundary`, `just_before`
and the replay filter are all epoch-ms today. Writing it down closes the two
that were not.

**Why epoch-ms and not ISO**, given ISO is the friendlier form: the comparison
helpers are numeric. `ts_to_float` cannot parse ISO at all, so `ts_gt` falls
through to a *string* comparison — and `ts_gt(iso, epoch)` is therefore `True`
for every pair a deployment can currently produce, regardless of which instant
is actually later, because `"2026…" > "1786…"`. That accident expires when
epoch-ms crosses `"2000000000000"` in 2033. `just_before(iso)` is worse: it
returns its argument unchanged, so a bound meant to be exclusive silently is
not. A representation that the ordering primitives cannot order is not a
candidate for the internal one.

**Consequences at the two edges:**

* The control socket keeps accepting ISO from operators — it is the only
  human-typed timestamp in the system — and converts once, at that boundary,
  before calling any connector method.
* `fetch_room_history`'s `before_ts`/`after_ts` are epoch-ms like everything
  else. Its *return* dicts keep ISO in their `ts` field: that value is read by
  an agent, not compared by ACG.

**The failure this closes**, recorded because it was silent: connectors were
free to differ on tolerance. Rocket.Chat's bound normalizer accepted both forms
and Mattermost's raised on the wrong one, so the same epoch-ms value worked on
one connector and, on the other, raised into a blanket `except` that logged
"starting without history" — every recreation that minted a fresh session lost
its handoff, on the exact path the bound exists to serve. One documented
contract, two behaviours, no test that could see it.

```python
class Connector(ABC):
    # new
    @property
    def delivers_unsolicited_inbound(self) -> bool: ...
    async def resolve_room_by_id(self, room_id: str) -> Room: ...
    def reap_room(self, room_id: str) -> None: ...

    # new, optional — implemented only where a membership stream exists
    def register_membership_hook(self, hook: MembershipHook) -> None: ...

    # existing, semantics changed
    async def subscribe_room(self, room: Room, ...) -> None:
        """Local bookkeeping only; no longer gates delivery."""
```

`MembershipHook` receives added/removed events for the bot's own membership
(§2.7). Mattermost and RocketChat implement it; the base is a no-op, so a
connector without a membership stream needs no carve-out.

**Every path that resolves a room must take `room_id`, not a name.** Two
callers resolve by name today — the creation path and `fetch-history` — and
both break for a group DM, which has no name to resolve. `resolve_room_by_id`
is what they move to; `resolve_room(name)` survives only for the one case that
genuinely starts from a name, an eager rule with a literal room list (§2.6).

- **Mattermost**: replace the unknown-channel discard with the routing hook;
  hoist the system-message and own-message checks above the state lookup
  (both kinds are delivered, §6.2); take channel name, type and team id from
  the event rather than a REST call, treating an empty `data.team_id` as
  "not a team channel" (DMs) rather than a failure; use
  `channel_display_name` for a DM's identity, since `channel_name` is the
  opaque `<id>__<id>` form; add channel reaping. `resolve_room_by_id` is
  still needed, but as a fallback rather than on the hot path — and adding it
  means reconciling the two room-identity paths, since name-based resolution
  stores the configured string and cannot report DM type while id-based
  resolution returns the server's name, and only one form may reach the
  prompt prefix, session title and history.
- **RocketChat**: make `rid` authoritative rather than the DDP stream event
  name — confirmed necessary, since the event name is the literal reserved id
  and would otherwise collapse every room onto one queue and worker (§6.1);
  split the single room-id key space into a subscription registry keyed by
  stream and a dispatch registry keyed by room; thread the access object
  through instead of discarding it — it carries `roomParticipant` for the
  membership gate plus room type and name, which is what spares the routing
  path a REST lookup; add a participant-count lookup to tell a 1:1 DM from a
  group DM, since both report type `d` (§6.4) and the difference decides
  whether `require_mention` applies; map the raw `c`/`p`/`d` type letters onto the internal
  channel/group/dm vocabulary that history fetching depends on; resolve a DM's
  display identity separately, since the access object omits `roomName` for
  type `d`; **add a system-message filter to the live path** — the REST path
  has one and the live path does not, and under subscribe-all every
  join/leave/rename in every readable room now arrives (`t: "au"` observed);
  make subscribe local-only and never send a stream-level unsubscribe from a
  per-room call; attempt subscribe-all with a clean per-room fallback on
  `nosub`.
- **Both message filters**: the mention requirement is currently skipped on
  the test `room_type != "dm"`, which was written when `dm` meant 1:1. With
  `group_dm` as a distinct kind (§2.7), that test must send `group_dm` down
  the **mention-required** side — otherwise the agent answers every message
  from anyone in a group chat, which is the behaviour the whole 1:1/group
  distinction exists to prevent. Small change, easy to miss, and the reason
  the distinction is not merely cosmetic.
- **Voice**: default-deny unknown rooms; replace the busy reply for a
  routing miss; evict room state on expiry.
- **Script**: make the reply queue per-room before a rule may match more
  than one room, and surface the injection handler's result so an unmatched
  message fails fast instead of blocking.

### 5.3 State schema

Records are keyed on `(connector, room_id)`. Added to each record:

| Field | Purpose |
|---|---|
| `room_name` | a human-readable description of the room, refreshed from inbound messages: the platform's name for a named room, the counterpart or participant list for the DM kinds (§2.3). Display only — nothing keys on it |
| `room_kind` | `channel` / `group` / `dm` / `group_dm` — decides the label form and whether `require_mention` applies (§2.7) |
| `participants` | DM counterparts, for the `list` column; frozen at creation (refresh is issue #124), never part of a key |
| `connector`, `agent` | so a rule edit cannot silently re-point a dormant session |
| `backend_identity` | the resolved backend type + working directory the session was created against; compared before a stored `session_id` is reused, and a mismatch forces a fresh session rather than replaying the id into a different session store (§2.4) |
| `config_schema_version` | the record-format version the record was written at; frozen at creation |
| `created_at` | audit |
| `last_activity_at` | the idle clock (§2.5) |
| `dropped_at` | distinguishes was-active from was-idle at boot |
| `config` | the materialized watcher config used to recreate |
| `rule_name` | which rule created this watcher |
| `rule` | that rule as resolved at creation — the drift baseline (§2.4) |

Each field lands in two places — dataclass and current-format branch — and
**must ship with a round-trip serialization test**. This on-disk surface has no
serialization test today, which is why every addition here carries one.

There is deliberately no third place: `load_state()`'s existing legacy branch is
**deleted**, not extended. It best-effort reconstructs a record from
`watcher_id`, which cannot supply any of the fields above — no materialized
config, no originating rule, no agent identity, no lifecycle timestamps. Adding
the new fields to it would manufacture incomplete records and quietly bypass the
fail-closed refusal below, which is the actual contract.

`config` and `rule` are both nested structures rather than scalars, so the
serialization test must cover nesting and the empty/absent cases, not just
presence.

#### Upgrading: a clean break, not a migration

**There is no state migration, and none should be built.** A legacy state
record cannot be converted into a new one, and the cost of pretending otherwise
outweighs what it would preserve.

Why it cannot: a legacy record carries `watcher_name`, `session_id`, `room_id`,
`room_type`, `context_injected`, `paused` and `last_processed_ts` — and no
agent, no context files, no history-handoff settings. Those live only in
`config.yaml`, whose concrete-watcher shape is being removed. Any automatic
conversion would therefore have to either guess the agent from whichever rule
now matches the room — the silent re-binding sticky binding exists to prevent
(§2.4) — or read a config shape the loader no longer accepts, which means
keeping the old schema alive indefinitely for a one-time path.

So the upgrade is an operator procedure with a documented, accepted loss. This
is a deliberate trade: the alternative is permanent complexity in the loader and
a migration path that must stay tested forever, in exchange for continuity on
one upgrade.

**The procedure**, which belongs in the migration guide:

```
1. agent-chat-gateway list                      # record what exists, and what is paused
2. agent-chat-gateway schedule list             # record scheduled jobs
3. stop the gateway
4. rewrite config.yaml as rules (§5.4) — see "not a 1:1 rewrite" below
      – drop any `session_id:`; it no longer exists (§2.4)
5. remove the old state files:  ~/.agent-chat-gateway/state.*.json
6. start, then re-create the scheduled jobs from step 2
```

**This is not a 1:1 rewrite, and the guide should not present one.**

The mapping people will look for — one concrete watcher becomes one rule — teaches the
model the release is replacing. A concrete watcher answers *which room does this agent
sit in*; a rule answers *which rooms may this agent be drawn into*. An operator who
transcribes entry by entry ends up with a rule per room, which is the old shape wearing
new syntax, and never sees the point of the change. The guide's job is to make them
restate their intent, not to save them typing.

**A long-lived paused watcher is the clearest case of that.** Pause exists for temporary
operational reasons — mute this agent while something is being fixed. A watcher that has
been paused for weeks is a question about why it was created, not a state to carry
forward. So the upgrade offers no translation for it, and instead two honest paths:

- **The room is not the agent's to engage with** — then it needs no rule at all. This is
  where `include: ["*"]` with an `except_for:` list genuinely earns its place: it says
  *everywhere except these*, which is a statement about scope, not about state.
- **The pause really was temporary** — then write the ordinary rule, let the watcher
  start normally, and pause it with the CLI. Pause belongs to `state.<connector>.json`
  and to the operator's hands (§2.5); expressing it in config would conflate "which
  rooms are in scope" with "what is this watcher doing right now", and the two change
  for entirely different reasons and on entirely different timescales.

**And this is the same reasoning that rules out an automatic migration.** A converter
would have to produce exactly the 1:1 mapping described above — it has nothing else to
work from — so it would encode the old model into the new config and keep a second schema
alive in the loader to read it. The trade is deliberate: worse upgrade UX in exchange for
markedly less permanent complexity, taken while the installed base is small enough for
that to be the cheaper side. With a large installed base the calculation changes, and so
should the answer.

**What is lost, stated plainly rather than discovered:**

| Lost | Consequence |
|---|---|
| Agent sessions | Every room starts a fresh session. Conversational memory inside the agent is gone; history handoff refetches recent room messages, so there is partial continuity from the room's own transcript |
| `last_processed_ts` watermarks | A one-off boundary effect per room: a message either side of the cut may be reprocessed or skipped once |
| Paused state | A paused room becomes active again unless the operator decides what it was for. Pause is an operational verb — mute this watcher for now — and it is not a way to express "this room is not ours", so it does not translate into config. See "not a 1:1 rewrite" below |
| Scheduled jobs | A job keyed on a STATIC watcher name resolves to nothing after the cutover; those are re-created in step 6. A job already keyed on a derived handle converts instead — `schedule migrate` records its room id |
| Pinned `session_id` | The field is gone (§2.4). A config that sets it fails to load as an unknown key. The replacement: have the agent summarise its session to a file and read that back in the new one — which also survives the backend expiring a session, as pinning never did |

**One guard is worth the ten lines**: if the gateway finds legacy-format state
files it **refuses to start**, naming them and pointing at the guide. This is a
version check rather than a fallback — no conversion logic, no dual-schema
reader. It exists because the alternative is starting with an empty registry,
which silently abandons every session and looks like a successful boot.

**Records that would have needed special handling anyway**, noted because their
absence simplifies things: a legacy record with an empty `room_id` (produced by
pausing a name that never started) has no room identity at all, and a legacy
record has no originating rule. Under a clean break neither needs an answer.
Fresh records get a rule at creation, and pausing an unseen room is not
expressible in the new model (§2.5).

### 5.4 Config schema

- `watchers[].room` / `.rooms` → `watchers[].rooms.include` /
  `.rooms.except_for`, patterns, order-significant.
- New `watchers[].rooms.direct` and `.group_direct` booleans, both defaulting
  to false — DMs cannot be matched by name pattern on either platform (§2.7).
  Accept only the boolean form for now; the JSON schema should leave room for
  the object form (`direct: {include: [...], except_for: [...]}`) so the later
  extension in §2.7 is additive rather than a breaking schema change.
- `session_idle_days` / `session_expire_days` move from the agent to the
  rule, so two rules sharing an agent can differ.
- `session_id` removed entirely — a hard load error naming the handoff
  replacement (§2.4). Also drops the multi-room and duplicate-id cross-entry
  checks that only existed to police it.
- Literal-only `rooms.include` enforced for connectors declaring no
  unsolicited inbound.
- Pattern compilation, never-firing and shadowed-rule detection at load.
- Warning when a rule's `session_expire_days` exceeds the session retention
  its agent's backend declares (§3).
- **Rejection of two connectors sharing a bot account** — but only as an early
  best-effort here, for connectors whose config declares a username. The
  enforcement point is the post-authentication registry in §4.5, because
  token-only auth leaves no identity in config to compare. Only connector
  *names* are checked for uniqueness today.

**Migration is manual, and so is the rest of the upgrade.** Every removed or
moved field is a hard load error naming its replacement — no silent acceptance,
no auto-rewrite of the operator's file, no dual-path parsing. The same applies
to runtime state and scheduled jobs: neither is converted (§5.3). One migration
guide covers all three, following the precedent set by earlier breaking config
changes. Rationale is in §3.

This is affordable precisely because adoption is early. Choosing the clean
break now trades a one-time operator procedure for permanently simpler
loader, state and job code — the opposite trade to carrying a compatibility
path that would need maintaining and testing indefinitely.

### 5.5 Config tooling

> **Amended at implementation (`impl/config-tooling`, 2026-08-18):** the
> Rules tab shipped as specified below. The **Sessions tab is deferred** by
> owner decision — the config tool operates on `config.yaml` only and never
> talks to the control socket; runtime observability stays in `agent-chat-gateway list`,
> and the session verbs (`pause`/`resume`/`reset`/`expire`) stay CLI-only
> permanently. Of the four display states, only the two that concern the
> config side remain applicable (valid config → Rules; unparseable → the
> error banner). The delete-rule warning survives without the daemon: the
> stranded-session and orphaned-job counts are read directly from the
> persisted `state.<connector>.json` / `data/jobs.json` files, read-only.

Split the single Watchers view into a config-backed **Rules** tab and a
runtime-backed **Sessions** tab. Editing a rule and acting on a session are
different operations on different sources of truth; sharing one screen is
what makes §5.1's corruption reachable.

- Rules rows keyed by **list index**, which is also what preserves the
  ordering §2.1 depends on.
- Sessions rows keyed by `(connector, room_id)`, each labelled with its
  originating rule and **marked when that rule no longer exists or has
  changed since materialization**.
- Deleting a rule warns with the live and on-disk session counts it strands
  and the scheduled jobs it orphans.
- Four display states specified rather than discovered: daemon down → Rules
  only with an explicit note, never an empty table implying zero rooms;
  daemon up → both; config unparseable → the existing error banner; and the
  case with no precedent — the daemon's config is frozen at startup while
  the tool edits the file, so a live session can reference a rule already
  deleted on disk.
- The daemon query must not block the paint; refresh asynchronously with a
  spinner and degrade to "daemon down" on timeout.

### 5.6 Order

1. §5.1 prerequisites.
2. Rule parsing: patterns, include/except_for, order preservation,
   literal-only enforcement, shadowing detection.
3. State schema and its serialization tests, plus the legacy-state **refusal**
   (§5.3) — a version check with a message, not a converter. Test that a
   legacy-format file causes a clean refusal naming the file, rather than a
   crash or an empty-registry start.
4. **Re-key the system-prompt file and attachment workspace on a hash of `(connector, room_id)`**
   (§2.3). Independent of everything else, and it must land before labels can
   change freely — which the group-DM and rename cases both require. Existing
   installs have paths under the old names, so this needs a one-time move or a
   documented "these become orphaned, delete them" note in the migration guide.
5. **The post-authentication identity registry** (§4.5): each connector
   reports `(server origin, platform user id)` after login, duplicates are
   rejected fail-closed before any subscription opens. Independent of the rest
   and cheap; doing it early means every later step runs under the invariant.
6. Processor registration becomes reject-or-replace, **and the reverse
   `(agent, session_id)` uniqueness index** (§4.1) — validated across
   persisted records at load and atomically at provisioning; capacity preflight
   distinguishes empty from full.
7. The watcher manager: resolution as a pure function of (rule, room),
   materialization, the per-room lock, transparent recreation in `get`, the
   four-state lifecycle.
8. Routing: connector subscribes to everything, router walks rules,
   unmatched dropped — with the pre-routing cost audit. Mattermost first,
   then RocketChat.
9. The creation path in §2.7's ordering, including the dedup transaction with
   **per-room serialized resolution and an abort that halts the room's commit
   frontier** (§2.2) — the mention gate runs after classification, not with the
   cheap rejects. Ships with the **startup replay** over persisted records that
   the abort guarantee depends on, since the reconnect path does not run on a
   fresh process.
10. `list` with its state filter, in the control server and the CLI — before
   the idle tick, so there is a way to observe what idling does.
11. The idle tick, for connectors declaring unsolicited inbound.
12. Expiry, with full reclamation.
13. Membership-event registration (join → idle record, leave → expire), plus
    the **periodic membership reconciliation** for paused and idle records
    (§2.7) — the backstop for a removal event missed while disconnected, which
    no message-triggered path can discover for a dormant room. Last of the
    runtime work because it is an optimisation over the message-triggered path,
    which must be correct on its own first.
14. Config tooling.
15. The migration guide, shipped with the release that lands the schema
    change.

---

## 6. Verified platform behaviour

Both connector assumptions were resolved against a live Rocket.Chat and
Mattermost instance. Reproducible with `scripts/probe_a1_rc.py`,
`scripts/probe_a1_rc_followup.py`, `scripts/probe_a2_mm.py`,
`scripts/probe_group_dm_and_teams.py` and
`scripts/probe_rc_dm_immutability.py` — kept so each finding can be re-checked
against a different server version rather than trusted indefinitely.

Versions tested: **Rocket.Chat 8.5.1** and **Mattermost 11.7.0**.

**Re-verified 2026-08-20** against the E2E stack, which now pins the same
8.5.1 rather than the 6.12 it had been running (`make e2e-probe` reruns
`probe_a1_rc.py` against it, so this is a command rather than an
archaeology exercise). Every §6.1 row below still held: the literal
`__my_messages__` eventName, `len(args) == 2`, the access object carrying
`roomParticipant`/`roomType`/`roomName`, `roomParticipant: false` for a
readable non-member room, own messages delivered, and raw `roomType`
letters.

One refinement the original probe did not separate: **system messages are
delivered for rooms the account is a MEMBER of, and were not observed for a
non-member room.** Confirmed by kicking and re-adding a user in a member
room, which produced `t: "ru"` then `t: "au"` frames, while the same event
in a non-member room produced none (with `Hide_System_Messages` empty, so
nothing was being suppressed). This does not change what the runtime must
do — the `t`-field filter is still required, because the member-room case
is the one that reaches a live watcher — it only narrows where the traffic
comes from.

### 6.1 Rocket.Chat: `__my_messages__` works, and carries what routing needs

The subscription is accepted (`ready`, never `nosub`) and delivers messages
for rooms the account never per-room-subscribed to. Frame shape:

```json
{"msg":"changed","collection":"stream-room-messages","id":"id",
 "fields":{"eventName":"__my_messages__",
           "args":[ {...message...},
                    {"roomParticipant":true,"roomType":"p","roomName":"sandbox"} ]}}
```

| Observed | Design consequence |
|---|---|
| `fields.eventName` is the **literal** `"__my_messages__"`, not the originating room id | **The key-space split is required.** Current fan-out reads `eventName or rid` with eventName winning, so every room would collapse onto one queue and worker. |
| `args` has length 2 — message first, access object second | `args[0]` parsing survives unchanged; `args[1]` must be threaded through rather than discarded. |
| The access object carries `roomParticipant`, `roomType` **and** `roomName` | **No by-id REST resolver is needed on the routing path** for channels and groups — type and name arrive with the message. |
| For a DM the access object is `{"roomParticipant":true,"roomType":"d"}` — **`roomName` is absent** | A DM has no name to carry, so a display identity must be resolved another way (REST lookup, or derived from the sender). This is the one case that still needs a resolver. |
| `roomParticipant` is `true` for a room the account belongs to and `false` for a public channel it can merely read | This is the membership gate, server-computed per message, exactly as needed. |
| Own messages **are** delivered | Own-message filtering is required (already present). |
| System messages **are** delivered — observed `t: "au"` for a member-added event | **A `t`-field filter is required on the live path.** Only the REST history path filters system messages today; under subscribe-all every join/leave/rename in every readable room arrives. |
| `roomType` uses Rocket.Chat's raw letters: `c` public channel, `p` private group, `d` direct | Needs mapping to the internal `channel`/`group`/`dm` vocabulary that history fetching depends on. |
| Second `sub` parameter `false` (what ACG sends) vs `true` (what Rocket.Chat's own SDK sends) | No observable difference in the emitted frames. No change needed. |

#### Subscribe-all: the stream's lifecycle, and what depends on it

Written after six review rounds on the implementation, every one of which found a
consequence of the previous round's fix. The findings were not unrelated: switching
delivery to one stream introduces a **state machine** and a set of invariants that hold
across it, and discovering those one review at a time is how the same defect keeps coming
back wearing a different hat. Stated here so the code can be checked against a model
rather than against the last thing someone noticed.

**States**, per connector:

| State | Meaning | Who is subscribed | Who delivers |
|---|---|---|---|
| **absent** | no router registered; nothing wants the stream | each tracked room | its own subscription |
| **wanted** | a router exists, the stream is intended | — | — |
| **live** | the server confirmed and has not stopped it | the stream | the stream, for every room |
| **lost** | wanted but not live: refused, timed out, stopped, or the socket dropped | each tracked room | its own subscription |

Transitions: `absent → wanted` when a router is registered; `wanted → live` on a `ready`
**that still stands when the caller acts on it** (invariant 7);
`live → lost` on `nosub` for the stream id, on a socket drop, or on an explicit stop;
`lost → live` on a successful resubscribe. **Intent is not a state that failure clears** —
only `disconnect` returns a connector to `absent`.

**Invariants**, each of which a review round found violated:

1. **Exactly one delivery path per tracked room.** Never both — a room subscribed while
   the stream is live receives every message twice, and message-id dedup hides the second
   dispatch but not the queue slot it occupies. Never neither — a room whose subscription
   was released when the stream went live, and never restored when it went lost, receives
   nothing at all and looks healthy.
2. **Liveness is a transport fact, never a copy.** A connector-side flag saying the stream
   is live disagrees with reality the moment a restore fails, and a watcher added in that
   window registers a callback for a room nobody subscribed to.
3. **Intent survives failure; only the subscription id is per-attempt.** Recording intent
   on success means one transient failure leaves a connector on per-room delivery for the
   rest of its life, having asked for the stream exactly once.
4. **`live → lost` is not complete until every tracked room has its own subscription.**
   The transition is the point at which delivery would otherwise stop silently. Of the
   three ways it happens, only the socket drop reconnects — a `nosub` for a confirmed
   stream leaves a healthy socket, so nothing else will notice and nothing else will
   recover it.

   Completing the transition restores *delivery*; it does not recover what the gap lost.
   Between the stream stopping and the per-room confirmations, messages to tracked rooms
   reached nobody, and that is an outage whether or not a socket went down with it. The
   history replay therefore runs after this recovery exactly as it does after a reconnect —
   restoring delivery and recovering the outage are two separate obligations, and a path
   that discharges only the first loses messages quietly.
5. **Replay may not assume membership.** The access object — and with it
   `roomParticipant` — exists only on live-stream frames. History fetched after an outage
   carries none, and the removal itself is a system event the history filter drops. So a
   bot removed from a room *during* an outage replays that room's missed messages as
   though nothing happened. **Membership must be revalidated per room before replay is
   dispatched**, not inferred from what the live path happened to see.

   It is read from the account's subscription record for the room
   (`subscriptions.getOne`), which is what Rocket.Chat removes on leaving or being kicked;
   a *hidden* room keeps its record and is still membership. A room with no record is a
   **200 with a null subscription** — verified against the endpoint's handler, which is a
   plain `success({subscription: findOneByRoomIdAndUserId(...)})`, and against its own
   end-to-end tests; its declared failures are 400 for a malformed request and 401 for
   authentication, neither of which says anything about membership. So an HTTP error is
   **unknown**, and must stay unknown: answering "not a member" there would let an auth
   failure close the replay window and drop the watermark, which is silent message loss
   caused by an unrelated defect. That is the
   safe direction to be wrong in: a message withheld can still be read in the room it was
   sent to, and one sent to a room the agent was removed from cannot be taken back.
6. **The replay boundary is where delivery stopped, not where replay starts.** Rooms are
   resubscribed one at a time, so the first is live again while the last is still
   confirming — and a message arriving in that window is dispatched at once and moves that
   room's watermark past the whole outage. Replay, reading the watermark when it finally
   runs, then asks for history *after* the gap and never fetches it. The boundary must be
   captured while nothing is subscribed, before either the stream restore or the first
   per-room `sub`, and carried into the replay. Freezing the watermark instead would break
   live dedup during the recovery; the two marks are separate facts and both are needed.

   A boundary is spent when the window it names has been **dispatched**, not when it was
   fetched and not when a replay was attempted. Fetching a batch is not reading it: a
   shutdown or a second disconnect cancelling the loop midway leaves the tail
   unprocessed, and the restored live traffic has already moved the watermark past it, so
   a boundary cleared at fetch time makes the next recovery skip that tail for good.

   And a window may not span membership epochs — at **both** marks a delivery leaves
   behind, not only the watermark. A delivery still running when the removal lands writes
   twice: the cursor it commits on acceptance, and the window it claims when the processor
   hands the message back. Guarding one of them leaves the other pointing a later replay
   below the removal, which is the worse of the two, since it is the mark a replay reads.

   A *confirmed* removal closes it —
   otherwise an account that is later re-added replays from before it was removed and
   delivers everything said while it was not in the room, which the rejected-id window
   cannot prevent because those messages were never seen live at all. Unknown membership
   is not removal and still keeps the window open.

   Closing it means dropping the watermark as well, not only the boundary. The watermark
   *is* the fallback boundary, and it is frozen at the moment of removal — the live
   membership gate remembers a rejected id without advancing it — so a reconnect arriving
   before the first post-re-add message would snapshot that frozen value and replay the
   whole time away regardless. An empty watermark means for a re-added room exactly what
   it means for one seen for the first time: no window, and no ts-dedup until live traffic
   establishes one. The two ways a replay declines — membership unknown, the history fetch
   failing — are both correlated with the outage, since the network has only just come
   back, so they are the likely path rather than the exotic one; clearing the mark there
   would close a gap nobody looked at. For the same reason a new outage does not overwrite
   an unread boundary: the older mark covers both windows, and dedup bounds the cost.

   Because the window is never narrowed, **the timestamp cannot say who is owed a read of
   it, and a count of claims has to.** A live message handed back for capacity while a
   replay is dispatching claims the very window that replay is reading, so the value it
   writes back is the one already there. A batch that compares the boundary it snapshotted
   against the boundary it finds therefore sees "unchanged" in exactly the case the
   comparison exists to catch, closes the window, and the next accepted message moves the
   watermark past the handed-back message for good. Every claim increments a counter, and
   a window is closed only against a count read before the claim could have happened — at
   *both* sites that can decide a replay has read it, the dispatched batch and the fetch
   that came back empty, since a REST round trip is ample room for a hand-back.
7. **A confirmation is only as good as the transport's last word about it.** `ready` and
   `nosub` for the same subscription can arrive in one batch of frames, and the receive
   loop processes both before the coroutine awaiting the confirmation is scheduled again —
   resolving a future only *schedules* its waiter, and reading an already-buffered frame
   does not yield. In that window the subscription has an id, a resolved future, and no
   entry in `_stream_sub_id`: the rejection path sees a future with nothing left to
   reject, and the stream path does not recognise its own subscription. So the id has to
   be published *before* the wait, and the caller has to re-check that its confirmation
   still stands before recording it. Recording a revoked one claims delivery the server
   has already stopped — and the connector releases every per-room subscription on that
   claim.
8. **One recovery, one owner — structurally, not by checklist.** Restoring delivery has a
   single entry point: `_start_recovery(reason, try_stream=…)`, which retires whatever is
   running and installs one task in one slot. A socket drop and a stream terminated under
   a healthy socket differ only in whether the stream is worth asking for again in that
   instant; everything else they did was the same sequence written twice over the same
   state, and every review round on this file found another write in it with no owner.

   Two mechanisms carry the rule where a single sequence cannot reach:

   - **A room has at most one subscription, and installing one releases its predecessor —
     releasing *before* the map records the successor.** The map is the only record of
     what can still be live on the server, so at every await point it must name something
     releasable. The other order leaves it naming an id whose `sub` frame has not gone out
     while the predecessor is live and invisible. This has two sites — the migration loop
     and the install path — and fixing one of them is how the second was found.
     A recovery interrupted partway through releasing them is a normal event now — a
     recovery cancels whatever it displaces — so the next one must not overwrite a mapping
     whose server-side subscription is still live. Untracked is unreleasable: removing the
     watcher would stop only the replacement.
   - **A generation, for work that outlives its starter.** `subscribe_all()` is called
     directly by `start_inbound` as well as from a recovery, so it is not identifiable by
     the slot, and `stop()` retires every attempt at once without knowing any of their
     ids. Each attempt captures the generation and compares before publishing.

   The historical form of this invariant — "an attempt clears only the ids it published, a
   migration releases only the subscription it captured" — is what the structure now makes
   true by construction. It was stated as a checklist first, and three further violations
   followed in the next round alone, in nouns the checklist had not been applied to.

   Recoveries overlap: a socket drop starts a
   replacement while the previous attempt is still unwinding, and a stream lost during a
   migration starts a fallback that touches rooms the migration has already read. So every
   write to shared state names what it owns — an attempt clears only the ids *it*
   published, a migration releases only the subscription id *it* captured, and `stop()`
   owns all of it, being the transport half of `→ absent`. Unconditional clears are how
   the last attempt to finish wins, and the last to finish is not the current one.

   It covers the *task slot* as much as the fields in it: a recovery that displaces
   another stops it first and waits for the cancellation to be observed. Two recoveries
   both reaching the replay callback read the same boundary, and a message id is recorded
   only once its handler finishes — so the visible failure is one message answered twice.

   Two of the three violations that produced this rule were introduced by the fixes for
   the two invariants above it: each added a shared field without adding an owner. That is
   the argument for stating it as a rule and for testing the *surface* — the check that
   `stop()` clears every stream field is derived from the object, so the next field added
   fails locally rather than in a review.
9. **An unknown classification is not a default.** Rocket.Chat cannot distinguish a 1:1
   from a group DM without a lookup, and a failed lookup answering "1:1" is not a
   conservative guess: it lets a group DM be claimed by a `direct: true` rule *and* skip
   the mention gate, so the agent answers everyone in it. Unknown means do not offer the
   room; the next message asks again. (The same rule as §2.4's session identity —
   unverifiable is not verified.)

### 6.2 Mattermost: delivery tracks membership, not readability

The docstring claim holds, verified with an explicit preflight so that "no event"
cannot be confused with "no access".

Readability was established three ways with the probe's **own** token: `GET` on
the channel returned 200 with `type: "O"`, the channel appeared in the team's
public channel list, and the channel's posts were readable. Non-membership was
established by the channel being absent from the probe's own joined-channel list
and by an **admin-token** membership lookup returning 404 — note that the
probe's own membership lookup returns **403**, since a non-member cannot query
even its own membership row, which is why the admin-token check is the
load-bearing one.

Against that setup, a post in the readable-but-not-joined channel produced **no
`posted` event at all** for 12s, a post to a channel the probe belonged to
arrived in ~10ms as a positive control, and — the control that isolates the
variable — adding the probe to that *same* channel and posting again delivered
in ~10ms. Only the membership row changed between silence and delivery, which
rules out a channel-specific quirk.

| Observed | Design consequence |
|---|---|
| No event for a readable-but-not-joined public channel | Delivery **is** the membership signal. No additional membership gate is needed, and the creation path does not need a REST membership check. |
| `data.channel_name`, `data.channel_type` and `data.team_id` are all populated for channel posts | Routing can resolve name, type and team **from the event**, skipping a REST call — which matters for keeping work off the semaphore-held handler path. |
| `data.team_id` is empty for DMs; `broadcast.team_id` is empty always | Use `data.team_id`, and treat empty as "not a team channel" rather than a lookup failure. |
| DM `channel_name` is the opaque `<userid>__<userid>` form, but `channel_display_name` is the counterpart handle — **`@`-prefixed**, e.g. `@alice`. A **group** DM's is the member list and is **not** prefixed (`acg_bot, mmadmin, probe-extra`, §6.4) | Mattermost supplies a usable DM display name where Rocket.Chat does not, and is inconsistent between the two direct kinds. The prefix must be **stripped** on the way into `participants` (`normalize.bare_handle`), or one person ends up with two handles: `<connector>:dm:%40alice` on Mattermost against `<connector>:dm:alice` on Rocket.Chat, since `@` is outside `_LABEL_SAFE` and gets percent-encoded. Strip before testing for emptiness — a display of `"@"` alone would otherwise yield one empty participant and a nameless `dm:` label instead of the room-id digest fallback. Only the event path carries any name at all: REST's `display_name` for a direct channel is the empty string, which is why the membership-add path produces no counterpart rather than a bare one. |
| Own messages and system messages (`system_join_channel`, `system_leave_channel`) are delivered | Both filters are required; the existing non-empty-`type` check covers the system case. |

**Re-verified 2026-08-20** against the E2E stack, which now runs Mattermost
11.7.0 — the same version this section was probed on (`make e2e-probe-mm`
reruns `probe_a2_mm.py` against it). Every row above held as stated, including
the isolation case: no event for the readable-but-not-joined channel, delivery
within milliseconds once the membership row was added.

**But the probe did not catch everything, and that is worth recording.** The
`@` prefix on a DM's `channel_display_name` — now the subject of the row above
— was missed by both the original probe and this re-verification, and was
found afterwards by reading a populated `agent-chat-gateway list --all` and
seeing a watcher called `mm-e2e:dm:%40test_user`. The probe prints the raw
event and so contained the evidence all along; nothing asserted anything about
it. The group-DM half of the same field was then confirmed bare (§6.4) by
`probe_group_dm_and_teams.py` on 2026-08-21, after a comment in the connector
had already claimed the opposite. A probe answers the questions it was written
to ask; a field it merely prints is not thereby verified.

This is now also covered **through the runtime**, not only at the platform
level, by `tests/e2e/test_mm_membership_delivery.py`. The distinction is worth
keeping: the probe establishes what Mattermost does, while the E2E test
establishes that ACG honours it — with a rule that matches the unjoined
channel, an allow-listed poster and a mentioned bot, so that none of those
can account for the silence. A probe that passes while the test fails locates
the regression in ACG; both failing locates it in the platform.

### 6.3 Mattermost: channel names are per-team, DMs are per-account

Two further observations, both with design consequences:

**A channel name is unique only within a team.** Creating `sandbox` in a
second team succeeded while `sandbox` already existed in the first, yielding
two distinct room ids. So `channel name` is not a global identifier on
Mattermost, and a derived display name of the form `<connector>-<channel>` is
unique only because a connector is scoped to exactly one team. That scoping
stops being an organisational preference and becomes load-bearing — see §4.5.

**A direct message belongs to no team and reaches every socket the account
has open.** Observed by opening two independent sessions for one bot account:
a single DM was delivered to **both**, with `team_id` empty on each. A
team-channel post was likewise delivered to both sockets, because the account
belongs to that team.

The channel case is handled by the team gate — a connector scoped to team B
discards a team-A channel event. **The DM case is not**, and it cannot be,
because there is no team to gate on. Two connectors serving two teams with the
same bot account would therefore both create a watcher for the same DM. Since
each connector keys its state by `(connector, room_id)` and writes its own
state file, the two records differ in their connector component and the usual
one-watcher-per-room dedup never sees the collision. The result is two agents
answering one DM — an R4 violation invisible to every existing check. Closed
by §4.5.

### 6.4 Group direct messages, and Mattermost cross-team delivery

**Mattermost distinguishes group DMs; Rocket.Chat does not.**

| | Mattermost group DM | Rocket.Chat group DM |
|---|---|---|
| type on the wire | `channel_type: "G"` — distinct from `"D"` | `roomType: "d"` — **identical to a 1:1** |
| stable identifier | `channel_name` is a stable opaque hash (e.g. `1b4c4b32…`) | none in the frame |
| human-readable name | `channel_display_name` is the member list, e.g. `"glin, probe-bot, probe-extra"` — **not `@`-prefixed**, unlike a 1:1 DM's counterpart (§6.2). Re-observed 2026-08-21 on 11.7.0 as `"acg_bot, mmadmin, probe-extra"` with `scripts/probe_group_dm_and_teams.py` | absent |
| team | empty, as for a 1:1 | n/a |

Two corrections to earlier wording follow. Mattermost group DMs *do* have a
stable identifier — it is `channel_name`, which is opaque and does not move
within an observed session (whether it survives a membership change is still
open, §6.5 — do not rely on it as a durable key until that is confirmed);
what is unstable is `channel_display_name`, since it is derived from the member
list and includes the bot itself. And on Rocket.Chat a group DM is
indistinguishable from a 1:1 **from the frame alone**: the participant list
exists only over REST, so classifying the two requires a lookup per DM room
(cacheable, but a lookup).

**A Rocket.Chat DM's member set is immutable, which is what makes that cache
safe.** On 8.5.1, every route for adding a participant to an existing type-`d`
room is refused on the grounds of the room *type* rather than permissions —
`addUsersToRoom`, the route the web UI itself uses, returns
`error-cant-invite-for-direct-room`; `channels.invite` and `groups.invite` return
`error-room-not-found` because a DM is neither; `im.invite` does not exist.
Removal is refused the same way, so the set cannot shrink either. Instead,
`im.create` is idempotent *per member set*: asking for a different set returns a
**different room id**, and the original 1:1 keeps its own id with two members.
A group DM is therefore a separate room, never a mutated 1:1 — which is why the
kind cache in §2.2 needs no invalidation path.

Two incidental facts, both of which would be easy to assume wrongly: DM room ids
on 8.5.1 are ordinary 24-character ObjectIds, **not** the concatenated-sorted-user-id
form some older material describes, so participants cannot be recovered from the
id; and the member cap is a server setting (`DirectMesssage_maxUsers`, 8 on the
lab).

**Why the distinction has to be kept anyway.** It is not about display names.
`require_mention` is skipped entirely when a room's type is `dm` — on both
platforms — so a group DM classified as a plain DM makes the agent answer
*every* message from *anyone* in that group chat. That is the real cost of
getting the classification wrong, and it is why `group_direct` is a separate
opt-in (§2.7) rather than folded into `direct`.

Consequence for Rocket.Chat: honouring a separate `group_direct` there needs a
participant-count lookup when a DM room is first seen. **That answer never
expires**, per the immutability finding above — a DM cannot gain members, and a
different member set is a different room id — so the cache needs no invalidation
path. An earlier draft of this paragraph said a DM that later gained members
would need re-classifying; that was written before the 8.5.1 probe and describes
a transition the platform does not allow.

**Cross-team delivery is real, and the team gate is load-bearing.** With the
probe account added to a second team, a post in that team's channel arrived on
the same socket, carrying `data.team_id` for the second team. The channel was
also named `sandbox` — the same name as the first team's channel — so a single
socket delivered two *different* rooms whose names are identical. Without the
team gate a connector would derive one watcher name for both, which is the
collision §4.5 exists to prevent, now demonstrated end to end rather than
argued.

### 6.5 Still open

None of these gate the design.

- **Which Rocket.Chat versions support `__my_messages__`, and whether an
  administrator can disable it.** Confirmed working on **8.5**; that is a
  floor, not a range. A refusal arrives as `nosub` at subscribe time, so
  attempting it with a per-room fallback is safe regardless — this only
  decides how long the fallback must be kept.
- **Server-side cost of subscribe-all on a large workspace**, given the access
  check runs per message per subscriber. Not measurable on a lab with no load;
  wants observation on a real deployment before wide rollout.
- **Whether a Mattermost group DM's `channel_name` hash is stable across
  membership changes.** It is stable across the observed session, and its
  construction suggests it is derived from the member set — which would mean
  adding a member yields a *different* channel entirely rather than mutating
  this one. That is exactly how Rocket.Chat behaves (§6.4), which makes it the
  likely answer here too, but it is inference rather than observation. Worth
  confirming before relying on it as a durable key, though `room_id` is the
  actual key regardless (§2.3).
- **Whether Rocket.Chat's refusal to mutate a DM's member set holds on other
  versions.** Established on 8.5.1, at the API level, and the refusals are on
  room type rather than on permissions — so they apply to any caller, including
  an administrator. The kind cache in §2.2 depends on this. If a later version
  permits in-place growth, the cache needs the invalidation hook that section
  identifies; nothing else in the design changes.
