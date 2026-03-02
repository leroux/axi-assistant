# flowcoder-tui

Terminal UI for flowcoder — interactive REPL with spinners, streaming output, and progress display.

## Purpose

Provides a rich terminal interface for interacting with Claude and executing flowchart workflows. Features a braille spinner, live streaming text preview, block-by-block progress tracking, and color-coded output.

## Usage

```bash
# Start interactive REPL
flowcoder

# With options
flowcoder --model sonnet --commands-dir ./commands
```

### REPL Commands

| Command | Description |
|---|---|
| `/flowchart <name> [args]` | Execute a flowchart command |
| `/list` | Show available commands |
| `/model <name>` | Switch Claude model |
| `/cost` | Show total session cost |
| `/clear` | Reset conversation history |
| `/help` | Show help |
| `/quit`, `/exit` | Exit |

Normal text input is sent to Claude as a conversational query. Conversation history persists across both chat and flowchart modes.

## Architecture

```
User input
    |
  Repl (session manager)
    |
  /flowchart?
    |           |
   yes          no
    |           |
  GraphWalker   Session.query()
    |           |
  TuiProtocol (display)
    |
  Spinner + streaming lines + progress counter
```

## API

| Export | Description |
|---|---|
| `Repl` | Interactive REPL session |
| `TuiProtocol` | Rich terminal display (implements `ProtocolHandler`) |

### TuiProtocol Display

- Braille spinner at ~12 FPS during block execution
- Last 8 lines of streaming Claude output shown live
- Block progress counter `[N/M]`
- Color-coded blocks: prompt (cyan), branch (yellow), bash (blue), variable (magenta), command (white), refresh (dim)
- Summary footer with status, duration, cost, block count
- Falls back to plain text when stdout is not a TTY

## Dependencies

- `flowcoder-engine>=0.1.0`
- `flowcoder-flowchart>=0.1.0`

Requires Python 3.12+.
