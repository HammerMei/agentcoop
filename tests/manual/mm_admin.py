"""Lab housekeeping on the lab Mattermost.

Usage: mm_admin.py teams | ensure-team <name> | user <username>
"""

import json
import sys
import urllib.error
import urllib.request

from _lab_profile import lab_profile

prof = lab_profile("COOP_LAB_MM_PROFILE", "mm")
base = prof["server_url"].rstrip("/") + "/api/v4"


def call(method, path, body=None, token=None):
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = "Bearer " + token
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(base + path, method=method, data=data, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.headers, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        return e.headers, {"error": e.code, "body": e.read().decode()[:200]}


headers, me = call("POST", "/users/login", {"login_id": prof["username"], "password": prof["password"]})
tok = headers["Token"]
cmd = sys.argv[1]
if cmd == "teams":
    _, teams = call("GET", "/teams", token=tok)
    print([(t["name"], t["display_name"], t["type"]) for t in teams])
elif cmd == "ensure-team":
    name = sys.argv[2]
    _, teams = call("GET", "/teams", token=tok)
    if any(t["name"] == name for t in teams):
        print("team exists:", name)
    else:
        _, team = call("POST", "/teams", {"name": name, "display_name": name, "type": "O"}, token=tok)
        print("created team:", team.get("name"), team.get("error"))
elif cmd == "user":
    _, user = call("GET", f"/users/username/{sys.argv[2]}", token=tok)
    print({k: user.get(k) for k in ("id", "username", "delete_at")} if "id" in user else user)
