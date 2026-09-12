"""CLI entry point for AgentCoop."""

import argparse
import asyncio
import json
import os
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from .paths import RUNTIME_DIR

CONTROL_SOCK = RUNTIME_DIR / "control.sock"

# Default config: the COOP_CONFIG env var first, then ~/.agentcoop/config.yaml.
DEFAULT_CONFIG = os.environ.get("COOP_CONFIG", str(RUNTIME_DIR / "config.yaml"))


def main():
    parser = argparse.ArgumentParser(
        prog="coop",
        description="Standalone service bridging Rocket.Chat rooms to agent sessions",
    )
    sub = parser.add_subparsers(dest="command", help="Available commands")

    # start
    start_p = sub.add_parser("start", help="Start the gateway service")
    start_p.add_argument(
        "--config", default=DEFAULT_CONFIG,
        help="Path to config.yaml (default: $COOP_CONFIG or ~/.agentcoop/config.yaml)",
    )

    # stop
    sub.add_parser("stop", help="Stop the gateway service")

    # restart
    restart_p = sub.add_parser("restart", help="Restart the gateway service")
    restart_p.add_argument(
        "--config", default=DEFAULT_CONFIG,
        help="Path to config.yaml (default: $COOP_CONFIG or ~/.agentcoop/config.yaml)",
    )

    # status
    sub.add_parser("status", help="Show gateway status")

    # list
    list_p = sub.add_parser(
        "list",
        help="List watchers (default: every state except idle)",
        description=(
            "List the watchers the gateway holds state records for.  With no "
            "state flag, shows every state except idle — the watchers an "
            "operator is realistically about to act on."
        ),
    )
    list_p.add_argument(
        "--connector",
        default=None,
        metavar="NAME",
        help="Filter by connector name (default: show watchers across all connectors)",
    )
    # Additive flags rather than a mutually exclusive group: the states compose,
    # and `--active --idle` is a meaningful question.  `--all` is the shorthand
    # for naming every one of them.
    list_p.add_argument(
        "--active", action="store_true", help="Include active watchers"
    )
    list_p.add_argument(
        "--idle",
        action="store_true",
        help="Include idle watchers (known rooms with nothing running)",
    )
    list_p.add_argument(
        "--paused", action="store_true", help="Include paused watchers"
    )
    list_p.add_argument(
        "--failed",
        action="store_true",
        help="Include watchers whose start failed (a record, but nothing running)",
    )
    list_p.add_argument(
        "--all", action="store_true", help="Include every state"
    )

    # pause / resume / reset / expire share one positional and one flag: the
    # name may be a glob, and a glob run wants a way to keep going past a
    # failure (#151).
    def _watcher_target(p: argparse.ArgumentParser) -> None:
        p.add_argument(
            "watcher_name",
            help="Watcher name as shown by 'list', or a glob over names "
                 "('*' and '?'; e.g. 'mm-*', '*:nest', '*') — quote it so the "
                 "shell does not expand it. A glob acts on every matching "
                 "watcher, one at a time, and ends with a summary line",
        )
        p.add_argument(
            "--force", action="store_true",
            help="With a glob: keep going after a watcher fails instead of "
                 "aborting the run (the summary still reports the failure and "
                 "the exit code is still non-zero)",
        )

    # pause
    pause_p = sub.add_parser("pause", help="Pause a watcher (stops processing messages)")
    _watcher_target(pause_p)

    # resume
    resume_p = sub.add_parser("resume", help="Resume a paused watcher")
    _watcher_target(resume_p)

    # reset
    reset_p = sub.add_parser(
        "reset",
        help="Reset a watcher: clear runtime state and start a fresh session",
    )
    _watcher_target(reset_p)

    # expire
    expire_p = sub.add_parser(
        "expire",
        help="Expire a rule-derived watcher now: clear its session and reclaim "
             "its record and files (overrides pause, audibly). Scheduled jobs "
             "are KEPT — the room's next message, or the job itself, recreates "
             "the watcher. Refused on connectors that receive no unsolicited "
             "messages (voice, script): only a restart or a scheduled job would "
             "bring the watcher back — use 'reset' there",
    )
    _watcher_target(expire_p)

    # onboard
    onboard_p = sub.add_parser(
        "onboard",
        help="Interactive setup wizard (run after install or to update config)",
    )
    onboard_p.add_argument(
        "--repo-path",
        default=None,
        help="Path to the AgentCoop repo (stored in install metadata)",
    )

    # upgrade
    sub.add_parser("upgrade", help="Upgrade AgentCoop to the latest version")

    # send
    send_p = sub.add_parser("send", help="Send a message to a room")
    send_p.add_argument("room", help="Room name or room ID")
    send_p.add_argument(
        "message",
        nargs="*",
        help='Inline message text (joined with spaces); use "-" to read from stdin',
    )
    send_p.add_argument("--file", default=None, metavar="PATH", help="Read message text from a file")
    send_p.add_argument(
        "--attach", default=None, metavar="PATH",
        help="Upload a file attachment (message/--file/stdin become optional caption)",
    )
    send_p.add_argument(
        "--connector",
        default=None,
        metavar="NAME",
        help=(
            "Connector to send through. Optional when exactly one is "
            "configured; REQUIRED when there are several — the daemon refuses "
            "to guess rather than picking one"
        ),
    )

    # fetch-history
    fh_p = sub.add_parser(
        "fetch-history",
        help="Fetch channel history on-demand (for agent mid-session use)",
    )
    fh_p.add_argument("--watcher", required=True, metavar="NAME",
                      help="Watcher name (from Coop Session Identity context)")
    fh_p.add_argument("--count", type=int, default=50, metavar="N",
                      help="Max messages to fetch (default: 50; server cap: max_fetch_count)")
    fh_p.add_argument("--before", default=None, metavar="TS",
                      help="ISO 8601 exclusive upper-bound timestamp — fetch messages older than this")
    fh_p.add_argument("--after", default=None, metavar="TS",
                      help="ISO 8601 inclusive lower-bound timestamp — fetch messages from this point forward")
    fh_p.add_argument("--verbatim", type=int, default=15, metavar="N",
                      help="Last N messages kept verbatim; older messages condensed (default: 15)")

    # instructions
    instructions_p = sub.add_parser(
        "instructions",
        help="Print bundled AgentCoop instruction docs by name",
    )
    instructions_p.add_argument(
        "name",
        choices=_instruction_names(),
        help="Instruction document to print",
    )

    # config (sub-subcommands)
    config_p = sub.add_parser(
        "config",
        help="Interactive config TUI (or a subcommand — see below)",
    )
    # NOTE: dest is deliberately NOT "config"/"lint" for either flag below —
    # config_validate_p also defines its own --config/--lint (so
    # 'config validate --config X --lint' keeps working exactly as before).
    # If both wrote to the same attribute, argparse would apply
    # config_validate_p's own default over whatever this parent parser had
    # already parsed whenever the flag isn't repeated after 'validate' —
    # silently discarding a value the user gave before the subcommand.
    # Separate dest names sidestep that collision entirely. (--lint had the
    # exact same bug until this comment was extended to cover it too — it
    # was fixed for --config only, missed here, then caught in review.)
    config_p.add_argument(
        "--config", dest="config_path_for_tui", default=DEFAULT_CONFIG,
        help="Path to config.yaml (default: $COOP_CONFIG or ~/.agentcoop/config.yaml)",
    )
    config_p.add_argument(
        "--lint", dest="lint_for_tui", action="store_true",
        help="Also flag values that just restate a built-in default or "
             "duplicate a value inherited from a *_templates entry",
    )
    config_sub = config_p.add_subparsers(dest="config_cmd", help="Config subcommands")

    config_validate_p = config_sub.add_parser(
        "validate",
        help="Validate config.yaml without starting the daemon",
    )
    config_validate_p.add_argument(
        "--config", default=DEFAULT_CONFIG,
        help="Path to config.yaml (default: $COOP_CONFIG or ~/.agentcoop/config.yaml)",
    )
    config_validate_p.add_argument(
        "--lint", action="store_true",
        help="Also flag values that just restate a built-in default or "
             "duplicate a value inherited from a *_templates entry",
    )
    config_validate_p.add_argument(
        "--json", action="store_true",
        help="Emit the findings as a JSON document instead of text",
    )

    config_reload_p = config_sub.add_parser(
        "reload",
        help="Apply config.yaml changes to the running daemon (validate, diff, "
             "restart only what changed, reconcile every record)",
    )
    config_reload_p.add_argument(
        "--config", default=DEFAULT_CONFIG,
        help="Path to config.yaml (default: $COOP_CONFIG or ~/.agentcoop/config.yaml)",
    )
    config_reload_p.add_argument(
        "--dry-run", action="store_true",
        help="Print the plan and change nothing; with the daemon stopped, print "
             "the plan the next start will execute",
    )
    config_reload_p.add_argument(
        "--json", action="store_true",
        help="Emit the plan as a JSON document instead of text",
    )

    config_show_p = config_sub.add_parser(
        "show",
        help="Print the resolved configuration's digest and its flattened, "
             "secret-redacted contents; warn if the running daemon differs",
    )
    config_show_p.add_argument(
        "--config", default=DEFAULT_CONFIG,
        help="Path to config.yaml (default: $COOP_CONFIG or ~/.agentcoop/config.yaml)",
    )
    config_show_p.add_argument(
        "--json", action="store_true",
        help="Emit the digest and the redacted config as a JSON document",
    )

    config_migrate_env_p = config_sub.add_parser(
        "migrate-env",
        help="One-time: fold .env secrets into config.yaml as literal values, then remove .env",
    )
    config_migrate_env_p.add_argument(
        "--config", default=DEFAULT_CONFIG,
        help="Path to config.yaml (default: $COOP_CONFIG or ~/.agentcoop/config.yaml)",
    )

    # schedule (sub-subcommands)
    schedule_p = sub.add_parser("schedule", help="Manage scheduled agent tasks")
    schedule_sub = schedule_p.add_subparsers(dest="schedule_cmd", help="Schedule subcommands")

    # schedule create
    sched_create_p = schedule_sub.add_parser("create", help="Create a new scheduled task")
    sched_create_p.add_argument("watcher", help="Watcher name as defined in config.yaml")
    sched_create_p.add_argument("message", help="Message text to inject into the agent session")
    sched_create_p.add_argument(
        "--every",
        default=None,
        metavar="INTERVAL",
        help="Recurrence interval: 30m, 1h, 6h, 1d, 1w. Use --starting to set a time anchor.",
    )
    sched_create_p.add_argument(
        "--starting",
        default=None,
        metavar="TIME",
        help=(
            "Time anchor / start time. With --every: sets the first run time and (for 1d/1w) "
            "pins the cron time-of-day (e.g. '09:00', 'Mon 10:00', 'Apr 15 09:00'). "
            "Without --every: specific datetime for a one-shot task (e.g. '2026-04-10 15:30'). "
            "Accepts smart partial inputs: '09:00' (today/tomorrow), 'Apr 15 09:00', "
            "'04-15 09:00', 'Mon 09:00', or '2026-05-01 09:00' (explicit full datetime)."
        ),
    )
    sched_create_p.add_argument(
        "--times",
        type=int,
        default=0,
        metavar="N",
        help="Number of times to run (0 = forever, default: 0)",
    )
    sched_create_p.add_argument(
        "--tz",
        default=None,
        metavar="TIMEZONE",
        help="IANA timezone (e.g. 'Asia/Taipei', 'America/New_York', 'UTC'). "
             "If omitted, a --starting time is read in the server's local timezone; "
             "the connector's timezone setting applies only to schedules with no --starting.",
    )

    # schedule list
    sched_list_p = schedule_sub.add_parser("list", help="List scheduled tasks")
    sched_list_p.add_argument(
        "--connector",
        default=None,
        metavar="NAME",
        help="Filter by connector name",
    )
    sched_list_p.add_argument(
        "--all",
        action="store_true",
        dest="include_completed",
        help="Also show recently completed or cancelled tasks (within the TTL window)",
    )

    # schedule delete
    sched_delete_p = schedule_sub.add_parser("delete", help="Delete a scheduled task")
    sched_delete_p.add_argument("job_id", help="Job ID (e.g. acg-a3f2b1c0)")

    # schedule pause
    sched_pause_p = schedule_sub.add_parser("pause", help="Pause a scheduled task")
    sched_pause_p.add_argument("job_id", help="Job ID")

    # schedule resume
    sched_resume_p = schedule_sub.add_parser("resume", help="Resume a paused scheduled task, or restore a cancelled one")
    sched_resume_p.add_argument("job_id", help="Job ID")

    # schedule migrate
    schedule_sub.add_parser(
        "migrate",
        help="Bring jobs.json up to the current schema (run after upgrading)",
        description=(
            "Records what each scheduled job needs to keep working after an "
            "upgrade. Safe to re-run: a job that is already up to date is left "
            "alone, and nothing is ever guessed — a job whose room cannot be "
            "identified is reported and left exactly as it was.\n\n"
            "Run it BEFORE renaming any rooms. The migration finds each job's "
            "room through its watcher name, and a name that has moved to a "
            "different room would point the job at the wrong one."
        ),
    )

    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        sys.exit(1)

    if args.command == "start":
        from .daemon import is_running, start_daemon
        # Asked here as well as inside start_daemon(): with only the daemon-side
        # check, a bare `start` while a gateway runs from some OTHER config path
        # reported whatever was wrong with the default config instead of "already
        # running". The daemon keeps its own check, which is the one that closes
        # the race between this line and the fork.
        running, pid = is_running()
        if running:
            print(f"Gateway already running (pid={pid})")
            sys.exit(1)
        _validate_or_exit(args.config)
        start_daemon(args.config)

    elif args.command == "stop":
        from .daemon import stop_daemon
        stop_daemon()

    elif args.command == "restart":
        from .daemon import start_daemon, stop_daemon
        # Before stop_daemon(), not after: validating inside the start half
        # would stop a healthy running gateway and then refuse to restart it,
        # leaving the operator worse off than before the command.
        _validate_or_exit(args.config, stops_a_running_gateway=True)
        stop_daemon()
        start_daemon(args.config)

    elif args.command == "status":
        from .daemon import LOG_FILE, PID_FILE, is_running
        running, pid = is_running()
        if running:
            # Get uptime
            import time
            pid_mtime = PID_FILE.stat().st_mtime
            uptime_secs = int(time.time() - pid_mtime)
            hours, remainder = divmod(uptime_secs, 3600)
            minutes, secs = divmod(remainder, 60)
            uptime_str = f"{hours}h {minutes}m {secs}s"

            print(f"Gateway:  running (pid={pid})")
            print(f"Uptime:   {uptime_str}")
            print(f"PID file: {PID_FILE}")
            print(f"Log file: {LOG_FILE}")

            # Get watcher count from daemon
            try:
                # Explicitly every state, not the `list` default.  `status`
                # answers "what does this daemon know about"; `list` answers
                # "what is an operator about to act on".  Inheriting the default
                # would silently drop idle rooms from a count that reads as a
                # total.
                result = _send_command({"cmd": "list", "states": _ALL_STATES})
                if result["ok"]:
                    count = len(result.get("data", []))
                    print(f"Watchers: {count}")
            except SystemExit:
                print("Watchers: (unable to query)")
            # The active configuration (#144): what the daemon loaded and
            # when, so an operator can tell whether a reload took effect —
            # and any section a reload could not bring back.
            try:
                shown = _send_command({"cmd": "config-show", "include_config": False})
                if shown.get("ok"):
                    _print_active_config(shown)
            except SystemExit:
                print("Config:   (unable to query)")
        else:
            print("Gateway:  not running")

    elif args.command == "list":
        cmd_data = {"cmd": "list"}
        if args.connector is not None:
            cmd_data["connector"] = args.connector
        states = _requested_states(args)
        if states is not None:
            cmd_data["states"] = states
        result = _send_command(cmd_data)
        watchers = result.get("data", [])
        connector_errors = result.get("errors", [])
        if watchers:
            _print_watcher_table(watchers)
        elif not connector_errors and result["ok"]:
            # `result["ok"]` matters: an unknown --connector comes back as a
            # hard failure with no `errors` list, and "no watchers, try --all"
            # is a substantive answer to a query that never ran.
            # Says which question was asked, because the default excludes idle:
            # "none" and "none you asked about" are different answers.  The
            # default branch names the excluded state rather than the
            # included ones, so it survives a fourth state being added.  The
            # argparse help below does NOT: it enumerates, and it has to be
            # edited whenever `StateFilter.OPERABLE` changes.
            if states:
                print(f"No {'/'.join(states)} watchers")
            else:
                print("No watchers (idle ones are hidden by default — use --all)")
        # Surface per-connector failures (partial failure case)
        for ce in connector_errors:
            print(
                f"Warning: connector '{ce['connector']}' failed to list watchers: {ce['error']}",
                file=sys.stderr,
            )
        if not result["ok"] and not connector_errors:
            # Hard failure (e.g. unknown connector specified by --connector)
            print(f"Error: {result.get('error')}", file=sys.stderr)
            sys.exit(1)
        if connector_errors:
            sys.exit(1)

    elif args.command in _LIFECYCLE_VERBS:
        _run_lifecycle_verb(args)

    elif args.command == "onboard":
        from .onboard import run_onboard
        run_onboard(repo_path=Path(args.repo_path) if args.repo_path else None)

    elif args.command == "upgrade":
        from .upgrade import run_upgrade
        run_upgrade()

    elif args.command == "send":
        _run_send(args)

    elif args.command == "fetch-history":
        _run_fetch_history(args)

    elif args.command == "instructions":
        _run_instructions(args)

    elif args.command == "schedule":
        _run_schedule(args)

    elif args.command == "config":
        _run_config(args)


