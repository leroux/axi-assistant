//! Core types for agent orchestration.
//!
//! AgentSession is a flat data container — lifecycle operations are
//! module-level functions in lifecycle.rs, registry.rs, etc.

use std::collections::VecDeque;
use std::sync::Arc;

use chrono::{DateTime, Utc};
use tokio::sync::Mutex;

use claudewire::events::ActivityState;

/// Message content: either plain text or structured content blocks (JSON array).
#[derive(Debug, Clone)]
pub enum MessageContent {
    Text(String),
    Blocks(Vec<serde_json::Value>),
}

impl MessageContent {
    pub fn preview(&self, max_len: usize) -> String {
        match self {
            MessageContent::Text(s) => {
                if s.len() <= max_len {
                    s.clone()
                } else {
                    format!("{}...", &s[..max_len])
                }
            }
            MessageContent::Blocks(blocks) => {
                let s = serde_json::to_string(blocks).unwrap_or_default();
                if s.len() <= max_len {
                    s
                } else {
                    format!("{}...", &s[..max_len])
                }
            }
        }
    }
}

/// Queued message entry — content + opaque metadata.
#[derive(Debug, Clone)]
pub struct QueuedMessage {
    pub content: MessageContent,
    pub metadata: Option<serde_json::Value>,
}

/// One agent's state as seen by the orchestration layer.
pub struct AgentSession {
    pub name: String,
    pub agent_type: String,
    /// Opaque client handle — None when sleeping.
    pub client: Option<Box<dyn std::any::Any + Send + Sync>>,
    pub cwd: String,
    pub query_lock: Arc<Mutex<()>>,
    pub message_queue: VecDeque<QueuedMessage>,
    pub last_activity: DateTime<Utc>,
    pub idle_reminder_count: u32,
    pub session_id: Option<String>,
    pub system_prompt: Option<serde_json::Value>,
    pub system_prompt_hash: Option<String>,
    pub mcp_servers: Option<serde_json::Value>,
    pub mcp_server_names: Option<Vec<String>>,
    pub reconnecting: bool,
    pub bridge_busy: bool,
    pub activity: ActivityState,
    pub plan_mode: bool,
    pub last_failed_resume_id: Option<String>,
    /// Opaque frontend state — the bot casts this to its own type.
    pub frontend_state: Option<Box<dyn std::any::Any + Send + Sync>>,
    pub compact_instructions: Option<String>,
    pub context_tokens: u64,
    pub context_window: u64,
}

impl AgentSession {
    pub fn new(name: String) -> Self {
        Self {
            name,
            agent_type: "claude_code".to_string(),
            client: None,
            cwd: String::new(),
            query_lock: Arc::new(Mutex::new(())),
            message_queue: VecDeque::new(),
            last_activity: Utc::now(),
            idle_reminder_count: 0,
            session_id: None,
            system_prompt: None,
            system_prompt_hash: None,
            mcp_servers: None,
            mcp_server_names: None,
            reconnecting: false,
            bridge_busy: false,
            activity: ActivityState::default(),
            plan_mode: false,
            last_failed_resume_id: None,
            frontend_state: None,
            compact_instructions: None,
            context_tokens: 0,
            context_window: 0,
        }
    }

    pub fn is_awake(&self) -> bool {
        self.client.is_some()
    }
}

// ---------------------------------------------------------------------------
// Usage tracking
// ---------------------------------------------------------------------------

#[derive(Debug, Clone)]
pub struct SessionUsage {
    pub agent_name: String,
    pub queries: u64,
    pub total_cost_usd: f64,
    pub total_turns: u64,
    pub total_duration_ms: u64,
    pub total_input_tokens: u64,
    pub total_output_tokens: u64,
    pub first_query: Option<DateTime<Utc>>,
    pub last_query: Option<DateTime<Utc>>,
}

impl SessionUsage {
    pub fn new(agent_name: String) -> Self {
        Self {
            agent_name,
            queries: 0,
            total_cost_usd: 0.0,
            total_turns: 0,
            total_duration_ms: 0,
            total_input_tokens: 0,
            total_output_tokens: 0,
            first_query: None,
            last_query: None,
        }
    }
}

#[derive(Debug, Clone)]
pub struct RateLimitQuota {
    pub status: String,
    pub resets_at: DateTime<Utc>,
    pub rate_limit_type: String,
    pub utilization: Option<f64>,
    pub updated_at: DateTime<Utc>,
}

// ---------------------------------------------------------------------------
// Errors
// ---------------------------------------------------------------------------

#[derive(Debug, thiserror::Error)]
pub enum HubError {
    #[error("concurrency limit reached: {0}")]
    ConcurrencyLimit(String),
    #[error("agent not found: {0}")]
    NotFound(String),
    #[error("agent not awake: {0}")]
    NotAwake(String),
    #[error("query failed: {0}")]
    QueryFailed(String),
    #[error("query timeout for agent {0}")]
    QueryTimeout(String),
    #[error("{0}")]
    Other(String),
}
