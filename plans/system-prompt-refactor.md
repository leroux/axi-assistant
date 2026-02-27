# Plan: System Prompt Refactor — Transparency & Append Mode

## Problem Statement

The current setup has three issues:
1. **We replace the default Claude Code system prompt** instead of appending to it — agents lose ~15k tokens of built-in tool usage instructions, coding guidelines, and safety rules.
2. **Hidden context injection** — CLAUDE.md and MEMORY.md silently add content the user never sees in Discord.
3. **Subordinate agents are naked** — `system_prompt=None` wipes the default entirely, so spawned agents have zero system prompt guidance.

## Agent Taxonomy

| Agent Type | Detection | System Prompt |
|---|---|---|
| **Master (Axi Prime)** | The single master agent | Claude Code default + `SOUL.md` + `dev_context.md` |
| **Axi dev agent** | Spawned agent whose `cwd` is in `BOT_DIR` or `BOT_WORKTREES_DIR` | Claude Code default + `SOUL.md` + `dev_context.md` |
| **Claw** (general agent) | Spawned agent whose `cwd` is anywhere else | Claude Code default + `SOUL.md` |

The **initial task prompt** is always provided by whoever launches the agent. It is not templated into the system prompt.

## Prompt File Layout

```
axi-assistant/
├── SOUL.md               # Shared personality for ALL agents (identity, Discord style,
│                         #   scheduling, agent spawning, user profile ref, tool restrictions)
├── dev_context.md        # Axi dev context: architecture, safety rules, patterns,
│                         #   test workflow (merged from CLAUDE.md + MEMORY.md + test_instructions.md)
├── CLAUDE.md             # DELETED — content moved to dev_context.md
├── test_instructions.md  # DELETED — content moved to dev_context.md
└── ...
```

Composition per agent type:
```
Master    = claude_code preset + SOUL.md + dev_context.md
Axi dev   = claude_code preset + SOUL.md + dev_context.md
Claw      = claude_code preset + SOUL.md
```

## Decisions Made

| # | Fork | Decision |
|---|---|---|
| 1 | Master agent prompt strategy | **Append mode** — `SystemPromptPreset(preset="claude_code", append=...)` keeps the full default |
| 2 | CLAUDE.md content | **Merge into `dev_context.md`**, then delete CLAUDE.md |
| 3 | MEMORY.md content | **Merge into `dev_context.md`**, then clear MEMORY.md |
| 4 | test_instructions.md content | **Merge into `dev_context.md`**, then delete test_instructions.md |
| 5 | Discord visibility | **File attachment** on first session start; brief note on resume |
| 6 | Axi dev agents | **Claude Code default + `SOUL.md` + `dev_context.md`** |
| 7 | Claws (general agents) | **Claude Code default + `SOUL.md`** |
| 8 | Agent type detection | **By cwd** — `BOT_DIR` or `BOT_WORKTREES_DIR` → axi dev; else → claw |
| 9 | `setting_sources` | **`["local"]` only** — no ghost context from CLAUDE.md/MEMORY.md |
| 10 | Agent visibility | **All agents** post system prompt as file attachment + initial prompt shown in channel |
| 11 | Initial task prompt | **Provided by launcher** — not templated into system prompt |
| 12 | Prompt externalization | **All prompt content in .md files** — `SOUL.md` + `dev_context.md`, zero inline strings in bot.py |

---

## Implementation Steps

### Step 1: Create SOUL.md

Extract the Axi personality from the current `SYSTEM_PROMPT` literal in `bot.py` into a new file `SOUL.md` in the repo root. This is the **shared soul for all agents** — master, axi dev, and claws alike.

Content: Axi identity, Discord communication style, scheduling system (`schedules.json`), agent spawning conventions, working directory rules, user profile reference (`USER_PROFILE.md`), tool restrictions, communication style preferences.

**Audit the existing SYSTEM_PROMPT** for overlap with the default Claude Code prompt and exclude anything the default already covers (markdown formatting, tool usage patterns, general coding practices). Keep only Axi-specific content.

Since SOUL.md goes to ALL agents (including claws in arbitrary repos), it should be repo-agnostic — no axi-assistant-specific paths or patterns.

`SOUL.md` should use `%(variable)s` interpolation for `axi_user_data` and `bot_dir`, same as the current literal, so bot.py can expand them at load time.

### Step 2: Create dev_context.md