_LIFECYCLE_VERBS = ("pause", "resume", "reset", "expire")

# Reset stops and restarts the agent process and injects context (an agent
# round-trip). That can take minutes for a slow agent (OpenCode startup +
# injection), so its per-command wait is 5 minutes, not the default 60 s.
_LIFECYCLE_TIMEOUT = {"reset": 300.0}

# Present participle per verb, for the batch path's per-watcher lines
# ("Resuming watcher 'x'…" / "Done resuming watcher 'x'"). The single-name
# success lines are the historical ones and stay byte-identical — see
# `_single_success_line`.
_LIFECYCLE_WORDS = {
    "pause": "pausing",
    "resume": "resuming",
    "reset": "resetting",
    "expire": "expiring",
}

# A verb that would be a no-op on a watcher in this state is SKIPPED by the
# batch path, before any command is sent: `resume` is for paused watchers, so
# an active or idle match is "not paused — skipped"; `pause` on an already
# paused one likewise. Both count as succeeded — the watcher is already where
# the verb would leave it. Owner's call on #151 (resume); pause is the mirror.
# Keyed on the STATE column of the same listing the match set came from, and
# deliberately NOT re-checked at the watcher's turn: the run is "list, filter,
# loop", not a transaction (owner, #152). A state that changed in between
# fails at its turn or is skipped on the snapshot; `list` afterwards is the
# operator's confirmation step.
_SKIP_WHEN = {
    "resume": (lambda state: state != "paused", "is not paused"),
    "pause": (lambda state: state == "paused", "is already paused"),
}

