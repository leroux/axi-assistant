//! Event parsing and activity tracking for the Claude CLI stream-json protocol.

use chrono::{DateTime, Utc};

// ---------------------------------------------------------------------------
// Activity state
// ---------------------------------------------------------------------------

/// Real-time activity tracking for an agent during a query.
#[derive(Debug, Clone)]
pub struct ActivityState {
    pub phase: Phase,
    pub tool_name: Option<String>,
    pub tool_input_preview: String,
    pub thinking_text: String,
    pub turn_count: u32,
    pub query_started: Option<DateTime<Utc>>,
    pub last_event: Option<DateTime<Utc>>,
    pub text_chars: usize,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Phase {
    Idle,
    Thinking,
    Writing,
    ToolUse,
    Waiting,
    Starting,
}

impl Default for ActivityState {
    fn default() -> Self {
        Self {
            phase: Phase::Idle,
            tool_name: None,
            tool_input_preview: String::new(),
            thinking_text: String::new(),
            turn_count: 0,
            query_started: None,
            last_event: None,
            text_chars: 0,
        }
    }
}

impl std::fmt::Display for Phase {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            Self::Idle => write!(f, "idle"),
            Self::Thinking => write!(f, "thinking"),
            Self::Writing => write!(f, "writing"),
            Self::ToolUse => write!(f, "tool_use"),
            Self::Waiting => write!(f, "waiting"),
            Self::Starting => write!(f, "starting"),
        }
    }
}

/// Human-readable description of a tool call.
pub fn tool_display(name: &str) -> String {
    match name {
        "Bash" => "running bash command".into(),
        "Read" => "reading file".into(),
        "Write" => "writing file".into(),
        "Edit" | "MultiEdit" => "editing file".into(),
        "Glob" => "searching for files".into(),
        "Grep" => "searching code".into(),
        "WebSearch" => "searching the web".into(),
        "WebFetch" => "fetching web page".into(),
        "Task" => "running subagent".into(),
        "NotebookEdit" => "editing notebook".into(),
        "TodoWrite" => "updating tasks".into(),
        other => {
            if let Some(rest) = other.strip_prefix("mcp__")
                && let Some((server, tool)) = rest.split_once("__")
            {
                return format!("{server}: {tool}");
            }
            format!("using {other}")
        }
    }
}

/// Update activity state from a raw Claude stream event dict.
pub fn update_activity(activity: &mut ActivityState, event: &serde_json::Value) {
    activity.last_event = Some(Utc::now());
    let event_type = event.get("type").and_then(|v| v.as_str()).unwrap_or("");

    match event_type {
        "content_block_start" => {
            let block = event.get("content_block").unwrap_or(&serde_json::Value::Null);
            let block_type = block.get("type").and_then(|v| v.as_str()).unwrap_or("");

            match block_type {
                "tool_use" => {
                    activity.phase = Phase::ToolUse;
                    activity.tool_name = block.get("name").and_then(|v| v.as_str()).map(String::from);
                    activity.tool_input_preview.clear();
                }
                "thinking" => {
                    activity.phase = Phase::Thinking;
                    activity.tool_name = None;
                    activity.tool_input_preview.clear();
                    activity.thinking_text.clear();
                }
                "text" => {
                    activity.phase = Phase::Writing;
                    activity.tool_name = None;
                    activity.tool_input_preview.clear();
                    activity.text_chars = 0;
                }
                _ => {}
            }
        }
        "content_block_delta" => {
            let delta = event.get("delta").unwrap_or(&serde_json::Value::Null);
            let delta_type = delta.get("type").and_then(|v| v.as_str()).unwrap_or("");

            match delta_type {
                "thinking_delta" => {
                    activity.phase = Phase::Thinking;
                    if let Some(text) = delta.get("thinking").and_then(|v| v.as_str()) {
                        activity.thinking_text.push_str(text);
                    }
                }
                "text_delta" => {
                    activity.phase = Phase::Writing;
                    if let Some(text) = delta.get("text").and_then(|v| v.as_str()) {
                        activity.text_chars += text.len();
                    }
                }
                "input_json_delta" => {
                    if activity.tool_input_preview.len() < 200
                        && let Some(pj) = delta.get("partial_json").and_then(|v| v.as_str())
                    {
                        activity.tool_input_preview.push_str(pj);
                        activity.tool_input_preview.truncate(200);
                    }
                }
                _ => {}
            }
        }
        "content_block_stop" => {
            if activity.phase == Phase::ToolUse {
                activity.phase = Phase::Waiting;
            }
        }
        "message_start" => {
            activity.turn_count += 1;
        }
        "message_delta" => {
            let stop_reason = event
                .get("delta")
                .and_then(|d| d.get("stop_reason"))
                .and_then(|v| v.as_str());

            match stop_reason {
                Some("end_turn") => {
                    activity.phase = Phase::Idle;
                    activity.tool_name = None;
                }
                Some("tool_use") => {
                    activity.phase = Phase::Waiting;
                }
                _ => {}
            }
        }
        _ => {}
    }
}

