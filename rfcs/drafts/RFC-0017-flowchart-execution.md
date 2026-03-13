# RFC-0017: Flowchart Execution

**Status:** Draft
**Created:** 2026-03-09

## Problem

Flowchart execution is a multi-layered system spanning a graph walker (pure state machine), an async executor, a proxy engine for Claude CLI integration, and a standalone TUI. The interaction between these layers — especially the engine's control message relay, startup drain sequencing, and sub-command recursion — has produced multiple deadlock regressions. A normative specification is needed to pin down the message protocol, execution semantics, and cancellation behavior.

## Behavior

### Data Model

1. **Block types.** Flowchart JSON deserializes into a typed model via serde. `BlockData` is discriminated by a `type` tag with variants: `start`, `end`, `prompt`, `branch`, `variable`, `bash`, `command`, `refresh`, `exit`, `spawn`, `wait`.

2. **Connection aliases.** Connection fields accept both `source_id`/`target_id` and `source_block_id`/`target_block_id` aliases. `VariableType` accepts `int`/`float` as aliases for `Number`.

3. **Validation rules:**
   - Exactly one `start` block
   - At least one `end` or `exit` block
   - All connection `source_id`/`target_id` references resolve to existing blocks
   - No orphaned blocks (BFS reachability from start)
   - Branch blocks have exactly two outgoing connections, one with `is_true_path = true` and one with `is_true_path = false`

### Graph Walker

4. **Pure state machine.** `GraphWalker` advances through the flowchart graph, yielding `Action` variants whenever external I/O is needed: `Query`, `Bash`, `SubCommand`, `Clear`, `Spawn`, `Wait`. Variable and Branch blocks are processed internally without yielding.

5. **Template interpolation.** `{{variable_name}}` and `$N` positional references are replaced against the variable map. Whole-number floats (e.g., `"3.0"`) are rendered as integers (`"3"`).

6. **Branch evaluation.** Condition evaluation supports:
   - Simple truthiness: missing, empty, `"false"`, `"0"`, `"no"` are falsy; everything else is truthy
   - Negation: `!var` inverts truthiness
   - Comparison operators: `==`, `!=`, `>`, `<`, `>=`, `<=` with numeric-first then string fallback

7. **Variable coercion.** Variable blocks coerce values by declared type:
   - `Number`: validates numeric, normalizes whole floats to integer strings
   - `Boolean`: maps `"true"`/`"1"`/`"yes"` to `"true"`, all else to `"false"`
   - `Json`: validates JSON parseability, passes through
   - `String`: passthrough, no coercion

8. **Bash result handling.** `feed_bash` stores the exit code in the pending variable. On non-zero exit, execution halts with `Action::Error` unless `continue_on_error` is set. Stdout is trimmed before storing in the output variable.

9. **Sub-command result merging.** `feed_subcommand` merges child variables into the parent scope, skipping `$`-prefixed positional keys to prevent argument leakage from child to parent.

10. **Safety limit.** A configurable maximum block count (default 1000, set via `with_max_blocks` / `ExecutorConfig::max_blocks`) halts execution with `Action::Error` when exceeded, protecting against infinite loops.

### Command Resolution

11. **Search order.** Commands are resolved in this order:
    1. `cwd/commands/<name>.json`
    2. `cwd/<name>.json`
    3. Each search path: flat (`<path>/<name>.json`) and subdirectory (`<path>/commands/<name>.json`)
    4. `~/.flowchart/commands/<name>.json`

    `list_commands` scans the same directories, deduplicating by command name.

12. **Argument parsing.** `build_variables` performs shell-like splitting (single/double quotes, backslash escaping), maps positional args to `$N` keys and named keys from argument definitions, applies defaults for missing optional args, and returns an error for missing required args.

### Executor

13. **Async loop.** `run_flowchart` drives the walker in an async loop, dispatching each `Action` through a `Session` trait (query, bash, clear, sub-command) and reporting progress via a synchronous `Protocol` trait (block start/complete, stream text, flowchart start/complete).

14. **Sub-command recursion.** Sub-commands recurse via `Box::pin(run_walker)` with:
    - Call stack depth limit (default 10)
    - Direct recursion detection (same command calling itself)
    - Variable inheritance when `inherit_variables` is set
    - Child output variable merging when `merge_output` is set

