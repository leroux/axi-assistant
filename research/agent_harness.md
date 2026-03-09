# Agent Harness Protocol

## The Gap

There are two ways to control an agent session today:

1. **Interactively** — a human types prompts, reads responses, decides what's next
2. **Flowcharts** — a JSON graph defines prompts, branches, and variable wiring; a walker drives the session mechanically

Interactive is fully flexible but requires a human in the loop. Flowcharts are automated but can't express error handling, data-dependent loops, dynamic iteration, self-modification, or anything beyond what the graph format supports.

The gap: there's no way for an **external program** to take control of an agent session, drive it through an arbitrary sequence of operations, and hand it back. The program would have full language capabilities (loops, try/catch, string ops, imports) while using the same session, permissions, and consumer interface.

An agent harness fills this gap.

---

## What a Harness Is

A harness is a program that drives an agent session over a simple stdio protocol. The host spawns the harness as a subprocess, connects its stdin/stdout, and executes whatever operations the harness requests.

```
Host (axi, Discord bot, TUI, CI runner, whatever)
  │
  │ spawns
  ▼
Harness process (Python, Rust, bash, anything)
  │
  │ stdout: JSON-line requests  ("run this prompt", "clear context", "run bash")
  │ stdin:  JSON-line responses (prompt result, bash output, events)
  │
  ▼
Host dispatches against the live agent session
```

The harness doesn't have an agent. It has a pipe. It sends operation requests and gets results back. The host owns the session, permissions, model selection, cost tracking — all of that. The harness just says what to do next.

A JSON flowchart's GraphWalker is one harness — it reads a JSON file and emits operation requests based on graph traversal. A Python script is another harness. A self-modifying hot-reloading program is another. They all speak the same protocol.

---

## The Protocol

NDJSON (newline-delimited JSON) over stdin/stdout. The harness writes requests to stdout, reads responses from stdin. Each message is one JSON object per line.

### Lifecycle

```
Host spawns harness subprocess
  │
  ├─ stdin:  { "type": "init", "args": {...}, "variables": {...} }
  │
  │  Harness runs, sending requests and receiving responses...
  │
  ├─ stdout: { "type": "finish", "result": "..." }
  │
Host collects result, harness process exits
```

