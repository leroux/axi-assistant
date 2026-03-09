# Axi Self-Modification Workflow

## Test Instance Safety

- **NEVER tear down or stop a test instance created by others without explicit user approval.** Instances may be in active use by other agents or the user. Always ask first.
- When all bot tokens are in use, use `axi_test.py up <name> --wait` (from the repo root) to reserve a slot and wait (polls every 10s, times out after 2 hours). **Do not** automatically tear down an existing instance to free a slot.
- If `--wait` times out, ask the user how to proceed.
- **Always tear down your own test instances** after you're done with them so the slot is available for other agents.
- **NEVER restart, stop, or signal the production Axi bot process.** You must only restart your own test instances via `axi_test.py restart <name>`. Do not use `systemctl restart axi-bot`, `kill`, or any other method to restart the main bot. Only the master agent (axi-master) can restart itself via `axi_restart`. If your task requires a production restart, ask the user to do it.

## Test Instance Management

You have access to a disposable test instance system. Use it to test code changes before applying them to your own running code.

### Rule: Never Edit Your Own Running Code

You must NEVER directly modify the code you are currently running (`%(bot_dir)s/bot.py`, etc.). Instead:
1. Create a test instance
2. Spawn an agent in the test worktree to make changes
3. Test the changes via Discord MCP tools
4. When verified, commit in the worktree, merge to main, and restart yourself

Humans using Claude Code on the server can edit your code directly (the supervisor has auto-rollback), but you cannot — a bad edit could crash you mid-operation.

### CLI Commands

Run these via Bash:

- **`uv run --directory axi-py python ../axi_test.py up <name> [--guild GUILD] [--wait] [--wait-timeout SECS]`** — Reserve a bot/guild slot for a test instance. Writes `.env` and creates the data directory. Use `--wait` to poll until a bot token slot is available (default timeout: 2 hours).
- **`uv run --directory axi-py python ../axi_test.py down <name>`** — Release a bot/guild reservation.
- **`uv run --directory axi-py python ../axi_test.py restart <name>`** — Restart a test instance after code changes.
- **`uv run --directory axi-py python ../axi_test.py list`** — Show all test instances and their status.
- **`uv run --directory axi-py python ../axi_test.py merge [-m MSG] [--timeout SECS]`** — Squash-merge current branch into main via merge queue. Waits for queue turn, verifies fast-forward, squashes all commits into one. If main moved ahead, exits with code 1 (rebase and resubmit). No-op if already in main.
- **`uv run --directory axi-py python ../axi_test.py queue [show|drop] [--all]`** — Show merge queue status, or drop entries. `drop` removes your branch; `drop --all` clears the queue.
- **`uv run --directory axi-py python ../axi_test.py msg <name> "<message>" [--timeout SECS]`** — Send a message to a test instance and wait for its response.
- **`uv run --directory axi-py python ../axi_test.py logs <name>`** — Tail the test instance's journal logs.

### Test Guilds

Test instances run in separate Discord guilds. Your Discord MCP tools (`discord_list_channels`, `discord_read_messages`, `discord_send_message`) work in these guilds using your own bot token.

Available test guilds (from `~/.config/axi/test-config.json`):
- **nova** — Guild ID `1475631458243710977`

### Workflow: Testing a Code Change

The parent (Axi master) prepares the working directory, then spawns an agent in it. The agent codes, tests, and ships — it never needs to reference the main repo directly.

**Parent responsibilities:**
1. Create a git worktree: `git -C %(bot_dir)s worktree add /home/ubuntu/axi-tests/<name> -b feature/<name>`
   (or reuse an existing worktree)
2. Spawn the coding agent with `cwd` set to the worktree directory

**IMPORTANT — worktree rule for code agents:**
Any agent that MIGHT edit files in the axi-assistant codebase MUST be spawned in a worktree, never directly in `%(bot_dir)s` (the live main repo). The running bot reads from main — edits there affect the live system immediately and bypass testing. When in doubt, use a worktree. Reading from a worktree costs nothing.
Exceptions: pure research agents, design-only agents, agents working on external repos, or when the user explicitly requests a specific cwd.

**Agent workflow:**
1. **Edit files** in cwd (all edits naturally go to the right place)
2. **Reserve a test slot**: `uv run --directory axi-py python ../axi_test.py up <name> --wait` — only when ready to test
3. **Restart**: `uv run --directory axi-py python ../axi_test.py restart <name>`
4. **Test via Discord MCP**: Use `discord_send_message` to the test guild, then `discord_read_messages` or `python -m discordquery wait` to check the response
5. **Iterate**: Repeat 1-4 until it works
6. **Tear down**: `uv run --directory axi-py python ../axi_test.py down <name>` — always release the slot when done testing
7. **Commit**: `git add -A && git commit -m "description"`
8. **Merge to main**: `uv run --directory axi-py python ../axi_test.py merge` — submits to merge queue, waits for turn, squash-merges into main. If it exits with code 1 ("main has moved ahead"), run `git rebase main` and retry the merge.
9. **Restart**: Tell the parent to restart so it picks up the merged changes (spawned agents do NOT have `axi_restart` — only the master can restart itself)

### Fast Message Polling

For scripted test interactions, use `python -m discordquery wait`:

```bash
# Send a message, then wait for the bot's response
python -m discordquery wait <channel_id> --after <message_id> --timeout 60
```

This polls every 2 seconds and returns as soon as a non-system message appears. Output is JSONL with a trailing cursor line.

### Tips

- Instance names should be short and descriptive: `ping-test`, `schedule-fix`, `auth-refactor`
- Each test instance gets its own `.env`, venv, and data directory — fully isolated
- The test bot token can only run one instance at a time. Always use `--wait` when creating instances so you queue up if the slot is busy. If `--wait` times out, ask the user how to proceed — do **not** tear down someone else's instance
- Test instances use `Restart=on-failure` — they stay stopped when you stop them (unlike your own `Restart=always`)
- Crash handler and rollback are off by default on test instances (no `ENABLE_CRASH_HANDLER` or `ENABLE_ROLLBACK` set)
