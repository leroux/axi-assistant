//! Message processing — query dispatch, retry, timeout, interrupt.
//!
//! `StreamHandlerFn`: async (`agent_name`) -> Option<String>
//!   Returns None on success, or an error string for transient errors.

use std::future::Future;
use std::pin::Pin;
use std::sync::Arc;

use chrono::Utc;
use tracing::{debug, error, info, warn};

use crate::hub::AgentHub;
use crate::lifecycle;
use crate::registry;
use crate::types::{HubError, MessageContent};
use claudewire::events::ActivityState;

/// Callback: consume the SDK stream, render to user, return error or None.
pub type StreamHandlerFn = Arc<
    dyn Fn(&str) -> Pin<Box<dyn Future<Output = Option<String>> + Send>>
        + Send
        + Sync,
>;

// ---------------------------------------------------------------------------
// Interrupt
// ---------------------------------------------------------------------------

pub async fn interrupt_session(hub: &AgentHub, name: &str) {
    if let Some(ref conn) = *hub.process_conn.lock().await {
        match conn.kill(name).await {
            Ok(result) => {
                if !result.ok {
                    warn!(
                        "Bridge kill for '{}' failed: {:?}",
                        name, result.error
                    );
                }
            }
            Err(e) => {
                warn!("Bridge kill for '{}' raised: {}", name, e);
            }
        }
    }
}

// ---------------------------------------------------------------------------
// Core: process one conversation turn
// ---------------------------------------------------------------------------

pub async fn process_message(
    hub: &AgentHub,
    name: &str,
    content: &MessageContent,
    stream_handler: &StreamHandlerFn,
) -> Result<(), HubError> {
    {
        let sessions = hub.sessions.lock().await;
        let session = sessions
            .get(name)
            .ok_or_else(|| HubError::NotFound(name.to_string()))?;
        if session.client.is_none() {
            return Err(HubError::NotAwake(name.to_string()));
        }
    }

    {
        let mut sessions = hub.sessions.lock().await;
        if let Some(session) = sessions.get_mut(name) {
            lifecycle::reset_activity(session);
            session.bridge_busy = false;
        }
    }

    // Send query via client
    (hub.send_query)(name, content).await;

    // Stream response with retry
    let success = stream_with_retry(hub, name, stream_handler).await;
    if !success {
        return Err(HubError::QueryFailed(name.to_string()));
    }

    Ok(())
}

// ---------------------------------------------------------------------------
// Retry
// ---------------------------------------------------------------------------

async fn stream_with_retry(
    hub: &AgentHub,
    name: &str,
    stream_handler: &StreamHandlerFn,
) -> bool {
    let error = stream_handler(name).await;
    if error.is_none() {
        return true;
    }

    let error_text = error.unwrap();
    warn!(
        "Transient error for '{}': {} — will retry",
        name, error_text
    );

    for attempt in 2..=hub.max_retries {
        let delay = hub.retry_base_delay * 2f64.powi((attempt - 2).cast_signed());
        warn!(
            "Agent '{}' retrying in {:.0}s (attempt {}/{})",
            name, delay, attempt, hub.max_retries
        );
        hub.callbacks
            .post_system(
                name,
                &format!(
                    "API error, retrying in {:.0}s... (attempt {}/{})",
                    delay, attempt, hub.max_retries
                ),
            )
            .await;

        tokio::time::sleep(std::time::Duration::from_secs_f64(delay)).await;

        // Retry query
        let retry_content = MessageContent::Text("Continue from where you left off.".to_string());
        (hub.send_query)(name, &retry_content).await;

        let error = stream_handler(name).await;
        if error.is_none() {
            return true;
        }
    }

    error!(
        "Agent '{}' transient error persisted after {} retries",
        name, hub.max_retries
    );
    hub.callbacks
        .post_system(
            name,
            &format!(
                "API error persisted after {} retries. Try again later.",
                hub.max_retries
            ),
        )
        .await;
    false
}

// ---------------------------------------------------------------------------
// Timeout handling
// ---------------------------------------------------------------------------

