#!/usr/bin/env python3
"""CLI entry point for flowcoder TUI.

Starts an interactive REPL by default. Flowcharts are triggered via
the /flowchart slash command inside the session.

Usage:
    flowcoder                              # start interactive REPL
    flowcoder --model sonnet --verbose     # start REPL with options
"""

from __future__ import annotations

import argparse
import asyncio
import sys

from .repl import Repl


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Interactive coding assistant with flowchart support",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""\
Examples:
  flowcoder                    Start interactive REPL (default model: haiku)
  flowcoder --model sonnet     Start REPL with sonnet model
  flowcoder -v                 Start REPL with verbose output

Inside the REPL:
  > hello                      Chat with Claude
  > /flowchart story "a cat"   Run a flowchart
  > /list                      List available flowcharts
  > /help                      Show all commands""",
    )
    parser.add_argument(
        "--model", default="haiku", help="Model to use (default: haiku)"
    )
    parser.add_argument(
        "--verbose", "-v", action="store_true", help="Show execution details"
    )
    parser.add_argument(
        "--commands-dir",
        default=None,
        help="Directory containing command JSON files",
    )

    opts = parser.parse_args()

    repl = Repl(
        model=opts.model,
        commands_dir=opts.commands_dir,
        verbose=opts.verbose,
    )

    try:
        asyncio.run(repl.run())
    except KeyboardInterrupt:
        pass

    sys.stderr.write("\n")
    sys.stderr.flush()


if __name__ == "__main__":
    main()
