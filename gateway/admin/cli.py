"""Standalone argparse entrypoint for the RC/MM admin CLI.

Usage:
    acg-provision [--config PATH] [--log-file PATH] <profile> create-user <username> <email> <password> [--full-name NAME]
    acg-provision [--config PATH] [--log-file PATH] <profile> create-channel <name> [--private]
    acg-provision [--config PATH] [--log-file PATH] <profile> add-to-channel <username> <channel>
    acg-provision [--config PATH] [--log-file PATH] <profile> delete-user <username>
    acg-provision [--config PATH] [--log-file PATH] <profile> delete-channel <channel>

Not wired into gateway/cli.py — see gateway/admin/__init__.py for why.

create-user / create-channel treat "already exists" as an idempotent no-op
(printed as a note, not an error, exit code 0) rather than a failure: the
underlying PlatformAdmin methods still raise a distinguishable exception
(UserAlreadyExistsError / ChannelAlreadyExistsError) for library callers
that want to react to it, but the CLI's own default fits its primary use
case — reseeding a lab/test environment where re-running the same seed
script against a partially-set-up state should just work.

API failures (httpx.HTTPStatusError, e.g. a 400 from creating a user whose
email already exists) print a short, platform-specific message extracted
from the response body (see gateway/admin/_errors.py) rather than httpx's
own generic "Client error '400 Bad Request' for url '...'" — the full raw
response body is preserved in --log-file (default: ./acg-provision.log) for
troubleshooting.
"""

import argparse
import asyncio
import errno
import logging
import os
import signal
import sys

import httpx

from gateway.admin._errors import friendly_error_message, log_error_response
from gateway.admin.base import ChannelAlreadyExistsError, UserAlreadyExistsError
from gateway.admin.config import AdminConfigError, get_profile, load_profiles
from gateway.admin.factory import admin_factory

DEFAULT_LOG_FILE = "coop-provision.log"

_error_logger = logging.getLogger("coop.admin.errors")


def _has_file_handler_for(logger: logging.Logger, target: str) -> bool:
    return any(
        isinstance(h, logging.FileHandler) and h.baseFilename == target for h in logger.handlers
    )


