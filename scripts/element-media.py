#!/usr/bin/env python3
"""Find and remove photos/attachments that no Element message uses any more. Lives on the server as
~/bin/element-media.py; driven from the Mac by element-media-review.sh (review first, delete after).

A stored file is "unused" when ALL of these hold:
  - no event in any room on the server references it (Tuwunel's /_synapse/admin/v1/room/{id}/media,
    cross-checked 2026-11-14 against a full scan of every message: it misses nothing),
  - it is not anyone's profile picture or a room/space picture (those are in no message, but are in use),
  - it was stored more than 2 days ago (so a brand-new upload is never caught before its message lands).
Safety rules (security review R4, 2026-11-24): any lookup that fails stops the run instead of being
read as "not in use"; rooms Owner is not in are checked too; and if a room is encrypted, files
uploaded by anyone in that room are never listed (the server cannot see which files an encrypted
message uses).
Nothing here runs on a schedule. Nothing is deleted unless Owner finishes a review batch.

Usage (as homelab, token in ~/.element-nightly-token):
  element-media.py list                 -> JSON list of unused files (id, type, size, date, sender)
  element-media.py stage <ids.json>     -> download those files to ~/element-media-staging/ with readable names
  element-media.py delete <ids.json>    -> re-checks each id is still unused, then deletes it from the server
"""
import json, os, re, sys, time, shutil, mimetypes, urllib.request, urllib.parse, datetime

HS = os.environ.get("ELEMENT_HS", "http://127.0.0.1:6167")   # override is for the test only
SRV = "home.example.ts.net"
ME = "@owner:" + SRV
MIN_AGE_MS = 2 * 86400 * 1000
STAGE = os.path.expanduser("~/element-media-staging")
TOKEN = "<SECRET TOKEN>"
LOGDIR = os.path.expanduser("~/element-nightly-logs")
Q = urllib.parse.quote
MXC = re.compile(r"mxc://" + re.escape(SRV) + r"/([A-Za-z0-9_-]+)")
EXT = {"image/heic": ".heic", "image/jpeg": ".jpg", "image/webp": ".webp", "video/quicktime": ".mov",
       "audio/x-caf": ".caf", "audio/ogg": ".ogg", "application/json": ".json", "text/plain": ".txt"}


def req(method, path, body=None, raw=False, **q):
    url = HS + path + ("?" + urllib.parse.urlencode(q) if q else "")
    data = json.dumps(body).encode() if body is not None else None
    for attempt in range(5):
        r = urllib.request.Request(url, data=data, method=method,
                                   headers={"Authorization": "Bearer " + TOKEN, "Content-Type": "application/json"})
        try:
            resp = urllib.request.urlopen(r, timeout=180)
            return resp.read() if raw else json.load(resp)
        except urllib.error.HTTPError as e:
            if e.code == 429:
                time.sleep(2 + 2 * attempt); continue
            return {"_err": e.code, "_body": e.read().decode(errors="replace")[:200]}
        except Exception as e:
            if attempt == 4: return {"_err": "net", "_body": str(e)[:200]}
            time.sleep(5)
    return {"_err": 429}


def die(*a):
    print("!!", *a, file=sys.stderr); sys.exit(1)


def log(line):
    os.makedirs(LOGDIR, exist_ok=True)
    with open(os.path.join(LOGDIR, datetime.datetime.now(datetime.timezone.utc).strftime("%F") + ".log"), "a") as f:
        f.write(datetime.datetime.now(datetime.timezone.utc).strftime("%T ") + "media: " + line + "\n")


