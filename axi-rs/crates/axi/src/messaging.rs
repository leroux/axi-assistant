//! Message processing — query dispatch, retry, timeout, interrupt.
//!
//! `StreamHandlerFn`: async (`agent_name`) -> Option<String>
//!   Returns None on success, or an error string for transient errors.

use std::future::Future;
use std::pin::Pin;
use std::sync::Arc;

use chrono::Utc;
use tracing::{debug, error, info, warn};

use crate::lifecycle;
use crate::registry;
use crate::state::BotState;
use crate::types::{HubError, MessageContent};
use crate::activity::ActivityState;

/// Callback: consume the SDK stream, render to user, return error or None.
pub type StreamHandlerFn = Arc<
    dyn Fn(&str) -> Pin<Box<dyn Future<Output = Option<String>> + Send>>
        + Send
        + Sync,
>;

// ---------------------------------------------------------------------------
// Interrupt
// ---------------------------------------------------------------------------

/// Gracefully interrupt an agent's current generation.
pub async fn interrupt_session(state: &BotState, name: &str) {
    if let Some(ref conn) = *state.process_conn.lock().await {
        match conn.interrupt(name).await {
            Ok(result) => {
                if !result.ok {
                    warn!(
                        "Graceful interrupt for '{}' failed: {:?}, falling back to kill",
                        name, result.error
                    );
                    if let Err(e) = conn.kill(name).await {
                        warn!("Fallback kill for '{}' also failed: {}", name, e);
                    }
                }
            }
            Err(e) => {
                warn!(
                    "Graceful interrupt for '{}' raised: {}, falling back to kill",
                    name, e
                );
                let _ = conn.kill(name).await;
            }
        }
    }
}

// ---------------------------------------------------------------------------
// Core: process one conversation turn
// ---------------------------------------------------------------------------

pub async fn process_message(
    state: &BotState,
    name: &str,
    content: &MessageContent,
    stream_handler: &StreamHandlerFn,
) -> Result<(), HubError> {
    {
        let sessions = state.sessions.lock().await;
        let session = sessions
            .get(name)
            .ok_or_else(|| HubError::NotFound(name.to_string()))?;
        if !session.awake {
            return Err(HubError::NotAwake(name.to_string()));
        }
    }

    {
        let mut sessions = state.sessions.lock().await;
        if let Some(session) = sessions.get_mut(name) {
            lifecycle::reset_activity(session);
            session.bridge_busy = false;
        }
    }

    crate::claude_process::send_query(state, name, content).await;

    let success = stream_with_retry(state, name, stream_handler).await;
    if !success {
        return Err(HubError::QueryFailed(name.to_string()));
    }

    Ok(())
}

// ---------------------------------------------------------------------------
// Retry
// ---------------------------------------------------------------------------

async fn stream_with_retry(
    state: &BotState,
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

    for attempt in 2..=state.max_retries {
        let delay = state.retry_base_delay * 2f64.powi((attempt - 2).cast_signed());
        warn!(
            "Agent '{}' retrying in {:.0}s (attempt {}/{})",
            name, delay, attempt, state.max_retries
        );
        crate::frontend::post_system(
            state,
            name,
            &format!(
                "API error, retrying in {:.0}s... (attempt {}/{})",
                delay, attempt, state.max_retries
            ),
        )
        .await;

        tokio::time::sleep(std::time::Duration::from_secs_f64(delay)).await;

        let retry_content = MessageContent::Text("Continue from where you left off.".to_string());
        crate::claude_process::send_query(state, name, &retry_content).await;

        let error = stream_handler(name).await;
        if error.is_none() {
            return true;
        }
    }

    error!(
        "Agent '{}' transient error persisted after {} retries",
        name, state.max_retries
    );
    crate::frontend::post_system(
        state,
        name,
        &format!(
            "API error persisted after {} retries. Try again later.",
            state.max_retries
        ),
    )
    .await;
    false
}

// ---------------------------------------------------------------------------
// Timeout handling
// ---------------------------------------------------------------------------

