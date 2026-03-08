//! Bot startup sequence — channel reconstruction, master agent, scheduler.
//!
//! Called from the `on_ready` event handler. Discovers guild infrastructure,
//! reconstructs channel-to-agent mappings, registers the master agent,
//! initializes the scheduler, and starts background loops.

use std::collections::HashSet;
use std::sync::Arc;
use std::sync::atomic::Ordering;

use serenity::all::GuildId;
use serenity::client::Context;
use tracing::{error, info, warn};

use crate::slots::{GetSessionsFn, Scheduler, SessionInfo, SleepFn};
use crate::types::AgentSession;

use crate::channels;
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

    for (channel_id, agent_name) in &channel_map {
        state.register_channel(*channel_id, agent_name).await;
    }

    // 3. Initialize the scheduler
    let sessions_ref = state.sessions.clone();
    let get_sessions: GetSessionsFn = Arc::new(move || {
        if let Ok(sessions) = sessions_ref.try_lock() {
            sessions
                .values()
                .map(|s| SessionInfo {
                    name: s.name.clone(),
                    is_awake: s.awake,
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

    // Sleep callback uses a Weak<BotState> to avoid circular Arc reference
    let state_weak = Arc::downgrade(&state);
    let sleep_fn: SleepFn = Arc::new(move |name| {
        let weak = state_weak.clone();
        let name = name.to_string();
        Box::pin(async move {
            if let Some(state) = weak.upgrade() {
                crate::lifecycle::sleep_agent(&state, &name, false).await;
            }
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

    {
        let mut sched_lock = state.scheduler.write().await;
        *sched_lock = Some(scheduler);
    }

    // 4. Connect to procmux bridge
    {
        let conn = connect_bridge(&state.config).await;
        *state.process_conn.lock().await = conn;
    }

    // 5. Register the master agent session
    let master_name = state.config.master_agent_name.clone();
    let master_cwd = state.config.default_cwd.to_string_lossy().to_string();

    let master_session_id = std::fs::read_to_string(&state.config.master_session_path)
        .ok()
        .map(|s| s.trim().to_string())
        .filter(|s| !s.is_empty());

    let mut master_session = AgentSession::new(master_name.clone());
    master_session.agent_type = state.config.default_agent_type.clone();
    master_session.cwd = master_cwd.clone();
    master_session.session_id = master_session_id;

    // Build SDK MCP servers for the master agent
    let (sdk_servers, _sdk_json) =
        crate::mcp_tools::build_sdk_mcp_config(&state, &master_name, &master_cwd, true);
    master_session.sdk_mcp_servers = sdk_servers;

    crate::registry::register_session(&state, master_session).await;

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
    tokio::spawn(async move {
        crate::scheduler::run_scheduler(state_for_scheduler).await;
    });

    // 9. Start idle agent reminder loop
    let state_for_idle = state.clone();
    tokio::spawn(async move {
        idle_reminder_loop(state_for_idle).await;
    });

    // 10. Start bridge monitor
    let state_for_bridge = state.clone();
    tokio::spawn(async move {
        bridge_monitor_loop(state_for_bridge).await;
    });
}

/// Connect to the procmux bridge server, retrying with backoff.
async fn connect_bridge(
    config: &axi_config::Config,
) -> Option<crate::procmux_wire::ProcmuxProcessConnection> {
    let socket_path = config.bridge_socket_path.to_string_lossy().to_string();
    let mut backoff_ms = 500_u64;
    let max_attempts = 6;

    for attempt in 1..=max_attempts {
        match crate::procmux_wire::ProcmuxProcessConnection::connect(&socket_path).await {
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
pub(crate) async fn is_bridge_down(state: &BotState) -> bool {
    let conn = state.process_conn.lock().await;
    !conn
        .as_ref()
        .is_some_and(crate::procmux_wire::ProcmuxProcessConnection::is_alive)
}

/// Background loop that monitors the procmux bridge connection.
async fn bridge_monitor_loop(state: Arc<BotState>) {
    const CHECK_INTERVAL_SECS: u64 = 2;
    const GRACE_PERIOD_SECS: u64 = 3;

    loop {
        tokio::time::sleep(std::time::Duration::from_secs(CHECK_INTERVAL_SECS)).await;

        if !is_bridge_down(&state).await {
            continue;
        }

        warn!("Bridge connection lost — procmux is down");

        if let Some(ch_id) = state.channel_for_agent(&state.config.master_agent_name).await {
            let _ = state
                .discord_client
                .send_message(
                    ch_id.get(),
                    "*System:* Bridge connection lost. Restarting to reconnect...",
                )
                .await;
        }

        tokio::time::sleep(std::time::Duration::from_secs(GRACE_PERIOD_SECS)).await;

        error!("Exiting with code 42 to trigger restart after bridge loss");
        std::process::exit(42);
    }
}

/// Periodic loop that checks for idle agents and sends reminders.
async fn idle_reminder_loop(state: Arc<BotState>) {
    let check_interval = std::time::Duration::from_secs(60);
    let thresholds = &state.config.idle_reminder_thresholds;

    if thresholds.is_empty() {
        return;
    }

    loop {
        tokio::time::sleep(check_interval).await;

        let sessions = state.sessions.lock().await;
        let now = chrono::Utc::now();

        for (name, session) in sessions.iter() {
            if name == &state.config.master_agent_name {
                continue;
            }
            if !session.awake {
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

                    crate::frontend::on_idle_reminder(&state, &name, idle_minutes).await;

                    let mut sessions = state.sessions.lock().await;
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

    #[tokio::test]
    async fn bridge_down_when_no_connection() {
        let tmp = tempfile::tempdir().unwrap();
        let config = axi_config::Config::for_test(tmp.path());
        let discord = axi_config::DiscordClient::new("test-token");
        let state = Arc::new(BotState::new(config, discord));
        assert!(is_bridge_down(&state).await);
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

        let conn =
            crate::procmux_wire::ProcmuxProcessConnection::connect(socket_path)
                .await
                .unwrap();

        let tmp = tempfile::tempdir().unwrap();
        let config = axi_config::Config::for_test(tmp.path());
        let discord = axi_config::DiscordClient::new("test-token");
        let state = Arc::new(BotState::new(config, discord));
        *state.process_conn.lock().await = Some(conn);

        assert!(!is_bridge_down(&state).await);

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

        let conn =
            crate::procmux_wire::ProcmuxProcessConnection::connect(socket_path)
                .await
                .unwrap();
        assert!(conn.is_alive());

        server_handle.abort();
        let _ = server_handle.await;
        tokio::time::sleep(std::time::Duration::from_millis(200)).await;

        let tmp = tempfile::tempdir().unwrap();
        let config = axi_config::Config::for_test(tmp.path());
        let discord = axi_config::DiscordClient::new("test-token");
        let state = Arc::new(BotState::new(config, discord));
        *state.process_conn.lock().await = Some(conn);

        assert!(is_bridge_down(&state).await);

        let _ = std::fs::remove_file(socket_path);
    }
}