# Neither half of a watcher name can carry one of these: the room label's
# alphabet is [A-Za-z0-9._-] (watcher_manager._LABEL_SAFE), and config load
# refuses a connector name containing any of them (config.py, next to the ':'
# rule). So a name containing one is a pattern, unambiguously.
_GLOB_CHARS = frozenset("*?[")


def _single_success_line(verb: str, name: str) -> str:
    if verb == "expire":
        # NOT "scheduled jobs reclaimed" — expire does not touch them, and
        # this is the success line an operator actually reads. It said the
        # opposite of the `--help` two hundred lines up, which was fixed in
        # the same commit that claimed to have swept "all of it
        # operator-facing". Found by review.
        return (f"Watcher '{name}' expired — record, session and "
                f"files reclaimed. Its scheduled jobs are kept; the room's "
                f"next message, or a job's own next run, recreates the "
                f"watcher.")
    past = {"pause": "paused", "resume": "resumed", "reset": "reset"}[verb]
    return f"Watcher '{name}' {past}"


def _run_lifecycle_verb(args) -> None:
    """pause / resume / reset / expire — one name, or a glob over names (#151).

    A literal name is the path that always existed: one command, one line,
    exit 1 on refusal. A glob is expanded HERE, once, at the operator
    boundary — `list` every state, `fnmatchcase` on the NAME column — and the
    verb is then sent per matched name exactly as the literal form sends it,
    so the daemon protocol and the routing rule (§2.8) are untouched. The
    result is by construction what `list --all`, a filter on NAME and a loop
    over the matches would do.
    """
    verb = args.command
    target = args.watcher_name
    if not any(c in _GLOB_CHARS for c in target):
        result = _send_command({"cmd": verb, "watcher_name": target},
                               timeout=_LIFECYCLE_TIMEOUT.get(verb, 60.0))
        if result["ok"]:
            print(_single_success_line(verb, target))
        else:
            print(f"Error: {result.get('error')}", file=sys.stderr)
            sys.exit(1)
        return
    _run_lifecycle_glob(verb, target, force=args.force)


def _run_lifecycle_glob(verb: str, pattern: str, *, force: bool) -> None:
    import fnmatch

    listing = _send_command({"cmd": "list", "states": list(_ALL_STATES)})
    # A partial listing is refused outright: acting on "the watchers the
    # working connectors could see" is not what the operator asked for, and
    # nothing has been touched yet, so aborting here costs nothing.
    if not listing.get("ok"):
        for ce in listing.get("errors", []):
            print(f"[ERROR] connector '{ce['connector']}' failed to list watchers: "
                  f"{ce['error']}", file=sys.stderr)
        if "errors" not in listing:
            print(f"[ERROR] {listing.get('error')}", file=sys.stderr)
        print(f"[ERROR] Cannot {verb} '{pattern}': the watcher list is incomplete, "
              f"nothing was done.", file=sys.stderr)
        sys.exit(1)

    # `fnmatchcase`, not `fnmatch`: the latter goes through os.path.normcase,
    # which folds case on Windows (identity on POSIX), and names are
    # case-preserving — a pattern must match the same rows on every OS. Names
    # are unique across connectors, so the set() is only insurance against a
    # duplicate row.
    state_of = {
        w.get("watcher_name", ""): w.get("state", "")
        for w in listing.get("data", [])
        if fnmatch.fnmatchcase(w.get("watcher_name", ""), pattern)
    }
    names = sorted(state_of)

    word = _LIFECYCLE_WORDS[verb]
    skip_if, skip_reason = _SKIP_WHEN.get(verb, (lambda state: False, ""))
    succeeded = failed = 0
    aborted_at: int | None = None
    for i, name in enumerate(names):
        if skip_if(state_of[name]):
            print(f"Watcher '{name}' {skip_reason} — skipped")
            succeeded += 1
            continue
        # flush: a reset can take minutes, and the point of this line is that
        # it is visible BEFORE the wait, even through a pipe.
        print(f"{word.capitalize()} watcher '{name}'…", flush=True)
        try:
            result = _send_command({"cmd": verb, "watcher_name": name},
                                   timeout=_LIFECYCLE_TIMEOUT.get(verb, 60.0))
        except SystemExit:
            # `_send_command` exits the process on a transport failure — no
            # daemon, no socket, no answer in time — having printed why. Inside
            # a batch that is one watcher's failure, not the run's: the
            # outcome of THIS watcher is unknown (a timed-out reset may still
            # complete), the summary is still owed, and `--force` still means
            # "try the rest". Without it the run aborts like any failure.
            print(f"[ERROR] {word.capitalize()} watcher '{name}': no answer from "
                  f"the daemon — its outcome is unknown, check 'list'.",
                  file=sys.stderr)
            result = None
        if result is None:
            failed += 1
            if not force:
                aborted_at = i + 1
                break
            continue
        if result.get("ok"):
            print(f"Done {word} watcher '{name}'")
            succeeded += 1
        elif result.get("code") == "unknown_watcher":
            # Gone since the match set was collected. Its absence is the state
            # every verb here drives toward or tolerates, so it counts as done.
            print(f"Watcher '{name}' is no longer there — skipped")
            succeeded += 1
        else:
            print(f"[ERROR] {word.capitalize()} watcher '{name}' failed: "
                  f"{result.get('error')}", file=sys.stderr)
            failed += 1
            if not force:
                aborted_at = i + 1
                break

    not_run = len(names) - (aborted_at if aborted_at is not None else len(names))
    if aborted_at is not None:
        print(f"[ERROR] Aborted after the failure above; {not_run} watcher(s) not run "
              f"(use --force to keep going past failures).", file=sys.stderr)
    print(f"For {len(names)} watchers: {succeeded} succeeded, {failed} failed, "
          f"{not_run} not run.")
    if failed:
        sys.exit(1)


# The CLI's own spelling of every state, so `status` and `--all` cannot drift
# apart when a fourth state is added. `StateFilter.ALL` is the authority; this
# list is what a socket client sends, and `parse_state_filter` refuses a name
# the two do not agree on — loudly, which is why the duplication is safe here
# and importing `gateway.core` into the client for one list is not worth it.
_ALL_STATES = ["active", "idle", "paused", "failed"]


def _requested_states(args) -> list[str] | None:
    """Translate the list flags into wire state names; None means "the default".

    Returning None rather than spelling out the default keeps one definition of
    what "default" means, on the server side (``StateFilter.OPERABLE``), instead
    of a second copy here that has to be kept in step.
    """
    if args.all:
        return list(_ALL_STATES)
    chosen = [
        name
        for name, wanted in (
            ("active", args.active),
            ("idle", args.idle),
            ("paused", args.paused),
            ("failed", args.failed),
        )
        if wanted
    ]
    return chosen or None


def _print_watcher_table(watchers: list[dict]) -> None:
    """Print the watcher rows as an aligned table (design §2.3).

    The participants column is not decoration: an opaque group-DM label is only
    acceptable because something else in the same view answers "which group is
    this".  It therefore belongs in the default output rather than behind a
    verbose flag.
    """
    columns = (
        ("NAME", "watcher_name"),
        ("CONNECTOR", "connector"),
        ("ROOM", "room_name"),
        ("ROOM ID", "room_id"),
        ("AGENT", "agent_name"),
        ("STATE", "state"),
        # Kept despite the width: this is the only surface an operator can read
        # a session id from without opening state.<connector>.json, and its
        # remaining use is being pasted into the backend's own resume command.
        ("SESSION", "session_id"),
        ("PARTICIPANTS", "participants"),
    )
    rows = []
    for w in watchers:
        row = []
        for _, key in columns:
            value = w.get(key, "")
            if isinstance(value, list):
                # `str(v)`, not bare join: the loader refuses non-string
                # elements, but a formatter is the wrong place to discover that
                # — joining an int raises and takes down the whole table, every
                # connector's rows with it, rather than misrendering one cell.
                value = ", ".join(str(v) for v in value)
            row.append(str(value) if value else "—")
        rows.append(row)

    widths = [
        max([len(header)] + [len(row[i]) for row in rows])
        for i, (header, _) in enumerate(columns)
    ]

    def _line(cells: list[str]) -> str:
        # rstrip drops the last cell's ljust padding; no cell is ever empty
        # (an absent value renders as an em dash), so this is only cosmetic.
        return "  ".join(c.ljust(w) for c, w in zip(cells, widths)).rstrip()

    print(_line([header for header, _ in columns]))
    for row in rows:
        print(_line(row))


def _run_config(args) -> None:
    """Handle 'config' subcommands — or, with no subcommand, launch the
    interactive config TUI (gateway/configtool/)."""
    if not getattr(args, "config_cmd", None):
        from .configtool import run_app
        sys.exit(run_app(args.config_path_for_tui, lint=args.lint_for_tui))

    if args.config_cmd == "validate":
        _run_config_validate(args)
    elif args.config_cmd == "reload":
        _run_config_reload(args)
    elif args.config_cmd == "show":
        _run_config_show(args)
    elif args.config_cmd == "migrate-env":
        _run_config_migrate_env(args)
    else:
        print(f"Unknown config subcommand: {args.config_cmd}", file=sys.stderr)
        sys.exit(1)


