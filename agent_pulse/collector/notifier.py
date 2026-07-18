"""Push attention events to the user's phone / external channels.

Configured entirely via environment variables (all optional — with none
set, this module is inert):

    PULSE_NTFY_TOPIC          ntfy topic name -> https://ntfy.sh/<topic>
    PULSE_NTFY_SERVER         override ntfy server (default https://ntfy.sh)
    PULSE_TELEGRAM_BOT_TOKEN  Telegram bot token
    PULSE_TELEGRAM_CHAT_ID    Telegram chat id to message
    PULSE_WEBHOOK_URL         generic webhook; receives the event as JSON
    PULSE_NOTIFY_EVENTS       comma list (default "needs_input,error")

All sends are fail-silent and debounced (one push per instance+type per
60s window) so a flapping agent can't spam your phone.
"""
import os
import json
import time
import urllib.request
import urllib.parse
from typing import Dict, Any

NOTIFY_EVENTS = set(
    e.strip() for e in os.environ.get("PULSE_NOTIFY_EVENTS", "needs_input,error").split(",")
    if e.strip()
)
NTFY_TOPIC = os.environ.get("PULSE_NTFY_TOPIC")
NTFY_SERVER = os.environ.get("PULSE_NTFY_SERVER", "https://ntfy.sh").rstrip("/")
TG_TOKEN = os.environ.get("PULSE_TELEGRAM_BOT_TOKEN")
TG_CHAT = os.environ.get("PULSE_TELEGRAM_CHAT_ID")
WEBHOOK_URL = os.environ.get("PULSE_WEBHOOK_URL")

DEBOUNCE_SECONDS = 60
_last_sent: Dict[str, float] = {}


def enabled() -> bool:
    return bool(NTFY_TOPIC or (TG_TOKEN and TG_CHAT) or WEBHOOK_URL)


def _post(url: str, data: bytes, headers: Dict[str, str]) -> None:
    try:
        req = urllib.request.Request(url, data=data, headers=headers, method="POST")
        urllib.request.urlopen(req, timeout=5)
    except Exception:
        pass


def _title_for(event: Dict[str, Any]) -> str:
    cwd = event.get("cwd") or ""
    folder = cwd.rstrip("\\/").replace("\\", "/").split("/")[-1] if cwd else "agent"
    kind = "needs your input" if event["event_type"] == "needs_input" else event["event_type"]
    # Plain hyphen: this string travels in an HTTP header (latin-1 only).
    return f"{folder} - {kind}"


def notify_event(event: Dict[str, Any]) -> None:
    """Forward one Pulse event to all configured channels. Blocking —
    call from a thread (the collector uses run_in_executor)."""
    if not enabled() or event.get("event_type") not in NOTIFY_EVENTS:
        return

    key = f"{event.get('instance_id')}:{event.get('event_type')}"
    now = time.time()
    if now - _last_sent.get(key, 0) < DEBOUNCE_SECONDS:
        return
    _last_sent[key] = now

    title = _title_for(event)
    body = event.get("summary") or event.get("event_type", "")

    if NTFY_TOPIC:
        headers = {
            "Title": title.encode("ascii", "replace").decode(),
            "Priority": "high" if event["event_type"] in ("needs_input", "error") else "default",
            "Tags": "rotating_light" if event["event_type"] == "error" else "bell",
        }
        # Approve/deny straight from the notification when this is a
        # remote-approval request and the phone can reach the collector
        # (PULSE_PUBLIC_URL, e.g. the PC's Tailscale address).
        public = os.environ.get("PULSE_PUBLIC_URL", "").rstrip("/")
        decision_id = (event.get("payload") or {}).get("decision_id")
        if public and decision_id:
            token = os.environ.get("PULSE_COLLECTOR_TOKEN")
            respond = f"{public}/api/decisions/{decision_id}/respond"
            if token:
                respond += f"?token={urllib.parse.quote(token)}"
            headers["Actions"] = (
                f"http, Allow, {respond}, method=POST, "
                'headers.Content-Type=application/json, body={"status":"allow"}; '
                f"http, Deny, {respond}, method=POST, "
                'headers.Content-Type=application/json, body={"status":"deny"}'
            )
        elif public:
            headers["Click"] = public
        _post(
            f"{NTFY_SERVER}/{urllib.parse.quote(NTFY_TOPIC)}",
            body.encode(),
            headers,
        )

    if TG_TOKEN and TG_CHAT:
        _post(
            f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
            json.dumps({"chat_id": TG_CHAT, "text": f"{title}\n{body}"}).encode(),
            {"Content-Type": "application/json"},
        )

    if WEBHOOK_URL:
        _post(
            WEBHOOK_URL,
            json.dumps(event).encode(),
            {"Content-Type": "application/json"},
        )
