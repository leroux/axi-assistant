//! Process bridge — connects agent sessions to Claude CLI via procmux.
//!
//! Creates BridgeTransport instances for each agent, wiring procmux process
//! I/O to the claudewire stream protocol. Provides factory functions for
//! the AgentHub's create_client, disconnect_client, and send_query callbacks,
//! plus the stream handler that reads events and renders to Discord.

use std::collections::HashMap;
use std::sync::Arc;

use tokio::sync::Mutex;
use tracing::{debug, info, warn};

use axi_hub::procmux_wire::translate_process_msg;
use axi_hub::types::MessageContent;
use claudewire::events;
use claudewire::transport::BridgeTransport;

use crate::state::BotState;
use crate::streaming;

// ---------------------------------------------------------------------------
// Transport registry
// ---------------------------------------------------------------------------

/// Shared per-agent BridgeTransport storage.
pub type TransportMap = Mutex<HashMap<String, Arc<Mutex<BridgeTransport>>>>;

/// Create a new empty transport map.
pub fn new_transport_map() -> TransportMap {
    Mutex::new(HashMap::new())
}

// ---------------------------------------------------------------------------
// Client factory: create
// ---------------------------------------------------------------------------

/// Create a Claude CLI session for an agent via procmux.
///
/// Spawns a process, sets up event translation, creates a BridgeTransport,
/// and stores it in the transport map.
pub async fn create_client(
    state: &BotState,
    name: &str,
    resume_session_id: Option<&str>,
) -> anyhow::Result<()> {
    let hub = state.hub().await;
    let conn = {
        let conn_lock = hub.process_conn.lock().await;
        conn_lock
            .as_ref()
            .ok_or_else(|| anyhow::anyhow!("No bridge connection"))?
            .clone()
    };

    let reconnecting = resume_session_id.is_some();

    // Get session config
    let (cwd, system_prompt, mcp_servers) = {
        let sessions = hub.sessions.lock().await;
        let session = sessions
            .get(name)
            .ok_or_else(|| anyhow::anyhow!("Session not found: {}", name))?;
        (
            session.cwd.clone(),
            session.system_prompt.clone(),
            session.mcp_servers.clone(),
        )
    };

    // Register process queue in procmux client
    let procmux_rx = conn.register_process(name).await;

    // Build CLI args
    let cli_args = build_cli_args(resume_session_id, &state.config.config_path);

    // Build environment
    let env = build_env(system_prompt.as_ref(), mcp_servers.as_ref());

    // Spawn process via procmux
    let spawn_cwd = if cwd.is_empty() { None } else { Some(cwd) };

    let result = conn.spawn(name, cli_args, env, spawn_cwd).await?;
    if !result.ok {
        conn.unregister_process(name).await;
        anyhow::bail!(
            "Failed to spawn process for '{}': {:?}",
            name,
            result.error
        );
    }

    // Subscribe to process events
    let sub_result = conn.subscribe(name).await?;
    if let Some(replayed) = sub_result.replayed {
        debug!("Replayed {} buffered events for '{}'", replayed, name);
    }

    // Create mpsc channels for claudewire ProcessEvent
    let (event_tx, event_rx) = tokio::sync::mpsc::unbounded_channel();
    let event_tx_clone = event_tx.clone();

    // Spawn translator task: procmux ProcessMsg → claudewire ProcessEvent
    let agent_name = name.to_string();
    tokio::spawn(async move {
        let mut procmux_rx = procmux_rx;
        while let Some(msg) = procmux_rx.recv().await {
            if let Some(event) = translate_process_msg(msg) {
                if event_tx_clone.send(event).is_err() {
                    debug!("Event channel closed for '{}'", agent_name);
                    break;
                }
            }
        }
    });

    // Build BridgeTransport closures
    let conn_for_stdin = conn.clone();
    let stdin_name = name.to_string();
    let send_stdin: claudewire::transport::SendStdinFn = Box::new(move |_name, data| {
        let conn = conn_for_stdin.clone();
        let name = stdin_name.clone();
        Box::pin(async move { conn.send_stdin(&name, data).await })
    });

    let conn_for_kill = conn.clone();
    let kill_name = name.to_string();
    let kill: claudewire::transport::KillFn = Box::new(move |_name| {
        let conn = conn_for_kill.clone();
        let name = kill_name.clone();
        Box::pin(async move { conn.kill(&name).await.map(|_| ()) })
    });

    let conn_for_alive = conn.clone();
    let is_alive: Box<dyn Fn() -> bool + Send + Sync> =
        Box::new(move || conn_for_alive.is_alive());

    let transport = BridgeTransport::new(
        name.to_string(),
        event_rx,
        event_tx,
        send_stdin,
        kill,
        is_alive,
        reconnecting,
        None,
    );

    // Store transport
    let transport = Arc::new(Mutex::new(transport));
    {
        let mut transports = state.transports.lock().await;
        transports.insert(name.to_string(), transport);
    }

    info!(
        "Bridge transport created for agent '{}' (reconnecting={})",
        name, reconnecting
    );
    Ok(())
}

