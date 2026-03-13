# RFC-0003: Flowcoder

**Status:** Draft
**Created:** 2026-03-09

## Context

An **agent** is a long-lived coding assistant backed by a Claude CLI process (see
RFC-0001). Agents are powerful but unstructured — they receive a prompt and run
autonomously until done. This works well for open-ended tasks but poorly for
repeatable, multi-step workflows where the sequence of operations is known in advance.

A **flowchart** is a directed graph of typed blocks (prompts, bash commands, branches,
variables) that encodes a workflow as data. Each block yields an **action** requiring
external I/O (query an agent, run a shell command, invoke a sub-flowchart), and the
graph determines what happens next based on the result.

**Flowcoder** is the execution system for flowcharts. Four layers, each ignorant of
the others' concerns:

1. **Walker** — Pure synchronous state machine. Advances through the graph, yields
   actions, consumes results. No I/O.
2. **Executor** — Async loop driving the walker. Dispatches actions through abstract
   session and protocol interfaces. No display knowledge.
3. **Frontends** — TUI/REPL for interactive use; headless engine for bot integration.
   No graph knowledge.
4. **Bot integration** — Axi spawns the engine as a subprocess, transparently
   intercepting `/command`-prefixed messages.

## Problem

The system spans four crates and integrates with the main bot, but its behavior is
defined only by source code. Key behaviors — variable scoping in recursive
sub-commands, cancellation semantics, the engine's proxy/intercept split, control
message routing — are implicit in the implementation.

This RFC defines the canonical behavior: data model, execution semantics, variable
handling, control flow, sub-command recursion, the engine proxy protocol, and bot
integration.

## Data Model

A **command** is a named flowchart with typed argument definitions. A **flowchart** is
a list of blocks and directed connections between them. Connections have optional
`is_true_path` flags (required for branch blocks). Field aliases exist for
interoperability (`source_block_id` for `source_id`, `int`/`float` for `Number`).

### Block types

Every block has a unique `id` and a human-readable `name`.

| Type | Yields action? | Description |
|------|:-:|---|
| `start` | no | Entry point. Exactly one per flowchart. |
| `end` | no | Normal exit. At least one per flowchart. |
| `prompt` | **Query** | Send prompt to agent, optionally capture response in a variable. Supports `output_schema` for structured JSON extraction. |
| `variable` | no | Set a variable with type coercion. |
| `branch` | no | Conditional split — exactly 2 outgoing connections (true/false). |
| `bash` | **Bash** | Run shell command, capture stdout and exit code. |
| `command` | **SubCommand** | Invoke another flowchart with optional variable inheritance/merging. |
| `refresh` | **Clear** | Reset agent session context. |
| `exit` | **Exit** | Early termination with exit code. |
| `spawn` | **Spawn** | Concurrent sub-session (stub). |
| `wait` | **Wait** | Synchronize spawned sessions (stub). |

### Variable types

All variables are stored as strings. Type annotations on Variable blocks control
coercion: `String` (any value), `Number`/`int`/`float` (must parse as numeric),
`Boolean` (truthy → `"true"`, falsy → `"false"`), `JSON` (must be valid JSON).

## Resolution and Validation

**Resolution** searches for `<name>.json` in order: `$CWD/commands/`, `$CWD/`,
each search path (flat and `/commands/` subdir), then `~/.flowchart/commands/`.
First match wins. Search paths come from CLI flags and `$FLOWCODER_SEARCH_PATH`.

**Validation** runs after parsing, before execution. All errors collected (not
fail-fast): exactly one start block, at least one end/exit block, all connection
references valid, no orphaned blocks (BFS reachability), branch blocks have exactly
2 outgoing connections with distinct true/false paths.

## Execution: Walker

The walker is a **pure, synchronous state machine** — no I/O, no async, no side
effects. It holds the graph, current position, variable map, and block counter.

### Execution loop

