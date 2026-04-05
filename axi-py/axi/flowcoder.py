"""Flowcoder engine helpers — binary resolution, CLI arg building, env construction.

Builds CLI args and env for spawning the flowcoder-engine process.
Replicates the exact env vars and CLI flags that claude-code-sdk's
SubprocessCLITransport sets, without importing from the SDK.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
from typing import TYPE_CHECKING, Any, cast

if TYPE_CHECKING:
    from claude_agent_sdk.types import ClaudeAgentOptions

log = logging.getLogger(__name__)


_SDK_VERSION = "0.1.39"


# ------------------------------------------------------------------
# Shared helpers
# ------------------------------------------------------------------


def _default_commands_dir() -> str:
    """Derive the default commands dir from the installed flowcoder_engine package."""
    import flowcoder_engine

    pkg_dir = os.path.dirname(os.path.dirname(flowcoder_engine.__file__))
    return os.path.join(pkg_dir, "examples", "commands")


def get_engine_binary() -> str:
    """Resolve the flowcoder-engine binary path."""
    engine_bin = shutil.which("flowcoder-engine")
    if engine_bin:
        return engine_bin
    raise FileNotFoundError(
        "flowcoder-engine not found on PATH. Is the flowcoder-engine package installed?"
    )


def get_search_paths(extra: list[str] | None = None) -> list[str]:
    """Return flowchart command search paths."""
    from axi import config

    default_search = _default_commands_dir()
    bot_commands = os.path.join(config.BOT_DIR, "commands")
    env_raw = os.environ.get("FLOWCODER_SEARCH_PATH", "")
    env_paths = [p for p in env_raw.split(":") if p]
    return [default_search, bot_commands] + env_paths + (extra or [])


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


def build_engine_env(options: ClaudeAgentOptions | None = None) -> dict[str, str]:
    """Build env for the flowcoder-engine process.

    Replicates the exact env vars from claude-code-sdk's
    SubprocessCLITransport.connect() so the engine can propagate them
    to the inner Claude CLI for SDK control protocol support.

    Env var chain:
      os.environ → options.env → SDK required vars → strip CLAUDECODE
    """
    env = dict(os.environ)

    # Merge user-provided env vars (from ClaudeAgentOptions.env)
    if options and getattr(options, "env", None):
        env.update(options.env)

    # SDK control protocol vars — exact match of what SubprocessCLITransport
    # sets in connect(). Without these, inner Claude auto-denies tool
    # permissions in pipe mode.
    env["CLAUDE_CODE_ENTRYPOINT"] = "sdk-py"
    env["CLAUDE_AGENT_SDK_VERSION"] = _SDK_VERSION

    # File checkpointing (matches SDK: enable_file_checkpointing option)
    if options and getattr(options, "enable_file_checkpointing", False):
        env["CLAUDE_CODE_ENABLE_SDK_FILE_CHECKPOINTING"] = "true"

    # CWD propagation (matches SDK: sets PWD if cwd is specified)
    if options and getattr(options, "cwd", None):
        env["PWD"] = str(options.cwd)

    # Strip nested-session guard — the engine IS the outer session
    env.pop("CLAUDECODE", None)

    return env


def _build_claude_cli_args(options: Any) -> list[str]:
    """Build Claude CLI flags from ClaudeAgentOptions.

    Replicates the exact CLI flag logic from claude-code-sdk's
    SubprocessCLITransport._build_command(), without importing from the SDK.

    The engine will receive these as passthrough args via parse_known_args
    and forward them to the inner Claude process.
    """
    # Base flags — always present (matches SDK)
    cmd: list[str] = [
        "--output-format", "stream-json",
        "--verbose",
    ]

    # System prompt
    sp = getattr(options, "system_prompt", None)
    if sp is None:
        cmd.extend(["--system-prompt", ""])
    elif isinstance(sp, str):
        cmd.extend(["--system-prompt", sp])
    elif isinstance(sp, dict):
        sp_d = cast("dict[str, Any]", sp)
        if sp_d.get("type") == "preset" and "append" in sp_d:
            cmd.extend(["--append-system-prompt", str(sp_d["append"])])

    # Tools base set
    tools: list[str] | str | None = getattr(options, "tools", None)
    if tools is not None:
        if isinstance(tools, list):
            cmd.extend(["--tools", ",".join(tools) if tools else ""])
        else:
            cmd.extend(["--tools", "default"])

    # Allowed / disallowed tools
    if getattr(options, "allowed_tools", None):
        cmd.extend(["--allowedTools", ",".join(options.allowed_tools)])
    if getattr(options, "disallowed_tools", None):
        cmd.extend(["--disallowedTools", ",".join(options.disallowed_tools)])

    # Max turns / budget
    if getattr(options, "max_turns", None):
        cmd.extend(["--max-turns", str(options.max_turns)])
    if getattr(options, "max_budget_usd", None) is not None:
        cmd.extend(["--max-budget-usd", str(options.max_budget_usd)])

    # Model
    model: str | None = getattr(options, "model", None)
    if model:
        cmd.extend(["--model", model])
    fallback_model: str | None = getattr(options, "fallback_model", None)
    if fallback_model:
        cmd.extend(["--fallback-model", fallback_model])

    # Betas
    if getattr(options, "betas", None):
        cmd.extend(["--betas", ",".join(options.betas)])

    # Permission prompt tool — for embedded mode, the engine handles
    # control protocol itself, so we set "stdio" if can_use_tool is set
    ppt_name: str | None = getattr(options, "permission_prompt_tool_name", None)
    if ppt_name:
        cmd.extend(["--permission-prompt-tool", ppt_name])
    elif getattr(options, "can_use_tool", None):
        cmd.extend(["--permission-prompt-tool", "stdio"])

    # Permission mode
    perm_mode: str | None = getattr(options, "permission_mode", None)
    if perm_mode:
        cmd.extend(["--permission-mode", perm_mode])

    # Continue / resume
    if getattr(options, "continue_conversation", False):
        cmd.append("--continue")
    resume: str | None = getattr(options, "resume", None)
    if resume:
        cmd.extend(["--resume", resume])

    # Settings (sandbox merged in) — matches SDK's _build_settings_value()
    _add_settings_flags(cmd, options)

    # Additional directories
    if getattr(options, "add_dirs", None):
        for d in options.add_dirs:
            cmd.extend(["--add-dir", str(d)])

    # MCP servers — filter out SDK-type servers (engine doesn't support
    # the SDK MCP initialize handshake)
    mcp = getattr(options, "mcp_servers", None)
    if mcp:
        if isinstance(mcp, dict):
            mcp_d = cast("dict[str, Any]", mcp)
            servers_for_cli: dict[str, Any] = {}
            for name, srv_config in mcp_d.items():
                if isinstance(srv_config, dict) and cast("dict[str, Any]", srv_config).get("type") == "sdk":
                    # Skip SDK MCP servers — engine can't do SDK handshake
                    continue
                servers_for_cli[name] = srv_config
            if servers_for_cli:
                cmd.extend(["--mcp-config", json.dumps({"mcpServers": servers_for_cli})])
        else:
            cmd.extend(["--mcp-config", str(mcp)])

    # Partial messages
    if getattr(options, "include_partial_messages", False):
        cmd.append("--include-partial-messages")

    # Fork session
    if getattr(options, "fork_session", False):
        cmd.append("--fork-session")

    # Setting sources
    sources = getattr(options, "setting_sources", None)
    sources_val = ",".join(sources) if sources is not None else ""
    cmd.extend(["--setting-sources", sources_val])

    # Plugins
    if getattr(options, "plugins", None):
        for plugin in options.plugins:
            if plugin["type"] == "local":
                cmd.extend(["--plugin-dir", plugin["path"]])

    # Extra args (matches SDK: flag → value or flag-only for None)
    if getattr(options, "extra_args", None):
        for flag, value in options.extra_args.items():
            if value is None:
                cmd.append(f"--{flag}")
            else:
                cmd.extend([f"--{flag}", str(value)])

    # Thinking tokens — resolved exactly like the SDK
    resolved_max_thinking = getattr(options, "max_thinking_tokens", None)
    thinking: dict[str, Any] | None = getattr(options, "thinking", None)
    if thinking is not None:
        t_type = thinking.get("type", "")
        if t_type == "adaptive":
            if resolved_max_thinking is None:
                resolved_max_thinking = 32_000
        elif t_type == "enabled":
            resolved_max_thinking = thinking["budget_tokens"]
        elif t_type == "disabled":
            resolved_max_thinking = 0
    if resolved_max_thinking is not None:
        cmd.extend(["--max-thinking-tokens", str(resolved_max_thinking)])

    # Effort
    effort: str | None = getattr(options, "effort", None)
    if effort:
        cmd.extend(["--effort", effort])

    # Output format / JSON schema
    output_fmt = getattr(options, "output_format", None)
    if isinstance(output_fmt, dict):
        fmt_d = cast("dict[str, Any]", output_fmt)
        if fmt_d.get("type") == "json_schema":
            schema: Any = fmt_d.get("schema")
            if schema is not None:
                cmd.extend(["--json-schema", json.dumps(schema)])

    # Input format — always stream-json (matches SDK)
    cmd.extend(["--input-format", "stream-json"])

    return cmd


def _add_settings_flags(cmd: list[str], options: Any) -> None:
    """Build --settings flag from sandbox + settings options (matches SDK)."""
    settings: dict[str, Any] = {}

    # Sandbox → settings merge (matches SDK _build_settings_value)
    sandbox = getattr(options, "sandbox", None)
    if sandbox and isinstance(sandbox, dict):
        settings["sandbox"] = sandbox

    if settings:
        cmd.extend(["--settings", json.dumps(settings)])


def build_engine_cli_args(options: ClaudeAgentOptions) -> list[str]:
    """Build full flowcoder-engine CLI args from agent options.

    Produces the command that BridgeTransport.spawn() passes to procmux.
    The engine parses --search-path and --max-blocks; everything else is
    forwarded as passthrough args to inner Claude via parse_known_args.

    Builds Claude CLI flags directly from ClaudeAgentOptions, matching the
    exact flag logic of claude-code-sdk's SubprocessCLITransport._build_command()
    without importing from the SDK.
    """
    # Engine-specific prefix
    cmd: list[str] = [get_engine_binary()]
    for sp in get_search_paths():
        cmd += ["--search-path", sp]

    # Claude CLI flags — built directly from options
    cmd += _build_claude_cli_args(options)

    return cmd
