import os
import uuid
import asyncio
from fastapi import FastAPI, WebSocket, Query, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from pathlib import Path
from typing import Optional, Set
from pydantic import BaseModel

from agent_pulse.collector.db import (
    init_db, insert_event, get_events, get_sessions, get_session,
    set_session_mode, touch_session, reap_stale_sessions, prune_events,
    create_decision, get_decision, respond_decision, get_pending_decisions,
    expire_decisions, get_stats, queue_message, pop_message,
)
from agent_pulse.collector import notifier
from agent_pulse import netinfo

STALE_MINUTES = int(os.environ.get("PULSE_STALE_MINUTES", "10"))
RETENTION_DAYS = int(os.environ.get("PULSE_RETENTION_DAYS", "14"))
DECISION_TTL = int(os.environ.get("PULSE_DECISION_TTL", "90"))
AUTH_TOKEN = os.environ.get("PULSE_COLLECTOR_TOKEN")

app = FastAPI(title="Agent Pulse")


def _request_token(request) -> Optional[str]:
    auth = request.headers.get("authorization", "")
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    return request.query_params.get("token")


def _is_loopback(client) -> bool:
    return client is not None and client.host in ("127.0.0.1", "::1")


@app.middleware("http")
async def _auth_middleware(request: Request, call_next):
    # When a token is configured, /api routes require it from the network.
    # Loopback stays open so local emitters keep working untouched — a
    # local process could read the database directly anyway.
    # Proxied/tunneled requests (cloudflared, reverse proxies) reach us from
    # loopback but are NOT local — the forwarding headers give them away.
    forwarded = request.headers.get("cf-connecting-ip") or request.headers.get("x-forwarded-for")
    if AUTH_TOKEN and request.url.path.startswith("/api") and (forwarded or not _is_loopback(request.client)):
        if _request_token(request) != AUTH_TOKEN:
            return JSONResponse({"detail": "unauthorized"}, status_code=401)
    return await call_next(request)

init_db()

STATIC_DIR = Path(__file__).parent / "static"

ws_clients: Set[WebSocket] = set()


class Event(BaseModel):
    instance_id: str
    hostname: str
    event_type: str
    summary: str
    payload: dict = {}
    source: str = "unknown"
    session_id: Optional[str] = None
    cwd: Optional[str] = None
    hook_event_name: Optional[str] = None
    spec_version: str = "0.1"
    timestamp: Optional[str] = None


class DecisionRequest(BaseModel):
    instance_id: str
    summary: str
    payload: dict = {}


class DecisionResponse(BaseModel):
    status: str  # "allow" | "deny"
    reason: str = ""


class SessionMode(BaseModel):
    remote_approval: bool


async def broadcast(message: dict):
    for client in list(ws_clients):
        try:
            await client.send_json(message)
        except Exception:
            ws_clients.discard(client)


def _notify_bg(event: dict):
    """Push to phone channels without blocking the request."""
    if notifier.enabled():
        asyncio.get_running_loop().run_in_executor(None, notifier.notify_event, event)


@app.post("/api/events")
async def post_event(event: Event):
    # Heartbeats refresh the session without polluting the event feed.
    if event.event_type == "heartbeat":
        session = touch_session(event.instance_id)
        if session:
            await broadcast({"type": "session", "data": session})
        return {"ok": True, "heartbeat": True}

    result = insert_event(
        instance_id=event.instance_id,
        hostname=event.hostname,
        event_type=event.event_type,
        summary=event.summary,
        payload=event.payload,
        source=event.source,
        session_id=event.session_id,
        cwd=event.cwd,
        hook_event_name=event.hook_event_name,
        created_at=event.timestamp,
    )
    await broadcast({"type": "event", "data": result["event"]})
    await broadcast({"type": "session", "data": result["session"]})
    _notify_bg(result["event"])
    return result["event"]


@app.get("/api/events")
async def get_events_endpoint(
    instance_id: Optional[str] = Query(None),
    event_type: Optional[str] = Query(None),
    source: Optional[str] = Query(None),
    since: Optional[str] = Query(None),
    limit: int = Query(100),
):
    return get_events(instance_id=instance_id, event_type=event_type,
                      source=source, since=since, limit=limit)


