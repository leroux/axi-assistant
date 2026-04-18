<!-- test: tests/test_agent_spawn_and_smoke_generated.py -->

# Axi Agent Spawn and Smoke

## Application Overview

The master agent accepts a natural-language "spawn an agent" request, allocates a dedicated channel in the guild, and boots a fresh Claude Code agent session scoped to that channel. The spawned agent's startup prompt runs once and its output is streamed back to the channel. This scenario exercises the full spawn path end-to-end and the basic agent output path.

## Scenarios

### 1. Master spawns a flowcoder that echoes a sentinel

**Steps:**
1. In `#axi-master`, send: `Spawn an agent named "smoke-spawn-<timestamp>" with cwd "<agent_cwd>" and prompt "Say exactly: SPAWN_SMOKE_OK"`.
2. Wait (up to 180 seconds) for the master to confirm the spawn and drop its "awaiting input" sentinel.
3. Poll the guild channel list (up to 60 seconds) for a channel whose name starts with `smoke-spawn-<timestamp>`.
4. In the spawned agent's channel, poll history (up to 120 seconds) for messages from the agent.

**Expected Results:**
- Step 2: The master confirms the spawn (response mentions the agent name or a spawn verb like "spawned", "created").
- Step 3: A channel starting with `smoke-spawn-<timestamp>` is visible within 60 seconds.
- Step 4: The spawned agent's output contains the sentinel `SPAWN_SMOKE_OK`.
- Cleanup: Any agent created by this scenario is killed in a `finally` block.
