// AgentBisect recorder plugin for OpenCode (Alan Code).
//
// Rendered by `agentbisect.hooks.opencode.prepare_opencode_run` into
// <workdir>/.opencode/plugin/agentbisect.js, with the three quoted placeholders below replaced by
// absolute paths. OpenCode loads every file in .opencode/plugin/ when it starts in this folder.
//
// What it does
//   tool.execute.after  -> `python -m agentbisect.hooks.opencode step`  (one Step per tool call)
//   event               -> appends bus events to <run>/events.jsonl
//   session.idle        -> `python -m agentbisect.hooks.opencode finalize`
//
// It must never break the agent: every hook catches all errors and logs them to <run>/plugin.log.
// Edit nothing here by hand; re-run `agentbisect record` to regenerate it.

import { appendFileSync } from "node:fs"
import { spawnSync } from "node:child_process"
import { join } from "node:path"

const PYTHON = "__AGENTBISECT_PYTHON__"
const RUN_DIR = "__AGENTBISECT_RUN_DIR__"
const REPO_ROOT = "__AGENTBISECT_REPO_ROOT__"

const STEP_TIMEOUT_MS = 120000
const FINALIZE_TIMEOUT_MS = 120000
// Event types worth keeping. Everything else on the bus (file watcher, lsp, tui) is noise.
const KEEP = new Set([
  "message.updated",
  "message.part.updated",
  "session.created",
  "session.status",
  "session.idle",
  "session.error",
])
// Streaming text parts fire many updates. Keep the latest per part and write it when it has been
// quiet for FLUSH_AFTER_MS, when another kind of part of the same message arrives (so parts stay in
// order), or when the session goes idle.
const FLUSH_AFTER_MS = 2000

function log(message) {
  try {
    appendFileSync(join(RUN_DIR, "plugin.log"), `${new Date().toISOString()} ${message}\n`)
  } catch (_) {
    // nothing else we can do
  }
}

function runPython(command, input, timeout) {
  try {
    const result = spawnSync(
      PYTHON,
      ["-m", "agentbisect.hooks.opencode", command, "--run-dir", RUN_DIR],
      {
        input,
        timeout,
        encoding: "utf8",
        env: { ...process.env, PYTHONPATH: REPO_ROOT + (process.env.PYTHONPATH ? ":" + process.env.PYTHONPATH : "") },
      },
    )
    if (result.error) log(`${command}: ${result.error.message}`)
    else if (result.status !== 0) log(`${command}: exit ${result.status}: ${result.stderr}`)
  } catch (e) {
    log(`${command}: ${e && e.message ? e.message : e}`)
  }
}

function writeEvent(type, properties) {
  appendFileSync(join(RUN_DIR, "events.jsonl"), JSON.stringify({ ts: Date.now(), type, properties }) + "\n")
}

export const AgentBisect = async () => {
  const pending = new Map() // part id -> { at, event }

  function flush(all, messageID) {
    const now = Date.now()
    for (const [id, item] of pending) {
      const mine = messageID && item.properties.part && item.properties.part.messageID === messageID
      if (all || mine || now - item.at > FLUSH_AFTER_MS) {
        writeEvent("message.part.updated", item.properties)
        pending.delete(id)
      }
    }
  }

  return {
    "tool.execute.after": async (input, output) => {
      try {
        const payload = {
          tool: input.tool,
          sessionID: input.sessionID,
          callID: input.callID,
          args: input.args,
          title: output && output.title,
          output: output && output.output,
          metadata: output && output.metadata,
        }
        runPython("step", JSON.stringify(payload), STEP_TIMEOUT_MS)
      } catch (e) {
        log(`tool.execute.after: ${e && e.message ? e.message : e}`)
      }
    },

    event: async ({ event }) => {
      try {
        if (!event || !KEEP.has(event.type)) return
        const properties = event.properties || {}
        if (event.type === "message.part.updated") {
          const part = properties.part || {}
          if (part.type === "text" || part.type === "reasoning") {
            pending.set(part.id, { at: Date.now(), properties })
            flush(false)
            return
          }
          // Keep the order of parts: write this message's waiting text first.
          flush(false, part.messageID)
          writeEvent(event.type, properties)
          return
        }
        if (event.type === "session.idle") {
          flush(true)
          writeEvent(event.type, properties)
          runPython("finalize", "", FINALIZE_TIMEOUT_MS)
          return
        }
        writeEvent(event.type, properties)
      } catch (e) {
        log(`event: ${e && e.message ? e.message : e}`)
      }
    },
  }
}
