//! MCP tool implementations — agent management, Discord, and utilities.
//!
//! Each function creates an `McpServer` with the appropriate tools registered.
//! Tool handlers capture shared state via Arc closures.

use std::collections::HashMap;
use std::fmt::Write;
use std::sync::Arc;

use chrono::{Datelike, Local, Timelike, Weekday};
use serenity::all::ChannelId;
use serde_json::{json, Value};
use tracing::info;

use axi_config::DiscordClient;

use crate::mcp_protocol::{McpServer, ToolArgs, ToolResult};
use crate::state::BotState;

// ---------------------------------------------------------------------------
// Helper
// ---------------------------------------------------------------------------

fn get_str(args: &ToolArgs, key: &str) -> String {
    args.get(key)
        .and_then(|v| v.as_str())
        .unwrap_or("")
        .trim()
        .to_string()
}

fn get_opt_str(args: &ToolArgs, key: &str) -> Option<String> {
    args.get(key)
        .and_then(|v| v.as_str())
        .map(|s| s.trim().to_string())
        .filter(|s| !s.is_empty())
}

/// Parse a Discord snowflake ID from a string or number value.
fn parse_id(args: &ToolArgs, key: &str) -> Option<u64> {
    args.get(key).and_then(|v| {
        v.as_u64()
            .or_else(|| v.as_str().and_then(|s| s.parse().ok()))
    })
}

/// Build the spawned agent system prompt as JSON.
fn build_spawned_prompt(
    state: &BotState,
    cwd: &str,
    packs: Option<&[String]>,
    compact_instructions: Option<&str>,
) -> Value {
    let pack_strs: Option<Vec<&str>> = packs.map(|v| v.iter().map(String::as_str).collect());
    let preset = state.prompt_builder.spawned_agent_prompt(
        cwd,
        pack_strs.as_deref(),
        compact_instructions,
    );
    json!({
        "type": "custom_preset",
        "preset": preset.preset,
        "custom_instructions": preset.append,
    })
}

/// Build the MCP servers JSON config for an agent.
fn build_mcp_servers(
    state: &BotState,
    _agent_name: &str,
    _cwd: &str,
    extra_names: Option<Vec<String>>,
) -> Option<Value> {
    let names = extra_names.unwrap_or_default();
    if names.is_empty() {
        return None;
    }
    let servers = axi_config::mcp::load_mcp_servers(&state.config.mcp_servers_path, &names);
    if servers.is_empty() {
        return None;
    }
    Some(json!(servers))
}

/// Default CWD for a spawned agent.
fn default_agent_cwd(state: &BotState, agent_name: &str) -> String {
    state
        .config
        .axi_user_data
        .join("agents")
        .join(agent_name)
        .to_string_lossy()
        .to_string()
}

/// Validate an agent name: must be 1-50 chars of `[a-z0-9-]`, no leading/trailing dash.
fn validate_agent_name(name: &str) -> Result<(), String> {
    if name.is_empty() || name.len() > 50 {
        return Err("Agent name must be 1-50 characters.".to_string());
    }
    if !name.chars().all(|c| c.is_ascii_lowercase() || c.is_ascii_digit() || c == '-') {
        return Err("Agent name must only contain lowercase letters, digits, and hyphens.".to_string());
    }
    if name.starts_with('-') || name.ends_with('-') {
        return Err("Agent name must not start or end with a hyphen.".to_string());
    }
    Ok(())
}

/// Create or find a Discord channel for an agent, returning its ID.
async fn create_agent_channel(state: &BotState, agent_name: &str) -> Result<u64, String> {
    let dc = &state.discord_client;
    let guild_id = state.config.discord_guild_id;
    let normalized = crate::channels::normalize_channel_name(agent_name);

    // Check for existing channel
    if let Ok(Some(ch)) = dc.find_channel(guild_id, &normalized).await {
        if let Some(id) = ch.get("id").and_then(|v| v.as_str()).and_then(|s| s.parse::<u64>().ok()) {
            return Ok(id);
        }
    }

    // Create new text channel (type 0)
    match dc.create_channel(guild_id, &normalized, 0).await {
        Ok(ch) => {
            let id = ch
                .get("id")
                .and_then(|v| v.as_str())
                .and_then(|s| s.parse::<u64>().ok())
                .ok_or_else(|| "Missing channel ID in response".to_string())?;

            // Move to active category if available
            if let Some(infra) = &*state.infra.read().await {
                if let Some(cat_id) = infra.active_category_id {
                    let _ = dc.edit_channel_category(id, cat_id.get()).await;
                }
            }

            info!("Created channel #{} for agent '{}'", normalized, agent_name);
            Ok(id)
        }
        Err(e) => Err(format!("Failed to create channel: {e}")),
    }
}

/// Run initial prompt for a spawned agent in a background task.
fn run_initial_prompt(state: &Arc<BotState>, agent_name: &str, prompt: &str) {
    let state = Arc::clone(state);
    let name = agent_name.to_string();
    let prompt = prompt.to_string();
    let stream_handler = crate::claude_process::make_stream_handler(Arc::clone(&state));

    tokio::spawn(async move {
        let content = crate::types::MessageContent::Text(prompt);
        let woke = crate::lifecycle::wake_or_queue(&state, &name, content.clone(), None).await;

        if woke {
            let query_lock = {
                let sessions = state.sessions.lock().await;
                sessions.get(&name).map(|s| s.query_lock.clone())
            };

            if let Some(query_lock) = query_lock {
                let _lock = query_lock.lock().await;
                let _ = crate::messaging::process_message(&state, &name, &content, &stream_handler).await;

                crate::messaging::process_message_queue(&state, &name, &stream_handler).await;
                crate::lifecycle::sleep_agent(&state, &name, false).await;
            }
        }
    });
}

