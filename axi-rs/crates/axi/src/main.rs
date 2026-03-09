//! Axi Discord Bot — event handlers, slash commands, and agent orchestration.

// Many items are written for runtime use but not yet wired — suppress until fully integrated.
#![allow(dead_code)]

// --- Bot layer ---
mod claude_process;
mod channels;
mod commands;
mod events;
mod flowcoder;
mod frontend;
mod permissions;
mod prompts;
mod scheduler;
mod startup;
mod state;
mod streaming;
mod todos;

// --- Activity tracking ---
mod activity;

// --- Agent orchestration ---
mod lifecycle;
mod messaging;
mod procmux_wire;
mod rate_limits;
mod reconnect;
mod registry;
mod shutdown;
mod slots;
mod tasks;
mod types;

// --- MCP layer (tool servers) ---
mod mcp_protocol;
mod mcp_schedule;
mod mcp_tools;

use std::sync::Arc;

use opentelemetry::trace::TracerProvider;
use opentelemetry_otlp::WithExportConfig;
use serenity::all::GatewayIntents;
use serenity::client::{Client, Context, EventHandler};
use serenity::model::gateway::Ready;
use tracing::{error, info};
use tracing_subscriber::EnvFilter;
use tracing_subscriber::layer::SubscriberExt;
use tracing_subscriber::util::SubscriberInitExt;

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
    let env_filter = EnvFilter::try_new(&log_level).unwrap_or_else(|_| EnvFilter::new("info"));
    let fmt_layer = tracing_subscriber::fmt::layer().with_target(false);

    if let Ok(endpoint) = std::env::var("OTEL_ENDPOINT") {
        // OTel export to OTLP/gRPC (Jaeger-compatible)
        let exporter = opentelemetry_otlp::SpanExporter::builder()
            .with_tonic()
            .with_endpoint(&endpoint)
            .build()
            .expect("Failed to create OTLP exporter");
        let tracer_provider = opentelemetry_sdk::trace::SdkTracerProvider::builder()
            .with_batch_exporter(exporter)
            .with_resource(
                opentelemetry_sdk::Resource::builder()
                    .with_service_name("axi-bot")
                    .build(),
            )
            .build();
        let otel_layer = tracing_opentelemetry::layer()
            .with_tracer(tracer_provider.tracer("axi-bot"));
        tracing_subscriber::registry()
            .with(env_filter)
            .with(fmt_layer)
            .with(otel_layer)
            .init();
        info!("OpenTelemetry tracing initialized (endpoint={})", endpoint);
    } else {
        tracing_subscriber::registry()
            .with(env_filter)
            .with(fmt_layer)
            .init();
    }

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
