"""stdio-spy: bidirectional stdio proxy/logger."""

from stdio_spy.claude_spy import main as claude_spy_main
from stdio_spy.proxy import main

__all__ = ["claude_spy_main", "main"]
