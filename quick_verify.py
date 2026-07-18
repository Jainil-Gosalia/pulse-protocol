#!/usr/bin/env python
"""Quick verification of Agent Pulse (multi-tool sessions model)."""
import os
import subprocess
import sys
import time
import json
import tempfile
import urllib.request
from threading import Thread

# Isolated port + database so a live collector is untouched.
os.environ["PULSE_DB"] = os.path.join(tempfile.gettempdir(), "pulse_verify.db")
if os.path.exists(os.environ["PULSE_DB"]):
    os.remove(os.environ["PULSE_DB"])
BASE = "http://127.0.0.1:8766"


def api(path, body=None):
    if body is not None:
        req = urllib.request.Request(
            BASE + path, data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json"}, method="POST")
    else:
        req = urllib.request.Request(BASE + path)
    with urllib.request.urlopen(req, timeout=3) as r:
        return json.load(r)


def start_server():
    import uvicorn
    from agent_pulse.collector.main import app
    uvicorn.run(app, host="127.0.0.1", port=8766, log_level="error")


Thread(target=start_server, daemon=True).start()
for i in range(50):
    try:
        api("/health")
        print("[OK] server started")
        break
    except Exception:
        time.sleep(0.2)
else:
    sys.exit("server failed to start")

failures = 0


def check(name, cond):
    global failures
    print(f"  {'[OK]' if cond else '[FAIL]'} {name}")
    if not cond:
        failures += 1


def evt(instance_id, event_type, source="testtool", summary="test", cwd="C:/proj/demo"):
    return api("/api/events", {
        "instance_id": instance_id, "hostname": "test-host", "source": source,
        "event_type": event_type, "summary": summary, "payload": {"t": 1},
        "session_id": "sess-" + instance_id, "cwd": cwd,
    })


print("\n1. Event ingest + source field")
r = evt("vt-001", "session_start")
check("event stored with source", r.get("source") == "testtool")

print("\n2. Session lifecycle transitions")
evt("vt-001", "user_prompt")
s = {x["instance_id"]: x for x in api("/api/sessions")}
check("user_prompt -> working", s["vt-001"]["status"] == "working")
evt("vt-001", "needs_input")
s = {x["instance_id"]: x for x in api("/api/sessions")}
check("needs_input -> needs_input", s["vt-001"]["status"] == "needs_input")
evt("vt-001", "completed")
s = {x["instance_id"]: x for x in api("/api/sessions")}
check("completed -> idle", s["vt-001"]["status"] == "idle")
evt("vt-001", "session_end")
s = {x["instance_id"]: x for x in api("/api/sessions")}
check("session_end -> ended", s["vt-001"]["status"] == "ended")
check("ended_at set", s["vt-001"]["ended_at"] is not None)

print("\n3. Query endpoints")
evt("vt-002", "error", source="othertool")
check("filter by source", all(e["source"] == "othertool"
      for e in api("/api/events?source=othertool")))
check("session timeline", len(api("/api/sessions/vt-001/events")) == 5)
check("include_ended=false hides ended",
      "vt-001" not in {x["instance_id"] for x in api("/api/sessions?include_ended=false")})

print("\n4. Dashboard")
with urllib.request.urlopen(BASE + "/", timeout=3) as r:
    check("dashboard HTML served", "<title>Agent Pulse" in r.read().decode())

print("\n5. Claude Code hook client (stdin -> POST)")
hook_event = {"hook_event_name": "PreToolUse", "tool_name": "TestTool",
              "tool_input": {"command": "echo hi"},
              "session_id": "hook-session", "cwd": "C:\\test"}
# Point the emitter at the isolated collector via the Pulse Protocol
# discovery env var — this also exercises spec conformance.
env = dict(os.environ, PULSE_COLLECTOR_URL="http://127.0.0.1:8766/api/events")
p = subprocess.run([sys.executable, "agent_pulse/hook_client.py"],
                   input=json.dumps(hook_event).encode(),
                   capture_output=True, timeout=5, env=env)
time.sleep(0.3)
latest = api("/api/events?limit=1")
check("hook client exit 0", p.returncode == 0)
check("hook event delivered with source claude_code",
      latest and latest[0]["source"] == "claude_code"
      and latest[0]["event_type"] == "activity"
      and "echo hi" in latest[0]["summary"])

print("\n6. Heartbeat + stale reaper")
evt("vt-003", "user_prompt")  # -> working
before = len(api("/api/events?instance_id=vt-003"))
api("/api/events", {
    "instance_id": "vt-003", "hostname": "test-host", "source": "testtool",
    "event_type": "heartbeat", "summary": "alive", "payload": {},
    "session_id": "sess-vt-003", "cwd": "C:/proj/demo",
})
after = len(api("/api/events?instance_id=vt-003"))
check("heartbeat stores no event row", before == after)
s = {x["instance_id"]: x for x in api("/api/sessions")}
check("heartbeat keeps session working", s["vt-003"]["status"] == "working")

from agent_pulse.collector.db import reap_stale_sessions, prune_events
reaped = reap_stale_sessions(0)  # everything 'working' is instantly stale
s = {x["instance_id"]: x for x in api("/api/sessions")}
check("reaper marks silent working session stale",
      s["vt-003"]["status"] == "stale" and any(r["instance_id"] == "vt-003" for r in reaped))
evt("vt-003", "activity")
s = {x["instance_id"]: x for x in api("/api/sessions")}
check("activity revives stale session", s["vt-003"]["status"] == "working")

print("\n7. Remote approval decisions")
d = api("/api/decisions", {"instance_id": "vt-003", "summary": "Bash: rm -rf build",
                           "payload": {"tool_name": "Bash"}})
check("decision created pending", d["status"] == "pending")
s = {x["instance_id"]: x for x in api("/api/sessions")}
check("pending decision flips session to needs_input", s["vt-003"]["status"] == "needs_input")
check("decision listed as pending", any(x["id"] == d["id"] for x in api("/api/decisions")))
r = api(f"/api/decisions/{d['id']}/respond", {"status": "allow", "reason": "ok"})
check("respond allow", r["status"] == "allow")
check("responded decision no longer pending",
      all(x["id"] != d["id"] for x in api("/api/decisions")))
s = {x["instance_id"]: x for x in api("/api/sessions")}
check("response returns session to working", s["vt-003"]["status"] == "working")
mode = api("/api/sessions/vt-003/mode", {"remote_approval": True})
check("away mode toggles on", mode["remote_approval"] is True)
check("mode readable", api("/api/sessions/vt-003/mode")["remote_approval"] is True)

print("\n8. Stats")
st = api("/api/stats")
check("stats counts sessions", st["sessions"] >= 1)
check("median attention response computed", st["median_response_s"] is not None)

print("\n9. Retention pruning")
deleted = prune_events(0)  # everything is older than 'now - 0 days'
check("prune removes old events", deleted > 0 and len(api("/api/events")) == 0)

print("\n10. Surrogate scrub (blank-page regression)")
from agent_pulse.collector.db import insert_event as _ins, get_events as _get
# A lone UTF-16 surrogate, as produced by mis-decoded Windows stdin — this
# used to poison the row and 500 the whole /api/events response.
_ins("vt-surrogate", "h", "activity", "bad \udc8f char", {"cmd": "x \udc8f y"},
     session_id="s", cwd="C:/x")
rows = _get(instance_id="vt-surrogate")
check("event with lone surrogate stored", len(rows) == 1)
check("get_events survives + serializes", json.dumps(rows) and "\udc8f" not in rows[0]["summary"])
check("events endpoint stays 200 with bad row",
      # round-trips through JSON like the real API response
      json.dumps(api("/api/events?instance_id=vt-surrogate")) is not None)

print("\n11. Connect-phone (QR) endpoints")
# Generous timeout: the first call may probe a slow/absent tailscale daemon.
with urllib.request.urlopen(BASE + "/api/connect-info", timeout=10) as r:
    ci = json.load(r)
check("connect-info returns urls + loopback flag",
      "urls" in ci and "loopback_only" in ci)
with urllib.request.urlopen(BASE + "/api/qr?data=hello", timeout=10) as r:
    ctype = r.headers.get("Content-Type", "")
    svg = r.read().decode("utf-8", "replace")
check("qr endpoint serves svg", "svg" in ctype and "<svg" in svg)

from agent_pulse import netinfo
check("qr_terminal renders", bool(netinfo.qr_terminal("https://example.com")))

print("\n" + "=" * 50)
if failures:
    print(f"{failures} check(s) FAILED")
    sys.exit(1)
print("All checks passed.")
