# Recording a Claude Code session

`agentbisect.hooks.claude_code` turns a live Claude Code session into the same trace format the
Alan harness writes, so `agentbisect run` and `report` work on it unchanged.

## What it records

Each tool call becomes one step in `runs/cc-<session_id>/trace.jsonl`, with `source = claude_code`.

| Claude Code tool | Step kind | Snapshot? |
|---|---|---|
| `Write` | `write` | yes |
| `Edit`, `MultiEdit`, `NotebookEdit` | `edit` | yes |
| `Bash` | `shell` | yes |
| `Read`, `Grep`, `Glob`, `LS` | `read` | no |
| `Task`, `Agent`, `mcp__*` | `other` | yes (they can change files) |
| anything else | `other` | no |

Snapshots go to `runs/cc-<id>/shadow.git`. Your project directory and its `.git` are only read.
Your `.gitignore` files are honored, so ignored directories are not snapshotted.

From the session transcript (best effort) the hook also fills in the agent's text before each call
(`reasoning`), the tokens of the model turn that made the call, and the model name. If the
transcript cannot be read, those stay empty and the trace still works.

## Install

Merge the block below into `~/.claude/settings.json` (all projects) or into
`<project>/.claude/settings.local.json` (one project). Print it, with this checkout's Python
filled in, with:

```bash
uv run python -m agentbisect.hooks.claude_code --print-settings
```

Add to the arrays you already have. Do not replace them. This machine already has `Stop` and
`SessionEnd` hooks for Alan session ingestion; Claude Code runs every matching hook, so a second
entry in the `SessionEnd` array sits beside the Alan one:

```json
{
  "hooks": {
    "SessionStart": [
      { "hooks": [ { "type": "command", "timeout": 60,
          "command": "\"/Users/akshat/Desktop/Alan AI/.venv/bin/python\" -m agentbisect.hooks.claude_code" } ] }
    ],
    "PostToolUse": [
      { "matcher": "*", "hooks": [ { "type": "command", "timeout": 60,
          "command": "\"/Users/akshat/Desktop/Alan AI/.venv/bin/python\" -m agentbisect.hooks.claude_code" } ] }
    ],
    "PostToolUseFailure": [
      { "matcher": "*", "hooks": [ { "type": "command", "timeout": 60,
          "command": "\"/Users/akshat/Desktop/Alan AI/.venv/bin/python\" -m agentbisect.hooks.claude_code" } ] }
    ],
    "SessionEnd": [
      { "hooks": [ { "type": "command", "timeout": 60,
          "command": "\"/Users/akshat/Desktop/Alan AI/.venv/bin/python\" -m agentbisect.hooks.claude_code" } ] }
    ]
  }
}
```

Each event matters:

* **`SessionStart`** takes the base snapshot (step 0) before the agent acts. Without it, the base
  is taken after the first tool call, so step 1 can hide inside the base. The hook logs this.
* **`PostToolUse`** fires when a tool succeeds.
* **`PostToolUseFailure`** fires when a tool fails. A shell command that exits non-zero, such as
  a failing `pytest`, arrives here with `error` starting `Exit code N`. Skip this event and the
  recorder never sees the failure output, so every run looks like the agent never noticed.
* **`SessionEnd`** is optional. It sets `finished = true`.

Where runs go: `$AGENTBISECT_RUNS_DIR`, else `runs/` in the AgentBisect checkout.

## Use

```bash
# work in Claude Code as usual, then:
uv run agentbisect run    --trace runs/cc-<session_id> --check "pytest tests/test_x.py -q"
uv run agentbisect report --trace runs/cc-<session_id>
```

## Safety

* The hook prints nothing and always exits 0. On any error it writes a traceback to
  `runs/cc-<id>/hook.log` (or `runs/hook-errors.log` when the input has no session id) and lets
  the session go on.
* Parallel tool calls run their hooks at once; a file lock per session keeps the trace in order.
  A repeated event for the same `tool_use_id` is ignored.
* Each hook call runs `git add -A` on your project against the shadow repo, which is quick for
  normal repos and slow for huge ones. The `timeout` of 60 seconds caps it.
* Known limits: state outside the project directory is not captured, and a long session adds one
  hook process per tool call.
