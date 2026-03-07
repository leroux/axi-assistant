# stdio-spy

Bidirectional stdio proxy/logger. Sits between a parent process and a child command, forwarding stdin/stdout/stderr while logging everything with timestamps and direction markers.

## Install

```bash
uv pip install -e packages/stdio-spy
```

## Usage

### Generic proxy

```bash
stdio-spy --log /tmp/capture.log -- <command> [args...]
```

Runs `<command>` with a PTY for stdin/stdout (so TUI apps see a real terminal) and a pipe for stderr. All traffic is logged with direction markers:

```
2026-03-06 23:45:12,345 >>> STDIN  hello
2026-03-06 23:45:13,456 <<< STDOUT world
2026-03-06 23:45:13,789 <<< STDERR warning: something
```

### Claude CLI wrapper

```bash
claude-spy [claude-args...]
```

Convenience wrapper that logs to `~/claude-captures/YYYYMMDD_HHMMSS.log` and passes all args to `claude`:

```bash
claude-spy -p "hello" --output-format stream-json
claude-spy --model opus --resume
```

## Capturing Claude Code SDK Protocol Traffic

stdio-spy can sit between the Claude Code SDK and the CLI binary to capture the full stream-json wire protocol.

### How it works

The Claude Code SDK (`SubprocessCLITransport`) spawns `claude` as a subprocess and communicates via NDJSON on stdin/stdout. By wrapping the CLI with stdio-spy, every message in both directions gets logged without affecting the protocol.

### With `claude -p` (single-turn)

The simplest way to capture protocol traffic:

```bash
stdio-spy --log /tmp/protocol.log -- env -u CLAUDECODE claude -p "read README.md" --output-format stream-json
```

`env -u CLAUDECODE` is needed to bypass the CLI's nesting check when running inside another Claude session.

### With the SDK transport

To capture traffic from a real SDK session, wrap the CLI binary path:

```python
# Instead of spawning "claude" directly, spawn it through stdio-spy
import subprocess

proc = subprocess.Popen(
    ["stdio-spy", "--log", "/tmp/sdk-capture.log", "--", "claude", "--output-format", "stream-json", "-p", ...],
    stdin=subprocess.PIPE,
    stdout=subprocess.PIPE,
    stderr=subprocess.PIPE,
)
```

Or modify the SDK's transport to use a wrapper script.

### Analyzing captures

**Extract all message types:**

```bash
grep '<<< STDOUT' capture.log | grep -oP '"type":"[^"]+"' | sort | uniq -c | sort -rn
```

**Extract system subtypes:**

```bash
grep '<<< STDOUT' capture.log | grep '"type":"system"' | grep -oP '"subtype":"[^"]+"' | sort | uniq -c | sort -rn
```

**Pretty-print a specific message type:**

```bash
grep '<<< STDOUT' capture.log | grep '"type":"result"' | sed 's/^[^{]*//' | python3 -m json.tool
```

**Find all unique message types (including stream_event subtypes):**

```bash
grep '<<< STDOUT' capture.log | sed 's/^[^{]*//' | python3 -c "
import sys, json
types = set()
for line in sys.stdin:
    line = line.strip()
    if not line: continue
    try:
        msg = json.loads(line)
        t = msg.get('type', '?')
        if t == 'stream_event':
            et = msg.get('event', {}).get('type', '?')
            types.add(f'stream_event.{et}')
        else:
            types.add(t)
    except json.JSONDecodeError:
        pass
for t in sorted(types):
    print(t)
"
```

## Log format

Matches procmux bridge-stdio format — UTC timestamps, direction markers:

| Marker | Meaning |
|--------|---------|
| `>>> STDIN ` | Data sent to the child process |
| `<<< STDOUT` | Data received from the child's stdout |
| `<<< STDERR` | Data received from the child's stderr |

In stream-json mode, stdout lines are NDJSON (one JSON object per line). In TUI mode, stdin contains raw terminal escape sequences.

## Architecture

- **PTY** for stdin/stdout — the child sees a real terminal, so TUI apps work correctly
- **Pipe** for stderr — kept separate from stdout for distinct logging
- **Signal forwarding** — SIGINT, SIGTERM, SIGHUP, SIGWINCH forwarded to child
- **EOF handling** — sends EOT (0x04) through PTY line discipline instead of closing the fd (avoids SIGHUP)
- **Non-blocking I/O** — `select()` polls master_fd, stderr pipe, and stdin simultaneously