The `init` message delivers arguments (from the user's command invocation) and any inherited variables. The harness MUST send a `finish` message before exiting. Exit without `finish` is treated as an error.

### Requests (harness → host)

The harness writes these to stdout:

#### `query` — Send a prompt to the agent

```json
{
  "type": "query",
  "id": "q1",
  "prompt": "Analyze this code for bugs:\n...",
  "session": null,
  "output_schema": null
}
```

- `id` — caller-chosen request ID, echoed in the response
- `prompt` — the text to send to the agent
- `session` — optional named session (for multi-session flows)
- `output_schema` — optional JSON Schema; if set, the host appends format instructions to the prompt and extracts structured fields from the response

Response:

```json
{
  "type": "query_result",
  "id": "q1",
  "text": "I found three issues...",
  "fields": null,
  "cost_usd": 0.003,
  "duration_ms": 4200
}
```

- `text` — the agent's full text response
- `fields` — if `output_schema` was set, extracted top-level JSON fields as `{key: value}`; `null` otherwise
- `cost_usd` — cost of this query
- `duration_ms` — wall-clock time

If the query fails (agent error, cancellation), the response has `"type": "error"` instead:

```json
{
  "type": "error",
  "id": "q1",
  "message": "Agent session was cancelled"
}
```

#### `bash` — Run a shell command

```json
{
  "type": "bash",
  "id": "b1",
  "command": "python3 validate.py puzzle.json",
  "working_directory": null
}
```

Response:

```json
{
  "type": "bash_result",
  "id": "b1",
  "stdout": "true",
  "exit_code": 0
}
```

Why have the host run bash instead of the harness doing it directly? Two reasons: (1) the host can apply sandboxing, working directory, and permission policies consistently, and (2) the host can emit `block_start`/`block_complete` events so the TUI tracks what's happening. That said, harnesses CAN run subprocesses directly — the protocol doesn't prevent it — they just won't show up in the TUI.

#### `clear` — Reset agent conversation context

```json
{
  "type": "clear",
  "id": "c1",
  "session": null
}
```

Response:

```json
{
  "type": "clear_result",
  "id": "c1"
}
```

Kills the inner agent subprocess and spawns a fresh one. Cost accumulation is preserved.

#### `finish` — Return result and terminate

```json
{
  "type": "finish",
  "result": "The analysis found 3 bugs...",
  "variables": {"rule": "rotate 90°", "valid": "true"}
}
```

No response — the host collects the result and the harness process exits. The `variables` field lets the harness pass data back to the host or a parent flowchart (for composability with sub-commands).

#### `log` — Emit a status message

```json
{
  "type": "log",
  "message": "Attempt 3: validation failed, evolving strategy..."
}
```

No response. The host displays this in the TUI or logs it. This is how harnesses participate in observability without the host needing to infer block structure.

#### `run_command` — Execute a named flowchart or harness

```json
{
  "type": "run_command",
  "id": "r1",
  "command": "story-writer",
  "args": "dragons",
  "inherit_variables": false
}
```

Response:

```json
{
  "type": "run_command_result",
  "id": "r1",
  "result": "Once upon a time...",
  "variables": {"draft_text": "...", "feedback": "..."},
  "status": "completed",
  "cost_usd": 0.012
}
```

Enables composability: a harness can invoke JSON flowcharts, or other harnesses, by name. The host resolves the command from search paths, runs it (as a nested flowchart walk or by spawning another harness subprocess), and returns the result.

### Notifications (host → harness, during a query)

While a `query` is pending, the host may send notifications on stdin. These are informational — the harness can ignore them or use them for progress tracking.

```json
{"type": "stream_text", "id": "q1", "text": "I found"}
{"type": "stream_text", "id": "q1", "text": " three issues"}
```

```json
{"type": "tool_use", "id": "q1", "tool": "Read", "input": {"file_path": "/src/main.rs"}}
```

```json
{
  "type": "control_request",
  "id": "q1",
  "request": {"subtype": "permissions_request", "request_id": "...", ...}
}
```

For `control_request`, the harness MAY respond:

```json
{
  "type": "control_response",
  "request_id": "...",
  "response": {"allowed": true}
}
```

If the harness doesn't respond to a `control_request`, the host auto-denies after a timeout. Most harnesses will ignore control requests and let the host handle permissions via its own policy.

### Summary

| Request | Response | Blocking? |
|---|---|---|
| `query` | `query_result` or `error` | Yes — harness waits for response |
| `bash` | `bash_result` | Yes |
| `clear` | `clear_result` | Yes |
| `finish` | (none) | Terminal |
| `log` | (none) | Fire-and-forget |
| `run_command` | `run_command_result` | Yes |

The protocol is strictly serial: one pending request at a time. The harness sends a request, waits for the response, then decides what to do next. This keeps the protocol simple and the harness easy to write.

---

## How It Fits

The host's dispatch loop doesn't care whether operations come from a GraphWalker or a harness subprocess:

```
                                    ┌─ GraphWalker (JSON flowchart)
                                    │    reads JSON, walks edges, emits ops
Operation source ───────────────────┤
                                    │
                                    └─ Harness subprocess
                                         any program, any language, emits ops

                                              │
                                              ▼

                                    Executor / Dispatcher
                                    (same code either way)
                                              │
                                    ┌─────────┼─────────┐
                                    ▼         ▼         ▼
                                 Session    Bash     Protocol
                                (agent)   (shell)   (events/TUI)
```

The `Action` enum already exists in the Rust codebase. The harness protocol is a 1:1 mapping:

| Harness request | Action variant |
|---|---|
| `query` | `Action::Query` |
| `bash` | `Action::Bash` |
| `clear` | `Action::Clear` |
| `finish` | `Action::Done` |
| `run_command` | `Action::SubCommand` |

The executor's dispatch code (`run_walker` loop in `executor.rs`) doesn't change. What changes is the **source** of actions — instead of always coming from `GraphWalker`, they can come from a harness subprocess via the NDJSON pipe.

### Command Resolution

A command's `command.json` gains a `type` field:

```json
{
  "name": "arc-solver",
  "type": "harness",
  "entrypoint": "arc_solver.py",
  "description": "Self-modifying ARC-AGI-2 puzzle solver",
  "arguments": [
    {"name": "puzzle", "description": "Path to puzzle JSON", "required": true}
  ],
  "session": {
    "model": "claude-sonnet",
    "system_prompt": "You are an expert at solving ARC-AGI puzzles."
  }
}
```

- `type: "flowchart"` (default) — existing behavior, `flowchart` field contains the graph
- `type: "harness"` — spawn `entrypoint` as subprocess, communicate via protocol

Arguments, session config, and discovery all stay in `command.json`. The consumer needs this metadata before spawning anything — it determines model, permissions, argument validation, and how to list available commands.

### TUI Integration

The existing TUI (`tui_protocol.rs`) renders block progress with spinners, colors by block type, stream text buffers, and timing. For harness commands:

- `log` messages display as status lines
- `query` requests show as prompt blocks (with streaming text)
- `bash` requests show as bash blocks
- `clear` shows as refresh blocks
- Duration and cost tracking work the same way

The TUI doesn't need to know the control flow. It just renders the operations as they happen.

---

## Why Not Just...

### ...the Agent SDK?

The Agent SDK creates and manages agents. A harness controls an **existing** session that belongs to someone else (the host). The session's model, permissions, system prompt, and cost tracking are already configured. The harness doesn't bootstrap any of that — it just drives.

Also: the Agent SDK is language-specific (Python, TypeScript). The harness protocol is language-agnostic — any process that speaks NDJSON on stdio.

### ...bash/spawn blocks in flowcharts?

Bash blocks execute in the context of the graph walker. They can't change the walker's control flow. They can't decide "based on this output, I need to run 3 more prompts in a specific order." They're leaves, not drivers.

### ...a library instead of a protocol?

A library (`from harness import runtime`) would be simpler for Python. But:

- A protocol works across languages without per-language libraries
- The process boundary gives natural isolation
- The host can apply security policy uniformly
- Testing is trivial — mock the stdin/stdout pipe

The library could exist as sugar on top of the protocol (reading/writing NDJSON internally). The protocol is the foundation.

### ...LangGraph / CrewAI / etc.?

Those are agent orchestration frameworks. They create their own agents, manage their own state, and expose their own interfaces. A harness plugs into an existing system — the host's session, the host's TUI, the host's permission model, the host's cost tracking. It's a component, not a framework.

---

## Self-Modification

The most interesting harness capability is self-modification — the harness changes its own behavior during or between runs. This takes several forms:

### Data accumulation

The harness reads and writes persistent files. A code reviewer accumulates project-specific rules in `knowledge.md`. Each run loads the current rules, does the review, observes the project's patterns, and appends new rules. The rules file is human-readable and prunable.

### Prompt evolution

The harness rewrites its own prompt templates. A prompt evolver runs a prompt against examples, evaluates the output, and rewrites the prompt to fix failures. The evolved prompt is saved to a file and loaded on next run. Over repeated runs, the prompt converges to something effective for the specific use case.

### Strategy hot-reload

The harness rewrites a Python module and `importlib.reload()`s it mid-run. The ARC solver writes a `transform.py` function, validates it, and if it fails, asks the agent to rewrite the function, reloads it, and retries — all within a single run. Between runs, the strategy module and knowledge base persist.

### Why this matters

A JSON flowchart can't self-modify because the graph IS the program. Changing a prompt means rewriting the JSON file, which means you need a program to rewrite JSON files, which means you need... a harness. The self-modification surface is the key capability that justifies the protocol as a separate layer.

---

## The Client Library

The protocol is simple enough that a minimal client fits in ~100 lines. A Python client:

```python
"""Harness client library. Reads/writes NDJSON on stdin/stdout."""

import json
import sys


def _send(msg):
    print(json.dumps(msg), flush=True)


def _recv():
    line = sys.stdin.readline()
    if not line:
        raise EOFError("Host closed connection")
    return json.loads(line)


def _request(msg):
    _send(msg)
    while True:
        resp = _recv()
        # Skip notifications (stream_text, tool_use, etc.)
        if resp.get("id") == msg.get("id") and resp["type"].endswith("_result"):
            return resp
        if resp.get("id") == msg.get("id") and resp["type"] == "error":
            raise RuntimeError(resp["message"])
        # Notification — ignore or pass to callback


def start():
    """Wait for init message, return args dict."""
    msg = _recv()
    assert msg["type"] == "init", f"expected init, got {msg['type']}"
    return msg.get("args", {}), msg.get("variables", {})


def query(prompt, session=None, output_schema=None, request_id=None):
    """Send a prompt to the agent, return response text (or fields if schema set)."""
    rid = request_id or f"q{id(prompt)}"
    resp = _request({
        "type": "query",
        "id": rid,
        "prompt": prompt,
        "session": session,
        "output_schema": output_schema,
    })
    if output_schema and resp.get("fields"):
        return resp["fields"]
    return resp["text"]


def bash(command, working_directory=None, request_id=None):
    """Run a shell command via the host. Returns (stdout, exit_code)."""
    rid = request_id or f"b{id(command)}"
    resp = _request({
        "type": "bash",
        "id": rid,
        "command": command,
        "working_directory": working_directory,
    })
    return resp["stdout"], resp["exit_code"]


def clear(session=None):
    """Reset agent conversation context."""
    _send({"type": "clear", "id": "clear", "session": session})
    _recv()  # clear_result


def log(message):
    """Emit a status message to the host (non-blocking)."""
    _send({"type": "log", "message": message})


def run_command(name, args="", inherit_variables=False, request_id=None):
    """Run a named flowchart or harness. Returns result string."""
    rid = request_id or f"r{id(name)}"
    resp = _request({
        "type": "run_command",
        "id": rid,
        "command": name,
        "args": args,
        "inherit_variables": inherit_variables,
    })
    return resp["result"]


def finish(result="", variables=None):
    """Return result to host and terminate."""
    _send({
        "type": "finish",
        "result": result if isinstance(result, str) else json.dumps(result),
        "variables": variables or {},
    })
    sys.exit(0)
```

Usage in a harness:

```python
import harness

args, _ = harness.start()

draft = harness.query(f"Write a story about {args['topic']}")
review = harness.query(f"Critique this:\n{draft}")
harness.clear()
final = harness.query(f"Rewrite incorporating feedback:\n{review}")

harness.finish(final)
```

---

## What Needs to Change in the Rust Codebase

### 1. Command resolution (`flowchart/src/resolve.rs`)

Add `type` field to `Command`. When `type == "harness"`, read `entrypoint` instead of `flowchart`. The resolver returns a `ResolvedCommand` enum:

```rust
enum ResolvedCommand {
    Flowchart(Command),               // existing
    Harness { meta: CommandMeta, entrypoint: PathBuf },
}
```

### 2. Harness adapter (`flowchart-runner/src/harness.rs`, new)

A new module that:
- Spawns the harness subprocess
- Sends `init` with args and variables
- Reads NDJSON requests from the child's stdout
- Dispatches them through the existing `Session` and `Protocol`
- Writes responses to the child's stdin
- Collects the `finish` result

This adapter produces the same `FlowchartResult` that `run_walker` produces. The executor doesn't know which path ran.

### 3. Executor integration (`flowchart-runner/src/executor.rs`)

`run_flowchart` gains a branch: if `ResolvedCommand::Harness`, call `harness::run_harness(session, protocol, meta, entrypoint, args, config, cancel)` instead of `run_walker`.

### 4. TUI protocol mapping

`log` messages → `protocol.on_log()`
`query` requests → `protocol.on_block_start()` with type `"prompt"`
`bash` requests → `protocol.on_block_start()` with type `"bash"`

The TUI already handles these. The main addition is that for harness commands, `total_blocks` is unknown upfront (the harness decides at runtime), so the progress display shows `[3/?]` instead of `[3/20]`.

### 5. Engine integration (`flowcoder-engine/src/main.rs`)

The engine's `try_run_flowchart` already calls `run_flowchart`. Once the executor handles harnesses, the engine gets harness support for free.

---

## Open Questions

**Should harnesses be able to run bash directly (bypassing the host)?**
Yes. The protocol offers `bash` as a request for TUI visibility, but harnesses are real processes — they can `subprocess.run()` whatever they want. The protocol doesn't enforce sandboxing; the host does that externally if needed. Same trust model as flowchart bash blocks.

**Should there be a `spawn_session` request for parallelism?**
Not in v1. Serial queries keep the protocol simple. A `spawn_session` extension (returns a session handle, future queries target it) is the natural extension point for multi-session harnesses, but it can wait until there's a concrete need.

**How does the harness know the working directory?**
The host spawns the harness with CWD set to the command's directory (where `command.json` lives). The harness's own files (strategies.py, knowledge.md) are relative to that.

**Can a harness be written in a compiled language?**
Yes. The `entrypoint` field is a path to an executable. If it's `./solver` (an ELF binary), the host spawns it directly. If it's `solver.py`, the host spawns `python3 solver.py`. Resolution follows shebang conventions.

**What about timeouts?**
Same as flowcharts: `soft_timeout_secs` on the executor config emits a warning. The host can set a hard timeout by cancelling after N seconds. The harness receives no special timeout signal — it just stops getting responses after the host cancels.
