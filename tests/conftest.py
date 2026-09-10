"""Test-wide isolation of the runtime directory.

`gateway.core.state.RUNTIME_DIR` defaults to `~/.agentcoop`, so anything
that reaches `load_state` — `validate_config`, and through it the config TUI —
reads the *developer's own* state files unless a test remembers to patch it.

That was invisible for as long as `load_state` swallowed every failure and returned
`[]`: a real state file on the machine simply produced no findings. Adding the
legacy-format refusal made it visible in the worst possible way — a test asserting
"config is valid" started failing on machines with an existing install (mine had two
unversioned state files from April and June), while passing in CI, which has none.

Rather than patch the one test that surfaced it, this isolates every test: 17 test
modules reach that code path without patching `RUNTIME_DIR`, and the next one to be
written would have had no reason to know it needed to. A test that wants its own
runtime directory still patches `RUNTIME_DIR` itself; nested patches win over this
fixture, so those keep working unchanged.
"""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _isolated_runtime_dir(tmp_path_factory, monkeypatch):
    """Point RUNTIME_DIR at a per-test temporary directory.

    Patched on the module attribute rather than on the environment, because that is
    what `_state_file()` reads. `gateway.state` re-exports the name but every reader
    goes through `gateway.core.state`, so one patch covers both.
    """
    runtime = tmp_path_factory.mktemp("runtime")
    monkeypatch.setattr("gateway.core.state.RUNTIME_DIR", runtime)
    return runtime
