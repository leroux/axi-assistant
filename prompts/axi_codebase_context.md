# Axi Assistant — Development Context

This file is appended to the system prompt for agents working on the axi-assistant codebase.

## Architecture

- **Axi Prime**: Main bot at %(bot_dir)s (branch `main`, service `axi-bot.service`)
- **Disposable test instances**: Managed by `axi_test.py` CLI, git worktrees in `~/axi-tests/<name>/`
- Each test instance has its own worktree, `.env`, venv, data dir, and systemd service (`axi-test@<name>`)
- Config at `~/.config/axi/test-config.json` (bots, guilds, defaults)
- See [test-system.md](test-system.md) for details
- **Stress testing** — `prompts/refs/stress-testing.md` — read when asked to stress test, verify test infra, or after test infra changes

## Key Files

- `axi/main.py` — Discord bot setup, slash commands, event loop
- `axi/supervisor.py` — Process supervisor (manages bot lifecycle)
- `axi/agents.py` — Agent management (spawn, wake, sleep, permissions)
- `axi/handlers.py` — Agent handler (lifecycle, message routing through /soul flowchart)
- `axi_test.py` — CLI for test instances (up/down/restart/list/merge/msg/logs)
- `axi-test@.service` — Systemd template unit for test instances
- `prompts/SOUL.md` — Shared personality prompt for all agents (identity, style, constraints)
- `prompts/axi_codebase_context.md` — This file; context for agents working on the axi codebase
- `commands/soul.json` — Core /soul flowchart (message classification, task lifecycle, hook dispatch)
- `extensions/` — Modular extensions: prompt.md (system prompt), commands/ (flowchart hooks), and/or prompt_hooks (in-prompt text injection)
- `.env` — Instance-specific config (gitignored)
- `schedules.json` — Scheduled events config (in `AXI_USER_DATA`, not the repo)

## Development Philosophy

Read `~/app-user-data/axi-assistant/CODE-PHILOSOPHY.md` for the principles guiding this codebase: data-oriented design, mechanical sympathy (hardware awareness), explicit over convention, performance-aware, pragmatic functional programming, clear data flow, and no over-abstraction. This philosophy should inform all architectural decisions.

## Core vs Extension vs User Boundary

Three layers, each with strict boundaries:
- **Core files** — generic, instance-independent logic and structure. No extension-specific concepts, tools, CLIs, or record IDs. No user/instance-specific data.
- **Extension files** — feature-specific concepts, tools, CLIs, record IDs. Keep extensions self-contained; don't leak extension concepts into core.
- **User data** — instance-specific values (guild IDs, server names, tokens, user IDs, machine names, OS types, deployment details) belong in profile ref files or runtime template variables, never hardcoded in core or extension files.

Core files: SOUL.md, soul.json, axi_codebase_context.md, axi/main.py, axi/handlers.py, axi/supervisor.py, axi/prompts.py


## Important Patterns

- `BOT_WORKTREES_DIR` (default `~/axi-tests`, configurable via `AXI_WORKTREES_DIR`) gates Discord MCP tools and worktree write access
- Permission callback: agents rooted in BOT_DIR or worktrees get write access to worktrees dir
- Bot message filter: own messages always ignored, other bots allowed if in ALLOWED_USER_IDS
- `httpx.AsyncClient` used for Discord REST API (MCP tools), not discord.py
- Agents use lazy wake/sleep pattern — sleeping agents have `client=None`
- `msg` command sends as Prime's bot (reads token from main repo `.env`)

## Test Instance Safety

- **NEVER tear down or stop a test instance created by others without explicit user approval.** Instances may be in active use by other agents or the user. Always ask first.
- When all bot tokens are in use, use `axi_test.py up <name> --wait` to reserve a slot and wait (polls every 10s, times out after 2 hours). **Do not** automatically tear down an existing instance to free a slot.
- If `--wait` times out, ask the user how to proceed.
- **Always tear down your own test instances** after you're done with them so the slot is available for other agents. Teardown procedure:
  1. Run `axi_test.py list` to identify which instances you created this session.
  2. For each instance you created, run `axi_test.py down <name>`.
  3. Run `axi_test.py list` again to verify the instances are gone.
- **NEVER restart, stop, or signal the production Axi bot process.** You must only restart your own test instances via `axi_test.py restart <name>`. Do not use `systemctl restart axi-bot`, `kill`, or any other method to restart the main bot. Only the user can restart the production bot (`systemctl --user restart axi-bot`). If your task requires a production restart, ask the user to do it.

## Self-Modification Workflow

You have access to a disposable test instance system. Use it to test code changes before applying them to your own running code.

### Rule: Never Edit Your Own Running Code

You must NEVER directly modify the code you are currently running (`%(bot_dir)s/axi/main.py`, etc.). Instead:
1. Create a test instance
2. Spawn an agent in the test worktree to make changes
3. Test the changes via Discord MCP tools
4. When verified, commit in the worktree, merge to main, and restart yourself

