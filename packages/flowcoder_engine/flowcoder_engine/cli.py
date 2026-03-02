"""CLI argument parsing for the flowcoder-engine binary.

Flowcoder-engine is a transparent proxy for claude CLI.  It only parses
its own flags (--search-path, --claude-path, --max-blocks) and passes
everything else through to the inner claude subprocess.
"""

from __future__ import annotations

import argparse
import shlex
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from flowcoder_flowchart import Argument


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse CLI arguments.

    Only flowcoder-specific flags are parsed.  All other arguments
    (--model, --system-prompt, --verbose, etc.) are collected in
    args.passthrough and forwarded to the inner claude process.
    """
    parser = argparse.ArgumentParser(
        prog="flowcoder-engine",
        description="Claude CLI proxy with flowchart execution",
    )

    parser.add_argument(
        "--claude-path",
        help="Path to the claude CLI binary (auto-detected if not specified)",
    )
    parser.add_argument(
        "--search-path",
        action="append",
        dest="search_paths",
        help="Search paths for flowchart command resolution (can specify multiple)",
    )
    parser.add_argument(
        "--max-blocks",
        type=int,
        default=1000,
        help="Maximum number of blocks to execute per flowchart (safety limit)",
    )

    args, remaining = parser.parse_known_args(argv)
    args.passthrough = remaining
    return args


def build_variables(
    args_string: str,
    declared_args: list[Argument],
) -> dict[str, Any]:
    """Build initial variable dict from a shell-style args string.

    Maps positional args to $1, $2, etc. and to declared argument names.
    """
    if args_string.strip():
        parts = shlex.split(args_string)
    else:
        parts = []

    variables: dict[str, Any] = {}

    for i, arg_def in enumerate(declared_args):
        pos = i + 1
        if i < len(parts):
            variables[f"${pos}"] = parts[i]
            variables[arg_def.name] = parts[i]
        elif arg_def.default is not None:
            variables[f"${pos}"] = arg_def.default
            variables[arg_def.name] = arg_def.default
        elif arg_def.required:
            raise ValueError(
                f"Missing required argument: {arg_def.name} (position {pos})"
            )

    # Extra positional beyond declared
    for i in range(len(declared_args), len(parts)):
        variables[f"${i + 1}"] = parts[i]

    return variables