// ---------------------------------------------------------------------------
// Client factory: disconnect
// ---------------------------------------------------------------------------

/// Disconnect a Claude CLI session — close the transport and kill the process.
pub async fn disconnect_client(state: &BotState, name: &str) {
    let transport = {
        let mut transports = state.transports.lock().await;
        transports.remove(name)
    };

    if let Some(transport) = transport {
        let mut transport = transport.lock().await;
        transport.close().await;
        info!("Bridge transport closed for agent '{}'", name);
    }

    // Unregister from procmux client
    let hub = state.hub().await;
    let conn = hub.process_conn.lock().await;
    if let Some(conn) = conn.as_ref() {
        conn.unregister_process(name).await;
    }
}

// ---------------------------------------------------------------------------
// Client factory: send query
// ---------------------------------------------------------------------------

/// Send a message to an agent's Claude CLI process.
pub async fn send_query(state: &BotState, name: &str, content: &MessageContent) {
    let transport = {
        let transports = state.transports.lock().await;
        transports.get(name).cloned()
    };

    let Some(transport) = transport else {
        warn!("No transport for agent '{}', cannot send query", name);
        return;
    };

    let json_content = match content {
        MessageContent::Text(text) => serde_json::Value::String(text.clone()),
        MessageContent::Blocks(blocks) => serde_json::Value::Array(blocks.clone()),
    };

    let msg = events::make_user_message(json_content);
    let msg_str = serde_json::to_string(&msg).unwrap_or_default();

    let mut transport = transport.lock().await;
    if let Err(e) = transport.write(&msg_str).await {
        warn!("Failed to send query to '{}': {}", name, e);
    }
}

// ---------------------------------------------------------------------------
// Stream handler
// ---------------------------------------------------------------------------

/// Create a StreamHandlerFn that reads claudewire events and renders to Discord.
pub fn make_stream_handler(
    state: Arc<BotState>,
) -> axi_hub::messaging::StreamHandlerFn {
    Arc::new(move |agent_name: &str| {
        let state = state.clone();
        let name = agent_name.to_string();
        Box::pin(async move { stream_response(&state, &name).await })
    })
}