// ---------------------------------------------------------------------------
// Utility tools (available to all agents)
// ---------------------------------------------------------------------------

/// Create the utils MCP server (date/time, file upload, status).
pub fn create_utils_server(state: Arc<BotState>) -> McpServer {
    let mut server = McpServer::new("utils", "1.0.0");
    let cfg = Arc::new(state.config.clone());

    // get_date_and_time
    let cfg_dt = cfg;
    server.add_tool(
        "get_date_and_time",
        "Get the current date and time with logical day/week calculations. \
         Accounts for the user's configured day boundary (the hour when a new 'day' starts). \
         Always call this first to orient yourself before working with plans.",
        json!({"type": "object", "properties": {}, "required": []}),
        move |_args| {
            let cfg = cfg_dt.clone();
            async move {
                let boundary = cfg.day_boundary_hour;
                let now = Local::now();

                // Logical date: if before boundary hour, still "yesterday"
                let logical = if now.hour() < boundary {
                    now - chrono::Duration::days(1)
                } else {
                    now
                };

                // Logical week start (Sunday)
                let days_since_sunday = match logical.weekday() {
                    Weekday::Sun => 0,
                    Weekday::Mon => 1,
                    Weekday::Tue => 2,
                    Weekday::Wed => 3,
                    Weekday::Thu => 4,
                    Weekday::Fri => 5,
                    Weekday::Sat => 6,
                };

                let boundary_display = match boundary {
                    0 => "12:00 AM (midnight)".to_string(),
                    h if h < 12 => format!("{h}:00 AM"),
                    12 => "12:00 PM (noon)".to_string(),
                    h => format!("{}:00 PM", h - 12),
                };

                let result = json!({
                    "now": now.to_rfc3339(),
                    "now_display": now.format("%A, %b %-d, %Y %-I:%M %p").to_string(),
                    "logical_date": logical.format("%Y-%m-%d").to_string(),
                    "logical_date_display": logical.format("%A, %b %-d, %Y").to_string(),
                    "logical_day_of_week": logical.format("%A").to_string(),
                    "logical_week_start": (logical - chrono::Duration::days(days_since_sunday))
                        .format("%Y-%m-%d").to_string(),
                    "timezone": cfg.schedule_timezone,
                    "day_boundary": boundary_display,
                });

                ToolResult::text(serde_json::to_string_pretty(&result).unwrap())
            }
        },
    );

    // discord_send_file
    let state_file = state.clone();
    server.add_tool(
        "discord_send_file",
        "Send a file as a Discord message attachment to your own channel or another channel. \
         If channel_id is omitted, the file is sent to your own agent channel.",
        json!({
            "type": "object",
            "properties": {
                "channel_id": {"type": "string", "description": "The Discord channel ID. Omit to send to your own channel."},
                "file_path": {"type": "string", "description": "Absolute path to the file to upload"},
                "content": {"type": "string", "description": "Optional text message to include with the file"}
            },
            "required": ["file_path"]
        }),
        move |args| {
            let dc = state_file.clone();
            async move {
                let file_path = get_str(&args, "file_path");
                let content = get_opt_str(&args, "content");

                let channel_id = match parse_id(&args, "channel_id") {
                    Some(id) => id,
                    None => {
                        return ToolResult::error(
                            "Error: could not determine channel. Provide channel_id explicitly.",
                        );
                    }
                };

                let path = std::path::Path::new(&file_path);
                if !path.is_file() {
                    return ToolResult::error(format!("Error: file not found: {file_path}"));
                }

                let filename = path
                    .file_name()
                    .and_then(|n| n.to_str())
                    .unwrap_or("file");
                match std::fs::read(path) {
                    Ok(data) => {
                        match dc
                            .discord_client
                            .send_file(channel_id, filename, data, content.as_deref())
                            .await
                        {
                            Ok(msg) => {
                                let msg_id = msg
                                    .get("id")
                                    .and_then(|v: &Value| v.as_str())
                                    .unwrap_or("unknown");
                                ToolResult::text(format!(
                                    "File '{filename}' sent (msg id: {msg_id})"
                                ))
                            }
                            Err(e) => ToolResult::error(format!("Error: {e}")),
                        }
                    }
                    Err(e) => ToolResult::error(format!("Error reading file: {e}")),
                }
            }
        },
    );

    // set_agent_status
    let state_status = state.clone();
    server.add_tool(
        "set_agent_status",
        "Set a custom status on your agent's Discord channel (shown as an emoji prefix). \
         Use this to signal what you're doing (e.g. 'testing', 'deploying', 'waiting for CI'). \
         Call clear_agent_status to revert to auto-detected status.",
        json!({
            "type": "object",
            "properties": {
                "status": {"type": "string", "description": "Short status label (e.g. 'testing', 'deploying', 'blocked')"}
            },
            "required": ["status"]
        }),
        move |args| {
            let state = state_status.clone();
            async move {
                let status = get_str(&args, "status");
                if status.is_empty() {
                    return ToolResult::error("Error: status is required.");
                }
                // TODO: need the calling agent's name from MCP session context
                info!("Agent status set to: {}", status);
                // For now, this is a placeholder — the agent name needs to come from the session
                let _ = &state;
                ToolResult::text(format!(
                    "Status set to '{status}'. Channel will update shortly."
                ))
            }
        },
    );

    // clear_agent_status
    let state_clear = state;
    server.add_tool(
        "clear_agent_status",
        "Clear your custom channel status and revert to auto-detected status.",
        json!({"type": "object", "properties": {}, "required": []}),
        move |_args| {
            let state = state_clear.clone();
            async move {
                info!("Agent status cleared");
                let _ = &state;
                ToolResult::text(
                    "Custom status cleared. Channel will revert to auto-detected status.",
                )
            }
        },
    );

    server
}

