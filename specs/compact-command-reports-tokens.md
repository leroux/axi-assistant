<!-- test: tests/test_compact_command_reports_tokens_generated.py -->

# Axi /compact Reports Tokens

## Application Overview

The `/compact` command asks the Claude API to compact the current session's context and returns a short summary including the resulting token count. It is useful when a session grows large but the user wants to preserve the gist of the conversation. The response should mention either "compact" or "token" so the user can see something actually happened.

## Scenarios

### 1. /compact reports a compacted session

**Steps:**
1. In `#axi-master`, send `/compact` (as plain text). Record the sent message ID.
2. Poll history after that message ID for up to 60 seconds, collecting bot replies.

**Expected Results:**
- The concatenated bot reply after step 1 contains either the word `compact` or `token` (case-insensitive) — a signal that the compaction path was exercised.