@app.get("/api/sessions")
async def get_sessions_endpoint(include_ended: bool = Query(True)):
    return get_sessions(include_ended=include_ended)


@app.get("/api/stats")
async def get_stats_endpoint(days: int = Query(7, ge=1, le=90)):
    return get_stats(days=days)


@app.get("/api/sessions/{instance_id}/events")
async def get_session_events(instance_id: str, limit: int = Query(200)):
    return get_events(instance_id=instance_id, limit=limit)


@app.get("/api/sessions/{instance_id}/mode")
async def get_session_mode(instance_id: str):
    session = get_session(instance_id)
    if not session:
        return {"remote_approval": False}
    return {"remote_approval": session["remote_approval"]}


@app.post("/api/sessions/{instance_id}/mode")
async def set_session_mode_endpoint(instance_id: str, mode: SessionMode):
    session = set_session_mode(instance_id, mode.remote_approval)
    if not session:
        raise HTTPException(404, "unknown session")
    await broadcast({"type": "session", "data": session})
    return session


# ---------- remote approval (Pulse Protocol remote-approval extension) ----

@app.post("/api/decisions")
async def create_decision_endpoint(req: DecisionRequest):
    decision = create_decision(uuid.uuid4().hex[:12], req.instance_id,
                               req.summary, req.payload, DECISION_TTL)
    # A pending decision *is* a needs-input moment: emit the event so the
    # session flips to needs_input and phone push fires through one path.
    session = get_session(req.instance_id)
    result = insert_event(
        instance_id=req.instance_id,
        hostname=(session or {}).get("hostname") or "unknown",
        event_type="needs_input",
        summary=f"Approval requested: {req.summary}",
        payload={"decision_id": decision["id"], **req.payload},
        source=(session or {}).get("source") or "unknown",
        session_id=(session or {}).get("session_id"),
        cwd=(session or {}).get("cwd"),
        hook_event_name="remote_approval",
    )
    await broadcast({"type": "decision", "data": decision})
    await broadcast({"type": "event", "data": result["event"]})
    await broadcast({"type": "session", "data": result["session"]})
    _notify_bg(result["event"])
    return decision


@app.get("/api/decisions")
async def list_decisions():
    return get_pending_decisions()


@app.get("/api/decisions/{decision_id}")
async def get_decision_endpoint(decision_id: str):
    decision = get_decision(decision_id)
    if not decision:
        raise HTTPException(404, "unknown decision")
    return decision


@app.post("/api/decisions/{decision_id}/respond")
async def respond_decision_endpoint(decision_id: str, resp: DecisionResponse):
    if resp.status not in ("allow", "deny"):
        raise HTTPException(400, "status must be 'allow' or 'deny'")
    decision = respond_decision(decision_id, resp.status, resp.reason)
    if not decision:
        raise HTTPException(409, "decision is not pending")
    session = get_session(decision["instance_id"])
    result = insert_event(
        instance_id=decision["instance_id"],
        hostname=(session or {}).get("hostname") or "unknown",
        event_type="activity",
        summary=f"Remote decision: {resp.status} — {decision['summary']}",
        payload={"decision_id": decision_id, "status": resp.status},
        source=(session or {}).get("source") or "unknown",
        session_id=(session or {}).get("session_id"),
        cwd=(session or {}).get("cwd"),
        hook_event_name="remote_approval",
    )
    await broadcast({"type": "decision", "data": decision})
    await broadcast({"type": "event", "data": result["event"]})
    await broadcast({"type": "session", "data": result["session"]})
    return decision


# ---------- remote follow-up messages ----------

class MessageRequest(BaseModel):
    instance_id: str
    text: str


@app.post("/api/messages")
async def post_message(req: MessageRequest):
    """Queue a follow-up for a session; the emitter delivers it when the
    agent finishes its current turn (Claude Code: Stop-hook continuation)."""
    if not req.text.strip():
        raise HTTPException(400, "empty message")
    return queue_message(req.instance_id, req.text.strip())


