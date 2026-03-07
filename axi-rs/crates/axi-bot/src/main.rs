//! Axi Discord Bot — event handlers, slash commands, and agent orchestration.
//!
//! Thin layer wiring Discord events to the `AgentHub`. All agent state and
//! operations live in axi-hub; Discord-specific rendering lives here.

// Many items are written for runtime use but not yet wired — suppress until fully integrated.
#![allow(dead_code)]

mod bridge;
mod channels;
mod commands;
mod crash_handler;
mod events;
mod frontend;
mod permissions;
mod prompts;
mod scheduler;
mod startup;
mod state;
mod streaming;
mod todos;

use std::sync::Arc;

use serenity::all::GatewayIntents;
use serenity::client::{Client, Context, EventHandler};
use serenity::model::gateway::Ready;
use tracing::{error, info};
use tracing_subscriber::EnvFilter;

use state::BotState;

struct Handler;

#[serenity::async_trait]
impl EventHandler for Handler {
    async fn ready(&self, ctx: Context, ready: Ready) {
        info!(
            "Bot connected as {} (id={})",
            ready.user.name, ready.user.id
        );

        // Register slash commands
        if let Err(e) = commands::register_commands(&ctx).await {
            error!("Failed to register slash commands: {}", e);
        }

        // Full startup: hub init, channel reconstruction, master agent, scheduler
        let data = ctx.data.read().await;
        if let Some(state) = data.get::<BotState>() {
            let state = Arc::clone(state);
            drop(data);
            startup::initialize(&ctx, state).await;
        }
    }

    async fn message(&self, ctx: Context, msg: serenity::model::channel::Message) {
        events::handle_message(&ctx, &msg).await;
    }

    async fn interaction_create(&self, ctx: Context, interaction: serenity::model::application::Interaction) {
        events::handle_interaction(&ctx, interaction).await;
    }

    async fn reaction_add(&self, ctx: Context, reaction: serenity::model::channel::Reaction) {
        events::handle_reaction_add(&ctx, &reaction).await;
    }
}

#[tokio::main]
async fn main() -> anyhow::Result<()> {
    // Load .env
    dotenvy::dotenv().ok();

    // Initialize tracing
    let log_level = std::env::var("LOG_LEVEL").unwrap_or_else(|_| "info".to_string());
    tracing_subscriber::fmt()
        .with_env_filter(
            EnvFilter::try_new(&log_level).unwrap_or_else(|_| EnvFilter::new("info")),
        )
        .with_target(false)
        .init();

    // Load config
    let config = axi_config::Config::from_env()?;

    info!("Starting Axi bot...");

    // Build serenity client
    let intents = GatewayIntents::GUILDS
        | GatewayIntents::GUILD_MESSAGES
        | GatewayIntents::GUILD_MESSAGE_REACTIONS
        | GatewayIntents::MESSAGE_CONTENT
        | GatewayIntents::DIRECT_MESSAGES;

    let discord_client =
        axi_config::DiscordClient::new(&config.discord_token);

    let bot_state = BotState::new(config, discord_client);

    let mut client = Client::builder(&bot_state.config.discord_token, intents)
        .event_handler(Handler)
        .await?;

    // Store state in serenity's TypeMap
    {
        let mut data = client.data.write().await;
        data.insert::<BotState>(Arc::new(bot_state));
    }

    // Start the bot
    info!("Connecting to Discord...");
    if let Err(e) = client.start().await {
        error!("Bot error: {}", e);
    }

    Ok(())
}
