<!-- test: tests/test_agent_restart_reuses_channel_generated.py -->

# Axi Agent Restart Reuses Channel

## Application Overview

Axi exposes a restart action on existing agents. Restart preserves the agent's Discord channel (does not create a new one) and relaunches the underlying Claude Code process so the agent is responsive again. This scenario verifies that restart reuses the same channel and the agent is usable after restart.

## Scenarios

### 1. Spawn, restart, and verify responsive

**Steps:**
1. In `#axi-master`, send: `Spawn an agent named "smoke-restart-<timestamp>" with cwd "<agent_cwd>" and prompt "Say exactly: FIRST_BOOT"`.
2. Wait for the master to confirm the spawn (up to 180 seconds) and locate the new agent channel.
3. Wait for the agent's first message in its channel (up to 120 seconds).
4. In `#axi-master`, send: `Restart the agent named "smoke-restart-<timestamp>"` and wait (up to 90 seconds) for the master to drop its sentinel.
5. In the agent's channel, send: `Say exactly: POST_RESTART_OK` and wait (up to 120 seconds) for the agent to respond.

**Expected Results:**
- Step 2 + 5: The channel ID from step 2 equals the channel ID used in step 5 (restart preserves the channel).
- Step 5: The agent's reply contains `POST_RESTART_OK`.
- Cleanup: The agent is killed in a `finally` block.
