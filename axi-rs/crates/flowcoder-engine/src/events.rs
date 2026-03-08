//! Engine stdout event types — JSON protocol emitted to outer client.
//!
//! In proxy mode, inner Claude's stdout passes through unchanged.
//! In flowchart mode, these engine-specific events bracket execution.

use std::collections::HashMap;

use serde::Serialize;

/// Engine event emitted as a JSON line to stdout.
#[derive(Debug, Serialize)]
#[serde(tag = "type")]
pub enum EngineEvent {
    #[serde(rename = "flowchart_start")]
    FlowchartStart {
        command: String,
        args: String,
        block_count: usize,
    },

    #[serde(rename = "block_start")]
    BlockStart {
        block_id: String,
        block_name: String,
        block_type: String,
        block_index: usize,
        total_blocks: usize,
    },

    #[serde(rename = "block_complete")]
    BlockComplete {
        block_id: String,
        block_name: String,
        success: bool,
        duration_ms: u64,
    },

    /// A claudewire message forwarded during a flowchart block.
    #[serde(rename = "forwarded")]
    Forwarded {
        message: serde_json::Value,
        block_id: String,
        block_name: String,
    },

    #[serde(rename = "flowchart_complete")]
    FlowchartComplete {
        status: String,
        duration_ms: u64,
        blocks_executed: usize,
        cost_usd: f64,
        variables: HashMap<String, String>,
    },

    #[serde(rename = "engine_status")]
    EngineStatus {
        mode: String,
        current_block: Option<String>,
        blocks_done: usize,
        total_blocks: usize,
        paused: bool,
    },

    #[serde(rename = "engine_log")]
    EngineLog { message: String },
}

/// Emit an engine event as a JSON line to stdout.
pub fn emit(event: &EngineEvent) {
    if let Ok(json) = serde_json::to_string(event) {
        println!("{json}");
    }
}

/// Emit a raw JSON value as a line to stdout (for proxy passthrough).
pub fn emit_raw(value: &serde_json::Value) {
    if let Ok(json) = serde_json::to_string(value) {
        println!("{json}");
    }
}

/// Format an `ExecutionStatus` as a human-readable string.
pub fn format_status(status: &flowchart_runner::ExecutionStatus) -> String {
    match status {
        flowchart_runner::ExecutionStatus::Completed => "completed".into(),
        flowchart_runner::ExecutionStatus::Halted { exit_code } => {
            format!("halted:{exit_code}")
        }
        flowchart_runner::ExecutionStatus::Interrupted => "interrupted".into(),
        flowchart_runner::ExecutionStatus::Error(msg) => format!("error:{msg}"),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn flowchart_start_serializes_correctly() {
        let event = EngineEvent::FlowchartStart {
            command: "story".into(),
            args: "dragons".into(),
            block_count: 3,
        };
        let json: serde_json::Value = serde_json::to_value(&event).unwrap();
        assert_eq!(json["type"], "flowchart_start");
        assert_eq!(json["command"], "story");
        assert_eq!(json["args"], "dragons");
        assert_eq!(json["block_count"], 3);
    }

    #[test]
    fn block_start_serializes_correctly() {
        let event = EngineEvent::BlockStart {
            block_id: "p1".into(),
            block_name: "Research".into(),
            block_type: "prompt".into(),
            block_index: 0,
            total_blocks: 5,
        };
        let json: serde_json::Value = serde_json::to_value(&event).unwrap();
        assert_eq!(json["type"], "block_start");
        assert_eq!(json["block_id"], "p1");
        assert_eq!(json["block_name"], "Research");
        assert_eq!(json["block_type"], "prompt");
        assert_eq!(json["block_index"], 0);
        assert_eq!(json["total_blocks"], 5);
    }

    #[test]
    fn block_complete_serializes_correctly() {
        let event = EngineEvent::BlockComplete {
            block_id: "p1".into(),
            block_name: "Research".into(),
            success: true,
            duration_ms: 1500,
        };
        let json: serde_json::Value = serde_json::to_value(&event).unwrap();
        assert_eq!(json["type"], "block_complete");
        assert_eq!(json["success"], true);
        assert_eq!(json["duration_ms"], 1500);
    }

    #[test]
    fn forwarded_wraps_inner_message() {
        let inner = serde_json::json!({"type": "stream_event", "event": {"type": "text_delta"}});
        let event = EngineEvent::Forwarded {
            message: inner.clone(),
            block_id: "b1".into(),
            block_name: "Ask".into(),
        };
        let json: serde_json::Value = serde_json::to_value(&event).unwrap();
        assert_eq!(json["type"], "forwarded");
        assert_eq!(json["message"], inner);
        assert_eq!(json["block_id"], "b1");
    }

    #[test]
    fn flowchart_complete_includes_variables() {
        let mut vars = HashMap::new();
        vars.insert("result".into(), "done".into());
        let event = EngineEvent::FlowchartComplete {
            status: "completed".into(),
            duration_ms: 5000,
            blocks_executed: 3,
            cost_usd: 0.05,
            variables: vars,
        };
        let json: serde_json::Value = serde_json::to_value(&event).unwrap();
        assert_eq!(json["type"], "flowchart_complete");
        assert_eq!(json["status"], "completed");
        assert_eq!(json["variables"]["result"], "done");
    }

    #[test]
    fn engine_status_serializes() {
        let event = EngineEvent::EngineStatus {
            mode: "flowchart".into(),
            current_block: Some("Research".into()),
            blocks_done: 2,
            total_blocks: 5,
            paused: true,
        };
        let json: serde_json::Value = serde_json::to_value(&event).unwrap();
        assert_eq!(json["type"], "engine_status");
        assert_eq!(json["mode"], "flowchart");
        assert_eq!(json["current_block"], "Research");
        assert_eq!(json["paused"], true);
    }

    #[test]
    fn engine_log_serializes() {
        let event = EngineEvent::EngineLog {
            message: "test message".into(),
        };
        let json: serde_json::Value = serde_json::to_value(&event).unwrap();
        assert_eq!(json["type"], "engine_log");
        assert_eq!(json["message"], "test message");
    }

    #[test]
    fn format_status_variants() {
        assert_eq!(
            format_status(&flowchart_runner::ExecutionStatus::Completed),
            "completed"
        );
        assert_eq!(
            format_status(&flowchart_runner::ExecutionStatus::Halted { exit_code: 42 }),
            "halted:42"
        );
        assert_eq!(
            format_status(&flowchart_runner::ExecutionStatus::Interrupted),
            "interrupted"
        );
        assert_eq!(
            format_status(&flowchart_runner::ExecutionStatus::Error("oops".into())),
            "error:oops"
        );
    }
}