pub async fn handle_query_timeout(hub: &AgentHub, name: &str) {
    warn!("Query timeout for agent '{}', killing session", name);

    interrupt_session(hub, name).await;

    let old_session_id = {
        let sessions = hub.sessions.lock().await;
        sessions
            .get(name)
            .and_then(|s| s.session_id.clone())
    };

    registry::rebuild_session(hub, name, None, old_session_id.clone(), None, None).await;

    let msg = if old_session_id.is_some() {
        format!(
            "Agent **{name}** timed out and was recovered (sleeping). Context preserved."
        )
    } else {
        format!(
            "Agent **{name}** timed out and was reset (sleeping). Context lost."
        )
    };
    hub.callbacks.post_system(name, &msg).await;
}

// ---------------------------------------------------------------------------
// Initial prompt
// ---------------------------------------------------------------------------

pub async fn run_initial_prompt(
    hub: &AgentHub,
    name: &str,
    prompt: MessageContent,
    stream_handler: &StreamHandlerFn,
) {
    // Acquire query_lock
    let query_lock = {
        let sessions = hub.sessions.lock().await;
        sessions.get(name).map(|s| s.query_lock.clone())
    };

    let Some(query_lock) = query_lock else {
        return;
    };

    {
        let _lock = query_lock.lock().await;

        // Wake if needed
        let awake = {
            let sessions = hub.sessions.lock().await;
            sessions.get(name).is_some_and(super::types::AgentSession::is_awake)
        };

        if !awake {
            if let Err(e) = lifecycle::wake_agent(hub, name).await {
                warn!("Failed to wake agent '{}' for initial prompt: {}", name, e);
                hub.callbacks
                    .post_system(name, &format!("Failed to wake agent **{name}**."))
                    .await;
                return;
            }
        }

        {
            let mut sessions = hub.sessions.lock().await;
            if let Some(session) = sessions.get_mut(name) {
                session.last_activity = Utc::now();
                session.activity = ActivityState {
                    phase: claudewire::events::Phase::Starting,
                    query_started: Some(Utc::now()),
                    ..Default::default()
                };
            }
        }

        match tokio::time::timeout(
            std::time::Duration::from_secs_f64(hub.query_timeout),
            process_message(hub, name, &prompt, stream_handler),
        )
        .await
        {
            Ok(Ok(())) => {
                let mut sessions = hub.sessions.lock().await;
                if let Some(session) = sessions.get_mut(name) {
                    session.last_activity = Utc::now();
                }
            }
            Ok(Err(e)) => {
                warn!("Handler error for '{}' initial prompt: {}", name, e);
                hub.callbacks
                    .post_system(name, &format!("Error: {e}"))
                    .await;
            }
            Err(_) => {
                handle_query_timeout(hub, name).await;
            }
        }

        {
            let mut sessions = hub.sessions.lock().await;
            if let Some(session) = sessions.get_mut(name) {
                session.activity = ActivityState::default();
            }
        }
    }

    debug!("Initial prompt completed for '{}'", name);
    hub.callbacks
        .post_system(name, &format!("Agent **{name}** finished initial task."))
        .await;

    // Process queue or sleep
    let should_yield = hub.scheduler.should_yield(name).await;
    if !should_yield {
        process_message_queue(hub, name, stream_handler).await;
    }

    lifecycle::sleep_agent(hub, name, false).await;
}

// ---------------------------------------------------------------------------
// Message queue
// ---------------------------------------------------------------------------

