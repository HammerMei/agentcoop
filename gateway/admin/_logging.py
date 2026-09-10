"""Internal logging helper shared by RocketChatAdmin/MattermostAdmin.

Not part of PlatformAdmin's public surface.
"""

import contextvars
import logging
from contextlib import contextmanager

# Task-local, not a module-level flag: two concurrent admin operations in
# the same process (e.g. two agents provisioned around the same time by a
# future AgentCoop-integrated version of this tool — see gateway/admin/__init__.py)
# would otherwise race on a single shared mutable flag. contextvars.ContextVar
# is copied per asyncio.Task at creation, so each task's set()/reset() is
# invisible to every other task, regardless of how their awaits interleave.
_quiet = contextvars.ContextVar("quiet_expected_error", default=False)


class _TaskLocalQuietFilter(logging.Filter):
    """Drops a record iff the *current task* is inside quiet_expected_error().

    A logging.Filter runs synchronously at the point the log call happens,
    inside whatever task is executing then — so reading _quiet here always
    reflects that task's own suppression state, never another task's.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        return not _quiet.get()


@contextmanager
def quiet_expected_error(logger: logging.Logger):
    """Suppress ``logger``'s output for the ``with`` block, for the current
    task only.

    RocketChatREST/MattermostREST's shared ``_request()`` logs every non-2xx
    response as an ERROR before raising, with no way to tell it "this 404 is
    an expected existence check, not a real failure." That's exactly what
    RocketChatAdmin/MattermostAdmin's ``_get_user_or_none``, ``_get_channel_or_none``,
    and ``_ensure_team_member`` do — they deliberately probe for existence and
    treat a 404 (RC: 400 or 404) as a normal, handled outcome, not an error.
    Left as-is, every pre-create idempotency check and every "does this
    already exist" lookup would print a scary API-error log line even on the
    happy path (create a brand new user -> the very first thing that happens
    is a 404 for "not found yet").

    Wrapping just those calls avoids editing the shared REST clients
    (deliberately out of scope for this package — see
    gateway/admin/__init__.py) while still killing the noisy duplicate log
    line for the common case.

    Implemented as a task-local logging.Filter rather than mutating
    ``logger.level`` directly: the logger object is a shared, module-level
    singleton (one per platform, not one per PlatformAdmin instance), so a
    naive save-level/restore-level approach breaks under concurrency — if
    task B's ``with`` block starts while task A's is still open, B's
    "previous level" snapshot is actually the CRITICAL level A set, and B's
    exit then permanently reinstates CRITICAL once A has already exited,
    silencing all future errors on that logger for the rest of the process.
    The filter approach has no such shared state to race on.

    Trade-off: a genuine non-404 failure raised during one of these specific
    calls also has its REST-layer log line suppressed for that one call.
    Nothing is actually lost — the caller still re-raises non-404 errors,
    and gateway/admin/cli.py prints the exception's own message — this only
    removes a duplicate log line, not the error information itself.
    """
    if not any(isinstance(f, _TaskLocalQuietFilter) for f in logger.filters):
        logger.addFilter(_TaskLocalQuietFilter())
    token = _quiet.set(True)
    try:
        yield
    finally:
        _quiet.reset(token)
