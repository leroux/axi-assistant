use std::io::{IsTerminal, Write};
use std::sync::atomic::{AtomicBool, AtomicUsize, Ordering};
use std::sync::Arc;
use std::time::Instant;

use flowchart_runner::protocol::{ExecutionStatus, FlowchartResult, Protocol};

// ANSI escape codes
const RESET: &str = "\x1b[0m";
const BOLD: &str = "\x1b[1m";
const DIM: &str = "\x1b[2m";
const RED: &str = "\x1b[31m";
const GREEN: &str = "\x1b[32m";
const YELLOW: &str = "\x1b[33m";
const BLUE: &str = "\x1b[34m";
const MAGENTA: &str = "\x1b[35m";
const CYAN: &str = "\x1b[36m";
const WHITE: &str = "\x1b[37m";
const HIDE_CURSOR: &str = "\x1b[?25l";
const SHOW_CURSOR: &str = "\x1b[?25h";
const ERASE_TO_END: &str = "\x1b[J";

const SPINNER_FRAMES: &[char] = &['⠋', '⠙', '⠹', '⠸', '⠼', '⠴', '⠦', '⠧', '⠇', '⠏'];
const MAX_STREAM_LINES: usize = 8;
const MAX_LINE_WIDTH: usize = 120;

/// Raw ANSI TUI protocol — renders flowchart execution progress to stderr.
pub struct TuiProtocol {
    is_tty: bool,
    // Current block state
    current_block_name: Option<String>,
    current_block_type: Option<String>,
    block_start: Option<Instant>,
    // Progress counters
    blocks_done: usize,
    total_blocks: usize,
    // Overall timing
    overall_start: Option<Instant>,
    command_name: Option<String>,
    // Stream text buffer
    stream_lines: Vec<String>,
    stream_line_count: usize, // lines currently drawn on screen
    // Spinner
    spinner_frame: Arc<AtomicUsize>,
    spinner_running: Arc<AtomicBool>,
    spinner_handle: Option<tokio::task::JoinHandle<()>>,
    // Verbose mode
    verbose: bool,
}

impl TuiProtocol {
    pub fn new(verbose: bool) -> Self {
        let is_tty = std::io::stderr().is_terminal();
        Self {
            is_tty,
            current_block_name: None,
            current_block_type: None,
            block_start: None,
            blocks_done: 0,
            total_blocks: 0,
            overall_start: None,
            command_name: None,
            stream_lines: Vec::new(),
            stream_line_count: 0,
            spinner_frame: Arc::new(AtomicUsize::new(0)),
            spinner_running: Arc::new(AtomicBool::new(false)),
            spinner_handle: None,
            verbose,
        }
    }

    /// Start the background spinner task.
    pub fn start_spinner(&mut self) {
        if !self.is_tty {
            return;
        }
        Self::write_raw(HIDE_CURSOR);
        self.spinner_running.store(true, Ordering::Relaxed);
        let frame = Arc::clone(&self.spinner_frame);
        let running = Arc::clone(&self.spinner_running);

        self.spinner_handle = Some(tokio::spawn(async move {
            while running.load(Ordering::Relaxed) {
                frame.fetch_add(1, Ordering::Relaxed);
                tokio::time::sleep(std::time::Duration::from_millis(83)).await;
            }
        }));
    }

    /// Stop the spinner and restore cursor.
    pub fn stop_spinner(&mut self) {
        self.spinner_running.store(false, Ordering::Relaxed);
        if let Some(handle) = self.spinner_handle.take() {
            handle.abort();
        }
        if self.is_tty {
            Self::write_raw(SHOW_CURSOR);
        }
    }

    /// Reset state for a new flowchart execution (used by REPL).
    pub fn reset(&mut self) {
        self.current_block_name = None;
        self.current_block_type = None;
        self.block_start = None;
        self.blocks_done = 0;
        self.total_blocks = 0;
        self.overall_start = None;
        self.command_name = None;
        self.stream_lines.clear();
        self.stream_line_count = 0;
    }

    fn spinner_char(&self) -> char {
        let idx = self.spinner_frame.load(Ordering::Relaxed) % SPINNER_FRAMES.len();
        SPINNER_FRAMES[idx]
    }

