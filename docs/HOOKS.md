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

---

# Recording Alan Code (OpenCode in the Alan app)

The agent under test is Alan Code: the Alan app runs OpenCode (`opencode serve`) in the folder of
a conversation, with models proxied through Alan. We have no API key, so we watch OpenCode work
instead of calling the model. A plugin inside OpenCode reports every tool call to
`agentbisect.hooks.opencode`.

## Steps for one run

1. Prepare the run (this copies the task repo, takes the base snapshot, installs the plugin):

   ```bash
   uv run agentbisect record --task tasks/t01.yaml --model flash
   ```

   It prints the run folder and the workdir, `runs/<id>/workdir`, and saves the prompt in
   `runs/<id>/PROMPT.txt`.
2. In the Alan app, start a new conversation in the **workdir** folder (`runs/<id>/workdir`),
   not in `runs/<id>`. OpenCode loads `.opencode/plugin/agentbisect.js` from that folder when it
   starts.
3. Pick the model, paste the contents of `PROMPT.txt`, and let the agent work until it stops.
   Do not edit files yourself.
4. Wait until the agent is idle. The plugin then runs `finalize` by itself.
5. Check the run (`finalize.json` says which mode was used):

   ```bash
   cat runs/<id>/finalize.json
   uv run agentbisect run --trace runs/<id>
   uv run agentbisect report --trace runs/<id>
   ```

   If `finalize.json` is missing, run it by hand:
   `uv run python -m agentbisect.hooks.opencode finalize --run-dir runs/<id>`.

One conversation is one run. For a second run, prepare a new run folder.

## What gets recorded

| File in `runs/<id>/` | Written by | Content |
|---|---|---|
| `trace.jsonl`, `meta.json` | `step`, `finalize` | the trace (`source = alan_code`) |
| `events.jsonl` | plugin `event` hook | OpenCode bus events: `message.updated`, `message.part.updated`, `session.*` |
| `plugin.log` | plugin | plugin errors (it never throws into OpenCode) |
| `hook.log` | python side | tracebacks from `step` and `finalize` |
| `finalize.json` | `finalize` | mode (`hooks` or `events_only`), step count, model seen |
| `sabotage.json`, `task.json` | `step`, prepare | sabotage state; the task spec |

Step kinds: `write` is a write; `edit`, `multiedit`, `patch`, `apply_patch` are edits; `bash` is
shell (exit code from `metadata.exit`); `read`, `glob`, `grep`, `list` are reads; everything else
is `other`. Steps are snapshotted after writes, edits, shell commands, and unknown tools (`task`
subagents, MCP tools); `todowrite`, `webfetch` and similar are not. Each step keeps
`args["_session"]` and `args["_call"]` (OpenCode's session and call ids). Subagent calls are
recorded too.

`.opencode/` is excluded from snapshots, so the plugin never shows up as a change.

## How the steps get their reasoning and tokens

`tool.execute.after` gives the tool, its arguments, output and metadata, but not the model's
text or token counts. `finalize` reads those from `events.jsonl`:

* **Reasoning:** the assistant `text` and `reasoning` parts since the previous tool call (or the
  start of that model request), joined.
* **Tokens:** one model request is one `step-start` to `step-finish` group of parts. Its tokens go
  to the first tool call of the group, and the other calls of that group get 0, so the sum over
  steps counts each request once. `tokens_in = input + cache.read + cache.write` and
  `tokens_out = output + reasoning`. A request with no tool call (the final answer) is not
  attributed. A message with no `step-finish` parts uses its own `tokens`, on its first call.
* **Model and cost:** model id from the first assistant message (the alias from `--model` stays
  in `meta.model` when given); cost is the sum over assistant messages.

## If `tool.execute.after` does not fire

`trace.jsonl` stays empty. `finalize` then rebuilds the steps from completed tool parts in
`events.jsonl` and takes one snapshot of the final workdir, attached to the last mutating step
(`finalize.json` shows `"mode": "events_only"`). Limits: no per-step snapshots, so bisect can only
tell that the final state differs from the base, and sabotage injection does not happen. Treat such
a run as a smoke test, not as study data.

## If events are missing too

The trace still has the tool calls; reasoning and tokens stay empty. Check `plugin.log`.

## Assumptions to confirm on the first live run

Checked against OpenCode 1.19.3's strings, never against a live Alan run: the hook input shapes,
the part and message event shapes, and that bash reports `metadata.exit`. If the exit code is
missing, `exit_code` is `None` and the observed-step rule still accepts it.

---

# Your own project (bring your own repo)

`prepare_repo_run(origin, prompt, runs_dir, ...)` in `agentbisect.hooks.opencode` sets up an Alan
Code run on a Python project of yours. `origin` is a local directory or a git URL (`https://` or
`file://`; `ref` picks a branch or tag).

* A local directory is copied to `runs/<id>/workdir`, minus `.git`, `.venv`, `venv`,
  `__pycache__`, `.pytest_cache`, `.tox`, `node_modules`, `.opencode`, `build`, `dist` and
  `*.egg-info`. A URL is cloned with `--depth 1` and its `.git` is dropped. Your original is only
  read, never changed. The workdir then gets a fresh local git repo and a shadow repo, as for tasks.
* The dependencies go into `runs/<id>/venv`, outside the workdir, so snapshots never include them.
  The venv python is saved as `meta.python`, and the oracle runs your tests with it. The log of
  every install command is `runs/<id>/setup.log`. If the install fails, the run folder stays so you
  can read that log.
* `PROMPT.txt` is your prompt plus a line that names the venv python and the test command,
  because Alan's shell has no `python` or `pytest`.

What gets installed (`agentbisect/workspace.py`, `uv pip install --python <venv python> ...`):

| Project has | Command |
|---|---|
| `requirements*.txt`, `requirements/test*.txt` (names containing `doc` are skipped) | one command, `-r <file>` for each |
| `pyproject.toml` with `[project]` | `<workdir>[test,tests,testing,dev]` (only the extras it defines), a normal install, not editable |
| `[dependency-groups]` `test`, `tests`, `dev` | `--group <workdir>/pyproject.toml:<name>`, in the same command |
| `setup.py` or `setup.cfg`, no pyproject | `<workdir>` |
| anything | `pytest`, if the venv cannot import it |

If building the project itself fails, setup retries with only its declared dependencies and
test extras. The scratch tree comes first on `PYTHONPATH` when the oracle runs, so the installed
copy of your project never shadows the snapshot under test.

**Security: installing a repo's dependencies runs its build code (`setup.py`, build backends,
sdist builds). Use this mode only on repos you trust.** Copying or cloning runs nothing from the
repo, and a clone only accepts `https://` and `file://` URLs.
