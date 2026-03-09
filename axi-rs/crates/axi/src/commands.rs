//! Slash command registration and handlers.
//!
//! All Discord slash commands are defined and registered here. Each command
//! handler extracts bot state from serenity's `TypeMap` and delegates to
//! the appropriate hub/config module.

use std::sync::Arc;

use serenity::all::{
    ChannelId, CommandDataOptionValue, CommandInteraction, CommandOptionType, Context,
    CreateCommand, CreateCommandOption, CreateInteractionResponse,
    CreateInteractionResponseMessage,
};
use tracing::{error, info};

use crate::state::BotState;

// ---------------------------------------------------------------------------
// Command registration
// ---------------------------------------------------------------------------

/// Register all slash commands with Discord.
pub async fn register_commands(ctx: &Context) -> anyhow::Result<()> {
    let data = ctx.data.read().await;
    let state = data
        .get::<BotState>()
        .ok_or_else(|| anyhow::anyhow!("BotState not found"))?;
    let guild_id = serenity::all::GuildId::new(state.config.discord_guild_id);

    let commands = vec![
        CreateCommand::new("ping").description("Check bot latency and uptime."),
        CreateCommand::new("model")
            .description("Get or set the default LLM model for spawned agents.")
            .add_option(
                CreateCommandOption::new(
                    CommandOptionType::String,
                    "name",
                    "Model name (haiku, sonnet, opus) — omit to view current",
                )
                .required(false),
            ),
        CreateCommand::new("list-agents").description("List all active agent sessions."),
        CreateCommand::new("status")
            .description("Show what an agent is currently doing.")
            .add_option(
                CreateCommandOption::new(
                    CommandOptionType::String,
                    "agent_name",
                    "Agent name (omit to infer from channel)",
                )
                .required(false),
            ),
        CreateCommand::new("debug")
            .description("Toggle debug output (tool calls, thinking) for an agent.")
            .add_option(
                CreateCommandOption::new(
                    CommandOptionType::String,
                    "mode",
                    "on / off / omit to toggle",
                )
                .required(false),
            ),
        CreateCommand::new("kill-agent")
            .description("Terminate an agent session.")
            .add_option(
                CreateCommandOption::new(
                    CommandOptionType::String,
                    "agent_name",
                    "Agent name (omit to infer from channel)",
                )
                .required(false),
            ),
        CreateCommand::new("restart-agent")
            .description(
                "Restart an agent's CLI process with a fresh system prompt (preserves session context).",
            )
            .add_option(
                CreateCommandOption::new(
                    CommandOptionType::String,
                    "agent_name",
                    "Agent name (omit to infer from channel)",
                )
                .required(false),
            ),
        CreateCommand::new("stop")
            .description("Interrupt a running agent query (like Ctrl+C).")
            .add_option(
                CreateCommandOption::new(
                    CommandOptionType::String,
                    "agent_name",
                    "Agent name (omit to infer from channel)",
                )
                .required(false),
            ),
        CreateCommand::new("skip")
            .description("Interrupt the current query but keep processing queued messages.")
            .add_option(
                CreateCommandOption::new(
                    CommandOptionType::String,
                    "agent_name",
                    "Agent name (omit to infer from channel)",
                )
                .required(false),
            ),
        CreateCommand::new("plan")
            .description("Toggle plan mode — agent will plan before implementing.")
            .add_option(
                CreateCommandOption::new(
                    CommandOptionType::String,
                    "agent_name",
                    "Agent name (omit to infer from channel)",
                )
                .required(false),
            ),
        CreateCommand::new("reset-context")
            .description("Reset an agent's context.")
            .add_option(
                CreateCommandOption::new(
                    CommandOptionType::String,
                    "agent_name",
                    "Agent name (omit to infer from channel)",
                )
                .required(false),
            )
            .add_option(
                CreateCommandOption::new(
                    CommandOptionType::String,
                    "working_dir",
                    "New working directory (optional)",
                )
                .required(false),
            ),
        CreateCommand::new("compact")
            .description("Compact an agent's conversation context.")
            .add_option(
                CreateCommandOption::new(
                    CommandOptionType::String,
                    "agent_name",
                    "Agent name (omit to infer from channel)",
                )
                .required(false),
            ),
        CreateCommand::new("clear")
            .description("Clear an agent's conversation context.")
            .add_option(
                CreateCommandOption::new(
                    CommandOptionType::String,
                    "agent_name",
                    "Agent name (omit to infer from channel)",
                )
                .required(false),
            ),
        CreateCommand::new("restart")
            .description("Hot-reload bot.py (bridge stays alive, agents keep running).")
            .add_option(
                CreateCommandOption::new(
                    CommandOptionType::Boolean,
                    "force",
                    "Skip waiting for busy agents",
                )
                .required(false),
            ),
        CreateCommand::new("restart-including-bridge")
            .description("Full restart — kills bridge + all agents.")
            .add_option(
                CreateCommandOption::new(
                    CommandOptionType::Boolean,
                    "force",
                    "Skip waiting for busy agents",
                )
                .required(false),
            ),
        CreateCommand::new("claude-usage")
            .description("Show Claude API usage for current sessions and rate limit status.")
            .add_option(
                CreateCommandOption::new(
                    CommandOptionType::Integer,
                    "history",
                    "Number of recent rate limit events to show",
                )
                .required(false),
            ),
        CreateCommand::new("send")
            .description("Send a message to a spawned agent.")
            .add_option(
                CreateCommandOption::new(
                    CommandOptionType::String,
                    "agent_name",
                    "Target agent name",
                )
                .required(true),
            )
            .add_option(
                CreateCommandOption::new(
                    CommandOptionType::String,
                    "message",
                    "Message to send",
                )
                .required(true),
            ),
        CreateCommand::new("flowchart")
            .description("Run a flowchart command in the current agent's channel.")
            .add_option(
                CreateCommandOption::new(
                    CommandOptionType::String,
                    "name",
                    "Flowchart command name",
                )
                .required(true),
            )
            .add_option(
                CreateCommandOption::new(
                    CommandOptionType::String,
                    "args",
                    "Arguments for the flowchart command",
                )
                .required(false),
            ),
        CreateCommand::new("flowchart-list")
            .description("List available flowchart commands."),
    ];

    guild_id
        .set_commands(&ctx.http, commands)
        .await?;

    info!("Registered {} slash commands", 19);
    Ok(())
}

