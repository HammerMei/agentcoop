"""Lab stand-in for a human on the lab Rocket.Chat: post as the admin user and
print bot replies. Usage: rc_post.py <#channel|@user> "<message>" [wait-seconds]"""

import json
import sys
import time
import urllib.parse
import urllib.request

from _lab_profile import lab_profile

prof = lab_profile("COOP_LAB_RC_PROFILE", "rc")
base = prof["server_url"].rstrip("/") + "/api/v1"


def call(method, path, body=None, auth=None):
    headers = {"Content-Type": "application/json"}
    if auth:
        headers["X-Auth-Token"], headers["X-User-Id"] = auth
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(base + path, method=method, data=data, headers=headers)
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read() or b"{}")


login = call("POST", "/login", {"user": prof["username"], "password": prof["password"]})["data"]
auth = (login["authToken"], login["userId"])
target = sys.argv[1]
posted = call("POST", "/chat.postMessage", {"channel": target, "text": sys.argv[2]}, auth)
rid = posted["message"]["rid"]
since = urllib.parse.quote(posted["message"]["ts"])
history = "/channels.history" if target.startswith("#") else "/im.history"
deadline = time.time() + float(sys.argv[3] if len(sys.argv) > 3 else 120)
seen = set()
while time.time() < deadline:
    time.sleep(5)
    hist = call("GET", f"{history}?roomId={rid}&oldest={since}&count=20", auth=auth)
    for m in sorted(hist.get("messages", []), key=lambda m: m["ts"]):
        if m["u"]["_id"] != login["userId"] and m["_id"] not in seen:
            seen.add(m["_id"])
            print(f"BOT[{m['u']['username']}]:", m["msg"][:600])
    if seen and time.time() > deadline - 90:
        break
if not seen:
    print("NO REPLY within timeout")
