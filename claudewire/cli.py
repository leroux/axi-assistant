"""CLI argument building for Claude Code.

Constructs the CLI command, environment, and working directory from
ClaudeAgentOptions, staying in sync with the SDK's internal logic.
"""

from __future__ import annotations

import os
from typing import Any


def build_cli_spawn_args(options: Any) -> tuple[list[str], dict[str, str], str]:
    """Build CLI command, env, and cwd from ClaudeAgentOptions.

    Uses SubprocessCLITransport._build_command() to stay in sync with
    the SDK's command building logic.
    """
    from dataclasses import replace

    from claude_agent_sdk._internal.transport.subprocess_cli import SubprocessCLITransport
    from claude_agent_sdk._version import __version__ as sdk_version

    # Replicate the permission_prompt_tool_name injection from
    # ClaudeSDKClient.connect() (client.py ~line 122).  In direct mode,
    # connect() sets this before creating SubprocessCLITransport; in bridge
    # mode the CLI is already spawned by the time connect() runs, so we must
    # apply the same logic here.
    if getattr(options, "can_use_tool", None) and not options.permission_prompt_tool_name:
        options = replace(options, permission_prompt_tool_name="stdio")

    # Create temp transport just for _build_command() -- no side effects
    temp = SubprocessCLITransport(prompt="", options=options)
    cmd = temp._build_command()  # pyright: ignore[reportPrivateUsage]

    # Replicate env construction from SubprocessCLITransport.connect()
    env: dict[str, str] = {
        **os.environ,
        **(options.env or {}),
        "CLAUDE_CODE_ENTRYPOINT": "sdk-py",
        "CLAUDE_AGENT_SDK_VERSION": sdk_version,
    }
    if getattr(options, "enable_file_checkpointing", False):
        env["CLAUDE_CODE_ENABLE_SDK_FILE_CHECKPOINTING"] = "true"
    cwd_path = str(options.cwd or os.getcwd())
    if cwd_path:
        env["PWD"] = cwd_path

    return cmd, env, cwd_path