// ---------------------------------------------------------------------------
// Command dispatch
// ---------------------------------------------------------------------------

/// Dispatch a slash command interaction to its handler.
pub async fn handle_command(ctx: &Context, command: &CommandInteraction) {
    let data = ctx.data.read().await;
    let state = if let Some(s) = data.get::<BotState>() { Arc::clone(s) } else {
        error!("BotState not found in TypeMap");
        return;
    };
    drop(data);

    // Auth check
    if !state.config.allowed_user_ids.contains(&command.user.id.get()) {
        let _ = command
            .create_response(
                &ctx.http,
                CreateInteractionResponse::Message(
                    CreateInteractionResponseMessage::new()
                        .content("Not authorized.")
                        .ephemeral(true),
                ),
            )
            .await;
        return;
    }

    let name = command.data.name.as_str();
    info!("Slash command /{} from {}", name, command.user.name);

    match name {
        "ping" => handle_ping(ctx, command, &state).await,
        "model" => handle_model(ctx, command, &state).await,
        "list-agents" => handle_list_agents(ctx, command, &state).await,
        "status" => handle_status(ctx, command, &state).await,
        "kill-agent" => handle_kill_agent(ctx, command, &state).await,
        "stop" => handle_stop(ctx, command, &state).await,
        "skip" => handle_skip(ctx, command, &state).await,
        "restart" => handle_restart(ctx, command, &state).await,
        "restart-including-bridge" => handle_restart_bridge(ctx, command, &state).await,
        "reset-context" => handle_reset_context(ctx, command, &state).await,
        "send" => handle_send(ctx, command, &state).await,
        "debug" => handle_debug(ctx, command, &state).await,
        "plan" => handle_plan(ctx, command, &state).await,
        "compact" => handle_compact(ctx, command, &state).await,
        "clear" => handle_clear(ctx, command, &state).await,
        "claude-usage" => handle_claude_usage(ctx, command, &state).await,
        "restart-agent" => handle_restart_agent(ctx, command, &state).await,
        "flowchart" => handle_flowchart(ctx, command, &state).await,
        "flowchart-list" => handle_flowchart_list(ctx, command, &state).await,
        _ => {
            let _ = command
                .create_response(
                    &ctx.http,
                    CreateInteractionResponse::Message(
                        CreateInteractionResponseMessage::new()
                            .content(format!("Command `/{name}` is not yet wired."))
                            .ephemeral(true),
                    ),
                )
                .await;
        }
    }
}

