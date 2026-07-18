#!/usr/bin/env python3
"""Generic Agent Pulse wrapper for tools without native hooks.

Wraps any CLI command, reporting session start/end (and error on nonzero
exit) to the collector while passing stdio straight through — interactive
TUIs work normally.

Usage:
    python agent_pulse/adapters/pulse_run.py [--source NAME] -- <command> [args...]

Examples:
    python agent_pulse/adapters/pulse_run.py --source aider -- aider --model gpt-4o
    python agent_pulse/adapters/pulse_run.py -- gemini
"""
import sys
import os
import json
import time
import socket
import hashlib
import uuid
import subprocess
import threading
import urllib.request

COLLECTOR_URL = os.environ.get("PULSE_COLLECTOR_URL",
                               "http://127.0.0.1:8765/api/events")
COLLECTOR_TOKEN = os.environ.get("PULSE_COLLECTOR_TOKEN")
SPEC_VERSION = "0.1"
TIMEOUT = 2


def send(source, session_id, event_type, summary, cwd, payload=None):
    try:
        hostname = socket.gethostname()
        body = {
            "spec_version": SPEC_VERSION,
            "instance_id": hashlib.md5(f"{hostname}:{session_id}".encode()).hexdigest()[:12],
            "hostname": hostname,
            "source": source,
            "event_type": event_type,
            "summary": summary,
            "payload": payload or {},
            "session_id": session_id,
            "cwd": cwd,
            "hook_event_name": "pulse_run",
        }
        headers = {"Content-Type": "application/json"}
        if COLLECTOR_TOKEN:
            headers["Authorization"] = f"Bearer {COLLECTOR_TOKEN}"
        req = urllib.request.Request(
            COLLECTOR_URL,
            data=json.dumps(body).encode(),
            headers=headers,
            method="POST",
        )
        urllib.request.urlopen(req, timeout=TIMEOUT)
    except Exception:
        pass


def main() -> int:
    args = sys.argv[1:]
    source = None
    if args[:1] == ["--source"] and len(args) >= 2:
        source = args[1]
        args = args[2:]
    if args[:1] == ["--"]:
        args = args[1:]
    if not args:
        print(__doc__)
        return 2

    source = source or os.path.basename(args[0]).split(".")[0]
    session_id = f"{source}-{uuid.uuid4().hex[:8]}"
    cwd = os.getcwd()
    cmdline = " ".join(args)

    send(source, session_id, "session_start", f"Started: {cmdline[:80]}", cwd)
    send(source, session_id, "activity", "Running", cwd)

    # Heartbeat while the child runs so the collector's stale reaper knows
    # this session is alive even when the wrapped tool emits nothing.
    stop = threading.Event()

    def _heartbeats():
        while not stop.wait(60):
            send(source, session_id, "heartbeat", "alive", cwd)

    threading.Thread(target=_heartbeats, daemon=True).start()

    started = time.time()
    try:
        exit_code = subprocess.call(args, shell=(os.name == "nt"))
    except KeyboardInterrupt:
        exit_code = 130
    except FileNotFoundError:
        print(f"pulse_run: command not found: {args[0]}", file=sys.stderr)
        exit_code = 127

    stop.set()
    elapsed = int(time.time() - started)
    if exit_code != 0:
        send(source, session_id, "error", f"Exited with code {exit_code}", cwd,
             {"exit_code": exit_code, "elapsed_s": elapsed})
    send(source, session_id, "session_end",
         f"Ended after {elapsed}s (exit {exit_code})", cwd,
         {"exit_code": exit_code, "elapsed_s": elapsed})
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
