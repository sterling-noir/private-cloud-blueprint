"""Adds a temporary always-failing monitor so Uptime Kuma sends one real 'down' alert through
ntfy, then deletes it. Usage on the server: ~/monitoring/kuma-configure.sh test"""
import time, socketio
CFG = "/cfg"; read = lambda n: open(f"{CFG}/{n}").read().strip()
sio = socketio.Client(reconnection=False); state = {}
sio.on("notificationList", lambda d: state.__setitem__("n", d))
def emit(ev, *a):
    box = {}; sio.emit(ev, a, callback=lambda r: box.setdefault("r", r))
    for _ in range(300):
        if "r" in box: break
        time.sleep(0.1)
    r = box.get("r")
    if isinstance(r, dict) and r.get("ok") is False: raise SystemExit(f"{ev}: {r.get('msg')}")
    return r
sio.connect("http://127.0.0.1:3001", transports=["websocket"])
emit("login", {"username": "owner", "password": read("kuma-password"), "token": ""})
time.sleep(1); nid = state["n"][0]["id"]
r = emit("add", dict(name="Alert test (temporary)", type="http", url="http://127.0.0.1:9/", active=True,
     description="This is a test alert from the home server. Nothing is wrong and there is nothing to do.",
     interval=20, retryInterval=20, maxretries=0, resendInterval=0, timeout=5, accepted_statuscodes=["200-299"],
     notificationIDList={str(nid): True}, kafkaProducerBrokers=[], kafkaProducerSaslOptions={}, conditions=[],
     rabbitmqNodes=[], method="GET", maxredirects=10))
mid = r["monitorID"]; print("test monitor added; waiting for the down alert…")
time.sleep(30); emit("deleteMonitor", mid); print("test monitor deleted"); sio.disconnect()
