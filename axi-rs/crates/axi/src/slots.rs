//! Agent scheduler — slot management and priority-based eviction.
//!
//! Manages which agents hold awake slots. When slots are full:
//! 1. Evicts the longest-idle non-busy agent (background before interactive).
//! 2. If all agents are busy, queues the request and marks an agent for
//!    deferred eviction (sleep after its current turn completes).

use std::collections::{HashSet, VecDeque};
use std::future::Future;
use std::pin::Pin;
use std::sync::Arc;

use chrono::{DateTime, Utc};
use tokio::sync::{Mutex, Notify};
use tracing::{debug, info, warn};

use crate::types::HubError;

/// Callback type for sleeping an agent.
pub type SleepFn =
    Arc<dyn Fn(&str) -> Pin<Box<dyn Future<Output = ()> + Send>> + Send + Sync>;

/// Callback to get current sessions.
pub type GetSessionsFn = Arc<dyn Fn() -> Vec<SessionInfo> + Send + Sync>;

/// Minimal info the scheduler needs about a session for eviction decisions.
#[derive(Debug, Clone)]
pub struct SessionInfo {
    pub name: String,
    pub is_awake: bool,
    pub is_busy: bool,
    pub is_bridge_busy: bool,
    pub last_activity: DateTime<Utc>,
    pub query_started: Option<DateTime<Utc>>,
}

pub struct Scheduler {
    max_slots: usize,
    protected: HashSet<String>,
    slots: Mutex<HashSet<String>>,
    waiters: Mutex<VecDeque<(String, Arc<Notify>)>>,
    yield_set: Mutex<HashSet<String>>,
    interactive: Mutex<HashSet<String>>,
    get_sessions: GetSessionsFn,
    sleep_fn: SleepFn,
}

impl Scheduler {
    pub fn new(
        max_slots: usize,
        protected: HashSet<String>,
        get_sessions: GetSessionsFn,
        sleep_fn: SleepFn,
    ) -> Self {
        info!(
            "Scheduler initialized: max_slots={}, protected={:?}",
            max_slots, protected
        );
        Self {
            max_slots,
            protected,
            slots: Mutex::new(HashSet::new()),
            waiters: Mutex::new(VecDeque::new()),
            yield_set: Mutex::new(HashSet::new()),
            interactive: Mutex::new(HashSet::new()),
            get_sessions,
            sleep_fn,
        }
    }

    /// Acquire an awake slot. Blocks until one is available.
    pub async fn request_slot(
        &self,
        agent_name: &str,
        timeout_secs: f64,
    ) -> Result<(), HubError> {
        {
            let mut slots = self.slots.lock().await;
            if slots.contains(agent_name) {
                return Ok(());
            }

            // Fast path: slot available
            if slots.len() < self.max_slots {
                slots.insert(agent_name.to_string());
                debug!(
                    "Slot granted to '{}' ({}/{})",
                    agent_name,
                    slots.len(),
                    self.max_slots
                );
                return Ok(());
            }
        }

        // Try evicting idle agents
        loop {
            let slots_len = self.slots.lock().await.len();
            if slots_len < self.max_slots {
                break;
            }
            if !self.evict_idle(agent_name).await {
                break;
            }
        }

        {
            let mut slots = self.slots.lock().await;
            if slots.len() < self.max_slots {
                slots.insert(agent_name.to_string());
                debug!(
                    "Slot granted to '{}' after eviction ({}/{})",
                    agent_name,
                    slots.len(),
                    self.max_slots
                );
                return Ok(());
            }
        }

        // All agents busy — queue and mark an eviction target
        let notify = Arc::new(Notify::new());
        {
            let mut waiters = self.waiters.lock().await;
            waiters.push_back((agent_name.to_string(), notify.clone()));
            self.select_yield_target(agent_name).await;
            info!(
                "All {} slots busy, '{}' queued (position {})",
                self.max_slots,
                agent_name,
                waiters.len()
            );
        }

        // Wait for notification
        let timeout = tokio::time::Duration::from_secs_f64(timeout_secs);
        if tokio::time::timeout(timeout, notify.notified()).await == Ok(()) {
            // Slot was granted by release_slot
            info!(
                "Slot granted to '{}' from wait queue",
                agent_name
            );
            Ok(())
        } else {
            // Timeout — remove from waiters
            let slots = self.slots.lock().await;
            if slots.contains(agent_name) {
                return Ok(());
            }
            let mut waiters = self.waiters.lock().await;
            waiters.retain(|(name, _)| name != agent_name);
            Err(HubError::ConcurrencyLimit(format!(
                "Cannot wake agent '{}': all {} slots busy after {:.0}s wait",
                agent_name, self.max_slots, timeout_secs
            )))
        }
    }

    /// Release a slot. Called when an agent sleeps.
    pub async fn release_slot(&self, agent_name: &str) {
        let was_held;
        {
            let mut slots = self.slots.lock().await;
            was_held = slots.remove(agent_name);
            let mut yield_set = self.yield_set.lock().await;
            yield_set.remove(agent_name);
            let mut interactive = self.interactive.lock().await;
            interactive.remove(agent_name);

            if !was_held {
                return;
            }

            debug!(
                "Slot released by '{}' ({}/{})",
                agent_name,
                slots.len(),
                self.max_slots
            );

            // Grant slot to next waiter
            let mut waiters = self.waiters.lock().await;
            if let Some((waiter_name, notify)) = waiters.pop_front() {
                slots.insert(waiter_name.clone());
                notify.notify_one();
                info!(
                    "Slot granted to waiter '{}' (freed by '{}')",
                    waiter_name, agent_name
                );
            }
        }
    }

