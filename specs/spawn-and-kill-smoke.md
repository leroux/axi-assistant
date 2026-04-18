<!-- test: tests/test_spawn_and_kill_smoke_generated.py -->

# Axi Spawn-and-Kill Smoke

## Application Overview

Axi is a Discord bot that spawns disposable Claude Code agents on demand. The master agent (`#axi-master`) accepts natural-language requests to spawn and kill agents, and creates a dedicated channel for each spawned agent. This plan is the minimum liveness check: a user can ask for an agent, the agent appears and produces output, and a kill request removes it.

## Scenarios

### 1. Spawn and kill a throwaway flowcoder

**Steps:**
1. In `#axi-master`, send a natural-language request: "Spawn a flowcoder agent named smoke-probe-<timestamp>."
2. Wait (up to 60 seconds) for a new channel to appear in the guild whose name starts with `smoke-probe-<timestamp>`.
3. Wait (up to 60 seconds) for the spawned agent to post at least one message in its channel.
4. In `#axi-master`, send: "Kill smoke-probe-<timestamp>."
5. Wait (up to 30 seconds) for the master to reply confirming the kill.

**Expected Results:**
- Step 2 succeeds: a channel whose name starts with `smoke-probe-<timestamp>` is visible in the guild within 60 seconds.
- Step 3 succeeds: the spawned agent's first message in that channel is non-empty.
- Step 5 succeeds: the master posts a message in `#axi-master` that mentions either "stopped", "killed", or the agent's name alongside a confirmation verb.