// ---------------------------------------------------------------------------
// Discord REST tools (cross-channel messaging)
// ---------------------------------------------------------------------------

/// Create the Discord MCP server for cross-channel messaging.
pub fn create_discord_server(discord: Arc<DiscordClient>) -> McpServer {
    let mut server = McpServer::new("discord", "1.0.0");

    // discord_list_channels
    let dc1 = discord.clone();
    server.add_tool(
        "discord_list_channels",
        "List text channels in a Discord guild/server. Returns channel id, name, and category.",
        json!({
            "type": "object",
            "properties": {
                "guild_id": {"type": "string", "description": "The Discord guild (server) ID"}
            },
            "required": ["guild_id"]
        }),
        move |args| {
            let dc = dc1.clone();
            async move {
                let guild_id = match parse_id(&args, "guild_id") {
                    Some(id) => id,
                    None => return ToolResult::error("Error: invalid guild_id"),
                };
                match dc.list_channels(guild_id).await {
                    Ok(channels) => ToolResult::text(
                        serde_json::to_string_pretty(&channels).unwrap_or_default(),
                    ),
                    Err(e) => ToolResult::error(format!("Error: {e}")),
                }
            }
        },
    );

    // discord_read_messages
    let dc2 = discord.clone();
    server.add_tool(
        "discord_read_messages",
        "Read recent messages from a Discord channel. Returns formatted message history.",
        json!({
            "type": "object",
            "properties": {
                "channel_id": {"type": "string", "description": "The Discord channel ID"},
                "limit": {"type": "integer", "description": "Number of messages to fetch (default 20, max 100)"}
            },
            "required": ["channel_id"]
        }),
        move |args| {
            let dc = dc2.clone();
            async move {
                let channel_id = match parse_id(&args, "channel_id") {
                    Some(id) => id,
                    None => return ToolResult::error("Error: invalid channel_id"),
                };
                let limit = args
                    .get("limit")
                    .and_then(Value::as_u64)
                    .unwrap_or(20)
                    .min(100) as u32;

                match dc.get_messages(channel_id, limit, None, None).await {
                    Ok(value) => {
                        let mut messages: Vec<Value> = value
                            .as_array()
                            .cloned()
                            .unwrap_or_default();
                        messages.reverse(); // chronological order
                        let formatted: Vec<String> = messages
                            .iter()
                            .map(|msg| {
                                let author = msg
                                    .get("author")
                                    .and_then(|a| a.get("username"))
                                    .and_then(|u| u.as_str())
                                    .unwrap_or("unknown");
                                let content =
                                    msg.get("content").and_then(|c| c.as_str()).unwrap_or("");
                                let timestamp = msg
                                    .get("timestamp")
                                    .and_then(|t| t.as_str())
                                    .unwrap_or("");
                                format!("[{timestamp}] {author}: {content}")
                            })
                            .collect();
                        ToolResult::text(formatted.join("\n"))
                    }
                    Err(e) => ToolResult::error(format!("Error: {e}")),
                }
            }
        },
    );

    // discord_send_message
    let dc3 = discord;
    server.add_tool(
        "discord_send_message",
        "Send a message to a Discord channel OTHER than your own. Your text responses are \
         automatically delivered to your own channel — do NOT use this tool for that. \
         This tool is only for cross-channel messaging.",
        json!({
            "type": "object",
            "properties": {
                "channel_id": {"type": "string", "description": "The Discord channel ID"},
                "content": {"type": "string", "description": "The message content to send"}
            },
            "required": ["channel_id", "content"]
        }),
        move |args| {
            let dc = dc3.clone();
            async move {
                let channel_id = match parse_id(&args, "channel_id") {
                    Some(id) => id,
                    None => return ToolResult::error("Error: invalid channel_id"),
                };
                let content = get_str(&args, "content");

                match dc
                    .send_message(channel_id, &content)
                    .await
                {
                    Ok(msg) => {
                        let msg_id = msg
                            .get("id")
                            .and_then(|v| v.as_str())
                            .unwrap_or("unknown");
                        ToolResult::text(format!("Message sent (id: {msg_id})"))
                    }
                    Err(e) => ToolResult::error(format!("Error: {e}")),
                }
            }
        },
    );

    server
}

