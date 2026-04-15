"""Extension system — modular prompt fragments, flowchart hooks, and prompt hooks.

Extensions replace the simpler "packs" system with richer metadata:
- prompt.md: prompt text appended to agent system prompts (like packs)
- meta.json: audience filtering, flowchart hook registrations, prompt hook files
- commands/: flowchart commands symlinked into commands/ for FlowCoder discovery

Each extension lives in extensions/<name>/ under BOT_DIR.
"""

from __future__ import annotations

__all__ = [
    "DEFAULT_EXTENSIONS",
    "EXTENSIONS_DIR",
    "extension_prompt_text",
    "resolve_extension_hooks",
    "resolve_extension_sandbox",
    "resolve_prompt_hooks",
    "sync_extension_commands",
]

import json
import logging
import os

from axi import config

log = logging.getLogger("axi")

EXTENSIONS_DIR = os.path.join(config.BOT_DIR, "extensions")

# Which extensions each agent type gets by default.
# Comma-separated extension names via env, e.g. DEFAULT_EXTENSIONS=algorithm,research
_default_extensions_str = os.environ.get("DEFAULT_EXTENSIONS", "")
DEFAULT_EXTENSIONS: list[str] = [p.strip() for p in _default_extensions_str.split(",") if p.strip()]

# Prompt variable substitutions shared with prompts.py
_PROMPT_VARS = {"axi_user_data": config.AXI_USER_DATA, "bot_dir": config.BOT_DIR}


def _load_prompt_file(path: str, variables: dict[str, str] | None = None) -> str:
    """Load a prompt .md file, optionally expanding %(var)s placeholders."""
    with open(path, encoding="utf-8") as f:
        content = f.read()
    if variables:
        content = content % variables
    return content


def _load_extensions() -> dict[str, dict]:
    """Scan extensions/ and load each extension's prompt.md content, metadata, and hooks.

    Returns {ext_name: {"text": str, "audience": str, "hooks": dict, "prompt_hooks": dict}}
    for every valid extension found.

    Audience defaults to "all" if no meta.json or no audience field.
    Extensions without a prompt.md get empty text (they may be flowchart-only).
    Extensions with neither prompt.md nor meta.json are skipped.
    """
    extensions: dict[str, dict] = {}
    if not os.path.isdir(EXTENSIONS_DIR):
        return extensions
    for name in sorted(os.listdir(EXTENSIONS_DIR)):
        ext_dir = os.path.join(EXTENSIONS_DIR, name)
        if not os.path.isdir(ext_dir):
            continue
        prompt_path = os.path.join(ext_dir, "prompt.md")
        meta_path = os.path.join(ext_dir, "meta.json")
        has_prompt = os.path.isfile(prompt_path)
        has_meta = os.path.isfile(meta_path)
        if not has_prompt and not has_meta:
            continue
        try:
            text = _load_prompt_file(prompt_path, _PROMPT_VARS) if has_prompt else ""
            audience = "all"
            hooks: dict = {}
            prompt_hooks: dict = {}
            if has_meta:
                with open(meta_path, encoding="utf-8") as f:
                    meta = json.loads(f.read())
                    audience = meta.get("audience", "all")
                    hooks = meta.get("hooks", {})
                    prompt_hooks = meta.get("prompt_hooks", {})
            sandbox: dict = meta.get("sandbox", {}) if has_meta else {}
            extensions[name] = {"text": text, "audience": audience, "hooks": hooks, "prompt_hooks": prompt_hooks, "sandbox": sandbox}
        except Exception:
            log.exception("Failed to load extension '%s'", name)
    return extensions


def extension_prompt_text(ext_names: list[str], audience: str = "all") -> str:
    """Concatenate prompt text for the given extension names, filtered by audience.

    Reads extension files from disk each time so edits take effect without a restart.
    An extension is included if its audience is "all" or matches the requested audience.
    Unknown names are skipped with a warning.
    """
    extensions = _load_extensions()
    parts: list[str] = []
    for name in ext_names:
        ext = extensions.get(name)
        if not ext:
            log.warning("Extension '%s' not found (available: %s)", name, list(extensions.keys()))
            continue
        ext_audience = ext["audience"]
        if (ext_audience == "all" or ext_audience == audience) and ext["text"]:
            parts.append(ext["text"])
    return "\n\n".join(parts)


