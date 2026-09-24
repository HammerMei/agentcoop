"""Operator stand-in for the lab: copy a lab profile's credentials, field by
field, into the profile the keeper wrote, printing only the field names.

Usage: fill_profile.py <lab-profile> <keeper-profile>
"""

import os
import sys

import yaml
from _lab_profile import lab_profile

old = lab_profile("COOP_LAB_PROFILE_OVERRIDE", sys.argv[1])
keeper_file = os.path.expanduser("~/.agentcoop/admin-profiles.yaml")
with open(keeper_file) as f:
    doc = yaml.safe_load(f)
prof = doc["profiles"][sys.argv[2]]
touched = []
for key in ("username", "password", "token"):
    if old.get(key):
        prof[key] = old[key]
        touched.append(key)
with open(keeper_file, "w") as f:
    yaml.safe_dump(doc, f, sort_keys=False)
os.chmod(keeper_file, 0o600)
print("filled", sys.argv[2], "fields:", touched)
