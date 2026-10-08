"""Read-only peek at the daemon's persisted runtime files, for the Rules
tab's delete-rule warning (design §5.5: "Deleting a rule warns with the
session counts it strands and the scheduled jobs it orphans").

Owner decision (2026-08-18): the config tool operates on config.yaml ONLY —
it never talks to the control socket and never writes runtime state. So the
counts here come from reading the persisted files directly
(`state.<connector>.json`, `data/jobs.json`), read-only and best-effort:
these files belong to the daemon, their absence or corruption is not this
tool's problem to report, and a count of 0 in that case simply means the
warning has nothing extra to say. The daemon's own load path
(gateway/core/state.py, gateway/core/job_store.py) remains the only
authority on what the files MEAN — this module only counts, using the same
top-level shapes those modules document ({"version", "watchers": [...]}
and {"version", "jobs": [...]}).
"""

from __future__ import annotations

import json
from pathlib import Path

from ..core.job_store import JOBS_FILE
from ..core.state import state_files
from ..schedule_types import JobStatus


def stranded_by_rule(  # noqa: C901
    rule_name: str,
    state_paths: list[Path] | None = None,
    jobs_file: Path = JOBS_FILE,
) -> tuple[int, int]:
    """(persisted session records, scheduled jobs) that reference `rule_name`.

    Records match on their own `rule_name` column; jobs reference a WATCHER
    name (`ScheduledJob.watcher`), not a rule, so a job counts as orphaned
    when its watcher belongs to one of the matched records. Best-effort:
    unreadable or unexpected files contribute nothing (see module docstring).

    `state_paths`/`jobs_file` default to the daemon's real files
    (`state_files()`, `JOBS_FILE`); parameters exist so tests never touch a
    developer's actual runtime directory.
    """
    watcher_names: set[str] = set()
    # The stable identity beside the handle: a job created against a room keeps
    # that room's id even after a rename + expire + recreate has given the
    # watcher a new handle, so counting by handle alone read zero jobs for a
    # rule whose rooms still had jobs firing (Codex, PR #140 round 2).
    room_keys: set[tuple[str, str]] = set()
    records = 0
    if state_paths is None:
        # The enumeration itself can raise (state_files() ->
        # ensure_runtime_dir() on an uncreatable/unlistable runtime dir) —
        # this module's contract is best-effort read-only counting, so a
        # failed enumeration counts nothing rather than crashing the
        # delete-confirm flow (Codex review of #129).
        try:
            state_paths = state_files()
        except OSError:
            state_paths = []
    for path in state_paths:
        for record in _read_list(path, "watchers"):
            if record.get("rule_name") == rule_name:
                records += 1
                name = record.get("watcher_name")
                if isinstance(name, str) and name:
                    watcher_names.add(name)
                connector, room_id = record.get("connector"), record.get("room_id")
                if isinstance(connector, str) and isinstance(room_id, str) and room_id:
                    room_keys.add((connector, room_id))

    jobs = 0
    if watcher_names or room_keys:
        for job in _read_list(jobs_file, "jobs"):
            # A COMPLETED job no longer fires — it sits in jobs.json only
            # until the TTL purge (JobStore.list_jobs excludes them by
            # default for the same reason). Counting one here would make
            # the delete warning claim a job is stranded and keeps running
            # when nothing will ever run again (Codex review of #129).
            # Active AND paused jobs both count: paused is an operator
            # choice that resuming re-arms.
            # CANCELLED is terminal too: hidden from `schedule list` by default,
            # never fires without an explicit resume, purged after the same TTL.
            # Counting one told the operator a job "keeps running" that the
            # gateway had already stopped (Codex, PR #140).
            if job.get("status") in (JobStatus.COMPLETED.value, JobStatus.CANCELLED.value):
                continue
            # isinstance first: a non-string `watcher` (a list or mapping in
            # a hand-edited jobs.json) is UNHASHABLE, so the membership test
            # raised TypeError straight out of this function and crashed the
            # rule-delete confirmation — the exact opposite of the
            # best-effort contract stated above (Codex review of #129,
            # round 10).
            watcher = job.get("watcher")
            by_handle = isinstance(watcher, str) and watcher in watcher_names
            connector, room_id = job.get("connector"), job.get("room_id")
            by_room = (isinstance(connector, str) and isinstance(room_id, str)
                       and (connector, room_id) in room_keys)
            if by_handle or by_room:
                jobs += 1
    return records, jobs


def _read_list(path: Path, key: str) -> list[dict]:
    """`path`'s top-level `key` list, mappings only — [] on any failure."""
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError, RecursionError):
        return []
    if not isinstance(data, dict):
        return []
    items = data.get(key)
    if not isinstance(items, list):
        return []
    return [item for item in items if isinstance(item, dict)]