// ---------------------------------------------------------------------------
// Individual command handlers
// ---------------------------------------------------------------------------

fn get_string_option(command: &CommandInteraction, name: &str) -> Option<String> {
    command
        .data
        .options
        .iter()
        .find(|o| o.name == name)
        .and_then(|o| o.value.as_str())
        .map(ToString::to_string)
}

fn get_bool_option(command: &CommandInteraction, name: &str) -> Option<bool> {
    command
        .data
        .options
        .iter()
        .find(|o| o.name == name)
        .and_then(|o| match &o.value {
            CommandDataOptionValue::Boolean(b) => Some(*b),
            _ => None,
        })
}

async fn handle_ping(ctx: &Context, command: &CommandInteraction, state: &BotState) {
    let start = state.start_time;
    let uptime_secs = start.elapsed().as_secs();
    let hours = uptime_secs / 3600;
    let minutes = (uptime_secs % 3600) / 60;
    let seconds = uptime_secs % 60;

    let msg = format!(
        "Pong! | Bot uptime: {hours}h {minutes}m {seconds}s"
    );

    let _ = command
        .create_response(
            &ctx.http,
            CreateInteractionResponse::Message(
                CreateInteractionResponseMessage::new().content(msg),
            ),
        )
        .await;
}

async fn handle_model(ctx: &Context, command: &CommandInteraction, state: &BotState) {
    let name = get_string_option(command, "name");

    let msg = if let Some(model_name) = name {
        let result = axi_config::model::set_model(&state.config.config_path, &model_name);
        match result {
            None => format!("*System:* Model set to **{}**.", model_name.to_lowercase()),
            Some(e) => format!("*System:* {e}"),
        }
    } else {
        let current = axi_config::model::get_model(&state.config.config_path);
        format!("Current model: **{current}**")
    };

    let _ = command
        .create_response(
            &ctx.http,
            CreateInteractionResponse::Message(
                CreateInteractionResponseMessage::new().content(msg),
            ),
        )
        .await;
}

/// Resolve agent name: explicit option > infer from channel.
async fn resolve_agent_name(
    command: &CommandInteraction,
    state: &BotState,
) -> Option<String> {
    if let Some(name) = get_string_option(command, "agent_name") {
        return Some(name);
    }
    // Infer from channel
    state
        .agent_for_channel(ChannelId::new(command.channel_id.get()))
        .await
}

async fn handle_list_agents(ctx: &Context, command: &CommandInteraction, state: &BotState) {
    let sessions = state.sessions.lock().await;

    if sessions.is_empty() {
        let _ = command
            .create_response(
                &ctx.http,
                CreateInteractionResponse::Message(
                    CreateInteractionResponseMessage::new()
                        .content("No active agents.")
                        .ephemeral(true),
                ),
            )
            .await;
        return;
    }

    let mut lines = Vec::new();
    for (name, session) in sessions.iter() {
        let status = if session.is_awake() {
            if crate::lifecycle::is_processing(session) {
                "working"
            } else {
                "awake"
            }
        } else {
            "sleeping"
        };
        let sid = session
            .session_id
            .as_deref()
            .map(|s| format!(" `{}`", &s[..8.min(s.len())]))
            .unwrap_or_default();
        lines.push(format!("- **{name}** — {status}{sid}"));
    }

    let msg = format!("**Active agents ({}):**\n{}", sessions.len(), lines.join("\n"));
    drop(sessions);

    let _ = command
        .create_response(
            &ctx.http,
            CreateInteractionResponse::Message(
                CreateInteractionResponseMessage::new()
                    .content(msg)
                    .ephemeral(true),
            ),
        )
        .await;
}

