<!-- test: tests/test_duplicate_live_agent_name_generated.py -->

# Axi Rejects Duplicate Live Agent Name

## Application Overview

Agent names must be unique among live agents. If the user asks the master to spawn an agent with a name that's already in use by a running agent, the master should refuse rather than clobber the first agent's channel or state. This is a basic guard against accidental duplication.

## Scenarios

### 1. Second spawn with the same live name is rejected

**Steps:**
1. In `#axi-master`, spawn an agent named `smoke-dupe-<timestamp>` with cwd `<agent_cwd>` and prompt `Say OK`.
2. Wait (up to 180 seconds) for the master to confirm the first spawn.
3. In `#axi-master`, send a second spawn request for the SAME name: `Spawn an agent named "smoke-dupe-<timestamp>" with cwd "<agent_cwd>" and prompt "Say OK again"` and wait (up to 60 seconds) for the master's reply.

**Expected Results:**
- Step 3: The master's reply indicates the spawn failed because the name is taken — the reply contains one of: `already`, `exists`, `duplicate`, or `in use` (case-insensitive).
- Cleanup: The original agent from step 1 is killed in a `finally` block.
