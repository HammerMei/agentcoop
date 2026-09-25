"""Shared by the lab stand-ins: the lab servers' admin credentials.

They come from a coop-provision profiles file that is NOT the one the keeper
under test writes: COOP_LAB_ADMIN_PROFILES (default
~/.agentcoop/admin-profiles.lab.yaml), profile named by the given environment
variable. Nothing here prints a credential.
"""

import os

import yaml


def lab_profile(env_name: str, default: str) -> dict:
    path = os.path.expanduser(
        os.environ.get("COOP_LAB_ADMIN_PROFILES", "~/.agentcoop/admin-profiles.lab.yaml")
    )
    with open(path) as f:
        return yaml.safe_load(f)["profiles"][os.environ.get(env_name, default)]
