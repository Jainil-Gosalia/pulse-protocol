// Agent Pulse adapter for OpenCode.
//
// Install (either):
//   global:  ~/.config/opencode/plugin/agent-pulse.js
//   project: <project>/.opencode/plugin/agent-pulse.js
// (Newer OpenCode versions also accept the "plugins/" directory name.)
//
// Fire-and-forget: never awaits network I/O on the hot path and swallows
// all errors, so a missing collector never slows the agent down.

import os from "os"

const COLLECTOR = process.env.PULSE_COLLECTOR_URL || "http://127.0.0.1:8765/api/events"
const TOKEN = process.env.PULSE_COLLECTOR_TOKEN
const SPEC_VERSION = "0.1"

// Kilo is an OpenCode fork and loads plugins from both .kilo/plugin/ and
// .opencode/plugin/, so this file may run inside either tool (and both
// Agent Pulse plugin files may load in the same process). Detect the real
// host from the process path instead of hardcoding it.
function detectSource(fallback) {
  try {
    const hay = [process.execPath, ...(process.argv || []).slice(0, 2)].join(" ").toLowerCase()
    if (hay.includes("kilo")) return "kilo"
    if (hay.includes("opencode")) return "opencode"
  } catch {}
  return fallback
}
const SOURCE = detectSource("opencode")

function instanceId(sessionId) {
  const s = `${os.hostname()}:${sessionId}`
  let h = 5381
  for (let i = 0; i < s.length; i++) h = ((h * 33) ^ s.charCodeAt(i)) >>> 0
  return `${SOURCE}-${h.toString(16)}`
}

const API_BASE = COLLECTOR.replace(/\/api\/events\/?$/, "")

function headers() {
  const h = { "Content-Type": "application/json" }
  if (TOKEN) h["Authorization"] = `Bearer ${TOKEN}`
  return h
}

function send(evt) {
  try {
    const ctl = new AbortController()
    const timer = setTimeout(() => ctl.abort(), 1500)
    fetch(COLLECTOR, {
      method: "POST",
      headers: headers(),
      body: JSON.stringify({ spec_version: SPEC_VERSION, ...evt }),
      signal: ctl.signal,
    }).catch(() => {}).finally(() => clearTimeout(timer))
  } catch {}
}

async function api(path, body, timeoutMs = 1500) {
  try {
    const ctl = new AbortController()
    const timer = setTimeout(() => ctl.abort(), timeoutMs)
    const res = await fetch(API_BASE + path, {
      method: body ? "POST" : "GET",
      headers: headers(),
      body: body ? JSON.stringify(body) : undefined,
      signal: ctl.signal,
    })
    clearTimeout(timer)
    return res.ok ? await res.json() : null
  } catch { return null }
}

export const AgentPulse = async ({ directory, client }) => {
  // If another Agent Pulse plugin file already registered in this process
  // (kilo loading both .kilo and .opencode copies), stay silent to avoid
  // reporting every event twice.
  if (globalThis.__agentPulseActive) return {}
  globalThis.__agentPulseActive = true

  const hostname = os.hostname()

  // Remote follow-up: when the session goes idle, deliver any message the
  // user queued from the dashboard (💬) as a new prompt — the opencode
  // equivalent of Claude Code's Stop-hook continuation. Fail-safe: if the
  // SDK shape differs or no message is queued, nothing happens.
  const drainFollowup = async (sid) => {
    try {
      if (!client?.session?.prompt) return
      const msg = await api(`/api/messages/next?instance_id=${instanceId(sid)}`)
      if (!msg?.text) return
      await client.session.prompt({
        path: { id: sid },
        body: { parts: [{ type: "text", text: msg.text }] },
      })
    } catch {}
  }

  const report = (sessionId, eventType, summary, hookName, payload = {}) => {
    const sid = sessionId || "unknown"
    send({
      instance_id: instanceId(sid),
      hostname,
      source: SOURCE,
      event_type: eventType,
      summary,
      payload,
      session_id: sid,
      cwd: directory,
      hook_event_name: hookName,
    })
  }

  const sessionIdOf = (props) =>
    props?.sessionID || props?.info?.id || props?.info?.sessionID || null

  return {
    "tool.execute.before": async (input) => {
      report(input?.sessionID, "activity", `Using tool: ${input?.tool || "unknown"}`,
        "tool.execute.before", { tool: input?.tool })
    },

    // Away mode: when a permission prompt appears and remote approval is on
    // for this session, hold it for a dashboard decision. On timeout or any
    // failure, leave output untouched -> the normal terminal prompt applies.
    "permission.ask": async (input, output) => {
      try {
        const sid = input?.sessionID || input?.id || "unknown"
        const iid = instanceId(sid)
        const mode = await api(`/api/sessions/${iid}/mode`, null, 500)
        if (!mode?.remote_approval) return
        const decision = await api("/api/decisions", {
          instance_id: iid,
          summary: `Permission: ${input?.title || input?.type || "unknown"}`,
          payload: { type: input?.type },
        })
        if (!decision?.id) return
        const deadline = Date.now() + 45000
        while (Date.now() < deadline) {
          await new Promise((r) => setTimeout(r, 1000))
          const cur = await api(`/api/decisions/${decision.id}`)
          if (!cur || cur.status === "pending") continue
          if (cur.status === "allow" || cur.status === "deny") output.status = cur.status
          return
        }
      } catch {}
    },

    event: async ({ event }) => {
      const props = event?.properties || {}
      const sid = sessionIdOf(props)
      switch (event?.type) {
        case "session.created":
          return report(sid, "session_start", "Session started", event.type)
        case "session.idle":
          report(sid, "completed", "Finished responding", event.type)
          return drainFollowup(sid)
        case "session.error":
          return report(sid, "error", "Session error", event.type,
            { error: String(props?.error?.name || props?.error || "unknown").slice(0, 300) })
        case "permission.asked":
        case "permission.updated":
          return report(sid, "needs_input",
            `Waiting for permission: ${props?.title || props?.type || ""}`.trim(), event.type)
        case "session.deleted":
          return report(sid, "session_end", "Session ended", event.type)
      }
    },
  }
}