def _configure_error_log(path: str) -> None:
    """Attach file handlers so full API error bodies are preserved even
    though the console only shows a short, friendly message. Idempotent —
    safe to call more than once (e.g. across tests) without duplicating
    handlers/output lines.

    Two separate handlers are attached, both pointed at the same file:

    1. On ``_error_logger`` directly — this is what log_error_response()
       writes to explicitly from _run()'s httpx.HTTPStatusError handler.
       ``propagate`` is turned off so this doesn't ALSO get written via
       handler 2 below (same file, would otherwise double the line).

    2. On the "agent-chat-gateway" umbrella logger — this is the actual fix
       for what prompted this function to exist: RocketChatREST/
       MattermostREST's shared ``_request()`` calls ``logger.error()``
       itself on every non-2xx response, on loggers named
       "agent-chat-gateway.connectors.<platform>.rest". With NO handler
       configured anywhere in that hierarchy, Python's logging module falls
       back to its "handler of last resort" and prints the raw log record
       (including the full JSON body) straight to stderr — which is exactly
       the noisy line this whole feature was meant to get rid of; quieting
       the *expected*-404 existence checks (see gateway/admin/_logging.py)
       was never going to catch this, since a genuine creation failure like
       "email already exists" is deliberately NOT one of those suppressed
       calls. Attaching a WARNING+ handler here (a common ancestor of every
       "agent-chat-gateway.*" logger this CLI touches) means a handler is
       always found during that walk, so the "no handler" fallback never
       triggers — the detail lands in the file instead of leaking to the
       console a second time, redundant with the friendly message _run()
       already prints.
    """
    target = os.path.abspath(path)

    # logging.FileHandler opens its target EAGERLY, and open() on a FIFO with
    # no reader attached blocks forever — which is strictly worse than a
    # traceback: no message, no exit code, and the process is left hung
    # indefinitely (an orphan that outlives whatever created the pipe).
    # A non-blocking probe turns exactly that case into an immediate ENXIO.
    #
    # Deliberately probe rather than test S_ISFIFO: "is this a FIFO" is the
    # wrong question — a piped `--log-file /dev/stdout`, an inherited pipe,
    # and a FIFO that DOES have a reader are all FIFOs that open perfectly
    # well, and rejecting them would break working invocations. "Would
    # opening it block right now" is the actual question, and O_NONBLOCK
    # answers it directly.
    #
    # The probe fd is then held open until BOTH handlers have been
    # constructed, and only closed in the finally below. Closing it early
    # (as this originally did) reintroduced the very hang it prevents: with
    # a one-shot reader like `cat fifo > log`, closing the probe drops the
    # writer count to zero, the reader sees EOF and exits, and the next
    # FileHandler's blocking open() then waits forever for a reader that is
    # gone. Measured: ~30-68% of runs, and the resulting hang is silent AND
    # SIGINT-proof (the blocked open() restarts under SA_RESTART, so no
    # bytecode boundary is reached and main()'s KeyboardInterrupt handler
    # never runs) — it has to be SIGTERM'd by hand. Keeping one writer fd
    # open throughout means the reader never observes EOF in the gap.
    probe = None
    try:
        probe = os.open(target, os.O_WRONLY | os.O_APPEND | os.O_NONBLOCK)
    except FileNotFoundError:
        pass  # normal case: FileHandler creates it, or reports its own OSError
    except OSError as e:
        if e.errno == errno.ENXIO:
            raise OSError(
                f"{target} is a pipe with no reader attached — "
                "opening it for logging would block forever"
            ) from e
        raise

    try:
        _error_logger.setLevel(logging.ERROR)
        _error_logger.propagate = False
        if not _has_file_handler_for(_error_logger, target):
            handler = logging.FileHandler(path)
            handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s: %(message)s"))
            _error_logger.addHandler(handler)

        umbrella_logger = logging.getLogger("coop")
        if not _has_file_handler_for(umbrella_logger, target):
            umbrella_handler = logging.FileHandler(path)
            umbrella_handler.setLevel(logging.WARNING)
            umbrella_handler.setFormatter(
                logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
            )
            umbrella_logger.addHandler(umbrella_handler)
    finally:
        if probe is not None:
            os.close(probe)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="coop-provision",
        description="Standalone admin CLI for Rocket.Chat / Mattermost user & channel provisioning.",
    )
    parser.add_argument(
        "--config",
        help="Path to the profiles YAML file (default: ./admin-profiles.yaml, or $COOP_ADMIN_CONFIG)",
    )
    parser.add_argument(
        "--log-file", default=DEFAULT_LOG_FILE,
        help=f"Path to append full API error bodies to for troubleshooting "
        f"(default: ./{DEFAULT_LOG_FILE})",
    )
    parser.add_argument("profile", help="Profile name from the config file")

    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("create-user", help="Create a user account")
    p.add_argument("username")
    p.add_argument("email")
    p.add_argument("password")
    p.add_argument("--full-name")

    p = sub.add_parser("create-channel", help="Create a channel")
    p.add_argument("name")
    p.add_argument("--private", action="store_true")

    p = sub.add_parser("add-to-channel", help="Add an existing user to an existing channel")
    p.add_argument("username")
    p.add_argument("channel")

    p = sub.add_parser("delete-user", help="Delete (or deactivate) a user account")
    p.add_argument("username")

    p = sub.add_parser("delete-channel", help="Delete (or archive) a channel")
    p.add_argument("channel")

    return parser


async def _dispatch(admin, args) -> None:
    if args.command == "create-user":
        try:
            user = await admin.create_user(
                args.username, args.email, args.password, full_name=args.full_name,
            )
            print(f"Created user '{user.username}' (id={user.id})")
        except UserAlreadyExistsError as e:
            if not e.identity_matches:
                # Same reasoning as the channel-privacy-mismatch check
                # below: a username collision alone isn't proof of shared
                # identity (see gateway/admin/base.py's emails_match,
                # which both admins already used to decide this before
                # raising) — silently treating it as a successful skip
                # would let a provisioning script proceed to add-to-channel
                # next and grant an unrelated pre-existing account access
                # it was never meant to have.
                raise RuntimeError(
                    f"User '{e.username}' already exists but with a different "
                    f"email (existing: {e.existing.email!r}, requested: "
                    f"{args.email!r}, id={e.existing.id}) — refusing to treat "
                    "this as the same account"
                ) from e
            print(
                f"User '{e.username}' already exists (id={e.existing.id}) — skipping",
                file=sys.stderr,
            )

    elif args.command == "create-channel":
        try:
            channel = await admin.create_channel(args.name, is_private=args.private)
            print(f"Created channel '{channel.name}' (id={channel.id})")
        except ChannelAlreadyExistsError as e:
            if e.existing.is_private != args.private:
                # The idempotent-no-op default only makes sense when the
                # existing thing actually matches what was requested. A
                # channel that exists with the WRONG privacy is a state
                # mismatch, not a success — silently exiting 0 here would
                # let a provisioning script believe e.g. a sensitive
                # channel is private when it's actually public.
                existing_kind = "private" if e.existing.is_private else "public"
                requested_kind = "private" if args.private else "public"
                raise RuntimeError(
                    f"Channel '{e.name}' already exists but is {existing_kind}, "
                    f"not {requested_kind} as requested (id={e.existing.id})"
                ) from e
            print(
                f"Channel '{e.name}' already exists (id={e.existing.id}) — skipping",
                file=sys.stderr,
            )

    elif args.command == "add-to-channel":
        await admin.add_user_to_channel(args.username, args.channel)
        print(f"Added '{args.username}' to channel '{args.channel}'")

    elif args.command == "delete-user":
        await admin.delete_user(args.username)
        print(f"Deleted user '{args.username}'")

    elif args.command == "delete-channel":
        await admin.delete_channel(args.channel)
        print(f"Deleted channel '{args.channel}'")


