<!-- test: tests/test_debug_toggle_command_generated.py -->

# Axi /debug Toggle

## Application Overview

The `/debug` slash-style command toggles debug mode for the current agent. Debug mode changes how the bot formats tool-call output (adds a wrench emoji and exposes tool arguments). Calling `/debug` twice should flip the mode off and on again, with the bot confirming the new state each time.

## Scenarios

### 1. /debug toggles both on and off

**Steps:**
1. In `#axi-master`, send `/debug` (sent as plain text, no "awaiting input" sentinel required) and wait up to 15 seconds for the bot's reply.
2. In `#axi-master`, send `/debug` again and wait up to 15 seconds for the bot's reply.

**Expected Results:**
- Step 1 reply contains the phrase `debug mode` (case-insensitive).
- Step 2 reply contains the phrase `debug mode` (case-insensitive).
- Both replies are non-empty.
