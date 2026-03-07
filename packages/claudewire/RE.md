# Reverse Engineering the Claude CLI Protocol

How PROTOCOL.md was built — methodology and tools for extracting protocol details from the Claude CLI binary.

## Binary Identification

The Claude CLI (`~/.claude/local/claude`) is a **Bun-compiled** ELF binary, not a Node.js Single Executable Application (SEA). Identified by:

```bash
# Check for Bun signatures
strings ~/.claude/local/claude | grep -i "BUN_"
# → BUN_1.2, __BUN_INTERNALS_DO_NOT_USE_OR_YOU_WILL_BE_FIRED__

# ELF section headers show Bun's JIT opcodes
readelf -S ~/.claude/local/claude | grep jsc
# → __DATA,__jsc_opcodes
```

This matters because the embedded JS is minified but not encrypted — all string literals survive in the binary.

## Method 1: String Extraction from Binary

The primary method. The minified JS contains all Zod schema definitions, enum values, and type literals as plain strings.

### Extract message type discriminators

```bash
# All top-level type values
strings ~/.claude/local/claude | grep -oP '"type":"[a-z_]+"' | sort -u

# Zod enum definitions (z.enum patterns)
strings ~/.claude/local/claude | grep -oP 'z\.enum\(\[("[a-z_]+"(,"[a-z_]+")*)\]\)' | head -40

# Literal union types (z.literal patterns)
strings ~/.claude/local/claude | grep -oP 'z\.literal\("([^"]+)"\)' | sort -u
```

### Extract system subtypes

```bash
strings ~/.claude/local/claude | grep -oP 'z\.literal\("(init|status|compact_boundary|task_started|hook_started|[a-z_]+)"\)' | sort -u
```

### Extract control_request subtypes

```bash
strings ~/.claude/local/claude | grep -oP '"subtype":"(can_use_tool|mcp_message|initialize|interrupt|set_mode|elicitation[^"]*)"' | sort -u
```

### Extract field names from Zod object schemas

```bash
# Look for object property definitions near known type names
strings ~/.claude/local/claude | grep -oP '[a-zA-Z_]+:z\.(string|number|boolean|enum|literal|object|array|optional)' | sort -u
```

### Extract Task tool input schema

```bash
strings ~/.claude/local/claude | grep -A5 -B5 "subagent_type"
strings ~/.claude/local/claude | grep -oP '"(general-purpose|Explore|Plan|codebase-locator|thoughts-locator|thoughts-analyzer|web-search-researcher|codebase-analyzer|codebase-pattern-finder|statusline-setup|worker)"'
```

### Extract rate limit enums

```bash
strings ~/.claude/local/claude | grep -oP '"(overage_not_provisioned|org_level_disabled|out_of_credits|[a-z_]+)"' | sort -u
# Cross-reference with overageDisabledReason context
strings ~/.claude/local/claude | grep "overageDisabledReason"
```

### Extract teammate mailbox message types

```bash
strings ~/.claude/local/claude | grep -oP '"(idle_notification|permission_request|permission_response|shutdown_request|shutdown_approved|shutdown_rejected|plan_approval_request|plan_approval_response|mode_set_request|team_permission_update|sandbox_permission_request|sandbox_permission_response|task_completed)"'
```

### Extract hook event types

```bash
strings ~/.claude/local/claude | grep -oP '"(PreToolUse|PostToolUse|Notification|Stop|SubagentStop|TeammateIdle|TaskCompleted|Elicitation|Compact)"'
```

## Method 2: Bridge-stdio Log Analysis

Cross-reference binary findings with real protocol traffic captured by procmux's bridge-stdio logger.

```bash
# Log location
ls ~/logs/procmux-stdio-*.log

# Extract all message types seen in traffic
grep '<<< STDOUT' ~/logs/procmux-stdio-axi-master.log | \
  grep -oP '"type":"[^"]+"' | sort | uniq -c | sort -rn

# Extract all system subtypes
grep '<<< STDOUT' ~/logs/procmux-stdio-axi-master.log | \
  grep '"type":"system"' | grep -oP '"subtype":"[^"]+"' | sort | uniq -c | sort -rn

# Extract control_request subtypes
grep '<<< STDOUT' ~/logs/procmux-stdio-axi-master.log | \
  grep '"type":"control_request"' | grep -oP '"subtype":"[^"]+"' | sort | uniq -c | sort -rn

# Full message examples for a given type
grep '<<< STDOUT' ~/logs/procmux-stdio-axi-master.log | \
  grep '"subtype":"task_started"' | head -3 | python3 -m json.tool
```

## Method 3: stdio-spy Captures

Use the `stdio-spy` package (in this repo) to capture fresh protocol traffic from live Claude CLI sessions:

```bash
# Install
cd packages/stdio-spy && pip install -e .

# Capture a session
claude-spy -p "hello" --output-format stream-json

# Or wrap manually with a custom log path
stdio-spy --log /tmp/capture.log -- env -u CLAUDECODE claude -p "hello" --output-format stream-json

# Analyze the capture
grep '<<< STDOUT' /tmp/capture.log | grep -oP '"type":"[^"]+"' | sort | uniq -c | sort -rn
```

## Method 4: Targeted String Searches

When you know a field name or value exists but need surrounding context:

```bash
# Find all strings near a known field (within 200 chars)
strings -n 10 ~/.claude/local/claude | grep "parent_tool_use_id"

# Extract longer string fragments containing protocol-related terms
strings -n 50 ~/.claude/local/claude | grep -i "elicitation"

# Find JSON-like fragments
strings -n 100 ~/.claude/local/claude | grep '"type"' | grep '"subtype"'
```

## What Didn't Work

- **Decompiling the Bun binary**: No reliable Bun decompiler exists. The JS is minified and embedded in Bun's custom format, not standard V8 snapshots.
- **`--sdk-url` WebSocket bridge**: The CLI supports a `--sdk-url` flag for remote bridge sessions, but it requires authentication infrastructure we don't control.
- **Intercepting internal IPC**: The TUI and agent loop run in the **same process** — there's no internal transport to intercept. The stream-json output from `-p --output-format stream-json` produces the same protocol the TUI consumes internally.
- **Patching the JS**: While theoretically possible to extract and modify the embedded JS, Bun's binary format makes reinsertion impractical.

## Tips

- **Binary updates**: After Claude CLI updates (`~/.claude/local/claude` changes), re-run the string extraction to find new message types or fields.
- **Zod patterns**: The minified Zod calls are the most reliable source — they define the exact validation schema the CLI uses internally.
- **Cross-validate**: Always cross-reference binary strings with actual traffic. Some extracted strings may be dead code or internal-only types never emitted on the wire.
- **`strings -n`**: Use `-n 10` minimum to filter noise. The binary contains millions of short strings from Bun's runtime.
