# Testing Plan for Axi Assistant

## Context

The bot currently has zero automated tests. The code is well-structured and working, but has testability issues: module-level side effects (`os.environ[]` at import time), hardcoded file paths, and pure logic mixed into async handlers. A targeted refactor extracts testable logic into importable modules, then we write comprehensive tests against those modules.

## Phase 1: Refactor for testability

Extract pure/near-pure logic from `bot.py` and `supervisor.py` into three new modules. No behavior changes — just moving code.

### 1a. Create `scheduling.py` — schedule evaluation logic

Move from `bot.py`:
- `load_schedules()`, `save_schedules()` — parameterize with `path` arg
- `load_history()`, `append_history()`, `prune_history()` — parameterize with `path` arg
- `load_skips()`, `save_skips()`, `prune_skips()`, `check_skip()` — parameterize with `path` and `tz` args
- `load_agent_history()`, `save_agent_history()`, `record_agent_spawn()`, `update_agent_history_session_id()`, `mark_agent_killed()` — parameterize with `path` arg
- `save_active_sessions()`, `load_active_sessions()` — parameterize with `path` arg

Each function gets an explicit file path parameter instead of reading module-level constants. `bot.py` calls them with the real paths.

### 1b. Create `helpers.py` — pure utility functions

Move from `bot.py`:
- `split_message(text, limit)` — already pure
- `_normalize_channel_name(name)` — already pure
- `make_cwd_permission_callback(allowed_cwd)` — nearly pure (just needs `os.path.realpath`)

Move from `discord_query.py`:
- `datetime_to_snowflake(dt)` — already pure
- `resolve_snowflake(value)` — pure except for `sys.exit` (refactor to raise ValueError instead, catch in CLI)
- `format_message(msg, fmt)` — already pure

### 1c. Refactor `supervisor.py` for testability

- Extract `classify_crash(elapsed, crash_threshold)` → returns `"startup"` | `"runtime"` | `"restart"` | `"clean"`
- Make `ensure_default_files(dir)` accept a directory parameter
- Make `tail_log(path, n)` accept a file path parameter
- Make `write_crash_marker(dir, ...)` and `write_rollback_marker(dir, ...)` accept directory parameter

### 1d. Update `bot.py` and `discord_query.py` imports

- `bot.py` imports from `scheduling` and `helpers`, passes real paths
- `discord_query.py` imports `datetime_to_snowflake`, `resolve_snowflake`, `format_message` from `helpers`
- Verify bot still starts and works identically

## Phase 2: Add test infrastructure

### 2a. Add test dependencies to `pyproject.toml`

```toml
[project.optional-dependencies]
test = ["pytest", "pytest-asyncio"]
```

### 2b. Create `tests/` directory structure

```
tests/
├── conftest.py          # Shared fixtures (tmp_path based file helpers)
├── test_helpers.py      # Pure utility function tests
├── test_scheduling.py   # Schedule/history/skip file operations
├── test_supervisor.py   # Crash classification, marker writing, rollback logic
└── test_discord_query.py # Snowflake conversion, message formatting
```

### 2c. Create `conftest.py` with shared fixtures

- `schedules_file(tmp_path)` — creates a temp schedules.json, returns path
- `history_file(tmp_path)` — creates a temp schedule_history.json, returns path
- `skips_file(tmp_path)` — creates a temp schedule_skips.json, returns path
- `agent_history_file(tmp_path)` — creates a temp agent_history.json, returns path

## Phase 3: Write tests

### `test_helpers.py` (~15 tests)

**`split_message`:**
- Short text returns single chunk
- Text exactly at limit returns single chunk
- Long text splits on newline boundary
- Long text with no newlines splits at limit
- Multiple chunks needed for very long text
- Empty string returns `[""]` or `[]` (verify actual behavior)

**`_normalize_channel_name`:**
- Lowercases input
- Replaces spaces with hyphens
- Strips invalid characters
- Truncates to 100 chars
- Handles already-valid names

