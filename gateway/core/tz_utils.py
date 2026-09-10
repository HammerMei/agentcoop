"""Timezone utilities shared between gateway.cli and gateway.control.

A single canonical implementation avoids the subtle divergence that arises
when two independent copies of "read /etc/localtime" have different fallback
behaviors (one returned "PST", an invalid IANA name; the other returned "UTC").
"""

from __future__ import annotations

import logging

logger = logging.getLogger("coop.core.tz_utils")


def local_iana_timezone() -> str:
    """Return the IANA timezone name for the server's local timezone.

    Reads the ``/etc/localtime`` symlink (standard on Linux and macOS) to
    extract the IANA name (e.g. ``"America/Los_Angeles"``).  Falls back to
    ``"UTC"`` if the symlink is absent, is not a symlink, or points to a path
    that cannot be parsed as an IANA zone name.

    **Why not ``datetime.now().astimezone().strftime("%Z")``?**
    That returns an abbreviation like ``"PST"`` or ``"PDT"`` which is *not* a
    valid IANA timezone name and will cause ``zoneinfo.ZoneInfo("PST")`` to
    raise ``ZoneInfoNotFoundError``.

    **Platform support:** Linux and macOS only.  Windows does not have
    ``/etc/localtime``; if Windows support is ever needed, use the
    ``tzlocal`` package (``pip install tzlocal``) instead.
    """
    import pathlib
    try:
        tz_path = pathlib.Path("/etc/localtime")
        if tz_path.is_symlink():
            target = str(tz_path.resolve())
            # Both macOS (/var/db/timezone/zoneinfo/America/Los_Angeles) and
            # Linux (/usr/share/zoneinfo/America/Los_Angeles) embed "zoneinfo/"
            # in the resolved path.
            if "zoneinfo/" in target:
                return target.split("zoneinfo/", 1)[1]
        logger.debug(
            "/etc/localtime is not a symlink or path is not under zoneinfo/ — "
            "falling back to UTC"
        )
    except Exception as exc:
        logger.debug("Could not resolve /etc/localtime: %s — falling back to UTC", exc)
    return "UTC"
