# Flowcoder Requirements

## What is Flowcoder

Flowcoder executes flowcharts — directed graphs of blocks — where each block performs an action (prompt Claude, run a bash command, evaluate a condition, set a variable, etc.). A user triggers a flowchart via a slash command (e.g. `/story "a dragon"`), and the system walks the graph, executing blocks in order until it reaches an end block.

Flowcharts are defined in YAML files and discovered from configurable search paths. Each command is a directory containing a `command.yaml` with the flowchart definition, arguments, and session configuration.

## Purpose

Flowcoder is an SDK — a set of Rust crates that any application can use to parse, validate, and execute flowcharts through any coding agent. The SDK is both application-agnostic and agent-agnostic. Axi, a GUI, a TUI, or any other frontend can build on top of it, and any coding agent (Claude Code, Codex, OpenCode, etc.) can power the prompt execution.

## Architecture

Two crates:

### `flowchart` — Data Layer

Parses YAML flowchart definitions into typed Rust structs. Validates graph structure. Resolves slash commands to flowchart files from search paths.

No async, no IO beyond filesystem reads. Depends on `serde`, `serde_yaml`, and standard library.

Data model:
- **Flowchart**: a graph of blocks connected by edges, plus declared arguments and session config
- **Block**: a node in the graph. Eight types:
  - `start` — entry point, exactly one per flowchart
  - `end` — terminal, stores the final output variable name
  - `prompt` — sends a prompt (with variable interpolation) to the LLM session and stores the response
  - `branch` — evaluates a condition and routes to one of two outputs (true/false)
  - `variable` — sets a variable to a value (with interpolation)
  - `bash` — runs a shell command, stores stdout
  - `command` — invokes another flowchart (sub-command composition)
  - `refresh` — clears the LLM conversation (resets context)
- **Connection**: directed edge between blocks, with an optional label (used by branch blocks for true/false routing)
- **Argument**: declared command argument with name, description, required flag, and default value
- **SessionConfig**: per-flowchart agent settings (model, system prompt, permission mode)

Variable interpolation: block prompts and values use `{{variable_name}}` syntax. Positional arguments are also available as `$1`, `$2`, etc.

### `flowchart-runner` — Execution Layer

Walks the flowchart graph and executes blocks. Takes an LLM session (provided by the consumer) and an event channel (for progress reporting). The runner does not create sessions or manage processes — the consumer does that.

The runner is agent-agnostic. It defines the interface it needs from a coding agent session — send a prompt, receive results (including intermediate events like tool use, file edits, streaming text), clear conversation, stop — and the consumer provides an implementation for their agent. This could be Claude Code (via claudewire), Codex, OpenCode, or any other coding agent that can accept a prompt and execute it.

A coding agent session is not a simple LLM API call. A single prompt may trigger a full agentic loop — the agent reads files, writes code, runs commands, uses tools — before returning a final result. The session interface accounts for this by streaming intermediate events during execution.

Depends on `flowchart`, `tokio`, `serde_json`.

Core type: `GraphWalker` — given a parsed flowchart, an LLM session, initial variables, and an event sender, it walks the graph from the start block to an end block, executing each block's action.

Block execution:
- **prompt**: interpolate variables into the prompt template, send to the coding agent session, store response text in the block's output variable
- **branch**: interpolate the condition, evaluate it (agent-assisted or simple comparison), follow the true or false edge
- **variable**: interpolate the value template, store in the variables map
- **bash**: interpolate the command, run it as a subprocess, store stdout in the output variable
- **command**: resolve the sub-command, parse its flowchart, create a nested walker, run it, merge output variables back
- **refresh**: clear/restart the coding agent session (reset conversation context)
- **start/end**: control flow only

Events emitted during execution:
- Flowchart started/completed
- Block started/completed (with block type, name, status)
- Agent messages forwarded from the session (assistant text, tool use, file edits, command execution, streaming) during prompt blocks

The consumer decides what to do with events: render in a GUI, print in a TUI, forward over Discord, ignore them.

Safety: configurable max-blocks limit to prevent infinite loops in cyclic graphs.

### Coding Agent Session Interface

The runner requires a coding agent session that supports three operations:

1. **Query** — send a prompt, wait for the agent to complete its work. During execution, the agent may perform many actions (tool use, file reads/writes, command execution, streaming text). These intermediate events are forwarded to the consumer. Returns the final result text.
2. **Clear** — reset the conversation context (used by refresh blocks).
3. **Stop** — terminate the agent session.

This is a small interface. The consumer implements it for their coding agent. A Claude Code adapter wraps claudewire's `CliSession` (write a user message, read events until a result message). A Codex adapter wraps Codex's CLI subprocess protocol. A mock implementation returns canned responses for testing.

## What the Consumer Provides

The consumer (axi, GUI, TUI, etc.) is responsible for:

1. **A coding agent session implementation** — wraps their chosen agent (Claude Code, Codex, OpenCode, etc.) into the session interface the runner expects

2. **An event channel** — receives progress events from the walker and decides how to present them

3. **Slash command detection** — parsing user input to identify flowchart commands. The `flowchart` crate provides command resolution (name to YAML file), but the consumer decides when and how to check for slash commands

4. **Session lifecycle** — creating, resuming, and destroying agent sessions. The walker uses whatever session it's given; it doesn't manage session lifecycle

## What the SDK Does NOT Do

- Create or manage agent sessions (the consumer provides them)
- Manage processes or connections (that's the consumer's infrastructure)
- Handle reconnection or resumption (that's the consumer's session management)
- Read from stdin or write to stdout (that's a frontend concern)
- Know about Discord, procmux, or any specific agent CLI protocol or deployment topology
- Provide a binary or proxy server (consumers build their own if needed)
