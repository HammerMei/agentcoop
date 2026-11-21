"""Certificate fingerprints (design §3.2): what ``coop config add agent`` prints and
the agent's client pins. Both sides import this module and nothing else of each
other's TLS."""

from __future__ import annotations

import hashlib
import ssl
from pathlib import Path


def fingerprint(cert_pem: str) -> str:
    """The certificate's SHA-256 fingerprint as ``openssl x509 -fingerprint -sha256``
    prints it: upper-case hex pairs joined by colons."""
    der = ssl.PEM_cert_to_DER_cert(cert_pem)
    digest = hashlib.sha256(der).hexdigest().upper()
    return ":".join(digest[i:i + 2] for i in range(0, len(digest), 2))


def fingerprint_of_file(cert: Path | str) -> str:
    return fingerprint(Path(cert).read_text())


def same_fingerprint(a: str, b: str) -> bool:
    """Fingerprints compare without case, colons or spaces, so a hand-copied one matches."""
    return _plain(a) == _plain(b)


def _plain(text: str) -> str:
    return "".join(text.split()).replace(":", "").upper()
