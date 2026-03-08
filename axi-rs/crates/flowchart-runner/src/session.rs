use std::future::Future;

use crate::error::ExecutionError;
use crate::protocol::Protocol;

/// Result of a single query to the coding agent.
#[derive(Debug, Clone)]
pub struct QueryResult {
    pub response_text: String,
    pub cost_usd: f64,
    pub duration_ms: u64,
    pub session_id: Option<String>,
}

/// Drives a coding agent session (Claude CLI, mock, etc).
///
/// The executor passes `&mut dyn Protocol` into `query()` so the session
/// can forward stream events (text deltas, assistant messages) to the
/// display layer without shared ownership.
pub trait Session: Send {
    /// Send a prompt, wait for completion.
    /// Forward intermediate events (stream text, assistant messages) to `protocol`.
    fn query(
        &mut self,
        prompt: &str,
        block_id: &str,
        block_name: &str,
        protocol: &mut dyn Protocol,
    ) -> impl Future<Output = Result<QueryResult, ExecutionError>> + Send;

    /// Reset conversation context. Cost accumulation survives this.
    fn clear(&mut self) -> impl Future<Output = Result<(), ExecutionError>> + Send;

    /// Terminate the session.
    fn stop(&mut self) -> impl Future<Output = ()> + Send;

    /// Graceful interrupt: abort current API call, session stays alive.
    fn interrupt(&mut self) -> impl Future<Output = Result<(), ExecutionError>> + Send;

    /// Total cost accumulated across all queries and clears.
    fn total_cost(&self) -> f64;
}
