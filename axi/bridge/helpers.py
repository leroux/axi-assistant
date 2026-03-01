"""Bridge helpers -- compatibility shim.

build_cli_spawn_args has moved to the claudewire package.
This module re-exports it for backward compatibility.
"""

from claudewire.cli import build_cli_spawn_args

__all__ = ["build_cli_spawn_args"]