async fn handle_status(ctx: &Context, command: &CommandInteraction, state: &BotState) {
    let agent_name = if let Some(n) = resolve_agent_name(command, state).await { n } else {
        let _ = command
            .create_response(
                &ctx.http,
                CreateInteractionResponse::Message(
                    CreateInteractionResponseMessage::new()
                        .content("Could not determine agent. Specify a name or use in an agent channel.")
                        .ephemeral(true),
                ),
            )
            .await;
        return;
    };

    let sessions = state.sessions.lock().await;

    let msg = if let Some(session) = sessions.get(&agent_name) {
        let status = if session.is_awake() {
            if crate::lifecycle::is_processing(session) {
                "Working"
            } else {
                "Awake (idle)"
            }
        } else {
            "Sleeping"
        };
        let queued = session.message_queue.len();
        let sid = session
            .session_id
            .as_deref()
            .unwrap_or("none");
        format!(
            "**{}**: {} | session: `{}` | queued: {} | cwd: `{}`",
            agent_name, status, sid, queued, session.cwd
        )
    } else {
        format!("Agent '{agent_name}' not found.")
    };

    let _ = command
        .create_response(
            &ctx.http,
            CreateInteractionResponse::Message(
                CreateInteractionResponseMessage::new()
                    .content(msg)
                    .ephemeral(true),
            ),
        )
        .await;
}

async fn handle_kill_agent(ctx: &Context, command: &CommandInteraction, state: &BotState) {
    let agent_name = if let Some(n) = resolve_agent_name(command, state).await { n } else {
        let _ = command
            .create_response(
                &ctx.http,
                CreateInteractionResponse::Message(
                    CreateInteractionResponseMessage::new()
                        .content("Could not determine agent.")
                        .ephemeral(true),
                ),
            )
            .await;
        return;
    };

    if agent_name == state.config.master_agent_name {
        let _ = command
            .create_response(
                &ctx.http,
                CreateInteractionResponse::Message(
                    CreateInteractionResponseMessage::new()
                        .content("Cannot kill the master agent.")
                        .ephemeral(true),
                ),
            )
            .await;
        return;
    }

    // Get session ID before killing
    let session_id = {
        let sessions = state.sessions.lock().await;
        sessions.get(&agent_name).and_then(|s| s.session_id.clone())
    };

    crate::registry::end_session(state, &agent_name).await;
    crate::frontend::on_kill(state, &agent_name, session_id.as_deref()).await;

    let sid_text = session_id
        .as_deref()
        .map(|s| format!(" (session: `{s}`)"))
        .unwrap_or_default();

    let _ = command
        .create_response(
            &ctx.http,
            CreateInteractionResponse::Message(
                CreateInteractionResponseMessage::new()
                    .content(format!("Agent **{agent_name}** killed.{sid_text}")),
            ),
        )
        .await;
}

async fn handle_stop(ctx: &Context, command: &CommandInteraction, state: &BotState) {
    let agent_name = if let Some(n) = resolve_agent_name(command, state).await { n } else {
        let _ = command
            .create_response(
                &ctx.http,
                CreateInteractionResponse::Message(
                    CreateInteractionResponseMessage::new()
                        .content("Could not determine agent.")
                        .ephemeral(true),
                ),
            )
            .await;
        return;
    };

    crate::messaging::interrupt_session(state, &agent_name).await;

    let _ = command
        .create_response(
            &ctx.http,
            CreateInteractionResponse::Message(
                CreateInteractionResponseMessage::new()
                    .content(format!("Interrupted agent **{agent_name}**.")),
            ),
        )
        .await;
}

async fn handle_skip(ctx: &Context, command: &CommandInteraction, state: &BotState) {
    let agent_name = if let Some(n) = resolve_agent_name(command, state).await { n } else {
        let _ = command
            .create_response(
                &ctx.http,
                CreateInteractionResponse::Message(
                    CreateInteractionResponseMessage::new()
                        .content("Could not determine agent.")
                        .ephemeral(true),
                ),
            )
            .await;
        return;
    };

    crate::messaging::interrupt_session(state, &agent_name).await;

    // Skip doesn't sleep — the agent will process the next queued message
    let _ = command
        .create_response(
            &ctx.http,
            CreateInteractionResponse::Message(
                CreateInteractionResponseMessage::new()
                    .content(format!(
                        "Skipped current query for **{agent_name}** (will process queue)."
                    )),
            ),
        )
        .await;
}

async fn handle_reset_context(ctx: &Context, command: &CommandInteraction, state: &BotState) {
    let agent_name = if let Some(n) = resolve_agent_name(command, state).await { n } else {
        let _ = command
            .create_response(
                &ctx.http,
                CreateInteractionResponse::Message(
                    CreateInteractionResponseMessage::new()
                        .content("Could not determine agent.")
                        .ephemeral(true),
                ),
            )
            .await;
        return;
    };

    let cwd = get_string_option(command, "working_dir");
    crate::registry::reset_session(state, &agent_name, cwd.clone()).await;

    let cwd_msg = cwd
        .as_deref()
        .map(|c| format!(" (new cwd: `{c}`)"))
        .unwrap_or_default();

    let _ = command
        .create_response(
            &ctx.http,
            CreateInteractionResponse::Message(
                CreateInteractionResponseMessage::new()
                    .content(format!(
                        "Context reset for **{agent_name}**.{cwd_msg}"
                    )),
            ),
        )
        .await;
}

