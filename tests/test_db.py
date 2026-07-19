"""Unit tests for the collector's storage + state-machine core."""
import time
import pytest

from agent_pulse.collector import db


def emit(instance_id, event_type, summary="x", source="test",
         session_id="s", cwd="/proj", payload=None):
    return db.insert_event(
        instance_id=instance_id, hostname="host", event_type=event_type,
        summary=summary, payload=payload or {}, source=source,
        session_id=session_id, cwd=cwd)


def status_of(instance_id):
    s = db.get_session(instance_id)
    return s["status"] if s else None


# ---- session state machine (spec §6) --------------------------------

@pytest.mark.parametrize("event_type,expected", [
    ("session_start", "idle"),
    ("user_prompt", "working"),
    ("activity", "working"),
    ("subagent_completed", "working"),
    ("needs_input", "needs_input"),
    ("error", "error"),
    ("completed", "idle"),
    ("session_end", "ended"),
])
def test_event_maps_to_status(event_type, expected):
    r = emit("i1", event_type)
    assert r["session"]["status"] == expected


def test_full_lifecycle_sequence():
    for et, expected in [("session_start", "idle"), ("user_prompt", "working"),
                         ("needs_input", "needs_input"), ("completed", "idle"),
                         ("session_end", "ended")]:
        emit("seq", et)
        assert status_of("seq") == expected


def test_unknown_event_type_leaves_status_but_is_stored():
    emit("u", "user_prompt")            # -> working
    emit("u", "some_future_type")       # not in table -> unchanged
    assert status_of("u") == "working"
    assert len(db.get_events(instance_id="u")) == 2


def test_session_end_sets_ended_at():
    emit("e", "session_start")
    emit("e", "session_end")
    assert db.get_session("e")["ended_at"] is not None


def test_heartbeat_touches_without_event_row():
    emit("hb", "user_prompt")
    before = len(db.get_events(instance_id="hb"))
    db.touch_session("hb")
    assert len(db.get_events(instance_id="hb")) == before
    assert status_of("hb") == "working"


# ---- reaper + idle timeout ------------------------------------------

def test_stale_reaper_only_touches_working():
    emit("w", "user_prompt")     # working
    emit("d", "completed")       # idle
    time.sleep(0.02)
    reaped = {s["instance_id"] for s in db.reap_stale_sessions(0)}
    assert reaped == {"w"}
    assert status_of("w") == "stale"
    assert status_of("d") == "idle"


def test_idle_ages_out_to_ended_and_revives():
    emit("gone", "completed")    # idle
    time.sleep(0.02)
    ended = {s["instance_id"] for s in db.end_abandoned_sessions(0)}
    assert "gone" in ended
    assert status_of("gone") == "ended"
    emit("gone", "user_prompt")  # any event revives it
    assert status_of("gone") == "working"


def test_stale_also_ages_out_to_ended():
    emit("st", "user_prompt")
    time.sleep(0.02)
    db.reap_stale_sessions(0)             # -> stale
    assert status_of("st") == "stale"
    db.end_abandoned_sessions(0)          # stale is abandoned too
    assert status_of("st") == "ended"


# ---- delete ----------------------------------------------------------

def test_delete_session_removes_row_and_events():
    emit("del", "user_prompt")
    assert db.delete_session("del") is True
    assert db.get_session("del") is None
    assert db.get_events(instance_id="del") == []


def test_delete_unknown_returns_false():
    assert db.delete_session("nope") is False


def test_bulk_clear_by_status():
    emit("a", "session_end")     # ended
    emit("b", "session_end")     # ended
    emit("c", "user_prompt")     # working
    removed = set(db.delete_sessions_by_status("ended"))
    assert removed == {"a", "b"}
    assert {s["instance_id"] for s in db.get_sessions()} == {"c"}


# ---- surrogate scrub (blank-page regression) ------------------------

def test_lone_surrogate_is_scrubbed():
    emit("s", "activity", summary="bad \udc8f char", payload={"c": "x \udc8f y"})
    rows = db.get_events(instance_id="s")
    assert len(rows) == 1
    assert "\udc8f" not in rows[0]["summary"]
    import json
    json.dumps(rows)  # must not raise


# ---- decisions -------------------------------------------------------

def test_decision_lifecycle():
    emit("dsess", "working")
    d = db.create_decision("d1", "dsess", "run rm -rf", {}, ttl_seconds=90)
    assert d["status"] == "pending"
    assert any(x["id"] == "d1" for x in db.get_pending_decisions())
    answered = db.respond_decision("d1", "allow", "ok")
    assert answered["status"] == "allow"
    assert db.get_pending_decisions() == []
    # already-answered decisions can't be answered again
    assert db.respond_decision("d1", "deny") is None


def test_expired_decisions():
    db.create_decision("d2", "x", "s", {}, ttl_seconds=0)
    time.sleep(0.02)
    expired = {d["id"] for d in db.expire_decisions()}
    assert "d2" in expired
    assert db.get_decision("d2")["status"] == "expired"


# ---- follow-up messages ---------------------------------------------

def test_message_queue_is_fifo_and_drains():
    db.queue_message("m", "first")
    db.queue_message("m", "second")
    assert db.pop_message("m") == "first"
    assert db.pop_message("m") == "second"
    assert db.pop_message("m") is None


# ---- stats -----------------------------------------------------------

def test_stats_counts_and_response_latency():
    emit("st1", "needs_input")
    time.sleep(0.05)
    emit("st1", "completed")     # answered ~50ms after needs_input
    stats = db.get_stats(days=7)
    assert stats["sessions"] >= 1
    assert stats["median_response_s"] is not None
