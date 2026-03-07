//! Bot startup sequence — hub initialization, channel reconstruction, master agent.
//!
//! Called from the `on_ready` event handler. Sets up the `AgentHub`, discovers
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
use crate::frontend::DiscordFrontend;
use crate::state::BotState;

/// Full startup sequence. Called from `on_ready`.
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

    // SDK client factories — wired to procmux bridge via bridge module
    let state_for_create = state.clone();
    let create_client: CreateClientFn = Arc::new(move |name, resume| {
        let state = state_for_create.clone();
        let name = name.to_string();
        let resume = resume.map(ToString::to_string);
        Box::pin(async move {
            crate::bridge::create_client(&state, &name, resume.as_deref()).await?;
            Ok(Box::new(()) as Box<dyn std::any::Any + Send + Sync>)
        })
    });

    let state_for_disconnect = state.clone();
    let disconnect_client: DisconnectClientFn = Arc::new(move |name| {
        let state = state_for_disconnect.clone();
        let name = name.to_string();
        Box::pin(async move {
            crate::bridge::disconnect_client(&state, &name).await;
        })
    });

    let state_for_query = state.clone();
    let send_query: SendQueryFn = Arc::new(move |name, content| {
        let state = state_for_query.clone();
        let name = name.to_string();
        let content = content.clone();
        Box::pin(async move {
            crate::bridge::send_query(&state, &name, &content).await;
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
        process_conn: Arc::new(tokio::sync::Mutex::new(connect_bridge(&state.config).await)),
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

    // 6. Send startup notification
    let _ = state
        .discord_client
        .send_message(
            master_channel.id.get(),
            "*System:* Bot started (Rust). Ready for commands.",
        )
        .await;

    // 7. Mark startup complete
    state.startup_complete.store(true, Ordering::SeqCst);
    info!("Startup complete — bot is ready");

    // 8. Start cron scheduler loop in background
    let state_for_scheduler = state.clone();
    let hub_for_scheduler = hub.clone();
    tokio::spawn(async move {
        crate::scheduler::run_scheduler(state_for_scheduler, hub_for_scheduler).await;
    });

    // 9. Start idle agent reminder loop
    let state_for_idle = state.clone();
    let hub_for_idle = hub.clone();
    tokio::spawn(async move {
        idle_reminder_loop(state_for_idle, hub_for_idle).await;
    });

    // 10. Start bridge monitor (reconnects to procmux if it dies/restarts)
    let state_for_bridge = state.clone();
    let hub_for_bridge = hub.clone();
    tokio::spawn(async move {
        bridge_monitor_loop(state_for_bridge, hub_for_bridge).await;
    });
}

/// Connect to the procmux bridge server, retrying with backoff.
///
/// Tries for up to ~30 seconds at startup. If procmux isn't ready yet,
/// the bridge monitor loop will keep trying in the background.
async fn connect_bridge(
    config: &axi_config::Config,
) -> Option<axi_hub::procmux_wire::ProcmuxProcessConnection> {
    let socket_path = config.bridge_socket_path.to_string_lossy().to_string();
    let mut backoff_ms = 500_u64;
    let max_attempts = 6; // ~30s total: 0.5 + 1 + 2 + 4 + 8 + 16

    for attempt in 1..=max_attempts {
        match axi_hub::procmux_wire::ProcmuxProcessConnection::connect(&socket_path).await {
            Ok(conn) => {
                info!("Connected to procmux bridge at {}", socket_path);
                return Some(conn);
            }
            Err(e) => {
                if attempt == max_attempts {
                    warn!(
                        "Failed to connect to procmux bridge after {} attempts: {} (bridge monitor will retry)",
                        max_attempts, e
                    );
                    return None;
                }
                info!(
                    "Waiting for procmux bridge (attempt {}/{}, retrying in {}ms): {}",
                    attempt, max_attempts, backoff_ms, e
                );
                tokio::time::sleep(std::time::Duration::from_millis(backoff_ms)).await;
                backoff_ms = (backoff_ms * 2).min(16_000);
            }
        }
    }
    None
}

/// Check whether the procmux bridge connection is down.
///
/// Returns `true` if the bridge is unreachable (connection is `None` or dead).
pub(crate) async fn is_bridge_down(hub: &AgentHub) -> bool {
    let conn = hub.process_conn.lock().await;
    !conn.as_ref().is_some_and(axi_hub::procmux_wire::ProcmuxProcessConnection::is_alive)
}

/// Background loop that monitors the procmux bridge connection.
///
/// If procmux dies, all agent sessions are lost (procmux has no persistent
/// state). Rather than trying to reconnect in-place with stale state,
/// we notify the user and exit with code 42 so systemd restarts us cleanly.
/// We wait a few seconds first so procmux's own Restart= has time to bring
/// it back before we try to reconnect on startup.
async fn bridge_monitor_loop(state: Arc<BotState>, hub: Arc<AgentHub>) {
    const CHECK_INTERVAL_SECS: u64 = 2;
    const GRACE_PERIOD_SECS: u64 = 3;

    loop {
        tokio::time::sleep(std::time::Duration::from_secs(CHECK_INTERVAL_SECS)).await;

        if !is_bridge_down(&hub).await {
            continue;
        }

        warn!("Bridge connection lost — procmux is down");

        // Notify master channel
        if let Some(ch_id) = state.channel_for_agent(&state.config.master_agent_name).await {
            let _ = state
                .discord_client
                .send_message(
                    ch_id.get(),
                    "*System:* Bridge connection lost. Restarting to reconnect...",
                )
                .await;
        }

        // Give procmux time to restart (its RestartSec=1)
        tokio::time::sleep(std::time::Duration::from_secs(GRACE_PERIOD_SECS)).await;

        // Exit with restart code — systemd will relaunch us
        error!("Exiting with code 42 to trigger restart after bridge loss");
        std::process::exit(42);
    }
}

/// Periodic loop that checks for idle agents and sends reminders.
async fn idle_reminder_loop(state: Arc<BotState>, hub: Arc<AgentHub>) {
    let check_interval = std::time::Duration::from_secs(60);
    let thresholds = &state.config.idle_reminder_thresholds;

    if thresholds.is_empty() {
        return;
    }

    loop {
        tokio::time::sleep(check_interval).await;

        let sessions = hub.sessions.lock().await;
        let now = chrono::Utc::now();

        for (name, session) in sessions.iter() {
            if name == &state.config.master_agent_name {
                continue;
            }
            if session.client.is_none() {
                continue;
            }
            if session.query_lock.try_lock().is_err() {
                continue; // busy
            }

            let idle_secs = (now - session.last_activity).num_seconds().max(0) as u64;
            let reminder_idx = session.idle_reminder_count as usize;

            if reminder_idx < thresholds.len() {
                let threshold = thresholds[reminder_idx].as_secs();
                if idle_secs >= threshold {
                    let idle_minutes = idle_secs as f64 / 60.0;
                    let name = name.clone();
                    drop(sessions);

                    hub.callbacks
                        .on_idle_reminder(&name, idle_minutes)
                        .await;

                    let mut sessions = hub.sessions.lock().await;
                    if let Some(s) = sessions.get_mut(&name) {
                        s.idle_reminder_count += 1;
                    }
                    break; // re-acquire lock next iteration
                }
            }
        }
    }
}

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

#[cfg(test)]
mod tests {
    use super::*;
    use std::collections::{HashMap, HashSet};

    use axi_hub::hub::AgentHub;
    use axi_hub::procmux_wire::ProcmuxProcessConnection;
    use axi_hub::rate_limits::RateLimitTracker;
    use axi_hub::scheduler::Scheduler;
    use axi_hub::tasks::BackgroundTaskSet;

    /// Build a minimal `AgentHub` with the given process connection.
    fn test_hub(conn: Option<ProcmuxProcessConnection>) -> Arc<AgentHub> {
        // Dummy frontend callbacks
        struct NoopCallbacks;
        impl axi_hub::FrontendCallbacks for NoopCallbacks {
            fn post_message(&self, _: &str, _: &str) -> axi_hub::callbacks::CallbackResult {
                Box::pin(async {})
            }
            fn post_system(&self, _: &str, _: &str) -> axi_hub::callbacks::CallbackResult {
                Box::pin(async {})
            }
            fn on_wake(&self, _: &str) -> axi_hub::callbacks::CallbackResult {
                Box::pin(async {})
            }
            fn on_sleep(&self, _: &str) -> axi_hub::callbacks::CallbackResult {
                Box::pin(async {})
            }
            fn on_session_id(&self, _: &str, _: &str) -> axi_hub::callbacks::CallbackResult {
                Box::pin(async {})
            }
            fn on_spawn(&self, _: &str) -> axi_hub::callbacks::CallbackResult {
                Box::pin(async {})
            }
            fn on_kill(&self, _: &str, _: Option<&str>) -> axi_hub::callbacks::CallbackResult {
                Box::pin(async {})
            }
            fn broadcast(&self, _: &str) -> axi_hub::callbacks::CallbackResult {
                Box::pin(async {})
            }
            fn schedule_rate_limit_expiry(&self, _: f64) {}
            fn on_idle_reminder(&self, _: &str, _: f64) -> axi_hub::callbacks::CallbackResult {
                Box::pin(async {})
            }
            fn on_reconnect(&self, _: &str, _: bool) -> axi_hub::callbacks::CallbackResult {
                Box::pin(async {})
            }
            fn close_app(&self) -> axi_hub::callbacks::CallbackResult {
                Box::pin(async {})
            }
            fn kill_process(&self) -> axi_hub::callbacks::CallbackResult {
                Box::pin(async {})
            }
            fn send_goodbye(&self) -> axi_hub::callbacks::CallbackResult {
                Box::pin(async {})
            }
        }

        let scheduler = Arc::new(Scheduler::new(
            7,
            HashSet::new(),
            Arc::new(Vec::new),
            Arc::new(|_| Box::pin(async {})),
        ));

        Arc::new(AgentHub {
            sessions: Arc::new(tokio::sync::Mutex::new(HashMap::new())),
            callbacks: Arc::new(NoopCallbacks),
            scheduler,
            rate_limits: Arc::new(tokio::sync::Mutex::new(RateLimitTracker::new(None, None))),
            tasks: BackgroundTaskSet::new(),
            wake_lock: tokio::sync::Mutex::new(()),
            process_conn: Arc::new(tokio::sync::Mutex::new(conn)),
            create_client: Arc::new(|_, _| Box::pin(async { Ok(Box::new(()) as Box<dyn std::any::Any + Send + Sync>) })),
            disconnect_client: Arc::new(|_| Box::pin(async {})),
            send_query: Arc::new(|_, _| Box::pin(async {})),
            query_timeout: 300.0,
            max_retries: 3,
            retry_base_delay: 5.0,
            slot_timeout: 300.0,
            shutdown_requested: std::sync::atomic::AtomicBool::new(false),
        })
    }

    #[tokio::test]
    async fn bridge_down_when_no_connection() {
        let hub = test_hub(None);
        assert!(is_bridge_down(&hub).await);
    }

    #[tokio::test]
    async fn bridge_up_with_live_connection() {
        use procmux::server::ProcmuxServer;

        let socket_path = "/tmp/procmux-test-bridge-up.sock";
        let _ = std::fs::remove_file(socket_path);

        let server = ProcmuxServer::new(socket_path);
        let server_handle = tokio::spawn(async move {
            server.run().await.unwrap();
        });
        tokio::time::sleep(std::time::Duration::from_millis(100)).await;

        let conn = ProcmuxProcessConnection::connect(socket_path).await.unwrap();
        let hub = test_hub(Some(conn));

        assert!(!is_bridge_down(&hub).await);

        server_handle.abort();
        let _ = std::fs::remove_file(socket_path);
    }

    #[tokio::test]
    async fn bridge_down_after_server_killed() {
        use procmux::server::ProcmuxServer;

        let socket_path = "/tmp/procmux-test-bridge-down.sock";
        let _ = std::fs::remove_file(socket_path);

        let server = ProcmuxServer::new(socket_path);
        let server_handle = tokio::spawn(async move {
            server.run().await.unwrap();
        });
        tokio::time::sleep(std::time::Duration::from_millis(100)).await;

        let conn = ProcmuxProcessConnection::connect(socket_path).await.unwrap();
        assert!(conn.is_alive());

        // Kill the server
        server_handle.abort();
        let _ = server_handle.await;

        // Wait for the connection to detect the loss
        tokio::time::sleep(std::time::Duration::from_millis(200)).await;

        let hub = test_hub(Some(conn));
        assert!(is_bridge_down(&hub).await);

        let _ = std::fs::remove_file(socket_path);
    }
}
