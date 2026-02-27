# Axi Assistant — Development Context

This file is appended to the system prompt for agents working on the axi-assistant codebase.

## Architecture

- **Axi Prime**: Main bot at %(bot_dir)s (branch `main`, service `axi-bot.service`)
- **Disposable test instances**: Managed by `axi_test.py` CLI, git worktrees in `/home/ubuntu/axi-tests/<name>/`
- Each test instance has its own worktree, `.env`, venv, data dir, and systemd service (`axi-test@<name>`)
- Config at `~/.config/axi/test-config.json` (bots, guilds, defaults)
- See [test-system.md](test-system.md) for details

## Key Files

- `bot.py` — Main bot code (all instances run same code, behavior differs via env vars)
- `supervisor.py` — Process supervisor (manages bot.py lifecycle)
- `axi_test.py` — CLI for test instances (up/down/restart/list/msg/logs)
- `axi-test@.service` — Systemd template unit for test instances
- `SOUL.md` — Shared personality prompt for all agents (loaded at startup)
- `CODE-PHILOSOPHY.md` — Software development philosophy, principles, and design guidelines for this codebase (data-oriented, explicit, performance-aware)
- `dev_context.md` — This file; axi dev context appended for agents working on the codebase
- `.env` — Instance-specific config (gitignored)
- `schedules.json` — Scheduled events config

## Important Patterns

- `BOT_WORKTREES_DIR` (hardcoded `/home/ubuntu/axi-tests`) gates Discord MCP tools and worktree write access
- Permission callback: agents rooted in BOT_DIR or worktrees get write access to worktrees dir
- Bot message filter: own messages always ignored, other bots allowed if in ALLOWED_USER_IDS
- `httpx.AsyncClient` used for Discord REST API (MCP tools), not discord.py
- Agents use lazy wake/sleep pattern — sleeping agents have `client=None`
- `msg` command sends as Prime's bot (reads token from main repo `.env`)

## Test Instance Safety

- **NEVER tear down or stop a test instance created by others without explicit user approval.** Instances may be in active use by other agents or the user. Always ask first.
- When all bot tokens are in use, use `axi_test.py up <name> --wait` to reserve a slot and wait (polls every 10s, times out after 2 hours). **Do not** automatically tear down an existing instance to free a slot.
- If `--wait` times out, ask the user how to proceed.
- **Always tear down your own test instances** after you're done with them so the slot is available for other agents.

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

- **`uv run python axi_test.py up <name> [--branch BRANCH] [--guild GUILD] [--wait] [--wait-timeout SECS]`** — Create a test instance. Creates a git worktree, installs deps, starts the service. Use `--wait` to poll until a bot token slot is available (default timeout: 2 hours).
- **`uv run python axi_test.py down <name> [--keep]`** — Stop and remove a test instance. Use `--keep` to preserve the worktree for inspection.
- **`uv run python axi_test.py restart <name>`** — Restart a test instance after code changes.
- **`uv run python axi_test.py list`** — Show all test instances and their status.
- **`uv run python axi_test.py msg <name> "<message>" [--timeout SECS]`** — Send a message to a test instance and wait for its response.
- **`uv run python axi_test.py logs <name>`** — Tail the test instance's journal logs.

### Test Guilds

Test instances run in separate Discord guilds. Your Discord MCP tools (`discord_list_channels`, `discord_read_messages`, `discord_send_message`) work in these guilds using your own bot token.

Available test guilds (from `~/.config/axi/test-config.json`):
- **nova** — Guild ID `1475631458243710977`

### Workflow: Testing a Code Change

Spawn the coding agent with `cwd=%(bot_dir)s` (the main repo, NOT a worktree). The agent is responsible for the full lifecycle: creating the test instance, making changes in the worktree, testing, and tearing it down. You (the parent) should NOT create or manage the test instance — let the agent handle it.

The agent should follow these steps:

1. **Create instance**: `uv run python axi_test.py up <name> --wait` — creates a worktree + starts a test service. `--wait` queues if the slot is busy.
2. **Make changes** in `/home/ubuntu/axi-tests/<name>/`
3. **Restart**: `uv run python axi_test.py restart <name>`
4. **Test via Discord MCP**: Use `discord_send_message` to the test guild, then `discord_read_messages` or `wait_for_message.py` to check the response
5. **Iterate**: Repeat 2-4 until it works. No commits needed during iteration.
6. **Commit when it works**: `cd /home/ubuntu/axi-tests/<name> && git add -A && git commit -m "description"`
7. **Merge to main**: `cd %(bot_dir)s && git merge <branch>`
8. **Tear down**: `uv run python axi_test.py down <name>` — always clean up your own instances
9. **Restart yourself**: Use `axi_restart` to pick up the merged changes (or tell the parent to restart)

### Fast Message Polling

For scripted test interactions, use `wait_for_message.py`:

```bash
# Send a message, then wait for the bot's response
python wait_for_message.py <channel_id> --after <message_id> --timeout 60
```

This polls every 2 seconds and returns as soon as a non-system message appears. Output is JSONL with a trailing cursor line.

### Tips

- Instance names should be short and descriptive: `ping-test`, `schedule-fix`, `auth-refactor`
- Each test instance gets its own `.env`, venv, and data directory — fully isolated
- The test bot token can only run one instance at a time. Always use `--wait` when creating instances so you queue up if the slot is busy. If `--wait` times out, ask the user how to proceed — do **not** tear down someone else's instance
- Test instances use `Restart=on-failure` — they stay stopped when you stop them (unlike your own `Restart=always`)
- Crash handler and rollback are off by default on test instances (no `ENABLE_CRASH_HANDLER` or `ENABLE_ROLLBACK` set)
