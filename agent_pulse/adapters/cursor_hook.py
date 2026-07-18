#!/usr/bin/env python3
"""Agent Pulse adapter for Cursor agent hooks.

Cursor passes hook input as JSON on stdin and REQUIRES a JSON response on
stdout for before* hooks ({"permission": "allow"}), otherwise the agent
action may be blocked. This script therefore always answers "allow" no
matter what happens internally — it observes, never gates.

Register in ~/.cursor/hooks.json (global) or <project>/.cursor/hooks.json:

    {
      "version": 1,
      "hooks": {
        "beforeSubmitPrompt":  [{ "command": "python C:/d/Projects/my_apps/agent_pulse/adapters/cursor_hook.py" }],
        "beforeShellExecution": [{ "command": "python C:/d/Projects/my_apps/agent_pulse/adapters/cursor_hook.py" }],
        "beforeMCPExecution":  [{ "command": "python C:/d/Projects/my_apps/agent_pulse/adapters/cursor_hook.py" }],
        "afterFileEdit":       [{ "command": "python C:/d/Projects/my_apps/agent_pulse/adapters/cursor_hook.py" }],
        "stop":                [{ "command": "python C:/d/Projects/my_apps/agent_pulse/adapters/cursor_hook.py" }]
      }
    }

(beforeReadFile is intentionally not registered — too noisy.)
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
SOURCE = "cursor"
TIMEOUT = 1.5


def report(data: dict) -> None:
    hostname = socket.gethostname()
    session_id = data.get("conversation_id") or "unknown"
    hook = data.get("hook_event_name", "unknown")

    roots = data.get("workspace_roots") or []
    cwd = data.get("cwd") or (roots[0] if roots else None)

    if hook == "beforeSubmitPrompt":
        event_type, summary = "user_prompt", "User submitted a prompt"
    elif hook == "beforeShellExecution":
        cmd = (data.get("command") or "").strip().replace("\n", " ")
        event_type = "activity"
        summary = f"Shell: {cmd[:80]}" + ("…" if len(cmd) > 80 else "") if cmd else "Shell command"
    elif hook == "beforeMCPExecution":
        event_type, summary = "activity", f"MCP tool: {data.get('tool_name', 'unknown')}"
    elif hook == "afterFileEdit":
        event_type, summary = "activity", f"Edited: {data.get('file_path', 'file')}"
    elif hook == "stop":
        event_type, summary = "completed", "Finished responding"
    else:
        event_type, summary = "activity", hook

    payload = {
        "spec_version": SPEC_VERSION,
        "instance_id": hashlib.md5(f"{hostname}:{session_id}".encode()).hexdigest()[:12],
        "hostname": hostname,
        "source": SOURCE,
        "event_type": event_type,
        "summary": summary,
        "payload": {"hook": hook},
        "session_id": session_id,
        "cwd": cwd,
        "hook_event_name": hook,
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
        raw = sys.stdin.read()
        data = json.loads(raw) if raw.strip() else {}
        report(data)
    except Exception:
        pass
    finally:
        # Always allow — this adapter observes, it never blocks the agent.
        print(json.dumps({"permission": "allow"}))
    sys.exit(0)