    fn block_color(block_type: &str) -> &'static str {
        match block_type {
            "prompt" => CYAN,
            "branch" | "spawn" | "wait" => YELLOW,
            "bash" => BLUE,
            "variable" => MAGENTA,
            "start" | "end" => GREEN,
            "refresh" => DIM,
            _ => WHITE,
        }
    }

    fn write_status_line(&self) {
        if !self.is_tty {
            return;
        }

        let spinner = self.spinner_char();
        let block_name = self
            .current_block_name
            .as_deref()
            .unwrap_or("...");
        let block_type = self
            .current_block_type
            .as_deref()
            .unwrap_or("unknown");
        let color = Self::block_color(block_type);
        let block_type_upper = block_type.to_uppercase();
        let elapsed = self
            .block_start
            .map(|s| format_duration(s.elapsed()))
            .unwrap_or_default();

        let line = format!(
            "\r{ERASE_TO_END}{DIM}[{}/{total}]{RESET} {spinner} {color}{BOLD}{block_type_upper}{RESET} {block_name} {DIM}{elapsed}{RESET}",
            self.blocks_done,
            total = self.total_blocks,
        );
        Self::write_raw(&line);
    }

    fn clear_active_area(&mut self) {
        if !self.is_tty {
            return;
        }
        // Move cursor up past status line + stream lines
        let lines_up = 1 + self.stream_line_count;
        if lines_up > 1 {
            Self::write_raw(&format!("\x1b[{}A", lines_up - 1));
        }
        Self::write_raw(&format!("\r{ERASE_TO_END}"));
        self.stream_line_count = 0;
    }

    fn redraw_active_area(&mut self) {
        if !self.is_tty || self.current_block_name.is_none() {
            return;
        }

        // Move up to the status line
        let lines_up = 1 + self.stream_line_count;
        if lines_up > 1 {
            Self::write_raw(&format!("\x1b[{}A", lines_up - 1));
        }

        // Rewrite status line
        self.write_status_line();

        // Rewrite visible stream lines
        let visible: Vec<_> = self
            .stream_lines
            .iter()
            .rev()
            .take(MAX_STREAM_LINES)
            .collect::<Vec<_>>()
            .into_iter()
            .rev()
            .collect();

        for line in &visible {
            Self::write_raw(&format!("\n{ERASE_TO_END}{DIM}│{RESET} {line}"));
        }
        self.stream_line_count = visible.len();

        let _ = std::io::stderr().flush();
    }

    fn append_stream_text(&mut self, text: &str) {
        // Split on newlines, extend last line
        let parts: Vec<&str> = text.split('\n').collect();
        for (i, part) in parts.iter().enumerate() {
            if i == 0 {
                // Append to last line
                if let Some(last) = self.stream_lines.last_mut() {
                    last.push_str(part);
                    truncate_line(last);
                } else {
                    let mut line = part.to_string();
                    truncate_line(&mut line);
                    self.stream_lines.push(line);
                }
            } else {
                // New line
                let mut line = part.to_string();
                truncate_line(&mut line);
                self.stream_lines.push(line);
            }
        }

        // Cap total buffer
        let max_buf = MAX_STREAM_LINES * 3;
        if self.stream_lines.len() > max_buf {
            let drain_to = self.stream_lines.len() - MAX_STREAM_LINES;
            self.stream_lines.drain(..drain_to);
        }

        self.redraw_active_area();
    }

    fn write(&self, text: &str) {
        if self.is_tty {
            eprint!("{text}");
        } else {
            // Strip ANSI codes for non-TTY
            let stripped = strip_ansi(text);
            eprint!("{stripped}");
        }
    }

    fn write_raw(text: &str) {
        eprint!("{text}");
    }
}

impl Protocol for TuiProtocol {
    fn on_block_start(&mut self, _block_id: &str, block_name: &str, block_type: &str) {
        // Clear previous active area
        if self.current_block_name.is_some() {
            self.clear_active_area();
        }

        self.blocks_done += 1;
        self.current_block_name = Some(block_name.to_owned());
        self.current_block_type = Some(block_type.to_owned());
        self.block_start = Some(Instant::now());
        self.stream_lines.clear();
        self.stream_line_count = 0;

        self.write_status_line();
        if self.is_tty {
            Self::write_raw("\n"); // Reserve line for stream text area start
            self.stream_line_count = 0;
        }
    }

    fn on_block_complete(
        &mut self,
        _block_id: &str,
        block_name: &str,
        success: bool,
        duration_ms: u64,
    ) {
        self.clear_active_area();

        let (icon, color) = if success {
            ("✓", GREEN)
        } else {
            ("✗", RED)
        };
        let block_type = self
            .current_block_type
            .as_deref()
            .unwrap_or("unknown");
        let type_color = Self::block_color(block_type);
        let block_type_upper = block_type.to_uppercase();
        let dur = format_duration_ms(duration_ms);

        self.write(&format!(
            "{DIM}[{}/{}]{RESET} {color}{icon}{RESET} {type_color}{BOLD}{block_type_upper}{RESET} {block_name} {DIM}{dur}{RESET}\n",
            self.blocks_done, self.total_blocks,
        ));

        // Flush buffered stream lines as pipe-prefixed lines
        if self.verbose && !self.stream_lines.is_empty() {
            for line in &self.stream_lines {
                Self::write_raw(&format!("  {DIM}│{RESET} {line}\n"));
            }
        }

        self.current_block_name = None;
        self.current_block_type = None;
        self.block_start = None;
        self.stream_lines.clear();
        self.stream_line_count = 0;
    }

