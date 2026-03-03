"""Flowcoder engine helpers — binary resolution, CLI arg building, env construction."""

from __future__ import annotations

import logging
import os
import shutil
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from claude_agent_sdk.types import ClaudeAgentOptions

log = logging.getLogger(__name__)

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
    default_search = _default_commands_dir()
    env_raw = os.environ.get("FLOWCODER_SEARCH_PATH", "")
    env_paths = [p for p in env_raw.split(":") if p]
    return [default_search] + env_paths + (extra or [])


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
    """Build env for the flowcoder-engine process.

    Only strips CLAUDECODE (nested-session guard).  The SDK vars are kept
    so the engine can propagate them to the inner Claude CLI, which needs
    them to use the SDK control protocol for tool permissions and MCP.
    """
    env = dict(os.environ)
    env.pop("CLAUDECODE", None)
    return env


def build_engine_cli_args(options: ClaudeAgentOptions) -> list[str]:
    """Build full flowcoder-engine CLI args from agent options.

    Produces the command that BridgeTransport.spawn() passes to procmux.
    The engine parses --search-path and --max-blocks; everything after is
    forwarded as passthrough args to inner Claude via parse_known_args.

    Delegates to claudewire's build_cli_spawn_args() (which calls the SDK's
    SubprocessCLITransport._build_command()) so claude flags stay in sync
    with the SDK automatically — no manual mirroring.
    """
    from claudewire.cli import build_cli_spawn_args

    # Engine-specific prefix
    cmd: list[str] = [get_engine_binary()]
    for sp in get_search_paths():
        cmd += ["--search-path", sp]

    # Claude flags — reuse the SDK's own command builder
    claude_cmd, _env, _cwd = build_cli_spawn_args(options)
    # Strip the binary path (claude_cmd[0] is the claude binary);
    # the engine will resolve its own inner claude binary.
    cmd += claude_cmd[1:]

    return cmd