pub async fn handle_query_timeout(state: &BotState, name: &str) {
    warn!("Query timeout for agent '{}', killing session", name);

    interrupt_session(state, name).await;

    let old_session_id = {
        let sessions = state.sessions.lock().await;
        sessions.get(name).and_then(|s| s.session_id.clone())
    };

    registry::rebuild_session(state, name, None, old_session_id.clone(), None, None).await;

    let msg = if old_session_id.is_some() {
        format!("Agent **{name}** timed out and was recovered (sleeping). Context preserved.")
    } else {
        format!("Agent **{name}** timed out and was reset (sleeping). Context lost.")
    };
    crate::frontend::post_system(state, name, &msg).await;
}

// ---------------------------------------------------------------------------
// Initial prompt
// ---------------------------------------------------------------------------

pub async fn run_initial_prompt(
    state: &BotState,
    name: &str,
    prompt: MessageContent,
    stream_handler: &StreamHandlerFn,
) {
    let query_lock = {
        let sessions = state.sessions.lock().await;
        sessions.get(name).map(|s| s.query_lock.clone())
    };

    let Some(query_lock) = query_lock else {
        return;
    };

    {
        let _lock = query_lock.lock().await;

        let awake = {
            let sessions = state.sessions.lock().await;
            sessions
                .get(name)
                .is_some_and(super::types::AgentSession::is_awake)
        };

        if !awake {
            if let Err(e) = lifecycle::wake_agent(state, name).await {
                warn!("Failed to wake agent '{}' for initial prompt: {}", name, e);
                crate::frontend::post_system(
                    state,
                    name,
                    &format!("Failed to wake agent **{name}**."),
                )
                .await;
                return;
            }
        }

        {
            let mut sessions = state.sessions.lock().await;
            if let Some(session) = sessions.get_mut(name) {
                session.last_activity = Utc::now();
                session.activity = ActivityState {
                    phase: crate::activity::Phase::Starting,
                    query_started: Some(Utc::now()),
                    ..Default::default()
                };
            }
        }

        match tokio::time::timeout(
            std::time::Duration::from_secs_f64(state.query_timeout),
            process_message(state, name, &prompt, stream_handler),
        )
        .await
        {
            Ok(Ok(())) => {
                let mut sessions = state.sessions.lock().await;
                if let Some(session) = sessions.get_mut(name) {
                    session.last_activity = Utc::now();
                }
            }
            Ok(Err(e)) => {
                warn!("Handler error for '{}' initial prompt: {}", name, e);
                crate::frontend::post_system(state, name, &format!("Error: {e}"))
                    .await;
            }
            Err(_) => {
                handle_query_timeout(state, name).await;
            }
        }

        {
            let mut sessions = state.sessions.lock().await;
            if let Some(session) = sessions.get_mut(name) {
                session.activity = ActivityState::default();
            }
        }
    }

    debug!("Initial prompt completed for '{}'", name);
    crate::frontend::post_system(
        state,
        name,
        &format!("Agent **{name}** finished initial task."),
    )
    .await;

    // Check for pending flowchart (queued by run_flowchart MCP tool)
    inject_pending_flowchart(state, name).await;

    let should_yield = state.scheduler().await.should_yield(name).await;
    if !should_yield {
        process_message_queue(state, name, stream_handler).await;
    }

    lifecycle::post_awaiting_input(state, name).await;
    lifecycle::sleep_agent(state, name, false).await;
}

// ---------------------------------------------------------------------------
// Message queue
// ---------------------------------------------------------------------------

