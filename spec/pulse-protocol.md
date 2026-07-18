# Pulse Protocol — v0.1 (draft)

**Session and attention events for coding agents.**

Status: draft · License: Apache-2.0 · Reference implementation: [Agent Pulse](../README.md)

The key words MUST, MUST NOT, SHOULD, SHOULD NOT, MAY are to be interpreted as described in [RFC 2119](https://www.rfc-editor.org/rfc/rfc2119).

---

## 1. Why

Developers increasingly run many coding agents at once — Claude Code, OpenCode, Kilo, Codex, Cursor, and others — across terminals, editors, and machines. Each tool has a proprietary eventing surface (hooks, plugins, notify programs), so every monitoring effort re-integrates every tool from scratch: the same N×M problem the Model Context Protocol solved for tool access.

Pulse Protocol standardizes the *other* direction: a minimal, uniform way for any coding agent (or an adapter wrapped around it) to report **session lifecycle and human-attention events** to a local collector.

**Goals**

- Answer one question well: *which of my agents needs me right now?*
- Emittable from anything: one HTTP POST, no SDK, no dependencies.
- Human attention (`needs_input`) as a first-class signal, not an afterthought.
- Zero risk to the host agent: emitters are fail-silent by contract.

**Non-goals**

- Full observability (spans, tokens, cost) — that is [OpenTelemetry GenAI](#82-opentelemetry-genai-semantic-conventions)'s job; see the mapping in §8.
- Rich UI streaming (token-by-token text, state deltas) — that is AG-UI's job.
- Driving or controlling agents — one-way in v0.1; a command channel is a candidate for v0.2.

## 2. Terminology

| Term | Meaning |
|---|---|
| **Emitter** | Anything that sends Pulse events: a native tool integration, a hook script, a plugin, or a wrapper process. |
| **Collector** | An HTTP endpoint that receives, stores, and/or forwards Pulse events. |
| **Session** | One conversational run of an agent (one Claude Code session, one OpenCode session, …). |
| **Instance** | A session observed on a specific host — the unit the collector tracks. |

## 3. Transport and discovery

- Events are delivered as `POST` requests with a JSON body and `Content-Type: application/json`.
- Emitters MUST resolve the collector endpoint in this order:
  1. the `PULSE_COLLECTOR_URL` environment variable, if set;
  2. the default: `http://127.0.0.1:8765/api/events`.
- Collectors MUST respond `2xx` on accepted events. Emitters MUST treat any response, including errors, as fire-and-forget.

## 4. Emitter conduct (fail-silent contract)

An emitter:

- MUST NOT crash, block, or visibly degrade its host agent under any failure (collector down, network error, malformed config).
- SHOULD use a total timeout of 2 seconds or less per event.
- MUST NOT retry an event more than once.
- SHOULD truncate large strings before sending (see `payload`, §5); a full event SHOULD stay under 16 KiB.

*A conforming emitter is one that a user can install and forget: if the collector never runs, nothing about the agent changes.*

## 5. Event envelope

```json
{
  "spec_version": "0.1",
  "source": "claude_code",
  "instance_id": "9f2c41ab77d1",
  "session_id": "b3c4d5e6-...",
  "hostname": "dev-laptop",
  "cwd": "C:/work/my-service",
  "event_type": "needs_input",
  "summary": "Waiting for permission: Bash(rm -rf build)",
  "payload": { "tool_name": "Bash" },
  "hook_event_name": "Notification",
  "timestamp": "2026-07-16T15:36:34.120Z"
}
```

| Field | Type | Req | Notes |
|---|---|---|---|
| `spec_version` | string | SHOULD | Protocol version, `"0.1"`. Collectors MUST accept events without it and assume `"0.1"`. |
| `source` | string | MUST | Tool identifier, lowercase snake_case. Well-known values: `claude_code`, `opencode`, `kilo`, `codex`, `cursor`. Unknown values MUST be accepted. |
| `instance_id` | string | MUST | Stable, unique per (host, session). RECOMMENDED: a hash of `hostname:session_id`. Opaque to collectors. |
| `session_id` | string | SHOULD | The tool's native session identifier, for display and correlation. |
| `hostname` | string | MUST | Machine name the agent runs on. |
| `cwd` | string | SHOULD | Working directory / project root — collectors use it as the human-facing session name. |
| `event_type` | string | MUST | One of the vocabulary in §6. Collectors SHOULD accept unknown types and treat them as `activity`. |
| `summary` | string | MUST | One human-readable line (≤ 120 chars recommended). This is what appears in feeds and notifications. |
| `payload` | object | MAY | Tool-specific extras. Collectors MUST store/forward it opaquely and MUST NOT require any key inside it. |
| `hook_event_name` | string | MAY | The native event name at the source (e.g. `PreToolUse`, `session.idle`), for debugging and mapping audits. |
| `timestamp` | string | MAY | ISO 8601 UTC. If absent, the collector assigns receipt time. |

Unknown top-level fields MUST be ignored by collectors (forward compatibility).

## 6. Event vocabulary and session state machine

| event_type | Meaning | Session status after |
|---|---|---|
| `session_start` | Session began | `idle` |
| `user_prompt` | User submitted input; agent is now working | `working` |
| `activity` | Progress: a tool call, a step | `working` |
| `subagent_completed` | A subagent finished; parent still going | `working` |
| `heartbeat` | Session is alive (no new progress to report) | `working` |
| `needs_input` | **Agent is blocked on the human** (permission, question) | `needs_input` |
| `error` | Failure worth human attention | `error` |
| `completed` | Agent finished responding; awaiting next prompt | `idle` |
| `session_end` | Session closed | `ended` |

Rules:

- Statuses are: `working`, `needs_input`, `error`, `idle`, `stale`, `ended`.
- A collector derives status purely from the latest event per §6's table; events not in the table leave status unchanged.
- `needs_input` and `error` are **attention states**: collectors surfacing UI SHOULD rank them above all others and MAY notify the user.
- Emitters SHOULD NOT send an event for every successful tool completion (send `activity` on start instead); this keeps feeds readable.
- **Staleness (reaper):** agents crash and terminals close without a `session_end`. Collectors SHOULD transition a `working` session to `stale` after a configurable silence window (RECOMMENDED default: 10 minutes). Any subsequent event revives the session per the table above. Long-running emitters that go quiet legitimately (a wrapper around a silent process) SHOULD send `heartbeat` every ~60s; collectors SHOULD update `last_seen` on heartbeats but SHOULD NOT include them in activity feeds.
- **Retention:** collectors SHOULD prune events beyond a configurable window (RECOMMENDED default: 14 days), keeping session rows.

## 7. Collector conformance

A minimal conforming collector implements exactly one endpoint:

- `POST /api/events` — accept the envelope (§5), respond `2xx`.

Everything else is OPTIONAL but RECOMMENDED for interoperable dashboards:

- `GET /api/events?instance_id=&event_type=&source=&since=&limit=` — history.
- `GET /api/sessions?include_ended=` — derived session list with `status`, `last_summary`, `last_seen`.
- `GET /api/sessions/{instance_id}/events` — one session's timeline.
- `WS /ws` — pushes `{"type": "event"|"session", "data": {...}}` frames to subscribers in real time.

A conformance harness ships with the reference implementation: `agent-pulse conformance --url <collector>` drives the full event vocabulary against any collector and checks the state machine and response shapes, reporting CONFORMANT / NOT CONFORMANT.

## 8. Interoperability mappings

Pulse Protocol is deliberately a small dialect of ideas that larger standards are converging on. Collectors and bridges SHOULD use these mappings.

### 8.1 A2A task states

| Pulse status | A2A TaskState |
|---|---|
| `working` | `working` |
| `needs_input` | `input-required` |
| `error` | `failed` |
| `idle` | `completed` |
| `ended` | `canceled` / terminal |

### 8.2 OpenTelemetry GenAI semantic conventions

A Pulse session corresponds to an agent invocation span tree; a bridge exporting Pulse events to OTLP SHOULD emit:

- `session_start`/`session_end` → start/end of an `invoke_agent` span (`gen_ai.operation.name = "invoke_agent"`).
- `activity` → `execute_tool` span events (`gen_ai.tool.name` from `payload.tool_name` when present).
- `error` → span status `ERROR`.
- `needs_input` has no stable OTel equivalent today — bridges SHOULD emit a span event named `pulse.needs_input`. (This gap is the reason Pulse exists.)

### 8.3 AG-UI

`session_start`→`RUN_STARTED`, `activity`→`TOOL_CALL_START`, `error`→`RUN_ERROR`, `completed`→`RUN_FINISHED`. AG-UI has no `needs_input` lifecycle event; bridges SHOULD use a custom event.

### 8.4 Native tool surfaces

Reference emitters maintained alongside this spec:

| Tool | Surface | Adapter |
|---|---|---|
| Claude Code | hooks (`settings.json`) | `agent_pulse/hook_client.py` |
| OpenCode | JS plugin | `agent_pulse/adapters/opencode_plugin.js` |
| Kilo | JS plugin (OpenCode-compatible) | `agent_pulse/adapters/kilo_plugin.js` |
| Codex CLI | `notify` program | `agent_pulse/adapters/codex_notify.py` |
| Cursor | `hooks.json` | `agent_pulse/adapters/cursor_hook.py` |
| anything | process wrapper | `agent_pulse/adapters/pulse_run.py` |

## 9. Security considerations

- Collectors SHOULD bind to loopback (`127.0.0.1`) by default. The protocol carries prompts, file paths, and command lines — treat it as sensitive.
- For non-loopback deployment, collectors SHOULD require `Authorization: Bearer <token>` and TLS; emitters MUST send the token from `PULSE_COLLECTOR_TOKEN` when set.
- Emitters SHOULD slim payloads at the source (truncate commands/prompts) rather than rely on the collector to redact.

## 10. Extension: remote approval (experimental)

The base protocol is one-way. This OPTIONAL extension adds the reverse
direction for exactly one purpose: answering an agent's permission prompt
from the collector's UI ("away mode"). It uses plain request/response —
no new transport.

**Resources** (on the same base URL as `/api/events`):

- `POST /api/decisions` `{instance_id, summary, payload}` → creates a
  pending decision `{id, status: "pending", expires_at, ...}`. The
  collector SHOULD also emit a `needs_input` event for the session so
  status and notifications flow through the normal path.
- `GET /api/decisions/{id}` → `{status: "pending"|"allow"|"deny"|"expired", reason}`.
- `POST /api/decisions/{id}/respond` `{status: "allow"|"deny", reason?}` —
  called by the user-facing surface. Only pending decisions may be answered.
- `GET /api/sessions/{instance_id}/mode` → `{remote_approval: bool}` and
  `POST` of the same shape to toggle. Emitters MUST treat an unreachable
  collector or missing session as `false`.

**Emitter flow** (only when the session's `remote_approval` mode is on):
when the agent is about to prompt for permission, the emitter creates a
decision, then polls it until answered or a local deadline (RECOMMENDED
≤ 45s, always below the host tool's own hook timeout). On `allow`/`deny`
it returns that verdict through the tool's native decision mechanism
(e.g. Claude Code `PreToolUse` `permissionDecision`, OpenCode
`permission.ask` output). On timeout, expiry, or any error the emitter
MUST fall back to the tool's normal interactive prompt — remote approval
may never make an agent *less* safe or *more* stuck than it was.

Decisions are short-lived: collectors SHOULD expire pending decisions
after ~90 seconds and clear them from UIs.

**Follow-up messages.** The extension also defines a queued-message
resource so the user can steer an agent remotely:

- `POST /api/messages` `{instance_id, text}` — queue a follow-up.
- `GET /api/messages/next?instance_id=` — pops the oldest queued message
  (collector SHOULD emit a `user_prompt` event on delivery).

Emitters deliver at a natural turn boundary through the tool's native
continuation mechanism (Claude Code: a `Stop` hook answering
`{"decision": "block", "reason": <text>}`, which continues the turn with
the text as instruction; emitters MUST NOT deliver when the tool signals
stop-loop protection, e.g. `stop_hook_active`). Undelivered messages
simply wait; an unreachable collector means normal stop behavior.

## 11. Versioning

- The spec uses semver. `0.x` may break between minors; from `1.0`, breaking envelope changes require a major version and collectors MUST accept the previous major during a deprecation window.
