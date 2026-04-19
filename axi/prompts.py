"""System prompt construction and extension loading for the Axi bot.

Handles layered .md prompt files, extension system, and per-agent prompt assembly.
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
import re
from typing import TYPE_CHECKING

import discord
from discord import TextChannel

from axi import config
from axi.discord_wire import audited_channel_send
from axi.extensions import DEFAULT_EXTENSIONS, extension_prompt_text

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


_PROMPT_VARS = {
    "axi_user_data": config.AXI_USER_DATA,
    "bot_dir": config.BOT_DIR,
}

_PROMPTS_DIR = os.path.join(config.BOT_DIR, "prompts")
_SOUL = _load_prompt_file(os.path.join(_PROMPTS_DIR, "SOUL.md"), _PROMPT_VARS)
_DEV_CONTEXT = _load_prompt_file(os.path.join(_PROMPTS_DIR, "axi_codebase_context.md"), _PROMPT_VARS)

# User profile — loaded from profile/ subdirectory of AXI_USER_DATA
_PROFILE_DIR = os.path.join(config.AXI_USER_DATA, "profile")
_USER_PROFILE = ""
_profile_path = os.path.join(_PROFILE_DIR, "USER_PROFILE.md")
if os.path.isfile(_profile_path):
    _USER_PROFILE = _load_prompt_file(_profile_path)
    # Make ref paths absolute so agents spawned outside the repo can resolve them
    _USER_PROFILE = _USER_PROFILE.replace("profile/refs/", os.path.join(_PROFILE_DIR, "refs") + "/")


# ---------------------------------------------------------------------------
# Extension system: modular prompt fragments loaded from extensions/<name>/
# ---------------------------------------------------------------------------
# Extensions replace the old "packs" system. Each extension has prompt.md,
# meta.json (audience, hooks, prompt_hooks), and optional commands/.
# See axi.extensions for loading logic.


# ---------------------------------------------------------------------------
# System prompt templates
# ---------------------------------------------------------------------------

def _is_axi_dev_cwd(cwd: str) -> bool:
    """Check if a working directory is within the axi-assistant codebase."""
    return cwd.startswith(config.BOT_DIR) or bool(config.BOT_WORKTREES_DIR and cwd.startswith(config.BOT_WORKTREES_DIR))


# Master agent: soul + dev context + admin extensions
_master_ext_text = extension_prompt_text(DEFAULT_EXTENSIONS, audience="admin")
_master_append = _SOUL + "\n\n" + _DEV_CONTEXT + ("\n\n" + _USER_PROFILE if _USER_PROFILE else "") + ("\n\n" + _master_ext_text if _master_ext_text else "")
MASTER_SYSTEM_PROMPT: SystemPromptPreset = {
    "type": "preset",
    "preset": "claude_code",
    "append": _master_append.replace("{agent_name}", config.MASTER_AGENT_NAME).replace("{cwd}", config.DEFAULT_CWD),
}


_CWD_PROMPT_FILENAME = "SYSTEM_PROMPT.md"


_CWD_MODE_RE = re.compile(r"<!--\s*mode:\s*(overwrite|append)\s*-->")


def _load_cwd_prompt(cwd: str) -> tuple[str, str] | None:
    """Load SYSTEM_PROMPT.md from the agent's working directory, if it exists.

    Returns (content, mode) where mode is "append" (default) or "overwrite".
    Mode is detected from an HTML comment directive: ``<!-- mode: overwrite -->``.
    The directive is stripped from the returned content.
    """
    path = os.path.join(cwd, _CWD_PROMPT_FILENAME)
    try:
        with open(path, encoding="utf-8") as f:
            content = f.read().strip()
        if not content:
            return None
        mode = "append"
        m = _CWD_MODE_RE.search(content)
        if m:
            mode = m.group(1)
            content = (content[: m.start()] + content[m.end() :]).strip()
        log.info("Loaded CWD system prompt from %s (%d chars, mode=%s)", path, len(content), mode)
        return (content, mode)
    except FileNotFoundError:
        return None
    except Exception:
        log.exception("Failed to load CWD system prompt from %s", path)
        return None


def make_spawned_agent_system_prompt(
    cwd: str,
    extensions: list[str] | None = None,
    compact_instructions: str | None = None,
    agent_name: str = "unknown",
) -> SystemPromptPreset:
    """Build system prompt for a spawned agent based on its working directory.

    extensions: explicit list of extension names to include, or None for DEFAULT_EXTENSIONS.
                Pass [] to disable extensions entirely.
    compact_instructions: if provided, appended as a compaction guidance section.

    If SYSTEM_PROMPT.md exists in the agent's CWD, its contents are appended
    after the base prompt and extensions (default). If the file contains
    ``<!-- mode: overwrite -->``, it replaces the base prompt entirely
    (compact_instructions are still appended).
    """
    is_admin = _is_axi_dev_cwd(cwd)
    if is_admin:
        # Admin agent — full soul + dev context
        append = _SOUL + "\n\n" + _DEV_CONTEXT + ("\n\n" + _USER_PROFILE if _USER_PROFILE else "")
    else:
        # Non-admin agent — soul + user profile (same as pre-migration 934842d)
        append = _SOUL + ("\n\n" + _USER_PROFILE if _USER_PROFILE else "")
    audience = "admin" if is_admin else "general"
    ext_names = list(extensions if extensions is not None else DEFAULT_EXTENSIONS)
    ext_text = extension_prompt_text(ext_names, audience)
    if ext_text:
        append += "\n\n" + ext_text

    # Auto-load SYSTEM_PROMPT.md from agent CWD
    cwd_result = _load_cwd_prompt(cwd)
    if cwd_result:
        cwd_prompt, cwd_mode = cwd_result
        if cwd_mode == "overwrite":
            # Overwrite mode: replaces soul/extensions/dev-context entirely
            append = cwd_prompt
        else:
            # Append mode (default): added after base prompt + extensions
            append += "\n\n" + cwd_prompt

    if compact_instructions:
        append += (
            "\n\n# Context Compaction Instructions\n"
            "When summarizing/compacting this conversation, prioritize preserving:\n"
            f"- {compact_instructions}"
        )
    append = append.replace("{agent_name}", agent_name).replace("{cwd}", cwd)
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
        await audited_channel_send(channel, f"*System:* 📋 Resumed session {sid_display}", operation="system_prompt.resume")
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
        await audited_channel_send(
            channel,
            f"*System:* 📋 **System prompt updated** — {label} ({line_count} lines){sid_suffix}",
            file=file,
            operation="system_prompt.post",
        )
    else:
        await audited_channel_send(
            channel,
            f"*System:* 📋 {label} ({line_count} lines){sid_suffix}",
            file=file,
            operation="system_prompt.post",
        )
