# Library Extraction Opportunities — Axi Codebase

## Ranked Candidates

---

### 1. `supervisor` — Process Supervisor with Crash Recovery & Git Rollback

**What it does:** Manages a child process lifecycle with signal-aware restart (SIGHUP hot restart, SIGTERM clean stop), crash detection (startup vs runtime), configurable git rollback on crash, output tee-to-file, and crash markers for post-mortem analysis.

**Current location:** `axi/supervisor.py` (~350 lines)

**Why it's reusable:** Any long-running Python service that wraps a child process could use this — bot supervisors, web server launchers, CI runners, dev tools. The crash threshold + rollback pattern is genuinely novel. It has zero axi-specific imports: pure stdlib (`subprocess`, `signal`, `threading`, `json`, `os`, `pathlib`).

**Dependencies:** Python stdlib only. Zero external deps.

**Extraction effort:** **Easy.** Already self-contained with no project imports. The only axi-specific thing is `uv run python -m axi.main` as the hardcoded launch command — make that configurable and it's done. The `ENABLE_ROLLBACK`, `CRASH_THRESHOLD`, `MAX_RUNTIME_CRASHES` are already env-var-driven.

**Verdict:** **Extract now.** This is the cleanest candidate. Zero entanglement, clear standalone purpose, genuinely useful. Could be a `pip install procsupervisor` or similar.

---

### 2. `shutdown` — Graceful Shutdown Coordinator

**What it does:** Coordinates graceful shutdown of concurrent async workers: waits for busy workers to finish, sends progress notifications, has a safety-deadline thread (os._exit fallback), and supports both graceful and force paths.

**Current location:** `axi/shutdown.py` (~307 lines)

**Why it's reusable:** Any multi-agent or multi-worker async application needs graceful shutdown — web servers, task queues, bot frameworks. The `Sleepable` protocol + dependency injection pattern makes it framework-agnostic. The safety-deadline thread preventing zombie processes is a pattern people reimplements constantly.

**Dependencies:** Python stdlib only (`asyncio`, `signal`, `threading`, `os`). The `Sleepable` protocol uses only `name`, `query_lock`, and `client` — trivially generic.

