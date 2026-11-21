"""TLS for the doorbell's listener (design §3.2): the gateway serves a certificate
the operator gives it. Generating one is the configuration command's job (``coop
config add agent``, a later increment); the fingerprint the agent pins is
:mod:`coop.fingerprint`.
"""

from __future__ import annotations

import ssl
from pathlib import Path


def server_context(cert: Path | str, key: Path | str) -> ssl.SSLContext:
    context = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
    context.load_cert_chain(str(cert), str(key))
    return context