def _validate_or_exit(config_path: str, *, stops_a_running_gateway: bool = False) -> None:
    """Refuse to start a gateway on a config `config validate` rejects.

    `start` used to hand the path straight to the daemon, which loads it with
    `GatewayConfig.from_file()` — parsing and dataclass construction, none of
    the cross-checks (state orphans, room/session uniqueness, rule shadowing)
    that `validate_config()` runs. A config the operator could see rejected by
    `coop config validate` still started a gateway.

    The condition mirrors the reload path's (`GatewayService._handle_config_reload`)
    exactly: errors refuse, warnings and lint findings do not. Callers run this
    BEFORE forking (and, for `restart`, before stopping the running daemon), so
    the errors land on the terminal rather than in the log the daemon redirects
    into.

    **A pending `.env` migration is deferred for `start`, refused for `restart`.**
    `validate_config()` reads the raw document, where an unmigrated `${RC_URL}`
    is still a literal string that fails the URL check — so validating here would
    reject every config the daemon is about to migrate, and
    `migrate_env_to_config()` (which runs inside the daemon, after the fork)
    could never run.

    For `start` the answer is to defer: nothing is running to damage, so the
    worst case is one boot validated the way it was before this change —
    `from_file()` alone — and the daemon's own fail-closed migration handles the
    rest. The migration then moves `.env` away, so every later start validates
    in full.

    For `restart` deferring is not safe, because `stop_daemon()` runs next. A
    config that cannot load would take a healthy gateway down and leave it down,
    which is the outage the validate-before-stop order exists to prevent. So
    `restart` refuses instead, and names the way through.
    """
    from .config_migrate import has_pending_migration
    from .config_validate import validate_config

    if has_pending_migration(config_path):
        if not stops_a_running_gateway:
            return
        print(
            f"[ERROR] {config_path}: a .env file still sits beside it, so its "
            f"secrets have not been folded in yet — refusing to restart, because "
            f"stopping the gateway before that migration is what would leave it "
            f"down.\n"
            f"  [ERROR] Complete the migration first: 'coop config migrate-env', "
            f"then 'coop restart'. (A 'coop stop' followed by 'coop start' does "
            f"it too — the daemon migrates on its own at startup.)\n"
            f"  [ERROR] If that .env is a leftover you no longer need, delete it.",
            file=sys.stderr,
        )
        sys.exit(1)

    try:
        result = validate_config(config_path)
    except Exception as exc:
        # Malformed YAML reaches here as a `yaml.YAMLError` from `collect_config()`;
        # a missing file as `FileNotFoundError`. Before this preflight existed the
        # daemon caught both and reported a controlled failure, so letting them
        # escape would regress a traceback onto the most ordinary config mistake
        # there is. Converting them here rather than inside `validate_config()`
        # keeps the change to the path this increment owns — `config validate`
        # and `config show` raise on the same input today, and that is theirs.
        print(f"[ERROR] {config_path}: {type(exc).__name__}: {exc}", file=sys.stderr)
        sys.exit(1)

    if result.ok and result.config is not None:
        return

    print(f"[ERROR] {config_path}: {len(result.errors)} error(s) — not starting",
          file=sys.stderr)
    for err in result.errors:
        print(f"  [ERROR] {err}", file=sys.stderr)
    sys.exit(1)


def _run_config_validate(args) -> None:
    """Handle 'config validate': load + cross-check config.yaml, no daemon needed."""
    from .config_validate import validate_config

    result = validate_config(args.config, lint=args.lint)

    if getattr(args, "json", False):
        print(json.dumps(result.to_dict(), indent=2))
        sys.exit(0 if result.ok else 1)

    if result.errors:
        print(f"✗ {args.config}: {len(result.errors)} error(s)", file=sys.stderr)
        for err in result.errors:
            print(f"  - {err}", file=sys.stderr)
    else:
        summary = f"✓ {args.config}: valid — {result.watcher_count} watcher(s)"
        if result.entry_count and result.entry_count != result.watcher_count:
            summary += f" (expanded from {result.entry_count} entries)"
        print(summary)

    for warning in result.warnings:
        print(f"  ⚠ {warning}")

    if args.lint:
        if result.lint_findings:
            print(f"\n{len(result.lint_findings)} lint finding(s):")
            for finding in result.lint_findings:
                print(f"  - {finding}")
        else:
            print("\nlint: no redundant defaults found")

    if not result.ok:
        sys.exit(1)


def _print_active_config(shown: dict) -> None:
    """The `status` lines for the active configuration (#144)."""
    digest = shown.get("digest") or ""
    print(f"Config:   {digest[:12]} (loaded {shown.get('loaded_at') or '?'})")
    for d in shown.get("degraded") or []:
        print(f"[ERROR] Degraded: {d.get('kind')} '{d.get('name')}' — {d.get('error')}")
    if shown.get("reloading"):
        print("Reload:   in progress")


def _emit_plan(plan, as_json: bool) -> None:
    if as_json:
        print(json.dumps(plan.to_dict(), indent=2))
    else:
        print(plan.render())


def _run_config_reload(args) -> None:
    """Handle 'config reload [--dry-run] [--json]' (#144).

    Three situations, told apart deliberately:

    * the daemon is running — the request goes to it; the file is read THERE,
      once, and the plan comes back as data;
    * the daemon is not running — `--dry-run` computes the record-level plan
      offline from the state files (it is the plan the next start executes);
      without `--dry-run` the plan is printed and the command refuses, because
      nothing is running to apply it to;
    * the daemon appears to be running but its socket cannot be reached — an
      error, never a silent fall-back to the offline plan: a daemon in that
      state is the thing to fix first.
    """
    from .config_validate import finding_to_dict, validate_config
    from .daemon import is_running
    from .reload_plan import ReloadPlan, boot_plan

    running, pid = is_running()
    if running:
        response = _send_command(
            {"cmd": "config-reload", "dry_run": bool(args.dry_run),
             "config_path": os.path.abspath(args.config)},
            timeout=600.0,
        )
        if "exit_code" not in response:
            # Not a plan: the control server itself refused the request. Still
            # a document under --json — the failure path is where a script
            # most needs `ok` and `error` as data.
            error = response.get("error", "unknown error")
            if args.json:
                print(json.dumps(ReloadPlan.refused(error, dry_run=bool(args.dry_run)).to_dict(),
                                 indent=2))
            else:
                print(f"Error: {error}", file=sys.stderr)
            sys.exit(1)
        plan = ReloadPlan.from_dict(response)
        _emit_plan(plan, args.json)
        sys.exit(plan.exit_code)

    # Offline: validate, then the next start's plan from the state files.
    result = validate_config(args.config)
    if not result.ok or result.config is None:
        plan = ReloadPlan.refused(
            f"{args.config}: {len(result.errors)} error(s) — nothing changed",
            dry_run=bool(args.dry_run),
            findings=[finding_to_dict(f) for f in result.findings if f.severity != "lint"],
        )
        _emit_plan(plan, args.json)
        sys.exit(1)
    try:
        plan = boot_plan(result.config)
    except Exception as exc:
        print(f"Error: could not read the persisted state: {exc}", file=sys.stderr)
        sys.exit(1)
    plan.findings = [finding_to_dict(f) for f in result.findings if f.severity != "lint"]
    if not args.dry_run:
        # The plan is still worth seeing — it is what `start` will do.
        plan.dry_run = False
        plan.error = ("the daemon is not running, so there is nothing to apply to — the plan "
                      "above is what the next start executes. Start it with: "
                      "coop start")
        if args.json:
            # Refused, with the plan body kept: `ok: false` plus `changes` and
            # `watchers` is what text mode shows too (plan, then the error).
            plan.ok = False
            _emit_plan(plan, True)
        else:
            print(plan.render())
            print(f"Error: {plan.error}", file=sys.stderr)
        sys.exit(1)
    _emit_plan(plan, args.json)
    sys.exit(0)


def _run_config_show(args) -> None:
    """Handle 'config show [--json]' (#144).

    Prints the SHA-256 digest of the RESOLVED file and its flattened contents
    with secrets redacted. When the daemon is running, its active digest is
    fetched and a mismatch is reported — "I edited but forgot to reload" made
    visible. Never contacts the daemon when it is not running.
    """
    from .config_diff import config_digest, flatten_config, redacted_config
    from .config_validate import validate_config
    from .daemon import is_running

    result = validate_config(args.config)
    if not result.ok or result.config is None:
        if args.json:
            print(json.dumps(result.to_dict(), indent=2))
        else:
            print(f"[ERROR] {args.config}: {len(result.errors)} error(s)", file=sys.stderr)
            for err in result.errors:
                print(f"  [ERROR] {err}", file=sys.stderr)
        sys.exit(1)
    config = result.config
    digest = config_digest(config)

    active: dict | None = None
    running, _pid = is_running()
    if running:
        shown = _send_command({"cmd": "config-show", "include_config": False})
        if not shown.get("ok"):
            # A running daemon that will not answer is an error, not "no daemon":
            # text mode would otherwise look exactly like an offline show.
            error = f"the daemon refused config-show: {shown.get('error', 'unknown error')}"
            if args.json:
                print(json.dumps({"ok": False, "error": error, "digest": digest,
                                  "config_path": os.path.abspath(args.config)}, indent=2))
            else:
                print(f"[ERROR] {error}", file=sys.stderr)
            sys.exit(1)
        active = {"digest": shown.get("digest"), "loaded_at": shown.get("loaded_at"),
                  "config_path": shown.get("config_path")}
    in_sync = None if active is None else (active["digest"] == digest)

    if args.json:
        print(json.dumps({
            "ok": True,
            "config_path": os.path.abspath(args.config),
            "digest": digest,
            "active": active,
            "in_sync": in_sync,
            "config": redacted_config(config),
        }, indent=2))
    else:
        print(f"Config:  {args.config}")
        print(f"Digest:  {digest}")
        if active is not None:
            print(f"Active:  {active['digest']} (loaded {active['loaded_at']})")
            if not in_sync:
                print("⚠ The running daemon's configuration differs from the file — "
                      "run 'coop config reload' to apply it.")
        print()
        for path, value in flatten_config(config):
            print(f"{path}: {value}")
    sys.exit(0)


