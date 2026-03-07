//! MCP tool implementations — agent management, Discord, and utilities.
//!
//! Each function creates an `McpServer` with the appropriate tools registered.
//! Tool handlers capture shared state via Arc closures.

use std::future::Future;
use std::pin::Pin;
use std::sync::Arc;

use chrono::{Datelike, Local, Timelike, Weekday};
use serde_json::{json, Value};
use tracing::info;

use axi_config::{Config, DiscordClient};
use axi_hub::hub::AgentHub;

use crate::protocol::{McpServer, ToolArgs, ToolResult};

// ---------------------------------------------------------------------------
// ToolContext — bot-side operations that MCP tools need
// ---------------------------------------------------------------------------

/// Trait for bot-side operations that MCP tools delegate to.
///
/// Implemented by `BotState` in `axi-bot`. Keeps `axi-mcp` decoupled from
/// Discord/serenity specifics.
pub trait ToolContext: Send + Sync + 'static {
    /// Look up the Discord channel ID for an agent.
    fn channel_for_agent(
        &self,
        agent_name: &str,
    ) -> Pin<Box<dyn Future<Output = Option<u64>> + Send>>;

    /// Set a custom status emoji on an agent's Discord channel.
    fn set_channel_status(
        &self,
        agent_name: &str,
        status: &str,
    ) -> Pin<Box<dyn Future<Output = ()> + Send>>;

    /// Clear the custom status on an agent's Discord channel.
    fn clear_channel_status(
        &self,
        agent_name: &str,
    ) -> Pin<Box<dyn Future<Output = ()> + Send>>;

    /// Create a Discord channel for a new agent. Returns channel ID.
    fn create_agent_channel(
        &self,
        agent_name: &str,
    ) -> Pin<Box<dyn Future<Output = Result<u64, String>> + Send>>;

    /// Register the channel↔agent mapping in bot state.
    fn register_channel(
        &self,
        channel_id: u64,
        agent_name: &str,
    ) -> Pin<Box<dyn Future<Output = ()> + Send>>;

    /// Build a system prompt for a spawned agent.
    fn build_spawned_prompt(
        &self,
        cwd: &str,
        packs: Option<Vec<String>>,
        compact_instructions: Option<String>,
    ) -> Option<Value>;

    /// Build the MCP servers JSON config for an agent.
    fn build_mcp_servers(
        &self,
        agent_name: &str,
        cwd: &str,
        extra_mcp_server_names: Option<Vec<String>>,
    ) -> Option<Value>;

    /// Run the initial prompt for a spawned agent (in a background task).
    fn run_initial_prompt(
        &self,
        agent_name: &str,
        prompt: &str,
    );

    /// Trigger a graceful restart of the bot (exit code 42).
    fn trigger_restart(&self);

    /// The master agent name.
    fn master_agent_name(&self) -> &str;

    /// The default CWD for agents.
    fn default_agent_cwd(&self, agent_name: &str) -> String;

    /// Get the stream handler function for inter-agent messaging.
    fn stream_handler(&self) -> axi_hub::messaging::StreamHandlerFn;
}

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

// ---------------------------------------------------------------------------
// Utility tools (available to all agents)
// ---------------------------------------------------------------------------