// ---------------------------------------------------------------------------
// Rate limit event parsing
// ---------------------------------------------------------------------------

/// Parsed rate limit event from the Claude CLI stream.
#[derive(Debug, Clone)]
pub struct ParsedRateLimit {
    pub rate_limit_type: String,
    pub status: String,
    pub resets_at: DateTime<Utc>,
    pub utilization: Option<f64>,
}

/// Parse a `rate_limit_event` from raw JSON.
pub fn parse_rate_limit_event(data: &serde_json::Value) -> Option<ParsedRateLimit> {
    if data.get("type")?.as_str()? != "rate_limit_event" {
        return None;
    }
    let info = data.get("rate_limit_info")?;
    let resets_at_unix = info.get("resetsAt")?.as_f64()?;
    let resets_at = DateTime::from_timestamp(resets_at_unix as i64, 0)?;

    Some(ParsedRateLimit {
        rate_limit_type: info
            .get("rateLimitType")
            .and_then(|v| v.as_str())
            .unwrap_or("unknown")
            .to_string(),
        status: info
            .get("status")
            .and_then(|v| v.as_str())
            .unwrap_or("unknown")
            .to_string(),
        resets_at,
        utilization: info.get("utilization").and_then(serde_json::Value::as_f64),
    })
}

// ---------------------------------------------------------------------------
// Stream helper
// ---------------------------------------------------------------------------

/// Create a user message for the SDK streaming interface.
pub fn make_user_message(content: &serde_json::Value) -> serde_json::Value {
    serde_json::json!({
        "type": "user",
        "session_id": "",
        "message": {"role": "user", "content": content},
        "parent_tool_use_id": null,
    })
}

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn activity_text_delta() {
        let mut state = ActivityState::default();
        let event = serde_json::json!({
            "type": "content_block_start",
            "index": 0,
            "content_block": {"type": "text", "text": ""}
        });
        update_activity(&mut state, &event);
        assert_eq!(state.phase, Phase::Writing);

        let delta = serde_json::json!({
            "type": "content_block_delta",
            "index": 0,
            "delta": {"type": "text_delta", "text": "Hello world"}
        });
        update_activity(&mut state, &delta);
        assert_eq!(state.text_chars, 11);
    }

    #[test]
    fn activity_tool_use() {
        let mut state = ActivityState::default();
        let event = serde_json::json!({
            "type": "content_block_start",
            "index": 1,
            "content_block": {"type": "tool_use", "id": "t1", "name": "Bash", "input": {}}
        });
        update_activity(&mut state, &event);
        assert_eq!(state.phase, Phase::ToolUse);
        assert_eq!(state.tool_name.as_deref(), Some("Bash"));

        let stop = serde_json::json!({
            "type": "content_block_stop",
            "index": 1
        });
        update_activity(&mut state, &stop);
        assert_eq!(state.phase, Phase::Waiting);
    }

    #[test]
    fn activity_thinking() {
        let mut state = ActivityState::default();
        let event = serde_json::json!({
            "type": "content_block_start",
            "index": 0,
            "content_block": {"type": "thinking", "thinking": "", "signature": ""}
        });
        update_activity(&mut state, &event);
        assert_eq!(state.phase, Phase::Thinking);

        let delta = serde_json::json!({
            "type": "content_block_delta",
            "index": 0,
            "delta": {"type": "thinking_delta", "thinking": "Let me think..."}
        });
        update_activity(&mut state, &delta);
        assert_eq!(state.thinking_text, "Let me think...");
    }

    #[test]
    fn activity_end_turn() {
        let mut state = ActivityState::default();
        state.phase = Phase::Writing;
        let event = serde_json::json!({
            "type": "message_delta",
            "delta": {"stop_reason": "end_turn"}
        });
        update_activity(&mut state, &event);
        assert_eq!(state.phase, Phase::Idle);
    }

    #[test]
    fn tool_display_names() {
        assert_eq!(tool_display("Bash"), "running bash command");
        assert_eq!(tool_display("Read"), "reading file");
        assert_eq!(tool_display("mcp__utils__get_date_and_time"), "utils: get_date_and_time");
        assert_eq!(tool_display("CustomTool"), "using CustomTool");
    }

    #[test]
    fn parse_rate_limit() {
        let data = serde_json::json!({
            "type": "rate_limit_event",
            "rate_limit_info": {
                "status": "allowed_warning",
                "resetsAt": 1709856000,
                "rateLimitType": "five_hour",
                "utilization": 0.85
            },
            "uuid": "u1",
            "session_id": "s1"
        });
        let parsed = parse_rate_limit_event(&data).unwrap();
        assert_eq!(parsed.status, "allowed_warning");
        assert_eq!(parsed.rate_limit_type, "five_hour");
        assert_eq!(parsed.utilization, Some(0.85));
    }
}
