//! Claude CLI process management — spawn, stream, query, disconnect.
//!
//! Manages Claude CLI processes for each agent via procmux. Provides
//! `create_client`, `disconnect_client`, `send_query`, and `stream_response`
//! plus the permission and MCP control protocol handlers.

use std::collections::HashMap;
use std::sync::Arc;

use tokio::sync::Mutex;
use tracing::{debug, info, warn};

use crate::activity;
use crate::procmux_wire::translate_process_msg;
use crate::state::BotState;
use crate::streaming;
use crate::types::MessageContent;
use claudewire::config::{Config as CliConfig, McpServers};
use claudewire::session::CliSession;

// ---------------------------------------------------------------------------
// Transport registry
// ---------------------------------------------------------------------------

/// Shared per-agent `CliSession` storage.
pub type TransportMap = Mutex<HashMap<String, Arc<Mutex<CliSession>>>>;

/// Create a new empty transport map.
pub fn new_transport_map() -> TransportMap {
    Mutex::new(HashMap::new())
}

// ---------------------------------------------------------------------------
// Client factory: create
// ---------------------------------------------------------------------------

/// Create a Claude CLI session for an agent via procmux.
///
/// Spawns a process, sets up event translation, creates a `BridgeTransport`,
/// and stores it in the transport map.
pub async fn create_client(
    state: &BotState,
    name: &str,
    resume_session_id: Option<&str>,
) -> anyhow::Result<()> {
    let conn = {
        let conn_lock = state.process_conn.lock().await;
        conn_lock
            .as_ref()
            .ok_or_else(|| anyhow::anyhow!("No bridge connection"))?
            .clone()
    };

    let reconnecting = resume_session_id.is_some();

    // Get session config
    let (cwd, _agent_type, system_prompt, mcp_servers, sdk_mcp_json, plan_mode) = {
        let sessions = state.sessions.lock().await;
        let session = sessions
            .get(name)
            .ok_or_else(|| anyhow::anyhow!("Session not found: {name}"))?;
        // Build SDK MCP JSON from the stored servers
        let sdk_json: serde_json::Value = session.sdk_mcp_servers.iter().map(|(k, s)| {
            (k.clone(), serde_json::json!({"type": "sdk", "name": s.name, "version": s.version}))
        }).collect::<serde_json::Map<String, serde_json::Value>>().into();
        (
            session.cwd.clone(),
            session.agent_type.clone(),
            session.system_prompt.clone(),
            session.mcp_servers.clone(),
            sdk_json,
            session.plan_mode,
        )
    };

    // Register process queue in procmux client
    let procmux_rx = conn.register_process(name).await;

    // Build Claude CLI config — single source of truth for flags and env vars
    let cli_config = build_cli_config(
        &state.config.config_path,
        resume_session_id,
        system_prompt.as_ref(),
        mcp_servers.as_ref(),
        &sdk_mcp_json,
        plan_mode,
    );

    let cli_args = match crate::flowcoder::get_engine_binary() {
        Some(engine) => {
            let search_paths = crate::flowcoder::get_search_paths(&[]);
            info!("Spawning flowcoder engine for '{}': {}", name, engine.display());
            // Engine relays control_request messages (including SDK MCP handshake)
            // so SDK MCP servers are kept in the config.
            crate::flowcoder::build_engine_cli_args(&engine, &search_paths, &cli_config.to_cli_args())
        }
        None => {
            anyhow::bail!(
                "flowcoder-engine binary not found on PATH for agent '{name}'"
            );
        }
    };

    info!("CLI args for '{}': {:?}", name, cli_args);

    // Build environment — SDK control protocol vars + system essentials
    let env = cli_config.to_env();

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

    // Build CliSession closures
    let conn_for_stdin = conn.clone();
    let stdin_name = name.to_string();
    let send_stdin: claudewire::session::SendStdinFn = Box::new(move |_name, data| {
        let conn = conn_for_stdin.clone();
        let name = stdin_name.clone();
        Box::pin(async move { conn.send_stdin(&name, data).await })
    });

    let conn_for_kill = conn.clone();
    let kill_name = name.to_string();
    let kill: claudewire::session::KillFn = Box::new(move |_name| {
        let conn = conn_for_kill.clone();
        let name = kill_name.clone();
        Box::pin(async move { conn.kill(&name).await.map(|_| ()) })
    });

    let conn_for_alive = conn.clone();
    let is_alive: Box<dyn Fn() -> bool + Send + Sync> =
        Box::new(move || conn_for_alive.is_alive());

    let transport = CliSession::new(
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
    let conn = state.process_conn.lock().await;
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

    let msg = activity::make_user_message(&json_content);
    let msg_str = serde_json::to_string(&msg).unwrap_or_default();

    let mut transport = transport.lock().await;
    if let Err(e) = transport.write(&msg_str).await {
        warn!("Failed to send query to '{}': {}", name, e);
    }
}

// ---------------------------------------------------------------------------
// Stream handler
// ---------------------------------------------------------------------------

/// Create a `StreamHandlerFn` that reads claudewire events and renders to Discord.
pub fn make_stream_handler(
    state: Arc<BotState>,
) -> crate::messaging::StreamHandlerFn {
    Arc::new(move |agent_name: &str| {
        let state = state.clone();
        let name = agent_name.to_string();
        Box::pin(async move { stream_response(&state, &name).await })
    })
}

/// Read stream events from a `BridgeTransport` and render to Discord.
///
/// Returns None on success, Some(error) on transient error (triggers retry).
#[allow(unused_assignments)]
#[tracing::instrument(skip(state), fields(agent.name = agent_name))]
async fn stream_response(state: &BotState, agent_name: &str) -> Option<String> {
    let transport = {
        let transports = state.transports.lock().await;
        transports.get(agent_name).cloned()
    };

    let transport = if let Some(t) = transport { t } else {
        warn!("No transport for agent '{}', cannot stream", agent_name);
        return Some("No transport available".to_string());
    };

    let channel_id = state.channel_for_agent(agent_name).await;
    let streaming_enabled = state.config.streaming_discord && channel_id.is_some();
    let mut ctx = streaming::StreamContext::new(channel_id.map(serenity::all::ChannelId::get), streaming_enabled);

    // Set per-session flags
    {
        let sessions = state.sessions.lock().await;
        if let Some(session) = sessions.get(agent_name) {
            ctx.debug = session.debug;
        }
    }
    ctx.clean_tool_messages = state.config.clean_tool_messages;

    let mut current_block_type: Option<String> = None;
    let mut got_result = false;

    loop {
        let event = {
            let mut transport = transport.lock().await;
            transport.read_message().await
        };

        let event = if let Some(e) = event { e } else {
            if !got_result {
                warn!(
                    "Stream ended unexpectedly for agent '{}'",
                    agent_name
                );
            }
            break;
        };

        let top_type = event
            .get("type")
            .and_then(|v| v.as_str())
            .unwrap_or("")
            .to_string();

        // Unwrap stream_event wrappers — the inner event is in .event
        let (event_type, inner) = if top_type == "stream_event" {
            let inner_event = event.get("event").unwrap_or(&event);
            let inner_type = inner_event
                .get("type")
                .and_then(|v| v.as_str())
                .unwrap_or("")
                .to_string();
            (inner_type, inner_event)
        } else {
            (top_type, &event)
        };

        // Update activity state
        {
            let mut sessions = state.sessions.lock().await;
            if let Some(session) = sessions.get_mut(agent_name) {
                activity::update_activity(&mut session.activity, &event);
            }
        }

        match event_type.as_str() {
            "content_block_start" => {
                let block_type = inner
                    .get("content_block")
                    .and_then(|b| b.get("type"))
                    .and_then(|v| v.as_str())
                    .unwrap_or("");

                current_block_type = Some(block_type.to_string());

                if block_type == "thinking" {
                    // Show thinking indicator
                    streaming::show_thinking(
                        &mut ctx,
                        &state.discord_client,
                        agent_name,
                    )
                    .await;
                } else if block_type == "tool_use" {
                    // Hide thinking indicator if shown
                    streaming::hide_thinking(
                        &mut ctx,
                        &state.discord_client,
                        agent_name,
                    )
                    .await;

                    // Finalize any pending text before tool use
                    if !ctx.text_buffer.is_empty() {
                        streaming::live_edit_finalize(
                            &mut ctx,
                            &state.discord_client,
                            agent_name,
                        )
                        .await;
                    }

                    let tool_name = inner
                        .get("content_block")
                        .and_then(|b| b.get("name"))
                        .and_then(|v| v.as_str())
                        .unwrap_or("tool");
                    debug!("Agent '{}' using tool: {}", agent_name, tool_name);
                    ctx.current_tool_name = Some(tool_name.to_string());
                    ctx.tool_input_json.clear();

                    // Debug mode: post persistent tool usage message
                    if ctx.debug {
                        let ch = ctx.live_edit.as_ref().map(|le| le.channel_id)
                            .or(ctx.channel_id);
                        if let Some(ch) = ch {
                            let display = activity::tool_display(tool_name);
                            let _ = state.discord_client.send_message(
                                ch, &format!("\u{1f527} {display}"),
                            ).await;
                        }
                    }

                    // Show tool progress message
                    streaming::show_tool_progress(
                        &mut ctx,
                        &state.discord_client,
                        agent_name,
                        tool_name,
                    )
                    .await;
                } else if block_type == "text" {
                    // Hide thinking indicator when text starts
                    streaming::hide_thinking(
                        &mut ctx,
                        &state.discord_client,
                        agent_name,
                    )
                    .await;

                    // Delete tool progress messages when text output starts
                    streaming::delete_tool_progress(
                        &mut ctx,
                        &state.discord_client,
                        agent_name,
                    )
                    .await;
                }
            }

            "content_block_delta" => {
                let delta = inner.get("delta").unwrap_or(&serde_json::Value::Null);
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
                if delta_type == "input_json_delta" {
                    if let Some(json_part) = delta.get("partial_json").and_then(|v| v.as_str()) {
                        ctx.tool_input_json.push_str(json_part);
                    }
                }
                // thinking_delta, signature_delta — not rendered
            }

            "content_block_stop" => {
                if current_block_type.as_deref() == Some("text") {
                    streaming::live_edit_finalize(
                        &mut ctx,
                        &state.discord_client,
                        agent_name,
                    )
                    .await;
                } else if current_block_type.as_deref() == Some("tool_use") {
                    // Check if it's TodoWrite
                    if ctx.current_tool_name.as_deref() == Some("TodoWrite") {
                        if let Ok(parsed) = serde_json::from_str::<serde_json::Value>(&ctx.tool_input_json) {
                            if let Some(todos) = parsed.get("todos").and_then(|v| v.as_array()) {
                                let channel_id = ctx.live_edit.as_ref().map(|le| le.channel_id);
                                if let Some(ch_id) = channel_id {
                                    let msg = crate::todos::format_todo_message(todos);
                                    if !msg.is_empty() {
                                        let _ = state.discord_client.send_message(ch_id, &msg).await;
                                    }
                                }
                            }
                        }
                    }
                    ctx.current_tool_name = None;
                    ctx.tool_input_json.clear();
                } else if current_block_type.as_deref() == Some("thinking") {
                    // Hide thinking indicator when block completes
                    streaming::hide_thinking(
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
                    let mut sessions = state.sessions.lock().await;
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

                // Track context tokens for auto-compact
                {
                    let context_tokens = event
                        .get("total_input_tokens")
                        .and_then(serde_json::Value::as_u64)
                        .or_else(|| {
                            event
                                .get("usage")
                                .and_then(|u| u.get("input_tokens"))
                                .and_then(serde_json::Value::as_u64)
                        })
                        .unwrap_or(0);
                    let context_window = event
                        .get("context_window")
                        .and_then(serde_json::Value::as_u64)
                        .unwrap_or(0);

                    if context_tokens > 0 || context_window > 0 {
                        let mut sessions = state.sessions.lock().await;
                        if let Some(session) = sessions.get_mut(agent_name) {
                            session.context_tokens = context_tokens;
                            if context_window > 0 {
                                session.context_window = context_window;
                            }
                        }
                    }
                }

                // Record usage
                record_usage(state, agent_name, &event).await;

                // Check for error result
                if event
                    .get("is_error")
                    .and_then(serde_json::Value::as_bool)
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
                if let Some(parsed) = activity::parse_rate_limit_event(&event) {
                    debug!(
                        "Rate limit for '{}': type={} status={} util={:?}",
                        agent_name,
                        parsed.rate_limit_type,
                        parsed.status,
                        parsed.utilization
                    );

                    // Update rate limit quota tracking
                    let mut tracker = state.rate_limits.lock().await;
                    tracker.rate_limit_quotas.insert(
                        parsed.rate_limit_type.clone(),
                        crate::types::RateLimitQuota {
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

            "system" => {
                let subtype = inner
                    .get("subtype")
                    .and_then(|v| v.as_str())
                    .unwrap_or("");
                match subtype {
                    "compacting" => {
                        if let Some(ch_id) = ctx.live_edit.as_ref().map(|le| le.channel_id) {
                            let _ = state
                                .discord_client
                                .send_message(ch_id, "*Compacting context...*")
                                .await;
                        }
                    }
                    "compact_boundary" => {
                        debug!("Compact boundary for agent '{}'", agent_name);
                    }
                    _ => {
                        debug!(
                            "Unhandled system message subtype '{}' for '{}'",
                            subtype, agent_name
                        );
                    }
                }
            }

            "control_request" => {
                let request_id = event
                    .get("request_id")
                    .and_then(|v| v.as_str())
                    .unwrap_or("")
                    .to_string();
                let subtype = event
                    .get("request")
                    .and_then(|r| r.get("subtype"))
                    .and_then(|v| v.as_str())
                    .unwrap_or("");

                if subtype == "permission" {
                    let tool_name = event
                        .get("request")
                        .and_then(|r| r.get("tool"))
                        .and_then(|t| t.get("tool_name"))
                        .and_then(|v| v.as_str())
                        .unwrap_or("");

                    let tool_input = event
                        .get("request")
                        .and_then(|r| r.get("tool"))
                        .and_then(|t| t.get("tool_input"))
                        .cloned()
                        .unwrap_or(serde_json::Value::Null);

                    let response = handle_permission_request(
                        state,
                        agent_name,
                        &request_id,
                        tool_name,
                        &tool_input,
                        &transport,
                    )
                    .await;

                    // Send control response
                    let control_response = serde_json::json!({
                        "type": "control_response",
                        "response": response,
                    });
                    let msg_str = serde_json::to_string(&control_response).unwrap_or_default();
                    let mut transport = transport.lock().await;
                    if let Err(e) = transport.write(&msg_str).await {
                        warn!("Failed to send control response for '{}': {}", agent_name, e);
                    }
                } else if subtype == "mcp_message" {
                    let request_data = event.get("request");
                    let server_name = request_data
                        .and_then(|r| r.get("server_name"))
                        .and_then(|v| v.as_str())
                        .unwrap_or("");
                    let mcp_message = request_data
                        .and_then(|r| r.get("message"))
                        .cloned()
                        .unwrap_or(serde_json::Value::Null);

                    let mcp_response = handle_mcp_message(
                        state,
                        agent_name,
                        server_name,
                        &mcp_message,
                    )
                    .await;

                    let control_response = serde_json::json!({
                        "type": "control_response",
                        "response": {
                            "subtype": "success",
                            "request_id": request_id,
                            "response": {
                                "mcp_response": mcp_response,
                            },
                        },
                    });
                    let msg_str = serde_json::to_string(&control_response).unwrap_or_default();
                    let mut transport = transport.lock().await;
                    if let Err(e) = transport.write(&msg_str).await {
                        warn!("Failed to send MCP response for '{}': {}", agent_name, e);
                    }
                } else {
                    debug!(
                        "Unhandled control_request subtype '{}' for '{}'",
                        subtype, agent_name
                    );
                }
            }

            // ---- Engine-specific events (flowchart execution) ----
            "flowchart_start" => {
                let command = event.get("command").and_then(|v| v.as_str()).unwrap_or("?");
                let block_count = event.get("block_count").and_then(serde_json::Value::as_u64).unwrap_or(0);
                info!(
                    "Flowchart started for '{}': command={}, blocks={}",
                    agent_name, command, block_count
                );
                if let Some(ch_id) = ctx.live_edit.as_ref().map(|le| le.channel_id) {
                    let msg = format!("*Running flowchart `{command}` ({block_count} blocks)...*");
                    let _ = state.discord_client.send_message(ch_id, &msg).await;
                }
            }

            "block_start" => {
                let block_name = event.get("block_name").and_then(|v| v.as_str()).unwrap_or("?");
                let block_type = event.get("block_type").and_then(|v| v.as_str()).unwrap_or("?");
                let block_index = event.get("block_index").and_then(serde_json::Value::as_u64).unwrap_or(0);
                let total = event.get("total_blocks").and_then(serde_json::Value::as_u64).unwrap_or(0);
                debug!(
                    "Block started for '{}': {} ({}) [{}/{}]",
                    agent_name, block_name, block_type, block_index + 1, total
                );
            }

            "block_complete" => {
                let block_name = event.get("block_name").and_then(|v| v.as_str()).unwrap_or("?");
                let success = event.get("success").and_then(serde_json::Value::as_bool).unwrap_or(false);
                let duration_ms = event.get("duration_ms").and_then(serde_json::Value::as_u64).unwrap_or(0);
                debug!(
                    "Block complete for '{}': {} success={} ({}ms)",
                    agent_name, block_name, success, duration_ms
                );
            }

            "forwarded" => {
                // A claudewire message forwarded during a flowchart block.
                // Extract the inner message and process stream events for rendering.
                if let Some(inner_msg) = event.get("message") {
                    let inner_type = inner_msg
                        .get("type")
                        .and_then(|v| v.as_str())
                        .unwrap_or("");

                    if inner_type == "stream_event" {
                        // Extract and render text_delta for live streaming
                        if let Some(text) = inner_msg
                            .get("event")
                            .and_then(|e| e.get("delta"))
                            .and_then(|d| d.get("text"))
                            .and_then(|t| t.as_str())
                        {
                            ctx.text_buffer.push_str(text);
                            streaming::live_edit_tick(
                                &mut ctx,
                                &state.discord_client,
                                agent_name,
                            )
                            .await;
                        }
                    } else if inner_type == "assistant" {
                        // Finalize any pending text when assistant message arrives
                        if !ctx.text_buffer.is_empty() {
                            streaming::live_edit_finalize(
                                &mut ctx,
                                &state.discord_client,
                                agent_name,
                            )
                            .await;
                        }
                    }
                }
            }

            "flowchart_complete" => {
                let status = event.get("status").and_then(|v| v.as_str()).unwrap_or("?");
                let blocks_executed = event.get("blocks_executed").and_then(serde_json::Value::as_u64).unwrap_or(0);
                let duration_ms = event.get("duration_ms").and_then(serde_json::Value::as_u64).unwrap_or(0);
                let cost = event.get("cost_usd").and_then(serde_json::Value::as_f64).unwrap_or(0.0);
                info!(
                    "Flowchart complete for '{}': status={}, blocks={}, {}ms, ${:.4}",
                    agent_name, status, blocks_executed, duration_ms, cost
                );

                // Finalize any remaining text from the last block
                if !ctx.text_buffer.is_empty() {
                    streaming::live_edit_finalize(
                        &mut ctx,
                        &state.discord_client,
                        agent_name,
                    )
                    .await;
                }

                if let Some(ch_id) = ctx.live_edit.as_ref().map(|le| le.channel_id) {
                    let secs = duration_ms as f64 / 1000.0;
                    let msg = format!(
                        "*Flowchart `{status}` — {blocks_executed} blocks in {secs:.1}s (${cost:.4})*"
                    );
                    let _ = state.discord_client.send_message(ch_id, &msg).await;
                }
            }

            "engine_status" => {
                debug!(
                    "Engine status for '{}': {:?}",
                    agent_name,
                    event
                );
            }

            "engine_log" => {
                let message = event.get("message").and_then(|v| v.as_str()).unwrap_or("");
                debug!("Engine log for '{}': {}", agent_name, message);
            }

            _ => {}
        }
    }

    // Finalize any remaining text
    if !ctx.text_buffer.is_empty() {
        streaming::live_edit_finalize(&mut ctx, &state.discord_client, agent_name).await;
    }

    // Clean up any remaining thinking or tool progress indicators
    streaming::hide_thinking(&mut ctx, &state.discord_client, agent_name).await;
    streaming::delete_tool_progress(&mut ctx, &state.discord_client, agent_name).await;

    // Append response timing to last message
    streaming::append_timing(&ctx, &state.discord_client, agent_name).await;

    // Auto-compact check
    check_auto_compact(state, agent_name, &transport).await;

    None
}

/// Check if context utilization exceeds the compact threshold.
///
/// Returns `Some(utilization)` if compact should trigger, `None` otherwise.
fn should_auto_compact(context_tokens: u64, context_window: u64, threshold: f64) -> Option<f64> {
    if context_window == 0 || context_tokens == 0 {
        return None;
    }
    let utilization = context_tokens as f64 / context_window as f64;
    if utilization >= threshold {
        Some(utilization)
    } else {
        None
    }
}

/// Check if context usage exceeds threshold and trigger compact if needed.
async fn check_auto_compact(
    state: &BotState,
    agent_name: &str,
    transport: &Arc<Mutex<CliSession>>,
) {
    let (context_tokens, context_window) = {
        let sessions = state.sessions.lock().await;
        match sessions.get(agent_name) {
            Some(s) => (s.context_tokens, s.context_window),
            None => return,
        }
    };

    let utilization = match should_auto_compact(context_tokens, context_window, state.config.compact_threshold) {
        Some(u) => u,
        None => return,
    };

    info!(
        "Auto-compact for '{}': {:.0}% context used ({}/{})",
        agent_name,
        utilization * 100.0,
        context_tokens,
        context_window
    );

    if let Some(ch_id) = state.channel_for_agent(agent_name).await {
        let _ = state
            .discord_client
            .send_message(ch_id.get(), "*Auto-compacting context...*")
            .await;
    }

    // Send /compact as user message
    let compact_msg = MessageContent::Text("/compact".to_string());
    let json_content = serde_json::Value::String("/compact".to_string());
    let msg = activity::make_user_message(&json_content);
    let msg_str = serde_json::to_string(&msg).unwrap_or_default();

    {
        let mut session = transport.lock().await;
        if let Err(e) = session.write(&msg_str).await {
            warn!("Failed to send auto-compact for '{}': {}", agent_name, e);
        }
    }

    // Note: The compact response will be handled by the next stream_response call
    // since the caller (process_message) will loop if there's more to process.
    // We don't stream the compact result here — the hub's message processing handles it.
    let _ = compact_msg; // suppress unused warning
}

// ---------------------------------------------------------------------------
// Permission callbacks
// ---------------------------------------------------------------------------

const EMOJI_NUMBERS: &[&str] = &["1\u{fe0f}\u{20e3}", "2\u{fe0f}\u{20e3}", "3\u{fe0f}\u{20e3}", "4\u{fe0f}\u{20e3}"];

/// Handle a permission request from Claude CLI. Returns the control response inner object.
async fn handle_permission_request(
    state: &BotState,
    agent_name: &str,
    request_id: &str,
    tool_name: &str,
    tool_input: &serde_json::Value,
    _transport: &Arc<Mutex<CliSession>>,
) -> serde_json::Value {
    let channel_id = match state.channel_for_agent(agent_name).await {
        Some(ch) => ch.get(),
        None => {
            return serde_json::json!({
                "subtype": "success",
                "request_id": request_id,
                "response": {"permission": "allow"}
            });
        }
    };

    match tool_name {
        "AskUserQuestion" => {
            handle_ask_user_question(state, agent_name, request_id, tool_input, channel_id).await
        }
        "ExitPlanMode" => {
            handle_exit_plan_mode(state, agent_name, request_id, channel_id).await
        }
        _ => {
            // Run CWD-based permission check before auto-allowing
            let perm_result = {
                let sessions = state.sessions.lock().await;
                if let Some(session) = sessions.get(agent_name) {
                    let config = crate::permissions::PermissionConfig::new(
                        std::path::Path::new(&session.cwd),
                        &state.config.axi_user_data,
                        &state.config.bot_dir,
                        Some(state.config.bot_worktrees_dir.as_path()),
                        vec![],
                    );
                    Some(crate::permissions::check_permission(&config, tool_name, tool_input))
                } else {
                    None
                }
            };

            match perm_result {
                Some(crate::permissions::PermissionResult::Deny(reason)) => {
                    warn!(
                        "Permission denied for '{}' tool '{}': {}",
                        agent_name, tool_name, reason
                    );
                    serde_json::json!({
                        "subtype": "success",
                        "request_id": request_id,
                        "response": {"permission": "deny", "message": reason}
                    })
                }
                _ => {
                    serde_json::json!({
                        "subtype": "success",
                        "request_id": request_id,
                        "response": {"permission": "allow"}
                    })
                }
            }
        }
    }
}

async fn handle_ask_user_question(
    state: &BotState,
    agent_name: &str,
    request_id: &str,
    tool_input: &serde_json::Value,
    channel_id: u64,
) -> serde_json::Value {
    let questions = tool_input
        .get("questions")
        .and_then(|v| v.as_array())
        .cloned()
        .unwrap_or_default();

    if questions.is_empty() {
        return serde_json::json!({
            "subtype": "success",
            "request_id": request_id,
            "response": {"permission": "allow"}
        });
    }

    // Format the question message
    let mut msg_parts = Vec::new();
    let mut all_options = Vec::new();

    for (qi, q) in questions.iter().enumerate() {
        let question_text = q.get("question").and_then(|v| v.as_str()).unwrap_or("?");
        let options = q.get("options").and_then(|v| v.as_array()).cloned().unwrap_or_default();

        if questions.len() > 1 {
            msg_parts.push(format!("**Q{}:** {}", qi + 1, question_text));
        } else {
            msg_parts.push(format!("**{question_text}**"));
        }

        for (oi, opt) in options.iter().enumerate() {
            let label = opt.get("label").and_then(|v| v.as_str()).unwrap_or("");
            let desc = opt.get("description").and_then(|v| v.as_str()).unwrap_or("");
            let emoji = EMOJI_NUMBERS.get(oi).unwrap_or(&"");
            msg_parts.push(format!("{emoji} **{label}** — {desc}"));
            all_options.push(label.to_string());
        }
    }

    let msg_content = msg_parts.join("\n");

    // Post to Discord
    let msg_id = match state.discord_client.send_message(channel_id, &msg_content).await {
        Ok(resp) => resp.get("id").and_then(|v| v.as_str()).unwrap_or("").to_string(),
        Err(e) => {
            warn!("Failed to post question for '{}': {}", agent_name, e);
            return serde_json::json!({
                "subtype": "success",
                "request_id": request_id,
                "response": {"permission": "allow"}
            });
        }
    };

    // Add reaction emojis
    let emoji_count = all_options.len().min(4);
    for emoji in &EMOJI_NUMBERS[..emoji_count] {
        if let Ok(id) = msg_id.parse::<u64>() {
            let _ = state.discord_client.add_reaction(channel_id, id, emoji).await;
        }
    }

    // Create oneshot channel and register pending question
    let (tx, rx) = tokio::sync::oneshot::channel();
    {
        let question = crate::state::PendingQuestion {
            agent_name: agent_name.to_string(),
            channel_id,
            message_id: msg_id.clone(),
            request_id: request_id.to_string(),
            question_type: crate::state::QuestionType::AskUser,
            options: all_options.clone(),
            sender: tx,
        };
        let mut pending = state.pending_questions.lock().await;
        pending.insert(msg_id.clone(), question);
    }

    // Wait for answer (with timeout)
    let answer = tokio::time::timeout(std::time::Duration::from_secs(300), rx).await;

    // Clean up
    {
        let mut pending = state.pending_questions.lock().await;
        pending.remove(&msg_id);
    }

    match answer {
        Ok(Ok(crate::state::QuestionAnswer::Selection(idx))) => {
            let label = all_options.get(idx).cloned().unwrap_or_default();
            let mut answers = HashMap::new();
            for q in &questions {
                let question_text = q.get("question").and_then(|v| v.as_str()).unwrap_or("?");
                answers.insert(question_text.to_string(), serde_json::Value::String(label.clone()));
            }
            serde_json::json!({
                "subtype": "success",
                "request_id": request_id,
                "response": {
                    "permission": "allow",
                    "updated_input": {
                        "questions": questions,
                        "answers": answers,
                    }
                }
            })
        }
        Ok(Ok(crate::state::QuestionAnswer::Text(text))) => {
            let mut answers = HashMap::new();
            for q in &questions {
                let question_text = q.get("question").and_then(|v| v.as_str()).unwrap_or("?");
                answers.insert(question_text.to_string(), serde_json::Value::String(text.clone()));
            }
            serde_json::json!({
                "subtype": "success",
                "request_id": request_id,
                "response": {
                    "permission": "allow",
                    "updated_input": {
                        "questions": questions,
                        "answers": answers,
                    }
                }
            })
        }
        _ => {
            // Timeout or channel closed — auto-allow
            serde_json::json!({
                "subtype": "success",
                "request_id": request_id,
                "response": {"permission": "allow"}
            })
        }
    }
}

async fn handle_exit_plan_mode(
    state: &BotState,
    agent_name: &str,
    request_id: &str,
    channel_id: u64,
) -> serde_json::Value {
    // Post plan approval request
    let msg_content = "*Plan ready for review. React with \u{2705} to approve or \u{274c} to deny.*";

    let msg_id = match state.discord_client.send_message(channel_id, msg_content).await {
        Ok(resp) => resp.get("id").and_then(|v| v.as_str()).unwrap_or("").to_string(),
        Err(e) => {
            warn!("Failed to post plan approval for '{}': {}", agent_name, e);
            return serde_json::json!({
                "subtype": "success",
                "request_id": request_id,
                "response": {"permission": "allow"}
            });
        }
    };

    // Add approval reactions
    if let Ok(id) = msg_id.parse::<u64>() {
        let _ = state.discord_client.add_reaction(channel_id, id, "\u{2705}").await; // ✅
        let _ = state.discord_client.add_reaction(channel_id, id, "\u{274c}").await; // ❌
    }

    // Create oneshot channel
    let (tx, rx) = tokio::sync::oneshot::channel();
    {
        let question = crate::state::PendingQuestion {
            agent_name: agent_name.to_string(),
            channel_id,
            message_id: msg_id.clone(),
            request_id: request_id.to_string(),
            question_type: crate::state::QuestionType::PlanApproval,
            options: Vec::new(),
            sender: tx,
        };
        let mut pending = state.pending_questions.lock().await;
        pending.insert(msg_id.clone(), question);
    }

    // Wait for answer
    let answer = tokio::time::timeout(std::time::Duration::from_secs(600), rx).await;

    // Clean up
    {
        let mut pending = state.pending_questions.lock().await;
        pending.remove(&msg_id);
    }

    match answer {
        Ok(Ok(crate::state::QuestionAnswer::Approved)) => {
            serde_json::json!({
                "subtype": "success",
                "request_id": request_id,
                "response": {"permission": "allow"}
            })
        }
        Ok(Ok(crate::state::QuestionAnswer::Denied)) => {
            serde_json::json!({
                "subtype": "success",
                "request_id": request_id,
                "response": {"permission": "deny"}
            })
        }
        _ => {
            // Timeout or error — default to deny for safety
            serde_json::json!({
                "subtype": "success",
                "request_id": request_id,
                "response": {"permission": "deny"}
            })
        }
    }
}

// ---------------------------------------------------------------------------
// SDK MCP message handler
// ---------------------------------------------------------------------------

/// Handle an MCP message from Claude CLI for an SDK-type MCP server.
///
/// Routes JSON-RPC methods (`initialize`, `tools/list`, `tools/call`,
/// `notifications/initialized`) to the appropriate `McpServer` instance
/// stored on the agent's session.
async fn handle_mcp_message(
    state: &BotState,
    agent_name: &str,
    server_name: &str,
    message: &serde_json::Value,
) -> serde_json::Value {
    let jsonrpc_id = message.get("id").cloned();
    let method = message
        .get("method")
        .and_then(|v| v.as_str())
        .unwrap_or("");
    let params = message.get("params").cloned().unwrap_or(serde_json::Value::Null);

    // Look up the McpServer from the agent's session
    let server = {
        let sessions = state.sessions.lock().await;
        sessions
            .get(agent_name)
            .and_then(|s| s.sdk_mcp_servers.get(server_name).cloned())
    };

    let Some(server) = server else {
        return serde_json::json!({
            "jsonrpc": "2.0",
            "id": jsonrpc_id,
            "error": {"code": -32601, "message": format!("Server '{}' not found", server_name)},
        });
    };

    match method {
        "initialize" => {
            serde_json::json!({
                "jsonrpc": "2.0",
                "id": jsonrpc_id,
                "result": {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {
                        "tools": {}
                    },
                    "serverInfo": {
                        "name": server.name,
                        "version": server.version,
                    },
                },
            })
        }

        "tools/list" => {
            let tools_data: Vec<serde_json::Value> = server
                .tools
                .iter()
                .map(|t| {
                    serde_json::json!({
                        "name": t.name,
                        "description": t.description,
                        "inputSchema": t.input_schema,
                    })
                })
                .collect();

            serde_json::json!({
                "jsonrpc": "2.0",
                "id": jsonrpc_id,
                "result": {"tools": tools_data},
            })
        }

        "tools/call" => {
            let tool_name = params
                .get("name")
                .and_then(|v| v.as_str())
                .unwrap_or("");
            let arguments = params
                .get("arguments")
                .and_then(|v| v.as_object())
                .cloned()
                .unwrap_or_default();

            let result = server.call_tool(tool_name, arguments).await;

            let mut response_data = serde_json::json!({
                "content": result.content,
            });
            if result.is_error == Some(true) {
                response_data["is_error"] = serde_json::json!(true);
            }

            serde_json::json!({
                "jsonrpc": "2.0",
                "id": jsonrpc_id,
                "result": response_data,
            })
        }

        "notifications/initialized" => {
            // One-way notification, acknowledge with empty result
            serde_json::json!({"jsonrpc": "2.0", "result": {}})
        }

        _ => {
            serde_json::json!({
                "jsonrpc": "2.0",
                "id": jsonrpc_id,
                "error": {"code": -32601, "message": format!("Method '{}' not found", method)},
            })
        }
    }
}

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

/// Build a `claudewire::config::Config` from session state.
///
/// Every field is set explicitly — no hidden defaults from `Config::default()`.
/// This is Axi's policy for how to invoke Claude CLI.
fn build_cli_config(
    config_path: &std::path::Path,
    resume_session_id: Option<&str>,
    system_prompt: Option<&serde_json::Value>,
    mcp_servers: Option<&serde_json::Value>,
    sdk_mcp_servers: &serde_json::Value,
    plan_mode: bool,
) -> CliConfig {
    // Extract prompt string from JSON value
    let prompt = system_prompt.and_then(|v| {
        let s = v.as_str().map(String::from).or_else(|| serde_json::to_string(v).ok());
        s.filter(|s| !s.is_empty())
    });

    CliConfig {
        model: axi_config::get_model(config_path),
        append_system_prompt: prompt,
        permission_mode: if plan_mode { "plan" } else { "default" }.into(),
        setting_sources: vec!["local".into()],
        mcp_servers: McpServers {
            external: mcp_servers.cloned(),
            sdk: Some(sdk_mcp_servers.clone()),
        },
        disallowed_tools: vec!["Task".into()],
        allowed_tools: Vec::new(),
        max_thinking_tokens: Some(128_000),
        effort: Some("high".into()),
        sandbox_enabled: true,
        auto_allow_bash_if_sandboxed: true,
        resume: resume_session_id.map(ToString::to_string),
        include_partial_messages: true,
        verbose: true,
        debug_to_stderr: true,
        print_mode: false,
        replay_user_messages: false,
    }
}

/// Record usage statistics from a result event.
async fn record_usage(state: &BotState, agent_name: &str, result: &serde_json::Value) {
    let session_id = result
        .get("session_id")
        .and_then(|v| v.as_str())
        .unwrap_or("");
    let cost_usd = result.get("cost_usd").and_then(serde_json::Value::as_f64).unwrap_or(0.0);
    let num_turns = result.get("num_turns").and_then(serde_json::Value::as_u64).unwrap_or(0);
    let duration_ms = result
        .get("duration_ms")
        .and_then(serde_json::Value::as_u64)
        .unwrap_or(0);
    let input_tokens = result
        .get("usage")
        .and_then(|u| u.get("input_tokens"))
        .and_then(serde_json::Value::as_u64)
        .unwrap_or(0);
    let output_tokens = result
        .get("usage")
        .and_then(|u| u.get("output_tokens"))
        .and_then(serde_json::Value::as_u64)
        .unwrap_or(0);

    if !session_id.is_empty() {
        let mut tracker = state.rate_limits.lock().await;
        crate::rate_limits::record_session_usage(
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
    use std::sync::Arc;

    use serenity::all::ChannelId;
    use wiremock::matchers::{method, path_regex};
    use wiremock::{Mock, MockServer, ResponseTemplate};

    use crate::state::{BotState, QuestionAnswer};

    /// Create a `BotState` backed by wiremock for permission callback tests.
    ///
    /// Pre-registers agent "test-agent" → channel 100.
    async fn setup_state() -> (MockServer, Arc<BotState>) {
        let server = MockServer::start().await;

        // POST messages returns {"id": "999"}
        Mock::given(method("POST"))
            .and(path_regex(r"^/channels/\d+/messages$"))
            .respond_with(
                ResponseTemplate::new(200).set_body_json(serde_json::json!({"id": "999"})),
            )
            .expect(0..)
            .mount(&server)
            .await;

        // PUT reactions returns 204
        Mock::given(method("PUT"))
            .and(path_regex(r"^/channels/\d+/messages/\d+/reactions/"))
            .respond_with(ResponseTemplate::new(204))
            .expect(0..)
            .mount(&server)
            .await;

        let tmp = tempfile::tempdir().unwrap();
        let config = axi_config::Config::for_test(tmp.path());
        let discord = axi_config::DiscordClient::with_base_url("test-token", server.uri());
        let state = Arc::new(BotState::new(config, discord));

        // Register test agent channel
        state.register_channel(ChannelId::new(100), "test-agent").await;

        // Leak the tmpdir so it lives as long as the test
        std::mem::forget(tmp);

        (server, state)
    }

    #[test]
    fn cli_config_fresh() {
        let path = std::path::Path::new("/tmp/nonexistent-config.json");
        let cfg = build_cli_config(path, None, None, None, &serde_json::json!({}), false);
        let args = cfg.to_cli_args();
        assert!(args.contains(&"claude".to_string()));
        assert!(args.contains(&"--output-format".to_string()));
        assert!(args.contains(&"stream-json".to_string()));
        assert!(args.contains(&"--input-format".to_string()));
        // Must NOT contain --print (SDK behavior)
        assert!(!args.contains(&"--print".to_string()));
        assert!(!args.contains(&"-p".to_string()));
        // Must contain flags that were previously missing
        assert!(args.contains(&"--max-thinking-tokens".to_string()));
        assert!(args.contains(&"--effort".to_string()));
        assert!(args.contains(&"--settings".to_string()));
        assert!(args.contains(&"--debug-to-stderr".to_string()));
        assert!(!args.contains(&"--resume".to_string()));
        assert!(!args.contains(&"--append-system-prompt".to_string()));
        assert!(!args.contains(&"--mcp-config".to_string()));
    }

    #[test]
    fn cli_config_env_has_sdk_vars() {
        let path = std::path::Path::new("/tmp/nonexistent-config.json");
        let cfg = build_cli_config(path, None, None, None, &serde_json::json!({}), false);
        let env = cfg.to_env();
        // These were previously missing — critical for SDK control protocol
        assert_eq!(env.get("CLAUDE_CODE_ENTRYPOINT").unwrap(), "sdk-py");
        assert!(env.get("CLAUDE_AGENT_SDK_VERSION").is_some());
        assert_eq!(env.get("CLAUDE_AUTOCOMPACT_PCT_OVERRIDE").unwrap(), "100");
    }

    #[test]
    fn cli_config_resume() {
        let path = std::path::Path::new("/tmp/nonexistent-config.json");
        let cfg = build_cli_config(path, Some("session-123"), None, None, &serde_json::json!({}), false);
        let args = cfg.to_cli_args();
        assert!(args.contains(&"--resume".to_string()));
        assert!(args.contains(&"session-123".to_string()));
    }

    #[test]
    fn cli_config_with_system_prompt() {
        let path = std::path::Path::new("/tmp/nonexistent-config.json");
        let prompt = serde_json::Value::String("You are a test bot.".to_string());
        let cfg = build_cli_config(path, None, Some(&prompt), None, &serde_json::json!({}), false);
        let args = cfg.to_cli_args();
        assert!(args.contains(&"--append-system-prompt".to_string()));
        assert!(args.contains(&"You are a test bot.".to_string()));
    }

    #[test]
    fn cli_config_plan_mode() {
        let path = std::path::Path::new("/tmp/nonexistent-config.json");
        let cfg = build_cli_config(path, None, None, None, &serde_json::json!({}), true);
        let args = cfg.to_cli_args();
        let idx = args.iter().position(|a| a == "--permission-mode").unwrap();
        assert_eq!(args[idx + 1], "plan");
    }

    #[test]
    fn cli_config_with_mcp_servers() {
        let path = std::path::Path::new("/tmp/nonexistent-config.json");
        let mcp = serde_json::json!({"myserver": {"command": "node", "args": ["server.js"]}});
        let cfg = build_cli_config(path, None, None, Some(&mcp), &serde_json::json!({}), false);
        let args = cfg.to_cli_args();
        assert!(args.contains(&"--mcp-config".to_string()));
        let mcp_idx = args.iter().position(|a| a == "--mcp-config").unwrap();
        let mcp_val: serde_json::Value = serde_json::from_str(&args[mcp_idx + 1]).unwrap();
        assert!(mcp_val.get("mcpServers").is_some());
    }

    #[test]
    fn cli_config_with_sdk_mcp_servers() {
        let path = std::path::Path::new("/tmp/nonexistent-config.json");
        let sdk = serde_json::json!({"utils": {"type": "sdk", "name": "utils", "version": "1.0.0"}});
        let cfg = build_cli_config(path, None, None, None, &sdk, false);
        let args = cfg.to_cli_args();
        assert!(args.contains(&"--mcp-config".to_string()));
        let mcp_idx = args.iter().position(|a| a == "--mcp-config").unwrap();
        let mcp_val: serde_json::Value = serde_json::from_str(&args[mcp_idx + 1]).unwrap();
        let servers = mcp_val.get("mcpServers").unwrap();
        assert_eq!(servers.get("utils").unwrap().get("type").unwrap(), "sdk");
    }

    #[test]
    fn cli_config_merges_sdk_and_external_mcp() {
        let path = std::path::Path::new("/tmp/nonexistent-config.json");
        let external = serde_json::json!({"myserver": {"command": "node"}});
        let sdk = serde_json::json!({"utils": {"type": "sdk", "name": "utils", "version": "1.0.0"}});
        let cfg = build_cli_config(path, None, None, Some(&external), &sdk, false);
        let args = cfg.to_cli_args();
        let mcp_idx = args.iter().position(|a| a == "--mcp-config").unwrap();
        let mcp_val: serde_json::Value = serde_json::from_str(&args[mcp_idx + 1]).unwrap();
        let servers = mcp_val.get("mcpServers").unwrap();
        assert!(servers.get("utils").is_some());
        assert!(servers.get("myserver").is_some());
    }

    #[test]
    fn cli_config_flowcoder_keeps_sdk_mcp() {
        // Engine relays control_request messages so SDK MCP servers are kept.
        let path = std::path::Path::new("/tmp/nonexistent-config.json");
        let external = serde_json::json!({"ext": {"command": "node"}});
        let sdk = serde_json::json!({"utils": {"type": "sdk"}});
        let cfg = build_cli_config(path, None, None, Some(&external), &sdk, false);
        let args = cfg.to_cli_args();
        let mcp_idx = args.iter().position(|a| a == "--mcp-config").unwrap();
        let mcp_val: serde_json::Value = serde_json::from_str(&args[mcp_idx + 1]).unwrap();
        let servers = mcp_val.get("mcpServers").unwrap();
        // Both SDK and external should be present
        assert!(servers.get("utils").is_some());
        assert!(servers.get("ext").is_some());
    }

    #[test]
    fn transient_error_detection() {
        assert!(is_transient_error("Server overloaded, try again"));
        assert!(is_transient_error("rate_limit exceeded"));
        assert!(is_transient_error("HTTP 529 error"));
        assert!(!is_transient_error("Invalid API key"));
        assert!(!is_transient_error("Permission denied"));
    }

    // -----------------------------------------------------------------------
    // Permission callback tests
    // -----------------------------------------------------------------------

    #[tokio::test]
    async fn ask_user_question_empty_questions_auto_allows() {
        let (_server, state) = setup_state().await;
        let tool_input = serde_json::json!({"questions": []});

        let result = handle_ask_user_question(
            &state, "test-agent", "req-1", &tool_input, 100,
        ).await;

        assert_eq!(result["response"]["permission"], "allow");
        assert!(result.get("response").unwrap().get("updated_input").is_none());
    }

    #[tokio::test]
    async fn ask_user_question_posts_formatted_question() {
        let (server, state) = setup_state().await;
        let tool_input = serde_json::json!({
            "questions": [{
                "question": "Which database?",
                "header": "DB",
                "multiSelect": false,
                "options": [
                    {"label": "PostgreSQL", "description": "Relational"},
                    {"label": "MongoDB", "description": "Document store"},
                ]
            }]
        });

        // Spawn the handler and immediately resolve the oneshot
        let state_clone = state.clone();
        let handle = tokio::spawn(async move {
            handle_ask_user_question(
                &state_clone, "test-agent", "req-2", &tool_input, 100,
            ).await
        });

        // Wait briefly for the question to be posted
        tokio::time::sleep(std::time::Duration::from_millis(100)).await;

        // Resolve the pending question with a selection
        {
            let mut pending = state.pending_questions.lock().await;
            if let Some(q) = pending.remove("999") {
                q.sender.send(QuestionAnswer::Selection(1)).ok();
            }
        }

        let result = handle.await.unwrap();
        assert_eq!(result["response"]["permission"], "allow");
        assert!(result["response"]["updated_input"].is_object());

        // Verify POST was made with question content
        let requests = server.received_requests().await.unwrap();
        let post_reqs: Vec<_> = requests
            .iter()
            .filter(|r| r.method == wiremock::http::Method::POST)
            .collect();
        assert!(!post_reqs.is_empty());
        let body: serde_json::Value =
            serde_json::from_slice(&post_reqs[0].body).unwrap();
        let content = body["content"].as_str().unwrap();
        assert!(content.contains("Which database?"));
        assert!(content.contains("PostgreSQL"));
        assert!(content.contains("MongoDB"));

        // Verify reactions were added (PUT requests)
        let put_count = requests
            .iter()
            .filter(|r| r.method == wiremock::http::Method::PUT)
            .count();
        assert_eq!(put_count, 2); // two options → two emoji reactions
    }

    #[tokio::test]
    async fn ask_user_question_selection_answer() {
        let (_server, state) = setup_state().await;
        let tool_input = serde_json::json!({
            "questions": [{
                "question": "Pick a color?",
                "header": "Color",
                "multiSelect": false,
                "options": [
                    {"label": "Red", "description": "Warm"},
                    {"label": "Blue", "description": "Cool"},
                ]
            }]
        });

        let state_clone = state.clone();
        let handle = tokio::spawn(async move {
            handle_ask_user_question(
                &state_clone, "test-agent", "req-3", &tool_input, 100,
            ).await
        });

        tokio::time::sleep(std::time::Duration::from_millis(100)).await;

        {
            let mut pending = state.pending_questions.lock().await;
            if let Some(q) = pending.remove("999") {
                q.sender.send(QuestionAnswer::Selection(0)).ok();
            }
        }

        let result = handle.await.unwrap();
        let updated = &result["response"]["updated_input"];
        let answers = &updated["answers"];
        assert_eq!(answers["Pick a color?"], "Red");
    }

    #[tokio::test]
    async fn ask_user_question_text_answer() {
        let (_server, state) = setup_state().await;
        let tool_input = serde_json::json!({
            "questions": [{
                "question": "What name?",
                "header": "Name",
                "multiSelect": false,
                "options": [
                    {"label": "Alice", "description": "Name A"},
                    {"label": "Bob", "description": "Name B"},
                ]
            }]
        });

        let state_clone = state.clone();
        let handle = tokio::spawn(async move {
            handle_ask_user_question(
                &state_clone, "test-agent", "req-4", &tool_input, 100,
            ).await
        });

        tokio::time::sleep(std::time::Duration::from_millis(100)).await;

        {
            let mut pending = state.pending_questions.lock().await;
            if let Some(q) = pending.remove("999") {
                q.sender.send(QuestionAnswer::Text("Charlie".to_string())).ok();
            }
        }

        let result = handle.await.unwrap();
        let answers = &result["response"]["updated_input"]["answers"];
        assert_eq!(answers["What name?"], "Charlie");
    }

    #[tokio::test]
    async fn exit_plan_mode_posts_approval_request() {
        let (server, state) = setup_state().await;

        let state_clone = state.clone();
        let handle = tokio::spawn(async move {
            handle_exit_plan_mode(&state_clone, "test-agent", "req-5", 100).await
        });

        tokio::time::sleep(std::time::Duration::from_millis(100)).await;

        {
            let mut pending = state.pending_questions.lock().await;
            if let Some(q) = pending.remove("999") {
                q.sender.send(QuestionAnswer::Approved).ok();
            }
        }

        let result = handle.await.unwrap();
        assert_eq!(result["response"]["permission"], "allow");

        // Verify approval message was posted
        let requests = server.received_requests().await.unwrap();
        let post_count = requests
            .iter()
            .filter(|r| r.method == wiremock::http::Method::POST)
            .count();
        assert!(post_count > 0);

        // Verify ✅ and ❌ reactions added
        let put_count = requests
            .iter()
            .filter(|r| r.method == wiremock::http::Method::PUT)
            .count();
        assert_eq!(put_count, 2);
    }

    #[tokio::test]
    async fn exit_plan_mode_denied() {
        let (_server, state) = setup_state().await;

        let state_clone = state.clone();
        let handle = tokio::spawn(async move {
            handle_exit_plan_mode(&state_clone, "test-agent", "req-6", 100).await
        });

        tokio::time::sleep(std::time::Duration::from_millis(100)).await;

        {
            let mut pending = state.pending_questions.lock().await;
            if let Some(q) = pending.remove("999") {
                q.sender.send(QuestionAnswer::Denied).ok();
            }
        }

        let result = handle.await.unwrap();
        assert_eq!(result["response"]["permission"], "deny");
    }

    #[tokio::test]
    async fn permission_request_auto_allows_unknown_tools() {
        let (_server, state) = setup_state().await;
        let transport = Arc::new(Mutex::new(
            CliSession::new(
                "test-agent".to_string(),
                tokio::sync::mpsc::unbounded_channel().1,
                tokio::sync::mpsc::unbounded_channel().0,
                Box::new(|_name, _data| Box::pin(async move { Ok(()) })),
                Box::new(|_name| Box::pin(async move { Ok(()) })),
                Box::new(|| true),
                false,
                None,
            ),
        ));

        let result = handle_permission_request(
            &state, "test-agent", "req-7", "Bash",
            &serde_json::json!({}), &transport,
        ).await;

        assert_eq!(result["response"]["permission"], "allow");
    }

    #[tokio::test]
    async fn permission_request_no_channel_auto_allows() {
        let (_server, state) = setup_state().await;
        let transport = Arc::new(Mutex::new(
            CliSession::new(
                "unknown-agent".to_string(),
                tokio::sync::mpsc::unbounded_channel().1,
                tokio::sync::mpsc::unbounded_channel().0,
                Box::new(|_name, _data| Box::pin(async move { Ok(()) })),
                Box::new(|_name| Box::pin(async move { Ok(()) })),
                Box::new(|| true),
                false,
                None,
            ),
        ));

        // "unknown-agent" has no channel registered
        let result = handle_permission_request(
            &state, "unknown-agent", "req-8", "AskUserQuestion",
            &serde_json::json!({"questions": [{"question": "Q?", "options": []}]}),
            &transport,
        ).await;

        assert_eq!(result["response"]["permission"], "allow");
    }

    #[tokio::test]
    async fn permission_denies_write_outside_cwd() {
        let (_server, state) = setup_state().await;

        // Register agent with a specific CWD
        let mut session = crate::types::AgentSession::new("test-agent".to_string());
        session.cwd = "/tmp/agent-workdir".to_string();
        std::fs::create_dir_all("/tmp/agent-workdir").ok();
        {
            let mut sessions = state.sessions.lock().await;
            sessions.insert("test-agent".to_string(), session);
        }

        let transport = Arc::new(Mutex::new(
            CliSession::new(
                "test-agent".to_string(),
                tokio::sync::mpsc::unbounded_channel().1,
                tokio::sync::mpsc::unbounded_channel().0,
                Box::new(|_name, _data| Box::pin(async move { Ok(()) })),
                Box::new(|_name| Box::pin(async move { Ok(()) })),
                Box::new(|| true),
                false,
                None,
            ),
        ));

        // Write to /etc/passwd should be denied
        let result = handle_permission_request(
            &state, "test-agent", "req-9", "Write",
            &serde_json::json!({"file_path": "/etc/passwd"}),
            &transport,
        ).await;

        assert_eq!(result["response"]["permission"], "deny");
        assert!(result["response"]["message"].as_str().unwrap().contains("outside"));
    }

    #[tokio::test]
    async fn permission_allows_write_inside_cwd() {
        let (_server, state) = setup_state().await;

        let tmp = tempfile::tempdir().unwrap();
        let cwd = tmp.path().to_str().unwrap().to_string();

        let mut session = crate::types::AgentSession::new("test-agent".to_string());
        session.cwd = cwd.clone();
        {
            let mut sessions = state.sessions.lock().await;
            sessions.insert("test-agent".to_string(), session);
        }

        let transport = Arc::new(Mutex::new(
            CliSession::new(
                "test-agent".to_string(),
                tokio::sync::mpsc::unbounded_channel().1,
                tokio::sync::mpsc::unbounded_channel().0,
                Box::new(|_name, _data| Box::pin(async move { Ok(()) })),
                Box::new(|_name| Box::pin(async move { Ok(()) })),
                Box::new(|| true),
                false,
                None,
            ),
        ));

        let file_path = tmp.path().join("test.txt");
        std::fs::write(&file_path, "test").unwrap();
        let result = handle_permission_request(
            &state, "test-agent", "req-10", "Write",
            &serde_json::json!({"file_path": file_path.to_str().unwrap()}),
            &transport,
        ).await;

        assert_eq!(result["response"]["permission"], "allow");
    }

    #[tokio::test]
    async fn permission_denies_forbidden_tool() {
        let (_server, state) = setup_state().await;

        let mut session = crate::types::AgentSession::new("test-agent".to_string());
        session.cwd = "/tmp".to_string();
        {
            let mut sessions = state.sessions.lock().await;
            sessions.insert("test-agent".to_string(), session);
        }

        let transport = Arc::new(Mutex::new(
            CliSession::new(
                "test-agent".to_string(),
                tokio::sync::mpsc::unbounded_channel().1,
                tokio::sync::mpsc::unbounded_channel().0,
                Box::new(|_name, _data| Box::pin(async move { Ok(()) })),
                Box::new(|_name| Box::pin(async move { Ok(()) })),
                Box::new(|| true),
                false,
                None,
            ),
        ));

        let result = handle_permission_request(
            &state, "test-agent", "req-11", "Task",
            &serde_json::json!({}),
            &transport,
        ).await;

        assert_eq!(result["response"]["permission"], "deny");
    }

    // -----------------------------------------------------------------------
    // Auto-compact threshold tests
    // -----------------------------------------------------------------------

    #[test]
    fn auto_compact_zero_tokens_returns_none() {
        assert!(should_auto_compact(0, 200_000, 0.80).is_none());
    }

    #[test]
    fn auto_compact_zero_window_returns_none() {
        assert!(should_auto_compact(100_000, 0, 0.80).is_none());
    }

    #[test]
    fn auto_compact_below_threshold_returns_none() {
        // 50% utilization, 80% threshold
        assert!(should_auto_compact(100_000, 200_000, 0.80).is_none());
    }

    #[test]
    fn auto_compact_at_threshold_triggers() {
        // 80% utilization, 80% threshold
        let result = should_auto_compact(160_000, 200_000, 0.80);
        assert!(result.is_some());
        let util = result.unwrap();
        assert!((util - 0.80).abs() < 0.001);
    }

    #[test]
    fn auto_compact_above_threshold_triggers() {
        // 95% utilization
        let result = should_auto_compact(190_000, 200_000, 0.80);
        assert!(result.is_some());
        let util = result.unwrap();
        assert!((util - 0.95).abs() < 0.001);
    }

    // -----------------------------------------------------------------------
    // SDK MCP message handler tests
    // -----------------------------------------------------------------------

    #[tokio::test]
    async fn mcp_message_initialize() {
        let (_server, state) = setup_state().await;

        // Register a session with SDK MCP servers
        let mut session = crate::types::AgentSession::new("test-agent".to_string());
        let (sdk_servers, _) = crate::mcp_tools::build_sdk_mcp_config(
            &state, "test-agent", "/tmp/test", false,
        );
        session.sdk_mcp_servers = sdk_servers;
        {
            let mut sessions = state.sessions.lock().await;
            sessions.insert("test-agent".to_string(), session);
        }

        let msg = serde_json::json!({
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {}
        });

        let resp = handle_mcp_message(&state, "test-agent", "utils", &msg).await;
        assert_eq!(resp["jsonrpc"], "2.0");
        assert_eq!(resp["id"], 1);
        assert!(resp["result"]["serverInfo"]["name"].as_str().is_some());
        assert_eq!(resp["result"]["serverInfo"]["name"], "utils");
    }

    #[tokio::test]
    async fn mcp_message_tools_list() {
        let (_server, state) = setup_state().await;

        let mut session = crate::types::AgentSession::new("test-agent".to_string());
        let (sdk_servers, _) = crate::mcp_tools::build_sdk_mcp_config(
            &state, "test-agent", "/tmp/test", false,
        );
        session.sdk_mcp_servers = sdk_servers;
        {
            let mut sessions = state.sessions.lock().await;
            sessions.insert("test-agent".to_string(), session);
        }

        let msg = serde_json::json!({
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/list",
            "params": {}
        });

        let resp = handle_mcp_message(&state, "test-agent", "utils", &msg).await;
        let tools = resp["result"]["tools"].as_array().unwrap();
        assert!(!tools.is_empty());
        let tool_names: Vec<&str> = tools.iter()
            .filter_map(|t| t["name"].as_str())
            .collect();
        assert!(tool_names.contains(&"get_date_and_time"));
    }

    #[tokio::test]
    async fn mcp_message_tools_call() {
        let (_server, state) = setup_state().await;

        let mut session = crate::types::AgentSession::new("test-agent".to_string());
        let (sdk_servers, _) = crate::mcp_tools::build_sdk_mcp_config(
            &state, "test-agent", "/tmp/test", false,
        );
        session.sdk_mcp_servers = sdk_servers;
        {
            let mut sessions = state.sessions.lock().await;
            sessions.insert("test-agent".to_string(), session);
        }

        let msg = serde_json::json!({
            "jsonrpc": "2.0",
            "id": 3,
            "method": "tools/call",
            "params": {
                "name": "get_date_and_time",
                "arguments": {}
            }
        });

        let resp = handle_mcp_message(&state, "test-agent", "utils", &msg).await;
        assert_eq!(resp["jsonrpc"], "2.0");
        assert_eq!(resp["id"], 3);
        let content = resp["result"]["content"].as_array().unwrap();
        assert!(!content.is_empty());
        assert!(content[0]["text"].as_str().unwrap().contains("now"));
    }

    #[tokio::test]
    async fn mcp_message_unknown_server() {
        let (_server, state) = setup_state().await;

        let mut session = crate::types::AgentSession::new("test-agent".to_string());
        session.sdk_mcp_servers = HashMap::new();
        {
            let mut sessions = state.sessions.lock().await;
            sessions.insert("test-agent".to_string(), session);
        }

        let msg = serde_json::json!({
            "jsonrpc": "2.0",
            "id": 4,
            "method": "tools/list",
            "params": {}
        });

        let resp = handle_mcp_message(&state, "test-agent", "nonexistent", &msg).await;
        assert!(resp["error"].is_object());
        assert_eq!(resp["error"]["code"], -32601);
    }

    #[tokio::test]
    async fn mcp_message_unknown_method() {
        let (_server, state) = setup_state().await;

        let mut session = crate::types::AgentSession::new("test-agent".to_string());
        let (sdk_servers, _) = crate::mcp_tools::build_sdk_mcp_config(
            &state, "test-agent", "/tmp/test", false,
        );
        session.sdk_mcp_servers = sdk_servers;
        {
            let mut sessions = state.sessions.lock().await;
            sessions.insert("test-agent".to_string(), session);
        }

        let msg = serde_json::json!({
            "jsonrpc": "2.0",
            "id": 5,
            "method": "some/unknown/method",
            "params": {}
        });

        let resp = handle_mcp_message(&state, "test-agent", "utils", &msg).await;
        assert!(resp["error"].is_object());
        assert_eq!(resp["error"]["code"], -32601);
    }
}
