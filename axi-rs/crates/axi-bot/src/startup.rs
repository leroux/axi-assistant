//! Bot startup sequence — hub initialization, channel reconstruction, master agent.
//!
//! Called from the on_ready event handler. Sets up the AgentHub, discovers
//! guild infrastructure, reconstructs channel-to-agent mappings, registers
//! the master agent, and starts the cron scheduler.

use std::collections::HashSet;
use std::sync::Arc;
use std::sync::atomic::Ordering;

use serenity::all::GuildId;
use serenity::client::Context;
use tracing::{error, info, warn};

use axi_hub::hub::{AgentHub, CreateClientFn, DisconnectClientFn, SendQueryFn};
use axi_hub::scheduler::{GetSessionsFn, Scheduler, SessionInfo, SleepFn};
use axi_hub::tasks::BackgroundTaskSet;
use axi_hub::types::AgentSession;

use crate::channels;
use crate::crash_handler;
use crate::frontend::DiscordFrontend;
use crate::state::BotState;

/// Full startup sequence. Called from on_ready.
pub async fn initialize(ctx: &Context, state: Arc<BotState>) {
    info!("Starting initialization sequence...");

    // 1. Ensure guild infrastructure (categories)
    let infra = match channels::ensure_guild_infrastructure(ctx, &state.config).await {
        Ok(infra) => {
            info!("Guild infrastructure ready");
            infra
        }
        Err(e) => {
            error!("Failed to set up guild infrastructure: {}", e);
            return;
        }
    };

    // Store infrastructure
    {
        let mut infra_lock = state.infra.write().await;
        *infra_lock = Some(infra.clone());
    }

    // 2. Reconstruct channel-to-agent mappings from guild channels
    let channel_map = match channels::reconstruct_channel_map(
        ctx,
        GuildId::new(state.config.discord_guild_id),
        infra.active_category_id,
        infra.axi_category_id,
        infra.killed_category_id,
        state.config.channel_status_enabled,
    )
    .await
    {
        Ok(map) => {
            info!("Reconstructed {} channel mappings", map.len());
            map
        }
        Err(e) => {
            warn!("Failed to reconstruct channel map: {}", e);
            std::collections::HashMap::new()
        }
    };

    // Populate state channel maps
    for (channel_id, agent_name) in &channel_map {
        state.register_channel(*channel_id, agent_name).await;
    }

    // 3. Create the AgentHub
    let frontend = Arc::new(DiscordFrontend::new(state.clone()));

    // SDK client factories — placeholders that will be wired to claudewire bridge
    let create_client: CreateClientFn = Arc::new(|_name, _resume| {
        Box::pin(async move {
            // TODO: Connect to procmux bridge, start claudewire session
            // For now, return a dummy client
            Ok(Box::new(()) as Box<dyn std::any::Any + Send + Sync>)
        })
    });

    let disconnect_client: DisconnectClientFn = Arc::new(|_name| {
        Box::pin(async move {
            // TODO: Disconnect claudewire session via bridge
        })
    });

    let send_query: SendQueryFn = Arc::new(|_name, _content| {
        Box::pin(async move {
            // TODO: Send query to claudewire session via bridge
        })
    });

    // Scheduler setup — use the hub's sessions Arc for get_sessions
    let hub_sessions = Arc::new(tokio::sync::Mutex::new(
        std::collections::HashMap::<String, AgentSession>::new(),
    ));
    let sessions_ref = hub_sessions.clone();

    let get_sessions: GetSessionsFn = Arc::new(move || {
        // Called synchronously by scheduler. Use try_lock to avoid blocking.
        if let Ok(sessions) = sessions_ref.try_lock() {
            sessions
                .values()
                .map(|s| SessionInfo {
                    name: s.name.clone(),
                    is_awake: s.client.is_some(),
                    is_busy: s.query_lock.try_lock().is_err(),
                    is_bridge_busy: s.bridge_busy,
                    last_activity: s.last_activity,
                    query_started: s.activity.query_started,
                })
                .collect()
        } else {
            Vec::new()
        }
    });

    let sleep_fn: SleepFn = Arc::new(|_name| {
        Box::pin(async move {
            // Will be replaced with proper sleep through hub
        })
    });

    let mut protected = HashSet::new();
    protected.insert(state.config.master_agent_name.clone());

    let scheduler = Arc::new(Scheduler::new(
        state.config.max_awake_agents,
        protected,
        get_sessions,
        sleep_fn,
    ));

    let hub = AgentHub {
        sessions: hub_sessions,
        callbacks: frontend,
        scheduler,
        rate_limits: Arc::new(tokio::sync::Mutex::new(
            axi_hub::rate_limits::RateLimitTracker::new(
                Some(state.config.usage_history_path.to_string_lossy().to_string()),
                Some(state.config.rate_limit_history_path.to_string_lossy().to_string()),
            ),
        )),
        tasks: BackgroundTaskSet::new(),
        wake_lock: tokio::sync::Mutex::new(()),
        process_conn: Arc::new(tokio::sync::Mutex::new(None)),
        create_client,
        disconnect_client,
        send_query,
        query_timeout: state.config.query_timeout.as_secs_f64(),
        max_retries: state.config.api_error_max_retries,
        retry_base_delay: state.config.api_error_base_delay.as_secs_f64(),
        slot_timeout: 300.0,
        shutdown_requested: std::sync::atomic::AtomicBool::new(false),
    };

    let hub = Arc::new(hub);

    // 4. Register the master agent session
    let master_name = state.config.master_agent_name.clone();
    let master_cwd = state.config.default_cwd.to_string_lossy().to_string();

    // Load master session ID if persisted
    let master_session_id = std::fs::read_to_string(&state.config.master_session_path)
        .ok()
        .map(|s| s.trim().to_string())
        .filter(|s| !s.is_empty());

    let mut master_session = AgentSession::new(master_name.clone());
    master_session.cwd = master_cwd;
    master_session.session_id = master_session_id;

    axi_hub::registry::register_session(&hub, master_session).await;

    // Ensure master channel exists
    let master_channel = match channels::ensure_agent_channel(
        ctx,
        GuildId::new(state.config.discord_guild_id),
        &master_name,
        infra.axi_category_id,
        state.config.channel_status_enabled,
    )
    .await
    {
        Ok(ch) => {
            info!("Master channel: #{}", ch.name);
            ch
        }
        Err(e) => {
            error!("Failed to create master channel: {}", e);
            return;
        }
    };

    state
        .register_channel(master_channel.id, &master_name)
        .await;

    // 5. Store hub in state
    {
        let mut hub_lock = state.hub.write().await;
        *hub_lock = Some(hub.clone());
    }

    // 6. Check for crash analysis marker
    if state.config.enable_crash_handler {
        if let Some(marker) = crash_handler::consume_marker(
            &state.config.crash_analysis_marker_path.parent().unwrap_or(std::path::Path::new(".")),
        ) {
            let notification = crash_handler::crash_notification(&marker, true);
            let _ = state
                .discord_client
                .send_message(master_channel.id.get(), &notification)
                .await;
        }
    }

    // 7. Send startup notification
    let _ = state
        .discord_client
        .send_message(
            master_channel.id.get(),
            "*System:* Bot started (Rust). Ready for commands.",
        )
        .await;

    // 8. Mark startup complete
    state.startup_complete.store(true, Ordering::SeqCst);
    info!("Startup complete — bot is ready");

    // 9. Start cron scheduler loop in background
    let state_for_scheduler = state.clone();
    let hub_for_scheduler = hub.clone();
    tokio::spawn(async move {
        crate::scheduler::run_scheduler(state_for_scheduler, hub_for_scheduler).await;
    });
}
