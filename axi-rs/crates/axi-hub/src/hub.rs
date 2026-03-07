//! `AgentHub` — multi-agent session orchestrator.
//!
//! Thin state holder. Logic lives in lifecycle, registry, messaging,
//! reconnect modules. Methods are thin delegation to module functions.

use std::collections::HashMap;
use std::future::Future;
use std::pin::Pin;
use std::sync::atomic::AtomicBool;
use std::sync::Arc;

use tokio::sync::Mutex;

use crate::callbacks::FrontendCallbacks;
use crate::procmux_wire::ProcmuxProcessConnection;
use crate::rate_limits::RateLimitTracker;
use crate::scheduler::Scheduler;
use crate::tasks::BackgroundTaskSet;
use crate::types::{AgentSession, MessageContent};

/// Factory: create a client for an agent. (name, `resume_session_id`) -> client
pub type CreateClientFn = Arc<
    dyn Fn(
            &str,
            Option<&str>,
        ) -> Pin<
            Box<
                dyn Future<Output = Result<Box<dyn std::any::Any + Send + Sync>, anyhow::Error>>
                    + Send,
            >,
        > + Send
        + Sync,
>;

/// Factory: disconnect a client. (name) -> ()
pub type DisconnectClientFn =
    Arc<dyn Fn(&str) -> Pin<Box<dyn Future<Output = ()> + Send>> + Send + Sync>;

/// Factory: send a query to an agent. (name, content) -> ()
pub type SendQueryFn = Arc<
    dyn Fn(&str, &MessageContent) -> Pin<Box<dyn Future<Output = ()> + Send>> + Send + Sync,
>;

pub struct AgentHub {
    pub sessions: Arc<Mutex<HashMap<String, AgentSession>>>,
    pub callbacks: Arc<dyn FrontendCallbacks>,
    pub scheduler: Arc<Scheduler>,
    pub rate_limits: Arc<Mutex<RateLimitTracker>>,
    pub tasks: BackgroundTaskSet,
    pub wake_lock: Mutex<()>,
    pub process_conn: Arc<Mutex<Option<ProcmuxProcessConnection>>>,

    // SDK factories
    pub create_client: CreateClientFn,
    pub disconnect_client: DisconnectClientFn,
    pub send_query: SendQueryFn,

    // Config
    pub query_timeout: f64,
    pub max_retries: u32,
    pub retry_base_delay: f64,
    pub slot_timeout: f64,

    pub shutdown_requested: AtomicBool,
}

impl AgentHub {
    /// Create a lightweight reference to this hub for use in spawned tasks.
    /// This clones the Arcs, not the underlying data.
    pub fn clone_ref(&self) -> Self {
        Self {
            sessions: self.sessions.clone(),
            callbacks: self.callbacks.clone(),
            scheduler: self.scheduler.clone(),
            rate_limits: self.rate_limits.clone(),
            tasks: BackgroundTaskSet::new(),
            wake_lock: Mutex::new(()),
            process_conn: self.process_conn.clone(),
            create_client: self.create_client.clone(),
            disconnect_client: self.disconnect_client.clone(),
            send_query: self.send_query.clone(),
            query_timeout: self.query_timeout,
            max_retries: self.max_retries,
            retry_base_delay: self.retry_base_delay,
            slot_timeout: self.slot_timeout,
            shutdown_requested: AtomicBool::new(false),
        }
    }
}
