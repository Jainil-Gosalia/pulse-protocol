# Agent Pulse — one inbox for all your coding agents

Monitor every AI coding agent on your machine — Claude Code, OpenCode, Kilo, Codex, Cursor, and anything else — in a single live, session-centric dashboard. Each session shows up as a card with its live status (**working / needs input / error / idle / ended**), so you know which terminal needs you without tabbing through all of them.

Agent Pulse is the reference implementation of the **[Pulse Protocol](spec/pulse-protocol.md)** — an open spec (Apache-2.0) for session and attention events from coding agents. Any tool can join the inbox by POSTing one small JSON envelope; any dashboard can replace this one by implementing one endpoint. Emitters discover the collector via `PULSE_COLLECTOR_URL` (default `http://127.0.0.1:8765/api/events`) and authenticate with `PULSE_COLLECTOR_TOKEN` when set.

```
Claude Code ──(hooks)──────┐
OpenCode ────(plugin)──────┤
Kilo ────────(plugin)──────┼──▶  Collector (FastAPI @ localhost:8765)
Codex ───────(notify)──────┤         │            │
Cursor ──────(hooks.json)──┤      SQLite      WebSocket ──▶ Dashboard
anything ────(pulse_run)───┘     (events.db)               (sessions + live feed)
```

## Quick start

```bash
pipx install agent-pulse        # or: pip install agent-pulse / uvx agent-pulse serve
                                # (from this repo before PyPI publish: pip install -e .)

agent-pulse serve               # collector + dashboard on http://localhost:8765
agent-pulse install claude_code # wire up each tool you use (shows the change, asks first)
agent-pulse install opencode
agent-pulse install kilo
agent-pulse status              # collector health + live session list
agent-pulse conformance         # validate a collector against the Pulse spec
```

`install` merges the hook/plugin config globally (`~/.claude/settings.json`, `~/.config/opencode/plugin/`, …) with a backup, or into the current project with `--project`. Restart running agent sessions afterwards — hooks load at startup. Every adapter fails silently: if the collector isn't running, your agents are completely unaffected.