15. **Output schema extraction.** When `output_schema` is set on a prompt block, the executor appends JSON format instructions to the prompt text and extracts JSON fields from the response into individual walker variables.

16. **Cancellation and pause.** Cancellation is checked between every action via `CancellationToken`, returning `ExecutionStatus::Interrupted` and calling `session.interrupt()`. Pause/resume is driven by an `AtomicBool` flag and `Notify`.

### Flowcoder Engine

17. **Transparent NDJSON proxy.** The `flowcoder-engine` binary proxies between an outer client and an inner Claude CLI subprocess. It intercepts user messages starting with `/command` to run flowcharts, forwarding all other messages unchanged.

18. **CLI argument normalization.** `build_claude_args` ensures the inner CLI always has `--print`, `--output-format stream-json`, `--input-format stream-json`, and `--replay-user-messages`, deduplicating any caller-provided equivalents.

19. **Command extraction.** `extract_command_name` parses user message content starting with `/` to extract a flowchart command name and arguments. Returns `None` for plain text, bare `/`, or non-string content.

20. **Engine session I/O.** `EngineSession` implements the `Session` trait by:
    - Writing user messages as NDJSON to the inner CLI's stdin
    - Reading stream events from the inner CLI's stdout
    - Relaying `control_request` messages to the outer client via stdout
    - Waiting for `control_response` messages from the control router

21. **Clear and respawn.** On `clear()`, the engine kills the inner CLI subprocess and respawns from the original CLI args, preserving accumulated cost tracking across clears.

22. **Control message routing.** `control::spawn_control_reader` owns the message channel during flowchart execution. It processes `engine_control` commands (pause/resume/cancel/status) and buffers non-control messages for replay after the flowchart completes.

### Flowcoder TUI

23. **Execution modes.** The `flowcoder` TUI supports:
    - Single-command mode: resolve, validate, run, exit with the flowchart's exit code
    - REPL mode: interactive command loop
    - SIGINT mapped to cancellation token
    - `--skip-permissions` auto-approves all tool permission requests

24. **Engine binary resolution.** The `axi` crate's flowcoder module resolves `flowcoder-engine` from `$PATH`, builds CLI args with `--search-path` flags and `--` separator for Claude passthrough args, and discovers commands by scanning `FLOWCODER_SEARCH_PATH` directories.

## Invariants

**I17.1:** Engine must drain startup `control_request` messages before accepting user input (60s first-message timeout, 2s inter-message timeout). Without this drain, the inner CLI blocks waiting for `control_response`s during SDK MCP server initialization while the engine blocks waiting for user messages — a mutual deadlock.

**I17.2:** Pre-query drain must flush pending `control_request`s with 100ms timeout before writing user messages to the inner CLI. `control_request` messages arriving between the startup drain and the first user message would otherwise corrupt the message protocol, causing the inner CLI to read a user message when it expects a `control_response`.

**I17.3:** SDK MCP servers must not be stripped from CLI config when spawning the flowcoder engine. The engine relays `control_request`/`control_response` messages, so stripping SDK servers removes MCP tool access from the inner Claude session.

## Open Questions

1. **Block type extensibility.** New block types (e.g., `http`, `parallel`) would require walker changes. Should the walker have a plugin/extension mechanism, or is direct modification acceptable?

2. **Variable scoping rules.** Sub-commands inherit and merge variables but skip `$`-prefixed keys. Should there be explicit `export`/`local` semantics for clearer scoping?

3. **Engine cost tracking.** Cost is preserved across `clear()` calls but there is no mechanism to report cumulative cost back to the outer client. Should the engine emit cost summary events?

## Implementation Notes

**axi-rs:** The flowchart system spans four crates:
- `flowchart/` — data model, parser, validator, walker (pure, no async)
- `flowchart-runner/` — async executor, `Session` and `Protocol` traits, variable builder
- `flowcoder-engine/` — NDJSON proxy binary with control message routing
- `flowcoder/` — standalone TUI binary

The walker is intentionally pure (no I/O, no async) so it can be tested without mocking. The `Session` trait abstracts all I/O, and the `Protocol` trait abstracts all progress reporting. Sub-command recursion uses `Box::pin` to allow async recursion within the executor loop.