def _run_config_migrate_env(args) -> None:
    """Handle 'config migrate-env': the same one-time migration
    gateway/daemon.py's start_daemon() runs automatically on every server
    start — exposed standalone for a manual run, a dry check before
    starting the gateway, or Docker's entrypoint script. No-op (exit 0,
    clear message) if there's no .env to migrate."""
    from .config_migrate import migrate_env_to_config

    try:
        result = migrate_env_to_config(args.config)
    except Exception as e:
        # Broad on purpose, matching gateway/daemon.py's own handling of
        # this exact function: ValueError (unresolvable $VAR), OSError
        # (FileNotFoundError, a PermissionError from env_path.rename()),
        # and yaml.YAMLError (malformed config.yaml, from EditableConfig.
        # load()'s yaml.safe_load()) all need the same clean treatment here
        # — a raw traceback for any of them defeats the point of this
        # command existing as a friendly, standalone entry point.
        print(f"✗ Migration failed: {e}", file=sys.stderr)
        sys.exit(1)

    if not result.migrated:
        print("Nothing to migrate — no .env file found.")
        return

    print(
        f"✓ Migrated {result.ref_count} secret reference(s) from .env into "
        f"{args.config}."
    )
    print(f"  .env moved to {result.env_backup_path}")


def _run_instructions(args) -> None:
    """Print a bundled instruction document without requiring the daemon."""
    from .instructions import read_instruction

    try:
        print(read_instruction(args.name), end="")
    except (OSError, ValueError) as exc:
        print(f"Error: could not read instruction {args.name!r}: {exc}", file=sys.stderr)
        sys.exit(1)


def _instruction_names() -> list[str]:
    from .instructions import instruction_names

    return instruction_names()


def _run_fetch_history(args) -> None:
    """Handle the 'fetch-history' subcommand: pull channel history on-demand.

    Intended for agent use mid-session.  Routes through the control socket
    so the daemon applies its connector's allowlist filter (same security
    boundary as live message processing).

    Output is printed to stdout so the agent reads it as Bash tool output.
    """
    cmd_data: dict = {
        "cmd": "fetch-history",
        "watcher": args.watcher,
        "count": args.count,
        "verbatim": args.verbatim,
    }
    if args.before:
        cmd_data["before_ts"] = args.before
    if args.after:
        cmd_data["after_ts"] = args.after

    result = _send_command(cmd_data)
    if result["ok"]:
        history = result.get("history", "")
        if history:
            print(history)
        else:
            print("[fetch-history] No messages found.")
    else:
        print(f"Error: {result.get('error')}", file=sys.stderr)
        sys.exit(1)


def _run_send(args) -> None:
    """Handle the 'send' subcommand: post a message or upload a file via the control socket.

    Routes through the running daemon's control socket so the send goes through
    the connector abstraction layer instead of coupling directly to a specific
    platform's REST client.
    """
    from pathlib import Path as _Path

    has_inline = bool(args.message)
    is_stdin = has_inline and args.message == ["-"]
    has_file = bool(args.file)

    # Validate mutual exclusions
    if has_inline and not is_stdin and has_file:
        print("Error: cannot use both inline message and --file", file=sys.stderr)
        sys.exit(1)
    if is_stdin and has_file:
        print("Error: cannot use both '-' (stdin) and --file", file=sys.stderr)
        sys.exit(1)
    if not has_inline and not has_file and not args.attach:
        print("Error: provide a message, --file PATH, or --attach PATH", file=sys.stderr)
        sys.exit(1)

    # Validate paths exist before making any network calls
    if has_file and not _Path(args.file).exists():
        print(f"Error: file not found: {args.file}", file=sys.stderr)
        sys.exit(1)
    if args.attach and not _Path(args.attach).exists():
        print(f"Error: attachment not found: {args.attach}", file=sys.stderr)
        sys.exit(1)

    # Read message text
    text = ""
    if is_stdin:
        text = sys.stdin.read()
    elif has_file:
        text = _Path(args.file).read_text()
    elif has_inline:
        text = " ".join(args.message)

    # Build command payload and send through the control socket
    cmd_data: dict = {
        "cmd": "send",
        "room": args.room,
        "text": text,
    }
    if args.attach:
        # Resolve to absolute path so the daemon can find the file
        cmd_data["attachment_path"] = str(_Path(args.attach).resolve())
    if args.connector is not None:
        cmd_data["connector"] = args.connector

    result = _send_command(cmd_data)
    if result["ok"]:
        print("Sent.")
    else:
        print(f"Error: {result.get('error')}", file=sys.stderr)
        sys.exit(1)


def _run_schedule(args) -> None:
    """Handle 'schedule' subcommands."""
    if not hasattr(args, "schedule_cmd") or not args.schedule_cmd:
        print(
            "Usage: coop schedule "
            "{create,list,delete,pause,resume,migrate}"
        )
        sys.exit(1)

    if args.schedule_cmd == "create":
        _run_schedule_create(args)
    elif args.schedule_cmd == "list":
        _run_schedule_list(args)
    elif args.schedule_cmd == "delete":
        _run_schedule_delete(args)
    elif args.schedule_cmd == "pause":
        _run_schedule_pause(args)
    elif args.schedule_cmd == "resume":
        _run_schedule_resume(args)
    elif args.schedule_cmd == "migrate":
        _run_schedule_migrate(args)
    else:
        print(f"Unknown schedule subcommand: {args.schedule_cmd}", file=sys.stderr)
        sys.exit(1)


def _run_schedule_create(args) -> None:
    """Handle 'schedule create': parse interval, build cron, send to daemon."""
    # For one-shot tasks (--starting datetime, no --every), enforce times=1 to prevent
    # the job from re-firing every year (5-field cron has no year field).
    times = args.times
    if args.every is None and times == 0:
        # No recurring interval + default times=0 (forever) → treat as one-shot.
        # This branch only fires when --starting is provided without --every
        # (pure datetime one-shot).  Branch 2 below (relative one-shot via --every
        # Nm/Nh --times 1) does NOT reach here because args.every is not None.
        # The two branches are mutually exclusive: Branch 1 fires when
        # args.starting is not None; Branch 2 fires when args.every is not None
        # and times (after this assignment) is 1.
        times = 1  # default one-shot to exactly 1 run

    cron: str | None = None
    next_run_override: str | None = None
    # Tracks whether we generated a UTC-coordinate one-shot cron.  When True,
    # the daemon must interpret the cron in UTC — not in the server's local
    # timezone — otherwise the offset is double-applied (e.g. UTC-7 shifts the
    # fire time 7 hours into the future instead of N minutes).
    _one_shot_utc_cron = False
    parsed: "_ParsedStarting | None" = None

    # M4: capture now_utc once and reuse across all branches.  Two separate
    # datetime.now(UTC) calls in Branch 1 and Branch 2 could straddle a minute
    # boundary if the process is preempted between them, causing the generated
    # one-shot cron to reflect a minute that is already in the past.
    now_utc = datetime.now(UTC)

    tz_name = args.tz or None

    # ── Branch 1: --starting provided ─────────────────────────────────────────
    if args.starting is not None:
        try:
            parsed = _parse_starting(args.starting, tz_name, now_utc)
        except ValueError as e:
            print(f"Error: {e}", file=sys.stderr)
            sys.exit(1)

        if parsed.was_past:
            print(
                f"Warning: --starting {args.starting!r} was in the past. "
                f"Advancing to next occurrence: {parsed.first_run.isoformat()}",
                file=sys.stderr,
            )

        if args.every is None:
            # One-shot: --starting "2026-04-10 15:30" → specific datetime cron
            fr = parsed.first_run
            cron = f"{fr.minute} {fr.hour} {fr.day} {fr.month} *"
            next_run_override = fr.isoformat()
            _one_shot_utc_cron = True
        else:
            every_lower = args.every.strip().lower()
            # For sub-hourly/sub-daily intervals (Nm, Nh): --starting sets first_run
            # but does NOT change the cron pattern.
            # For 1d / 1w: --starting also anchors the cron time.
            _at_for_cron: str | None = None
            if every_lower in ("1d", "1w"):
                # Build the --at-style string for cron anchoring
                if parsed.dow is not None:
                    _dow_rev = {v: k for k, v in _DOW_MAP.items()}
                    day_name = _dow_rev.get(parsed.dow, parsed.dow)
                    _at_for_cron = f"{day_name.capitalize()} {parsed.hour:02d}:{parsed.minute:02d}"
                else:
                    _at_for_cron = f"{parsed.hour:02d}:{parsed.minute:02d}"

            try:
                cron = _build_cron_expression(args.every, _at_for_cron)
            except ValueError as e:
                print(f"Error: {e}", file=sys.stderr)
                sys.exit(1)

            next_run_override = parsed.first_run.isoformat()

    # ── Branch 2: no --starting, one-shot relative reminders ──────────────────
    # For --every Nm/Nh --times 1 (no --starting), accept ANY positive integer interval
    # (e.g. 7m, 23m, 90m) and compute now + N to generate a one-shot datetime cron.
    elif times == 1 and args.every is not None:
        from datetime import timedelta

        interval_minutes = _parse_one_shot_interval(args.every)
        if interval_minutes is not None:
            target = now_utc + timedelta(minutes=interval_minutes)
            cron = f"{target.minute} {target.hour} {target.day} {target.month} *"
            _one_shot_utc_cron = True

    # ── Branch 3: no --starting, recurring with no time anchor ─────────────────
    if cron is None:
        try:
            cron = _build_cron_expression(args.every, None)
        except ValueError as e:
            print(f"Error: {e}", file=sys.stderr)
            sys.exit(1)

    cmd_data: dict = {
        "cmd": "schedule-create",
        "watcher": args.watcher,
        "message": args.message,
        "cron": cron,
        "times": times,
    }
    if next_run_override is not None:
        cmd_data["next_run"] = next_run_override
    if _one_shot_utc_cron and next_run_override is None:
        # Relative one-shot: cron coordinates are UTC, force UTC timezone.
        # Any --tz flag is intentionally ignored: timezone is irrelevant for
        # relative one-shot reminders ("in 7 minutes" means the same everywhere).
        cmd_data["timezone"] = "UTC"
    elif _one_shot_utc_cron and next_run_override is not None:
        # --starting + no --every: UTC datetime cron
        cmd_data["timezone"] = "UTC"
    elif args.tz:
        cmd_data["timezone"] = args.tz
    elif parsed is not None:
        # --starting was used without explicit --tz: store the resolved timezone
        # (server local) so that 1d/1w cron expressions are interpreted correctly
        # on subsequent runs.
        cmd_data["timezone"] = parsed.tz_str

    result = _send_command(cmd_data)
    if result["ok"]:
        job_id = result.get("job_id", "?")
        next_run = result.get("next_run", "?")
        print(f"Scheduled job created: {job_id}")
        print(f"Next run:              {next_run}")
        if times == 0:
            print(f"Recurrence:            {args.every or 'see cron: ' + cron} (forever)")
        else:
            print(f"Runs:                  {times} time(s)")
    else:
        print(f"Error: {result.get('error')}", file=sys.stderr)
        sys.exit(1)


