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

