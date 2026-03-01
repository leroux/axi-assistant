"""System prompt construction and pack loading for the Axi bot.

Handles layered .md prompt files, pack system, and per-agent prompt assembly.
"""

from __future__ import annotations

__all__ = [
    "MASTER_SYSTEM_PROMPT",
    "compute_prompt_hash",
    "make_spawned_agent_system_prompt",
    "post_system_prompt_to_channel",
]

import hashlib
import io
import logging
import os
from typing import TYPE_CHECKING

import discord
from discord import TextChannel

from axi import config

if TYPE_CHECKING:
    from claude_agent_sdk.types import SystemPromptPreset

log = logging.getLogger("axi")

# ---------------------------------------------------------------------------
# Prompt hashing
# ---------------------------------------------------------------------------


def compute_prompt_hash(system_prompt: SystemPromptPreset | str | None) -> str | None:
    """Compute a short hash of the system prompt text for change detection.

    Extracts the prompt text (from 'append' for dicts, or the string directly),
    returns the first 16 hex chars of its SHA-256 hash.
    """
    if system_prompt is None:
        return None
    if isinstance(system_prompt, dict):
        text = system_prompt.get("append", "")
    else:
        text = system_prompt
    if not text:
        return None
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


# ---------------------------------------------------------------------------
# Prompt file loading
# ---------------------------------------------------------------------------


def _load_prompt_file(path: str, variables: dict[str, str] | None = None) -> str:
    """Load a prompt .md file, optionally expanding %(var)s placeholders."""
    with open(path, encoding="utf-8") as f:
        content = f.read()
    if variables:
        content = content % variables
    return content


_PROMPT_VARS = {"axi_user_data": config.AXI_USER_DATA, "bot_dir": config.BOT_DIR}

_SOUL = _load_prompt_file(os.path.join(config.BOT_DIR, "SOUL.md"), _PROMPT_VARS)
_DEV_CONTEXT = _load_prompt_file(os.path.join(config.BOT_DIR, "dev_context.md"), _PROMPT_VARS)


# ---------------------------------------------------------------------------
# Pack system: modular prompt fragments loaded from packs/<name>/prompt.md
# ---------------------------------------------------------------------------

PACKS_DIR = os.path.join(config.BOT_DIR, "packs")

# Which packs each agent type gets by default. Override per-spawn via axi_spawn_agent.
MASTER_PACKS: list[str] = ["algorithm"]
DEFAULT_SPAWNED_PACKS: list[str] = ["algorithm"]
AXI_DEV_PACKS: list[str] = ["axi-dev"]  # Auto-added for agents with axi-dev cwd


def _load_packs() -> dict[str, str]:
    """Scan packs/ and load each pack's prompt.md content.

    Returns {pack_name: prompt_text} for every valid pack found.
    Packs without a prompt.md are silently skipped.
    """
    packs: dict[str, str] = {}
    if not os.path.isdir(PACKS_DIR):
        return packs
    for name in sorted(os.listdir(PACKS_DIR)):
        prompt_path = os.path.join(PACKS_DIR, name, "prompt.md")
        if os.path.isfile(prompt_path):
            try:
                packs[name] = _load_prompt_file(prompt_path, _PROMPT_VARS)
                log.info("Loaded pack '%s' (%d chars)", name, len(packs[name]))
            except Exception:
                log.exception("Failed to load pack '%s'", name)
    return packs


_PACKS: dict[str, str] = _load_packs()


def _pack_prompt_text(pack_names: list[str]) -> str:
    """Concatenate prompt text for the given pack names. Unknown names are skipped with a warning."""
    parts: list[str] = []
    for name in pack_names:
        text = _PACKS.get(name)
        if text:
            parts.append(text)
        else:
            log.warning("Pack '%s' not found (available: %s)", name, list(_PACKS.keys()))
    return "\n\n".join(parts)


# ---------------------------------------------------------------------------
# System prompt templates
# ---------------------------------------------------------------------------