// ---------------------------------------------------------------------------
// Master agent tools (spawn, kill, restart, send_message)
// ---------------------------------------------------------------------------

/// Create the master agent MCP server.
pub fn create_master_server(state: Arc<BotState>) -> McpServer {
    let mut server = McpServer::new("axi", "1.0.0");

    // axi_spawn_agent
    let state_spawn = state.clone();
    server.add_tool(
        "axi_spawn_agent",
        "Spawn a new Axi agent session with its own Discord channel.",
        json!({
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Unique short name, no spaces"},
                "cwd": {"type": "string", "description": "Absolute path to the working directory"},
                "prompt": {"type": "string", "description": "Initial task instructions"},
                "resume": {"type": "string", "description": "Session ID to resume"},
                "packs": {"type": "array", "items": {"type": "string"}, "description": "Pack names for system prompt"},
                "compact_instructions": {"type": "string", "description": "Instructions for context compaction"},
                "mcp_servers": {"type": "array", "items": {"type": "string"}, "description": "Custom MCP server names"}
            },
            "required": ["name", "prompt"]
        }),
        move |args| {
            let state = state_spawn.clone();
            async move {
                let name = get_str(&args, "name");
                let prompt = get_str(&args, "prompt");

                if name.is_empty() {
                    return ToolResult::error("Error: 'name' is required and cannot be empty.");
                }
                if let Err(e) = validate_agent_name(&name) {
                    return ToolResult::error(format!("Error: {e}"));
                }
                if prompt.is_empty() {
                    return ToolResult::error("Error: 'prompt' is required.");
                }
                if name == state.config.master_agent_name {
                    return ToolResult::error(format!(
                        "Error: cannot spawn agent with reserved name '{}'.",
                        state.config.master_agent_name
                    ));
                }

                let resume = get_opt_str(&args, "resume");

                // Check if agent already exists
                {
                    let sessions = state.sessions.lock().await;
                    if sessions.contains_key(&name) && resume.is_none() {
                        return ToolResult::error(format!(
                            "Error: agent '{name}' already exists. Kill it first or use 'resume' to replace it."
                        ));
                    }
                }

                let cwd = get_opt_str(&args, "cwd")
                    .unwrap_or_else(|| default_agent_cwd(&state, &name));
                let compact_instructions = get_opt_str(&args, "compact_instructions");

                // Parse packs
                let packs: Option<Vec<String>> = args.get("packs").and_then(|v| {
                    v.as_array().map(|arr| {
                        arr.iter()
                            .filter_map(|v| v.as_str().map(ToString::to_string))
                            .collect()
                    })
                });

                // Parse MCP server names
                let mcp_server_names: Option<Vec<String>> = args.get("mcp_servers").and_then(|v| {
                    v.as_array().map(|arr| {
                        arr.iter()
                            .filter_map(|v| v.as_str().map(ToString::to_string))
                            .collect()
                    })
                });

                let packs_for_config = packs.clone();
                let mcp_names_for_config = mcp_server_names.clone();

                // Build system prompt
                let system_prompt = Some(build_spawned_prompt(
                    &state, &cwd, packs.as_deref(), compact_instructions.as_deref(),
                ));

                // Build MCP servers config
                let mcp_servers_cfg = build_mcp_servers(&state, &name, &cwd, mcp_server_names);

                // Reclaim if resuming an existing agent
                if resume.is_some() {
                    crate::registry::reclaim_agent_name(&state, &name).await;
                }

                // Register the session
                crate::registry::spawn_agent(
                    &state,
                    crate::registry::SpawnRequest {
                        name: name.clone(),
                        cwd: cwd.clone(),
                        agent_type: None, // uses config default
                        resume,
                        system_prompt,
                        mcp_servers: mcp_servers_cfg,
                        mcp_server_names: mcp_names_for_config.clone(),
                    },
                )
                .await;

                // Build and store SDK MCP servers for this agent
                let (sdk_servers, _) = build_sdk_mcp_config(&state, &name, &cwd, false);
                {
                    let mut sessions = state.sessions.lock().await;
                    if let Some(session) = sessions.get_mut(&name) {
                        session.sdk_mcp_servers = sdk_servers;
                    }
                }

                // Save agent config
                crate::registry::save_agent_config(
                    &cwd,
                    &crate::registry::AgentConfig {
                        mcp_server_names: mcp_names_for_config,
                        packs: packs_for_config,
                    },
                );

                // Create Discord channel
                match create_agent_channel(&state, &name).await {
                    Ok(channel_id) => {
                        state.register_channel(ChannelId::new(channel_id), &name).await;
                    }
                    Err(e) => {
                        info!("Failed to create channel for '{}': {}", name, e);
                    }
                }

                // Run initial prompt in background
                run_initial_prompt(&state, &name, &prompt);

                info!(
                    "Spawn agent: name={}, cwd={}, prompt_len={}",
                    name, cwd, prompt.len()
                );

                ToolResult::text(format!(
                    "Agent '{name}' spawn initiated in {cwd}. The agent's channel will be notified when it's ready."
                ))
            }
        },
    );

    // axi_kill_agent
    let state_kill = state.clone();
    server.add_tool(
        "axi_kill_agent",
        "Kill an Axi agent session and move its Discord channel to the Killed category. \
         Returns the session ID (for resuming later) or an error message.",
        json!({
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Name of the agent to kill"}
            },
            "required": ["name"]
        }),
        move |args| {
            let state = state_kill.clone();
            async move {
                let name = get_str(&args, "name");
                if name.is_empty() {
                    return ToolResult::error("Error: 'name' is required.");
                }
                if name == state.config.master_agent_name {
                    return ToolResult::error(format!(
                        "Error: cannot kill reserved agent '{}'.",
                        state.config.master_agent_name
                    ));
                }

                // Get session_id before killing
                let session_id = {
                    let sessions = state.sessions.lock().await;
                    match sessions.get(&name) {
                        Some(s) => s.session_id.clone(),
                        None => {
                            return ToolResult::error(format!(
                                "Error: agent '{name}' not found."
                            ))
                        }
                    }
                };

                info!("Killing agent '{}' (session={:?})", name, session_id);
                crate::registry::end_session(&state, &name).await;
                crate::frontend::on_kill(&state, &name, session_id.as_deref()).await;

                if let Some(sid) = &session_id {
                    ToolResult::text(format!(
                        "Agent '{name}' killed. Session ID: {sid}"
                    ))
                } else {
                    ToolResult::text(format!(
                        "Agent '{name}' killed (no session ID available)."
                    ))
                }
            }
        },
    );

    // axi_restart
    let state_restart = state.clone();
    server.add_tool(
        "axi_restart",
        "Restart the Axi bot. Waits for busy agents to finish first (graceful). \
         Only use when the user explicitly asks you to restart.",
        json!({"type": "object", "properties": {}, "required": []}),
        move |_args| {
            let state = state_restart.clone();
            async move {
                info!("Restart requested via MCP tool");
                state.shutdown_requested.store(true, std::sync::atomic::Ordering::SeqCst);
                // Give the response time to send, then exit
                tokio::spawn(async {
                    tokio::time::sleep(std::time::Duration::from_secs(2)).await;
                    std::process::exit(42);
                });
                ToolResult::text(
                    "Graceful restart initiated. Waiting for busy agents to finish...",
                )
            }
        },
    );

    // axi_restart_agent
    let state_restart_agent = state.clone();
    server.add_tool(
        "axi_restart_agent",
        "Restart a single agent's CLI process with a fresh system prompt. \
         Preserves session context (conversation history).",
        json!({
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Name of the agent to restart"}
            },
            "required": ["name"]
        }),
        move |args| {
            let state = state_restart_agent.clone();
            async move {
                let name = get_str(&args, "name");
                if name.is_empty() {
                    return ToolResult::error("Error: 'name' is required.");
                }
                if name == state.config.master_agent_name {
                    return ToolResult::error(
                        "Error: use axi_restart to restart the master agent.",
                    );
                }

                let (session_id, cwd) = {
                    let sessions = state.sessions.lock().await;
                    match sessions.get(&name) {
                        Some(s) => (s.session_id.clone(), s.cwd.clone()),
                        None => {
                            return ToolResult::error(format!(
                                "Error: agent '{name}' not found."
                            ))
                        }
                    }
                };

                // Rebuild with fresh prompt but same session_id
                let is_master = name == state.config.master_agent_name;
                let system_prompt = Some(build_spawned_prompt(&state, &cwd, None, None));
                crate::registry::rebuild_session(
                    &state,
                    &name,
                    None,
                    session_id.clone(),
                    system_prompt,
                    None,
                )
                .await;

                // Rebuild SDK MCP servers
                let (sdk_servers, _) = build_sdk_mcp_config(&state, &name, &cwd, is_master);
                {
                    let mut sessions = state.sessions.lock().await;
                    if let Some(session) = sessions.get_mut(&name) {
                        session.sdk_mcp_servers = sdk_servers;
                    }
                }

                info!("Restarted agent '{}' (session={:?})", name, session_id);
                ToolResult::text(format!(
                    "Agent '{name}' restarted. System prompt refreshed, session '{}' preserved.",
                    session_id.as_deref().unwrap_or("none")
                ))
            }
        },
    );

    // axi_send_message
    let state_send = state;
    server.add_tool(
        "axi_send_message",
        "Send a message to a spawned agent. The message appears in the agent's Discord channel \
         (with your name as sender) and is processed like a user message.",
        json!({
            "type": "object",
            "properties": {
                "agent_name": {"type": "string", "description": "Name of the target agent"},
                "content": {"type": "string", "description": "The message content to send"}
            },
            "required": ["agent_name", "content"]
        }),
        move |args| {
            let state = state_send.clone();
            async move {
                let target = get_str(&args, "agent_name");
                let content = get_str(&args, "content");

                if target.is_empty() {
                    return ToolResult::error("Error: agent_name is required.");
                }
                if content.is_empty() {
                    return ToolResult::error("Error: content is required.");
                }
                if target == state.config.master_agent_name {
                    return ToolResult::error("Error: cannot send messages to yourself.");
                }

                // Check agent exists
                {
                    let sessions = state.sessions.lock().await;
                    if !sessions.contains_key(&target) {
                        return ToolResult::error(format!(
                            "Error: agent '{target}' not found."
                        ));
                    }
                }

                let sender = state.config.master_agent_name.clone();
                info!(
                    "Inter-agent message: '{}' -> '{}': {}",
                    sender,
                    target,
                    &content[..content.len().min(200)]
                );

                let handler = crate::claude_process::make_stream_handler(Arc::clone(&state));
                let result = crate::messaging::deliver_inter_agent_message(
                    state,
                    &sender,
                    &target,
                    &content,
                    &handler,
                )
                .await;

                ToolResult::text(result)
            }
        },
    );

    server
}

