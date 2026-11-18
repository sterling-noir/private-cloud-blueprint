"""Configure Uptime Kuma for the home server (Phase 8a). Idempotent: existing items (matched by
name) are updated in place, missing ones are added.

Runs inside a throwaway python container with host networking (see kuma-configure.sh):
secrets are read from /cfg (= ~/.config/monitoring, read-only) and never printed.
"""
import json
import sys
import time

import socketio

KUMA = "http://127.0.0.1:3001"
CFG = "/cfg"
USER = "owner"
NOTIF_NAME = "Phone (ntfy)"
DOCKER_HOST_NAME = "This server (read-only Docker proxy)"

read = lambda name: open(f"{CFG}/{name}").read().strip()
PASSWORD = "<SECRET PASSWORD>"
TOPIC = read("topic")
TOKEN = "<SECRET TOKEN>"

# Alert text = what happened + what to do. "Down" fires after interval*(retries+1) seconds.
MONITORS = [
    dict(name="iMessage relay on the Mac", type="http", url="http://100.64.0.20:1235",
         accepted=["200-299", "401"], retries=4, priority=3,
         description="The Mac's iMessage relay (BlueBubbles) has not answered for 5 minutes, so new "
                     "iMessages are not reaching Element. Usually the Mac went to sleep: open the lid, "
                     "check Tailscale is on and NordVPN is off. Messages still arrive in the Messages app."),
    dict(name="iMessage bridge", type="docker", container="mautrix-imessage", retries=3, priority=4,
         description="The iMessage bridge on the server has been stopped for 4 minutes. It usually restarts "
                     "by itself; if this is still red in 15 minutes, ask Claude to look at it. "
                     "Messages still arrive in the Messages app."),
    dict(name="Telegram bridge", type="docker", container="mautrix-telegram", retries=3, priority=4,
         description="The Telegram bridge on the server has been stopped for 4 minutes. It usually restarts "
                     "by itself; if this is still red in 15 minutes, ask Claude to look at it. "
                     "Telegram itself keeps working."),
    dict(name="Signal bridge", type="docker", container="mautrix-signal", retries=3, priority=4,
         description="The Signal bridge on the server has been stopped for 4 minutes. It usually restarts "
                     "by itself; if this is still red in 15 minutes, ask Claude to look at it. "
                     "Signal itself keeps working."),
    dict(name="Matrix server (Tuwunel)", type="docker", container="tuwunel", retries=3, priority=4,
         description="The Matrix server has been stopped for 4 minutes, so Element shows nothing new. "
                     "It usually restarts by itself; if this is still red in 15 minutes, ask Claude. "
                     "Telegram, Signal and Messages keep working in their own apps."),
    dict(name="Matrix web address", type="http",
         url="https://home.example.ts.net/_matrix/client/versions", retries=3, priority=4,
         description="https://home.example.ts.net has not answered for 4 minutes, so Element cannot "
                     "connect. If 'Matrix server' is green, the Tailscale web address on the server is the "
                     "problem: ask Claude. Telegram, Signal and Messages keep working in their own apps."),
    dict(name="Home Assistant", type="docker", container="homeassistant", retries=3, priority=4,
         description="Home Assistant has been stopped for 4 minutes. It usually restarts by itself; if "
                     "this is still red in 15 minutes, ask Claude. Lights and plugs still work by hand."),
    dict(name="Home Assistant web page", type="http", url="http://127.0.0.1:8123/manifest.json",
         retries=3, priority=4,
         description="Home Assistant's web page (port 8123) has not answered for 4 minutes. If "
                     "'Home Assistant' is green it is probably still starting or stuck: wait 10 minutes, "
                     "then ask Claude."),
]

BODY = """{%- if heartbeatJSON -%}
{%- if heartbeatJSON.status == 0 -%}
{"topic": "@TOPIC@", "title": {{ monitorJSON.name | append: " is down" | json }}, "message": {{ monitorJSON.description | json }}, "priority": {% if monitorJSON.name contains "relay" %}3{% else %}4{% endif %}, "tags": ["red_circle"]}
{%- else -%}
{"topic": "@TOPIC@", "title": {{ monitorJSON.name | append: " is working again" | json }}, "message": "Back to normal. Nothing to do.", "priority": 2, "tags": ["white_check_mark"]}
{%- endif -%}
{%- else -%}
{"topic": "@TOPIC@", "title": "Uptime Kuma", "message": {{ msg | json }}, "priority": 3, "tags": ["information_source"]}
{%- endif -%}""".replace("@TOPIC@", TOPIC)

