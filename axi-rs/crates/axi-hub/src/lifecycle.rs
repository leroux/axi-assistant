//! Agent lifecycle — wake, sleep, and transport management.
//!
//! Functions that operate on hub + session. `AgentHub` delegates to these.

use chrono::Utc;
use tracing::{debug, info, warn};

use crate::hub::AgentHub;
use crate::types::{AgentSession, HubError, QueuedMessage};
use claudewire::events::ActivityState;

// ---------------------------------------------------------------------------
// Pure helpers
// ---------------------------------------------------------------------------

pub fn is_awake(session: &AgentSession) -> bool {
    session.client.is_some()
}

pub fn is_processing(session: &AgentSession) -> bool {
    // Try to acquire the lock — if we can't, it's held (processing)
    session.query_lock.try_lock().is_err()
}

pub fn count_awake(sessions: &std::collections::HashMap<String, AgentSession>) -> usize {
    sessions.values().filter(|s| s.client.is_some()).count()
}

pub fn reset_activity(session: &mut AgentSession) {
    session.last_activity = Utc::now();
    session.idle_reminder_count = 0;
    session.activity = ActivityState {
        phase: claudewire::events::Phase::Starting,
        query_started: Some(Utc::now()),
        ..Default::default()
    };
}

// ---------------------------------------------------------------------------
// Sleep
// ---------------------------------------------------------------------------

pub async fn sleep_agent(hub: &AgentHub, name: &str, force: bool) {
    let sessions = hub.sessions.lock().await;
    let session = match sessions.get(name) {
        Some(s) => s,
        None => return,
    };

    if !force && is_processing(session) {
        debug!("Skipping sleep for '{}' — query_lock is held", name);
        return;
    }

    if session.client.is_none() {
        return;
    }

    info!("Sleeping agent '{}'", name);
    drop(sessions);

    // Disconnect client
    (hub.disconnect_client)(name).await;

    let mut sessions = hub.sessions.lock().await;
    if let Some(session) = sessions.get_mut(name) {
        session.bridge_busy = false;
        session.client = None;
    }

    hub.scheduler.release_slot(name).await;
    info!("Agent '{}' is now sleeping", name);
}

// ---------------------------------------------------------------------------
// Wake
// ---------------------------------------------------------------------------

pub async fn wake_agent(hub: &AgentHub, name: &str) -> Result<(), HubError> {
    {
        let sessions = hub.sessions.lock().await;
        let session = sessions
            .get(name)
            .ok_or_else(|| HubError::NotFound(name.to_string()))?;
        if is_awake(session) {
            return Ok(());
        }

        // Fail fast if cwd doesn't exist
        if !session.cwd.is_empty() && !std::path::Path::new(&session.cwd).is_dir() {
            return Err(HubError::Other(format!(
                "Working directory does not exist: {}",
                session.cwd
            )));
        }
    }

    let _wake_lock = hub.wake_lock.lock().await;

    // Double-check after acquiring lock
    {
        let sessions = hub.sessions.lock().await;
        if let Some(session) = sessions.get(name) {
            if is_awake(session) {
                return Ok(());
            }
        }
    }

    hub.scheduler
        .request_slot(name, hub.slot_timeout)
        .await?;

    let resume_id = {
        let sessions = hub.sessions.lock().await;
        sessions
            .get(name)
            .and_then(|s| s.session_id.clone())
    };

    info!("Waking agent '{}' (session_id={:?})", name, resume_id);

    match (hub.create_client)(name, resume_id.as_deref()).await {
        Ok(client) => {
            let mut sessions = hub.sessions.lock().await;
            if let Some(session) = sessions.get_mut(name) {
                session.client = Some(client);
                session.last_failed_resume_id = None;
            }
            info!(
                "Agent '{}' is now awake (resumed={:?})",
                name, resume_id
            );
        }
        Err(_) if resume_id.is_some() => {
            warn!(
                "Failed to resume agent '{}' with session_id={:?}, retrying fresh",
                name, resume_id
            );
            if let Ok(client) = (hub.create_client)(name, None).await {
                let mut sessions = hub.sessions.lock().await;
                if let Some(session) = sessions.get_mut(name) {
                    session.client = Some(client);
                    session.last_failed_resume_id = resume_id.clone();
                    session.session_id = None;
                }
                warn!(
                    "Agent '{}' woke with fresh session (previous context lost)",
                    name
                );
            } else {
                hub.scheduler.release_slot(name).await;
                return Err(HubError::Other(format!(
                    "Failed to create client for agent '{name}'"
                )));
            }
        }
        Err(_) => {
            hub.scheduler.release_slot(name).await;
            return Err(HubError::Other(format!(
                "Failed to create client for agent '{name}'"
            )));
        }
    }

    hub.callbacks.on_wake(name).await;
    Ok(())
}

// ---------------------------------------------------------------------------
// Wake-or-queue
// ---------------------------------------------------------------------------

pub async fn wake_or_queue(
    hub: &AgentHub,
    name: &str,
    content: crate::types::MessageContent,
    metadata: Option<serde_json::Value>,
) -> bool {
    match wake_agent(hub, name).await {
        Ok(()) => true,
        Err(HubError::ConcurrencyLimit(_)) => {
            let mut sessions = hub.sessions.lock().await;
            if let Some(session) = sessions.get_mut(name) {
                session.message_queue.push_back(QueuedMessage { content, metadata });
                let position = session.message_queue.len();
                debug!(
                    "Concurrency limit hit for '{}', queuing message (position {})",
                    name, position
                );
            }
            false
        }
        Err(e) => {
            warn!("Failed to wake agent '{}': {}", name, e);
            false
        }
    }
}
