//! Slash command registration and handlers.
//!
//! All Discord slash commands are defined and registered here. Each command
//! handler extracts bot state from serenity's TypeMap and delegates to
//! the appropriate hub/config module.

use std::sync::Arc;

use serenity::all::{
    CommandDataOptionValue, CommandInteraction, CommandOptionType, Context, CreateCommand,
    CreateCommandOption, CreateInteractionResponse, CreateInteractionResponseMessage,
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
    ];

    guild_id
        .set_commands(&ctx.http, commands)
        .await?;

    info!("Registered {} slash commands", 17);
    Ok(())
}

// ---------------------------------------------------------------------------
// Command dispatch
// ---------------------------------------------------------------------------

/// Dispatch a slash command interaction to its handler.
pub async fn handle_command(ctx: &Context, command: &CommandInteraction) {
    let data = ctx.data.read().await;
    let state = match data.get::<BotState>() {
        Some(s) => Arc::clone(s),
        None => {
            error!("BotState not found in TypeMap");
            return;
        }
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
        "restart" => handle_restart(ctx, command, &state).await,
        _ => {
            // Placeholder for commands that need hub integration
            let _ = command
                .create_response(
                    &ctx.http,
                    CreateInteractionResponse::Message(
                        CreateInteractionResponseMessage::new()
                            .content(format!("Command `/{name}` is not yet implemented in Rust."))
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
        .map(|s| s.to_string())
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
        "Pong! | Bot uptime: {}h {}m {}s",
        hours, minutes, seconds
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
            Some(e) => format!("*System:* {}", e),
        }
    } else {
        let current = axi_config::model::get_model(&state.config.config_path);
        format!("Current model: **{}**", current)
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

async fn handle_list_agents(ctx: &Context, command: &CommandInteraction, _state: &BotState) {
    // TODO: integrate with AgentHub sessions
    let msg = "*System:* No active agents (hub integration pending).";

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

    // TODO: trigger actual shutdown coordinator
    info!("Restart requested via /restart (force={})", force);
}
