# RFC-0012: Configuration & Model Selection

**Status:** Draft
**Created:** 2026-03-09

## Problem

Configuration loading, model selection, prompt assembly, MCP server wiring, and feature flags are scattered across both codebases with subtle divergences. axi-py has a rich prompt layering system (SOUL + dev_context + packs + CWD prompts) and per-agent config persistence that axi-rs does not replicate. axi-rs has Discord token slot resolution and a test config constructor that axi-py handles differently. Without a unified spec, new agents may get wrong prompts, stale models, or missing MCP servers.

## Behavior

### Model Selection

1. **Read**: Check `AXI_MODEL` environment variable. If set and non-empty, use it. Otherwise read `model` from `config.json` in the user data directory. If absent or unparseable, default to `"opus"`.
2. **Write**: `set_model(name)` validates `name` against `VALID_MODELS` = {`haiku`, `sonnet`, `opus`}. Invalid names MUST be rejected before any file I/O. The entire load-mutate-save cycle on `config.json` MUST be serialized under a config lock to prevent TOCTOU races.
3. **Application**: `get_model()` is called at agent wake time so each session picks up the current model without requiring restart.
4. **Warning**: When the active model is not `opus`, a warning message MUST be posted to Discord.
5. **Test override**: Test instances set `AXI_MODEL=haiku` in their `.env`, which takes precedence over `config.json`.

### Discord Token Resolution

1. Check `DISCORD_TOKEN` environment variable.
2. If not set, look up the instance name in a test-slots file (`.test-slots.json`) to resolve a token from `test-config.json`.
3. Failure to resolve a token is a fatal startup error.

### MCP Server Loading

1. Read named server configurations from `mcp_servers.json` in the user data directory.
2. Unknown server names MUST be logged as warnings and skipped, not cause failures.
3. Agents receive MCP servers based on their role (see prompt assembly below).

### Prompt Assembly

System prompts are built in layers. The assembly differs by agent role:

