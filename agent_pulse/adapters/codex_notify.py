#!/usr/bin/env python3
"""Agent Pulse adapter for OpenAI Codex CLI.

Codex invokes the configured notify program with the notification JSON as
the final command-line argument. Register in ~/.codex/config.toml:

    notify = ["python", "C:/d/Projects/my_apps/agent_pulse/adapters/codex_notify.py"]

Codex currently only fires notify on turn completion (and approval events
in newer builds), so the feed is coarser than Claude Code / OpenCode.
Pure stdlib, fails silently.
"""
import os
import sys
import json
import socket
import hashlib
import urllib.request

COLLECTOR_URL = os.environ.get("PULSE_COLLECTOR_URL",
                               "http://127.0.0.1:8765/api/events")
COLLECTOR_TOKEN = os.environ.get("PULSE_COLLECTOR_TOKEN")
SPEC_VERSION = "0.1"
SOURCE = "codex"
TIMEOUT = 2


def main() -> None:
    data = json.loads(sys.argv[-1])
    hostname = socket.gethostname()

    cwd = data.get("cwd")
    session_id = (data.get("conversation-id") or data.get("session-id")
                  or data.get("thread-id"))
    if not session_id:
        # Fall back to a stable per-directory identity so repeated turns in
        # the same Codex session group together.
        session_id = "codex-" + hashlib.md5((cwd or "unknown").encode()).hexdigest()[:8]

    notify_type = data.get("type", "unknown")
    if notify_type == "agent-turn-complete":
        event_type = "completed"
        last = (data.get("last-assistant-message") or "").strip().replace("\n", " ")
        summary = last[:120] + ("…" if len(last) > 120 else "") if last else "Turn complete"
    elif "approval" in notify_type:
        event_type = "needs_input"
        summary = "Waiting for approval"
    else:
        event_type = "activity"
        summary = notify_type

    payload = {
        "spec_version": SPEC_VERSION,
        "instance_id": hashlib.md5(f"{hostname}:{session_id}".encode()).hexdigest()[:12],
        "hostname": hostname,
        "source": SOURCE,
        "event_type": event_type,
        "summary": summary,
        "payload": {"type": notify_type},
        "session_id": session_id,
        "cwd": cwd,
        "hook_event_name": notify_type,
    }
    headers = {"Content-Type": "application/json"}
    if COLLECTOR_TOKEN:
        headers["Authorization"] = f"Bearer {COLLECTOR_TOKEN}"
    req = urllib.request.Request(
        COLLECTOR_URL,
        data=json.dumps(payload).encode(),
        headers=headers,
        method="POST",
    )
    urllib.request.urlopen(req, timeout=TIMEOUT)


if __name__ == "__main__":
    try:
        main()
    except Exception:
        pass
    sys.exit(0)
