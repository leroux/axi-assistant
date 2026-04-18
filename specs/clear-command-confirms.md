<!-- test: tests/test_clear_command_confirms_generated.py -->

# Axi /clear Confirms Context Cleared

## Application Overview

The `/clear` command asks the current agent to wipe its Claude Code session context so the next message starts fresh. The bot posts a short confirmation back so the user knows the clear was received. This is a basic affordance for recovering from bad context or long histories.

## Scenarios

### 1. /clear confirms the context was cleared

**Steps:**
1. In `#axi-master`, send `/clear` (as plain text). Record the sent message ID so we can poll history after it.
2. Poll the channel history after that message ID for up to 15 seconds, collecting bot replies.

**Expected Results:**
- The concatenated bot replies after step 1 contain the word `clear` (case-insensitive), confirming the bot acknowledged the clear.
