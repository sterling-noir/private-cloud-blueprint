"""Posts the first-outage runbook into the Home room as @owner and pins it (Phase 8a).
Runs on the server: python3 ~/monitoring/post-runbook.py   (uses ~/.element-nightly-token; prints no secrets)"""
import json, os, urllib.parse, urllib.request, uuid

HS = "http://127.0.0.1:6167"
ROOM_ALIAS = "#home:home.example.ts.net"
TOKEN = "<SECRET TOKEN>"

PLAIN = """📌 If the house server stops working
1. Phone: is Tailscale on, and does it show “home” online?
2. Does https://home.example.ts.net open?
3. If not: wait 10 minutes — power and internet blips fix themselves.
4. Still down: safe to ignore until you're home. Telegram, Signal and Messages keep working in their own apps.
5. At home: router lights on? Restart the router first. The server restarts by itself when power returns.

Alerts: the ntfy app says what broke and what to do. An email “home-server heartbeat is DOWN” means the server or the house internet is off. Status page: https://home.example.ts.net:3001"""

HTML = """<p><strong>📌 If the house server stops working</strong></p>
<ol>
<li>Phone: is Tailscale on, and does it show “home” online?</li>
<li>Does https://home.example.ts.net open?</li>
<li>If not: wait 10 minutes — power and internet blips fix themselves.</li>
<li>Still down: safe to ignore until you're home. Telegram, Signal and Messages keep working in their own apps.</li>
<li>At home: router lights on? Restart the router first. The server restarts by itself when power returns.</li>
</ol>
<p><strong>Alerts:</strong> the ntfy app says what broke and what to do. An email “home-server heartbeat is DOWN” means the server or the house internet is off. Status page: https://home.example.ts.net:3001</p>"""


def api(method, path, body=None, ok404=False):
    req = urllib.request.Request(HS + path, method=method,
                                 data=json.dumps(body).encode() if body is not None else None,
                                 headers={"Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        if ok404 and e.code == 404:
            return None
        raise SystemExit(f"!! {method} {path.split('?')[0][:60]} -> HTTP {e.code}")


room = api("GET", "<REDACTED>" + urllib.parse.quote(ROOM_ALIAS))["room_id"]
ev = api("PUT", f"/_matrix/client/v3/rooms/{urllib.parse.quote(room)}/send/m.room.message/{uuid.uuid4().hex}",
         {"msgtype": "m.text", "body": PLAIN, "format": "org.matrix.custom.html", "formatted_body": HTML})["event_id"]
print("posted runbook:", ev)
state_path = f"/_matrix/client/v3/rooms/{urllib.parse.quote(room)}/state/m.room.pinned_events/"
cur = api("GET", state_path, ok404=True) or {}
pinned = [e for e in cur.get("pinned", []) if e != ev] + [ev]
api("PUT", state_path, {"pinned": pinned})
print("pinned; room now has", len(pinned), "pinned message(s)")
