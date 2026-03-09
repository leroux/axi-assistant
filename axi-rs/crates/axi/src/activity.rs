//! Activity tracking — phase, state, and helpers for agent status display.
//!
//! Replaces the former `claudewire::events` activity types with locally-owned
//! types and functions used by the axi orchestration layer.

use chrono::{DateTime, Utc};
use serde_json::Value;

// ---------------------------------------------------------------------------
// Phase / ActivityState
// ---------------------------------------------------------------------------

/// High-level phase of an agent's current turn.
#[derive(Debug, Clone, PartialEq, Eq, Default)]
pub enum Phase {
    #[default]
    Starting,
    Working,
    Thinking,
    ToolUse,
    Idle,
}

/// Tracks what an agent is doing right now within a single turn.
#[derive(Debug, Clone, Default)]
pub struct ActivityState {
    pub phase: Phase,
    pub idle_reminder_count: u32,
    pub query_started: Option<DateTime<Utc>>,
}

// ---------------------------------------------------------------------------
// User message construction
// ---------------------------------------------------------------------------

/// Wrap a content value into the JSON envelope Claude CLI expects on stdin.
pub fn make_user_message(content: &Value) -> Value {
    serde_json::json!({
        "type": "user",
        "session_id": "",
        "message": {
            "role": "user",
            "content": content
        }
    })
}

// ---------------------------------------------------------------------------
// Activity state update
// ---------------------------------------------------------------------------

/// Update an agent's activity phase based on a stream event.
pub fn update_activity(state: &mut ActivityState, event: &Value) {
    let event_type = event
        .get("type")
        .and_then(|v| v.as_str())
        .unwrap_or("");

    // Unwrap stream_event wrappers
    let inner_type = if event_type == "stream_event" {
        event
            .get("event")
            .and_then(|e| e.get("type"))
            .and_then(|v| v.as_str())
            .unwrap_or("")
    } else {
        event_type
    };

    match inner_type {
        "content_block_start" => {
            let block_type = event
                .get("content_block")
                .or_else(|| {
                    event
                        .get("event")
                        .and_then(|e| e.get("content_block"))
                })
                .and_then(|b| b.get("type"))
                .and_then(|v| v.as_str())
                .unwrap_or("");
            match block_type {
                "thinking" => state.phase = Phase::Thinking,
                "tool_use" => state.phase = Phase::ToolUse,
                "text" => state.phase = Phase::Working,
                _ => {}
            }
        }
        "result" => {
            state.phase = Phase::Idle;
        }
        _ => {}
    }
}

// ---------------------------------------------------------------------------
// Rate limit parsing
// ---------------------------------------------------------------------------

/// Parsed rate limit event data.
pub struct RateLimitInfo {
    pub status: String,
    pub resets_at: DateTime<Utc>,
    pub rate_limit_type: String,
    pub utilization: Option<f64>,
}

/// Extract rate limit fields from a `rate_limit_event` JSON value.
pub fn parse_rate_limit_event(event: &Value) -> Option<RateLimitInfo> {
    let status = event.get("status").and_then(|v| v.as_str())?;
    let resets_at_str = event.get("resets_at").and_then(|v| v.as_str())?;
    let rate_limit_type = event
        .get("rate_limit_type")
        .and_then(|v| v.as_str())
        .unwrap_or("unknown");
    let utilization = event
        .get("utilization")
        .and_then(Value::as_f64);

    let resets_at = resets_at_str.parse::<DateTime<Utc>>().ok()?;

    Some(RateLimitInfo {
        status: status.to_string(),
        resets_at,
        rate_limit_type: rate_limit_type.to_string(),
        utilization,
    })
}

// ---------------------------------------------------------------------------
// Tool display names
// ---------------------------------------------------------------------------

/// Map internal tool names to friendly display names for Discord.
pub fn tool_display(tool_name: &str) -> &str {
    match tool_name {
        "Bash" => "Running command",
        "Read" => "Reading file",
        "Write" => "Writing file",
        "Edit" => "Editing file",
        "Glob" => "Searching files",
        "Grep" => "Searching content",
        "LS" | "ListDirectory" => "Listing directory",
        "Agent" => "Delegating to agent",
        "WebSearch" => "Searching web",
        "WebFetch" => "Fetching URL",
        "TodoWrite" => "Updating tasks",
        "NotebookEdit" => "Editing notebook",
        "AskUserQuestion" => "Asking question",
        "ExitPlanMode" => "Plan complete",
        _ => tool_name,
    }
}
