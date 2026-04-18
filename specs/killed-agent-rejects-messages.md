<!-- test: tests/test_killed_agent_rejects_messages_generated.py -->

# Axi Killed Agent Rejects Messages

## Application Overview

When an agent is killed, its Discord channel remains as a read-only artifact but the underlying process is torn down. The bot should refuse to process new user messages in that channel — instead, it should post a short notice that the agent has been killed. This prevents accidental activation of dead agents and makes channel state obvious to the user.

## Scenarios

### 1. Messages to a killed agent's channel are rejected

**Steps:**
1. Spawn an agent named `smoke-killed-<timestamp>` in `#axi-master` and wait for its channel to appear.
2. Wait for the agent's first message in its channel (up to 120 seconds).
3. In `#axi-master`, send: `Kill the agent named "smoke-killed-<timestamp>"` and wait (up to 60 seconds) for the master to confirm.
4. In the killed agent's channel, send: `Are you alive?` and wait (up to 30 seconds) for the bot's reply (no "awaiting input" sentinel is required; the bot posts a plain notice).

**Expected Results:**
- Step 4: The bot's reply contains either the word `killed` or `has been killed` (case-insensitive).
- Step 4: The killed agent does NOT respond with a fresh Claude-generated answer.
- Cleanup: No further action needed — the agent is already dead.
