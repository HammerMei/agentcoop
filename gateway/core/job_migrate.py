"""Operator-run migrations for `jobs.json`.

`agent-chat-gateway schedule migrate` runs these. Deliberately not automatic,
and not lazy at fire time: the 1→2 step reads each job's watcher HANDLE to find
its room, and a handle only names the right room while nobody has renamed it.
The operator is the one who can choose a moment when that holds — right after an
upgrade, before touching room names. A job firing once a year would otherwise
wait a year to migrate, through a window in which anything could have happened
(owner, 2026-09-01).

**Two properties, and both are needed.** Version awareness decides WHICH steps
run, so a deployment jumping 1 → 3 gets both of them and one jumping 2 → 3 gets
only the second. Idempotence means each step is safe to run again anyway, so a
wrong version guess cannot corrupt anything. Either alone is insufficient:
without the version, a 1 → 3 upgrade cannot know it owes two steps; without
idempotence, an interrupted run leaves the file in a state no version describes.

**Nothing is guessed.** A job whose room cannot be resolved is reported and left
exactly as it was. The command's output is the record of what happened, which is
the whole advantage over doing this invisibly at fire time.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Callable

from ..schedule_types import JobStatus, ScheduledJob
from .job_store import _SCHEMA_VERSION, JobStore

logger = logging.getLogger("coop.core.job_migrate")


@dataclass
class JobOutcome:
    """What happened to one job, in the operator's terms.

    Three states, not two, and the third is why: `changed` and
    "needs the operator" are different questions, and a job that was already up
    to date is neither. Carried as a FLAG rather than inferred from `detail` —
    the CLI used to filter on the substring "already has", which is a sentence
    anyone could reword into a miscount.
    """

    job_id: str
    watcher: str
    changed: bool
    detail: str
    needs_attention: bool = False


@dataclass
class MigrationReport:
    """What the run did. `to_version` is what it AIMED at; `stamped` is whether
    the file is now actually there.

    The two are separate because the run can legitimately finish without moving
    the version — any job needing attention holds it back — and reporting
    `to_version` as if it had landed told the operator "jobs.json migrated 1 → 2"
    about a file still on 1, which the next startup warning then contradicted.
    """

    from_version: int
    to_version: int
    steps: list[str] = field(default_factory=list)
    outcomes: list[JobOutcome] = field(default_factory=list)
    stamped: bool = False

    @property
    def changed(self) -> int:
        return sum(1 for o in self.outcomes if o.changed)

    @property
    def unresolved(self) -> list[JobOutcome]:
        """The jobs a human has to do something about.

        NOT "everything that did not change": a job that already had a room id
        did not change and needs nothing. Conflating the two would leave the
        schema version un-stamped forever, because a clean re-run reports every
        job as unchanged.
        """
        return [o for o in self.outcomes if o.needs_attention]

    def to_dict(self) -> dict:
        return {
            "from_version": self.from_version,
            "to_version": self.to_version,
            "stamped": self.stamped,
            "steps": list(self.steps),
            "changed": self.changed,
            "outcomes": [
                {"job_id": o.job_id, "watcher": o.watcher,
                 "changed": o.changed, "detail": o.detail,
                 "needs_attention": o.needs_attention}
                for o in self.outcomes
            ],
        }


def split_handle(watcher: str) -> tuple[str, str]:
    """`<connector>:<room label>` → the two halves.

    The FIRST `:` is the boundary: a connector name may not contain one (config
    load refuses it) and the label encoder percent-encodes it out of room and
    user names, so this cannot be fooled by a room called `dm:alice`
    (`watcher_manager.watcher_label`).
    """
    connector, sep, label = watcher.partition(":")
    return (connector, label) if sep else ("", watcher)


def room_name_for_label(label: str) -> str | None:
    """The name to ask a connector to resolve, or `None` if the label is not one.

    * a channel label is the room name **percent-decoded**. `watcher_label` runs
      it through `_encode`, which escapes everything outside `[A-Za-z0-9._-]` —
      so a voice room `a/b` (from `/ask/a/b`, the case `_encode`'s own docstring
      cites) labels as `a%2Fb`. Asking a connector to resolve `a%2Fb` fails
      loudly on Rocket.Chat and Mattermost, whose names are slugs, but the voice
      connector ECHOES whatever it is given — it would have recorded a room id
      of `a%2Fb`, matching nothing, and reported it as a success;
    * `dm:alice` asks for `@alice`, the spelling `resolve_room` documents;
    * `gdm:<digest>` is a digest of the room id, not a name — nothing can resolve
      it, and inventing something would be a guess. Reported instead.

    A label truncated by `_encode`'s length cap carries a `-<digest>` suffix and
    is therefore not the name either. It fails loudly at the connector, which is
    the right outcome; detecting it here would mean re-deriving the cap.
    """
    from urllib.parse import unquote

    if label.startswith("gdm:"):
        return None
    if label.startswith("dm:"):
        counterpart = unquote(label[len("dm:"):])
        return f"@{counterpart}" if counterpart else None
    return unquote(label) or None


def _is_room_not_found(exc: BaseException) -> bool:
    """Is this exception a connector saying "no such room"?

    Matched on the class NAME rather than by importing one. There are two
    `RoomNotFoundError` classes — `connectors/rocketchat/rest.py` and
    `connectors/mattermost/rest.py` — and they are unrelated types, so importing
    either would silently treat the other platform's final answer as retryable,
    which is the exact confusion this predicate exists to remove. A core module
    importing a specific connector would also invert the dependency.

    The honest alternative is a shared exception in `core/`, which is a wider
    change than this increment owns; noted rather than done.
    """
    return type(exc).__name__ == "RoomNotFoundError"


async def _resolve_room_id(entry, job: ScheduledJob) -> tuple[str, str]:
    """`(room_id, detail)` for one job — `("", why not)` when it cannot be found.

    Two sources, strongest first:

    1. **The live record for this handle.** Authoritative: it holds the room id
       the watcher is actually bound to.
    2. **Asking the connector to resolve the label as a name.** Correct exactly
       while the handle still names the room it named when the job was created,
       which is the assumption the operator upholds by running this before
       renaming anything.
    """
    record = entry.session_manager.get_watcher_state(job.watcher)
    if record is not None and record.room_id:
        return record.room_id, "from its watcher's record"

    connector_name, label = split_handle(job.watcher)
    if not connector_name:
        # A derived handle ALWAYS contains a `:` — `watcher_label` builds it as
        # `f"{connector}:{label}"` and config refuses a connector name with a
        # colon in it. So a handle without one is a STATIC-era watcher name,
        # which is not a room name and never was.
        #
        # Resolving it as one is the guess this module promises not to make, and
        # it was the worst kind: a static watcher `stock-bot` watching #trading
        # bound its job to a channel that merely SHARED the name, was reported
        # as `✓ resolved 'stock-bot'`, and stamped the schema version — so the
        # startup warning went quiet and every later fire delivered into the
        # wrong room, silently. Measured, not reasoned.
        #
        # `migration-dynamic-watchers.md` step 7 already says these jobs must be
        # deleted and recreated. Saying so here is what makes that instruction
        # hold for an operator who skipped it.
        return "", (
            f"{job.watcher!r} is a static-era watcher name, not a derived "
            f"handle (no connector prefix), so there is no room name to look "
            f"up — delete this job and recreate it against a current watcher"
        )

    room_name = room_name_for_label(label)
    if room_name is None:
        return "", (
            "a group DM's label is a digest of its room id, not a name, and this "
            "watcher has no record to read the id from — delete this job and "
            "recreate it against the current watcher"
        )
    try:
        room = await entry.connector.resolve_room(room_name)
    except Exception as exc:
        if _is_room_not_found(exc):
            # FINAL. The same distinction `Connector.room_ref_by_id` makes
            # load-bearing: collapsing it would leave the operator re-running a
            # command that can never succeed for this job, and — since the
            # schema version is not stamped while anything needs attention —
            # chasing a startup warning that will not clear until they delete it.
            return "", (
                f"there is no room named {room_name!r} — this job cannot be "
                f"migrated; delete it, or recreate it against a current watcher"
            )
        # RETRYABLE: the ask failed, not the answer.
        return "", (
            f"could not reach the connector to resolve {room_name!r} ({exc}) — "
            f"run 'schedule migrate' again"
        )
    if room is None or not getattr(room, "id", ""):
        return "", (
            f"the connector knows no room named {room_name!r} — this job cannot "
            f"be migrated; delete it, or recreate it against a current watcher"
        )
    # Said out loud: this room was found by NAME, from the job's handle, not from
    # a record. A static-era watcher whose name happened to be shaped like a
    # derived handle (`<connector>:<something>` — static names forbade only `/`)
    # could name a room other than the one it served, and nothing here can tell.
    # The migration guide has the operator delete static-era jobs before this
    # runs; the report line is what lets them catch one that slipped through.
    return room.id, f"resolved by NAME {room_name!r} from the job's handle — verify this is the room the job meant"


async def _migrate_1_to_2(store: JobStore, entries) -> list[JobOutcome]:
    """Record each job's room id.

    Idempotent: a job that already has one is skipped, so a re-run touches
    nothing. `connector` is filled in at the same time when it is missing —
    `_resolve_target` needs it, and a job with neither field cannot be
    routed to a manager at all.
    """
    by_name = {e.name: e for e in entries}
    outcomes: list[JobOutcome] = []

    # Every job that can still fire, INCLUDING cancelled ones: a cancelled job
    # is kept so `schedule resume` can restore it, and a restored job needs
    # its room. Only COMPLETED has nothing left to fire — the same exclusion
    # `jobs_missing_room_id` makes, so the two cannot disagree about whether a
    # migration is still owed (internal review: they did, and the startup
    # warning became permanent for a cancelled pre-schema-2 job).
    for job in store.list_jobs(include_completed=True):
        if job.status == JobStatus.COMPLETED:
            continue
        if job.room_id:
            outcomes.append(JobOutcome(
                job.id, job.watcher, False, "already has a room id"))
            continue

        connector_name, _label = split_handle(job.watcher)
        entry = by_name.get(job.connector) or by_name.get(connector_name)
        if entry is None:
            outcomes.append(JobOutcome(
                job.id, job.watcher, False,
                f"no configured connector named {job.connector or connector_name!r}",
                needs_attention=True,
            ))
            continue

        room_id, detail = await _resolve_room_id(entry, job)
        if not room_id:
            outcomes.append(JobOutcome(
                job.id, job.watcher, False, detail, needs_attention=True))
            continue

        # Targeted, not `store.update(job)`: this job was read before the
        # `resolve_room` await, and `update` raises `KeyError` if a
        # `schedule delete` removed it during that await — aborting the run and
        # losing the report for every job already migrated. See
        # `JobStore.set_room_id`.
        if not store.set_room_id(job.id, room_id, connector=entry.name):
            # NOT attention-worthy: the job is gone, so there is nothing left
            # to fix and nothing to hold the schema version back for. Reported
            # so the operator can see why a job they expected is absent from
            # the ✓ list.
            outcomes.append(JobOutcome(
                job.id, job.watcher, False,
                "deleted while the migration was resolving its room",
            ))
            continue
        outcomes.append(JobOutcome(
            job.id, job.watcher, True, f"room {room_id} ({detail})"))

    return outcomes


# (from_version, to_version, description, step, outstanding)
#
# `outstanding` asks the store whether this step's work is still undone,
# independently of what the version claims. Version-awareness alone selects
# steps by a number the file asserts about itself, and that number can be true
# while the work is not: any writer holding a job across the migration's write
# can drop the field after the version was stamped, and `migrate` would then
# skip the only step that repairs it — silently, permanently, with the startup
# warning off. The predicate lives on the store (`jobs_missing_room_id`) so this
# and `needs_migration` cannot drift apart.
_MIGRATIONS: list[tuple[int, int, str, Callable, Callable]] = [
    (1, 2, "record each job's room id so it survives a rename or an expire",
     _migrate_1_to_2, lambda store: store.jobs_missing_room_id()),
]


async def migrate(store: JobStore, entries) -> MigrationReport:
    """Run every migration this file still owes, in order.

    Raises `ValueError` when the file is NEWER than this code: there is nothing
    safe to do, and saving would drop the fields this version does not know.
    """
    from_version = store.file_version
    if from_version > _SCHEMA_VERSION:
        raise ValueError(
            f"jobs.json declares schema version {from_version}, but this ACG "
            f"understands {_SCHEMA_VERSION}. It was written by a newer version — "
            f"upgrade ACG rather than migrating down."
        )

    report = MigrationReport(from_version=from_version, to_version=_SCHEMA_VERSION)
    owed = [m for m in _MIGRATIONS
            if m[0] >= from_version or m[4](store)]
    if not owed:
        report.stamped = True  # already there; nothing to write
        return report

    for start, end, description, step, _outstanding in owed:
        report.steps.append(f"{start} → {end}: {description}")
        report.outcomes.extend(await step(store, entries))

    # Stamped last, and only when every job was accounted for. Two reasons, and
    # the second is the one that bites:
    #
    # * an interrupted migration leaves the version where it was, so a re-run
    #   repeats the steps rather than skipping them — safe precisely because
    #   each step is idempotent;
    # * a job left UNRESOLVED is unfinished business. Stamping over it would
    #   make the early return above answer "nothing to do" on the re-run the
    #   operator is told to make after fixing the cause — bringing the room's
    #   watcher back, correcting a name — and that job could then never be
    #   migrated at all. So the version moves only when there is nothing left
    #   to do, and `schedule migrate` stays worth running again.
    if report.unresolved:
        logger.info(
            "jobs.json left at schema version %d: %d job(s) could not be "
            "resolved. Fix those and run 'schedule migrate' again.",
            from_version, len(report.unresolved),
        )
        return report
    store.stamp_version(_SCHEMA_VERSION)
    report.stamped = True
    logger.info(
        "jobs.json migrated %d → %d (%d job(s) changed)",
        from_version, _SCHEMA_VERSION, report.changed,
    )
    return report