def _run_schedule_list(args) -> None:
    """Handle 'schedule list': display jobs in a tabular format."""
    import textwrap

    cmd_data: dict = {"cmd": "schedule-list", "include_completed": args.include_completed}
    if args.connector:
        cmd_data["connector"] = args.connector

    result = _send_command(cmd_data)
    if not result["ok"]:
        print(f"Error: {result.get('error')}", file=sys.stderr)
        sys.exit(1)

    jobs = result.get("jobs", [])
    if not jobs:
        print("No scheduled tasks.")
        return

    def _fmt_ts(ts: str | None) -> str:
        """Format an ISO 8601 UTC timestamp for display, stripping the +00:00 suffix."""
        if not ts:
            return "-"
        # Strip trailing +00:00 / Z for readability; header already says (UTC)
        return ts.replace("+00:00", "").replace("Z", "").replace("T", " ")

    # Header
    print(
        f"{'ID':<14}  {'WATCHER':<20}  {'STATUS':<10}  "
        f"{'CRON':<20}  {'RUNS':<12}  {'NEXT RUN (UTC)':<22}  MESSAGE"
    )
    print("-" * 124)

    for j in jobs:
        job_id = j.get("id", "?")
        watcher = j.get("watcher", "?")
        status = j.get("status", "?")
        cron = j.get("cron", "?")
        run_count = j.get("run_count", 0)
        times = j.get("times", 0)
        runs_str = f"{run_count}/∞" if times == 0 else f"{run_count}/{times}"
        # For completed jobs, show completed_at under a different label
        if status == "completed":
            raw_ts = j.get("completed_at")
            next_run_str = f"done {_fmt_ts(raw_ts)}" if raw_ts else "done"
        elif status == "cancelled":
            raw_ts = j.get("cancelled_at")
            next_run_str = f"cancelled {_fmt_ts(raw_ts)}" if raw_ts else "cancelled"
        else:
            next_run_str = _fmt_ts(j.get("next_run"))
        message = textwrap.shorten(j.get("message", ""), width=40, placeholder="…")
        print(
            f"{job_id:<14}  {watcher:<20}  {status:<10}  "
            f"{cron:<20}  {runs_str:<12}  {next_run_str:<22}  {message}"
        )


def _run_schedule_delete(args) -> None:
    result = _send_command({"cmd": "schedule-delete", "job_id": args.job_id})
    if result["ok"]:
        print(f"Scheduled job {args.job_id!r} deleted.")
    else:
        print(f"Error: {result.get('error')}", file=sys.stderr)
        sys.exit(1)


def _run_schedule_migrate(args) -> None:
    """Print what the migration did, per job. Silence would be the failure mode."""
    result = _send_command({"cmd": "schedule-migrate"})
    if not result["ok"]:
        print(f"Error: {result.get('error')}", file=sys.stderr)
        sys.exit(1)

    # "Nothing to do" means no STEP ran — not that the versions match. A file
    # already at the current version can still owe work: `needs_migration`
    # also looks at the jobs, and a live job with no room id re-runs the 1→2
    # step at version 2. Keying this on the version alone hid that run's
    # outcomes — including jobs needing attention — behind "nothing to do",
    # while the startup warning kept firing (Codex, PR #140 round 2).
    if not result.get("steps") and not result.get("outcomes"):
        print(f"jobs.json is already at schema version {result['to_version']} "
              f"— nothing to do.")
        return

    for step in result.get("steps", []):
        print(f"  {step}")
    if result.get("steps"):
        print()

    outcomes = result.get("outcomes", [])
    for outcome in outcomes:
        mark = ("✓" if outcome["changed"]
                else "✗" if outcome.get("needs_attention") else "·")
        print(f"  {mark} {outcome['job_id']}  {outcome['watcher']}")
        print(f"      {outcome['detail']}")

    # The flag, not a substring of the human-readable detail — "already up to
    # date" is not attention-worthy and a reworded sentence must not change a
    # count.
    unresolved = [o for o in outcomes if o.get("needs_attention")]
    print()
    # `stamped`, not `to_version`: the version only moves when nothing was left
    # needing attention, so claiming the file reached `to_version` here would be
    # contradicted by the next startup warning.
    if result.get("stamped"):
        print(f"jobs.json migrated {result['from_version']} → "
              f"{result['to_version']}: {result['changed']} of {len(outcomes)} "
              f"job(s) changed.")
    else:
        print(f"jobs.json is STILL at schema version {result['from_version']}: "
              f"{result['changed']} of {len(outcomes)} job(s) changed, but the "
              f"version does not move while any job needs attention.")
    if unresolved:
        # Named rather than summarised: each of these needs a decision, and the
        # migration deliberately made none of them.
        print(f"{len(unresolved)} job(s) need attention — see the lines above. "
              f"Nothing was guessed for them; they work exactly as before.")
        print("Fix those, then run 'schedule migrate' again.")


def _run_schedule_pause(args) -> None:
    result = _send_command({"cmd": "schedule-pause", "job_id": args.job_id})
    if result["ok"]:
        print(f"Scheduled job {args.job_id!r} paused.")
    else:
        print(f"Error: {result.get('error')}", file=sys.stderr)
        sys.exit(1)


def _run_schedule_resume(args) -> None:
    result = _send_command({"cmd": "schedule-resume", "job_id": args.job_id})
    if result["ok"]:
        next_run = result.get("next_run", "?")
        print(f"Scheduled job {args.job_id!r} resumed.")
        print(f"Next run: {next_run}")
    else:
        print(f"Error: {result.get('error')}", file=sys.stderr)
        sys.exit(1)


def _get_local_tz_name() -> str:
    """Return the server's local IANA timezone name.

    Delegates to the canonical shared implementation in ``gateway.core.tz_utils``
    to avoid duplicating the /etc/localtime parsing logic here.
    """
    from gateway.core.tz_utils import local_iana_timezone
    return local_iana_timezone()


def _advance_by_one_year(candidate: "datetime") -> "datetime":
    """Return candidate advanced by one year, handling Feb 29 in non-leap years.

    ``datetime.replace(year=y+1)`` raises ``ValueError`` when the candidate is
    Feb 29 and the next year is not a leap year.  In that case we search forward
    for the next leap year (guaranteed within 8 years).
    """
    target_year = candidate.year + 1
    for offset in range(8):
        try:
            return candidate.replace(year=target_year + offset)
        except ValueError:
            continue
    # This is mathematically unreachable: leap years occur at least once every 4 years,
    # so within an 8-year search window there is always a valid Feb 29.  The raise
    # exists only as a defensive guard against a bug in the loop itself.
    raise ValueError(
        f"Internal error: _advance_by_one_year could not find a valid date for "
        f"{candidate.strftime('%b %d')} within 8 years — this is a bug, please report it."
    )


@dataclass
class _ParsedStarting:
    """Result of parsing a --starting value."""
    first_run: datetime       # UTC, always future after auto-advance
    hour: int                 # local hour (in the user's tz)
    minute: int               # local minute
    dow: str | None           # cron DOW digit e.g. "1" for Mon, or None
    was_past: bool            # True if the original parsed time was in the past
    tz_str: str               # IANA timezone name actually used (e.g. "America/Los_Angeles")