def resolve_extension_hooks(ext_names: list[str], audience: str = "all") -> dict[str, str]:
    """Resolve flowchart hook registrations from loaded extensions.

    Scans extensions' meta.json for hooks field, filtered by audience.
    Returns {hook_name: comma_separated_command_names} for each hook point.

    Hook points: pre_task, execute, post_task, post_respond
    """
    extensions = _load_extensions()
    hook_points: dict[str, list[str]] = {}
    for name in ext_names:
        ext = extensions.get(name)
        if not ext:
            continue
        ext_audience = ext["audience"]
        if ext_audience != "all" and ext_audience != audience:
            continue
        for hook_name, command_name in ext.get("hooks", {}).items():
            hook_points.setdefault(hook_name, []).append(command_name)
    return {k: ",".join(v) for k, v in hook_points.items()}


def resolve_prompt_hooks(ext_names: list[str], audience: str = "all") -> dict[str, str]:
    """Resolve in-prompt hook text from loaded extensions.

    Scans extensions' meta.json for prompt_hooks field, filtered by audience.
    Each prompt_hook maps a hook name to a file (relative to the extension dir)
    whose content gets appended to the corresponding prompt block in the flowchart.

    Returns {hook_name: combined_text} with text from all matching extensions.
    """
    extensions = _load_extensions()
    hook_texts: dict[str, list[str]] = {}
    for name in ext_names:
        ext = extensions.get(name)
        if not ext:
            continue
        ext_audience = ext["audience"]
        if ext_audience != "all" and ext_audience != audience:
            continue
        for hook_name, file_path in ext.get("prompt_hooks", {}).items():
            full_path = os.path.join(EXTENSIONS_DIR, name, file_path)
            try:
                with open(full_path, encoding="utf-8") as f:
                    text = f.read().strip()
                if text:
                    hook_texts.setdefault(hook_name, []).append(text)
            except FileNotFoundError:
                log.warning("Prompt hook file not found: %s (extension '%s')", full_path, name)
            except Exception:
                log.exception("Failed to read prompt hook file: %s", full_path)
    return {k: "\n\n".join(v) for k, v in hook_texts.items()}


def resolve_extension_sandbox(ext_names: list[str], audience: str = "all") -> tuple[list[str], list[str]]:
    """Resolve sandbox customizations from loaded extensions.

    Scans extensions' meta.json for sandbox field, filtered by audience.
    The sandbox field can contain:
      - excluded_commands: list of commands to exclude from sandbox
      - write_dirs: list of directories to add to write allowlist (~ expanded)

    Returns (excluded_commands, write_dirs) merged from all matching extensions.
    """
    extensions = _load_extensions()
    excluded: list[str] = []
    write_dirs: list[str] = []
    for name in ext_names:
        ext = extensions.get(name)
        if not ext:
            continue
        ext_audience = ext["audience"]
        if ext_audience != "all" and ext_audience != audience:
            continue
        sandbox = ext.get("sandbox", {})
        excluded.extend(sandbox.get("excluded_commands", []))
        write_dirs.extend(os.path.expanduser(d) for d in sandbox.get("write_dirs", []))
    return excluded, write_dirs


def sync_extension_commands() -> None:
    """Symlink extension flowchart commands into the main commands/ directory.

    Scans extensions/<name>/commands/*.json and creates symlinks in commands/
    so FlowCoder's StorageService can discover them without modification.
    Existing symlinks are updated; real files are never overwritten.
    """
    commands_dir = os.path.join(config.BOT_DIR, "commands")
    if not os.path.isdir(EXTENSIONS_DIR):
        return
    for ext_name in os.listdir(EXTENSIONS_DIR):
        ext_cmds = os.path.join(EXTENSIONS_DIR, ext_name, "commands")
        if not os.path.isdir(ext_cmds):
            continue
        for fname in os.listdir(ext_cmds):
            if not fname.endswith(".json"):
                continue
            src = os.path.join(ext_cmds, fname)
            dst = os.path.join(commands_dir, fname)
            if os.path.islink(dst):
                os.unlink(dst)
            elif os.path.exists(dst):
                log.warning("Extension command '%s' conflicts with existing command — skipping", fname)
                continue
            os.symlink(src, dst)
            log.info("Linked extension command: %s -> %s", fname, src)
