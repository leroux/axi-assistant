# Capturing Claude CLI Protocol Traffic

How to capture raw stdio traffic from the Claude CLI TUI to discover new protocol messages, test edge cases, and improve claudewire's schema coverage.

## Why

claudewire validates every CLI message against pydantic models. When the CLI adds new message types, fields, or changes behavior, validation warnings appear in procmux bridge logs — but only for scenarios that our agents actually exercise. Manual TUI sessions let you deliberately trigger features (plan mode, ask user question, todo write, file operations, etc.) that agents may not use frequently.

The captured logs use the same format as procmux bridge-stdio logs, so they can be fed directly to existing analysis tools or diffed against bridge logs.

## Setup

Install the `stdio-spy` package (sibling package in this repo):

```bash
# From the repo root
uv pip install -e packages/stdio-spy
```

This provides two commands:
- `stdio-spy` — generic stdio proxy/logger (works with any command)
- `claude-spy` — convenience wrapper for Claude CLI

## Capturing a session

### Quick (claude-spy)

```bash
claude-spy --model opus
```

Logs to `~/claude-captures/YYYYMMDD_HHMMSS.log` automatically.

Pass any Claude CLI args after `claude-spy`:

```bash
claude-spy --model sonnet --resume
claude-spy -p "explain this code"
```

### Manual (stdio-spy)

```bash
stdio-spy --log /tmp/my-capture.log -- claude --model opus
```

Use this when you want a custom log path or are capturing a non-Claude command.

## What to exercise

To maximize protocol coverage, try these during a capture session:

1. **Basic conversation** — send a message, let it respond
2. **Tool calls** — ask it to read/edit files, run commands
3. **Plan mode** — ask it to plan something (triggers `EnterPlanMode`, `ExitPlanMode` control requests)
4. **AskUserQuestion** — trigger a multi-choice question (e.g. "which approach should we use?")
5. **TodoWrite** — ask it to create a task list
6. **File operations** — read, write, edit, glob, grep
7. **Errors** — trigger a tool error (e.g. read a nonexistent file)
8. **Rate limits** — if you hit one, the `rate_limit_event` will be captured
9. **Context compaction** — long sessions trigger autocompact (watch stderr)
10. **Interrupts** — press Ctrl+C mid-response to see how the CLI handles it
11. **Multi-turn** — keep the session going to see session lifecycle messages

## Log format

Matches procmux bridge-stdio logs:

```
2026-03-06 23:45:12,345 >>> STDIN  {"type":"user_message","content":"hello"}
2026-03-06 23:45:13,456 <<< STDOUT {"type":"stream_event","event":{"type":"content_block_start",...}}
2026-03-06 23:45:13,789 <<< STDOUT {"type":"stream_event","event":{"type":"content_block_delta",...}}
```

Direction markers:
- `>>> STDIN ` — data sent to the CLI (your keystrokes / JSON messages)
- `<<< STDOUT` — data from the CLI (protocol messages, TUI output)
- `<<< STDERR` — stderr from the CLI (debug output, autocompact lines)

Note: In TUI mode, stdin contains raw terminal escape sequences (arrow keys, etc.), not clean JSON. The JSON protocol messages are visible in stdout.

## Analyzing captures

### Find all unique message types

```bash
grep '<<< STDOUT' capture.log | sed 's/^[^{]*//' | python -c "
import sys, json
types = set()
for line in sys.stdin:
    line = line.strip()
    if not line:
        continue
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

### Compare with known schema

Feed captures to the claudewire schema validator to find new/changed fields:

```python
import json
from claudewire.schema import validate_inbound_or_bare

with open("capture.log") as f:
    for line in f:
        if "<<< STDOUT" not in line:
            continue
        # Extract JSON after the direction marker
        json_start = line.find("{")
        if json_start == -1:
            continue
        try:
            msg = json.loads(line[json_start:])
            result = validate_inbound_or_bare(msg)
            if not result.ok:
                print(f"VALIDATION ERRORS: {result.errors}")
                print(f"  message: {json.dumps(msg)[:200]}")
        except json.JSONDecodeError:
            pass
```

### Diff against bridge logs

```bash
# Extract message types from a capture
grep '<<< STDOUT' capture.log | sed 's/^[^{]*//' | jq -r '.type' 2>/dev/null | sort -u > capture-types.txt

# Extract from bridge logs
grep '<<< STDOUT' /path/to/procmux-stdio-agent.log | sed 's/^[^{]*//' | jq -r '.type' 2>/dev/null | sort -u > bridge-types.txt

diff capture-types.txt bridge-types.txt
```

## Feeding results to Claude for claudewire updates

After capturing, share the log (or relevant excerpts) with Claude/Axi and ask it to:

1. Run the schema validator against the capture to find unknown fields or types
2. Add new pydantic models or fields to `claudewire/schema.py`
3. Add real samples to `tests/unit/test_claudewire_schema_real.py`
4. Update `PROTOCOL.md` if new message flows are discovered

Example prompt:
> Here's a stdio-spy capture of a Claude CLI TUI session where I exercised plan mode and tool calls. Run the schema validator against it, identify any validation warnings, and update claudewire's schema to handle the new fields/types.
