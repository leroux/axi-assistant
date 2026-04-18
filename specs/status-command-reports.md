<!-- test: tests/test_status_command_reports_generated.py -->

# Axi Status Slash Command

## Application Overview

Axi exposes a handful of built-in slash-style commands in `#axi-master`. `/status` asks the running master to report its own name, state, and basic metadata. This is a fast sanity check that slash-command routing is alive and the master can introspect itself.

## Scenarios

### 1. /status returns master info

**Steps:**
1. In `#axi-master`, send the text: `/status`.
2. Wait (up to 30 seconds) for the master to reply. The reply is a direct system message; no "awaiting input" sentinel is required.

**Expected Results:**
- The reply text references the master agent (contains `axi-master` or `master`, case-insensitive).
- The reply is non-empty.