async fn handle_send(ctx: &Context, command: &CommandInteraction, state: &BotState) {
    let agent_name = if let Some(n) = get_string_option(command, "agent_name") { n } else {
        let _ = command
            .create_response(
                &ctx.http,
                CreateInteractionResponse::Message(
                    CreateInteractionResponseMessage::new()
                        .content("Agent name required.")
                        .ephemeral(true),
                ),
            )
            .await;
        return;
    };

    let message = if let Some(m) = get_string_option(command, "message") { m } else {
        let _ = command
            .create_response(
                &ctx.http,
                CreateInteractionResponse::Message(
                    CreateInteractionResponseMessage::new()
                        .content("Message required.")
                        .ephemeral(true),
                ),
            )
            .await;
        return;
    };

    // Check if agent exists
    let exists = {
        let sessions = state.sessions.lock().await;
        sessions.contains_key(&agent_name)
    };

    if !exists {
        let _ = command
            .create_response(
                &ctx.http,
                CreateInteractionResponse::Message(
                    CreateInteractionResponseMessage::new()
                        .content(format!("Agent '{agent_name}' not found."))
                        .ephemeral(true),
                ),
            )
            .await;
        return;
    }

    let content = crate::types::MessageContent::Text(message.clone());
    crate::lifecycle::wake_or_queue(state, &agent_name, content, None).await;

    let _ = command
        .create_response(
            &ctx.http,
            CreateInteractionResponse::Message(
                CreateInteractionResponseMessage::new()
                    .content(format!(
                        "Message sent to **{}**: {}",
                        agent_name,
                        if message.len() > 100 {
                            format!("{}...", &message[..100])
                        } else {
                            message
                        }
                    )),
            ),
        )
        .await;
}

async fn handle_restart(ctx: &Context, command: &CommandInteraction, _state: &BotState) {
    let force = get_bool_option(command, "force").unwrap_or(false);

    let msg = if force {
        "*System:* Force restarting (hot reload)..."
    } else {
        "*System:* Initiating graceful restart (hot reload)..."
    };

    let _ = command
        .create_response(
            &ctx.http,
            CreateInteractionResponse::Message(
                CreateInteractionResponseMessage::new().content(msg),
            ),
        )
        .await;

    // Exit with code 42 to signal supervisor for restart
    info!("Restart requested via /restart (force={})", force);
    std::process::exit(42);
}

async fn handle_restart_bridge(ctx: &Context, command: &CommandInteraction, _state: &BotState) {
    let force = get_bool_option(command, "force").unwrap_or(false);

    let _ = command
        .create_response(
            &ctx.http,
            CreateInteractionResponse::Message(
                CreateInteractionResponseMessage::new()
                    .content("*System:* Full restart (including bridge)..."),
            ),
        )
        .await;

    info!(
        "Full restart requested via /restart-including-bridge (force={})",
        force
    );
    // Exit with code 0 to signal supervisor for full restart
    std::process::exit(0);
}

async fn handle_debug(ctx: &Context, command: &CommandInteraction, state: &BotState) {
    let agent_name = if let Some(n) = resolve_agent_name(command, state).await {
        n
    } else {
        let _ = respond_ephemeral(ctx, command, "Could not determine agent.").await;
        return;
    };

    let mode = get_string_option(command, "mode");
    let mut sessions = state.sessions.lock().await;

    let msg = if let Some(session) = sessions.get_mut(&agent_name) {
        let new_debug = match mode.as_deref() {
            Some("on") => true,
            Some("off") => false,
            _ => !session.debug,
        };
        session.debug = new_debug;
        format!(
            "Debug mode for **{agent_name}**: **{}**",
            if new_debug { "ON" } else { "OFF" }
        )
    } else {
        format!("Agent '{agent_name}' not found.")
    };

    drop(sessions);
    let _ = respond(ctx, command, &msg).await;
}