    fn on_stream_text(&mut self, text: &str) {
        if self.current_block_name.is_some() {
            self.append_stream_text(text);
        } else {
            // Chat mode — direct output to stderr, flush immediately
            Self::write_raw(text);
            let _ = std::io::stderr().flush();
        }
    }

    fn on_flowchart_start(&mut self, command: &str, _args: &str, block_count: usize) {
        self.overall_start = Some(Instant::now());
        self.command_name = Some(command.to_owned());
        self.total_blocks = block_count;
        self.blocks_done = 0;
        self.start_spinner();
    }

    fn on_flowchart_complete(&mut self, result: &FlowchartResult) {
        self.stop_spinner();

        let dur = format_duration_ms(result.duration_ms);
        let status = match &result.status {
            ExecutionStatus::Completed => format!("{GREEN}Done{RESET}"),
            ExecutionStatus::Halted { exit_code } => {
                format!("{YELLOW}Halted (exit {exit_code}){RESET}")
            }
            ExecutionStatus::Interrupted => format!("{YELLOW}Interrupted{RESET}"),
            ExecutionStatus::Error(msg) => format!("{RED}Error: {msg}{RESET}"),
        };

        self.write(&format!(
            "\n{status} in {dur} | {blocks} blocks | ${cost:.4}\n",
            blocks = result.blocks_executed,
            cost = result.cost_usd,
        ));
    }

    fn on_forwarded_message(
        &mut self,
        msg: &serde_json::Value,
        _block_id: &str,
        _block_name: &str,
    ) {
        // Extract text from assistant messages and route to on_stream_text.
        // This matches the Python emit_forwarded behavior — assistant messages
        // carry the assembled response text, while stream_event messages carry
        // incremental deltas. Both need to reach the display.
        let msg_type = msg
            .get("type")
            .and_then(serde_json::Value::as_str)
            .unwrap_or("");

        if msg_type == "assistant" {
            if let Some(content) = msg
                .get("message")
                .and_then(|m| m.get("content"))
                .and_then(serde_json::Value::as_array)
            {
                for block in content {
                    if block.get("type").and_then(serde_json::Value::as_str) == Some("text")
                        && let Some(text) =
                            block.get("text").and_then(serde_json::Value::as_str)
                        && !text.is_empty()
                    {
                        self.on_stream_text(text);
                    }
                }
            } else if let Some(text) = msg
                .get("message")
                .and_then(|m| m.get("content"))
                .and_then(serde_json::Value::as_str)
                && !text.is_empty()
            {
                self.on_stream_text(text);
            }
        }
        // stream_event text is already handled via on_stream_text from the session
    }

    fn on_log(&mut self, message: &str) {
        if self.is_tty && self.current_block_name.is_some() {
            // Insert log line cleanly between spinner redraws
            self.clear_active_area();
            self.write(&format!("{DIM}{message}{RESET}\n"));
            self.write_status_line();
            if self.is_tty {
                Self::write_raw("\n");
            }
        } else {
            self.write(&format!("{DIM}{message}{RESET}\n"));
        }
    }
}

impl Drop for TuiProtocol {
    fn drop(&mut self) {
        self.stop_spinner();
        if self.is_tty {
            // Ensure cursor is restored
            eprint!("{SHOW_CURSOR}");
            let _ = std::io::stderr().flush();
        }
    }
}

/// Format a duration for display.
fn format_duration(dur: std::time::Duration) -> String {
    format_duration_ms(dur.as_millis() as u64)
}

fn format_duration_ms(ms: u64) -> String {
    if ms < 100 {
        format!("{ms}ms")
    } else if ms < 10_000 {
        format!("{:.1}s", ms as f64 / 1000.0)
    } else if ms < 60_000 {
        format!("{}s", ms / 1000)
    } else {
        let mins = ms / 60_000;
        let secs = (ms % 60_000) / 1000;
        format!("{mins}m{secs}s")
    }
}

fn truncate_line(line: &mut String) {
    // Approximate: count visible chars (strip ANSI would be more precise, but this is fine)
    if line.len() > MAX_LINE_WIDTH {
        line.truncate(MAX_LINE_WIDTH - 3);
        line.push_str("...");
    }
}

fn strip_ansi(text: &str) -> String {
    let mut result = String::with_capacity(text.len());
    let mut in_escape = false;
    for c in text.chars() {
        if in_escape {
            if c.is_ascii_alphabetic() || c == '?' {
                // Check if this is the end of the escape sequence
                if c.is_ascii_alphabetic() {
                    in_escape = false;
                }
            }
        } else if c == '\x1b' {
            in_escape = true;
        } else {
            result.push(c);
        }
    }
    result
}