/// Read stream events from a BridgeTransport and render to Discord.
///
/// Returns None on success, Some(error) on transient error (triggers retry).
async fn stream_response(state: &BotState, agent_name: &str) -> Option<String> {
    let transport = {
        let transports = state.transports.lock().await;
        transports.get(agent_name).cloned()
    };

    let transport = match transport {
        Some(t) => t,
        None => {
            warn!("No transport for agent '{}', cannot stream", agent_name);
            return Some("No transport available".to_string());
        }
    };

    let channel_id = state.channel_for_agent(agent_name).await;
    let streaming_enabled = state.config.streaming_discord && channel_id.is_some();
    let mut ctx = streaming::StreamContext::new(channel_id.map(|c| c.get()), streaming_enabled);

    let mut current_block_type: Option<String> = None;
    let mut got_result = false;

    loop {
        let event = {
            let mut transport = transport.lock().await;
            transport.read_message().await
        };

        let event = match event {
            Some(e) => e,
            None => {
                if !got_result {
                    warn!(
                        "Stream ended unexpectedly for agent '{}'",
                        agent_name
                    );
                }
                break;
            }
        };

        let event_type = event
            .get("type")
            .and_then(|v| v.as_str())
            .unwrap_or("")
            .to_string();

        // Update activity state in hub
        {
            let hub = state.hub().await;
            let mut sessions = hub.sessions.lock().await;
            if let Some(session) = sessions.get_mut(agent_name) {
                events::update_activity(&mut session.activity, &event);
            }
        }

        match event_type.as_str() {
            "content_block_start" => {
                let block_type = event
                    .get("content_block")
                    .and_then(|b| b.get("type"))
                    .and_then(|v| v.as_str())
                    .unwrap_or("");

                current_block_type = Some(block_type.to_string());

                if block_type == "tool_use" {
                    // Finalize any pending text before tool use
                    if !ctx.text_buffer.is_empty() {
                        streaming::live_edit_finalize(
                            &mut ctx,
                            &state.discord_client,
                            agent_name,
                        )
                        .await;
                    }

                    let tool_name = event
                        .get("content_block")
                        .and_then(|b| b.get("name"))
                        .and_then(|v| v.as_str())
                        .unwrap_or("tool");
                    debug!("Agent '{}' using tool: {}", agent_name, tool_name);
                }
            }

            "content_block_delta" => {
                let delta = event.get("delta").unwrap_or(&serde_json::Value::Null);
                let delta_type = delta
                    .get("type")
                    .and_then(|v| v.as_str())
                    .unwrap_or("");

                if delta_type == "text_delta" {
                    if let Some(text) = delta.get("text").and_then(|v| v.as_str()) {
                        ctx.text_buffer.push_str(text);
                        streaming::live_edit_tick(
                            &mut ctx,
                            &state.discord_client,
                            agent_name,
                        )
                        .await;
                    }
                }
                // input_json_delta, thinking_delta, signature_delta — not rendered
            }

            "content_block_stop" => {
                if current_block_type.as_deref() == Some("text") {
                    streaming::live_edit_finalize(
                        &mut ctx,
                        &state.discord_client,
                        agent_name,
                    )
                    .await;
                }
                current_block_type = None;
            }

            "result" => {
                got_result = true;

                // Capture session_id
                if let Some(session_id) =
                    event.get("session_id").and_then(|v| v.as_str())
                {
                    let hub = state.hub().await;
                    let mut sessions = hub.sessions.lock().await;
                    if let Some(session) = sessions.get_mut(agent_name) {
                        session.session_id = Some(session_id.to_string());
                    }

                    // Persist master session ID
                    if agent_name == state.config.master_agent_name {
                        std::fs::write(
                            &state.config.master_session_path,
                            session_id,
                        )
                        .ok();
                    }
                }

                // Record usage
                record_usage(state, agent_name, &event).await;

                // Check for error result
                if event
                    .get("is_error")
                    .and_then(|v| v.as_bool())
                    .unwrap_or(false)
                {
                    let error_msg = event
                        .get("result")
                        .and_then(|v| v.as_str())
                        .unwrap_or("unknown error");

                    if is_transient_error(error_msg) {
                        return Some(error_msg.to_string());
                    }
                }

                break;
            }

            "rate_limit_event" => {
                if let Some(parsed) = events::parse_rate_limit_event(&event) {
                    debug!(
                        "Rate limit for '{}': type={} status={} util={:?}",
                        agent_name,
                        parsed.rate_limit_type,
                        parsed.status,
                        parsed.utilization
                    );

                    // Update rate limit quota tracking
                    let hub = state.hub().await;
                    let mut tracker = hub.rate_limits.lock().await;
                    tracker.rate_limit_quotas.insert(
                        parsed.rate_limit_type.clone(),
                        axi_hub::types::RateLimitQuota {
                            status: parsed.status.clone(),
                            resets_at: parsed.resets_at,
                            rate_limit_type: parsed.rate_limit_type,
                            utilization: parsed.utilization,
                            updated_at: chrono::Utc::now(),
                        },
                    );

                    if parsed.status == "blocked" {
                        tracker.rate_limited_until = Some(parsed.resets_at);
                        ctx.hit_rate_limit = true;
                    }
                }
            }

            "error" => {
                let error_msg = event
                    .get("error")
                    .and_then(|v| v.get("message"))
                    .and_then(|v| v.as_str())
                    .unwrap_or("unknown error");
                warn!("Stream error for '{}': {}", agent_name, error_msg);

                if is_transient_error(error_msg) {
                    return Some(error_msg.to_string());
                }
            }

            _ => {}
        }
    }

    // Finalize any remaining text
    if !ctx.text_buffer.is_empty() {
        streaming::live_edit_finalize(&mut ctx, &state.discord_client, agent_name).await;
    }

    None
}

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

