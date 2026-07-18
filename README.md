<div align="center">

# ⚡ Agent Pulse

### One inbox for every AI coding agent you're running.

Claude Code, OpenCode, Kilo, Codex, Cursor — all in one live dashboard that tells you **which agent needs you right now**, so you stop tab-hopping across ten terminals to babysit them.

[![CI](https://github.com/Jainil-Gosalia/pulse-protocol/actions/workflows/ci.yml/badge.svg)](https://github.com/Jainil-Gosalia/pulse-protocol/actions/workflows/ci.yml)
[![License](https://img.shields.io/badge/license-Apache%202.0-blue.svg)](LICENSE)
[![Spec](https://img.shields.io/badge/Pulse%20Protocol-v0.1-d97757.svg)](spec/pulse-protocol.md)
[![Python](https://img.shields.io/badge/python-3.9%2B-3776ab.svg)](pyproject.toml)
[![Reference implementation](https://img.shields.io/badge/status-reference%20implementation-22c55e.svg)](#its-a-protocol-not-just-an-app)

<img src="https://raw.githubusercontent.com/Jainil-Gosalia/pulse-protocol/main/docs/dashboard.png" alt="Agent Pulse dashboard" width="820">

</div>

---

## The problem

You started running agents in parallel because it's faster. Now you have six terminals open and you're **constantly checking each one** — did that one finish? is this one stuck waiting for permission? did that one error out twenty minutes ago while you were reading a different tab?

The bottleneck stopped being the agents. It's **you**, context-switching between windows to find the one that needs a decision.

## The fix

Agent Pulse gives every agent one place to report in. Sessions are grouped by **what needs your attention first**:

- 🟠 **Needs attention** — waiting for permission, or errored (this is what you actually care about)
- 🔵 **Working** — busy, leave it alone
- 🟢 **Idle** — done, waiting for your next prompt
- ⚪ **Stale** — went silent (crashed / terminal closed)

And it answers the one question that matters — *which of my agents needs me?* — at a glance, from your desk **or your phone**.

<div align="center">
<img src="https://raw.githubusercontent.com/Jainil-Gosalia/pulse-protocol/main/docs/mobile.png" alt="Agent Pulse on mobile" width="300">
</div>

---

## ✨ What makes it worth installing

| | |
|---|---|
| 📟 **Live multi-agent feed** | Every tool call, prompt, error, and completion streams in over WebSocket. No polling, no refresh. |
| 📱 **Control it from your phone** | The dashboard is a mobile PWA. Get push notifications (via [ntfy](https://ntfy.sh)) the instant an agent needs you — on any network. |
| 🌙 **Approve tool calls remotely ("away mode")** | Toggle a session to away mode and its permission prompts pop up on your dashboard with **Allow / Deny** buttons — or right inside the phone notification. Approve a `rm -rf` from the coffee line. |
| 💬 **Steer agents remotely** | Queue a follow-up ("also update the README") from the dashboard; when the agent finishes its turn it keeps going with your message instead of stopping. |
| ⏱️ **Attention analytics** | See how long your agents spend *blocked waiting on you* — the real throughput metric nobody else measures. |
| 🪶 **Zero-risk adapters** | Every adapter fails silent with a short timeout. If the collector is down, your agents don't even notice. |

---

## 🚀 Quick start

```bash
# install (PyPI coming soon — for now, straight from GitHub)
pipx install git+https://github.com/Jainil-Gosalia/pulse-protocol.git

agent-pulse serve                 # collector + dashboard → http://localhost:8765
agent-pulse install claude_code   # wire up each tool (shows the change, asks first, backs up)
agent-pulse install opencode
agent-pulse install kilo
```

Open **http://localhost:8765**, restart your agent sessions (hooks load at startup), and watch them show up. That's it.

> Prefer to inspect before you install? `agent-pulse install` prints the exact config change and asks before touching anything. Every adapter is a tiny, dependency-free script.

### Reach it from your phone

```bash
agent-pulse serve --host 0.0.0.0 --token "$(openssl rand -hex 16)"
agent-pulse setup-phone           # opens the firewall via one click, prints your phone URL
```

- **Same Wi-Fi** → open the printed `http://<pc-ip>:8765/?token=…` on your phone, *Add to Home Screen*.
- **Any network** → put both devices on [Tailscale](https://tailscale.com) and use the Tailscale IP, or run `agent-pulse serve --tunnel` for an instant public HTTPS URL (no VPN, no firewall).
- **Push notifications** → install the ntfy app, `export PULSE_NTFY_TOPIC=<something-unguessable>`, restart. Now your phone buzzes when an agent is blocked.

---

## 🔌 Supported tools

| Tool | How it hooks in | Activity feed | Away-mode approval | Follow-ups | Status |
|------|-----------------|:---:|:---:|:---:|--------|
| **Claude Code** | native hooks | ✅ per tool call | ✅ | ✅ | tested |
| **OpenCode** | JS plugin | ✅ per tool call | ✅ | ✅ | tested |
| **Kilo** | JS plugin | ✅ per tool call | ✅ | ✅ | tested |
| **Codex CLI** | `notify` program | ◐ turn-level | — | — | untested* |
| **Cursor** | `hooks.json` | ✅ | — | — | untested* |
| **anything else** | `pulse_run` wrapper | ◐ start/end | — | — | tested |

<sub>*Written from current docs; not yet verified on a live install. `agent-pulse install <tool>` sets each one up.</sub>

Running something not on this list? Wrap it:

```bash
python -m agent_pulse.adapters.pulse_run --source aider -- aider --model gpt-4o
```

---

## 🧩 How it works

```
Claude Code ──(hooks)──────┐
OpenCode ────(plugin)──────┤
Kilo ────────(plugin)──────┼──▶  Collector (FastAPI @ :8765)  ──▶  SQLite
Codex ───────(notify)──────┤            │
Cursor ──────(hooks.json)──┤            └── WebSocket ──▶  Dashboard (browser / phone)
anything ────(pulse_run)───┘
```

Each adapter maps its tool's native events onto one small, normalized envelope and `POST`s it to the collector. The collector derives session status, stores history, and streams everything to any connected dashboard in real time. One POST in, a live inbox out.

---

## 📐 It's a protocol, not just an app

Agent Pulse is the **reference implementation of the [Pulse Protocol](spec/pulse-protocol.md)** — an open spec (Apache-2.0) for session and attention events from coding agents.

The point: every coding tool exposes a *different* proprietary eventing surface, so everyone re-integrates every tool from scratch — the same N×M mess [MCP](https://modelcontextprotocol.io) solved for tool access. Pulse standardizes the other direction: **one JSON envelope any agent can emit, one endpoint any dashboard can implement.**

- 🎯 Answers one question well: *which of my agents needs me right now?*
- 🪶 Emittable from anything — one HTTP POST, no SDK, no dependencies
- 🤝 First-class `needs_input` signal — human attention is the point, not an afterthought
- 🔗 Maps cleanly to [OpenTelemetry GenAI](https://opentelemetry.io/blog/2026/genai-observability/), [A2A](https://a2a-protocol.org) task states, and [AG-UI](https://ag-ui.com) (see spec §8)

Building your own collector or emitter? Prove it conforms:

```bash
agent-pulse conformance --url https://your-collector/api/events
# drives the full event vocabulary + state machine → CONFORMANT / NOT CONFORMANT
```

Full spec: **[spec/pulse-protocol.md](spec/pulse-protocol.md)** · JSON Schema: **[spec/pulse-event.schema.json](spec/pulse-event.schema.json)**

---

## ❓ FAQ

**Does it slow my agents down?** No. Adapters fail silent with sub-second timeouts and (on remote collectors) can fire fully off the critical path. A dead collector costs your agents nothing.

**Where does my data go?** Nowhere. Everything runs locally — a collector on `localhost` and a SQLite file. Phone access is your own LAN, your own Tailnet, or a tunnel you control.

**Do I have to use the dashboard?** No — the collector is a plain HTTP+WebSocket service. Point your own UI, script, or Slack bot at it.

**Multiple machines?** Sessions are keyed by `hash(hostname + session_id)`, so several machines can report to one collector. Set `PULSE_COLLECTOR_URL` on the emitters.

---

## 🛠️ Configuration

Everything is environment variables — set what you need, ignore the rest.

| Variable | Default | What it does |
|----------|---------|--------------|
| `PULSE_COLLECTOR_URL` | `http://127.0.0.1:8765/api/events` | Where emitters send events |
| `PULSE_COLLECTOR_TOKEN` | — | Require a bearer token (needed for non-loopback access) |
| `PULSE_NTFY_TOPIC` | — | ntfy topic for phone push (also Telegram / webhook — see below) |
| `PULSE_STALE_MINUTES` | `10` | Mark a silent working session "stale" after this long |
| `PULSE_RETENTION_DAYS` | `14` | Prune events older than this |
| `PULSE_GATED_TOOLS` | `Bash,PowerShell,Write,Edit,NotebookEdit` | Which Claude Code tools away-mode holds for approval |
| `PULSE_DETACH` | off | Fire reporting POSTs off the critical path (for remote collectors) |

Phone push also supports `PULSE_TELEGRAM_BOT_TOKEN` + `PULSE_TELEGRAM_CHAT_ID`, or a generic `PULSE_WEBHOOK_URL`.

---

## 🤝 Contributing

The protocol is young (v0.1) and the emitter set is growing. Especially wanted:

- **New adapters** — Aider, Gemini CLI, Continue, Cline, Zed's agent, anything with an eventing surface
- **Live testing** of the Codex and Cursor adapters
- **Alternative collectors / dashboards** — the spec is the contract; build your own and run `agent-pulse conformance` against it

Open an issue or PR. Adapters are small — the Claude Code one is ~200 lines of stdlib Python.

---

## 📄 License

[Apache-2.0](LICENSE) — including a patent grant, chosen so the protocol is safe for organizations to adopt.