**Extraction effort:** **Easy.** Already uses DI (all side effects are callbacks). The `Sleepable` protocol is the only interface contract. Remove the `kill_supervisor()` / `exit_for_restart()` functions or make them pluggable (they're already passed as `kill_fn`). The `RESTART_EXIT_CODE = 42` convention could be configurable.

**Verdict:** **Extract later.** Good candidate, but the value proposition is narrower than the supervisor. The coordinator pattern is useful but might be too specific to the "async agents with query locks" model to attract a wide audience. Consider bundling with the supervisor as a single `procsupervisor` library.

---

### 3. `discord_rest` + `discord_query` + `wait_for_message` — Lightweight Discord REST CLI Toolkit

**What it does:** A minimal, httpx-based Discord REST client with rate-limit retry, plus CLI tools for querying message history, searching messages, and polling for new messages. No discord.py gateway — pure REST.

**Current location:** `axi/discord_rest.py` (~74 lines), `axi/discord_query.py` (~406 lines), `axi/wait_for_message.py` (~204 lines) — ~684 lines total

**Why it's reusable:** Many tools need to interact with Discord via REST without a full gateway bot — CI/CD notifications, monitoring scripts, cross-bot test harnesses, log scrapers, integration test frameworks. The "wait for message" polling pattern is especially useful for testing bots. There's no good lightweight Python package for "just query Discord channels from a script."

**Dependencies:** `httpx` only (plus stdlib). No discord.py.

**Extraction effort:** **Medium.** Three files that share `discord_rest.py` as a base layer. Need to:
- Remove the axi-specific `get_token()` fallback to `test-config.json` (make it pure env var or explicit)
- Remove `dotenv` import (or make it optional)
- Package as a CLI with `[cli]` extras for argparse entry points
- The `api_get` function does `sys.exit(1)` on errors — need to raise exceptions instead and let the CLI layer handle exits

**Verdict:** **Extract now.** Strong candidate. The "lightweight Discord REST CLI" niche is underserved. The `wait_for_message` polling tool alone is useful for anyone testing Discord bots. Package as `discord-rest-cli` or `discordquery`.

---

### 4. `axi_test` — Test Instance Slot Manager

**What it does:** Manages concurrent test instance reservations with file-lock-based slot allocation, systemd service lifecycle, worktree management, merge queue with deadlock detection, and Discord channel cleanup.

**Current location:** `axi/axi_test.py` (~1314 lines)

**Why it's reusable:** The slot allocation pattern (flock-based, with health checks, wait-and-retry, orphan cleanup) is generic. The merge queue (with PID heartbeats, stale entry detection, squash-merge execution) is independently useful. But the systemd + git worktree + Discord integration makes it very coupled.

**Dependencies:** `httpx`, `python-dotenv`, `fcntl` (Linux-only), systemd, git

**Extraction effort:** **Hard.** The module interleaves three independently useful systems: (a) slot allocation with flock, (b) merge queue, (c) systemd service management. To extract cleanly you'd need to factor each into its own layer. The slot manager alone (~300 lines) could be a library; the merge queue (~200 lines) could be another; the rest is axi-specific glue.

**Verdict:** **Extract the merge queue later.** The flock-based merge queue with heartbeat + stale detection is the most reusable piece. The slot allocator is useful but too tied to the test-config.json schema. The systemd integration is pure axi glue.

---

### 5. `schedule_tools` — Cron + One-Off Schedule Manager (JSON-backed)

**What it does:** Manages per-owner scheduled tasks (cron and one-off) with JSON file persistence, async locking, history with dedup, pruning, and an MCP tool interface.

**Current location:** `axi/schedule_tools.py` (~373 lines)

**Why it's reusable:** Schedule management is a common need for bots and automation tools. The core (load/save/create/delete/history/prune) is generic. However, it's tightly coupled to the Claude Agent SDK's MCP tool interface.

**Dependencies:** `croniter`, `claude_agent_sdk` (for MCP server/tool types), `axi.config` (for paths)

**Extraction effort:** **Medium.** The pure schedule CRUD (~120 lines: `_load`, `_save`, `load_schedules`, `save_schedules`, `load_history`, `append_history`, `prune_history`) could be extracted easily. The MCP tool wrappers (~250 lines) are claude_agent_sdk-specific and would stay.

**Verdict:** **Don't bother.** The reusable core (JSON file CRUD with a lock) is only ~120 lines — not enough for a library. The interesting part (MCP tool interface) is tied to claude_agent_sdk. A schedule library needs a lot more (timezone-aware evaluation, persistence backends, job execution) to be useful standalone.

---

### 6. `rate_limits` — API Rate Limit Tracker with Broadcasting

**What it does:** Parses rate limit durations from error text (regex-based), tracks rate limit state, records usage to JSONL, manages quota utilization tracking, and coordinates notifications via DI callbacks.

**Current location:** `axi/rate_limits.py` (~252 lines)

**Why it's reusable:** Rate limit handling is universal for API-heavy applications. The `parse_rate_limit_seconds()` regex parser and the state management pattern are generic.

**Dependencies:** `axi.config` (for file paths), `axi.axi_types` (for dataclasses). Stdlib otherwise.

**Extraction effort:** **Medium.** The pure helpers (`parse_rate_limit_seconds`, `format_time_remaining`, `is_rate_limited`, `rate_limit_remaining_seconds`) are generic. The usage recording is tied to `ResultMessage` from claude_agent_sdk. The notification coordination uses DI callbacks but the protocol is somewhat specific.

**Verdict:** **Don't bother.** The reusable core is ~50 lines of regex parsing and time formatting. Every project rolls its own rate limit handling because the specifics vary too much (headers vs error text, backoff strategy, per-endpoint vs global). Not enough substance for a standalone library.

---

### 7. Cross-cutting: Discord Message Sending Utilities

**What it does:** `split_message()` splits text at newline boundaries to fit Discord's 2000-char limit. `send_long()` sends multi-chunk messages with channel recreation on NotFound. `send_system()` prefixes system messages.

**Current location:** `axi/agents.py:394-443` (~50 lines)

**Why it's reusable:** Every Discord bot needs message splitting. But it's literally 15 lines of logic.

**Dependencies:** `discord.py` for the send part, pure Python for the split.

**Extraction effort:** **Trivial.**

**Verdict:** **Don't bother.** Too small. The `split_message()` function is 10 lines. This belongs in a Discord utilities library if one existed, but it's not worth creating a package for.

---

### 8. `channels.py` — Discord Channel Lifecycle Manager

**What it does:** Channel creation, categorization (Active/Killed), permission overwrites, topic metadata format/parse, and channel state management.

**Current location:** `axi/channels.py` (~297 lines)

**Why it's reusable:** The Active/Killed category pattern with permission management is axi-specific. The `format_channel_topic` / `parse_channel_topic` key-value format is generic but trivial.

**Dependencies:** `discord.py`, `axi.config` (deeply coupled)

**Extraction effort:** **Hard.** Module-level mutable state (`target_guild`, `active_category`, etc.), injected dependencies via `init()`, and deep coupling to axi's category naming convention.

**Verdict:** **Don't bother.** Too axi-specific. The channel lifecycle management is glue code for the agent orchestration pattern, not a general-purpose abstraction.

---

### 9. `prompts.py` — Layered System Prompt Assembly

**What it does:** Pack system (modular prompt fragments from `packs/<name>/prompt.md`), variable substitution, prompt hashing for change detection, admin vs non-admin prompt assembly.

**Current location:** `axi/prompts.py` (~233 lines)

**Why it's reusable:** The "packs" concept (modular prompt fragments loaded from disk, composable per-agent) could be useful for any multi-agent system. Prompt hashing for change detection is a nice pattern.

**Dependencies:** `discord.py` (for posting to channels), `axi.config`, `claude_agent_sdk.types`

**Extraction effort:** **Medium.** The core pack system (~60 lines: `_load_packs`, `_pack_prompt_text`, `_load_prompt_file`) is generic. The prompt assembly logic is axi-specific. The Discord posting function is an unrelated concern mixed in.

**Verdict:** **Don't bother.** The reusable pack-loading logic is ~60 lines. Prompt engineering patterns vary too much between projects to standardize. Not worth a library.

---

### 10. `flowcoder.py` — JSON-Lines Subprocess Manager

**What it does:** Manages a subprocess communicating via newline-delimited JSON on stdin/stdout, with stderr forwarding, graceful shutdown sequence (send shutdown msg → close stdin → SIGTERM → wait → SIGKILL), and a procmux-backed variant for persistence.

**Current location:** `axi/flowcoder.py` (~381 lines)

**Why it's reusable:** The pattern of managing a JSON-lines subprocess with proper lifecycle (start, async message iterator, send, graceful stop) is very common — LSP servers, language servers, any tool that speaks JSON-lines.

**Dependencies:** `asyncio` (stdlib), `claudewire.types` (for event types in the managed variant)

**Extraction effort:** **Medium.** `FlowcoderProcess` (~220 lines) is almost standalone — only imports `claudewire.types` for `ExitEvent`/`StderrEvent`/`StdoutEvent` in the managed variant. The base `FlowcoderProcess` class is pure asyncio. The `build_engine_cmd` and `build_engine_env` helpers are flowcoder-specific and would be removed.

**Verdict:** **Extract later, maybe.** The JSON-lines subprocess pattern is genuinely useful, but `asyncio.create_subprocess_exec` + readline is already pretty simple. The graceful shutdown sequence (msg → close stdin → term → wait → kill) is the real value. Could be a very small library (`jsonl-subprocess` or similar), but might be too thin to justify packaging. Revisit if you find yourself reimplementing this pattern elsewhere.

---

## Summary: Ranked by Priority

| Rank | Candidate | Verdict | Effort | Lines |
|------|-----------|---------|--------|-------|
| 1 | **supervisor** — Process supervisor with crash recovery | **Extract now** | Easy | ~350 |
| 2 | **discord REST toolkit** — Lightweight Discord REST CLI | **Extract now** | Medium | ~684 |
| 3 | **shutdown coordinator** — Graceful async shutdown | Extract later (bundle with supervisor) | Easy | ~307 |
| 4 | **merge queue** (from axi_test) — Flock-based merge queue | Extract later | Medium | ~200 |
| 5 | **jsonl-subprocess** (from flowcoder) — JSON-lines process manager | Extract later, maybe | Medium | ~220 |
| 6 | schedule_tools core | Don't bother | — | ~120 |
| 7 | rate_limits parser | Don't bother | — | ~50 |
| 8 | channels.py | Don't bother | — | — |
| 9 | prompts.py pack system | Don't bother | — | ~60 |
| 10 | split_message utility | Don't bother | — | ~15 |

**The two clear winners are `supervisor` (zero deps, already self-contained, genuinely novel crash recovery + git rollback) and the Discord REST CLI toolkit (underserved niche, httpx-only, three cohesive tools).**
