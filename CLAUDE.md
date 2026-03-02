# Development

## Setup

After cloning or creating a worktree, install git hooks:

    uv run pre-commit install

## Linting

Ruff runs automatically on commit via pre-commit hooks. To run manually:

    uv run ruff check --force-exclude .

Custom project-specific lint rules (LibCST + Fixit):

    uv run fixit lint .
    uv run fixit test .lint

## Type Checking

This project uses Pyright in strict mode. All Python files must pass `uv run pyright` with zero errors.

When editing a function, ensure it has complete type annotations (parameters and return type).
Do not annotate functions you didn't change.

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
- `axi_test.py` — CLI for test instances (up/down/restart/list/merge/msg/logs)
- `axi-test@.service` — Systemd template unit for test instances
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


# Code Philosophy

This document captures the software development philosophy, principles, and mental models that guide decisions in this codebase.

## Core Influences

- **Data-Oriented Design** (Mike Acton) — Understanding the problem domain first, structure data around actual usage patterns
- **Mechanical Sympathy** (Martin Thompson) — Code that understands hardware constraints: cache lines, memory layout, CPU behavior
- **Handmade Philosophy** (Casey Muratori) — Deep understanding of systems, explicit implementation, avoiding unnecessary abstractions
- **Linear/Algorithmic Thinking** — Preference for straightforward, predictable algorithms over complex patterns
- **Functional Programming Ideas** (pragmatic, not dogmatic) — Use FP concepts when they serve the problem

## What We Value

- **Explicitness over convention** — Code should be clear about what it does, not hidden behind abstractions
- **Performance awareness** — Not premature optimization, but understanding the cost of your choices
- **Simplicity as a first principle** — Small functions are fine *when needed*, not as a default pattern
- **Clear data flow** — How data moves through the system should be obvious
- **Pragmatism over purity** — Use FP concepts (immutability, composition, pure functions) when they serve the problem, not as dogma

## What We Reject

- **OO's hidden state** — Polymorphism, inheritance hierarchies that obscure what's actually happening
- **Over-abstraction** — Ten layers of indirection to avoid repeating three lines of code
- **Function proliferation** — Small functions for their own sake; functions should earn their existence
- **Convention-driven design** — "We do it this way because that's the pattern" without understanding why

## Our Approach

### Understand the Data First
What does it look like? How is it accessed? What are the hot paths? Data structure informs everything else.

### Organize by Concern/Domain
Not by OO classes, but by what the code actually *does*. Vertical slicing preferred over layer-based organization.

### Make Tradeoffs Visible
If you're trading memory for speed, or clarity for performance, *say so*. Explicit choices are better than hidden ones.

### Linear, Readable Code
Prefer following a straightforward path over clever abstractions. Code should tell a story.

### Composability Without Ceremony
Functions and modules work together, but without unnecessary boilerplate or design pattern overhead.

## Related Principles

- **Locality of Reference** — Related code and data should be near each other (physically and conceptually)
- **Testability Through Simplicity** — If code is hard to test, the design might be wrong
- **Explicit Error Handling** — Not exceptions hiding control flow
- **YAGNI (You Aren't Gonna Need It)** — Don't build for hypothetical futures
- **Vertical Slicing** — Features organized end-to-end, not by architectural layer

## Guidelines for This Codebase

When making design decisions, ask:

1. **Do I understand the problem domain?** (Mike Acton's first principle)
2. **Is this choice visible and explicit?** (Not hiding complexity behind abstraction)
3. **Does this serve the actual problem?** (Not pattern-driven)
4. **Am I aware of the hardware/performance implications?** (Mechanical sympathy)
5. **Could I explain this to someone without jargon?** (Simplicity test)

If a design choice requires defending "because that's the pattern," reconsider it.
