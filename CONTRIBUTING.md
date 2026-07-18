# Contributing to Pulse Protocol / Agent Pulse

Thanks for helping. The most valuable contributions right now are **new adapters** and **live-testing existing ones**.

## Ground rules for adapters

Every adapter — hook script, plugin, or wrapper — must honor the emitter contract ([spec §4](spec/pulse-protocol.md)):

- **Never crash, block, or degrade the host agent.** Swallow all errors.
- **Short timeouts** (≤2s), no more than one retry.
- **Fail silent** if the collector is down — the user's agent must not notice.

If your adapter can't guarantee that, it's not ready to merge. This is the whole promise of the project.

## Adding an adapter for a new tool

1. Find the tool's eventing surface (hooks, plugins, a `notify` program, or — worst case — wrap it with `pulse_run`).
2. Map its native events onto the Pulse vocabulary ([spec §6](spec/pulse-protocol.md)): `session_start`, `user_prompt`, `activity`, `needs_input`, `error`, `completed`, `session_end`.
3. Emit the envelope ([spec §5](spec/pulse-protocol.md)) via one `POST` to `PULSE_COLLECTOR_URL`. Read the token from `PULSE_COLLECTOR_TOKEN` if set. Set a distinct `source`.
4. Copy an existing adapter as a starting point — `agent_pulse/hook_client.py` (Python, ~200 lines of stdlib) or `agent_pulse/adapters/opencode_plugin.js` (JS).
5. Add an installer path in `agent_pulse/cli.py` if the tool has a config file to merge.
6. Note it in the README's supported-tools table, honestly marked `tested` or `untested`.

## Running the checks

```bash
pip install -e .
python quick_verify.py                              # end-to-end suite (isolated port + db)
python -m agent_pulse.cli conformance --url http://127.0.0.1:8765/api/events
```

CI runs both on every PR across Python 3.9–3.13. Green before review, please.

## Changing the protocol

The spec is versioned. Envelope or vocabulary changes need a spec edit ([spec/pulse-protocol.md](spec/pulse-protocol.md)) **and** a conformance-harness update (`agent_pulse/conformance.py`) in the same PR. Keep the interop mappings (§8) honest — if a change breaks the A2A / OTel / AG-UI correspondence, say so.

## Building a different collector or dashboard

The spec is the contract, not this codebase. Build your own and prove it:

```bash
agent-pulse conformance --url https://your-collector/api/events
```

A passing run is the bar for calling something Pulse-conformant.