New file in the repo root. Content merged from three sources:
- **CLAUDE.md** (test instance safety rules — 6 lines)
- **MEMORY.md** (architecture, key files, patterns, safety rules — ~30 lines)
- **test_instructions.md** (test workflow, CLI commands, test guilds, workflow steps — ~80 lines)

This is the single file for all axi-assistant development context: codebase architecture, key files, important patterns, test instance safety rules, and the full test workflow.

Uses `%(bot_dir)s` interpolation where needed.

### Step 3: Rewrite system prompt construction in bot.py

Replace the `SYSTEM_PROMPT` string literal and `test_instructions.md` loading with file-loading logic:

```python
def _load_prompt_file(path: str, variables: dict | None = None) -> str:
    """Load a prompt file, optionally expanding %(var)s placeholders."""
    with open(path) as f:
        content = f.read()
    if variables:
        content = content % variables
    return content

_PROMPT_VARS = {"axi_user_data": AXI_USER_DATA, "bot_dir": BOT_DIR}

# Layer 1: Shared personality (ALL agents)
_SOUL = _load_prompt_file(os.path.join(BOT_DIR, "SOUL.md"), _PROMPT_VARS)

# Layer 2: Axi dev context (axi dev agents + master)
_DEV_CONTEXT = _load_prompt_file(os.path.join(BOT_DIR, "dev_context.md"), _PROMPT_VARS)
```

### Step 4: Build system prompt objects per agent type

```python
def _is_axi_dev_cwd(cwd: str) -> bool:
    """Check if a working directory is within the axi-assistant codebase."""
    return cwd.startswith(BOT_DIR) or (
        BOT_WORKTREES_DIR and cwd.startswith(BOT_WORKTREES_DIR)
    )

# Master: soul + dev context (master is always an axi dev agent too)
MASTER_SYSTEM_PROMPT = {
    "type": "preset",
    "preset": "claude_code",
    "append": _SOUL + "\n\n" + _DEV_CONTEXT,
}

def _make_spawned_agent_system_prompt(cwd: str) -> dict:
    """Build system prompt for a spawned agent based on its working directory."""
    if _is_axi_dev_cwd(cwd):
        # Axi dev agent — soul + dev context
        append = _SOUL + "\n\n" + _DEV_CONTEXT
    else:
        # General claw — soul only
        append = _SOUL
    return {
        "type": "preset",
        "preset": "claude_code",
        "append": append,
    }
```

### Step 5: Update spawn_agent()

At `bot.py:2095–2106`:

```python
session = AgentSession(
    name=name,
    cwd=cwd,
    system_prompt=_make_spawned_agent_system_prompt(cwd),  # was: None
    client=None,
    session_id=resume,
    ...
)
```

### Step 6: Update _make_agent_options()

Change `setting_sources` at `bot.py:1167`:
```python
setting_sources=["local"],  # was: ["user", "project", "local"]
```

### Step 7: Add system prompt posting to Discord

```python
import io

async def _post_system_prompt_to_channel(
    channel: discord.TextChannel,
    system_prompt,
    is_resume: bool,
    session_id: str | None = None,
):
    """Post system prompt visibility info to the agent's Discord channel."""
    if is_resume:
        await channel.send(f"\U0001f4cb Resumed session `{session_id}`")
        return

    if isinstance(system_prompt, dict):
        prompt_text = system_prompt.get("append", "")
        label = "System prompt: claude_code preset + appended instructions"
    elif isinstance(system_prompt, str):
        prompt_text = system_prompt
        label = "System prompt: custom (full replacement)"
    else:
        return

    file = discord.File(
        io.BytesIO(prompt_text.encode("utf-8")),
        filename="system-prompt.md",
    )
    await channel.send(
        f"\U0001f4cb {label} ({len(prompt_text.splitlines())} lines)",
        file=file,
    )
```

**Call sites:**

1. **Master agent** — on first session start (`session.session_id is None`), post to master channel.
2. **Spawned agents** — in `_run_initial_prompt()`, before sending the initial prompt:
   ```python
   await _post_system_prompt_to_channel(channel, session.system_prompt, is_resume=False)
   await channel.send(f"\U0001f4dd **Initial prompt:**\n{prompt[:1900]}")
   ```
3. **On resume** — in `wake_agent()` when `resume_id is not None`:
   ```python
   await _post_system_prompt_to_channel(channel, session.system_prompt, is_resume=True, session_id=resume_id)
   ```

### Step 8: Post scheduled task prompts to Discord