async fn handle_plan(ctx: &Context, command: &CommandInteraction, state: &BotState) {
    let agent_name = if let Some(n) = resolve_agent_name(command, state).await {
        n
    } else {
        let _ = respond_ephemeral(ctx, command, "Could not determine agent.").await;
        return;
    };

    let mut sessions = state.sessions.lock().await;

    let msg = if let Some(session) = sessions.get_mut(&agent_name) {
        session.plan_mode = !session.plan_mode;
        format!(
            "Plan mode for **{agent_name}**: **{}**",
            if session.plan_mode { "ON" } else { "OFF" }
        )
    } else {
        format!("Agent '{agent_name}' not found.")
    };

    drop(sessions);
    let _ = respond(ctx, command, &msg).await;
}

async fn handle_compact(ctx: &Context, command: &CommandInteraction, state: &BotState) {
    let agent_name = if let Some(n) = resolve_agent_name(command, state).await {
        n
    } else {
        let _ = respond_ephemeral(ctx, command, "Could not determine agent.").await;
        return;
    };

    let content = crate::types::MessageContent::Text("/compact".to_string());
    crate::lifecycle::wake_or_queue(state, &agent_name, content, None).await;

    let _ = respond(ctx, command, &format!("Sent `/compact` to **{agent_name}**.")).await;
}

async fn handle_clear(ctx: &Context, command: &CommandInteraction, state: &BotState) {
    let agent_name = if let Some(n) = resolve_agent_name(command, state).await {
        n
    } else {
        let _ = respond_ephemeral(ctx, command, "Could not determine agent.").await;
        return;
    };

    let content = crate::types::MessageContent::Text("/clear".to_string());
    crate::lifecycle::wake_or_queue(state, &agent_name, content, None).await;

    let _ = respond(ctx, command, &format!("Sent `/clear` to **{agent_name}**.")).await;
}

async fn handle_claude_usage(ctx: &Context, command: &CommandInteraction, state: &BotState) {
    let history_count = command
        .data
        .options
        .iter()
        .find(|o| o.name == "history")
        .and_then(|o| match &o.value {
            CommandDataOptionValue::Integer(n) => Some(*n as usize),
            _ => None,
        })
        .unwrap_or(5);

    let tracker = state.rate_limits.lock().await;

    let mut lines = Vec::new();

    // Rate limit status
    if let Some(until) = tracker.rate_limited_until {
        let secs = (until - chrono::Utc::now()).num_seconds().max(0) as u64;
        let remaining = crate::rate_limits::format_time_remaining(secs);
        lines.push(format!("**Rate limited** — resets in {remaining}"));
    }

    // Quotas
    if !tracker.rate_limit_quotas.is_empty() {
        lines.push("**Quotas:**".to_string());
        for (key, quota) in &tracker.rate_limit_quotas {
            let util = quota
                .utilization
                .map(|u| format!(" ({:.0}%)", u * 100.0))
                .unwrap_or_default();
            lines.push(format!("- {key}: {}{util}", quota.status));
        }
    }

    // Session usage
    if !tracker.session_usage.is_empty() {
        lines.push(String::new());
        lines.push("**Session usage:**".to_string());
        for (name, usage) in &tracker.session_usage {
            lines.push(format!(
                "- **{name}**: {} queries, ${:.4}, {} turns",
                usage.queries, usage.total_cost_usd, usage.total_turns
            ));
        }
    }

    // Recent rate limit history
    if history_count > 0 {
        if let Some(ref path) = tracker.rate_limit_history_path {
            if let Ok(content) = std::fs::read_to_string(path) {
                let recent: Vec<&str> = content.lines().rev().take(history_count).collect();
                if !recent.is_empty() {
                    lines.push(String::new());
                    lines.push(format!("**Recent rate limits ({history_count}):**"));
                    for line in recent.into_iter().rev() {
                        lines.push(format!("```{line}```"));
                    }
                }
            }
        }
    }

    drop(tracker);

    let msg = if lines.is_empty() {
        "No usage data yet.".to_string()
    } else {
        lines.join("\n")
    };

    let _ = respond_ephemeral(ctx, command, &msg).await;
}

