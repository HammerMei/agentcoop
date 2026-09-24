"""Lab stand-in for a human: DM a bot on the lab Mattermost as the admin user
and print the bot's reply. Usage: mm_dm.py <bot-username> "<message>" [wait-seconds]"""

import json
import sys
import time
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
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.headers, json.loads(r.read() or b"{}")


headers, me = call("POST", "/users/login", {"login_id": prof["username"], "password": prof["password"]})
tok = headers["Token"]
_, bot = call("GET", f"/users/username/{sys.argv[1]}", token=tok)
_, channel = call("POST", "/channels/direct", [me["id"], bot["id"]], token=tok)
_, post = call("POST", "/posts", {"channel_id": channel["id"], "message": sys.argv[2]}, token=tok)
since = post["create_at"]
deadline = time.time() + float(sys.argv[3] if len(sys.argv) > 3 else 120)
while time.time() < deadline:
    time.sleep(5)
    _, posts = call("GET", f"/channels/{channel['id']}/posts?since={since}", token=tok)
    replies = [p for p in posts["posts"].values() if p["user_id"] == bot["id"]]
    if replies:
        for p in sorted(replies, key=lambda p: p["create_at"]):
            print("BOT:", p["message"][:1500])
        break
else:
    print("NO REPLY within timeout")
