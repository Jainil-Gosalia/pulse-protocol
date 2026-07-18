#!/usr/bin/env python3
"""Agent Pulse adapter for Claude Code.

Invoked by Claude Code hooks (settings.json), receives the hook event as
JSON on stdin and forwards a normalized event to the local collector.
Pure stdlib; fails silently and fast so a missing collector never
interferes with the Claude Code session.
"""
import os
import sys
import json
import time
import socket
import hashlib
import urllib.request
from typing import Dict, Any, Tuple, Optional

COLLECTOR_URL = os.environ.get("PULSE_COLLECTOR_URL",
                               "http://127.0.0.1:8765/api/events")
COLLECTOR_TOKEN = os.environ.get("PULSE_COLLECTOR_TOKEN")
API_BASE = COLLECTOR_URL.rsplit("/api/events", 1)[0]
SPEC_VERSION = "0.1"
SOURCE = "claude_code"
TIMEOUT = 2
# Reporting POSTs bound how long a hook can block the tool; kept short so a
# slow/remote collector can't stall the agent (spec section 4).
REPORT_TIMEOUT = float(os.environ.get("PULSE_REPORT_TIMEOUT", "1.5"))
# Opt-in: fire reporting POSTs in a detached child so they're fully off the
# tool's critical path. Worth it only for remote collectors — on localhost
# the synchronous POST (~3ms) is cheaper than spawning a process.
DETACH = os.environ.get("PULSE_DETACH", "").strip().lower() in ("1", "true", "yes")

# Remote approval ("away mode"): which tools get held for a dashboard
# decision when the session's away mode is on.
GATED_TOOLS = set(
    t.strip() for t in os.environ.get(
        "PULSE_GATED_TOOLS", "Bash,PowerShell,Write,Edit,NotebookEdit").split(",")
    if t.strip()
)
APPROVAL_TIMEOUT = int(os.environ.get("PULSE_APPROVAL_TIMEOUT", "45"))


def get_instance_id(hostname: str, session_id: str) -> str:
    hash_input = f"{hostname}:{session_id}".encode()
    return hashlib.md5(hash_input).hexdigest()[:12]


def _tool_detail(hook_event: Dict[str, Any]) -> str:
    """Short human-readable detail from tool_input, e.g. the command or path."""
    tool_input = hook_event.get("tool_input") or {}
    for key in ("command", "file_path", "pattern", "url", "prompt", "description"):
        value = tool_input.get(key)
        if isinstance(value, str) and value.strip():
            value = value.strip().replace("\n", " ")
            return value[:80] + ("…" if len(value) > 80 else "")
    return ""


def extract_summary(hook_event: Dict[str, Any]) -> str:
    event_name = hook_event.get("hook_event_name", "unknown")

    if event_name == "PreToolUse":
        tool_name = hook_event.get("tool_name", "unknown")
        detail = _tool_detail(hook_event)
        return f"{tool_name}: {detail}" if detail else f"Using tool: {tool_name}"
    elif event_name == "PostToolUse":
        tool_name = hook_event.get("tool_name", "unknown")
        return f"Tool error: {tool_name}"
    elif event_name == "Notification":
        message = hook_event.get("message", "")
        return message[:120] if message else "Waiting for input"
    elif event_name == "UserPromptSubmit":
        prompt = (hook_event.get("prompt") or "").strip().replace("\n", " ")
        return f"Prompt: {prompt[:80]}" + ("…" if len(prompt) > 80 else "") \
            if prompt else "User submitted a prompt"
    elif event_name == "Stop":
        return "Finished responding"
    elif event_name == "SubagentStop":
        return "Subagent finished"
    elif event_name == "SessionStart":
        return "Session started"
    elif event_name == "SessionEnd":
        return "Session ended"
    else:
        return event_name


def map_event_type(hook_event: Dict[str, Any]) -> Tuple[str, bool]:
    """Return (normalized event_type, should_send)."""
    event_name = hook_event.get("hook_event_name", "unknown")

    if event_name == "Notification":
        return ("needs_input", True)
    elif event_name == "UserPromptSubmit":
        return ("user_prompt", True)
    elif event_name == "Stop":
        return ("completed", True)
    elif event_name == "SubagentStop":
        return ("subagent_completed", True)
    elif event_name == "SessionStart":
        return ("session_start", True)
    elif event_name == "SessionEnd":
        return ("session_end", True)
    elif event_name == "PreToolUse":
        return ("activity", True)
    elif event_name == "PostToolUse":
        # Only report failures; successful tool completions would double
        # every activity event.
        response = hook_event.get("tool_response")
        is_error = False
        if isinstance(response, dict):
            is_error = bool(response.get("is_error") or response.get("error"))
        return ("error", is_error)
    else:
        return ("activity", True)


def _slim_payload(hook_event: Dict[str, Any]) -> Dict[str, Any]:
    """Keep the payload small — drop bulky fields like full tool responses."""
    slim = {}
    for key in ("hook_event_name", "tool_name", "message", "prompt",
                "session_id", "cwd", "reason", "source"):
        if key in hook_event:
            value = hook_event[key]
            if isinstance(value, str) and len(value) > 500:
                value = value[:500] + "…"
            slim[key] = value
    tool_input = hook_event.get("tool_input")
    if isinstance(tool_input, dict):
        slim["tool_input"] = {
            k: (v[:300] + "…" if isinstance(v, str) and len(v) > 300 else v)
            for k, v in tool_input.items()
        }
    return slim


