"""Unit tests for adapter-side pure logic: Claude Code event mapping and
QR/network helpers. No collector or network needed."""
import pytest

from agent_pulse import hook_client, netinfo


# ---- Claude Code event mapping (hook_client) ------------------------

@pytest.mark.parametrize("hook_event_name,expected_type,should_send", [
    ("Notification", "needs_input", True),
    ("UserPromptSubmit", "user_prompt", True),
    ("Stop", "completed", True),
    ("SubagentStop", "subagent_completed", True),
    ("SessionStart", "session_start", True),
    ("SessionEnd", "session_end", True),
    ("PreToolUse", "activity", True),
])
def test_map_event_type(hook_event_name, expected_type, should_send):
    etype, send = hook_client.map_event_type({"hook_event_name": hook_event_name})
    assert (etype, send) == (expected_type, should_send)


def test_posttooluse_success_is_not_sent():
    # A successful tool completion would double every activity event.
    etype, send = hook_client.map_event_type(
        {"hook_event_name": "PostToolUse", "tool_response": {"ok": True}})
    assert send is False


def test_posttooluse_error_is_sent():
    etype, send = hook_client.map_event_type(
        {"hook_event_name": "PostToolUse", "tool_response": {"is_error": True}})
    assert (etype, send) == ("error", True)


def test_summary_includes_command_detail():
    s = hook_client.extract_summary({
        "hook_event_name": "PreToolUse", "tool_name": "Bash",
        "tool_input": {"command": "npm test"}})
    assert "Bash" in s and "npm test" in s


def test_summary_truncates_long_detail():
    s = hook_client.extract_summary({
        "hook_event_name": "PreToolUse", "tool_name": "Bash",
        "tool_input": {"command": "x" * 500}})
    assert len(s) < 200 and s.endswith("…")


def test_instance_id_is_stable():
    a = hook_client.get_instance_id("host", "sess-1")
    b = hook_client.get_instance_id("host", "sess-1")
    c = hook_client.get_instance_id("host", "sess-2")
    assert a == b and a != c


def test_slim_payload_truncates_bulk_fields():
    slim = hook_client._slim_payload({
        "hook_event_name": "PreToolUse", "tool_name": "Write",
        "tool_input": {"content": "y" * 1000}})
    assert len(slim["tool_input"]["content"]) <= 301


# ---- QR + netinfo ---------------------------------------------------

def test_qr_svg_produces_svg():
    svg = netinfo.qr_svg("https://example.com/?token=abc")
    assert svg and "<svg" in svg


def test_qr_terminal_renders_blocks():
    out = netinfo.qr_terminal("https://example.com")
    assert out and any(ch in out for ch in "█▀▄")


def test_reachable_urls_shape():
    urls = netinfo.reachable_urls(8765, "tok")
    # may be empty in a network-less CI sandbox, but must be well-formed
    for u in urls:
        assert "label" in u and u["url"].startswith("http")
        assert "token=tok" in u["url"]
