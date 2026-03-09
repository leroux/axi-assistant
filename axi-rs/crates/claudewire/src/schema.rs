//! Claude CLI stream-json protocol types.
//!
//! Serde structs for every message type in the Claude CLI stream-json protocol.
//! Validation happens at deserialize time — no separate validation step.
//! Types that may gain new upstream fields use `#[serde(flatten)] extra: HashMap`
//! to capture unknown keys without breaking deserialization.

use std::collections::HashMap;

use serde::{Deserialize, Serialize};

// ---------------------------------------------------------------------------
// Content blocks (assistant message content, content_block_start)
// ---------------------------------------------------------------------------

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(tag = "type")]
pub enum ContentBlock {
    #[serde(rename = "text")]
    Text(TextBlock),
    #[serde(rename = "tool_use")]
    ToolUse(ToolUseBlock),
    #[serde(rename = "thinking")]
    Thinking(ThinkingBlock),
    #[serde(rename = "server_tool_use")]
    ServerToolUse(ServerToolUseBlock),
    /// Unknown content block types (`web_search_20250305`, `mcp_tools`, etc.).
    #[serde(other)]
    Unknown,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct TextBlock {
    pub text: String,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ToolUseBlock {
    pub id: String,
    pub name: String,
    pub input: serde_json::Value,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub caller: Option<serde_json::Value>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ThinkingBlock {
    pub thinking: String,
    pub signature: String,
}

/// Server-side tool use (e.g. web search).
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ServerToolUseBlock {
    pub id: String,
    pub name: String,
    #[serde(default)]
    pub input: serde_json::Value,
}

// ---------------------------------------------------------------------------
// Deltas (inside content_block_delta)
// ---------------------------------------------------------------------------

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(tag = "type")]
pub enum Delta {
    #[serde(rename = "text_delta")]
    Text(TextDelta),
    #[serde(rename = "input_json_delta")]
    InputJson(InputJsonDelta),
    #[serde(rename = "thinking_delta")]
    Thinking(ThinkingDelta),
    #[serde(rename = "signature_delta")]
    Signature(SignatureDelta),
    /// Unknown delta types (`citations_delta`, etc.).
    #[serde(other)]
    Unknown,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct TextDelta {
    pub text: String,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct InputJsonDelta {
    pub partial_json: String,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ThinkingDelta {
    pub thinking: String,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct SignatureDelta {
    pub signature: String,
}

// ---------------------------------------------------------------------------
// Shared models
// ---------------------------------------------------------------------------

/// Token usage — permissive (new fields appear frequently).
#[derive(Debug, Clone, Default, Serialize, Deserialize)]
pub struct Usage {
    #[serde(skip_serializing_if = "Option::is_none")]
    pub input_tokens: Option<i64>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub output_tokens: Option<i64>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub cache_creation_input_tokens: Option<i64>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub cache_read_input_tokens: Option<i64>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub cache_creation: Option<serde_json::Value>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub service_tier: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub inference_geo: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub iterations: Option<Vec<serde_json::Value>>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub server_tool_use: Option<serde_json::Value>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub speed: Option<String>,
    /// Catch-all for new fields.
    #[serde(flatten)]
    pub extra: HashMap<String, serde_json::Value>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct MessageDeltaDelta {
    #[serde(skip_serializing_if = "Option::is_none")]
    pub stop_reason: Option<StopReason>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub stop_sequence: Option<String>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum StopReason {
    EndTurn,
    ToolUse,
    MaxTokens,
}

// ---------------------------------------------------------------------------
// Inner Anthropic API stream events
// ---------------------------------------------------------------------------

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct MessageInner {
    pub model: String,
    pub id: String,
    pub role: String,
    #[serde(default)]
    pub content: Vec<serde_json::Value>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub stop_reason: Option<serde_json::Value>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub stop_sequence: Option<serde_json::Value>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub usage: Option<Usage>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub context_management: Option<serde_json::Value>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub container: Option<serde_json::Value>,
    /// type field always "message"
    #[serde(rename = "type")]
    pub msg_type: Option<String>,
}

/// Stream events from the Anthropic API (inside `stream_event` wrapper).
#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(tag = "type")]
pub enum StreamEvent {
    #[serde(rename = "message_start")]
    MessageStart { message: MessageInner },
    #[serde(rename = "message_delta")]
    MessageDelta {
        delta: MessageDeltaDelta,
        #[serde(skip_serializing_if = "Option::is_none")]
        usage: Option<Usage>,
        #[serde(skip_serializing_if = "Option::is_none")]
        context_management: Option<serde_json::Value>,
    },
    #[serde(rename = "message_stop")]
    MessageStop {},
    #[serde(rename = "content_block_start")]
    ContentBlockStart {
        index: usize,
        content_block: ContentBlock,
    },
    #[serde(rename = "content_block_delta")]
    ContentBlockDelta { index: usize, delta: Delta },
    #[serde(rename = "content_block_stop")]
    ContentBlockStop { index: usize },
}

// ---------------------------------------------------------------------------
// User message content blocks
// ---------------------------------------------------------------------------

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(tag = "type")]
pub enum UserContentBlock {
    #[serde(rename = "text")]
    Text(TextBlock),
    #[serde(rename = "tool_use")]
    ToolUse(ToolUseBlock),
    #[serde(rename = "tool_result")]
    ToolResult(ToolResultBlock),
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ToolResultBlock {
    pub tool_use_id: String,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub content: Option<serde_json::Value>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub is_error: Option<bool>,
    /// Catch-all for tool-specific fields.
    #[serde(flatten)]
    pub extra: HashMap<String, serde_json::Value>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct UserMessageInner {
    pub role: String,
    pub content: UserContent,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(untagged)]
pub enum UserContent {
    Text(String),
    Blocks(Vec<UserContentBlock>),
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct AssistantMessageInner {
    pub model: String,
    pub id: String,
    #[serde(rename = "type")]
    pub msg_type: String,
    pub role: String,
    pub content: Vec<ContentBlock>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub stop_reason: Option<serde_json::Value>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub stop_sequence: Option<serde_json::Value>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub usage: Option<Usage>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub context_management: Option<serde_json::Value>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub container: Option<serde_json::Value>,
}

// ---------------------------------------------------------------------------
// Control protocol
// ---------------------------------------------------------------------------

/// Control request payload — subtypes vary, so allow extra fields.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ControlRequestInner {
    pub subtype: String,
    #[serde(flatten)]
    pub extra: HashMap<String, serde_json::Value>,
}

/// Control response payload.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ControlResponseInner {
    pub subtype: String, // "success" or "error"
    pub request_id: String,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub response: Option<serde_json::Value>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub error: Option<String>,
    #[serde(flatten)]
    pub extra: HashMap<String, serde_json::Value>,
}

// ---------------------------------------------------------------------------
// Rate limit
// ---------------------------------------------------------------------------

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct RateLimitInfo {
    pub status: String, // "allowed", "allowed_warning", "rejected"
    #[serde(rename = "resetsAt")]
    #[serde(skip_serializing_if = "Option::is_none")]
    pub resets_at: Option<f64>,
    #[serde(rename = "rateLimitType")]
    #[serde(skip_serializing_if = "Option::is_none")]
    pub rate_limit_type: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub utilization: Option<f64>,
    #[serde(rename = "isUsingOverage")]
    #[serde(skip_serializing_if = "Option::is_none")]
    pub is_using_overage: Option<bool>,
    #[serde(rename = "surpassedThreshold")]
    #[serde(skip_serializing_if = "Option::is_none")]
    pub surpassed_threshold: Option<f64>,
    #[serde(rename = "overageStatus")]
    #[serde(skip_serializing_if = "Option::is_none")]
    pub overage_status: Option<String>,
    #[serde(rename = "overageDisabledReason")]
    #[serde(skip_serializing_if = "Option::is_none")]
    pub overage_disabled_reason: Option<String>,
    #[serde(flatten)]
    pub extra: HashMap<String, serde_json::Value>,
}

// ---------------------------------------------------------------------------
// Top-level inbound messages (CLI -> us)
// ---------------------------------------------------------------------------

/// Discriminated union of all messages the CLI can send us.
#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(tag = "type")]
pub enum InboundMsg {
    #[serde(rename = "stream_event")]
    StreamEvent {
        uuid: String,
        session_id: String,
        #[serde(skip_serializing_if = "Option::is_none")]
        parent_tool_use_id: Option<String>,
        event: StreamEvent,
    },
    #[serde(rename = "assistant")]
    Assistant {
        message: AssistantMessageInner,
        #[serde(skip_serializing_if = "Option::is_none")]
        parent_tool_use_id: Option<String>,
        session_id: String,
        uuid: String,
        #[serde(skip_serializing_if = "Option::is_none")]
        error: Option<String>,
    },
    #[serde(rename = "user")]
    User {
        #[serde(skip_serializing_if = "Option::is_none")]
        message: Option<UserMessageInner>,
        #[serde(skip_serializing_if = "Option::is_none")]
        content: Option<serde_json::Value>,
        #[serde(skip_serializing_if = "Option::is_none")]
        session_id: Option<String>,
        #[serde(skip_serializing_if = "Option::is_none")]
        parent_tool_use_id: Option<String>,
        #[serde(skip_serializing_if = "Option::is_none")]
        uuid: Option<String>,
        #[serde(skip_serializing_if = "Option::is_none")]
        tool_use_result: Option<serde_json::Value>,
        #[serde(flatten)]
        extra: HashMap<String, serde_json::Value>,
    },
    #[serde(rename = "system")]
    System {
        subtype: String,
        #[serde(flatten)]
        extra: HashMap<String, serde_json::Value>,
    },
    #[serde(rename = "result")]
    Result {
        subtype: String,
        is_error: bool,
        duration_ms: i64,
        duration_api_ms: i64,
        num_turns: i64,
        session_id: String,
        uuid: String,
        #[serde(skip_serializing_if = "Option::is_none")]
        result: Option<String>,
        #[serde(skip_serializing_if = "Option::is_none")]
        stop_reason: Option<serde_json::Value>,
        #[serde(skip_serializing_if = "Option::is_none")]
        total_cost_usd: Option<f64>,
        #[serde(skip_serializing_if = "Option::is_none")]
        usage: Option<Usage>,
        #[serde(rename = "modelUsage")]
        #[serde(skip_serializing_if = "Option::is_none")]
        model_usage: Option<serde_json::Value>,
        #[serde(skip_serializing_if = "Option::is_none")]
        permission_denials: Option<Vec<serde_json::Value>>,
        #[serde(skip_serializing_if = "Option::is_none")]
        fast_mode_state: Option<String>,
        #[serde(skip_serializing_if = "Option::is_none")]
        structured_output: Option<serde_json::Value>,
        #[serde(flatten)]
        extra: HashMap<String, serde_json::Value>,
    },
    #[serde(rename = "control_request")]
    ControlRequest {
        request_id: String,
        request: ControlRequestInner,
    },
    #[serde(rename = "control_response")]
    ControlResponse {
        response: ControlResponseInner,
    },
    #[serde(rename = "rate_limit_event")]
    RateLimitEvent {
        rate_limit_info: RateLimitInfo,
        uuid: String,
        session_id: String,
    },
    #[serde(rename = "tool_progress")]
    ToolProgress {
        tool_use_id: String,
        #[serde(skip_serializing_if = "Option::is_none")]
        tool_name: Option<String>,
        #[serde(skip_serializing_if = "Option::is_none")]
        elapsed_time_seconds: Option<f64>,
        #[serde(flatten)]
        extra: HashMap<String, serde_json::Value>,
    },
    #[serde(rename = "tool_use_summary")]
    ToolUseSummary {
        #[serde(skip_serializing_if = "Option::is_none")]
        summary: Option<String>,
        #[serde(flatten)]
        extra: HashMap<String, serde_json::Value>,
    },
    #[serde(rename = "keep_alive")]
    KeepAlive {},
    #[serde(rename = "auth_status")]
    AuthStatus {
        #[serde(flatten)]
        extra: HashMap<String, serde_json::Value>,
    },
    #[serde(rename = "prompt_suggestion")]
    PromptSuggestion {
        #[serde(skip_serializing_if = "Option::is_none")]
        suggestion: Option<String>,
        #[serde(flatten)]
        extra: HashMap<String, serde_json::Value>,
    },
    #[serde(rename = "control_cancel_request")]
    ControlCancelRequest {
        #[serde(flatten)]
        extra: HashMap<String, serde_json::Value>,
    },
}

// ---------------------------------------------------------------------------
// Top-level outbound messages (us -> CLI)
// ---------------------------------------------------------------------------

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(tag = "type")]
pub enum OutboundMsg {
    #[serde(rename = "user")]
    User {
        #[serde(skip_serializing_if = "Option::is_none")]
        content: Option<serde_json::Value>,
        #[serde(skip_serializing_if = "Option::is_none")]
        session_id: Option<String>,
        #[serde(skip_serializing_if = "Option::is_none")]
        message: Option<UserMessageInner>,
        #[serde(skip_serializing_if = "Option::is_none")]
        parent_tool_use_id: Option<String>,
    },
    #[serde(rename = "control_request")]
    ControlRequest {
        request_id: String,
        request: ControlRequestInner,
    },
    #[serde(rename = "control_response")]
    ControlResponse {
        response: ControlResponseInner,
    },
    #[serde(rename = "update_environment_variables")]
    UpdateEnvironmentVariables {
        variables: HashMap<String, String>,
    },
}

// ---------------------------------------------------------------------------
// Bare stream event types
// ---------------------------------------------------------------------------

/// The CLI emits every stream event twice: once wrapped in `stream_event` and
/// once bare. These are the bare types to filter.
pub const BARE_STREAM_TYPES: &[&str] = &[
    "message_start",
    "message_delta",
    "message_stop",
    "content_block_start",
    "content_block_delta",
    "content_block_stop",
];

/// Check if a message type is a bare stream event (should be deduplicated).
pub fn is_bare_stream_type(msg_type: &str) -> bool {
    BARE_STREAM_TYPES.contains(&msg_type)
}

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn parse_stream_event_text_delta() {
        let json = r#"{
            "type": "stream_event",
            "uuid": "abc-123",
            "session_id": "sess-1",
            "event": {
                "type": "content_block_delta",
                "index": 0,
                "delta": {
                    "type": "text_delta",
                    "text": "Hello"
                }
            }
        }"#;
        let msg: InboundMsg = serde_json::from_str(json).unwrap();
        match msg {
            InboundMsg::StreamEvent { event, uuid, .. } => {
                assert_eq!(uuid, "abc-123");
                match event {
                    StreamEvent::ContentBlockDelta { delta, index } => {
                        assert_eq!(index, 0);
                        match delta {
                            Delta::Text(td) => assert_eq!(td.text, "Hello"),
                            _ => panic!("expected TextDelta"),
                        }
                    }
                    _ => panic!("expected ContentBlockDelta"),
                }
            }
            _ => panic!("expected StreamEvent"),
        }
    }

    #[test]
    fn parse_result_msg() {
        let json = r#"{
            "type": "result",
            "subtype": "success",
            "is_error": false,
            "duration_ms": 5000,
            "duration_api_ms": 4500,
            "num_turns": 3,
            "session_id": "sess-1",
            "uuid": "uuid-1",
            "result": "Done",
            "total_cost_usd": 0.05,
            "usage": {"input_tokens": 100, "output_tokens": 50}
        }"#;
        let msg: InboundMsg = serde_json::from_str(json).unwrap();
        match msg {
            InboundMsg::Result {
                subtype,
                is_error,
                duration_ms,
                usage,
                ..
            } => {
                assert_eq!(subtype, "success");
                assert!(!is_error);
                assert_eq!(duration_ms, 5000);
                let u = usage.unwrap();
                assert_eq!(u.input_tokens, Some(100));
                assert_eq!(u.output_tokens, Some(50));
            }
            _ => panic!("expected Result"),
        }
    }

    #[test]
    fn parse_control_request() {
        let json = r#"{
            "type": "control_request",
            "request_id": "req-1",
            "request": {
                "subtype": "can_use_tool",
                "tool_name": "Bash",
                "tool_input": {"command": "ls"}
            }
        }"#;
        let msg: InboundMsg = serde_json::from_str(json).unwrap();
        match msg {
            InboundMsg::ControlRequest { request_id, request } => {
                assert_eq!(request_id, "req-1");
                assert_eq!(request.subtype, "can_use_tool");
                assert_eq!(request.extra["tool_name"], "Bash");
            }
            _ => panic!("expected ControlRequest"),
        }
    }

    #[test]
    fn parse_assistant_msg() {
        let json = r#"{
            "type": "assistant",
            "session_id": "sess-1",
            "uuid": "uuid-1",
            "message": {
                "model": "claude-opus-4-6",
                "id": "msg-1",
                "type": "message",
                "role": "assistant",
                "content": [
                    {"type": "text", "text": "Hello world"}
                ]
            }
        }"#;
        let msg: InboundMsg = serde_json::from_str(json).unwrap();
        match msg {
            InboundMsg::Assistant { message, .. } => {
                assert_eq!(message.model, "claude-opus-4-6");
                assert_eq!(message.content.len(), 1);
            }
            _ => panic!("expected Assistant"),
        }
    }

    #[test]
    fn parse_rate_limit_event() {
        let json = r#"{
            "type": "rate_limit_event",
            "uuid": "uuid-1",
            "session_id": "sess-1",
            "rate_limit_info": {
                "status": "allowed_warning",
                "resetsAt": 1709856000,
                "rateLimitType": "five_hour",
                "utilization": 0.85
            }
        }"#;
        let msg: InboundMsg = serde_json::from_str(json).unwrap();
        match msg {
            InboundMsg::RateLimitEvent {
                rate_limit_info, ..
            } => {
                assert_eq!(rate_limit_info.status, "allowed_warning");
                assert_eq!(rate_limit_info.resets_at, Some(1709856000.0));
                assert_eq!(rate_limit_info.utilization, Some(0.85));
            }
            _ => panic!("expected RateLimitEvent"),
        }
    }

    #[test]
    fn serialize_outbound_user() {
        let msg = OutboundMsg::User {
            content: Some(serde_json::json!("Hello")),
            session_id: Some("sess-1".into()),
            message: None,
            parent_tool_use_id: None,
        };
        let json = serde_json::to_string(&msg).unwrap();
        assert!(json.contains(r#""type":"user""#));
        assert!(json.contains(r#""content":"Hello""#));
    }

    #[test]
    fn serialize_control_response() {
        let msg = OutboundMsg::ControlResponse {
            response: ControlResponseInner {
                subtype: "success".into(),
                request_id: "req-1".into(),
                response: Some(serde_json::json!({})),
                error: None,
                extra: HashMap::new(),
            },
        };
        let json = serde_json::to_string(&msg).unwrap();
        assert!(json.contains(r#""type":"control_response""#));
        assert!(json.contains(r#""subtype":"success""#));
    }

    #[test]
    fn parse_system_msg_with_extras() {
        let json = r#"{
            "type": "system",
            "subtype": "init",
            "version": "1.0.0",
            "tools_count": 15
        }"#;
        let msg: InboundMsg = serde_json::from_str(json).unwrap();
        match msg {
            InboundMsg::System { subtype, extra, .. } => {
                assert_eq!(subtype, "init");
                assert_eq!(extra["version"], "1.0.0");
            }
            _ => panic!("expected System"),
        }
    }

    #[test]
    fn bare_stream_type_detection() {
        assert!(is_bare_stream_type("content_block_delta"));
        assert!(is_bare_stream_type("message_start"));
        assert!(!is_bare_stream_type("stream_event"));
        assert!(!is_bare_stream_type("result"));
    }

    #[test]
    fn parse_tool_progress() {
        let json = r#"{
            "type": "tool_progress",
            "tool_use_id": "toolu_123",
            "tool_name": "Bash",
            "elapsed_time_seconds": 12.5,
            "session_id": "sess-1",
            "uuid": "u1"
        }"#;
        let msg: InboundMsg = serde_json::from_str(json).unwrap();
        match msg {
            InboundMsg::ToolProgress {
                tool_use_id,
                tool_name,
                elapsed_time_seconds,
                ..
            } => {
                assert_eq!(tool_use_id, "toolu_123");
                assert_eq!(tool_name.as_deref(), Some("Bash"));
                assert!((elapsed_time_seconds.unwrap() - 12.5).abs() < f64::EPSILON);
            }
            _ => panic!("expected ToolProgress"),
        }
    }

    #[test]
    fn parse_keep_alive() {
        let json = r#"{"type": "keep_alive"}"#;
        let msg: InboundMsg = serde_json::from_str(json).unwrap();
        assert!(matches!(msg, InboundMsg::KeepAlive {}));
    }

    #[test]
    fn parse_server_tool_use_block() {
        let json = r#"{
            "type": "stream_event",
            "uuid": "u1",
            "session_id": "s1",
            "event": {
                "type": "content_block_start",
                "index": 0,
                "content_block": {
                    "type": "server_tool_use",
                    "id": "stu_123",
                    "name": "web_search",
                    "input": {"query": "rust serde"}
                }
            }
        }"#;
        let msg: InboundMsg = serde_json::from_str(json).unwrap();
        match msg {
            InboundMsg::StreamEvent { event, .. } => match event {
                StreamEvent::ContentBlockStart {
                    content_block: ContentBlock::ServerToolUse(st),
                    ..
                } => {
                    assert_eq!(st.name, "web_search");
                }
                _ => panic!("expected ContentBlockStart with ServerToolUse"),
            },
            _ => panic!("expected StreamEvent"),
        }
    }

    #[test]
    fn parse_unknown_content_block() {
        let json = r#"{
            "type": "stream_event",
            "uuid": "u1",
            "session_id": "s1",
            "event": {
                "type": "content_block_start",
                "index": 0,
                "content_block": {
                    "type": "web_search_20250305",
                    "id": "ws_123"
                }
            }
        }"#;
        let msg: InboundMsg = serde_json::from_str(json).unwrap();
        match msg {
            InboundMsg::StreamEvent { event, .. } => match event {
                StreamEvent::ContentBlockStart {
                    content_block: ContentBlock::Unknown,
                    ..
                } => {}
                _ => panic!("expected ContentBlockStart with Unknown"),
            },
            _ => panic!("expected StreamEvent"),
        }
    }

    #[test]
    fn parse_thinking_block() {
        let json = r#"{
            "type": "stream_event",
            "uuid": "u1",
            "session_id": "s1",
            "event": {
                "type": "content_block_start",
                "index": 0,
                "content_block": {
                    "type": "thinking",
                    "thinking": "",
                    "signature": ""
                }
            }
        }"#;
        let msg: InboundMsg = serde_json::from_str(json).unwrap();
        match msg {
            InboundMsg::StreamEvent { event, .. } => match event {
                StreamEvent::ContentBlockStart {
                    content_block: ContentBlock::Thinking(tb),
                    ..
                } => {
                    assert_eq!(tb.thinking, "");
                }
                _ => panic!("expected ContentBlockStart with Thinking"),
            },
            _ => panic!("expected StreamEvent"),
        }
    }
}