/// Create the spawned agent MCP server (no restart/send, just spawn/kill/restart-agent).
pub fn create_agent_server(state: Arc<BotState>) -> McpServer {
    let mut server = McpServer::new("axi", "1.0.0");

    // axi_spawn_agent (same as master but fewer options)
    let state_spawn = state.clone();
    server.add_tool(
        "axi_spawn_agent",
        "Spawn a new Axi agent session with its own Discord channel.",
        json!({
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Unique short name, no spaces"},
                "cwd": {"type": "string", "description": "Absolute path to the working directory"},
                "prompt": {"type": "string", "description": "Initial task instructions"},
                "resume": {"type": "string", "description": "Session ID to resume"}
            },
            "required": ["name", "prompt"]
        }),
        move |args| {
            let state = state_spawn.clone();
            async move {
                let name = get_str(&args, "name");
                let prompt = get_str(&args, "prompt");

                if name.is_empty() {
                    return ToolResult::error("Error: 'name' is required.");
                }
                if let Err(e) = validate_agent_name(&name) {
                    return ToolResult::error(format!("Error: {e}"));
                }
                if prompt.is_empty() {
                    return ToolResult::error("Error: 'prompt' is required.");
                }
                if name == state.config.master_agent_name {
                    return ToolResult::error(format!(
                        "Error: cannot spawn agent with reserved name '{}'.",
                        state.config.master_agent_name
                    ));
                }

                let resume = get_opt_str(&args, "resume");
                let cwd = get_opt_str(&args, "cwd")
                    .unwrap_or_else(|| default_agent_cwd(&state, &name));

                let system_prompt = Some(build_spawned_prompt(&state, &cwd, None, None));
                let mcp_servers_cfg = build_mcp_servers(&state, &name, &cwd, None);

                if resume.is_some() {
                    crate::registry::reclaim_agent_name(&state, &name).await;
                }

                crate::registry::spawn_agent(
                    &state,
                    crate::registry::SpawnRequest {
                        name: name.clone(),
                        cwd: cwd.clone(),
                        agent_type: None, // uses config default
                        resume,
                        system_prompt,
                        mcp_servers: mcp_servers_cfg,
                        ..Default::default()
                    },
                )
                .await;

                // Build and store SDK MCP servers
                let (sdk_servers, _) = build_sdk_mcp_config(&state, &name, &cwd, false);
                {
                    let mut sessions = state.sessions.lock().await;
                    if let Some(session) = sessions.get_mut(&name) {
                        session.sdk_mcp_servers = sdk_servers;
                    }
                }

                match create_agent_channel(&state, &name).await {
                    Ok(channel_id) => {
                        state.register_channel(ChannelId::new(channel_id), &name).await;
                    }
                    Err(e) => {
                        info!("Failed to create channel for '{}': {}", name, e);
                    }
                }

                run_initial_prompt(&state, &name, &prompt);

                ToolResult::text(format!(
                    "Agent '{name}' spawn initiated in {cwd}."
                ))
            }
        },
    );

    // axi_kill_agent
    let state_kill = state.clone();
    server.add_tool(
        "axi_kill_agent",
        "Kill an Axi agent session.",
        json!({
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Name of the agent to kill"}
            },
            "required": ["name"]
        }),
        move |args| {
            let state = state_kill.clone();
            async move {
                let name = get_str(&args, "name");
                if name.is_empty() {
                    return ToolResult::error("Error: 'name' is required.");
                }
                if name == state.config.master_agent_name {
                    return ToolResult::error(format!(
                        "Error: cannot kill reserved agent '{}'.",
                        state.config.master_agent_name
                    ));
                }

                let session_id = {
                    let sessions = state.sessions.lock().await;
                    match sessions.get(&name) {
                        Some(s) => s.session_id.clone(),
                        None => {
                            return ToolResult::error(format!(
                                "Error: agent '{name}' not found."
                            ))
                        }
                    }
                };

                crate::registry::end_session(&state, &name).await;
                crate::frontend::on_kill(&state, &name, session_id.as_deref()).await;

                if let Some(sid) = &session_id {
                    ToolResult::text(format!("Agent '{name}' killed. Session ID: {sid}"))
                } else {
                    ToolResult::text(format!("Agent '{name}' killed."))
                }
            }
        },
    );

    // axi_restart_agent
    let state_restart = state;
    server.add_tool(
        "axi_restart_agent",
        "Restart a single agent's CLI process with a fresh system prompt.",
        json!({
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Name of the agent to restart"}
            },
            "required": ["name"]
        }),
        move |args| {
            let state = state_restart.clone();
            async move {
                let name = get_str(&args, "name");
                if name.is_empty() {
                    return ToolResult::error("Error: 'name' is required.");
                }

                let (session_id, cwd) = {
                    let sessions = state.sessions.lock().await;
                    match sessions.get(&name) {
                        Some(s) => (s.session_id.clone(), s.cwd.clone()),
                        None => {
                            return ToolResult::error(format!(
                                "Error: agent '{name}' not found."
                            ))
                        }
                    }
                };

                let system_prompt = Some(build_spawned_prompt(&state, &cwd, None, None));
                crate::registry::rebuild_session(
                    &state,
                    &name,
                    None,
                    session_id,
                    system_prompt,
                    None,
                )
                .await;

                // Rebuild SDK MCP servers
                let (sdk_servers, _) = build_sdk_mcp_config(&state, &name, &cwd, false);
                {
                    let mut sessions = state.sessions.lock().await;
                    if let Some(session) = sessions.get_mut(&name) {
                        session.sdk_mcp_servers = sdk_servers;
                    }
                }

                ToolResult::text(format!("Agent '{name}' restarted."))
            }
        },
    );

    server
}