Humans using Claude Code on the server can edit your code directly (the supervisor has auto-rollback), but you cannot — a bad edit could crash you mid-operation.

### CLI Commands

Run these via Bash:

- **`uv run python axi_test.py up <name> [--guild GUILD] [--wait] [--wait-timeout SECS]`** — Reserve a bot/guild slot for a test instance. Writes `.env` and creates the data directory. Use `--wait` to poll until a bot token slot is available (default timeout: 2 hours).
- **`uv run python axi_test.py down <name>`** — Release a bot/guild reservation.
- **`uv run python axi_test.py restart <name>`** — Restart a test instance after code changes.
- **`uv run python axi_test.py list`** — Show all test instances and their status.
- **`uv run python axi_test.py merge [-m MSG] [--timeout SECS]`** — Squash-merge current branch into main via merge queue. Waits for queue turn, verifies fast-forward, squashes all commits into one. If main moved ahead, exits with code 1 (rebase and resubmit). No-op if already in main.
- **`uv run python axi_test.py queue [show|drop] [--all]`** — Show merge queue status, or drop entries. `drop` removes your branch; `drop --all` clears the queue.
- **`uv run python axi_test.py msg <name> "<message>" [--timeout SECS]`** — Send a message to a test instance and wait for its response.
- **`uv run python axi_test.py logs <name>`** — Tail the test instance's journal logs.

### Test Guilds

Test instances run in separate Discord guilds. Your Discord MCP tools (`discord_list_channels`, `discord_read_messages`, `discord_send_message`) work in these guilds using your own bot token.

Available test guilds are configured in `~/.config/axi/test-config.json`. Run `cat ~/.config/axi/test-config.json` to see guild IDs.

### Workflow: Testing a Code Change

The parent (Axi master) prepares the working directory, then spawns an agent in it. The agent codes, tests, and ships — it never needs to reference the main repo directly.

**Parent responsibilities:**
1. Spawn the coding agent with `cwd` set to the bot's working directory (or a specific repo)
2. Auto-worktree handles isolation: if another agent already uses the same git-repo cwd, a worktree is created automatically under `BOT_WORKTREES_DIR`
3. On agent kill, the worktree is auto-merged (squash) into main and cleaned up. Conflicts are reported to the agent's channel.

**Note:** Manual worktree creation is no longer required. The `no_worktree` parameter on `axi_spawn_agent` can opt out for read-only agents.

**Agent workflow:**
1. **Edit files** in cwd (all edits naturally go to the right place)
2. **Reserve a test slot**: `uv run python axi_test.py up <name> --wait` — only when ready to test
3. **Restart**: `uv run python axi_test.py restart <name>`
4. **Test via Discord MCP**: Use `discord_send_message` to the test guild, then `discord_wait_for_message` to wait for the bot's response
5. **Iterate**: Repeat 1-4 until it works
6. **Tear down**: `uv run python axi_test.py down <name>` — always release the slot when done testing
7. **Commit**: `git add -A && git commit -m "description"`
8. **Merge to main**: `uv run python axi_test.py merge` — submits to merge queue, waits for turn, squash-merges into main. If it exits with code 1 ("main has moved ahead"), run `git rebase main` and retry the merge.
9. **Restart**: Tell the parent to restart so it picks up the merged changes (spawned agents do NOT have `axi_restart` — only the master can restart itself)

### Reproduce Before Declaring Root Cause

When debugging axi bugs, reproduce the bug on a test instance before declaring a root cause. Reading code and theorizing is useful for forming hypotheses, but a reproduction on the test server is the strongest evidence -- it proves the bug exists and proves the fix works. This connects to the SOUL.md evidence principle: for axi bugs, the best evidence is a reproduction on the test server.

### Fast Message Polling

For scripted test interactions, use the `discord_wait_for_message` MCP tool:
- `channel_id` — the channel to watch
- `after` — message ID to wait after (optional, defaults to latest message)
- `timeout` — max seconds to wait (default 120, max 300)

Polls every 2 seconds and returns as soon as a non-system message appears. Returns the message content and a cursor ID for chaining.

### Tips

- Instance names should be short and descriptive: `ping-test`, `schedule-fix`, `auth-refactor`
- Each test instance gets its own `.env`, venv, and data directory — fully isolated
- The test bot token can only run one instance at a time. Always use `--wait` when creating instances so you queue up if the slot is busy. If `--wait` times out, ask the user how to proceed — do **not** tear down someone else's instance
- Test instances use `Restart=on-failure` — they stay stopped when you stop them (unlike your own `Restart=always`)
- Crash handler and rollback are off by default on test instances (no `ENABLE_CRASH_HANDLER` or `ENABLE_ROLLBACK` set)