def _parse_starting(starting_str: str, tz_name: str | None, now_utc: datetime) -> "_ParsedStarting":
    """Parse a --starting value into a _ParsedStarting result.

    Accepts the following formats:
      - "09:00"              → today at 09:00 local; advance to tomorrow if past
      - "Apr 15 09:00"       → this year Apr 15 at 09:00; advance one year if past
      - "04-15 09:00"        → this year Apr 15 at 09:00 (MM-DD)
      - "Mon 09:00"          → next Monday at 09:00
      - "2026-05-01 09:00"   → explicit full datetime

    All times are interpreted in tz_name (default: UTC).
    first_run is always returned in UTC and is always in the future.
    """
    import re as _re
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

    stripped = starting_str.strip()

    try:
        if tz_name:
            tz = ZoneInfo(tz_name)
            tz_str = tz_name
        else:
            # Fall back to the server's local IANA timezone (not UTC) so that
            # times like "22:38" are interpreted as local wall-clock time.
            # IMPORTANT: use ZoneInfo(tz_str), NOT datetime.now().astimezone().tzinfo.
            # The latter returns a fixed UTC-offset object (e.g. UTC-7) that does not
            # observe DST transitions, so tz and tz_str would silently diverge when
            # the clock changes — schedules could be off by one hour post-transition.
            tz_str = _get_local_tz_name()
            tz = ZoneInfo(tz_str)
    except ZoneInfoNotFoundError:
        tz_str = "UTC"
        tz = ZoneInfo("UTC")

    # Convert now_utc to local time for comparisons
    now_local = now_utc.astimezone(tz)

    # ── Format 1: "HH:MM" ─────────────────────────────────────────────────────
    if _re.fullmatch(r"\d{1,2}:\d{2}", stripped):
        h, m = _parse_hhmm(stripped)
        candidate = now_local.replace(hour=h, minute=m, second=0, microsecond=0)
        was_past = candidate <= now_local
        if was_past:
            from datetime import timedelta
            candidate += timedelta(days=1)
        first_run = candidate.astimezone(UTC)
        return _ParsedStarting(first_run=first_run, hour=h, minute=m, dow=None, was_past=was_past, tz_str=tz_str)

    # ── Format 2: "Mon 09:00" (day-of-week + time) ─────────────────────────────
    dow_match = _re.fullmatch(r"([A-Za-z]{3})\s+(\d{1,2}:\d{2})", stripped)
    if dow_match:
        day_str, time_str = dow_match.group(1), dow_match.group(2)
        dow_digit = _DOW_MAP.get(day_str.lower())
        if dow_digit is None:
            raise ValueError(
                f"Unknown day of week {day_str!r}. Use: Mon, Tue, Wed, Thu, Fri, Sat, Sun."
            )
        h, m = _parse_hhmm(time_str)
        # Find next occurrence of this weekday
        # cron dow: 0=Sun, 1=Mon, ..., 6=Sat
        # Python weekday: 0=Mon, ..., 6=Sun  →  convert
        cron_to_python_dow = {"0": 6, "1": 0, "2": 1, "3": 2, "4": 3, "5": 4, "6": 5}
        target_python_dow = cron_to_python_dow[dow_digit]
        from datetime import timedelta
        # Start from today
        candidate = now_local.replace(hour=h, minute=m, second=0, microsecond=0)
        days_ahead = (target_python_dow - now_local.weekday()) % 7
        candidate += timedelta(days=days_ahead)
        was_past = candidate <= now_local
        if was_past:
            candidate += timedelta(days=7)
        first_run = candidate.astimezone(UTC)
        return _ParsedStarting(first_run=first_run, hour=h, minute=m, dow=dow_digit, was_past=was_past, tz_str=tz_str)

    # ── Format 3: "Apr 15 09:00" (month-name day time) ─────────────────────────
    month_name_match = _re.fullmatch(
        r"([A-Za-z]{3})\s+(\d{1,2})\s+(\d{1,2}:\d{2})", stripped
    )
    if month_name_match:
        month_str, day_str, time_str = (
            month_name_match.group(1),
            month_name_match.group(2),
            month_name_match.group(3),
        )
        _MONTH_MAP = {
            "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
            "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
        }
        month_num = _MONTH_MAP.get(month_str.lower())
        if month_num is None:
            raise ValueError(f"Unknown month {month_str!r}.")
        day_num = int(day_str)
        h, m = _parse_hhmm(time_str)
        try:
            candidate = now_local.replace(
                month=month_num, day=day_num, hour=h, minute=m, second=0, microsecond=0
            )
        except ValueError as e:
            raise ValueError(f"Invalid date in --starting: {e}") from e
        was_past = candidate <= now_local
        if was_past:
            candidate = _advance_by_one_year(candidate)
        first_run = candidate.astimezone(UTC)
        return _ParsedStarting(first_run=first_run, hour=h, minute=m, dow=None, was_past=was_past, tz_str=tz_str)

    # ── Format 4: "04-15 09:00" (MM-DD HH:MM) ─────────────────────────────────
    mmdd_match = _re.fullmatch(r"(\d{2})-(\d{2})\s+(\d{1,2}:\d{2})", stripped)
    if mmdd_match:
        month_num, day_num = int(mmdd_match.group(1)), int(mmdd_match.group(2))
        time_str = mmdd_match.group(3)
        h, m = _parse_hhmm(time_str)
        try:
            candidate = now_local.replace(
                month=month_num, day=day_num, hour=h, minute=m, second=0, microsecond=0
            )
        except ValueError as e:
            raise ValueError(f"Invalid date in --starting: {e}") from e
        was_past = candidate <= now_local
        if was_past:
            candidate = _advance_by_one_year(candidate)
        first_run = candidate.astimezone(UTC)
        return _ParsedStarting(first_run=first_run, hour=h, minute=m, dow=None, was_past=was_past, tz_str=tz_str)

    # ── Format 5: "YYYY-MM-DD HH:MM" (explicit full datetime) ─────────────────
    # Unlike partial formats (HH:MM, Mon HH:MM, etc.) which auto-advance to the
    # next occurrence, a full explicit datetime represents a single unambiguous
    # point in time.  If that point is in the past, it's almost certainly a typo
    # — raise an error rather than silently creating a job that fires immediately.
    full_dt_formats = ["%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M", "%Y/%m/%d %H:%M"]
    for fmt in full_dt_formats:
        try:
            naive_dt = datetime.strptime(stripped, fmt)
            candidate = naive_dt.replace(tzinfo=tz)
            first_run = candidate.astimezone(UTC)
            was_past = first_run <= now_utc
            if was_past:
                raise ValueError(
                    f"--starting {starting_str!r} is in the past "
                    f"({first_run.strftime('%Y-%m-%d %H:%M UTC')}). "
                    "Please provide a future datetime."
                )
            return _ParsedStarting(
                first_run=first_run,
                hour=naive_dt.hour,
                minute=naive_dt.minute,
                dow=None,
                was_past=False,
                tz_str=tz_str,
            )
        except ValueError as exc:
            # If we raised the "is in the past" error above, propagate it.
            # Otherwise (strptime format mismatch), try the next format.
            if "is in the past" in str(exc):
                raise
            continue

    raise ValueError(
        f"Cannot parse --starting value {starting_str!r}. "
        "Accepted formats: '09:00', 'Apr 15 09:00', '04-15 09:00', "
        "'Mon 09:00', '2026-05-01 09:00'."
    )


def _parse_one_shot_interval(every: str) -> int | None:
    """Parse an arbitrary interval string for one-shot relative reminders.

    Accepts any positive integer followed by ``m`` (minutes) or ``h`` (hours).
    Returns the total number of minutes, or ``None`` if the format is not
    a simple Nm/Nh expression (e.g. ``"1d"``, ``"1w"`` return ``None`` and
    fall through to ``_build_cron_expression``).

    Unlike ``_INTERVAL_MAP``, this accepts arbitrary values such as ``7m``,
    ``23m``, ``90m``, ``3h`` — because we compute ``now + N`` and generate a
    specific one-shot datetime cron, so cron-alignment is not required.

    Examples::

        _parse_one_shot_interval("7m")   → 7
        _parse_one_shot_interval("23m")  → 23
        _parse_one_shot_interval("90m")  → 90
        _parse_one_shot_interval("2h")   → 120
        _parse_one_shot_interval("1d")   → None  (falls through to _INTERVAL_MAP)
        _parse_one_shot_interval("bad")  → None
    """
    # Cap at 1 year to prevent absurdly far-future one-shot jobs that would be
    # hard to cancel and may not be the user's intent (e.g. "100000h").
    _MAX_ONE_SHOT_MINUTES = 365 * 24 * 60  # 525 600 minutes ≈ 1 year

    import re as _re
    m = _re.fullmatch(r"(\d+)(m|h)", every.strip().lower())
    if not m:
        return None
    value, unit = int(m.group(1)), m.group(2)
    if value <= 0:
        return None
    total_minutes = value if unit == "m" else value * 60
    if total_minutes > _MAX_ONE_SHOT_MINUTES:
        return None  # reject; caller falls through to _build_cron_expression which will error
    return total_minutes

# Interval → (default_cron, description).  default_cron uses * for unset fields;
# --at overrides the time components.  Defined at module level (not inside the
# function) so the dict is not rebuilt on every call.
_INTERVAL_MAP: dict[str, tuple[str, str]] = {
    "1m":  ("* * * * *",    "every minute"),
    "5m":  ("*/5 * * * *",  "every 5 minutes"),
    "10m": ("*/10 * * * *", "every 10 minutes"),
    "15m": ("*/15 * * * *", "every 15 minutes"),
    "30m": ("*/30 * * * *", "every 30 minutes"),
    "1h":  ("0 * * * *",    "every hour"),
    "2h":  ("0 */2 * * *",  "every 2 hours"),
    "3h":  ("0 */3 * * *",  "every 3 hours"),
    "6h":  ("0 */6 * * *",  "every 6 hours"),
    "12h": ("0 */12 * * *", "every 12 hours"),
    "1d":  ("0 9 * * *",    "every day"),     # default 09:00
    "1w":  ("0 9 * * 1",    "every week"),    # default Monday 09:00
}

