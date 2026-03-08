//! `EngineProtocol` — implements `flowchart_runner::Protocol` by emitting
//! JSON events to stdout for the outer client to consume.

use flowchart_runner::protocol::{FlowchartResult, Protocol};

use crate::events::{self, EngineEvent};

/// Protocol implementation that emits engine events as JSON to stdout.
pub struct EngineProtocol {
    /// Total blocks in current flowchart (set on `flowchart_start`).
    total_blocks: usize,
    /// Blocks completed so far.
    blocks_done: usize,
    /// Current block being executed (for status queries).
    current_block: Option<String>,
}

impl EngineProtocol {
    pub const fn new() -> Self {
        Self {
            total_blocks: 0,
            blocks_done: 0,
            current_block: None,
        }
    }

    #[allow(dead_code)]
    pub const fn blocks_done(&self) -> usize {
        self.blocks_done
    }

    pub const fn total_blocks(&self) -> usize {
        self.total_blocks
    }

    #[allow(dead_code)]
    pub fn current_block(&self) -> Option<&str> {
        self.current_block.as_deref()
    }
}

impl Protocol for EngineProtocol {
    fn on_block_start(&mut self, block_id: &str, block_name: &str, block_type: &str) {
        self.current_block = Some(block_name.to_owned());
        events::emit(&EngineEvent::BlockStart {
            block_id: block_id.into(),
            block_name: block_name.into(),
            block_type: block_type.into(),
            block_index: self.blocks_done,
            total_blocks: self.total_blocks,
        });
    }

    fn on_block_complete(
        &mut self,
        block_id: &str,
        block_name: &str,
        success: bool,
        duration_ms: u64,
    ) {
        self.blocks_done += 1;
        self.current_block = None;
        events::emit(&EngineEvent::BlockComplete {
            block_id: block_id.into(),
            block_name: block_name.into(),
            success,
            duration_ms,
        });
    }

    fn on_stream_text(&mut self, _text: &str) {
        // Stream text is forwarded via on_forwarded_message as part of
        // the full stream_event message. No separate engine event needed.
    }

    fn on_flowchart_start(&mut self, command: &str, args: &str, block_count: usize) {
        self.total_blocks = block_count;
        self.blocks_done = 0;
        events::emit(&EngineEvent::FlowchartStart {
            command: command.into(),
            args: args.into(),
            block_count,
        });
    }

    fn on_flowchart_complete(&mut self, result: &FlowchartResult) {
        events::emit(&EngineEvent::FlowchartComplete {
            status: events::format_status(&result.status),
            duration_ms: result.duration_ms,
            blocks_executed: result.blocks_executed,
            cost_usd: result.cost_usd,
            variables: result.variables.clone(),
        });
    }

    fn on_forwarded_message(
        &mut self,
        msg: &serde_json::Value,
        block_id: &str,
        block_name: &str,
    ) {
        events::emit(&EngineEvent::Forwarded {
            message: msg.clone(),
            block_id: block_id.into(),
            block_name: block_name.into(),
        });
    }

    fn on_log(&mut self, message: &str) {
        events::emit(&EngineEvent::EngineLog {
            message: message.into(),
        });
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use flowchart_runner::Protocol;

    #[test]
    fn protocol_tracks_block_count() {
        let mut p = EngineProtocol::new();
        assert_eq!(p.total_blocks(), 0);
        assert_eq!(p.blocks_done(), 0);
        assert!(p.current_block().is_none());

        p.on_flowchart_start("test", "args", 5);
        assert_eq!(p.total_blocks(), 5);
        assert_eq!(p.blocks_done(), 0);

        p.on_block_start("b1", "First", "prompt");
        assert_eq!(p.current_block(), Some("First"));

        p.on_block_complete("b1", "First", true, 100);
        assert_eq!(p.blocks_done(), 1);
        assert!(p.current_block().is_none());

        p.on_block_start("b2", "Second", "prompt");
        p.on_block_complete("b2", "Second", true, 200);
        assert_eq!(p.blocks_done(), 2);
    }

    #[test]
    fn protocol_resets_on_new_flowchart() {
        let mut p = EngineProtocol::new();
        p.on_flowchart_start("first", "", 3);
        p.on_block_start("b1", "X", "prompt");
        p.on_block_complete("b1", "X", true, 50);
        assert_eq!(p.blocks_done(), 1);

        // Starting a new flowchart resets blocks_done
        p.on_flowchart_start("second", "", 10);
        assert_eq!(p.blocks_done(), 0);
        assert_eq!(p.total_blocks(), 10);
    }
}