def post_event(event_data: Dict[str, Any]) -> None:
    try:
        hostname = socket.gethostname()
        session_id = event_data.get("session_id", "unknown")

        event_type, should_send = map_event_type(event_data)
        if not should_send:
            return

        payload = {
            "spec_version": SPEC_VERSION,
            "instance_id": get_instance_id(hostname, session_id),
            "hostname": hostname,
            "source": SOURCE,
            "event_type": event_type,
            "summary": extract_summary(event_data),
            "payload": _slim_payload(event_data),
            "session_id": session_id,
            "cwd": event_data.get("cwd"),
            "hook_event_name": event_data.get("hook_event_name", "unknown"),
        }

        _send(json.dumps(payload).encode())
    except Exception:
        pass


def _send(body: bytes) -> None:
    """Deliver a reporting POST. Detached (off critical path) when
    PULSE_DETACH is set, else a short-timeout synchronous POST."""
    if DETACH and _send_detached(body):
        return
    headers = {"Content-Type": "application/json"}
    if COLLECTOR_TOKEN:
        headers["Authorization"] = f"Bearer {COLLECTOR_TOKEN}"
    req = urllib.request.Request(COLLECTOR_URL, data=body, headers=headers, method="POST")
    urllib.request.urlopen(req, timeout=REPORT_TIMEOUT)


_DETACH_CODE = (
    "import sys,urllib.request as u;"
    "h={'Content-Type':'application/json'};"
    "t=sys.argv[2];"
    "h.__setitem__('Authorization','Bearer '+t) if t else None;"
    "u.urlopen(u.Request(sys.argv[1],data=sys.stdin.buffer.read(),headers=h,method='POST'),timeout=5)"
)


def _send_detached(body: bytes) -> bool:
    """Spawn a fully detached child to POST, returning immediately. Returns
    False on any failure so the caller falls back to a synchronous send."""
    try:
        import subprocess
        kwargs = {}
        if os.name == "nt":
            # DETACHED_PROCESS | CREATE_NO_WINDOW
            kwargs["creationflags"] = 0x00000008 | 0x08000000
        else:
            kwargs["start_new_session"] = True
        p = subprocess.Popen(
            [sys.executable, "-c", _DETACH_CODE, COLLECTOR_URL, COLLECTOR_TOKEN or ""],
            stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, **kwargs,
        )
        p.stdin.write(body)
        p.stdin.close()
        return True
    except Exception:
        return False


def _api(path: str, body: Optional[Dict[str, Any]] = None,
         timeout: float = TIMEOUT) -> Optional[Dict[str, Any]]:
    try:
        headers = {"Content-Type": "application/json"}
        if COLLECTOR_TOKEN:
            headers["Authorization"] = f"Bearer {COLLECTOR_TOKEN}"
        req = urllib.request.Request(
            API_BASE + path,
            data=json.dumps(body).encode() if body is not None else None,
            headers=headers,
            method="POST" if body is not None else "GET",
        )
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read())
    except Exception:
        return None


def maybe_remote_gate(event_data: Dict[str, Any]) -> None:
    """Away mode: hold a gated PreToolUse call for a remote decision.

    Prints a PreToolUse permissionDecision JSON if the dashboard answers in
    time; prints nothing (normal terminal flow) on timeout or any failure.
    """
    try:
        if event_data.get("hook_event_name") != "PreToolUse":
            return
        if event_data.get("tool_name") not in GATED_TOOLS:
            return

        hostname = socket.gethostname()
        instance_id = get_instance_id(hostname, event_data.get("session_id", "unknown"))

        # Fast check — is away mode on for this session? (collector down,
        # unknown session, or mode off all mean: do nothing.)
        mode = _api(f"/api/sessions/{instance_id}/mode", timeout=0.5)
        if not mode or not mode.get("remote_approval"):
            return

        decision = _api("/api/decisions", {
            "instance_id": instance_id,
            "summary": extract_summary(event_data),
            "payload": _slim_payload(event_data),
        })
        if not decision:
            return

        deadline = time.time() + APPROVAL_TIMEOUT
        while time.time() < deadline:
            time.sleep(1.0)
            current = _api(f"/api/decisions/{decision['id']}", timeout=1.5)
            if not current or current.get("status") == "pending":
                continue
            if current["status"] in ("allow", "deny"):
                reason = current.get("reason") or f"{current['status']}ed remotely via Agent Pulse"
                print(json.dumps({
                    "hookSpecificOutput": {
                        "hookEventName": "PreToolUse",
                        "permissionDecision": current["status"],
                        "permissionDecisionReason": reason,
                    }
                }))
            return  # expired or answered — either way stop holding
    except Exception:
        return


def maybe_deliver_followup(event_data: Dict[str, Any]) -> None:
    """On Stop, deliver a queued remote follow-up as a continuation: a
    Stop hook answering {"decision": "block", "reason": <text>} makes
    Claude keep working with that text as the instruction."""
    try:
        if event_data.get("hook_event_name") != "Stop":
            return
        # Never fight Claude's own stop-loop protection.
        if event_data.get("stop_hook_active"):
            return
        hostname = socket.gethostname()
        instance_id = get_instance_id(hostname, event_data.get("session_id", "unknown"))
        r = _api(f"/api/messages/next?instance_id={instance_id}", timeout=1.0)
        if r and r.get("text"):
            print(json.dumps({"decision": "block", "reason": r["text"]}))
    except Exception:
        return


if __name__ == "__main__":
    try:
        # Read bytes and decode UTF-8 explicitly: text-mode stdin uses the
        # OS locale (cp1252 on Windows), which turns emoji into lone
        # surrogates that later break UTF-8 JSON responses downstream.
        event_data = json.loads(sys.stdin.buffer.read().decode("utf-8", "replace"))
        post_event(event_data)
        maybe_remote_gate(event_data)
        maybe_deliver_followup(event_data)
    except Exception:
        pass

    sys.exit(0)
