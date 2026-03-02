"""Flowcoder engine helpers — binary resolution, CLI arg building, env construction."""

from __future__ import annotations

import json
import logging
import os
import shutil
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from claude_agent_sdk.types import ClaudeAgentOptions

log = logging.getLogger(__name__)

# ------------------------------------------------------------------
# Shared helpers
# ------------------------------------------------------------------

_FLOWCODER_HOME = os.environ.get(
    "FLOWCODER_HOME",
    os.path.expanduser("~/flowcoder-rewrite"),
)


def get_engine_binary() -> str:
    """Resolve the flowcoder-engine binary path."""
    engine_bin = shutil.which("flowcoder-engine")
    if engine_bin:
        return engine_bin
    return os.path.join(
        _FLOWCODER_HOME, "packages", "flowcoder-engine", ".venv", "bin", "flowcoder-engine",
    )


def get_search_paths(extra: list[str] | None = None) -> list[str]:
    """Return flowchart command search paths."""
    default_search = os.path.join(_FLOWCODER_HOME, "examples", "commands")
    return [default_search] + (extra or [])


def build_engine_cmd(
    search_paths: list[str] | None = None,
) -> list[str]:
    """Build flowcoder-engine argv (binary resolution + flags).

    The engine is a persistent proxy — it waits for user messages on stdin
    and intercepts slash commands matching known flowcharts. The command
    and args are sent as a user message after starting, not as CLI flags.
    """
    cmd: list[str] = [get_engine_binary()]
    for sp in get_search_paths(search_paths):
        cmd += ["--search-path", sp]
    return cmd


def build_engine_env() -> dict[str, str]:
    """Build clean env (strip CLAUDECODE/SDK vars)."""
    return {
        k: v
        for k, v in os.environ.items()
        if k not in ("CLAUDECODE", "CLAUDE_AGENT_SDK_VERSION", "CLAUDE_CODE_ENTRYPOINT")
    }


def build_engine_cli_args(options: ClaudeAgentOptions) -> list[str]:
    """Build full flowcoder-engine CLI args from agent options.

    Produces the command that BridgeTransport.spawn() passes to procmux.
    The engine parses --search-path and --max-blocks; everything else is
    forwarded as passthrough args to inner Claude via parse_known_args.

    Mirrors the essential flags from SubprocessCLITransport._build_command().
    """
    cmd: list[str] = [get_engine_binary()]

    # Engine-specific flags
    for sp in get_search_paths():
        cmd += ["--search-path", sp]

    # -- Claude passthrough flags (engine forwards to inner Claude) --

    cmd += ["--output-format", "stream-json", "--verbose"]

    # System prompt
    if options.system_prompt is None:
        cmd += ["--system-prompt", ""]
    elif isinstance(options.system_prompt, str):
        cmd += ["--system-prompt", options.system_prompt]
    else:
        if options.system_prompt.get("type") == "preset" and "append" in options.system_prompt:
            cmd += ["--append-system-prompt", options.system_prompt["append"]]

    if options.model:
        cmd += ["--model", options.model]

    if options.fallback_model:
        cmd += ["--fallback-model", options.fallback_model]

    if options.permission_mode:
        cmd += ["--permission-mode", options.permission_mode]

    if options.resume:
        cmd += ["--resume", options.resume]

    if options.disallowed_tools:
        cmd += ["--disallowedTools", ",".join(options.disallowed_tools)]

    # MCP servers
    if options.mcp_servers and isinstance(options.mcp_servers, dict):
        servers_for_cli: dict[str, Any] = {}
        for name, cfg in options.mcp_servers.items():
            if cfg.get("type") == "sdk":
                servers_for_cli[name] = {k: v for k, v in cfg.items() if k != "instance"}
            else:
                servers_for_cli[name] = cfg
        if servers_for_cli:
            cmd += ["--mcp-config", json.dumps({"mcpServers": servers_for_cli})]

    if options.include_partial_messages:
        cmd.append("--include-partial-messages")

    # Thinking / effort
    resolved_max_thinking = options.max_thinking_tokens
    if options.thinking is not None:
        t = options.thinking
        if t["type"] == "adaptive":
            if resolved_max_thinking is None:
                resolved_max_thinking = 32_000
        elif t["type"] == "enabled":
            resolved_max_thinking = t["budget_tokens"]
        elif t["type"] == "disabled":
            resolved_max_thinking = 0
    if resolved_max_thinking is not None:
        cmd += ["--max-thinking-tokens", str(resolved_max_thinking)]

    if options.effort is not None:
        cmd += ["--effort", options.effort]

    # Settings / sandbox
    settings_obj: dict[str, Any] = {}
    if options.settings is not None:
        s = options.settings.strip()
        if s.startswith("{"):
            try:
                settings_obj = json.loads(s)
            except json.JSONDecodeError:
                pass
    if options.sandbox is not None:
        settings_obj["sandbox"] = options.sandbox
    if settings_obj:
        cmd += ["--settings", json.dumps(settings_obj)]

    # Setting sources
    sources_value = ",".join(options.setting_sources) if options.setting_sources is not None else ""
    cmd += ["--setting-sources", sources_value]

    # Extra args passthrough
    for flag, value in options.extra_args.items():
        if value is None:
            cmd.append(f"--{flag}")
        else:
            cmd += [f"--{flag}", str(value)]

    # Always last
    cmd += ["--input-format", "stream-json"]

    return cmd