Sanity-check the stack any time with `python quick_verify.py` (uses an isolated port + database). `agent-pulse conformance [--url … --token …]` runs the Pulse Protocol conformance harness against any collector (yours or a third party's) — it drives the full event vocabulary, checks the session state machine and response shapes, and reports CONFORMANT / NOT CONFORMANT. This is the credibility tool for the spec.

## Access from your phone

No app needed — two pieces:

1. **Push notifications**: install the [ntfy](https://ntfy.sh) app, subscribe to a topic, and set `PULSE_NTFY_TOPIC` (see below). Attention events reach your phone anywhere.
2. **The dashboard itself** is a mobile-friendly PWA. Serve it beyond loopback **with a token**:

   ```bash
   agent-pulse serve --host 0.0.0.0 --token some-long-secret
   agent-pulse setup-phone   # opens the firewall via one UAC click, prints your phone URLs
   ```

   - **Same Wi-Fi**: open `http://<your-pc-ip>:8765/?token=some-long-secret` on the phone (the token sticks in localStorage after the first visit; allow port 8765 in Windows Firewall). Use the browser's *Add to Home Screen* for an app-like standalone window.
   - **From anywhere (recommended)**: install [Tailscale](https://tailscale.com) on PC + phone and use the PC's Tailscale IP instead — no ports exposed to the internet, works from any network. Keep the token anyway; anyone who can reach the port can otherwise read prompts and approve tool calls.
   - Loopback connections skip the token, so emitters on the same machine keep working with zero config; only network clients (your phone, emitters on other machines via `PULSE_COLLECTOR_TOKEN`) need it.

## The dashboard

- **Session cards, grouped by what matters**: *Needs attention* (waiting for permission / errored) on top, then *Working* (with live pulse), *Idle* (finished, awaiting your next prompt), and *Ended* (collapsed).
- Each card: tool badge, project folder, live status, the agent's current/last action, and how long since it last reported. A "quiet Xm" hint flags working sessions that have gone silent.
- **Click a card** to open that session's full event timeline; click again to go back to the all-activity feed.
- **Browser notifications** (toggle in the top bar) fire when any session needs input or errors — the tab title also shows an attention counter like `(2!) Agent Pulse`.
- Feed filters: all / needs input / errors / completed.

## Normalized event model

Every adapter maps its tool's native events onto the [Pulse Protocol](spec/pulse-protocol.md) schema, so the collector and UI are tool-agnostic:

| event_type | meaning | session status becomes |
|---|---|---|
| `session_start` | agent session began | idle |
| `user_prompt` | user submitted a prompt | working |
| `activity` | tool call / progress | working |
| `subagent_completed` | a subagent finished | working |
| `needs_input` | waiting on permission/input | **needs_input** |
| `error` | tool failure / session error | **error** |
| `completed` | agent finished responding | idle |
| `session_end` | session closed | ended |

`POST /api/events` accepts: `instance_id`, `hostname`, `source`, `event_type`, `summary`, `payload`, `session_id`, `cwd`, `hook_event_name`. Other endpoints: `GET /api/events`, `GET /api/sessions`, `GET /api/sessions/{instance_id}/events`, `WS /ws`.

## Adapters

Replace `<PULSE>` with this repo's absolute path (forward slashes work on Windows, e.g. `C:/d/Projects/my_apps/agent_pulse`).

### Claude Code ✅ (tested)

`agent-pulse install claude_code` does this for you. Manually: add to `~/.claude/settings.json` for all projects, or `<project>/.claude/settings.json` for one project (install globally *or* per-project, not both — Claude Code runs both sets, which would double every event):

```json
{
  "hooks": {
    "SessionStart":     [{ "hooks": [{ "type": "command", "command": "python \"<PULSE>/agent_pulse/hook_client.py\"" }] }],
    "SessionEnd":       [{ "hooks": [{ "type": "command", "command": "python \"<PULSE>/agent_pulse/hook_client.py\"" }] }],
    "UserPromptSubmit": [{ "hooks": [{ "type": "command", "command": "python \"<PULSE>/agent_pulse/hook_client.py\"" }] }],
    "Notification":     [{ "hooks": [{ "type": "command", "command": "python \"<PULSE>/agent_pulse/hook_client.py\"" }] }],
    "Stop":             [{ "hooks": [{ "type": "command", "command": "python \"<PULSE>/agent_pulse/hook_client.py\"" }] }],
    "SubagentStop":     [{ "hooks": [{ "type": "command", "command": "python \"<PULSE>/agent_pulse/hook_client.py\"" }] }],
    "PreToolUse":       [{ "matcher": "*", "hooks": [{ "type": "command", "command": "python \"<PULSE>/agent_pulse/hook_client.py\"" }] }],
    "PostToolUse":      [{ "matcher": "*", "hooks": [{ "type": "command", "command": "python \"<PULSE>/agent_pulse/hook_client.py\"" }] }]
  }
}
```

Already-running sessions load hooks at startup — restart them to start reporting.

### OpenCode

Copy `agent_pulse/adapters/opencode_plugin.js` to `~/.config/opencode/plugin/agent-pulse.js` (global) or `<project>/.opencode/plugin/agent-pulse.js`. Maps `session.created/idle/error`, `permission.asked`, `tool.execute.before`, `session.deleted`.

### Kilo (CLI + VS Code extension)

Copy `agent_pulse/adapters/kilo_plugin.js` to `~/.config/kilo/plugin/agent-pulse.js` (global) or `<project>/.kilo/plugin/agent-pulse.js`. Kilo's plugin API is OpenCode-compatible; only the export shape differs.

### Codex CLI ⚠️ (untested — codex not installed on this machine)

Add to `~/.codex/config.toml`:

```toml
notify = ["python", "<PULSE>/agent_pulse/adapters/codex_notify.py"]
```

Codex currently only invokes notify on turn completion (and approval events in newer builds), so expect a coarser feed: completions and approvals, not per-tool activity.

### Cursor ⚠️ (untested — cursor not installed on this machine)

Create `~/.cursor/hooks.json` (global) or `<project>/.cursor/hooks.json` — full snippet in the header of `agent_pulse/adapters/cursor_hook.py`. The script always answers `{"permission": "allow"}`; it observes, never gates.

### Everything else (aider, gemini, custom scripts, …)

No hooks needed — launch through the generic wrapper:

```bash
python <PULSE>/agent_pulse/adapters/pulse_run.py --source aider -- aider --model gpt-4o
```

Reports `session_start` / `error` (nonzero exit) / `session_end` around any command, with stdio passed straight through so interactive TUIs work normally.

## Away mode — approve permissions remotely

Toggle **🌙 away** on any session card and that agent's permission prompts get held for you: a pending-approval banner appears at the top of the dashboard (and pushes to your phone if notifications are configured) with **Allow / Deny** buttons. The agent waits up to 45s for your answer, then falls back to its normal terminal prompt — away mode can never make an agent more stuck than it was.

- **Claude Code**: gated tools default to `Bash,PowerShell,Write,Edit,NotebookEdit` (override with `PULSE_GATED_TOOLS`); hold time via `PULSE_APPROVAL_TIMEOUT` (default 45s, keep below your hook timeout).
- **OpenCode / Kilo**: gates actual permission prompts via the `permission.ask` plugin hook — no tool list needed.
- Pending decisions expire after `PULSE_DECISION_TTL` (default 90s).

**Follow-up messages (💬 on a session card):** queue a message for any session — when the agent finishes its current turn, the message is delivered as a continuation and the agent keeps working on it. Queue "also update the README" from your phone while it works; it picks that up instead of stopping. Messages for idle sessions wait until the end of its next turn. Works for Claude Code (Stop-hook continuation) and OpenCode/Kilo (injected via `client.session.prompt` on `session.idle`).

## Phone push notifications

The collector forwards attention events (`needs_input`, `error`) to any of these, all optional and env-configured — set one and restart the collector:

```bash
PULSE_NTFY_TOPIC=my-agents          # ntfy.sh topic (install the ntfy app, subscribe to the topic)
PULSE_NTFY_SERVER=https://ntfy.sh   # optional self-hosted ntfy
PULSE_TELEGRAM_BOT_TOKEN=...        # plus PULSE_TELEGRAM_CHAT_ID
PULSE_WEBHOOK_URL=https://...       # generic: receives the event JSON
PULSE_NOTIFY_EVENTS=needs_input,error  # which events push (default shown)
```

Pushes are debounced (one per session+type per 60s) so a flapping agent can't spam you.

## Lifecycle housekeeping

- **Stale reaper**: a `working` session silent for `PULSE_STALE_MINUTES` (default 10) is marked **stale** — the agent likely crashed or its terminal closed without a `session_end`. Any new event revives it. Long-quiet wrapped tools stay alive via `heartbeat` events (`pulse_run` sends them every 60s automatically).
- **Retention**: events older than `PULSE_RETENTION_DAYS` (default 14) are pruned hourly; session cards are kept.

## Design guarantees

- **Adapters never interfere.** Every adapter swallows all errors, uses short timeouts (Claude Code reporting: `PULSE_REPORT_TIMEOUT`, default 1.5s), and the JS plugins are fire-and-forget (they don't await the POST). A dead collector costs your agents nothing. For a *remote* collector where even a fast POST adds round-trip latency to every tool call, set `PULSE_DETACH=1` so the Claude Code hook fires reporting POSTs in a detached child and returns immediately (on localhost the synchronous POST is already faster than spawning, so leave it off).
- **Payloads are slimmed** before sending (long strings truncated) so the feed stays light.
- **Sessions are keyed** by `instance_id = hash(hostname + session_id)`, so the schema already works across multiple machines pointing at one collector — change `COLLECTOR_URL` in an adapter to a reachable host to try it.

## Repo layout

- `spec/pulse-protocol.md` — the Pulse Protocol specification (v0.1 draft)
- `spec/pulse-event.schema.json` — JSON Schema for the event envelope
- `agent_pulse/cli.py` — the `agent-pulse` CLI (serve / install / status / conformance / setup-phone / autostart)
- `agent_pulse/conformance.py` — spec conformance harness
- `agent_pulse/collector/main.py` — FastAPI app: REST + WebSocket (reference collector)
- `agent_pulse/collector/db.py` — SQLite (events + sessions, status transitions)
- `agent_pulse/collector/static/index.html` — dashboard (no build step)
- `agent_pulse/hook_client.py` — Claude Code adapter (stdin → POST)
- `agent_pulse/adapters/` — opencode, kilo, codex, cursor, generic wrapper (reference emitters)
- `quick_verify.py` — isolated end-to-end check; `test_events.py` — demo data

## License

Apache-2.0 — see [LICENSE](LICENSE).