sio = socketio.Client(reconnection=False)
state = {}
for ev in ("monitorList", "notificationList", "dockerHostList", "maintenanceList"):
    sio.on(ev, (lambda e: lambda data: state.__setitem__(e, data))(ev))


def emit(event, *args):
    """socket.io call with positional args (Kuma handlers take (a, b, callback))."""
    box = {}
    sio.emit(event, args, callback=lambda r: box.setdefault("r", r))
    for _ in range(300):
        if "r" in box:
            break
        time.sleep(0.1)
    r = box.get("r")
    if not isinstance(r, dict) or r.get("ok") is False:
        sys.exit(f"!! {event} failed: {r.get('msg') if isinstance(r, dict) else r}")
    return r


def wait_for(key):
    for _ in range(100):
        if key in state:
            return state[key]
        time.sleep(0.1)
    return {}


need = {}
sio.on("setup", lambda *a: need.__setitem__("r", True))   # Kuma pushes this when no user exists yet
sio.connect(KUMA, transports=["websocket"])
time.sleep(2)
if need.get("r"):
    emit("setup", USER, PASSWORD)
    print("created the Uptime Kuma admin user", USER)
emit("login", {"username": USER, "password": PASSWORD, "token": ""})
print("logged in")

# 1. Notification -> ntfy (webhook with a custom JSON body so the text is a sentence)
notifs = {n["name"]: n for n in wait_for("notificationList")}
notif = dict(name=NOTIF_NAME, type="webhook", isDefault=True, active=True, applyExisting=True,
             webhookURL="<SECRET webhookURL>"
             webhookCustomBody="<SECRET webhookCustomBody>"
             webhookAdditionalHeaders="<SECRET webhookAdditionalHeaders>"
                                                  "Content-Type": "application/json"}))
r = emit("addNotification", notif, notifs[NOTIF_NAME]["id"] if NOTIF_NAME in notifs else None)
notif_id = r["id"]
print("notification ok:", NOTIF_NAME, "id", notif_id)

# 2. Docker host through the read-only proxy
hosts = {h["name"]: h for h in wait_for("dockerHostList")}
r = emit("addDockerHost", dict(name=DOCKER_HOST_NAME, dockerType="tcp", dockerDaemon="http://127.0.0.1:2375"),
         hosts[DOCKER_HOST_NAME]["id"] if DOCKER_HOST_NAME in hosts else None)
dh_id = r["id"]
print("docker host ok, id", dh_id)

# 3. Monitors
time.sleep(1)
existing = {m["name"]: m for m in (wait_for("monitorList") or {}).values()}
ids = []
for m in MONITORS:
    mon = dict(name=m["name"], type=m["type"], description=m["description"], active=True,
               interval=60, retryInterval=60, maxretries=m["retries"], resendInterval=0, timeout=24,
               accepted_statuscodes=m.get("accepted", ["200-299"]), notificationIDList={str(notif_id): True},
               upsideDown=False, ignoreTls=False, maxredirects=10, expiryNotification=False, method="GET",
               kafkaProducerBrokers=[], kafkaProducerSaslOptions={}, conditions=[], rabbitmqNodes=[])
    if m["type"] == "http":
        mon["url"] = m["url"]
    else:
        mon.update(docker_container=m["container"], docker_host=dh_id, url="https://")
    if m["name"] in existing:
        old = existing[m["name"]]
        mon["id"] = old["id"]
        emit("editMonitor", mon)
        ids.append(old["id"])
        print("updated monitor:", m["name"])
    else:
        r = emit("add", mon)
        ids.append(r["monitorID"])
        print("added monitor:", m["name"])

# 4. Quiet window for the nightly backup (containers stop ~1 min at 08:00 UTC)
title = "Nightly backup (08:00 UTC)"
maints = {x["title"]: x for x in (wait_for("maintenanceList") or {}).values()}
if title not in maints:
    r = emit("addMaintenance", dict(title=title, description="home-backup.sh stops the containers briefly.",
                                    strategy="cron", cron="58 7 * * *", durationMinutes=20, timezoneOption="UTC",
                                    active=True, intervalDay=1, dateRange=[None, None], timeRange=[None, None],
                                    weekdays=[], daysOfMonth=[]))
    mid = r["maintenanceID"]
    print("added maintenance window", title)
else:
    mid = maints[title]["id"]
emit("addMonitorMaintenance", mid, [{"id": i} for i in ids])
print("maintenance window covers", len(ids), "monitors")
sio.disconnect()