1. `start()`: Find start block, advance to first successor, return first action.
2. **Advance**: For each block reached:
   - **Variable/Branch**: Process internally (interpolate, coerce, evaluate condition),
     advance immediately — no action yielded.
   - **End**: Return `Done`.
   - **All others**: Yield an action and wait for the caller to feed a result.
3. **Feed**: Store result in the configured variable (if any), advance, repeat.
4. **Termination**: `Done`, `Exit` (with code), or `Error` (block limit, bad state).

### Block limit

A counter increments on every block visit (including Variable and Branch). Exceeding
`max_blocks` (default 1000) halts with error. Cycles are legal — the limit is the
only guard against infinite loops.

### Template interpolation

Happens at execution time on prompt content, variable values, bash commands, branch
conditions, and sub-command arguments:

- `{{variable_name}}` → value (empty string if missing)
- `$N` → positional argument (empty string if missing)
- Whole-number floats render as integers: `"3.0"` → `"3"`

### Condition evaluation

- **Truthiness**: `varname` — falsy if missing, empty, `"false"`, `"0"`, `"no"`
- **Negation**: `!varname`
- **Comparison**: `left op right` (`==`, `!=`, `>`, `<`, `>=`, `<=`) — numeric if
  both sides parse as numbers, string otherwise

## Execution: Executor

The async executor drives the walker through two abstract interfaces: a **session**
(query the agent, clear context, interrupt) and a **protocol** (report block
lifecycle, stream text, log). The executor has no knowledge of what implements either.

### Action dispatch

| Action | Behavior |
|--------|----------|
| **Query** | Send prompt to session. If `output_schema` is set, append format instructions and extract the first JSON object from the response into individual variables. |
| **Bash** | Run `sh -c <command>`, capture stdout and exit code. Non-zero exit halts unless `continue_on_error`. |
| **SubCommand** | Resolve, validate, build child variables, recurse with incremented call stack. Merge child output variables into parent if configured. |
| **Clear** | Reset session context (kill and respawn subprocess). Cost survives. |
| **Exit** | Return with `Halted` status and exit code. |

### Variable building

Arguments are shell-split (quotes, backslash escaping). Positional args stored as
both `$N` and named keys. Defaults apply to missing optionals. Missing required args
without defaults are an error.

### JSON extraction

When `output_schema` is set, the executor extracts a JSON object from the response:
try the whole text, then markdown code blocks, then first-`{`-to-last-`}`. Only
objects accepted. Each top-level key becomes a walker variable.

### Sub-command recursion

- **Inheritance**: Child starts with parent's variables when `inherit_variables` is
  set. Child's own positional args take precedence.
- **Merging**: Child's final variables (excluding `$`-prefixed keys) merge into
  parent when `merge_output` is set.
- **Safety**: Direct self-recursion is rejected. Depth limit (default 10) bounds
  the call stack.

### Cancellation and pause

Cancellation token checked between every action — fires `session.interrupt()` (SIGINT)
and returns `Interrupted`. Pause flag (atomic bool + notify) suspends between actions;
used by the engine for external control.

## Frontend: Flowcoder TUI

Two modes: **single-command** (`flowcoder <cmd> [args]` — run and exit) and **REPL**
(interactive loop where `/<cmd>` runs flowcharts and plain text chats directly with
Claude).

The session wraps a Claude CLI subprocess. Clear kills and respawns it (cost
accumulates). SIGINT maps to cancellation. Permission control requests are
auto-allowed (`--skip-permissions`) or denied.

## Frontend: Flowcoder Engine

The engine is a **transparent NDJSON proxy** between an outer client and an inner
Claude CLI. It intercepts `/command`-prefixed user messages to run flowcharts;
everything else passes through unchanged.

### Architecture

```
Outer Client (bot/stdin)
    ↓ NDJSON
┌─────────────────────────┐
│   flowcoder-engine      │
│                         │
│  stdin router ──────────│── control_response channel (high priority)
│  (background)  ─────────│── main message channel
│                         │
│  main loop ─────────────│── proxy mode / flowchart mode
│                         │
│  inner Claude CLI ──────│── stream-json subprocess
└─────────────────────────┘
    ↓ NDJSON
Outer Client (stdout)
```