pub async fn process_message_queue(
    hub: &AgentHub,
    name: &str,
    stream_handler: &StreamHandlerFn,
) {
    loop {
        let message = {
            let mut sessions = hub.sessions.lock().await;
            sessions
                .get_mut(name)
                .and_then(|s| s.message_queue.pop_front())
        };

        let Some(message) = message else {
            break;
        };

        if hub.shutdown_requested.load(std::sync::atomic::Ordering::SeqCst) {
            info!(
                "Shutdown requested — not processing further queued messages for '{}'",
                name
            );
            break;
        }

        if hub.scheduler.should_yield(name).await {
            let remaining = {
                let sessions = hub.sessions.lock().await;
                sessions
                    .get(name)
                    .map_or(0, |s| s.message_queue.len())
            };
            info!(
                "Scheduler yield: '{}' deferring {} queued messages",
                name, remaining
            );
            lifecycle::sleep_agent(hub, name, false).await;
            return;
        }

        let preview = message.content.preview(200);
        let remaining = {
            let sessions = hub.sessions.lock().await;
            sessions
                .get(name)
                .map_or(0, |s| s.message_queue.len())
        };
        let remaining_str = if remaining > 0 {
            format!(" ({remaining} more in queue)")
        } else {
            String::new()
        };

        hub.callbacks
            .post_system(
                name,
                &format!("Processing queued message{remaining_str}:\n> {preview}"),
            )
            .await;

        let query_lock = {
            let sessions = hub.sessions.lock().await;
            sessions.get(name).map(|s| s.query_lock.clone())
        };

        if let Some(query_lock) = query_lock {
            let _lock = query_lock.lock().await;

            // Wake if needed
            let awake = {
                let sessions = hub.sessions.lock().await;
                sessions.get(name).is_some_and(super::types::AgentSession::is_awake)
            };

            if !awake {
                if let Err(e) = lifecycle::wake_agent(hub, name).await {
                    warn!(
                        "Failed to wake agent '{}' for queued message: {}",
                        name, e
                    );
                    hub.callbacks
                        .post_system(
                            name,
                            &format!(
                                "Failed to wake agent **{name}** — dropping queued messages."
                            ),
                        )
                        .await;
                    // Clear remaining queue
                    let mut sessions = hub.sessions.lock().await;
                    if let Some(session) = sessions.get_mut(name) {
                        session.message_queue.clear();
                    }
                    return;
                }
            }

            {
                let mut sessions = hub.sessions.lock().await;
                if let Some(session) = sessions.get_mut(name) {
                    lifecycle::reset_activity(session);
                }
            }

            match process_message(hub, name, &message.content, stream_handler).await {
                Ok(()) => {}
                Err(e) => {
                    warn!(
                        "Error processing queued message for '{}': {}",
                        name, e
                    );
                    hub.callbacks.post_system(name, &e.to_string()).await;
                }
            }

            {
                let mut sessions = hub.sessions.lock().await;
                if let Some(session) = sessions.get_mut(name) {
                    session.activity = ActivityState::default();
                }
            }
        }
    }
}

// ---------------------------------------------------------------------------
// Inter-agent messaging
// ---------------------------------------------------------------------------

pub async fn deliver_inter_agent_message(
    hub: &AgentHub,
    sender_name: &str,
    target_name: &str,
    content: &str,
    stream_handler: &StreamHandlerFn,
) -> String {
    hub.callbacks
        .post_system(
            target_name,
            &format!("Message from {sender_name}:\n> {content}"),
        )
        .await;

    let ts_prefix = Utc::now().format("[%Y-%m-%d %H:%M:%S UTC] ").to_string();
    let prompt = MessageContent::Text(format!(
        "{ts_prefix}[Inter-agent message from {sender_name}] {content}"
    ));

    let is_busy = {
        let sessions = hub.sessions.lock().await;
        sessions
            .get(target_name)
            .is_some_and(lifecycle::is_processing)
    };

    if is_busy {
        // Queue and interrupt
        {
            let mut sessions = hub.sessions.lock().await;
            if let Some(session) = sessions.get_mut(target_name) {
                session.message_queue.push_front(crate::types::QueuedMessage {
                    content: prompt,
                    metadata: None,
                });
            }
        }
        info!(
            "Inter-agent message from '{}' to busy agent '{}' — interrupting",
            sender_name, target_name
        );
        interrupt_session(hub, target_name).await;
        format!(
            "delivered to busy agent '{target_name}' (interrupted, will process next)"
        )
    } else {
        // Fire and forget — wake and process
        let hub_ref = hub.clone_ref();
        let target = target_name.to_string();
        let handler = stream_handler.clone();
        tokio::spawn(async move {
            run_initial_prompt(&hub_ref, &target, prompt, &handler).await;
        });
        format!("delivered to agent '{target_name}'")
    }
}