Currently, scheduled events (`check_schedules()` → `send_prompt_to_agent()` / `spawn_agent()` → `_run_initial_prompt()`) send prompts directly to the agent via `session.client.query()`. The prompt text never appears in Discord — only the agent's response does.

**Fix:** Post every prompt to Discord before it reaches the agent. Two paths need updating:

1. **`_run_initial_prompt()`** (~`bot.py:2144`) — already covered by Step 7 call site #2 above. The initial prompt is posted before `session.client.query()`. This handles both manual agent spawns and scheduled spawns of new agents.

2. **`send_prompt_to_agent()`** (~`bot.py:2124`) — for scheduled prompts sent to **already-existing** agents. Currently this just dispatches to `_run_initial_prompt()`, so it's covered too. But we should also include the schedule name for context:
   ```python
   # In check_schedules(), when firing a scheduled event:
   schedule_label = f"*\U0001f4c5 Scheduled: `{entry['name']}`*"
   await channel.send(schedule_label)
   # Then the prompt itself is posted by _run_initial_prompt()
   ```

3. **`on_message()` user message path** (~`bot.py:2552`) — user messages are already visible in Discord (the user typed them). No change needed here.

This ensures **every prompt to every agent** is visible in Discord: system prompts (file attachment), user messages (already visible), spawned agent initial prompts (posted before query), and scheduled task prompts (posted with schedule context).

### Step 9: Delete CLAUDE.md

Content moved to `dev_context.md`.

### Step 10: Delete test_instructions.md

Content moved to `dev_context.md`. Remove the loading logic from bot.py (the `_TEST_INSTRUCTIONS_PATH` / `os.path.isfile` block).

### Step 11: Clear MEMORY.md

Write a tombstone:
```markdown
# Memory

Content moved to dev_context.md for transparency.
Do not add content here — it loads invisibly via Claude Code.
All agent context goes through the appended system prompt.
```

### Step 12: Update master agent initialization

In `on_ready()` or wherever the master `AgentSession` is created (~`bot.py:3310`), change `system_prompt=SYSTEM_PROMPT` to `system_prompt=MASTER_SYSTEM_PROMPT`.

---

## File Changes Summary

| File | Action |
|---|---|
| `SOUL.md` | **Create** — Shared Axi personality for all agents, extracted from SYSTEM_PROMPT literal in bot.py |
| `dev_context.md` | **Create** — All axi dev context merged from CLAUDE.md + MEMORY.md + test_instructions.md |
| `bot.py` | Remove SYSTEM_PROMPT literal + test_instructions.md loading, add `_load_prompt_file()`, add `_is_axi_dev_cwd()`, add `_make_spawned_agent_system_prompt()`, update `_make_agent_options()` setting_sources, add `_post_system_prompt_to_channel()`, update `spawn_agent()`, update master init, update wake/prompt call sites |
| `CLAUDE.md` | **Delete** (content moved to `dev_context.md`) |
| `test_instructions.md` | **Delete** (content moved to `dev_context.md`) |
| `MEMORY.md` | **Clear with tombstone** (content moved to `dev_context.md`) |

## Risks & Notes

- **Default Claude Code prompt is ~15k tokens** — adding it back increases context usage but provides much better tool guidance and coding patterns. The 1M context beta mitigates this.
- **SOUL.md must be audited** against the default Claude Code prompt during creation. The existing SYSTEM_PROMPT has ~140 lines and likely overlaps significantly with the default on things like markdown formatting. Since SOUL.md goes to ALL agents (including claws in arbitrary repos), it should be repo-agnostic — no axi-assistant-specific paths or patterns.
- **`setting_sources=["local"]`** means `/memory` commands will still write to MEMORY.md but it won't be loaded. Consider adding to SOUL.md: "Do not use /memory — context is managed explicitly via the system prompt."
- **Bridge reconnect** (`bot.py:2317–2418`) creates minimal `ClaudeAgentOptions` with no `system_prompt` since the CLI is already running. No changes needed on this path.
- **`_is_axi_dev_cwd()` edge case**: If `BOT_WORKTREES_DIR` is `None`, the `startswith` check is skipped. Already the pattern used elsewhere.
- **File encoding**: `_load_prompt_file()` should handle UTF-8 explicitly if any prompt files contain special characters.
- **`%(var)s` in prompt files**: Content authors must escape literal `%` as `%%` in SOUL.md and dev_context.md. Document this in a comment.