    /// Check if this agent should sleep after finishing its current turn.
    pub async fn should_yield(&self, agent_name: &str) -> bool {
        let yield_set = self.yield_set.lock().await;
        yield_set.contains(agent_name)
    }

    /// Register an agent that's already awake (e.g. reconnected from bridge).
    pub async fn restore_slot(&self, agent_name: &str) {
        let mut slots = self.slots.lock().await;
        slots.insert(agent_name.to_string());
        debug!(
            "Slot restored for '{}' ({}/{})",
            agent_name,
            slots.len(),
            self.max_slots
        );
    }

    pub async fn mark_interactive(&self, agent_name: &str) {
        let mut interactive = self.interactive.lock().await;
        interactive.insert(agent_name.to_string());
    }

    pub async fn mark_background(&self, agent_name: &str) {
        let mut interactive = self.interactive.lock().await;
        interactive.remove(agent_name);
    }

    pub async fn has_waiters(&self) -> bool {
        let waiters = self.waiters.lock().await;
        !waiters.is_empty()
    }

    pub async fn slot_count(&self) -> usize {
        let slots = self.slots.lock().await;
        slots.len()
    }

    pub async fn status(&self) -> serde_json::Value {
        let slots = self.slots.lock().await;
        let waiters = self.waiters.lock().await;
        let yield_set = self.yield_set.lock().await;
        let interactive = self.interactive.lock().await;

        serde_json::json!({
            "max_slots": self.max_slots,
            "slots": slots.iter().collect::<Vec<_>>(),
            "slot_count": slots.len(),
            "waiters": waiters.iter().map(|(n, _)| n.as_str()).collect::<Vec<_>>(),
            "yield_targets": yield_set.iter().collect::<Vec<_>>(),
            "interactive": interactive.iter().collect::<Vec<_>>(),
            "protected": self.protected.iter().collect::<Vec<_>>(),
        })
    }

    // -----------------------------------------------------------------------
    // Internal: eviction
    // -----------------------------------------------------------------------

    async fn evict_idle(&self, exclude: &str) -> bool {
        let sessions = (self.get_sessions)();
        let interactive = self.interactive.lock().await;

        let mut background: Vec<(f64, &str, &SessionInfo)> = Vec::new();
        let mut interactive_candidates: Vec<(f64, &str, &SessionInfo)> = Vec::new();

        for s in &sessions {
            if s.name == exclude || self.protected.contains(&s.name) {
                continue;
            }
            if !s.is_awake || s.is_busy || s.is_bridge_busy {
                continue;
            }
            let idle_secs = (Utc::now() - s.last_activity).num_seconds() as f64;
            if interactive.contains(&s.name) {
                interactive_candidates.push((idle_secs, &s.name, s));
            } else {
                background.push((idle_secs, &s.name, s));
            }
        }

        // Try background first, then interactive
        for (bucket_name, candidates) in
            [("background", &mut background), ("interactive", &mut interactive_candidates)]
        {
            if candidates.is_empty() {
                continue;
            }
            candidates.sort_by(|a, b| b.0.partial_cmp(&a.0).unwrap());
            let (idle_secs, evict_name, _) = &candidates[0];
            info!(
                "Evicting idle {} agent '{}' (idle {:.0}s) to free slot",
                bucket_name, evict_name, idle_secs
            );
            (self.sleep_fn)(evict_name).await;
            return true;
        }

        false
    }

    async fn select_yield_target(&self, exclude: &str) {
        let sessions = (self.get_sessions)();
        let interactive = self.interactive.lock().await;
        let mut yield_set = self.yield_set.lock().await;

        let mut background: Vec<(f64, String)> = Vec::new();
        let mut interactive_candidates: Vec<(f64, String)> = Vec::new();

        for s in &sessions {
            if s.name == exclude || self.protected.contains(&s.name) {
                continue;
            }
            if yield_set.contains(&s.name) || !s.is_awake {
                continue;
            }
            let busy_secs = s
                .query_started
                .map_or(0.0, |qs| (Utc::now() - qs).num_seconds() as f64);
            if interactive.contains(&s.name) {
                interactive_candidates.push((busy_secs, s.name.clone()));
            } else {
                background.push((busy_secs, s.name.clone()));
            }
        }

        for candidates in [&mut background, &mut interactive_candidates] {
            if candidates.is_empty() {
                continue;
            }
            candidates.sort_by(|a, b| b.0.partial_cmp(&a.0).unwrap());
            let target = &candidates[0].1;
            yield_set.insert(target.clone());
            info!(
                "Marked '{}' for yield after current turn (busy {:.0}s)",
                target, candidates[0].0
            );
            return;
        }

        warn!(
            "No yield target available for waiter '{}' — all agents are protected",
            exclude
        );
    }
}
