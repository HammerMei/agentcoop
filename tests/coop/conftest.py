from __future__ import annotations

import shutil
import subprocess

import pytest


@pytest.fixture(scope="session")
def cert(tmp_path_factory):
    """A self-signed certificate made by openssl, so a fingerprint test compares
    against openssl's own output rather than a value this code produced, and a TLS
    test talks to a listener that is what a lab's self-signed server is."""
    if shutil.which("openssl") is None:
        pytest.skip("openssl generates the test certificate")
    where = tmp_path_factory.mktemp("tls")
    cert, key = where / "cert.pem", where / "key.pem"
    subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "2", "-subj", "/CN=coop-test",
                    "-keyout", str(key), "-out", str(cert)], check=True, capture_output=True)
    return cert, key
