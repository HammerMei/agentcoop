"""The rules document and its version move together: an agent refetches the document
only when a wake carries a newer version, so an edit that leaves the number alone
reaches nobody already running (design §3.3)."""

import hashlib

from coop.doorbell.api import RULES
from coop.room.gateway import RULES_VERSION


def test_rules_version_is_bumped_with_the_rules_document():
    digest = hashlib.sha256(RULES.read_bytes()).hexdigest()[:12]
    assert (RULES_VERSION, digest) == (4, "86cc8affe484"), (
        "coop/rules.md changed: bump RULES_VERSION in coop/room/gateway.py and update this pin")
