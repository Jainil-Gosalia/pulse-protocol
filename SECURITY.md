# Security

Agent Pulse handles session activity (prompts, commands, file paths) and — in away mode — can **approve or deny tool calls remotely**. Treat it accordingly.

## Reporting a vulnerability

Please report privately via [GitHub Security Advisories](https://github.com/Jainil-Gosalia/pulse-protocol/security/advisories/new) rather than a public issue. Expect an initial response within a few days.

## Threat model & guarantees

- **Loopback by default.** The collector binds `127.0.0.1`. Local emitters are trusted (a local process could read the SQLite file anyway).
- **Token required off-loopback.** Any request arriving over the network — including through a proxy/tunnel (detected via `CF-Connecting-IP` / `X-Forwarded-For`) — must present `PULSE_COLLECTOR_TOKEN`. The loopback exemption never applies to forwarded traffic.
- **The event stream is sensitive.** It carries prompts, command lines, and paths. Don't expose the collector to an untrusted network without a token, and prefer a private path (Tailscale) over a public tunnel.
- **Away mode is a remote control surface.** A holder of the token can approve tool calls (e.g. a shell command) for any session in away mode. Scope the gated tools (`PULSE_GATED_TOOLS`), keep the token secret, and turn away mode off when you don't need it. On timeout or any failure the agent falls back to its normal local prompt — remote approval can never make an agent *less* safe.

## Operator checklist

- Use a long random `PULSE_COLLECTOR_TOKEN` for any non-loopback serving.
- Rotate the token if it may have been exposed (logs, screenshots, shared terminals).
- Public tunnel URLs (`--tunnel`) are guarded only by the token — rotate the URL by restarting, and don't post it anywhere.
