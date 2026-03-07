//! Graceful shutdown coordinator for agent sessions.
//!
//! - `graceful_shutdown()`: waits for busy agents to finish, then sleeps all and exits.
//! - `force_shutdown()`: skips the wait and exits immediately.

use std::sync::Arc;
use std::sync::atomic::{AtomicBool, Ordering};

use tracing::{info, warn};

use crate::lifecycle;
use crate::state::BotState;

const RESTART_EXIT_CODE: i32 = 42;
const STATUS_INTERVAL_SECS: u64 = 30;
const POLL_INTERVAL_SECS: u64 = 5;
const SHUTDOWN_DEADLINE_SECS: u64 = 30;
const SHUTDOWN_TIMEOUT_SECS: u64 = 300;

pub struct ShutdownCoordinator {
    state: Arc<BotState>,
    requested: AtomicBool,
    bridge_mode: bool,
}

impl ShutdownCoordinator {
    pub const fn new(state: Arc<BotState>, bridge_mode: bool) -> Self {
        Self {
            state,
            requested: AtomicBool::new(false),
            bridge_mode,
        }
    }

    pub fn is_requested(&self) -> bool {
        self.requested.load(Ordering::SeqCst)
    }

    async fn get_busy_agents(&self, skip: Option<&str>) -> Vec<String> {
        let sessions = self.state.sessions.lock().await;
        sessions
            .iter()
            .filter(|(name, session)| {
                lifecycle::is_processing(session) && (skip != Some(name.as_str()))
            })
            .map(|(name, _)| name.clone())
            .collect()
    }

    async fn sleep_all(&self, skip: Option<&str>) {
        let names: Vec<String> = {
            let sessions = self.state.sessions.lock().await;
            sessions
                .iter()
                .filter(|(name, session)| {
                    session.awake && (skip != Some(name.as_str()))
                })
                .map(|(name, _)| name.clone())
                .collect()
        };

        for name in names {
            lifecycle::sleep_agent(&self.state, &name, true).await;
        }
    }

    async fn execute_exit(&self, skip_agent: Option<&str>) {
        let deadline = SHUTDOWN_DEADLINE_SECS;
        std::thread::spawn(move || {
            std::thread::sleep(std::time::Duration::from_secs(deadline));
            warn!(
                "Shutdown safety deadline reached ({}s) — forcing exit",
                deadline
            );
            std::process::exit(RESTART_EXIT_CODE);
        });

        crate::frontend::send_goodbye(&self.state).await;

        if !self.bridge_mode {
            self.sleep_all(skip_agent).await;
        }

        crate::frontend::close_app();
        crate::frontend::kill_process();
    }

    pub async fn graceful_shutdown(&self, source: &str, skip_agent: Option<&str>) {
        if self.requested.swap(true, Ordering::SeqCst) {
            info!(
                "Graceful shutdown already in progress (ignoring duplicate from {})",
                source
            );
            return;
        }

        let busy = self.get_busy_agents(skip_agent).await;
        info!("Graceful shutdown initiated from {}", source);

        if self.bridge_mode {
            info!("Bridge mode — skipping agent wait, agents keep running");
            self.execute_exit(skip_agent).await;
            return;
        }

        if busy.is_empty() {
            info!("No agents busy — exiting immediately");
            self.execute_exit(skip_agent).await;
            return;
        }

        for name in &busy {
            crate::frontend::post_system(
                &self.state,
                name,
                &format!(
                    "Restart pending — waiting for **{name}** to finish current task..."
                ),
            )
            .await;
        }

        let start = std::time::Instant::now();
        let mut last_status = 0u64;

        loop {
            tokio::time::sleep(std::time::Duration::from_secs(POLL_INTERVAL_SECS)).await;
            let elapsed = start.elapsed().as_secs();

            let still_busy = self.get_busy_agents(skip_agent).await;
            if still_busy.is_empty() {
                info!("All agents finished after {}s — exiting", elapsed);
                break;
            }

            if elapsed > SHUTDOWN_TIMEOUT_SECS {
                warn!(
                    "Shutdown timeout after {}s — agents still busy: {:?}",
                    elapsed, still_busy
                );
                break;
            }

            if elapsed - last_status >= STATUS_INTERVAL_SECS {
                last_status = elapsed;
                for name in &still_busy {
                    crate::frontend::post_system(
                        &self.state,
                        name,
                        &format!(
                            "Still waiting for **{name}** to finish... ({elapsed}s)"
                        ),
                    )
                    .await;
                }
            }
        }

        self.execute_exit(skip_agent).await;
    }

    pub async fn force_shutdown(&self, source: &str) {
        if self.requested.swap(true, Ordering::SeqCst) {
            info!(
                "Shutdown already in progress — escalating to force (from {})",
                source
            );
        }
        info!("Force shutdown initiated from {}", source);
        self.execute_exit(None).await;
    }
}
