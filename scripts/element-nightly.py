#!/usr/bin/env python3
"""Nightly Element housekeeping. Lives on the server as ~/bin/element-nightly.py (cron, see below).

What it does, as @owner (token in ~/.element-nightly-token, mode 600):
  0. Accepts room invites from the bridges (new chats), files them under Unsorted, drops invites to abandoned rooms.
  1. Dead copies: leaves + forgets bridged rooms where Owner is the only one left
     (the iMessage bridge sometimes re-creates a chat's room after a restart and abandons the old one).
  2. Empty rooms: deletes rooms on the server that have no members at all.
  3. 30-day limit: in every room, removes messages older than 30 days (room settings/membership are kept).
     Tuwunel purges "everything before event X", so X = the first message newer than 30 days. Old messages
     that were stored after a newer one are left until the ones in front of them age out (never deletes recent ones).

Run with --dry-run to only print what it would do. Logs: ~<REDACTED>.log (counts only).
Cron (UTC): 30 7,8 * * *   -> once before the 08:00 backup, once after the bridges have restarted.
"""
import json, os, sys, time, urllib.request, urllib.parse, datetime

DRY = "--dry-run" in sys.argv
DAYS = 30
MSG_TYPES = {"m.room.message", "m.reaction", "m.sticker", "m.room.encrypted", "m.room.redaction"}  # bridge marker events are ignored
HS = "http://127.0.0.1:6167"
SRV = "home.example.ts.net"
ME = "@owner:" + SRV
TOKEN = "<SECRET TOKEN>"
LOGDIR = os.path.expanduser("~/element-nightly-logs")
os.makedirs(LOGDIR, exist_ok=True)
LOG = open(os.path.join(LOGDIR, datetime.datetime.now(datetime.timezone.utc).strftime("%F") + ".log"), "a")

def log(*a):
    line = datetime.datetime.now(datetime.timezone.utc).strftime("%T ") + " ".join(str(x) for x in a)
    print(line); LOG.write(line + "\n"); LOG.flush()

def req(method, path, body=None, **q):
    url = HS + path + ("?" + urllib.parse.urlencode(q) if q else "")
    data = json.dumps(body).encode() if body is not None else None
    for attempt in range(5):
        r = urllib.request.Request(url, data=data, method=method,
                                   headers={"Authorization": "Bearer " + TOKEN, "Content-Type": "application/json"})
        try:
            return json.load(urllib.request.urlopen(r, timeout=180))
        except urllib.error.HTTPError as e:
            if e.code == 429:
                time.sleep(2 + 2 * attempt); continue
            return {"_err": e.code, "_body": e.read().decode(errors="replace")[:200]}
        except Exception as e:
            if attempt == 4: return {"_err": "net", "_body": str(e)[:200]}
            time.sleep(5)
    return {"_err": 429}

Q = urllib.parse.quote
now_ms = int(time.time() * 1000)
cut = now_ms - DAYS * 86400 * 1000

who = req("GET", "<REDACTED>")
if who.get("user_id") != ME:
    log("!! token not accepted, stopping:", who); sys.exit(1)
log("== start", "(dry run)" if DRY else "")

# ---- 0. invites from the bridges <REDACTED>
# The iMessage bridge deletes a chat's room at its next restart if Owner has not joined it yet
# ("Room seems to be empty, cleaning up") and makes a new one - that is where the duplicates came from.
# So: accept bridge invites every night. An invite to a room the bridge already abandoned is dropped.
BRIDGE_SENDERS = ("@imessagebot:", "@imessage_", "@telegrambot:", "@telegram_", "@signalbot:", "@signal_")
s0 = req("GET", "/_matrix/client/v3/sync", filter=json.dumps({"room": {"timeline": {"limit": 1}}, "presence": {"types": []}}), timeout="0")
accepted, dropped, new_rooms = 0, 0, []
for rid, r in (s0.get("rooms", {}).get("invite", {}) if "_err" not in s0 else {}).items():
    inviter = next((e["sender"] for e in r.get("invite_state", {}).get("events", [])
                    if e["type"] == "m.room.member" and e.get("state_key") == ME), "")
    if not inviter.startswith(BRIDGE_SENDERS):
        continue                      # invites from anyone else are left for Owner to decide
    if DRY:
        accepted += 1; continue
    j = req("POST", f"/_matrix/client/v3/rooms/{Q(rid)}/join", {})
    members = req("GET", f"/_matrix/client/v3/rooms/{Q(rid)}/joined_members").get("joined", {}) if "_err" not in j else {}
    if len(members) > 1:
        accepted += 1; new_rooms.append(rid)
    else:                             # abandoned room (or join refused): clear it out
        req("POST", f"/_matrix/client/v3/rooms/{Q(rid)}/leave", {})
        req("POST", f"/_matrix/client/v3/rooms/{Q(rid)}/forget", {})
        dropped += 1
log(f"bridge invites: accepted {accepted}{' (dry run: not checked for abandoned rooms)' if DRY else ''}, dropped {dropped} abandoned")

# ---- 1. dead copies <REDACTED>
flt = json.dumps({"room": {"timeline": {"limit": 1}, "state": {"lazy_load_members": False}}, "presence": {"types": []}})
s = req("GET", "/_matrix/client/v3/sync", filter=flt, full_state="true", timeout="0")
if "_err" in s:
    log("!! sync failed, stopping:", s); sys.exit(1)
