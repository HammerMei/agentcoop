"""The doorbell over TLS with a pinned certificate (design §3.2): the client trusts the
fingerprint it was given and nothing else."""

from __future__ import annotations

import shutil
import subprocess

import pytest

from coop.agent.cli import plain_http, run
from coop.agent.client import HttpDoorbell
from coop.doorbell.tls import server_context
from coop.errors import CertificateMismatch
from coop.fingerprint import fingerprint_of_file, same_fingerprint
from coop.room.clock import AsyncioClock

from .helpers import FAST, Doorbell, Sandbox

TOKENS = {"tok-a": "a"}
pytestmark = pytest.mark.skipif(shutil.which("openssl") is None, reason="openssl generates the test certificate")


@pytest.fixture(scope="module")
def cert(tmp_path_factory):
    """A self-signed certificate made by openssl, so the fingerprint test compares
    against openssl's own output rather than a value this code produced."""
    where = tmp_path_factory.mktemp("tls")
    cert, key = where / "cert.pem", where / "key.pem"
    subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "2", "-subj", "/CN=coop-test",
                    "-keyout", str(key), "-out", str(cert)], check=True, capture_output=True)
    return cert, key


@pytest.fixture
async def tls_doorbell(cert):
    sb = Sandbox(clock=AsyncioClock(), attach_agent=False, params=FAST)
    async with Doorbell(sb.gateway, TOKENS, tls=server_context(*cert)) as doorbell:
        doorbell.sb, doorbell.cert = sb, cert[0]
        yield doorbell


def test_the_fingerprint_matches_openssls_and_compares_loosely(cert):
    found = fingerprint_of_file(cert[0])
    shown = subprocess.run(["openssl", "x509", "-in", str(cert[0]), "-noout", "-fingerprint", "-sha256"],
                           check=True, capture_output=True, text=True).stdout
    assert found == shown.strip().split("=", 1)[1]
    assert same_fingerprint(found, found.lower().replace(":", " ").strip())
    assert not same_fingerprint(found, "00" + found[2:])


async def test_a_pinned_client_talks_to_the_gateway_over_tls(tls_doorbell):
    assert tls_doorbell.url.startswith("https://")
    async with HttpDoorbell(tls_doorbell.url, "tok-a", fingerprint=fingerprint_of_file(tls_doorbell.cert)) as client:
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


def test_plain_http_warns_and_https_does_not():
    assert plain_http("http://10.0.0.5:7474") and plain_http("http://127.0.0.1:7474")
    assert not plain_http("https://10.0.0.5:7474")


async def test_a_gateway_that_is_down_is_an_error_line_not_a_traceback(capsys):
    assert await run(["--gateway", "http://127.0.0.1:1", "--token", "t", "read", "--room", "team"]) == 1
    assert capsys.readouterr().err.splitlines()[-1].startswith("[ERROR]")
