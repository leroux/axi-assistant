use std::collections::HashMap;

/// Display/reporting callbacks for flowchart execution.
///
/// All methods are synchronous — these are notification callbacks,
/// not async operations. The executor calls them inline.
pub trait Protocol: Send {
    fn on_block_start(&mut self, block_id: &str, block_name: &str, block_type: &str);
    fn on_block_complete(
        &mut self,
        block_id: &str,
        block_name: &str,
        success: bool,
        duration_ms: u64,
    );
    fn on_stream_text(&mut self, text: &str);
    fn on_flowchart_start(&mut self, command: &str, args: &str, block_count: usize);
    fn on_flowchart_complete(&mut self, result: &FlowchartResult);
    fn on_forwarded_message(
        &mut self,
        msg: &serde_json::Value,
        block_id: &str,
        block_name: &str,
    );
    fn on_log(&mut self, message: &str);
}

/// Outcome of a complete flowchart execution.
#[derive(Debug, Clone)]
pub struct FlowchartResult {
    pub variables: HashMap<String, String>,
    pub status: ExecutionStatus,
    pub duration_ms: u64,
    pub blocks_executed: usize,
    pub cost_usd: f64,
}

/// How the flowchart execution ended.
#[derive(Debug, Clone)]
pub enum ExecutionStatus {
    Completed,
    Halted { exit_code: i32 },
    Interrupted,
    Error(String),
}
