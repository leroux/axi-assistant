"""Interactive REPL for flowcoder.

Provides a persistent session with two modes:
- Chat mode (default): freeform messages relayed to Claude
- Flowchart mode: /flowchart <name> [args] triggers flowchart execution

Conversation history carries through both modes.
"""

from __future__ import annotations

import asyncio
import shlex
import sys
import time
from pathlib import Path
from typing import Any

from flowcoder_engine.session import Session
from flowcoder_engine.subprocess import find_claude
from flowcoder_engine.walker import GraphWalker
from flowcoder_flowchart import load_command, validate

from .protocol import TuiProtocol

_VERSION = "0.1.0"


def _resolve_commands_dir(path: str | None) -> Path:
    """Resolve the commands directory, checking common locations."""
    default = Path("examples") / "commands"
    if path:
        return Path(path)
    if default.exists():
        return default
    repo = Path(__file__).resolve().parent.parent.parent.parent.parent
    candidate = repo / "examples" / "commands"
    if candidate.exists():
        return candidate
    return default


class Repl:
    """Interactive REPL — chat with Claude, run flowcharts via slash commands."""

    def __init__(
        self,
        model: str = "haiku",
        commands_dir: str | None = None,
        verbose: bool = False,
    ) -> None:
        self._model = model
        self._commands_dir = _resolve_commands_dir(commands_dir)
        self._verbose = verbose
        self._session: Session | None = None
        self._protocol: TuiProtocol | None = None

    async def run(self) -> None:
        """Main REPL loop."""
        claude_path = find_claude()
        claude_cmd = [
            claude_path,
            "-p",
            "--input-format", "stream-json",
            "--output-format", "stream-json",
            "--model", self._model,
            "--dangerously-skip-permissions",
        ]
        self._protocol = TuiProtocol(verbose=self._verbose)
        self._session = Session("repl", claude_cmd, protocol=self._protocol)
        await self._session.start()
        self._print_banner()

        try:
            while True:
                try:
                    line = await self._read_input()
                except EOFError:
                    break

                if line is None:
                    break
                line = line.strip()
                if not line:
                    continue

                if line.startswith("/"):
                    should_quit = await self._handle_command(line)
                    if should_quit:
                        break
                else:
                    await self._handle_chat(line)
        except KeyboardInterrupt:
            pass
        finally:
            if self._session:
                await self._session.stop()

    async def _read_input(self) -> str | None:
        """Read a line of input without blocking the event loop."""
        loop = asyncio.get_event_loop()
        try:
            return await loop.run_in_executor(None, lambda: input("> "))
        except EOFError:
            return None

    async def _handle_chat(self, message: str) -> None:
        """Send message to Claude, streaming response shown via protocol."""
        assert self._session is not None
        try:
            await self._session.query(message)
            sys.stderr.write("\n\n")
            sys.stderr.flush()
        except KeyboardInterrupt:
            sys.stderr.write("\n")
            sys.stderr.flush()

    async def _handle_command(self, line: str) -> bool:
        """Dispatch slash commands. Returns True if the REPL should exit."""
        parts = line.split(None, 1)
        cmd = parts[0].lower()
        rest = parts[1] if len(parts) > 1 else ""

        match cmd:
            case "/quit" | "/exit":
                return True
            case "/help":
                self._print_help()
            case "/clear":
                await self._cmd_clear()
            case "/model":
                await self._cmd_model(rest.strip())
            case "/cost":
                self._cmd_cost()
            case "/flowchart":
                await self._cmd_flowchart(rest.strip())
            case "/list":
                self._cmd_list()
            case _:
                sys.stderr.write(f"Unknown command: {cmd}\n")
                sys.stderr.write("Type /help for available commands.\n\n")
        return False

    async def _cmd_clear(self) -> None:
        """Reset conversation history."""
        assert self._session is not None
        await self._session.clear()
        sys.stderr.write("Conversation cleared.\n\n")
        sys.stderr.flush()

    async def _cmd_model(self, name: str) -> None:
        """Switch model (restarts session)."""
        if not name:
            sys.stderr.write(f"Current model: {self._model}\n\n")
            sys.stderr.flush()
            return
        self._model = name
        assert self._session is not None
        # Update the --model arg in the command list
        cmd = self._session._claude_cmd
        if "--model" in cmd:
            idx = cmd.index("--model")
            cmd[idx + 1] = name
        else:
            cmd.extend(["--model", name])
        await self._session.clear()
        sys.stderr.write(f"Switched to model: {name}\n\n")
        sys.stderr.flush()

    def _cmd_cost(self) -> None:
        """Show total session cost so far."""
        assert self._session is not None
        cost = self._session.total_cost
        sys.stderr.write(f"Total session cost: ${cost:.4f}\n\n")
        sys.stderr.flush()

    def _cmd_list(self) -> None:
        """List available flowchart commands."""
        if not self._commands_dir.exists():
            sys.stderr.write(f"Commands directory not found: {self._commands_dir}\n\n")
            sys.stderr.flush()
            return

        sys.stderr.write("\nAvailable flowcharts:\n\n")
        for f in sorted(self._commands_dir.glob("*.json")):
            try:
                cmd = load_command(f)
                args_str = " ".join(
                    f"<{a.name}>" if a.required else f"[{a.name}]"
                    for a in cmd.arguments
                )
                sys.stderr.write(f"  {cmd.name:12s} {args_str}\n")
                if cmd.description:
                    sys.stderr.write(f"  {'':12s} {cmd.description}\n")
                sys.stderr.write("\n")
            except Exception as e:
                sys.stderr.write(f"  {f.stem:12s} (error: {e})\n")
        sys.stderr.flush()

    async def _cmd_flowchart(self, args_str: str) -> None:
        """Load and run a flowchart command."""
        assert self._session is not None
        assert self._protocol is not None

        if not args_str:
            sys.stderr.write("Usage: /flowchart <name> [args...]\n")
            sys.stderr.write("Use /list to see available flowcharts.\n\n")
            sys.stderr.flush()
            return

        # Parse: name arg1 "arg 2" ...
        try:
            tokens = shlex.split(args_str)
        except ValueError:
            tokens = args_str.split()

        name = tokens[0]
        args = tokens[1:]

        # Find the command
        cmd_file = self._commands_dir / f"{name}.json"
        if not cmd_file.exists():
            sys.stderr.write(f"Flowchart '{name}' not found in {self._commands_dir}\n")
            sys.stderr.write("Use /list to see available flowcharts.\n\n")
            sys.stderr.flush()
            return

        try:
            cmd = load_command(cmd_file)
        except Exception as e:
            sys.stderr.write(f"Error loading flowchart: {e}\n\n")
            sys.stderr.flush()
            return

        # Validate
        result = validate(cmd.flowchart)
        if not result.valid:
            sys.stderr.write(f"Validation errors: {result.errors}\n\n")
            sys.stderr.flush()
            return

        # Check required args
        required = [a for a in cmd.arguments if a.required]
        if len(args) < len(required):
            missing = required[len(args):]
            names = ", ".join(a.name for a in missing)
            usage_args = " ".join(
                f"<{a.name}>" if a.required else f"[{a.name}]"
                for a in cmd.arguments
            )
            sys.stderr.write(f"Missing required arguments: {names}\n")
            sys.stderr.write(f"Usage: /flowchart {name} {usage_args}\n\n")
            sys.stderr.flush()
            return

        # Build variables from positional args
        variables: dict[str, Any] = {}
        for i, val in enumerate(args, 1):
            variables[f"${i}"] = val
        for i, arg_def in enumerate(cmd.arguments):
            if i < len(args):
                variables[arg_def.name] = args[i]
            elif arg_def.default is not None:
                variables[f"${i+1}"] = arg_def.default
                variables[arg_def.name] = arg_def.default

        # Configure protocol for flowchart display
        args_display = " ".join(f'"{a}"' for a in args)
        self._protocol._command_name = cmd.name
        self._protocol._flowchart_name = cmd.flowchart.name or ""
        self._protocol._args_display = args_display
        self._protocol.set_total_blocks(len(cmd.flowchart.blocks))
        self._protocol._blocks_done = 0
        self._protocol._overall_start = time.monotonic()

        search_paths = [str(self._commands_dir)]

        try:
            # Print flowchart header
            self._protocol._print_header()

            walker = GraphWalker(
                cmd.flowchart,
                self._session,
                variables,
                self._protocol,
                search_paths=search_paths,
            )
            exec_result = await walker.run()

            # Print final output
            for entry in reversed(exec_result.log):
                bt = getattr(entry.block_type, "value", entry.block_type)
                if bt == "prompt" and entry.result.output:
                    print(entry.result.output)
                    break

            # Summary
            self._protocol.print_summary(
                status=exec_result.status,
                cost=self._session.total_cost,
                block_count=len(exec_result.log),
            )

            if exec_result.status != "completed":
                for entry in exec_result.log:
                    if entry.result.error:
                        sys.stderr.write(
                            f"  Error in {entry.block_name}: {entry.result.error}\n"
                        )

        except KeyboardInterrupt:
            sys.stderr.write("\nFlowchart execution cancelled.\n\n")
            sys.stderr.flush()
        finally:
            # Reset protocol state for chat mode
            self._protocol._command_name = ""
            self._protocol._flowchart_name = ""
            self._protocol._args_display = ""
            self._protocol._total_blocks = 0
            self._protocol._blocks_done = 0
            self._protocol._current_block_id = None

        sys.stderr.write("\n")
        sys.stderr.flush()

    def _print_banner(self) -> None:
        """Print the startup banner."""
        is_tty = hasattr(sys.stderr, "isatty") and sys.stderr.isatty()
        if is_tty:
            dim = "\033[2m"
            bold = "\033[1m"
            reset = "\033[0m"
        else:
            dim = bold = reset = ""

        sys.stderr.write(
            f"\n{bold}flowcoder{reset} v{_VERSION} {dim}\u00b7 model: {self._model}{reset}\n"
        )
        sys.stderr.write(
            f"{dim}Type /help for commands, Ctrl+D to exit.{reset}\n\n"
        )
        sys.stderr.flush()

    def _print_help(self) -> None:
        """Print available commands."""
        sys.stderr.write("\nCommands:\n\n")
        sys.stderr.write("  /flowchart <name> [args]  Run a flowchart command\n")
        sys.stderr.write("  /list                     List available flowcharts\n")
        sys.stderr.write("  /model [name]             Show or switch model\n")
        sys.stderr.write("  /clear                    Clear conversation history\n")
        sys.stderr.write("  /cost                     Show session cost\n")
        sys.stderr.write("  /help                     Show this help\n")
        sys.stderr.write("  /quit                     Exit the REPL\n")
        sys.stderr.write("\n")
        sys.stderr.write("Or just type a message to chat with Claude.\n\n")
        sys.stderr.flush()