### Message routing

The **stdin router** splits incoming NDJSON: `control_response` messages go to a
dedicated channel; everything else goes to the main channel.

The **main loop** reads from the main channel. `user` messages are checked for a
`/command` prefix — match triggers flowchart mode, miss triggers proxy mode.
`engine_control` messages handle pause/resume/cancel/status.

### Proxy mode

Forward user message to inner CLI. Relay responses to stdout. `control_request`
messages from the inner CLI are emitted to stdout; the engine waits for
`control_response` on the dedicated channel. This enables the outer client to handle
permission prompts.

Before each query, pending `control_request` messages are flushed (100ms timeout) to
prevent protocol corruption.

### Flowchart mode

Resolve and validate the command. Spawn a control_reader task that owns the message
channel during execution — it processes `engine_control` commands and buffers
non-control messages. Execute the flowchart. Reclaim channel and replay buffered
messages.

The engine session relays `control_request` messages to the outer client (unlike the
TUI which handles them locally). The engine protocol emits structured JSON events
for block lifecycle, forwarded messages, status queries, and diagnostics.

### Startup

On launch, drain `control_request` messages from the inner CLI during MCP
initialization (60s timeout). The inner CLI expects responses to handshake messages
before accepting user input — skipping this causes deadlock.

## Bot Integration

When `flowcoder_enabled` is set, the bot wraps the Claude CLI in the engine binary
instead of spawning it directly. The engine is resolved from `$PATH`. Search paths
and Claude CLI args are passed via `--search-path` flags and a `--` separator.

The bot discovers available commands by scanning `$FLOWCODER_SEARCH_PATH` directories
for `command.yaml` files. The engine is transparent — the bot communicates via NDJSON
over stdin/stdout identically to a raw Claude CLI.

## Invariants

1. **Walker purity.** No I/O, no async, no side effects in the walker. All external
   work represented as Action variants. *[B17.4]*

2. **Interpolation timing.** Template substitution at block execution time, not parse
   time. Variables from earlier blocks visible to later blocks. *[B17.5]*

3. **Block limit enforcement.** Counter increments on every block visit (including
   internal blocks) and halts when exceeded. *[B17.10]*

4. **Sub-command variable isolation.** Positional (`$`-prefixed) variables from a
   child never leak into the parent during merge. *[B17.9]*

5. **Cancellation responsiveness.** Token checked between every action. No block may
   prevent cancellation indefinitely. *[B17.16]*

6. **Startup drain.** Engine must drain inner CLI `control_request` messages before
   accepting user input. *[I17.1]*

7. **Pre-query drain.** Pending `control_request` messages flushed before each
   proxy-mode query. *[I17.2]*

8. **SDK MCP passthrough.** SDK MCP servers must not be stripped when spawning the
   engine — it relays handshake messages to the outer client. *[I17.3, I6.6, I16.2]*

9. **Validation before execution.** Invalid flowcharts must never reach the walker.
   *[B17.3]*

10. **Recursion safety.** Direct self-recursion rejected. Call stack depth bounded.
    *[B17.14]*

## Open Questions

1. **Spawn/Wait blocks**: Stubbed. Remove from model or specify concurrent
   multi-session execution?

2. **Named sessions**: Model supports them, executor ignores them. Define
   session-switching semantics or defer?

3. **Error recovery**: Prompt failures halt the flowchart. Should there be a
   catch-style mechanism?

4. **Engine message buffering**: Non-control messages buffered during flowchart
   execution. Should there be a size limit?

## Implementation Notes

**axi-rs:** Four crates (`flowchart`, `flowchart-runner`, `flowcoder`,
`flowcoder-engine`) plus `axi/src/flowcoder.rs`. Walker is a struct with
`start()`/`feed()` methods. Executor uses tokio. Engine uses a background router
task splitting `control_response` into a dedicated channel.

**axi-py:** No implementation. Invokes the Rust engine binary as a subprocess.
