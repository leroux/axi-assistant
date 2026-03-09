//! Agent lifecycle — wake, sleep, and transport management.

use chrono::Utc;
use tracing::{debug, info, warn};

use crate::state::BotState;
use crate::types::{AgentSession, HubError, QueuedMessage};
use crate::activity::ActivityState;

// ---------------------------------------------------------------------------
// Pure helpers
// ---------------------------------------------------------------------------

pub const fn is_awake(session: &AgentSession) -> bool {
    session.awake
}

pub fn is_processing(session: &AgentSession) -> bool {
    session.query_lock.try_lock().is_err()
}

pub fn count_awake(sessions: &std::collections::HashMap<String, AgentSession>) -> usize {
    sessions.values().filter(|s| s.awake).count()
}

pub fn reset_activity(session: &mut AgentSession) {
    session.last_activity = Utc::now();
    session.idle_reminder_count = 0;
    session.activity = ActivityState {
        phase: crate::activity::Phase::Starting,
        query_started: Some(Utc::now()),
        ..Default::default()
    };
}

// ---------------------------------------------------------------------------
// Awaiting input sentinel
// ---------------------------------------------------------------------------

pub async fn post_awaiting_input(state: &BotState, name: &str) {
    if !state.config.show_awaiting_input {
        return;
    }
    let mentions: String = state
        .config
        .allowed_user_ids
        .iter()
        .map(|uid| format!("<@{uid}>"))
        .collect::<Vec<_>>()
        .join(" ");
    crate::frontend::post_system(
        state,
        name,
        &format!("Bot has finished responding and is awaiting input. {mentions}"),
    )
    .await;
}

// ---------------------------------------------------------------------------
// Sleep
// ---------------------------------------------------------------------------

pub async fn sleep_agent(state: &BotState, name: &str, force: bool) {
    let sessions = state.sessions.lock().await;
    let session = match sessions.get(name) {
        Some(s) => s,
        None => return,
    };

    if !force && is_processing(session) {
        debug!("Skipping sleep for '{}' — query_lock is held", name);
        return;
    }

    if !session.awake {
        return;
    }

    info!("Sleeping agent '{}'", name);
    drop(sessions);

    crate::claude_process::disconnect_client(state, name).await;

    let mut sessions = state.sessions.lock().await;
    if let Some(session) = sessions.get_mut(name) {
        session.bridge_busy = false;
        session.awake = false;
    }

    state.scheduler().await.release_slot(name).await;
    info!("Agent '{}' is now sleeping", name);
}

// ---------------------------------------------------------------------------
// Wake
// ---------------------------------------------------------------------------

pub async fn wake_agent(state: &BotState, name: &str) -> Result<(), HubError> {
    {
        let sessions = state.sessions.lock().await;
        let session = sessions
            .get(name)
            .ok_or_else(|| HubError::NotFound(name.to_string()))?;
        if is_awake(session) {
            return Ok(());
        }

        if !session.cwd.is_empty() && !std::path::Path::new(&session.cwd).is_dir() {
            return Err(HubError::Other(format!(
                "Working directory does not exist: {}",
                session.cwd
            )));
        }
    }

    let _wake_lock = state.wake_lock.lock().await;

    // Double-check after acquiring lock
    {
        let sessions = state.sessions.lock().await;
        if let Some(session) = sessions.get(name) {
            if is_awake(session) {
                return Ok(());
            }
        }
    }

    state
        .scheduler()
        .await
        .request_slot(name, state.slot_timeout)
        .await?;

    let resume_id = {
        let sessions = state.sessions.lock().await;
        sessions.get(name).and_then(|s| s.session_id.clone())
    };

    info!("Waking agent '{}' (session_id={:?})", name, resume_id);

    match crate::claude_process::create_client(state, name, resume_id.as_deref()).await {
        Ok(()) => {
            let mut sessions = state.sessions.lock().await;
            if let Some(session) = sessions.get_mut(name) {
                session.awake = true;
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
            if crate::claude_process::create_client(state, name, None).await.is_ok() {
                let mut sessions = state.sessions.lock().await;
                if let Some(session) = sessions.get_mut(name) {
                    session.awake = true;
                    session.last_failed_resume_id = resume_id.clone();
                    session.session_id = None;
                }
                warn!(
                    "Agent '{}' woke with fresh session (previous context lost)",
                    name
                );
            } else {
                state.scheduler().await.release_slot(name).await;
                return Err(HubError::Other(format!(
                    "Failed to create client for agent '{name}'"
                )));
            }
        }
        Err(_) => {
            state.scheduler().await.release_slot(name).await;
            return Err(HubError::Other(format!(
                "Failed to create client for agent '{name}'"
            )));
        }
    }

    crate::frontend::on_wake(state, name).await;
    Ok(())
}

// ---------------------------------------------------------------------------
// Wake-or-queue
// ---------------------------------------------------------------------------

pub async fn wake_or_queue(
    state: &BotState,
    name: &str,
    content: crate::types::MessageContent,
    metadata: Option<serde_json::Value>,
) -> bool {
    match wake_agent(state, name).await {
        Ok(()) => {
            // Agent is awake. Check if it's currently processing (query_lock held).
            // If busy, queue the message — the running task's process_message_queue
            // will pick it up. Spawning a competing task causes race conditions
            // where the second task acquires the lock after the agent has slept.
            let busy = {
                let sessions = state.sessions.lock().await;
                sessions.get(name).is_some_and(is_processing)
            };
            if busy {
                let mut sessions = state.sessions.lock().await;
                if let Some(session) = sessions.get_mut(name) {
                    session
                        .message_queue
                        .push_back(QueuedMessage { content, metadata });
                    let position = session.message_queue.len();
                    debug!(
                        "Agent '{}' is busy, queuing message (position {})",
                        name, position
                    );
                }
                false
            } else {
                true
            }
        }
        Err(HubError::ConcurrencyLimit(_)) => {
            let mut sessions = state.sessions.lock().await;
            if let Some(session) = sessions.get_mut(name) {
                session
                    .message_queue
                    .push_back(QueuedMessage { content, metadata });
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
