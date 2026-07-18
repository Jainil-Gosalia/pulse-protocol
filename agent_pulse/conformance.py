"""Pulse Protocol conformance harness.

Point it at any collector and it validates the collector against the spec
(spec/pulse-protocol.md): event ingest, the session state machine, the
query endpoints, and response shapes. Dependency-free — stdlib only, in
keeping with the protocol's "no SDK" philosophy.

    agent-pulse conformance [--url http://host:8765/api/events] [--token T]

Exit code 0 = fully conformant, 1 = one or more checks failed.
Uses a unique throwaway instance_id so it never disturbs real sessions.
"""
import json
import os
import time
import uuid
import urllib.request
from typing import Any, Dict, List, Optional, Tuple

# event_type -> expected derived session status (spec section 6).
EXPECTED_STATUS = [
    ("session_start", "idle"),
    ("user_prompt", "working"),
    ("activity", "working"),
    ("needs_input", "needs_input"),
    ("error", "error"),
    ("completed", "idle"),
    ("session_end", "ended"),
]

REQUIRED_EVENT_FIELDS = ["instance_id", "hostname", "source", "event_type",
                         "summary", "event_type", "created_at"]
REQUIRED_SESSION_FIELDS = ["instance_id", "status", "last_summary", "last_seen"]


class Harness:
    def __init__(self, events_url: str, token: Optional[str]):
        self.events_url = events_url.rstrip("/")
        self.base = self.events_url.rsplit("/api/events", 1)[0]
        self.token = token
        self.instance = "conformance-" + uuid.uuid4().hex[:8]
        self.results: List[Tuple[bool, str]] = []

    # --- transport ---------------------------------------------------
    def _req(self, path: str, body: Optional[dict] = None) -> Tuple[int, Any]:
        url = self.base + path if path.startswith("/") else path
        headers = {"Content-Type": "application/json"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, headers=headers,
                                     method="POST" if body is not None else "GET")
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                raw = r.read().decode("utf-8")
                return r.status, (json.loads(raw) if raw else None)
        except urllib.error.HTTPError as e:
            return e.code, None
        except Exception as e:
            return 0, str(e)

    def _emit(self, event_type: str, summary: str = "conformance") -> Tuple[int, Any]:
        return self._req("/api/events", {
            "spec_version": "0.1",
            "instance_id": self.instance,
            "hostname": "conformance-host",
            "source": "conformance",
            "event_type": event_type,
            "summary": summary,
            "payload": {"probe": True},
            "session_id": self.instance,
            "cwd": "/conformance",
        })

    # --- checks ------------------------------------------------------
    def check(self, ok: bool, label: str):
        self.results.append((bool(ok), label))

    def _sessions(self) -> List[dict]:
        _, data = self._req("/api/sessions")
        return data if isinstance(data, list) else []

    def _my_session(self) -> Optional[dict]:
        return next((s for s in self._sessions() if s.get("instance_id") == self.instance), None)

    def run(self) -> bool:
        # 1. Ingest returns 2xx (spec section 7: the one required endpoint).
        status, body = self._emit("session_start", "conformance start")
        self.check(200 <= status < 300, f"POST /api/events accepts an event (got {status})")

        # 2. Session state machine (spec section 6).
        for event_type, expected in EXPECTED_STATUS[1:]:
            self._emit(event_type)
            time.sleep(0.05)
            sess = self._my_session()
            got = sess.get("status") if sess else None
            self.check(got == expected,
                       f"{event_type} -> status '{expected}' (got '{got}')")

        # 3. Event history round-trips with required fields.
        st, events = self._req(f"/api/events?instance_id={self.instance}&limit=20")
        events = events if isinstance(events, list) else []
        self.check(len(events) >= len(EXPECTED_STATUS),
                   f"GET /api/events returns this session's events ({len(events)})")
        if events:
            missing = [f for f in REQUIRED_EVENT_FIELDS if f not in events[0]]
            self.check(not missing, f"event shape has required fields "
                       f"({'missing ' + ','.join(missing) if missing else 'ok'})")

        # 4. Session list shape.
        sess = self._my_session()
        self.check(sess is not None, "GET /api/sessions includes this instance")
        if sess:
            missing = [f for f in REQUIRED_SESSION_FIELDS if f not in sess]
            self.check(not missing, f"session shape has required fields "
                       f"({'missing ' + ','.join(missing) if missing else 'ok'})")

        # 5. Unknown event_type is accepted (spec section 5/6: forward-compat).
        st, _ = self._emit("some_future_type_xyz")
        self.check(200 <= st < 300, f"unknown event_type accepted (got {st})")

        # 6. Optional per-session timeline endpoint.
        st, tl = self._req(f"/api/sessions/{self.instance}/events")
        self.check(st == 200 and isinstance(tl, list),
                   "GET /api/sessions/{id}/events returns a timeline")

        return all(ok for ok, _ in self.results)

    def report(self) -> bool:
        passed = sum(1 for ok, _ in self.results if ok)
        print(f"Pulse Protocol conformance — {self.base}")
        print(f"  test instance: {self.instance}\n")
        for ok, label in self.results:
            print(f"  {'PASS' if ok else 'FAIL'}  {label}")
        allok = passed == len(self.results)
        print(f"\n  {passed}/{len(self.results)} checks passed — "
              f"{'CONFORMANT' if allok else 'NOT CONFORMANT'}")
        return allok


def run(url: Optional[str] = None, token: Optional[str] = None) -> int:
    events_url = (url or os.environ.get("PULSE_COLLECTOR_URL")
                  or "http://127.0.0.1:8765/api/events")
    token = token or os.environ.get("PULSE_COLLECTOR_TOKEN")
    h = Harness(events_url, token)
    try:
        h.run()
    except Exception as e:
        print(f"Harness error against {events_url}: {e}")
        return 1
    return 0 if h.report() else 1