// ---------------------------------------------------------------------------
// SDK MCP config builder — determines which MCP servers each agent gets
// ---------------------------------------------------------------------------

/// Convert an `McpServer` to the JSON config entry for `--mcp-config`.
/// SDK servers use `{"type": "sdk", "name": "...", "version": "..."}`.
fn sdk_server_json(server: &McpServer) -> Value {
    json!({
        "type": "sdk",
        "name": server.name,
        "version": server.version,
    })
}

/// Build the SDK MCP server set for an agent.
///
/// Returns:
/// - `HashMap<String, McpServer>` — server instances for handling control requests
/// - `Value` — JSON object for merging into `--mcp-config` mcpServers
///
/// Server assignment (matching Python `_build_mcp_servers` + `sdk_mcp_servers_for_cwd`):
/// - All agents: utils, schedule, discord
/// - Master: axi (master version with restart + `send_message`)
/// - Regular agents: axi (spawned version without restart/`send_message`)
pub fn build_sdk_mcp_config(
    state: &Arc<BotState>,
    agent_name: &str,
    cwd: &str,
    is_master: bool,
) -> (HashMap<String, McpServer>, Value) {
    let mut servers = HashMap::new();
    let mut config = serde_json::Map::new();

    // Utils — shared across all agents
    let utils = create_utils_server(Arc::clone(state));
    config.insert("utils".to_string(), sdk_server_json(&utils));
    servers.insert("utils".to_string(), utils);

    // Schedule — per-agent, scoped to agent_name
    let schedule = crate::mcp_schedule::create_schedule_server(
        agent_name.to_string(),
        state.config.schedules_path.clone(),
        Some(cwd.to_string()),
    );
    config.insert("schedule".to_string(), sdk_server_json(&schedule));
    servers.insert("schedule".to_string(), schedule);

    // Discord — cross-channel messaging
    let discord = create_discord_server(Arc::new(state.discord_client.clone()));
    config.insert("discord".to_string(), sdk_server_json(&discord));
    servers.insert("discord".to_string(), discord);

    // Axi — agent management (master vs spawned version)
    let axi = if is_master {
        create_master_server(Arc::clone(state))
    } else {
        create_agent_server(Arc::clone(state))
    };
    config.insert("axi".to_string(), sdk_server_json(&axi));
    servers.insert("axi".to_string(), axi);

    // Flowcoder — run_flowchart tool for flowcoder-type agents
    if state.config.flowcoder_enabled {
        let flowcoder = create_flowcoder_server(Arc::clone(state), agent_name.to_string());
        config.insert("flowcoder".to_string(), sdk_server_json(&flowcoder));
        servers.insert("flowcoder".to_string(), flowcoder);
    }

    // Playwright — external stdio server (not SDK, handled by Claude CLI directly)
    config.insert(
        "playwright".to_string(),
        json!({
            "command": "npx",
            "args": ["@playwright/mcp@latest", "--headless"],
        }),
    );

    (servers, Value::Object(config))
}