# Mini system prompt for non-admin spawned agents (keeps context small)
_AGENT_CONTEXT_PROMPT = """\
You are an agent session in the Axi system — a Discord-based personal assistant for a single user. \
You communicate through a dedicated Discord text channel. The user reads your messages there. \
Keep responses concise and well-formatted for Discord (markdown, code blocks).

Key context:
- The user's profile and preferences are in USER_PROFILE.md at {bot_dir}/USER_PROFILE.md
- You are one of several agent sessions. The master agent (Axi) coordinates via #axi-master.
- Your working directory is set by whoever spawned you. Files you create/edit stay in that directory.
- The user's timezone is US/Pacific.

Sandbox policy:
- You are authorized to use `dangerouslyDisableSandbox: true` for git operations and for curl/HTTP requests to `localhost:9100` (MinFlow task management API).
- Do NOT disable the sandbox for anything else.

Communication rules:
- Never guess or fabricate answers. If you lack context, say so and look it up.
- Do NOT use AskUserQuestion, Skill, or EnterWorktree tools — they are invisible in Discord.
- TodoWrite IS supported — use it to track progress on multi-step tasks. The todo list will be displayed in Discord automatically. Do NOT repeat or narrate the todo list contents in your text response — the user already sees it.
- EnterPlanMode and ExitPlanMode ARE supported — use plan mode normally for non-trivial implementation tasks. Your plan will be posted to Discord for user approval.
- Ask questions as normal text messages. List options in your message if the user needs to choose.\
""".format(**_PROMPT_VARS)


def _is_axi_dev_cwd(cwd: str) -> bool:
    """Check if a working directory is within the axi-assistant codebase."""
    return cwd.startswith(config.BOT_DIR) or bool(config.BOT_WORKTREES_DIR and cwd.startswith(config.BOT_WORKTREES_DIR))


# Master agent: soul + dev context + master packs
_master_packs_text = _pack_prompt_text(MASTER_PACKS)
MASTER_SYSTEM_PROMPT: SystemPromptPreset = {
    "type": "preset",
    "preset": "claude_code",
    "append": _SOUL + "\n\n" + _DEV_CONTEXT + ("\n\n" + _master_packs_text if _master_packs_text else ""),
}


def make_spawned_agent_system_prompt(cwd: str, packs: list[str] | None = None) -> SystemPromptPreset:
    """Build system prompt for a spawned agent based on its working directory.

    packs: explicit list of pack names to include, or None for DEFAULT_SPAWNED_PACKS.
           Pass [] to disable packs entirely.
    """
    if _is_axi_dev_cwd(cwd):
        # Admin agent — full soul + dev context
        append = _SOUL + "\n\n" + _DEV_CONTEXT
    else:
        # Non-admin agent — mini context prompt
        append = _AGENT_CONTEXT_PROMPT
    pack_names = list(packs if packs is not None else DEFAULT_SPAWNED_PACKS)
    if _is_axi_dev_cwd(cwd):
        for p in AXI_DEV_PACKS:
            if p not in pack_names:
                pack_names.append(p)
    packs_text = _pack_prompt_text(pack_names)
    if packs_text:
        append += "\n\n" + packs_text
    return {
        "type": "preset",
        "preset": "claude_code",
        "append": append,
    }


# ---------------------------------------------------------------------------
# Discord visibility for system prompts
# ---------------------------------------------------------------------------


async def post_system_prompt_to_channel(
    channel: TextChannel,
    system_prompt: SystemPromptPreset | str | None,
    *,
    is_resume: bool = False,
    prompt_changed: bool = False,
    session_id: str | None = None,
) -> None:
    """Post the system prompt as a file attachment to the agent's Discord channel.

    On resume with no prompt change, posts a brief note.
    On resume with prompt change, posts the full updated prompt.
    On new sessions, posts the appended system prompt as an .md file attachment.
    """
    if is_resume and not prompt_changed:
        sid_display = f"`{session_id[:8]}…`" if session_id else "unknown"
        await channel.send(f"*System:* 📋 Resumed session {sid_display}")
        return

    if isinstance(system_prompt, dict):
        prompt_text = system_prompt.get("append", "")
        label = "claude_code preset + appended instructions"
    elif isinstance(system_prompt, str):
        prompt_text = system_prompt
        label = "custom system prompt (full replacement)"
    else:
        return

    line_count = len(prompt_text.splitlines())
    file = discord.File(
        io.BytesIO(prompt_text.encode("utf-8")),
        filename="system-prompt.md",
    )
    sid_suffix = f" — session `{session_id[:8]}…`" if session_id else ""
    if prompt_changed:
        await channel.send(f"*System:* 📋 **System prompt updated** — {label} ({line_count} lines){sid_suffix}", file=file)
    else:
        await channel.send(f"*System:* 📋 {label} ({line_count} lines){sid_suffix}", file=file)
