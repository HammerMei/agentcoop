"""The doorbell over TLS with a pinned certificate (design §3.2): the client trusts the
fingerprint it was given and nothing else."""

from __future__ import annotations

import pytest

from coop.agent.cli import plain_http_off_loopback, run
from coop.agent.client import HttpDoorbell
from coop.doorbell.tls import fingerprint_of_file, same_fingerprint, server_context
from coop.errors import CertificateMismatch
from coop.room.clock import AsyncioClock

from .helpers import FAST, FIXTURES, Doorbell, Sandbox

CERT, KEY = FIXTURES / "test-cert.pem", FIXTURES / "test-key.pem"
TOKENS = {"tok-a": "a"}


@pytest.fixture
async def tls_doorbell():
    sb = Sandbox(clock=AsyncioClock(), attach_agent=False, params=FAST)
    async with Doorbell(sb.gateway, TOKENS, tls=server_context(CERT, KEY)) as doorbell:
        doorbell.sb = sb
        yield doorbell


def test_the_fingerprint_matches_openssls_and_compares_loosely():
    found = fingerprint_of_file(CERT)
    assert found == "AC:49:F8:DC:F0:D2:20:55:24:7B:05:00:CE:14:6F:24:49:11:E1:BD:75:25:E8:50:9F:3A:E9:93:B1:66:4D:B2"
    assert same_fingerprint(found, found.lower().replace(":", " ").strip())
    assert not same_fingerprint(found, "00" + found[2:])


async def test_a_pinned_client_talks_to_the_gateway_over_tls(tls_doorbell):
    assert tls_doorbell.url.startswith("https://")
    async with HttpDoorbell(tls_doorbell.url, "tok-a", fingerprint=fingerprint_of_file(CERT)) as client:
        assert await client.bio("over tls") == 1


async def test_a_client_without_the_pin_refuses_the_self_signed_certificate(tls_doorbell):
    async with HttpDoorbell(tls_doorbell.url, "tok-a") as client:
        with pytest.raises(Exception, match="CERTIFICATE_VERIFY_FAILED|certificate verify failed"):
            await client.bio("x")


async def test_a_wrong_fingerprint_is_refused_before_any_request(tls_doorbell):
    wrong = "00:" * 31 + "00"
    async with HttpDoorbell(tls_doorbell.url, "tok-a", fingerprint=wrong) as client:
        with pytest.raises(CertificateMismatch, match="not the pinned"):
            await client.bio("x")
    assert tls_doorbell.sb.gateway.agents["a"].bio_version == 0
    assert await run(["--gateway", tls_doorbell.url, "--token", "tok-a", "--fingerprint", wrong,
                      "read", "--room", "team"]) == 1


def test_plain_http_warns_off_loopback_only():
    assert plain_http_off_loopback("http://10.0.0.5:7474")
    assert not plain_http_off_loopback("http://127.0.0.1:7474")
    assert not plain_http_off_loopback("http://localhost:7474")
    assert not plain_http_off_loopback("https://10.0.0.5:7474")
