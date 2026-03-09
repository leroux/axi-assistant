# Axi Assistant — Development Context

This file is appended to the system prompt for agents working on the axi-assistant codebase.

## Architecture

- **Axi Prime**: Main bot at %(bot_dir)s (branch `main`, service `axi-bot.service`)
- **Disposable test instances**: Managed by `axi_test.py` CLI, git worktrees in `/home/ubuntu/axi-tests/<name>/`
- Each test instance has its own worktree, `.env`, venv, data dir, and systemd service(s)
- **Python mode** (default): 1 service — `axi-test@<name>`
- **Rust mode** (`--rs`): 2 services — `axi-test-procmux@<name>` (process mux) + `axi-test-bot@<name>` (bot, depends on procmux)
- Mode is stored in `slots.json` per instance; `down`/`restart`/`logs`/`clean` read it automatically
- Config at `~/.config/axi/test-config.json` (bots, guilds, defaults)
- See [test-system.md](test-system.md) for details

## Key Files

- `bot.py` — Main bot code (all instances run same code, behavior differs via env vars)
- `supervisor.py` — Process supervisor (manages bot.py lifecycle)
- `axi_test.py` — CLI for test instances (top-level; up/down/restart/list/merge/msg/logs)
- `axi-test@.service` — Systemd template unit for Python test instances
- `axi-rs/systemd/axi-test-bot@.service` — Systemd template unit for Rust test bot
- `axi-rs/systemd/axi-test-procmux@.service` — Systemd template unit for Rust test procmux
- `SOUL.md` — Shared personality prompt for all agents (loaded at startup)
- `dev_context.md` — This file; axi dev context appended for agents working on the codebase
- `.env` — Instance-specific config (gitignored)
- `schedules.json` — Scheduled events config

## Development Philosophy

Read `/home/ubuntu/axi-user-data/CODE-PHILOSOPHY.md` for the principles guiding this codebase: data-oriented design, mechanical sympathy (hardware awareness), explicit over convention, performance-aware, pragmatic functional programming, clear data flow, and no over-abstraction. This philosophy should inform all architectural decisions.

## Important Patterns

- `BOT_WORKTREES_DIR` (hardcoded `/home/ubuntu/axi-tests`) gates Discord MCP tools and worktree write access
- Permission callback: agents rooted in BOT_DIR or worktrees get write access to worktrees dir
- Bot message filter: own messages always ignored, other bots allowed if in ALLOWED_USER_IDS
- `httpx.AsyncClient` used for Discord REST API (MCP tools), not discord.py
- Agents use lazy wake/sleep pattern — sleeping agents have `client=None`
- `msg` command sends as Prime's bot (reads token from main repo `.env`)
