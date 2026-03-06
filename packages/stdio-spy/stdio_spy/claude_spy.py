"""claude-spy: convenience wrapper to run Claude CLI through stdio-spy."""

from __future__ import annotations

import os
import sys
import time

from stdio_spy.proxy import run_proxy


def main() -> None:
    capture_dir = os.path.expanduser("~/claude-captures")
    os.makedirs(capture_dir, exist_ok=True)
    timestamp = time.strftime("%Y%m%d_%H%M%S")
    log_path = os.path.join(capture_dir, f"{timestamp}.log")

    # Build claude command: claude [user args...]
    claude_args = ["claude", *sys.argv[1:]]

    sys.stderr.write(f"stdio-spy: logging to {log_path}\n")
    sys.exit(run_proxy(claude_args, log_path))