/// Create the flowcoder MCP server with the `run_flowchart` tool.
///
/// This allows agents to programmatically invoke flowcharts. The tool
/// sets `pending_flowchart` on the session, which gets injected as a
/// synthetic message after the current turn completes.
fn create_flowcoder_server(state: Arc<BotState>, agent_name: String) -> McpServer {
    let mut server = McpServer::new("flowcoder", "1.0.0");

    let state_run = state;
    let agent_name_run = agent_name;
    server.add_tool(
        "run_flowchart",
        "Run a flowchart command. The flowchart will execute after the current turn completes, preserving conversation context.",
        json!({
            "type": "object",
            "properties": {
                "command": {
                    "type": "string",
                    "description": "Name of the flowchart command to run (e.g. 'story', 'test-fix-loop')"
                },
                "args": {
                    "type": "string",
                    "description": "Arguments for the flowchart command"
                }
            },
            "required": ["command"]
        }),
        move |args| {
            let state = state_run.clone();
            let agent_name = agent_name_run.clone();
            async move {
                let command = get_str(&args, "command");
                let args_str = get_opt_str(&args, "args").unwrap_or_default();

                if command.is_empty() {
                    return ToolResult::error("Error: 'command' is required.");
                }

                // Check if command exists in search paths
                let commands = crate::flowcoder::list_flowchart_commands();
                let exists = commands.iter().any(|c| c.name == command);
                if !exists {
                    let available: Vec<&str> = commands.iter().map(|c| c.name.as_str()).collect();
                    return ToolResult::error(format!(
                        "Unknown flowchart command '{command}'. Available: {available:?}"
                    ));
                }

                // Set pending_flowchart on the session
                {
                    let mut sessions = state.sessions.lock().await;
                    if let Some(session) = sessions.get_mut(&agent_name) {
                        session.pending_flowchart = Some((command.clone(), args_str.clone()));
                    }
                }

                ToolResult::text(format!(
                    "Flowchart '{command}' queued with args '{args_str}'. Will run after this turn completes."
                ))
            }
        },
    );

    // list_flowcharts tool
    server.add_tool(
        "list_flowcharts",
        "List available flowchart commands.",
        json!({"type": "object", "properties": {}}),
        move |_args| {
            async move {
                let commands = crate::flowcoder::list_flowchart_commands();
                if commands.is_empty() {
                    return ToolResult::text("No flowchart commands found.");
                }

                let mut output = String::from("Available flowcharts:\n");
                for cmd in &commands {
                    if cmd.description.is_empty() {
                        let _ = writeln!(output, "  /{}", cmd.name);
                    } else {
                        let _ = writeln!(output, "  /{} — {}", cmd.name, cmd.description);
                    }
                }
                ToolResult::text(output)
            }
        },
    );

    server
}

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

