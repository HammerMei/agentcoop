"""The agent side imports no gateway code (``coop/agent/__init__.py``): an agent host
installs the client without the server behind it."""

from __future__ import annotations

import json
import subprocess
import sys

FORBIDDEN = ["aiohttp", "coop.doorbell", "coop.doorbell.api", "coop.doorbell.tls", "coop.room.core",
             "coop.room.gateway", "coop.room.classifier", "coop.room.replies", "tools"]


def test_importing_the_agent_commands_loads_no_gateway_code():
    code = "import json, sys, coop.agent.cli, coop.agent.client; print(json.dumps(sorted(sys.modules)))"
    out = subprocess.run([sys.executable, "-c", code], check=True, capture_output=True, text=True).stdout
    loaded = set(json.loads(out))
    assert not loaded & set(FORBIDDEN), sorted(loaded & set(FORBIDDEN))
    assert {"coop.agent.cli", "coop.agent.client", "coop.wire", "coop.room.model"} <= loaded
