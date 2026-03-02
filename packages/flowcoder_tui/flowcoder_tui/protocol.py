"""Terminal UI protocol for flowcoder-engine.

Progressive terminal output with spinners, streaming text from Claude,
and a summary. Stdlib-only -- uses ANSI escape codes for styling and
cursor control. All output goes to stderr so stdout stays clean.
"""

from __future__ import annotations

import asyncio
import atexit
import re
import sys
import time
from typing import Any

# Braille spinner frames
_SPINNER = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"

# ANSI escape sequences
_RESET = "\033[0m"
_BOLD = "\033[1m"
_DIM = "\033[2m"
_GREEN = "\033[32m"
_CYAN = "\033[36m"
_YELLOW = "\033[33m"
_BLUE = "\033[34m"
_MAGENTA = "\033[35m"
_RED = "\033[31m"
_WHITE = "\033[37m"
_HIDE_CURSOR = "\033[?25l"
_SHOW_CURSOR = "\033[?25h"
_ERASE_TO_END = "\033[J"

_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")

_BLOCK_COLORS: dict[str, str] = {
    "prompt": _CYAN,
    "branch": _YELLOW,
    "bash": _BLUE,
    "variable": _MAGENTA,
    "start": _GREEN,
    "end": _GREEN,
    "command": _WHITE,
    "refresh": _DIM,
}

_MAX_STREAM_LINES = 8
_SPINNER_FPS = 12