async def _run(args: argparse.Namespace) -> int:
    try:
        _configure_error_log(args.log_file)
    except OSError as e:
        # logging.FileHandler opens the file immediately (not lazily) — an
        # unwritable --log-file (read-only cwd, bad path, permissions)
        # would otherwise raise here, before either try block below exists
        # to catch it, turning an otherwise-successful command into a raw
        # traceback.
        print(f"Error: could not open log file '{args.log_file}': {e}", file=sys.stderr)
        return 1

    try:
        profiles = load_profiles(args.config)
        profile = get_profile(profiles, args.profile)
        admin = admin_factory(profile)
    except AdminConfigError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1

    try:
        await admin.connect()
        await _dispatch(admin, args)
    except httpx.HTTPStatusError as e:
        # httpx's own str(e) is generic ("Client error '400 Bad Request' for
        # url '...'") and never shows *why* — the platform's actual message
        # only exists in the response body. Show that instead, and keep the
        # full raw body in --log-file so nothing is lost for troubleshooting.
        log_error_response(_error_logger, e)
        print(f"Error: {friendly_error_message(e)}", file=sys.stderr)
        print(f"(full response logged to {args.log_file})", file=sys.stderr)
        return 1
    except Exception as e:
        # Broad on purpose (matching gateway/cli.py's own CLI-boundary
        # handling): connect()/the REST clients underneath can also raise
        # plain RuntimeError (bad login), not just AdminError subclasses —
        # all of those should surface as a clean "Error: ..." line, not a
        # raw traceback.
        print(f"Error: {e}", file=sys.stderr)
        return 1
    finally:
        # This finally sits OUTSIDE the handlers above, so an exception from
        # close() would replace whatever the operation actually returned — a
        # successful exit 0, or an already-formatted "Error: ..." + exit 1 —
        # and escape asyncio.run() as a raw traceback. Cleanup must never
        # outrank the result it is cleaning up after, so a close failure is
        # reported as a secondary Warning and the real outcome is preserved.
        #
        # Reported as "Warning:", not "Error:", deliberately: an "Error:" line
        # paired with exit 0 would break the CLI contract's "Error implies
        # exit 1" pairing, which is what sank an earlier attempt at this.
        #
        # Exception, not BaseException — matching
        # PlatformAdmin.__aenter__'s contextlib.suppress(Exception): a
        # CancelledError/KeyboardInterrupt arriving here must still propagate
        # rather than be downgraded to a warning.
        try:
            await admin.close()
        except Exception as close_error:  # noqa: BLE001 - see comment above
            print(
                f"Warning: failed to release HTTP resources cleanly: {close_error}",
                file=sys.stderr,
            )

    return 0


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    try:
        sys.exit(asyncio.run(_run(args)))
    except KeyboardInterrupt:
        # Ctrl-C during a slow connect() otherwise ends in ~100 lines of
        # asyncio/anyio internals: KeyboardInterrupt and CancelledError are
        # BaseException, so neither `except AdminConfigError` nor the broad
        # `except Exception` in _run() sees them.
        #
        # Re-signalling (rather than exiting with a chosen code) keeps the
        # process dying *by* SIGINT, which is what a shell needs to abort a
        # seed loop like `for f in ...; do acg-provision ...; done`. Returning a
        # plain exit code here would silently make such loops run to
        # completion after a Ctrl-C.
        print("Error: interrupted", file=sys.stderr)
        signal.signal(signal.SIGINT, signal.SIG_DFL)
        os.kill(os.getpid(), signal.SIGINT)


if __name__ == "__main__":
    main()
