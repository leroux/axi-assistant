//! Frontend callback trait for Agent Hub.
//!
//! Defines the interface that a frontend (Discord, CLI, etc.) implements
//! to receive notifications from the agent orchestration layer.

use std::future::Future;
use std::pin::Pin;

/// Result type for async callbacks.
pub type CallbackResult = Pin<Box<dyn Future<Output = ()> + Send>>;

/// Frontend callbacks — the hub calls these to notify the frontend
/// about agent lifecycle events and to send messages to users.
///
/// All methods are async. We use trait objects rather than generics
/// so the hub can store a single Box<dyn FrontendCallbacks>.
pub trait FrontendCallbacks: Send + Sync {
    // Core messaging
    fn post_message(&self, agent_name: &str, text: &str) -> CallbackResult;
    fn post_system(&self, agent_name: &str, text: &str) -> CallbackResult;

    // Lifecycle events
    fn on_wake(&self, agent_name: &str) -> CallbackResult;
    fn on_sleep(&self, agent_name: &str) -> CallbackResult;
    fn on_session_id(&self, agent_name: &str, session_id: &str) -> CallbackResult;

    // Session events
    fn on_spawn(&self, agent_name: &str) -> CallbackResult;
    fn on_kill(&self, agent_name: &str, session_id: Option<&str>) -> CallbackResult;

    // Broadcast
    fn broadcast(&self, message: &str) -> CallbackResult;
    fn schedule_rate_limit_expiry(&self, delay_secs: f64);

    // Idle monitoring
    fn on_idle_reminder(&self, agent_name: &str, idle_minutes: f64) -> CallbackResult;

    // Hot restart
    fn on_reconnect(&self, agent_name: &str, was_mid_task: bool) -> CallbackResult;

    // Shutdown
    fn close_app(&self) -> CallbackResult;
    fn kill_process(&self) -> CallbackResult;
    fn send_goodbye(&self) -> CallbackResult;
}
