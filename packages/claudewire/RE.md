# Reverse Engineering Notes

How PROTOCOL.md was built.

## Two Sources

1. **Binary string extraction** — The Claude CLI (`~/.claude/local/claude`) is a Bun-compiled ELF. All JS string literals (including Zod schema definitions, enum values, and type discriminators) survive as plain text in the binary. `strings` + regex is the primary discovery method.

2. **Runtime log analysis** — Running the CLI with `--output-format stream-json` (via `-p` for single-turn or the SDK's `SubprocessCLITransport`) produces real NDJSON protocol traffic on stdout. procmux's bridge-stdio logger captures this in production; `stdio-spy` (sibling package) can capture ad-hoc sessions.

Binary extraction finds all *possible* types and fields. Runtime logs confirm which ones actually appear on the wire and show real message shapes.

## Quick Reference

```bash
# Discovery: extract all type literals from the binary
strings ~/.claude/local/claude | grep -oP 'z\.literal\("([^"]+)"\)' | sort -u

# Discovery: extract Zod enum definitions
strings ~/.claude/local/claude | grep -oP 'z\.enum\(\[("[a-z_]+"(,"[a-z_]+")*)\]\)'

# Validation: capture real protocol traffic
stdio-spy --log /tmp/capture.log -- env -u CLAUDECODE claude -p "hello" --output-format stream-json

# Validation: mine existing bridge-stdio logs
grep '<<< STDOUT' ~/logs/procmux-stdio-axi-master.log | grep -oP '"type":"[^"]+"' | sort | uniq -c | sort -rn
```

## Gotchas

- The CLI is Bun, not Node.js — no V8 snapshot decompilers work.
- TUI and agent loop run in the same process — no internal IPC to intercept.
- `-p --output-format stream-json` produces the same protocol the TUI uses internally.
- Always cross-validate binary strings against real traffic — some may be dead code.