class TuiProtocol:
    """Protocol handler with terminal UI: spinners, streaming text, progress."""

    def __init__(
        self,
        command_name: str = "",
        flowchart_name: str = "",
        args_display: str = "",
        verbose: bool = False,
    ) -> None:
        self.verbose = verbose
        self._command_name = command_name
        self._flowchart_name = flowchart_name
        self._args_display = args_display

        self._total_blocks = 0
        self._blocks_done = 0
        self._current_block_id: str | None = None
        self._current_block_name: str = ""
        self._current_block_type: str = ""
        self._block_start_time: float = 0.0
        self._start_times: dict[str, float] = {}

        self._spinner_task: asyncio.Task[None] | None = None
        self._spinner_frame = 0

        self._stream_lines: list[str] = []
        self._stream_line_count = 0  # lines physically written on screen

        self._is_tty = hasattr(sys.stderr, "isatty") and sys.stderr.isatty()
        self._status_line_shown = False
        self._overall_start: float = 0.0

        if self._is_tty:
            atexit.register(_restore_cursor)

    def set_total_blocks(self, n: int) -> None:
        """Set the total block count for the [N/M] counter."""
        self._total_blocks = n

    # ── Protocol interface ────────────────────────────────────────

    def emit(self, msg: dict[str, Any]) -> None:
        pass

    def emit_block_start(
        self, block_id: str, block_name: str, block_type: str
    ) -> None:
        self._start_times[block_id] = time.monotonic()
        self._blocks_done += 1
        self._current_block_id = block_id
        self._current_block_name = block_name
        # Normalize enum (e.g. BlockType.PROMPT) to plain string ("prompt")
        self._current_block_type = getattr(block_type, "value", str(block_type))
        self._block_start_time = time.monotonic()
        self._stream_lines = []
        self._stream_line_count = 0

        self._write_status_line()
        self._status_line_shown = True

    def emit_block_complete(
        self, block_id: str, block_name: str, success: bool
    ) -> None:
        elapsed = 0.0
        if block_id in self._start_times:
            elapsed = time.monotonic() - self._start_times[block_id]

        # Clear the in-place active area (status line + stream lines)
        self._clear_active_area()

        # Print permanent completed line
        dur = _format_duration(elapsed)
        icon = _c(self._is_tty, _GREEN, "✓") if success else _c(self._is_tty, _RED, "✗")
        color = _BLOCK_COLORS.get(self._current_block_type, "")
        counter = _c(self._is_tty, _DIM, self._counter_str())
        type_str = _c(self._is_tty, color, f"({self._current_block_type})")
        dur_str = _c(self._is_tty, _DIM, f"[{dur}]")

        self._write(f"  {counter} {icon} {block_name} {type_str}  {dur_str}\n")

        # Show block output
        if self._stream_lines:
            pipe = _c(self._is_tty, _DIM, "\u2502")
            for line in self._stream_lines:
                self._write(f"        {pipe} {line}\n")

        self._current_block_id = None
        self._stream_lines = []
        self._stream_line_count = 0
        self._status_line_shown = False

    def emit_result(self, *args: Any, **kwargs: Any) -> None:
        pass

    def emit_forwarded(
        self,
        inner_msg: dict[str, Any],
        session_name: str,
        block_id: str,
        block_name: str,
    ) -> None:
        """Extract streaming text deltas and display.

        In chat mode (no active flowchart block), text is printed directly
        to stderr.  During flowchart execution, text is shown in the block
        progress area with spinners.
        """
        msg_type = inner_msg.get("type")

        if msg_type == "stream_event":
            event = inner_msg.get("event", {})
            if event.get("type") == "content_block_delta":
                delta = event.get("delta", {})
                if delta.get("type") == "text_delta":
                    text = delta.get("text", "")
                    if text:
                        self._emit_text(text)

        elif msg_type == "assistant":
            # Claude CLI sends incremental assistant messages with content
            # blocks rather than stream_event deltas.
            message = inner_msg.get("message", {})
            for block in message.get("content", []):
                if isinstance(block, dict) and block.get("type") == "text":
                    text = block.get("text", "")
                    if text:
                        self._emit_text(text)

        else:
            raise ValueError(
                f"TuiProtocol.emit_forwarded: unhandled message type {msg_type!r}"
            )

    def _emit_text(self, text: str) -> None:
        """Route text to the appropriate display mode."""
        if self._current_block_id is None:
            # Chat mode — stream directly to terminal
            sys.stderr.write(text)
            sys.stderr.flush()
        else:
            # Flowchart mode — block progress display
            self._append_stream_text(text)

    def log(self, message: str) -> None:
        if not self.verbose:
            return

        if self._is_tty and self._status_line_shown:
            # Clear active area, print log (permanent), restore active area
            self._clear_active_area()
            self._write(f"  {_DIM}[{message}]{_RESET}\n")
            # Restore status line + stream lines
            self._raw_write(self._build_status_line() + "\n")
            self._status_line_shown = True
            visible = self._stream_lines[-_MAX_STREAM_LINES:]
            self._stream_line_count = len(visible)
            for line in visible:
                self._raw_write(f"  {_DIM}      \u2502{_RESET} {line}\n")
        else:
            self._write(f"  {_c(self._is_tty, _DIM, f'[{message}]')}\n")

    async def start(self) -> None:
        """Print header and start the spinner task."""
        self._overall_start = time.monotonic()
        if self._is_tty:
            self._raw_write(_HIDE_CURSOR)
        self._print_header()
        if self._is_tty:
            self._spinner_task = asyncio.create_task(self._spinner_loop())

    async def stop(self) -> None:
        """Stop the spinner and restore cursor visibility."""
        if self._spinner_task:
            self._spinner_task.cancel()
            try:
                await self._spinner_task
            except asyncio.CancelledError:
                pass
            self._spinner_task = None
        if self._is_tty:
            self._raw_write(_SHOW_CURSOR)

    async def forward_control_request(self, inner_request: dict[str, Any]) -> dict[str, Any]:
        return {
            "type": "control_response",
            "response": {
                "request_id": inner_request.get("request_id", ""),
                "allowed": True,
            },
        }

    # ── Summary ───────────────────────────────────────────────────

    def print_summary(
        self, status: str, cost: float, block_count: int
    ) -> None:
        elapsed = (
            time.monotonic() - self._overall_start if self._overall_start else 0
        )
        dur = _format_duration(elapsed)
        icon = (
            _c(self._is_tty, _GREEN, "✓")
            if status == "completed"
            else _c(self._is_tty, _RED, "✗")
        )
        sep = _c(self._is_tty, _DIM, "\u2500" * 38)

        self._write("\n")
        self._write(f"  {sep}\n")
        self._write(
            f"  {icon} {status} in {dur} \u00b7 "
            f"Cost: ${cost:.4f} \u00b7 {block_count} blocks\n"
        )
        self._write(f"  {sep}\n")

    # ── Header ────────────────────────────────────────────────────

    def _print_header(self) -> None:
        sep = _c(self._is_tty, _DIM, "\u2500" * 38)
        self._write("\n")
        self._write(f"  {sep}\n")
        if self._command_name:
            name = _c(self._is_tty, _BOLD, self._command_name)
            line = f"  {name}"
            if self._args_display:
                line += f" {self._args_display}"
            self._write(line + "\n")
        if self._flowchart_name:
            fc = _c(self._is_tty, _DIM, f"Flowchart: {self._flowchart_name}")
            self._write(f"  {fc}\n")
        self._write(f"  {sep}\n")
        self._write("\n")

    # ── Spinner ───────────────────────────────────────────────────

    async def _spinner_loop(self) -> None:
        """Background task that redraws the active area at ~12 FPS."""
        try:
            while True:
                await asyncio.sleep(1 / _SPINNER_FPS)
                if self._current_block_id and self._status_line_shown:
                    self._spinner_frame = (self._spinner_frame + 1) % len(
                        _SPINNER
                    )
                    self._redraw_active_area()
        except asyncio.CancelledError:
            pass

    def _redraw_active_area(self) -> None:
        """Redraw status line + stream lines in-place (TTY only)."""
        if not self._is_tty or not self._current_block_id:
            return

        # Move cursor to the top of the active area
        lines_up = 1 + self._stream_line_count
        if lines_up > 0:
            self._raw_write(f"\033[{lines_up}A\r")

        # Erase from cursor to end of screen
        self._raw_write(_ERASE_TO_END)

        # Redraw status line
        self._raw_write(self._build_status_line() + "\n")

        # Redraw visible stream lines
        visible = self._stream_lines[-_MAX_STREAM_LINES:]
        self._stream_line_count = len(visible)
        for line in visible:
            self._raw_write(f"  {_DIM}      \u2502{_RESET} {line}\n")

    def _write_status_line(self) -> None:
        """Write the initial status line for the current block."""
        if self._is_tty:
            self._raw_write(self._build_status_line() + "\n")
        else:
            counter = self._counter_str()
            self._write(
                f"  {counter} ... {self._current_block_name}"
                f" ({self._current_block_type})\n"
            )

    def _build_status_line(self) -> str:
        """Build the ANSI-colored status line string (TTY only)."""
        elapsed = time.monotonic() - self._block_start_time
        dur = _format_duration(elapsed)
        spinner_ch = _SPINNER[self._spinner_frame]
        color = _BLOCK_COLORS.get(self._current_block_type, "")
        counter = self._counter_str()

        return (
            f"  {_DIM}{counter}{_RESET} "
            f"{_CYAN}{spinner_ch}{_RESET} "
            f"{self._current_block_name} "
            f"{color}({self._current_block_type}){_RESET}"
            f"  {_DIM}[{dur}]{_RESET}"
        )

    def _counter_str(self) -> str:
        if self._total_blocks:
            return f"[{self._blocks_done}/{self._total_blocks}]"
        return f"[{self._blocks_done}]"

    # ── Streaming text ────────────────────────────────────────────

    def _append_stream_text(self, text: str) -> None:
        """Append text from a streaming delta to the visible stream area."""
        if not self._stream_lines:
            self._stream_lines.append("")

        # Split on newlines — first part extends current line, rest are new
        parts = text.split("\n")
        self._stream_lines[-1] += parts[0]
        for part in parts[1:]:
            self._stream_lines.append(part)

        # Truncate long lines
        for i in range(len(self._stream_lines)):
            if len(self._stream_lines[i]) > 120:
                self._stream_lines[i] = self._stream_lines[i][:117] + "..."

        # Cap list size to avoid unbounded growth
        if len(self._stream_lines) > _MAX_STREAM_LINES * 3:
            self._stream_lines = self._stream_lines[-_MAX_STREAM_LINES:]

        # On TTY the spinner loop handles redraws; nothing to do for non-TTY

    # ── Active area management ────────────────────────────────────

    def _clear_active_area(self) -> None:
        """Move cursor to top of active area and erase to end of screen."""
        if not self._is_tty:
            return
        lines_up = (1 if self._status_line_shown else 0) + self._stream_line_count
        if lines_up > 0:
            self._raw_write(f"\033[{lines_up}A\r" + _ERASE_TO_END)
        self._stream_line_count = 0
        self._status_line_shown = False

    # ── Output helpers ────────────────────────────────────────────

    def _write(self, text: str) -> None:
        """Write to stderr, stripping ANSI codes if not a TTY."""
        if not self._is_tty:
            text = _ANSI_RE.sub("", text)
        sys.stderr.write(text)
        sys.stderr.flush()

    def _raw_write(self, text: str) -> None:
        """Write raw text to stderr (no stripping). For ANSI control sequences."""
        sys.stderr.write(text)
        sys.stderr.flush()


# ── Module-level helpers ──────────────────────────────────────────


def _c(is_tty: bool, color: str, text: str) -> str:
    """Wrap *text* in ANSI color codes when outputting to a TTY."""
    if is_tty and color:
        return f"{color}{text}{_RESET}"
    return text


def _format_duration(seconds: float) -> str:
    """Human-friendly duration string."""
    if seconds < 0.1:
        return f"{int(seconds * 1000)}ms"
    elif seconds < 10:
        return f"{seconds:.1f}s"
    elif seconds < 60:
        return f"{seconds:.0f}s"
    else:
        m, s = divmod(int(seconds), 60)
        return f"{m}m{s}s"


def _restore_cursor() -> None:
    """atexit handler — ensure cursor is visible on unexpected exit."""
    try:
        sys.stderr.write(_SHOW_CURSOR)
        sys.stderr.flush()
    except Exception:
        pass
