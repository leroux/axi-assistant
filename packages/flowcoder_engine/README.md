# flowcoder-engine

Transparent Claude CLI proxy that intercepts slash commands and executes flowchart workflows.

## Purpose

Acts as a drop-in wrapper around `claude -p --input-format stream-json --output-format stream-json`. Normal messages pass through transparently. When a message matches a `/command` pattern and a corresponding flowchart JSON exists, the engine takes over and executes the flowchart — walking blocks, managing Claude sessions, and emitting structured output.

## Architecture

```
stdin (stream-json)
      |
  _StdinReader → _MessageRouter (demux)
      |                  |
  message_queue    control_response_queue
      |
  /command detected?
      |           |
     yes          no
      |           |
  GraphWalker   proxy to inner claude
      |
  Session(s) → ClaudeProcess (PTY subprocess)
      |
  ProtocolHandler → stdout (stream-json)
```

## Usage

### As a CLI proxy

```bash
# Wraps claude CLI — all normal args pass through
flowcoder-engine --model sonnet --search-path ./commands

# Flowchart commands are invoked with slash syntax
# e.g. user sends: /story "a brave knight"
# Engine resolves commands/story.json and executes it
```

### Programmatically

```python
from flowcoder_engine.walker import GraphWalker, ExecutionResult
from flowcoder_engine.session import Session
from flowcoder_engine.protocol import ProtocolHandler
from flowcoder_engine.resolver import resolve_command

cmd = resolve_command("story", search_paths=["./commands"])
walker = GraphWalker(
    flowchart=cmd.flowchart,
    protocol=protocol,
    session_factory=make_session,
    variables={"$1": "a dragon"},
)
result: ExecutionResult = await walker.run()
# result.status: "completed" | "halted" | "error"
# result.variables: final variable state
# result.log: list of LogEntry
```

## Key Components

| Module | Description |
|---|---|
| `walker.py` | `GraphWalker` — walks flowchart blocks from START to END |
| `session.py` | `Session` — manages one Claude CLI subprocess lifecycle |
| `subprocess.py` | `ClaudeProcess` — low-level async PTY subprocess I/O |
| `protocol.py` | `ProtocolHandler` — JSON-lines output (block start/complete, results) |
| `resolver.py` | `resolve_command()` — precedence-based command lookup |
| `json_parser.py` | Extract JSON from Claude responses (code blocks, raw) |
| `cli.py` | CLI argument parsing and variable mapping |
| `__main__.py` | Proxy core — stdin demux, slash command detection, forwarding |

## Command Resolution

Search order for `/story`:
1. `./commands/story.json`
2. Each `--search-path` directory
3. `~/.flowcoder/commands/story.json`

## Safety Limits

- Max blocks per execution: 1000 (configurable via `--max-blocks`)
- Max recursion depth: 10 (nested `CommandBlock` calls)
- Soft timeout warning: 300 seconds

## Dependencies

- `flowcoder-flowchart>=0.1.0`

Requires Python 3.12+.