/// Build CLI args for spawning the Claude CLI process.
fn build_cli_args(
    resume_session_id: Option<&str>,
    config_path: &std::path::Path,
) -> Vec<String> {
    let mut args = vec![
        "claude".to_string(),
        "--output-format".to_string(),
        "stream-json".to_string(),
        "--verbose".to_string(),
    ];

    // Set model
    let model = axi_config::get_model(config_path);
    args.extend(["--model".to_string(), model]);

    // Resume session if available
    if let Some(session_id) = resume_session_id {
        args.extend(["--resume".to_string(), session_id.to_string()]);
    }

    args
}

/// Build environment variables for the CLI process.
fn build_env(
    _system_prompt: Option<&serde_json::Value>,
    _mcp_servers: Option<&serde_json::Value>,
) -> std::collections::HashMap<String, String> {
    let mut env = std::collections::HashMap::new();

    // Pass through relevant env vars
    if let Ok(val) = std::env::var("ANTHROPIC_API_KEY") {
        env.insert("ANTHROPIC_API_KEY".to_string(), val);
    }
    if let Ok(val) = std::env::var("HOME") {
        env.insert("HOME".to_string(), val);
    }
    if let Ok(val) = std::env::var("PATH") {
        env.insert("PATH".to_string(), val);
    }

    env
}

/// Record usage statistics from a result event.
async fn record_usage(state: &BotState, agent_name: &str, result: &serde_json::Value) {
    let session_id = result
        .get("session_id")
        .and_then(|v| v.as_str())
        .unwrap_or("");
    let cost_usd = result.get("cost_usd").and_then(|v| v.as_f64()).unwrap_or(0.0);
    let num_turns = result.get("num_turns").and_then(|v| v.as_u64()).unwrap_or(0);
    let duration_ms = result
        .get("duration_ms")
        .and_then(|v| v.as_u64())
        .unwrap_or(0);
    let input_tokens = result
        .get("usage")
        .and_then(|u| u.get("input_tokens"))
        .and_then(|v| v.as_u64())
        .unwrap_or(0);
    let output_tokens = result
        .get("usage")
        .and_then(|u| u.get("output_tokens"))
        .and_then(|v| v.as_u64())
        .unwrap_or(0);

    if !session_id.is_empty() {
        let hub = state.hub().await;
        let mut tracker = hub.rate_limits.lock().await;
        axi_hub::rate_limits::record_session_usage(
            &mut tracker,
            agent_name,
            session_id,
            cost_usd,
            num_turns,
            duration_ms,
            input_tokens,
            output_tokens,
        );
    }
}

/// Check if an error message indicates a transient (retryable) error.
fn is_transient_error(msg: &str) -> bool {
    let lower = msg.to_lowercase();
    lower.contains("overloaded")
        || lower.contains("rate_limit")
        || lower.contains("rate limit")
        || lower.contains("529")
        || lower.contains("503")
}

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn cli_args_fresh() {
        let path = std::path::Path::new("/tmp/nonexistent-config.json");
        let args = build_cli_args(None, path);
        assert!(args.contains(&"claude".to_string()));
        assert!(args.contains(&"--output-format".to_string()));
        assert!(args.contains(&"stream-json".to_string()));
        assert!(!args.contains(&"--resume".to_string()));
    }

    #[test]
    fn cli_args_resume() {
        let path = std::path::Path::new("/tmp/nonexistent-config.json");
        let args = build_cli_args(Some("session-123"), path);
        assert!(args.contains(&"--resume".to_string()));
        assert!(args.contains(&"session-123".to_string()));
    }

    #[test]
    fn transient_error_detection() {
        assert!(is_transient_error("Server overloaded, try again"));
        assert!(is_transient_error("rate_limit exceeded"));
        assert!(is_transient_error("HTTP 529 error"));
        assert!(!is_transient_error("Invalid API key"));
        assert!(!is_transient_error("Permission denied"));
    }
}
