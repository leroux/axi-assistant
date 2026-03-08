//! Hot restart — bridge connection and agent reconnection.
//!
//! Handles reconnecting to agents that survived a bot restart via
//! the procmux bridge.

use std::sync::Arc;

use chrono::Utc;
use tracing::{info, warn};

use crate::procmux_wire::ProcmuxProcessConnection;
use crate::state::BotState;

pub async fn connect_procmux(state: Arc<BotState>, socket_path: &str) {
    match ProcmuxProcessConnection::connect(socket_path).await {
        Ok(conn) => {
            info!("Bridge connection established");

            match conn.list_agents().await {
                Ok(result) => {
                    let agents = &result.agents;
                    info!("Bridge reports {} agent(s): {:?}", agents.len(), agents);

                    for agent_name in agents {
                        let session_exists = {
                            let sessions = state.sessions.lock().await;
                            sessions.contains_key(agent_name)
                        };

                        if !session_exists {
                            warn!(
                                "Bridge has agent '{}' but no matching session — killing",
                                agent_name
                            );
                            conn.kill(agent_name).await.ok();
                            continue;
                        }

                        {
                            let mut sessions = state.sessions.lock().await;
                            if let Some(session) = sessions.get_mut(agent_name) {
                                session.reconnecting = true;
                            }
                        }

                        let state_ref = state.clone();
                        let name = agent_name.clone();
                        let conn_ref = conn.clone();
                        tokio::spawn(async move {
                            reconnect_single(&state_ref, &name, &conn_ref).await;
                        });
                    }
                }
                Err(e) => {
                    warn!("Failed to list bridge agents: {}", e);
                }
            }

            *state.process_conn.lock().await = Some(conn);
        }
        Err(e) => {
            warn!(
                "Failed to connect to bridge — agents will use direct subprocess mode: {}",
                e
            );
        }
    }
}

async fn reconnect_single(
    state: &BotState,
    name: &str,
    conn: &ProcmuxProcessConnection,
) {
    match conn.subscribe(name).await {
        Ok(result) => {
            let replayed = result.replayed.unwrap_or(0);
            let cli_status = result.status.unwrap_or_else(|| "unknown".to_string());
            let cli_idle = result.idle.unwrap_or(true);

            info!(
                "Subscribed to '{}' (replayed={}, status={}, idle={})",
                name, replayed, cli_status, cli_idle
            );

            if cli_status == "exited" {
                info!(
                    "Agent '{}' CLI exited while we were down — cleaning up",
                    name
                );
                let mut sessions = state.sessions.lock().await;
                if let Some(session) = sessions.get_mut(name) {
                    session.reconnecting = false;
                }
                return;
            }

            let resume_id = {
                let sessions = state.sessions.lock().await;
                sessions.get(name).and_then(|s| s.session_id.clone())
            };

            match crate::claude_process::create_client(state, name, resume_id.as_deref()).await {
                Ok(()) => {
                    let mut sessions = state.sessions.lock().await;
                    if let Some(session) = sessions.get_mut(name) {
                        session.awake = true;
                        session.last_activity = Utc::now();
                        session.reconnecting = false;

                        let was_mid_task = cli_status == "running" && !cli_idle;
                        if was_mid_task {
                            session.bridge_busy = true;
                            info!(
                                "Agent '{}' reconnected mid-task (idle=false, replayed={})",
                                name, replayed
                            );
                        }
                    }
                    state.scheduler().await.restore_slot(name).await;
                    drop(sessions);

                    let was_mid_task = cli_status == "running" && !cli_idle;
                    crate::frontend::on_reconnect(state, name, was_mid_task).await;
                    info!("Reconnect complete for '{}'", name);
                }
                Err(e) => {
                    warn!(
                        "Failed to create client for reconnecting agent '{}': {:?}",
                        name, e
                    );
                    let mut sessions = state.sessions.lock().await;
                    if let Some(session) = sessions.get_mut(name) {
                        session.reconnecting = false;
                    }
                }
            }
        }
        Err(e) => {
            warn!("Failed to subscribe to agent '{}': {}", name, e);
            let mut sessions = state.sessions.lock().await;
            if let Some(session) = sessions.get_mut(name) {
                session.reconnecting = false;
            }
        }
    }
}