async fn handle_restart_agent(ctx: &Context, command: &CommandInteraction, state: &BotState) {
    let agent_name = if let Some(n) = resolve_agent_name(command, state).await {
        n
    } else {
        let _ = respond_ephemeral(ctx, command, "Could not determine agent.").await;
        return;
    };

    // Get session ID and build a fresh system prompt
    let (session_id, system_prompt) = {
        let sessions = state.sessions.lock().await;
        let session_id = sessions.get(&agent_name).and_then(|s| s.session_id.clone());
        let system_prompt = sessions.get(&agent_name).map(|session| {
            let pack_strs: Option<Vec<&str>> = session
                .mcp_server_names
                .as_ref()
                .map(|v| v.iter().map(String::as_str).collect());
            let preset = state.prompt_builder.spawned_agent_prompt(
                &session.cwd,
                pack_strs.as_deref(),
                session.compact_instructions.as_deref(),
            );
            serde_json::json!({
                "type": "custom_preset",
                "preset": preset.preset,
                "custom_instructions": preset.append,
            })
        });
        (session_id, system_prompt)
    };

    crate::registry::rebuild_session(
        state,
        &agent_name,
        None,
        session_id,
        system_prompt,
        None,
    )
    .await;

    let _ = respond(
        ctx,
        command,
        &format!("Restarted agent **{agent_name}** with fresh system prompt."),
    )
    .await;
}

async fn handle_flowchart(ctx: &Context, command: &CommandInteraction, state: &BotState) {
    let agent_name = if let Some(n) = resolve_agent_name(command, state).await {
        n
    } else {
        let _ = respond_ephemeral(ctx, command, "Could not determine agent.").await;
        return;
    };

    let is_busy = {
        let sessions = state.sessions.lock().await;
        if let Some(session) = sessions.get(&agent_name) {
            crate::lifecycle::is_processing(session)
        } else {
            let _ = respond_ephemeral(ctx, command, &format!("Agent '{agent_name}' not found.")).await;
            return;
        }
    };
    let channel_id = state.channel_for_agent(&agent_name).await;

    if is_busy {
        let _ = respond_ephemeral(
            ctx,
            command,
            &format!("Agent **{agent_name}** is busy."),
        )
        .await;
        return;
    }

    let fc_name = get_string_option(command, "name").unwrap_or_default();
    let fc_args = get_string_option(command, "args").unwrap_or_default();
    let fc_name = fc_name.trim_start_matches('/');
    let slash_content = if fc_args.is_empty() {
        format!("/{fc_name}")
    } else {
        format!("/{fc_name} {fc_args}")
    };

    let content = crate::types::MessageContent::Text(slash_content);
    crate::lifecycle::wake_or_queue(state, &agent_name, content, None).await;

    // Fire and forget — the message processor will handle the rest
    if let Some(ch) = channel_id {
        let dc = &state.discord_client;
        let _ = dc
            .send_message(
                ch.get(),
                &format!("*System:* Flowchart `{fc_name}` started on **{agent_name}**."),
            )
            .await;
    }

    let _ = respond(
        ctx,
        command,
        &format!("Flowchart `{fc_name}` dispatched to **{agent_name}**."),
    )
    .await;
}

async fn handle_flowchart_list(ctx: &Context, command: &CommandInteraction, _state: &BotState) {
    let commands = crate::flowcoder::list_flowchart_commands();

    if commands.is_empty() {
        let _ = respond_ephemeral(ctx, command, "No flowchart commands found.").await;
        return;
    }

    let mut lines = Vec::new();
    for cmd in &commands {
        let desc = if cmd.description.is_empty() {
            String::new()
        } else {
            format!(" — {}", cmd.description)
        };
        lines.push(format!("- `{}`{desc}", cmd.name));
    }

    let msg = format!(
        "*System:* **Available flowcharts** ({}):\n{}",
        commands.len(),
        lines.join("\n")
    );

    let _ = respond_ephemeral(ctx, command, &msg).await;
}

// ---------------------------------------------------------------------------
// Response helpers
// ---------------------------------------------------------------------------

async fn respond(
    ctx: &Context,
    command: &CommandInteraction,
    msg: &str,
) -> serenity::Result<()> {
    command
        .create_response(
            &ctx.http,
            CreateInteractionResponse::Message(
                CreateInteractionResponseMessage::new().content(msg),
            ),
        )
        .await
}

async fn respond_ephemeral(
    ctx: &Context,
    command: &CommandInteraction,
    msg: &str,
) -> serenity::Result<()> {
    command
        .create_response(
            &ctx.http,
            CreateInteractionResponse::Message(
                CreateInteractionResponseMessage::new()
                    .content(msg)
                    .ephemeral(true),
            ),
        )
        .await
}