@app.get("/api/messages/next")
async def next_message(instance_id: str = Query(...)):
    text = pop_message(instance_id)
    if text:
        session = get_session(instance_id)
        result = insert_event(
            instance_id=instance_id,
            hostname=(session or {}).get("hostname") or "unknown",
            event_type="user_prompt",
            summary=f"Remote follow-up: {text[:100]}",
            payload={"remote_message": text[:500]},
            source=(session or {}).get("source") or "unknown",
            session_id=(session or {}).get("session_id"),
            cwd=(session or {}).get("cwd"),
            hook_event_name="remote_message",
        )
        await broadcast({"type": "event", "data": result["event"]})
        await broadcast({"type": "session", "data": result["session"]})
    return {"text": text}


# ---------- websocket / static ----------

@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    ws_forwarded = (websocket.headers.get("cf-connecting-ip")
                    or websocket.headers.get("x-forwarded-for"))
    if (AUTH_TOKEN and (ws_forwarded or not _is_loopback(websocket.client))
            and websocket.query_params.get("token") != AUTH_TOKEN):
        await websocket.close(code=4401)
        return
    await websocket.accept()
    ws_clients.add(websocket)
    try:
        while True:
            await websocket.receive_text()
    except Exception:
        ws_clients.discard(websocket)


@app.get("/")
async def index():
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/manifest.webmanifest")
async def manifest():
    icon = ("data:image/svg+xml,<svg xmlns='http://www.w3.org/2000/svg' "
            "viewBox='0 0 100 100'><rect width='100' height='100' rx='20' "
            "fill='%230d1117'/><text y='.9em' font-size='72' x='14'>⚡</text></svg>")
    return JSONResponse({
        "name": "Agent Pulse", "short_name": "Pulse",
        "start_url": "/", "display": "standalone",
        "background_color": "#0d1117", "theme_color": "#0d1117",
        "icons": [{"src": icon, "sizes": "any", "type": "image/svg+xml"}],
    }, media_type="application/manifest+json")


@app.get("/health")
async def health():
    return {"status": "ok"}


# These are sync (not async) on purpose: they do blocking work — a
# `tailscale` subprocess, a socket probe, QR generation — and FastAPI runs
# sync endpoints in a threadpool, so a slow probe can't freeze the event loop.
@app.get("/api/connect-info")
def connect_info(request: Request):
    """URLs a phone can use to reach this collector, for the 'Connect phone'
    QR panel. Derives the port from the request; includes the token so the
    scanned URL just works."""
    port = request.url.port or 8765
    urls = netinfo.reachable_urls(port, AUTH_TOKEN)
    bound = os.environ.get("PULSE_BOUND_HOST") or os.environ.get("PULSE_HOST") or "127.0.0.1"
    loopback_only = bound in ("127.0.0.1", "localhost", "::1")
    return {"urls": urls, "has_token": bool(AUTH_TOKEN), "loopback_only": loopback_only}


@app.get("/api/qr")
def qr(data: str = Query(..., max_length=1024)):
    """Render a QR code (SVG) for an arbitrary string — used by the
    dashboard's Connect-phone panel."""
    svg = netinfo.qr_svg(data)
    if svg is None:
        raise HTTPException(501, "qrcode library not available")
    return Response(svg, media_type="image/svg+xml",
                    headers={"Cache-Control": "no-store"})


# ---------- housekeeping loop ----------

async def _housekeeping():
    tick = 0
    while True:
        try:
            for session in reap_stale_sessions(STALE_MINUTES):
                await broadcast({"type": "session", "data": session})
            for decision in expire_decisions():
                await broadcast({"type": "decision", "data": decision})
            tick += 1
            if tick % 60 == 0:  # roughly hourly
                prune_events(RETENTION_DAYS)
        except Exception:
            pass
        await asyncio.sleep(30)


@app.on_event("startup")
async def _start_housekeeping():
    asyncio.create_task(_housekeeping())


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=8765)