_DOW_MAP: dict[str, str] = {
    "sun": "0", "mon": "1", "tue": "2", "wed": "3",
    "thu": "4", "fri": "5", "sat": "6",
}


def _build_cron_expression(every: str | None, at: str | None) -> str:
    """Convert ``--every INTERVAL`` + optional ``--at TIME`` to a 5-field cron string.

    Supported intervals:
      - Any ``Nm`` (1 ≤ N ≤ 59): ``*/N * * * *``  e.g. ``2m`` → ``*/2 * * * *``
      - Any ``Nh`` (1 ≤ N ≤ 23): ``0 */N * * *``  e.g. ``3h`` → ``0 */3 * * *``
      - Named: ``1d``, ``1w`` (with optional ``--at`` for time/day anchoring)

    Supported --at formats (with --every):
      - "09:00"         → set hour/minute for a daily/weekly schedule
      - "Mon 09:00"     → set day-of-week + time for weekly schedules
    Supported --at formats (without --every, one-shot):
      - "2026-04-10 15:30" → specific datetime → "30 15 10 4 *"

    Raises ValueError on invalid input.
    """
    import re as _re

    if every is None and at is None:
        raise ValueError("Specify --every INTERVAL and/or --starting TIME. See --help for details.")

    if every is None:
        # One-shot: --at "2026-04-10 15:30"
        if not at:
            raise ValueError("--at requires a datetime value when --every is not specified.")
        return _parse_one_shot_at(at)

    # Recurring: --every INTERVAL [--at TIME]
    every_lower = every.strip().lower()

    # ── Named intervals (daily / weekly with --at support) ────────────────────
    if every_lower in _INTERVAL_MAP:
        default_cron, _ = _INTERVAL_MAP[every_lower]
        if at is None:
            return default_cron
        # fall through to --at override logic below

    # ── Arbitrary Nm / Nh (any positive integer) ──────────────────────────────
    elif _m := _re.fullmatch(r"(\d+)(m|h)", every_lower):
        n, unit = int(_m.group(1)), _m.group(2)
        if unit == "m":
            if not 1 <= n <= 59:
                raise ValueError(
                    f"Minute interval must be between 1 and 59 (got {n}m)."
                )
            default_cron = f"*/{n} * * * *" if n > 1 else "* * * * *"
        else:  # hours
            if not 1 <= n <= 23:
                raise ValueError(
                    f"Hour interval must be between 1 and 23 (got {n}h)."
                )
            default_cron = f"0 */{n} * * *" if n > 1 else "0 * * * *"

        if at is None:
            return default_cron
        # with --at override, fall through (sub-hourly + --at is unusual but allowed)

    else:
        raise ValueError(
            f"Unsupported interval {every!r}. Use Nm (e.g. 2m, 15m), Nh (e.g. 1h, 6h), "
            f"or 1d / 1w for daily/weekly."
        )

    # Apply --at override to the default cron
    at_stripped = at.strip()
    parts = default_cron.split()  # [minute, hour, dom, month, dow]

    # Check for "Mon 09:00" style (weekly with day override)
    at_parts = at_stripped.split()
    if len(at_parts) == 2:
        day_str, time_str = at_parts
        dow = _DOW_MAP.get(day_str.lower())
        if dow is None:
            raise ValueError(
                f"Unknown day of week {day_str!r}. Use: Mon, Tue, Wed, Thu, Fri, Sat, Sun."
            )
        h, m = _parse_hhmm(time_str)
        if every_lower not in ("1w",):
            raise ValueError("Day-of-week syntax (e.g. 'Mon 09:00') is only valid with --every 1w")
        return f"{m} {h} * * {dow}"

    # Plain "HH:MM" time override
    h, m = _parse_hhmm(at_stripped)
    # Classify the interval for --at semantics:
    #   sub-hourly (Nm): --at HH:MM makes no sense → reject
    #   hourly (Nh, 1h–23h): only minute part applies; hour is ignored
    #   daily/weekly: full HH:MM applies
    _at_m = _re.fullmatch(r"(\d+)(m|h)", every_lower)
    if _at_m and _at_m.group(2) == "m":
        raise ValueError(
            f"--at HH:MM is not applicable with --every {every} (sub-hourly interval)"
        )
    if _at_m and _at_m.group(2) == "h":
        # For sub-daily intervals, only the minute component of --at applies.
        # The hour is silently discarded since these jobs fire every N hours
        # regardless of starting hour.  Warn the user if they specified a non-zero hour.
        if h != 0:
            print(
                f"Warning: --at {at_stripped!r} with --every {every}: "
                f"the hour ({h:02d}) is ignored for sub-daily intervals. "
                f"Only the minute :{m:02d} is applied.",
                file=sys.stderr,
            )
        parts[0] = str(m)
        return " ".join(parts)
    # Daily / weekly: set hour and minute
    parts[0] = str(m)
    parts[1] = str(h)
    return " ".join(parts)


def _parse_one_shot_at(at: str) -> str:
    """Parse a 'YYYY-MM-DD HH:MM' string into a one-shot cron expression."""
    import sys
    from datetime import datetime
    formats = ["%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M", "%Y/%m/%d %H:%M"]
    dt = None
    for fmt in formats:
        try:
            dt = datetime.strptime(at.strip(), fmt)
            break
        except ValueError:
            continue
    if dt is None:
        raise ValueError(
            f"Cannot parse --at value {at!r}. "
            "Expected format: 'YYYY-MM-DD HH:MM' (e.g. '2026-04-10 15:30')."
        )
    # Warn if the specified datetime is in the past — the job will fire
    # on the next scheduler tick (within 60 s) rather than at the intended time.
    # NOTE: this function is an internal helper only reachable via _build_cron_expression
    # with an explicit `at` argument; the public CLI path uses _parse_starting instead.
    # C1: use UTC-aware comparison to avoid timezone-wrong result on non-UTC servers.
    from datetime import UTC as _UTC
    if dt.replace(tzinfo=_UTC) < datetime.now(_UTC):
        print(
            f"Warning: --at {at!r} is in the past. "
            "The job will fire immediately on the next scheduler tick.",
            file=sys.stderr,
        )
    return f"{dt.minute} {dt.hour} {dt.day} {dt.month} *"


def _parse_hhmm(time_str: str) -> tuple[int, int]:
    """Parse 'HH:MM' → (hour, minute). Raises ValueError on bad input."""
    parts = time_str.split(":")
    if len(parts) != 2:
        raise ValueError(f"Expected HH:MM format, got {time_str!r}")
    try:
        h, m = int(parts[0]), int(parts[1])
    except ValueError:
        raise ValueError(f"Expected HH:MM format, got {time_str!r}")
    if not (0 <= h <= 23 and 0 <= m <= 59):
        raise ValueError(f"Invalid time {time_str!r}: hour must be 0-23, minute 0-59")
    return h, m


def _send_command(request: dict, timeout: float = 60.0) -> dict:
    """Send a command to the running daemon via Unix domain socket.

    Args:
        request: Command payload to send.
        timeout: Seconds to wait for a response (default 60 s).
                 Use a larger value for slow commands (e.g. reset: 300 s).
    """
    from .daemon import is_running

    running, pid = is_running()
    if not running:
        print("Error: Gateway is not running. Start it with: coop start", file=sys.stderr)
        sys.exit(1)

    if not CONTROL_SOCK.exists():
        print(f"Error: Control socket not found although the daemon appears to be running "
              f"(pid={pid}). The daemon may still be starting, or it is in a broken state — "
              f"check the log and restart it.", file=sys.stderr)
        sys.exit(1)

    try:
        return asyncio.run(_send_command_async(request, timeout=timeout))
    except asyncio.TimeoutError:
        # Before OSError — TimeoutError IS an OSError since 3.11. Not "unreachable":
        # the daemon took the request and may well still be working on it
        # (a long reload). Neither "nothing changed" nor "done": exit 2.
        print(f"[ERROR] No response from the daemon within {timeout:.0f}s (pid={pid}). "
              f"It may still be working on the request — check 'coop "
              f"status' and the log before running it again.", file=sys.stderr)
        sys.exit(2)
    except OSError as exc:
        # Deliberately an error and never a silent fallback (#144, story 19): a
        # daemon with a pid but no reachable socket is a state to fix, not to
        # route around.
        print(f"Error: could not reach the daemon's control socket (pid={pid}): {exc}",
              file=sys.stderr)
        sys.exit(1)


# The largest one-line response the CLI will read. asyncio's default
# StreamReader limit is 64 KiB, and `readline()` RAISES past it rather than
# returning a partial line — a reload plan for a few hundred rooms, or a
# `list` of as many, would fail on the client after the daemon had already
# acted (#144). 16 MiB is far beyond any fleet the state files could hold.
_RESPONSE_LIMIT = 16 * 1024 * 1024


async def _send_command_async(request: dict, timeout: float = 60.0) -> dict:
    """Async helper to send command over Unix socket."""
    reader, writer = await asyncio.open_unix_connection(str(CONTROL_SOCK), limit=_RESPONSE_LIMIT)
    try:
        writer.write(json.dumps(request).encode() + b"\n")
        await writer.drain()

        data = await asyncio.wait_for(reader.readline(), timeout=timeout)
        return json.loads(data.decode())
    finally:
        writer.close()
        await writer.wait_closed()


if __name__ == "__main__":
    main()
