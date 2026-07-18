#!/usr/bin/env python3
"""Generate sample hook events for testing the dashboard."""

import json
import subprocess
import time
from datetime import datetime

test_events = [
    {
        "hook_event_name": "PreToolUse",
        "tool_name": "Bash",
        "tool_input": {"command": "ls -la"},
        "session_id": "session-001",
        "cwd": "C:\\d\\Projects\\my_apps\\agent_pulse"
    },
    {
        "hook_event_name": "PostToolUse",
        "tool_name": "Bash",
        "session_id": "session-001",
        "cwd": "C:\\d\\Projects\\my_apps\\agent_pulse"
    },
    {
        "hook_event_name": "Notification",
        "message": "Waiting for permission to run command",
        "session_id": "session-001",
        "cwd": "C:\\d\\Projects\\my_apps\\agent_pulse"
    },
    {
        "hook_event_name": "Stop",
        "session_id": "session-001",
        "cwd": "C:\\d\\Projects\\my_apps\\agent_pulse"
    },
]

for event in test_events:
    print(f"Sending event: {event['hook_event_name']}")
    # Use subprocess to call hook_client with stdin
    proc = subprocess.Popen(
        ["python", "agent_pulse/hook_client.py"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    proc.communicate(input=json.dumps(event).encode())
    time.sleep(0.5)

print("Test events sent. Check http://127.0.0.1:8765 in your browser.")