**Admin/dev agents** (master agent and agents with admin packs):
1. SOUL.md (shared personality)
2. dev_context.md (development context)
3. Pack prompts (loaded from `packs/<name>/prompt.md`)
4. CWD-local `SYSTEM_PROMPT.md` (if present in agent's working directory)

**Non-admin spawned agents**:
1. Mini agent context prompt (reduced instruction set)
2. CWD-local `SYSTEM_PROMPT.md` (if present)

**CWD prompt modes**:
- Default: append to base prompt.
- `<!-- mode: overwrite -->` marker in `SYSTEM_PROMPT.md`: replace the entire base prompt.

**Pack loading**:
- Packs are loaded once at import/startup from `packs/<name>/prompt.md`.
- `axi_spawn_agent` accepts optional `packs` parameter: `None` = use defaults, `[]` = disable packs entirely.
- Unknown pack names are skipped with a warning.

**Prompt hash**: A 16-character SHA-256 prefix of the system prompt text, stored in the channel topic. Used to detect prompt drift between spawn and resume.

### Per-Agent Config Persistence

Per-agent config (pack names and MCP server names) MUST be persisted to `<user_data>/agents/<name>/agent_config.json` at spawn time. This config is reloaded during:
- `restart_agent` (rebuilds prompt from saved packs)
- Reconstruction at startup (restores packs and MCP servers)

### Allowed CWDs

The set of allowed working directories is assembled from:
1. `ALLOWED_CWDS` environment variable (colon-separated paths)
2. Bot directory (`BOT_DIR`)
3. User data directory (`AXI_USER_DATA`)
4. Worktrees directory (`BOT_WORKTREES_DIR`)

All paths MUST be canonicalized (resolved to real paths). Agent spawn MUST validate the requested CWD against this list.

### Feature Flags (axi-py)

Feature flags are read from environment variables at import time with falsy defaults:
- `STREAMING_DISCORD`
- `CHANNEL_STATUS_ENABLED`
- `CLEAN_TOOL_MESSAGES`
- `WEB_ENABLED`

### CLI Args

The `--permission-prompt-tool stdio` flag MUST be unconditionally added to all CLI invocations. Without it, permission prompts go to Claude CLI's default handler which auto-denies in pipe mode.

### DiscordClient HTTP Retry

Both implementations wrap their HTTP client with retry logic:
- **429 (rate limit)**: Sleep for `retry_after` from the response body, retry up to 3 times.
- **5xx (server error)**: Exponential backoff (`2^attempt` seconds in axi-py; similar in axi-rs), up to 3 retries.
- **4xx (non-429)**: Raise/return error immediately, no retry.

### Config Security

The Discord token MUST be redacted in any Debug/log output of the config struct. Raw tokens must never appear in log files.

## Invariants

- **I-CF-1**: `set_model` MUST hold the config lock across the entire load-mutate-save cycle. Without this, concurrent `/model` calls race and overwrite each other. [axi-py I12.1]
- **I-CF-2**: Specialized agents MUST rebuild their correct system prompt (including role-specific files and packs) on restart or reconstruction. Generic prompts strip role-specific instructions. [axi-py I12.2]
- **I-CF-3**: Default config files (`schedules.json`, `schedule_history.json`) MUST be seeded in `AXI_USER_DATA`, not `BOT_DIR`. [axi-py I12.3]
- **I-CF-4**: Discord token MUST be redacted in Debug/log output. [axi-rs I12.1]
- **I-CF-5**: `--permission-prompt-tool stdio` MUST be unconditionally added to all CLI invocations. Without it, permission prompts auto-deny in pipe mode, breaking any tool that requires permission. [axi-rs I12.2]

## Open Questions

1. **Token resolution divergence.** axi-py reads `DISCORD_TOKEN` from `.env` directly. axi-rs has a two-step fallback through `.test-slots.json`. Should the slot-based resolution be normative for both, or is it only needed for multi-instance test environments?

2. **Feature flags.** axi-py has four environment-based feature flags. axi-rs does not appear to have equivalent flags. Should these be normative, or are they axi-py implementation details that will be superseded?

3. **Pack system.** axi-py has a full pack system (loadable prompt modules from `packs/` directory). axi-rs does not implement packs. Should packs be normative? If so, what is the minimum viable pack interface?

4. **Prompt hash.** axi-py computes and stores a 16-char SHA-256 prompt hash for drift detection. axi-rs does not. Should prompt hash be normative?

5. **Config::for_test.** axi-rs has a test config constructor with placeholder credentials. axi-py uses environment-based test overrides. Should a test config constructor be normative?

6. **Async vs sync success codes.** axi-py's sync client accepts 200/204 while the async client accepts 200/201/204. Should success codes be unified?

## Implementation Notes

### axi-py
- `get_model` / `set_model` in `axi/config.py` with `_config_lock` (asyncio Lock).
- Packs loaded at import time from `packs/<name>/prompt.md` into `_PACKS` dict.
- `make_spawned_agent_system_prompt` in `axi/prompts.py` handles the layered prompt assembly.
- `_load_cwd_prompt` detects `<!-- mode: overwrite -->` for full replacement.
- Per-agent config in `axi/agents.py`: `_save_agent_config()` / `_load_agent_config()`.
- `_make_agent_options` in `axi/hub_wiring.py` calls `get_model()` at wake time.
- `load_mcp_servers` reads from `MCP_SERVERS_PATH` derived from `AXI_USER_DATA`.
- Feature flags read from env in `axi/config.py`.
- `compute_prompt_hash` produces 16-char SHA-256 prefix.
- `DiscordClient` / `AsyncDiscordClient` in `packages/discordquery/discordquery/client.py`.

### axi-rs
- `Config::from_env()` in `axi-config/src/config.rs` loads all config at startup.
- `resolve_discord_token` does env then slot-file lookup.
- `get_model` / `set_model` in `axi-config/src/model.rs` with `CONFIG_LOCK` (std Mutex).
- `DiscordClient` in `axi-config/src/discord.rs` wraps reqwest with 429/5xx retry.
- `Config::for_test` provides minimal test config.
- `Config::fmt` manually redacts `discord_token` in Debug output.
- `--permission-prompt-tool stdio` emitted in `claudewire/src/config.rs::Config::to_cli_args`.
- No pack system, no prompt hash, no feature flags, no per-agent config persistence.