joined = s.get("rooms", {}).get("join", {})
dead, keep = [], []
for rid, r in joined.items():
    st = {}
    for e in r.get("state", {}).get("events", []) + r.get("timeline", {}).get("events", []):
        if "state_key" in e: st[(e["type"], e["state_key"])] = e
    create = st.get(("m.room.create", ""), {})
    if create.get("content", {}).get("type") == "m.space":
        continue
    present = {k for (t, k), e in st.items() if t == "m.room.member" and e["content"].get("membership") in ("join", "invite")}
    bridged = any(t == "m.bridge" for (t, k) in st)
    old_enough = create.get("origin_server_ts", 0) < now_ms - 3600 * 1000
    if bridged and present == {ME} and old_enough:
        dead.append(rid)
    else:
        keep.append(rid)
left = 0
for rid in dead:
    if DRY: continue
    a = req("POST", f"/_matrix/client/v3/rooms/{Q(rid)}/leave", {})
    b = req("POST", f"/_matrix/client/v3/rooms/{Q(rid)}/forget", {})
    if "_err" in a or "_err" in b: log("  leave/forget problem", rid, a, b)
    else: left += 1
unsorted = next((rid for rid, r in joined.items()
                 if any(e["type"] == "m.room.create" and e["content"].get("type") == "m.space" and e["sender"] == ME
                        for e in r.get("state", {}).get("events", []))
                 and any(e["type"] == "m.room.name" and e["content"].get("name") == "Unsorted"
                         for e in r.get("state", {}).get("events", []))), None)
if unsorted:
    for rid in new_rooms:
        req("PUT", f"/_matrix/client/v3/rooms/{Q(unsorted)}/state/m.space.child/{Q(rid)}", {"via": [SRV]})
    if new_rooms: log(f"filed {len(new_rooms)} new rooms under Unsorted")
log(f"dead copies: found {len(dead)}, removed {left}; rooms kept {len(keep)}")

# ---- 2. rooms with nobody in them <REDACTED>
empty, frm = [], None
while True:
    kw = {"limit": "500"}
    if frm: kw["from"] = str(frm)
    r = req("GET", "/_synapse/admin/v1/rooms", **kw)
    if "_err" in r: log("  could not list server rooms:", r); break
    empty += [x["room_id"] for x in r.get("rooms", []) if x.get("joined_members") == 0 and x.get("joined_local_members") == 0]
    frm = r.get("next_batch")
    if not frm: break
gone = 0
for rid in empty:
    if DRY: continue
    d = req("DELETE", f"/_synapse/admin/v2/rooms/{Q(rid)}", {"purge": True, "block": False})
    if "_err" in d: log("  delete problem", rid, d)
    else: gone += 1
log(f"empty rooms on server: found {len(empty)}, delete requested for {gone}")

# ---- 3. 30-day limit <REDACTED>
purged_rooms = old_total = waiting = late = 0
for rid in keep:
    events, frm = [], None
    while True:
        kw = {"dir": "f", "limit": "1000"}
        if frm: kw["from"] = frm
        m = req("GET", f"/_matrix/client/v3/rooms/{Q(rid)}/messages", **kw)
        chunk = m.get("chunk", [])
        events += chunk
        frm = m.get("end")
        if not frm or not chunk: break
    msg_idx = [i for i, e in enumerate(events) if "state_key" not in e and e["type"] in MSG_TYPES]
    old_idx = [i for i in msg_idx if events[i]["origin_server_ts"] < cut]
    if not old_idx:
        continue
    old_total += len(old_idx)
    new_idx = [i for i in msg_idx if events[i]["origin_server_ts"] >= cut]
    if new_idx:
        # Only remove the old messages stored before the first recent one, so no recent message is lost.
        # Old ones stored after it ("late") go on a later night, once the messages in front of them age out.
        b_i = new_idx[0]
        late += sum(1 for i in old_idx if i > b_i)
        if old_idx[0] > b_i:
            continue
    else:
        b_i = old_idx[-1] + 1
        if b_i >= len(events):
            waiting += 1      # old message is the newest event in the room; it goes once anything newer exists
            continue
    if DRY:
        purged_rooms += 1; continue
    boundary = events[b_i]["event_id"]
    p = req("POST", f"<REDACTED>{Q(rid)}/{Q(boundary)}", {"delete_local_events": True})
    if "purge_id" not in p:
        log("  purge problem", rid, p); continue
    for _ in range(60):
        stt = req("GET", f"<REDACTED>{p['purge_id']}")
        if stt.get("status") != "active": break
        time.sleep(1)
    if stt.get("status") == "complete": purged_rooms += 1
    else: log("  purge did not complete", rid, stt)
log(f"30-day limit: {old_total} messages older than {DAYS} days found; purged in {purged_rooms} rooms; "
    f"{late} left for a later night (stored after a recent message), {waiting} rooms waiting for a newer event")
log("== done")

# keep 60 days of logs
for f in os.listdir(LOGDIR):
    p = os.path.join(LOGDIR, f)
    if os.path.getmtime(p) < time.time() - 60 * 86400: os.remove(p)
