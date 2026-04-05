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
- `handlers.py` — Agent handler (lifecycle, message routing through /soul flowchart)
- `axi_test.py` — CLI for test instances (up/down/restart/list/merge/msg/logs)
- `axi-test@.service` — Systemd template unit for test instances
- `prompts/SOUL.md` — Shared personality prompt for all agents (identity, style, constraints)
- `prompts/axi_codebase_context.md` — This file; context for agents working on the axi codebase
- `commands/soul.json` — Core /soul flowchart (message classification, task lifecycle, hook dispatch)
- `extensions/` — Modular extensions: prompt.md (system prompt), commands/ (flowchart hooks), and/or prompt_hooks (in-prompt text injection)
- `.env` — Instance-specific config (gitignored)
- `schedules.json` — Scheduled events config

## Development Philosophy

Read `/home/ubuntu/axi-user-data/CODE-PHILOSOPHY.md` for the principles guiding this codebase: data-oriented design, mechanical sympathy (hardware awareness), explicit over convention, performance-aware, pragmatic functional programming, clear data flow, and no over-abstraction. This philosophy should inform all architectural decisions.

## Core vs Extension Boundary

Extension-specific concepts, tools, CLIs, and record IDs must never appear in core files. Keep core wording generic and let extensions provide the specific instructions.

Core files: SOUL.md, soul.json, axi_codebase_context.md, bot.py, handlers.py, supervisor.py, prompts.py


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
- **Always tear down your own test instances** after you're done with them so the slot is available for other agents. Teardown procedure:
  1. Run `axi_test.py list` to identify which instances you created this session.
  2. For each instance you created, run `axi_test.py down <name>`.
  3. Run `axi_test.py list` again to verify the instances are gone.

## Test Instance Management

For full test instance documentation (CLI commands, workflow, tips), read `%(bot_dir)s/prompts/refs/test-instances.md`.
Do not assume the workflow — the test system has specific safety rules and conventions.
