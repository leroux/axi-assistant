//! Process IO protocol for claudewire.
//!
//! Defines event types and the command result struct that any process
//! transport uses. The `ProcessConnection` trait is defined here but
//! implemented by backends (procmux adapter, direct subprocess, etc.).

// ---------------------------------------------------------------------------
// Process output events
// ---------------------------------------------------------------------------

/// JSON data from a process's stdout.
#[derive(Debug, Clone)]
pub struct StdoutEvent {
    pub name: String,
    pub data: serde_json::Value,
}

/// Text line from a process's stderr.
#[derive(Debug, Clone)]
pub struct StderrEvent {
    pub name: String,
    pub text: String,
}

/// Process exited.
#[derive(Debug, Clone)]
pub struct ExitEvent {
    pub name: String,
    pub code: Option<i32>,
}

/// Union of all process events.
#[derive(Debug, Clone)]
pub enum ProcessEvent {
    Stdout(StdoutEvent),
    Stderr(StderrEvent),
    Exit(ExitEvent),
}

// ---------------------------------------------------------------------------
// Command result
// ---------------------------------------------------------------------------

/// Result of a spawn/subscribe/kill command.
#[derive(Debug, Clone)]
pub struct CommandResult {
    pub ok: bool,
    pub error: Option<String>,
    pub already_running: bool,
    pub replayed: Option<usize>,
    pub status: Option<String>,
    pub idle: Option<bool>,
    pub agents: Vec<String>,
}

impl CommandResult {
    pub const fn success() -> Self {
        Self {
            ok: true,
            error: None,
            already_running: false,
            replayed: None,
            status: None,
            idle: None,
            agents: vec![],
        }
    }

    pub fn failure(error: impl Into<String>) -> Self {
        Self {
            ok: false,
            error: Some(error.into()),
            already_running: false,
            replayed: None,
            status: None,
            idle: None,
            agents: vec![],
        }
    }
}