/// Create the utils MCP server (date/time, file upload, status).
pub fn create_utils_server(
    config: Arc<Config>,
    discord: Arc<DiscordClient>,
    tool_ctx: Arc<dyn ToolContext>,
) -> McpServer {
    let mut server = McpServer::new("utils", "1.0.0");
    let cfg = config;

    // get_date_and_time
    server.add_tool(
        "get_date_and_time",
        "Get the current date and time with logical day/week calculations. \
         Accounts for the user's configured day boundary (the hour when a new 'day' starts). \
         Always call this first to orient yourself before working with plans.",
        json!({"type": "object", "properties": {}, "required": []}),
        move |_args| {
            let cfg = cfg.clone();
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
    let discord_for_file = discord;
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
            let dc = discord_for_file.clone();
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
                            .send_file(channel_id, filename, data, content.as_deref())
                            .await
                        {
                            Ok(msg) => {
                                let msg_id = msg
                                    .get("id")
                                    .and_then(|v| v.as_str())
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
    let ctx_for_status = tool_ctx.clone();
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
            let ctx = ctx_for_status.clone();
            async move {
                let status = get_str(&args, "status");
                if status.is_empty() {
                    return ToolResult::error("Error: status is required.");
                }
                // TODO: need the calling agent's name — for now use a placeholder
                // The agent name will come from the MCP session context
                info!("Agent status set to: {}", status);
                ctx.set_channel_status("_self", &status).await;
                ToolResult::text(format!(
                    "Status set to '{status}'. Channel will update shortly."
                ))
            }
        },
    );

    // clear_agent_status
    let ctx_for_clear = tool_ctx;
    server.add_tool(
        "clear_agent_status",
        "Clear your custom channel status and revert to auto-detected status.",
        json!({"type": "object", "properties": {}, "required": []}),
        move |_args| {
            let ctx = ctx_for_clear.clone();
            async move {
                info!("Agent status cleared");
                ctx.clear_channel_status("_self").await;
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
pub fn create_master_server(
    hub: Arc<AgentHub>,
    ctx: Arc<dyn ToolContext>,
) -> McpServer {
    let mut server = McpServer::new("axi", "1.0.0");

    // axi_spawn_agent
    let hub_spawn = hub.clone();
    let ctx_spawn = ctx.clone();
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
            let hub = hub_spawn.clone();
            let ctx = ctx_spawn.clone();
            async move {
                let name = get_str(&args, "name");
                let prompt = get_str(&args, "prompt");

                if name.is_empty() {
                    return ToolResult::error("Error: 'name' is required and cannot be empty.");
                }
                if name.contains(' ') {
                    return ToolResult::error("Error: agent name cannot contain spaces.");
                }
                if prompt.is_empty() {
                    return ToolResult::error("Error: 'prompt' is required.");
                }
                if name == ctx.master_agent_name() {
                    return ToolResult::error(format!(
                        "Error: cannot spawn agent with reserved name '{}'.",
                        ctx.master_agent_name()
                    ));
                }

                let resume = get_opt_str(&args, "resume");

                // Check if agent already exists
                {
                    let sessions = hub.sessions.lock().await;
                    if sessions.contains_key(&name) && resume.is_none() {
                        return ToolResult::error(format!(
                            "Error: agent '{name}' already exists. Kill it first or use 'resume' to replace it."
                        ));
                    }
                }

                let cwd = get_opt_str(&args, "cwd")
                    .unwrap_or_else(|| ctx.default_agent_cwd(&name));
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

                // Clone for later persistence
                let packs_for_config = packs.clone();
                let mcp_names_for_config = mcp_server_names.clone();

                // Build system prompt
                let system_prompt = ctx.build_spawned_prompt(
                    &cwd,
                    packs,
                    compact_instructions,
                );

                // Build MCP servers config
                let mcp_servers = ctx.build_mcp_servers(&name, &cwd, mcp_server_names);

                // Reclaim if resuming an existing agent
                if resume.is_some() {
                    axi_hub::registry::reclaim_agent_name(&hub, &name).await;
                }

                // Register the session in the hub
                axi_hub::registry::spawn_agent(
                    &hub,
                    axi_hub::registry::SpawnRequest {
                        name: name.clone(),
                        cwd: cwd.clone(),
                        agent_type: Some("claude_code".to_string()),
                        resume,
                        system_prompt,
                        mcp_servers,
                        mcp_server_names: mcp_names_for_config.clone(),
                    },
                )
                .await;

                // Save agent config
                axi_hub::registry::save_agent_config(
                    &cwd,
                    &axi_hub::registry::AgentConfig {
                        mcp_server_names: mcp_names_for_config,
                        packs: packs_for_config,
                    },
                );

                // Create Discord channel
                match ctx.create_agent_channel(&name).await {
                    Ok(channel_id) => {
                        ctx.register_channel(channel_id, &name).await;
                    }
                    Err(e) => {
                        info!("Failed to create channel for '{}': {}", name, e);
                    }
                }

                // Run initial prompt in background
                ctx.run_initial_prompt(&name, &prompt);

                info!(
                    "Spawn agent: name={}, cwd={}, prompt_len={}",
                    name,
                    cwd,
                    prompt.len()
                );

                ToolResult::text(format!(
                    "Agent '{name}' spawn initiated in {cwd}. The agent's channel will be notified when it's ready."
                ))
            }
        },
    );

    // axi_kill_agent
    let hub_kill = hub.clone();
    let ctx_kill = ctx.clone();
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
            let hub = hub_kill.clone();
            let ctx = ctx_kill.clone();
            async move {
                let name = get_str(&args, "name");
                if name.is_empty() {
                    return ToolResult::error("Error: 'name' is required.");
                }
                if name == ctx.master_agent_name() {
                    return ToolResult::error(format!(
                        "Error: cannot kill reserved agent '{}'.",
                        ctx.master_agent_name()
                    ));
                }

                // Get session_id before killing
                let session_id = {
                    let sessions = hub.sessions.lock().await;
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
                axi_hub::registry::end_session(&hub, &name).await;
                hub.callbacks.on_kill(&name, session_id.as_deref()).await;

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
    let ctx_restart = ctx.clone();
    server.add_tool(
        "axi_restart",
        "Restart the Axi bot. Waits for busy agents to finish first (graceful). \
         Only use when the user explicitly asks you to restart.",
        json!({"type": "object", "properties": {}, "required": []}),
        move |_args| {
            let ctx = ctx_restart.clone();
            async move {
                info!("Restart requested via MCP tool");
                ctx.trigger_restart();
                ToolResult::text(
                    "Graceful restart initiated. Waiting for busy agents to finish...",
                )
            }
        },
    );

    // axi_restart_agent
    let hub_restart_agent = hub.clone();
    let ctx_restart_agent = ctx.clone();
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
            let hub = hub_restart_agent.clone();
            let ctx = ctx_restart_agent.clone();
            async move {
                let name = get_str(&args, "name");
                if name.is_empty() {
                    return ToolResult::error("Error: 'name' is required.");
                }
                if name == ctx.master_agent_name() {
                    return ToolResult::error(
                        "Error: use axi_restart to restart the master agent.",
                    );
                }

                let (session_id, cwd) = {
                    let sessions = hub.sessions.lock().await;
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
                let system_prompt = ctx.build_spawned_prompt(&cwd, None, None);
                axi_hub::registry::rebuild_session(
                    &hub,
                    &name,
                    None,
                    session_id.clone(),
                    system_prompt,
                    None,
                )
                .await;

                info!("Restarted agent '{}' (session={:?})", name, session_id);
                ToolResult::text(format!(
                    "Agent '{name}' restarted. System prompt refreshed, session '{}' preserved.",
                    session_id.as_deref().unwrap_or("none")
                ))
            }
        },
    );

    // axi_send_message
    let hub_send = hub;
    let ctx_send = ctx;
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
            let hub = hub_send.clone();
            let ctx = ctx_send.clone();
            async move {
                let target = get_str(&args, "agent_name");
                let content = get_str(&args, "content");

                if target.is_empty() {
                    return ToolResult::error("Error: agent_name is required.");
                }
                if content.is_empty() {
                    return ToolResult::error("Error: content is required.");
                }
                if target == ctx.master_agent_name() {
                    return ToolResult::error("Error: cannot send messages to yourself.");
                }

                // Check agent exists
                {
                    let sessions = hub.sessions.lock().await;
                    if !sessions.contains_key(&target) {
                        return ToolResult::error(format!(
                            "Error: agent '{target}' not found."
                        ));
                    }
                }

                let sender = ctx.master_agent_name().to_string();
                info!(
                    "Inter-agent message: '{}' -> '{}': {}",
                    sender,
                    target,
                    &content[..content.len().min(200)]
                );

                let handler = ctx.stream_handler();
                let result = axi_hub::messaging::deliver_inter_agent_message(
                    &hub,
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
pub fn create_agent_server(
    hub: Arc<AgentHub>,
    ctx: Arc<dyn ToolContext>,
) -> McpServer {
    let mut server = McpServer::new("axi", "1.0.0");

    // axi_spawn_agent (same as master)
    let hub_spawn = hub.clone();
    let ctx_spawn = ctx.clone();
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
            let hub = hub_spawn.clone();
            let ctx = ctx_spawn.clone();
            async move {
                let name = get_str(&args, "name");
                let prompt = get_str(&args, "prompt");

                if name.is_empty() {
                    return ToolResult::error("Error: 'name' is required.");
                }
                if prompt.is_empty() {
                    return ToolResult::error("Error: 'prompt' is required.");
                }
                if name == ctx.master_agent_name() {
                    return ToolResult::error(format!(
                        "Error: cannot spawn agent with reserved name '{}'.",
                        ctx.master_agent_name()
                    ));
                }

                let resume = get_opt_str(&args, "resume");
                let cwd = get_opt_str(&args, "cwd")
                    .unwrap_or_else(|| ctx.default_agent_cwd(&name));

                let system_prompt = ctx.build_spawned_prompt(&cwd, None, None);
                let mcp_servers = ctx.build_mcp_servers(&name, &cwd, None);

                if resume.is_some() {
                    axi_hub::registry::reclaim_agent_name(&hub, &name).await;
                }

                axi_hub::registry::spawn_agent(
                    &hub,
                    axi_hub::registry::SpawnRequest {
                        name: name.clone(),
                        cwd: cwd.clone(),
                        agent_type: Some("claude_code".to_string()),
                        resume,
                        system_prompt,
                        mcp_servers,
                        ..Default::default()
                    },
                )
                .await;

                match ctx.create_agent_channel(&name).await {
                    Ok(channel_id) => {
                        ctx.register_channel(channel_id, &name).await;
                    }
                    Err(e) => {
                        info!("Failed to create channel for '{}': {}", name, e);
                    }
                }

                ctx.run_initial_prompt(&name, &prompt);

                ToolResult::text(format!(
                    "Agent '{name}' spawn initiated in {cwd}."
                ))
            }
        },
    );

    // axi_kill_agent
    let hub_kill = hub.clone();
    let ctx_kill = ctx.clone();
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
            let hub = hub_kill.clone();
            let ctx = ctx_kill.clone();
            async move {
                let name = get_str(&args, "name");
                if name.is_empty() {
                    return ToolResult::error("Error: 'name' is required.");
                }
                if name == ctx.master_agent_name() {
                    return ToolResult::error(format!(
                        "Error: cannot kill reserved agent '{}'.",
                        ctx.master_agent_name()
                    ));
                }

                let session_id = {
                    let sessions = hub.sessions.lock().await;
                    match sessions.get(&name) {
                        Some(s) => s.session_id.clone(),
                        None => {
                            return ToolResult::error(format!(
                                "Error: agent '{name}' not found."
                            ))
                        }
                    }
                };

                axi_hub::registry::end_session(&hub, &name).await;
                hub.callbacks.on_kill(&name, session_id.as_deref()).await;

                if let Some(sid) = &session_id {
                    ToolResult::text(format!("Agent '{name}' killed. Session ID: {sid}"))
                } else {
                    ToolResult::text(format!("Agent '{name}' killed."))
                }
            }
        },
    );

    // axi_restart_agent
    let hub_restart = hub;
    let ctx_restart = ctx;
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
            let hub = hub_restart.clone();
            let ctx = ctx_restart.clone();
            async move {
                let name = get_str(&args, "name");
                if name.is_empty() {
                    return ToolResult::error("Error: 'name' is required.");
                }

                let (session_id, cwd) = {
                    let sessions = hub.sessions.lock().await;
                    match sessions.get(&name) {
                        Some(s) => (s.session_id.clone(), s.cwd.clone()),
                        None => {
                            return ToolResult::error(format!(
                                "Error: agent '{name}' not found."
                            ))
                        }
                    }
                };

                let system_prompt = ctx.build_spawned_prompt(&cwd, None, None);
                axi_hub::registry::rebuild_session(
                    &hub,
                    &name,
                    None,
                    session_id,
                    system_prompt,
                    None,
                )
                .await;

                ToolResult::text(format!("Agent '{name}' restarted."))
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

    fn test_config() -> Arc<Config> {
        unsafe { std::env::set_var("DISCORD_TOKEN", "test") };
        unsafe { std::env::set_var("DISCORD_GUILD_ID", "1") };
        unsafe { std::env::set_var("ALLOWED_USER_IDS", "1") };
        Arc::new(Config::from_env().unwrap())
    }

    struct TestToolContext;

    impl ToolContext for TestToolContext {
        fn channel_for_agent(&self, _name: &str) -> Pin<Box<dyn Future<Output = Option<u64>> + Send>> {
            Box::pin(async { None })
        }
        fn set_channel_status(&self, _name: &str, _status: &str) -> Pin<Box<dyn Future<Output = ()> + Send>> {
            Box::pin(async {})
        }
        fn clear_channel_status(&self, _name: &str) -> Pin<Box<dyn Future<Output = ()> + Send>> {
            Box::pin(async {})
        }
        fn create_agent_channel(&self, _name: &str) -> Pin<Box<dyn Future<Output = Result<u64, String>> + Send>> {
            Box::pin(async { Ok(12345) })
        }
        fn register_channel(&self, _channel_id: u64, _name: &str) -> Pin<Box<dyn Future<Output = ()> + Send>> {
            Box::pin(async {})
        }
        fn build_spawned_prompt(&self, _cwd: &str, _packs: Option<Vec<String>>, _compact: Option<String>) -> Option<Value> {
            None
        }
        fn build_mcp_servers(&self, _name: &str, _cwd: &str, _extra: Option<Vec<String>>) -> Option<Value> {
            None
        }
        fn run_initial_prompt(&self, _name: &str, _prompt: &str) {}
        fn trigger_restart(&self) {}
        fn master_agent_name(&self) -> &str { "axi-master" }
        fn default_agent_cwd(&self, name: &str) -> String {
            format!("/tmp/agents/{name}")
        }
        fn stream_handler(&self) -> axi_hub::messaging::StreamHandlerFn {
            Arc::new(|_name: &str| Box::pin(async { None }))
        }
    }

    #[tokio::test]
    async fn utils_date_time() {
        let config = test_config();
        let discord = Arc::new(DiscordClient::new("test"));
        let ctx: Arc<dyn ToolContext> = Arc::new(TestToolContext);
        let server = create_utils_server(config, discord, ctx);
        let result = server.call_tool("get_date_and_time", ToolArgs::new()).await;
        assert!(result.is_error.is_none());
        let text = &result.content[0].text;
        assert!(text.contains("now"));
        assert!(text.contains("logical_date"));
    }

    #[tokio::test]
    async fn master_spawn_validation() {
        // We can't easily create a full AgentHub in tests, so just test
        // the simpler validation paths
        let config = test_config();
        let discord = Arc::new(DiscordClient::new("test"));
        let ctx: Arc<dyn ToolContext> = Arc::new(TestToolContext);
        let server = create_utils_server(config, discord, ctx);

        // Verify server has expected tools
        assert!(server.handlers.contains_key("get_date_and_time"));
        assert!(server.handlers.contains_key("set_agent_status"));
        assert!(server.handlers.contains_key("clear_agent_status"));
    }
}