pub async fn process_message_queue(
    state: &BotState,
    name: &str,
    stream_handler: &StreamHandlerFn,
) {
    loop {
        let message = {
            let mut sessions = state.sessions.lock().await;
            sessions
                .get_mut(name)
                .and_then(|s| s.message_queue.pop_front())
        };

        let Some(message) = message else {
            break;
        };

        if state
            .shutdown_requested
            .load(std::sync::atomic::Ordering::SeqCst)
        {
            info!(
                "Shutdown requested — not processing further queued messages for '{}'",
                name
            );
            break;
        }

        if state.scheduler().await.should_yield(name).await {
            let remaining = {
                let sessions = state.sessions.lock().await;
                sessions
                    .get(name)
                    .map_or(0, |s| s.message_queue.len())
            };
            info!(
                "Scheduler yield: '{}' deferring {} queued messages",
                name, remaining
            );
            lifecycle::sleep_agent(state, name, false).await;
            return;
        }

        let preview = message.content.preview(200);
        let remaining = {
            let sessions = state.sessions.lock().await;
            sessions
                .get(name)
                .map_or(0, |s| s.message_queue.len())
        };
        let remaining_str = if remaining > 0 {
            format!(" ({remaining} more in queue)")
        } else {
            String::new()
        };

        crate::frontend::post_system(
            state,
            name,
            &format!("Processing queued message{remaining_str}:\n> {preview}"),
        )
        .await;

        let query_lock = {
            let sessions = state.sessions.lock().await;
            sessions.get(name).map(|s| s.query_lock.clone())
        };

        if let Some(query_lock) = query_lock {
            let _lock = query_lock.lock().await;

            let awake = {
                let sessions = state.sessions.lock().await;
                sessions
                    .get(name)
                    .is_some_and(super::types::AgentSession::is_awake)
            };

            if !awake {
                if let Err(e) = lifecycle::wake_agent(state, name).await {
                    warn!(
                        "Failed to wake agent '{}' for queued message: {}",
                        name, e
                    );
                    crate::frontend::post_system(
                        state,
                        name,
                        &format!(
                            "Failed to wake agent **{name}** — dropping queued messages."
                        ),
                    )
                    .await;
                    let mut sessions = state.sessions.lock().await;
                    if let Some(session) = sessions.get_mut(name) {
                        session.message_queue.clear();
                    }
                    return;
                }
            }

            {
                let mut sessions = state.sessions.lock().await;
                if let Some(session) = sessions.get_mut(name) {
                    lifecycle::reset_activity(session);
                }
            }

            match process_message(state, name, &message.content, stream_handler).await {
                Ok(()) => {
                    // Check for pending flowchart after each successful turn
                    inject_pending_flowchart(state, name).await;
                }
                Err(e) => {
                    warn!(
                        "Error processing queued message for '{}': {}",
                        name, e
                    );
                    crate::frontend::post_system(state, name, &e.to_string()).await;
                }
            }

            {
                let mut sessions = state.sessions.lock().await;
                if let Some(session) = sessions.get_mut(name) {
                    session.activity = ActivityState::default();
                }
            }
        }
    }
}

/// If the agent has a `pending_flowchart` set (by the `run_flowchart` MCP tool),
/// inject a synthetic `/command args` message at the front of the message queue.
/// The engine intercepts this as a flowchart command.
async fn inject_pending_flowchart(state: &BotState, name: &str) {
    let pending = {
        let mut sessions = state.sessions.lock().await;
        sessions
            .get_mut(name)
            .and_then(|s| s.pending_flowchart.take())
    };

    if let Some((command, args)) = pending {
        debug!(
            "Injecting pending flowchart for '{}': /{} {}",
            name, command, args
        );
        let content = if args.is_empty() {
            format!("/{command}")
        } else {
            format!("/{command} {args}")
        };
        let queued = super::types::QueuedMessage {
            content: MessageContent::Text(content),
            metadata: None,
        };
        let mut sessions = state.sessions.lock().await;
        if let Some(session) = sessions.get_mut(name) {
            session.message_queue.push_front(queued);
        }
    }
}

// ---------------------------------------------------------------------------
// Inter-agent messaging
// ---------------------------------------------------------------------------

pub async fn deliver_inter_agent_message(
    state: Arc<BotState>,
    sender_name: &str,
    target_name: &str,
    content: &str,
    stream_handler: &StreamHandlerFn,
) -> String {
    crate::frontend::post_system(
        &state,
        target_name,
        &format!("Message from {sender_name}:\n> {content}"),
    )
    .await;

    let ts_prefix = Utc::now().format("[%Y-%m-%d %H:%M:%S UTC] ").to_string();
    let prompt = MessageContent::Text(format!(
        "{ts_prefix}[Inter-agent message from {sender_name}] {content}"
    ));

    let is_busy = {
        let sessions = state.sessions.lock().await;
        sessions
            .get(target_name)
            .is_some_and(lifecycle::is_processing)
    };

    if is_busy {
        {
            let mut sessions = state.sessions.lock().await;
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
        interrupt_session(&state, target_name).await;
        format!("delivered to busy agent '{target_name}' (interrupted, will process next)")
    } else {
        let state_ref = state.clone();
        let target = target_name.to_string();
        let handler = stream_handler.clone();
        tokio::spawn(async move {
            run_initial_prompt(&state_ref, &target, prompt, &handler).await;
        });
        format!("delivered to agent '{target_name}'")
    }
}