**`make_cwd_permission_callback`:**
- Allows Edit within cwd
- Allows Write within cwd
- Denies Edit outside cwd
- Denies Write outside cwd
- Allows Read everywhere (inside and outside cwd)
- Allows Grep/Glob everywhere
- Allows Bash (handled by sandbox)
- Blocks symlink escape (path resolves outside cwd)

### `test_scheduling.py` (~20 tests)

**Load/save round-trips:**
- `load_schedules` returns `[]` for missing file
- `load_schedules` returns `[]` for malformed JSON
- `save_schedules` → `load_schedules` round-trip preserves data
- Same for history, skips, agent_history

**`append_history`:**
- Appends entry with correct fields
- Preserves existing entries

**`prune_history`:**
- Removes entries older than 7 days
- Keeps entries newer than 7 days
- No-op when no old entries exist

**`check_skip`:**
- Returns True and removes matching skip
- Returns False for no matching skip
- Handles multiple skips for different events

**`record_agent_spawn` / `mark_agent_killed`:**
- Records spawn with all fields
- Marks most recent active entry as killed
- Doesn't affect other agents' entries

**`update_agent_history_session_id`:**
- Updates session_id on most recent active entry
- Doesn't affect killed entries

**`save_active_sessions` / `load_active_sessions`:**
- Round-trip preserves session data
- Returns `[]` for missing file
- Cleans up file after loading

### `test_supervisor.py` (~10 tests)

**`classify_crash`:**
- Exit code 42 → `"restart"`
- Exit code 0 → `"clean"`
- Exit code 1 with elapsed >= 60 → `"runtime"`
- Exit code 1 with elapsed < 60 → `"startup"`

**`ensure_default_files`:**
- Creates missing files
- Doesn't overwrite existing files

**`tail_log`:**
- Returns last N lines
- Returns empty string for missing file
- Handles file shorter than N lines

**`write_crash_marker` / `write_rollback_marker`:**
- Creates valid JSON file
- Contains all expected fields

### `test_discord_query.py` (~10 tests)

**`datetime_to_snowflake`:**
- Known datetime produces expected snowflake
- Epoch produces snowflake 0
- Round-trip consistency

**`resolve_snowflake`:**
- Numeric string returns int directly
- ISO datetime string returns snowflake
- Invalid string raises ValueError (after refactor)

**`format_message`:**
- JSONL format includes all fields
- Text format includes timestamp, author, content
- Attachments/embeds shown in text format
- Missing fields handled gracefully

## Files modified

| File | Change |
|------|--------|
| `bot.py` | Import from `scheduling` and `helpers`; remove moved functions; pass paths to scheduling functions |
| `discord_query.py` | Import from `helpers`; remove moved functions; catch ValueError from `resolve_snowflake` |
| `supervisor.py` | Parameterize `ensure_default_files`, `tail_log`, marker writers; extract `classify_crash` |
| `scheduling.py` | **New** — schedule/history/skip/agent-history file operations |
| `helpers.py` | **New** — pure utilities (split_message, normalize_channel_name, permission callback, snowflake helpers, format_message) |
| `pyproject.toml` | Add `[project.optional-dependencies] test` |
| `tests/conftest.py` | **New** — shared fixtures |
| `tests/test_helpers.py` | **New** — ~15 tests |
| `tests/test_scheduling.py` | **New** — ~20 tests |
| `tests/test_supervisor.py` | **New** — ~10 tests |
| `tests/test_discord_query.py` | **New** — ~10 tests |

## Verification

```bash
# Run all tests
uv run --extra test pytest tests/ -v

# Run with coverage
uv run --extra test pytest tests/ --cov=. --cov-report=term-missing
```

After tests pass, manually verify the bot still starts and works:
```bash
uv run python supervisor.py
# Send a test message in Discord, verify response
# Check that scheduling, agent spawn/kill still work
```
