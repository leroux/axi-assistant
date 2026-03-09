# Testing Flowcoder Integration

## Overview

Flowcoder agents are a superset of Claude Code agents. They support normal conversation, session persistence, sleep/wake, and can additionally run flowcharts via `//flowchart`. This document covers how to test the integration end-to-end.

## Prerequisites

- A test instance with `FLOWCODER_ENABLED=true` in `.env`
- The `flowcoder-engine` binary available in the test instance's venv (installed via `pip install flowcoder`)
- `CLAUDE_MODEL` set in `.env` (use `claude-haiku-4-5-20251001` for cheap testing)
- Test guild configured in `~/.config/axi/test-config.json`

## Test Instance Setup

```bash
# From the axi-assistant repo (or a worktree)
cd /home/ubuntu/axi-tests/<worktree>

# Create and start a test instance
PYTHONPATH=. .venv/bin/python ../axi_test.py up <name> --wait

# Add flowcoder config to the test instance .env
echo 'FLOWCODER_ENABLED=true' >> /home/ubuntu/axi-tests/<name>/.env
echo 'CLAUDE_MODEL=claude-haiku-4-5-20251001' >> /home/ubuntu/axi-tests/<name>/.env

# Restart to pick up .env changes
PYTHONPATH=. .venv/bin/python ../axi_test.py restart <name>
```

## Sending Messages

### Via test_discord.py helper

The `test_discord.py` script (in the worktree root) sends a message to a channel in the test guild and waits for a response. It uses the sender bot token from test config.

```bash
# Send a message and print responses (waits for "awaiting input" sentinel)
.venv/bin/python test_discord.py <channel-name> '<message>'

# Examples:
.venv/bin/python test_discord.py axi-master '/spawn myagent --type flowcoder'
.venv/bin/python test_discord.py myagent 'What is 2+2?'
.venv/bin/python test_discord.py myagent '//flowchart story A brave knight'
```

### Via Python for scripted tests

```python
import sys
sys.path.insert(0, '<worktree-path>')
from test_discord import get_client, find_channel, send_and_wait

with get_client() as client:
    ch_id = find_channel(client, 'myagent')
    responses = send_and_wait(client, ch_id, 'Hello', timeout=60)
```

## Test Matrix

### 1. Spawn a flowcoder agent (no command)

Flowcoder agents are spawned like normal agents with `--type flowcoder`. No command is required.

```
/spawn fc-test --type flowcoder
```

**Expected**: Agent spawns, shows "Spawning **flowcoder** agent..." message, enters sleeping state.

### 2. Basic conversation

Send a simple message to the flowcoder agent.

```
What is 3 + 7?
```

**Expected**: Agent wakes up, responds with "10", behaves like a normal Claude Code agent.

### 3. Secret retention (session persistence)

```
Remember the secret code OMEGA-3344-BRAVO. Just acknowledge it.
```

Then sleep/wake the agent (send another message after idle timeout, or restart the bot).

```
What was the secret code I told you?
```

**Expected**: Agent remembers OMEGA-3344-BRAVO across conversation turns and sleep/wake cycles.

### 4. Inline flowchart

```
//flowchart story A brave cat named Whiskers
```

**Expected**:
- System message: "Running flowchart: `story A brave cat named Whiskers`"
- Block headers appear (e.g., "Write Draft", "Critique", "Refine")
- Content streams between blocks
- Completion message: "Flowchart **completed** in Xs | Cost: $X.XX | Blocks: N"
- Typical: 5 blocks, ~40s, ~$0.15-0.20 with haiku

### 5. Secrets survive flowcharts

After running a flowchart in a session that has a secret:

```
What was the secret code I told you earlier?
```

**Expected**: Agent still remembers the secret. Flowcharts run in a separate engine process and don't affect the Claude session.

### 6. Message queuing during flowchart

While a flowchart is running (within the first few seconds):

```
What is the meaning of life?
```

**Expected**:
- System message: "Agent **fc-test** is busy -- message queued (position 1)."
- After flowchart completes: "Processing queued message: ..."
- Agent answers the queued question

### 7. Claude Code agent restriction

Spawn a normal Claude Code agent and try a flowchart:

```
# In a claude_code agent channel:
//flowchart story test
```

**Expected**: "Flowcharts are only available for **flowcoder** agents."

### 8. Agent type persistence across restarts

After spawning a flowcoder agent and interacting with it:

```bash
# Restart the bot
PYTHONPATH=. .venv/bin/python ../axi_test.py restart <name>
```

Then check logs:

```bash
journalctl --user -u axi-test@<name> --no-pager -n 30 | grep fc-test
```

**Expected**: Log shows `type=flowcoder` in the reconstruction line. The channel topic should contain `type: flowcoder`.

After restart, sending `//flowchart story test` should still work (not rejected as claude_code).

### 9. Rapid-fire messages

Send 3+ messages in quick succession (< 1 second apart):

```
What is 10*10?
What is 20+30?
What is 100/4?
```

**Expected**: All messages are processed sequentially, each gets a correct response.

### 10. Available flowchart commands

The engine supports these commands (from `packages/flowcoder_engine/examples/commands/`):

- `story` — Write a story (draft, critique, refine)
- `explain` — Explain a topic (simple, medium, expert, synthesize)
- `recast` — Recast/transform text

Note: `chat` is NOT a valid command and will fail.

## Checking Logs

```bash
# Tail test instance logs
journalctl --user -u axi-test@<name> --no-pager -n 50

# Filter for flowcoder-specific lines
journalctl --user -u axi-test@<name> --no-pager -n 100 | grep -i flowcoder

# Check channel topics via Discord API
.venv/bin/python -c "
import json, httpx, os
with open(os.path.expanduser('~/.config/axi/test-config.json')) as f:
    cfg = json.load(f)
TOKEN = cfg['defaults']['sender_token']
GUILD = '1475631458243710977'
client = httpx.Client(base_url='https://discord.com/api/v10', headers={'Authorization': f'Bot {TOKEN}'}, timeout=10)
for c in client.get(f'/guilds/{GUILD}/channels').json():
    if c['name'] == 'fc-test':
        print(c.get('topic', 'NO TOPIC'))
"
```

## Teardown

Always tear down test instances when done:

```bash
PYTHONPATH=. .venv/bin/python ../axi_test.py down <name>
```

## Key Architecture Notes

- Flowcoder agents use the same `AgentSession` and Claude SDK session as Claude Code agents
- `agent_type` is stored in the channel topic (`type: flowcoder`) and reconstructed on restart
- `//flowchart` is intercepted by `_handle_text_command()` in `main.py` before reaching the Claude session
- The flowchart engine runs as a `ManagedFlowcoderProcess` (procmux-backed subprocess)
- `process_message()` in `agents.py` checks `flowcoder_process.is_running` to decide whether to forward to the engine or the Claude session
- The engine is spawned per-flowchart and killed after completion — it does NOT persist between flowcharts