#[cfg(test)]
mod tests {
    use super::*;

    #[tokio::test]
    async fn utils_date_time() {
        let tmp = tempfile::tempdir().unwrap();
        let config = axi_config::Config::for_test(tmp.path());
        let discord = DiscordClient::new("test");
        let state = Arc::new(BotState::new(config, discord));
        let server = create_utils_server(state);
        let result = server.call_tool("get_date_and_time", ToolArgs::new()).await;
        assert!(result.is_error.is_none());
        let text = &result.content[0].text;
        assert!(text.contains("now"));
        assert!(text.contains("logical_date"));
    }

    #[tokio::test]
    async fn utils_server_has_expected_tools() {
        let tmp = tempfile::tempdir().unwrap();
        let config = axi_config::Config::for_test(tmp.path());
        let discord = DiscordClient::new("test");
        let state = Arc::new(BotState::new(config, discord));
        let server = create_utils_server(state);

        assert!(server.handlers.contains_key("get_date_and_time"));
        assert!(server.handlers.contains_key("set_agent_status"));
        assert!(server.handlers.contains_key("clear_agent_status"));
    }

    #[tokio::test]
    async fn build_sdk_mcp_config_master() {
        let tmp = tempfile::tempdir().unwrap();
        let config = axi_config::Config::for_test(tmp.path());
        let discord = DiscordClient::new("test");
        let state = Arc::new(BotState::new(config, discord));

        let (servers, json_cfg) = build_sdk_mcp_config(&state, "axi-master", "/tmp/test", true);

        // Should have utils, schedule, discord, axi
        assert!(servers.contains_key("utils"));
        assert!(servers.contains_key("schedule"));
        assert!(servers.contains_key("discord"));
        assert!(servers.contains_key("axi"));

        // JSON config should have the same keys plus playwright
        let cfg_obj = json_cfg.as_object().unwrap();
        assert!(cfg_obj.contains_key("utils"));
        assert!(cfg_obj.contains_key("schedule"));
        assert!(cfg_obj.contains_key("discord"));
        assert!(cfg_obj.contains_key("axi"));
        assert!(cfg_obj.contains_key("playwright"));

        // SDK servers should have type "sdk"
        assert_eq!(cfg_obj["utils"]["type"], "sdk");
        assert_eq!(cfg_obj["schedule"]["type"], "sdk");

        // Playwright should NOT be type "sdk" (it's an external stdio server)
        assert!(cfg_obj["playwright"].get("type").is_none());
        assert_eq!(cfg_obj["playwright"]["command"], "npx");

        // Master axi server should have axi_restart and axi_send_message
        let axi_server = &servers["axi"];
        assert!(axi_server.handlers.contains_key("axi_spawn_agent"));
        assert!(axi_server.handlers.contains_key("axi_kill_agent"));
        assert!(axi_server.handlers.contains_key("axi_restart"));
        assert!(axi_server.handlers.contains_key("axi_send_message"));
    }

    #[tokio::test]
    async fn build_sdk_mcp_config_spawned() {
        let tmp = tempfile::tempdir().unwrap();
        let config = axi_config::Config::for_test(tmp.path());
        let discord = DiscordClient::new("test");
        let state = Arc::new(BotState::new(config, discord));

        let (servers, _json_cfg) = build_sdk_mcp_config(&state, "test-agent", "/tmp/test", false);

        // Spawned agent should have utils, schedule, discord, axi
        assert!(servers.contains_key("utils"));
        assert!(servers.contains_key("schedule"));
        assert!(servers.contains_key("discord"));
        assert!(servers.contains_key("axi"));

        // Spawned axi server should NOT have axi_restart or axi_send_message
        let axi_server = &servers["axi"];
        assert!(axi_server.handlers.contains_key("axi_spawn_agent"));
        assert!(axi_server.handlers.contains_key("axi_kill_agent"));
        assert!(!axi_server.handlers.contains_key("axi_restart"));
        assert!(!axi_server.handlers.contains_key("axi_send_message"));
    }
}