def inventory():
    """Return (all_media {id: item}, unused_ids set)."""
    if req("GET", "<REDACTED>").get("user_id") != ME:
        die("token not accepted - rerun element-cleanup-token.sh")
    # every stored local file, per uploader
    uploaders, frm = [], 0
    while True:
        stats = req("GET", "<REDACTED>", limit="1000", **{"from": str(frm)})
        if "_err" in stats: die("cannot list media:", stats)
        uploaders += stats.get("users", [])
        if not stats.get("next_token") or not stats.get("users"): break
        frm = stats["next_token"]
    allm = {}
    for u in uploaders:
        frm = 0
        while True:
            r = req("GET", f"/_synapse/admin/v1/users/{Q(u['user_id'])}/media", limit="500", **{"from": str(frm)})
            if "_err" in r: die("cannot list media of", u["user_id"], r)
            for m in r.get("media", []): allm[m["media_id"]] = m
            if not r.get("next_token") or not r.get("media"): break
            frm = r["next_token"]
    # files referenced by any event in any room on the server
    used, rooms, frm = set(), {}, None
    while True:
        r = req("GET", "/_synapse/admin/v1/rooms", limit="500", **({"from": str(frm)} if frm else {}))
        if "_err" in r: die("cannot list rooms:", r)
        rooms.update((x["room_id"], x) for x in r.get("rooms", []))
        frm = r.get("next_batch")
        if not frm: break
    for rid in rooms:
        r = req("GET", f"/_synapse/admin/v1/room/{Q(rid)}/media")
        if "_err" in r: die("cannot list media of room", rid, r)   # never guess: stop instead
        used.update(x.split("/")[-1] for x in r.get("local", []) + r.get("remote", []))
    # pictures: room/space/member avatars in room state, plus every user's profile picture.
    # Room state comes from one sync for the rooms Owner is in, and from the admin API for any other
    # room on the server (so a picture set in a room he is not in still counts as in use).
    flt = json.dumps({"room": {"timeline": {"limit": 1}, "state": {"lazy_load_members": False}}, "presence": {"types": []}})
    s = req("GET", "/_matrix/client/v3/sync", filter=flt, full_state="true", timeout="0")
    if "_err" in s: die("sync failed:", s)
    state = {rid: r.get("state", {}).get("events", []) for rid, r in s.get("rooms", {}).get("join", {}).items()}
    for rid, r in s.get("rooms", {}).get("invite", {}).items():
        state.setdefault(rid, r.get("invite_state", {}).get("events", []))
    for rid in rooms:
        if rid not in s.get("rooms", {}).get("join", {}):
            r = req("GET", f"/_synapse/admin/v1/rooms/{Q(rid)}/state")
            if "_err" in r or not isinstance(r.get("state"), list): die("cannot read state of room", rid, r)
            state[rid] = state.get(rid, []) + r["state"]
    users = {m["user_id"] for m in allm.values()}
    cannot_judge = set()   # everyone who is or was in an encrypted room
    for rid, st in state.items():
        used.update(MXC.findall(json.dumps(st)))
        members = {e["state_key"] for e in st if e.get("type") == "m.room.member" and e.get("state_key")}
        users.update(members)
        if (rooms.get(rid) or {}).get("encryption") or any(e.get("type") == "m.room.encryption" for e in st):
            cannot_judge.update(members)
    for u in users:
        p = req("GET", f"/_matrix/client/v3/profile/{Q(u)}")
        if "_err" in p:
            if p["_err"] == 404 and "M_NOT_FOUND" in p.get("_body", ""): continue   # no profile, so no picture
            die("cannot read the profile of", u, p)                                  # never guess: stop instead
        used.update(MXC.findall(json.dumps(p)))
    cutoff = int(time.time() * 1000) - MIN_AGE_MS
    unused = {k for k, m in allm.items() if k not in used and m.get("user_id") not in cannot_judge
              and isinstance(m.get("created_ts"), int) and m["created_ts"] < cutoff}
    if cannot_judge:
        held = sum(1 for k, m in allm.items() if k not in used and m.get("user_id") in cannot_judge)
        print(f"note: an encrypted room exists; {held} file(s) from people in it are left alone "
              "(the server cannot tell whether an encrypted message still uses them)", file=sys.stderr)
    return allm, unused


def sender_name(user_id, cache={}):
    if user_id not in cache:
        p = req("GET", f"/_matrix/client/v3/profile/{Q(user_id)}")
        cache[user_id] = p.get("displayname") or user_id.split(":")[0].lstrip("@")
    return cache[user_id]


def read_ids(path):
    ids = json.load(open(path))
    if not isinstance(ids, list) or not all(isinstance(i, str) and re.fullmatch(r"[A-Za-z0-9_-]+", i) for i in ids):
        die("bad id list")
    return ids


cmd = sys.argv[1] if len(sys.argv) > 1 else ""
if cmd == "list":
    allm, unused = inventory()
    out = [{"id": k, "type": allm[k]["media_type"], "bytes": allm[k]["media_length"],
            "date": time.strftime("%Y-%m-%d", time.localtime(allm[k]["created_ts"] / 1000)),
            "sender": sender_name(allm[k]["user_id"]), "name": allm[k].get("upload_name") or ""}
           for k in sorted(unused, key=lambda k: allm[k]["created_ts"])]
    json.dump(out, sys.stdout)

elif cmd == "stage":
    ids = read_ids(sys.argv[2])
    allm, unused = inventory()
    shutil.rmtree(STAGE, ignore_errors=True); os.makedirs(STAGE, mode=0o700)
    staged = []
    for k in ids:
        if k not in unused: continue                     # became used again, or vanished: leave it alone
        m = allm[k]
        data = req("GET", f"<REDACTED>{SRV}/{k}", raw=True)
        if isinstance(data, dict): print("  skip (download failed)", k, data, file=sys.stderr); continue
        ext = EXT.get(m["media_type"]) or mimetypes.guess_extension(m["media_type"]) or ".bin"
        who = re.sub(r"[^\w .()+'&-]", "_", sender_name(m["user_id"]))[:60].strip()
        day = time.strftime("%Y-%m-%d", time.localtime(m["created_ts"] / 1000))
        fn = f"{day} {who} {k[:6]}{ext}"
        with open(os.path.join(STAGE, fn), "wb") as f: f.write(data)
        staged.append(k)
    json.dump(staged, open(os.path.join(STAGE, ".batch.json"), "w"))
    print(len(staged))

elif cmd == "delete":
    ids = read_ids(sys.argv[2])
    allm, unused = inventory()
    done = skipped = 0
    for k in ids:
        if k not in unused:
            skipped += 1; continue                       # in use again (or already gone): never delete
        d = req("DELETE", f"/_synapse/admin/v1/media/{SRV}/{k}")
        if "_err" in d: print("  delete problem", k, d, file=sys.stderr); skipped += 1
        else: done += 1
    shutil.rmtree(STAGE, ignore_errors=True)
    log(f"review batch: deleted {done}, skipped {skipped}")
    print(json.dumps({"deleted": done, "skipped": skipped}))

else:
    print(__doc__); sys.exit(2)
