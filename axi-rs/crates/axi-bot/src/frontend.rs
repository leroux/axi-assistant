//! Discord frontend callbacks for `AgentHub`.
//!
//! Implements the `FrontendCallbacks` trait, translating hub lifecycle events
//! into Discord messages (channel posts, status updates, channel moves).

use std::sync::Arc;

use serenity::all::ChannelId;
use tracing::{info, warn};

use axi_config::DiscordClient;
use axi_hub::callbacks::{CallbackResult, FrontendCallbacks};

use crate::channels;
use crate::state::BotState;

/// Discord-backed frontend callbacks.
///
/// Holds a reference to bot state for channel lookups and a Discord REST
/// client for sending messages.
pub struct DiscordFrontend {
    state: Arc<BotState>,
}

impl DiscordFrontend {
    pub const fn new(state: Arc<BotState>) -> Self {
        Self { state }
    }

    fn discord_client(&self) -> &DiscordClient {
        &self.state.discord_client
    }

    /// Resolve agent name to channel ID.
    async fn channel_for(&self, agent_name: &str) -> Option<ChannelId> {
        self.state.channel_for_agent(agent_name).await
    }

    /// Send a message to an agent's channel via REST API.
    async fn send_to_agent(&self, agent_name: &str, text: &str) {
        if let Some(channel_id) = self.channel_for(agent_name).await {
            if let Err(e) = self
                .discord_client()
                .send_message(channel_id.get(), text)
                .await
            {
                warn!(
                    "Failed to send message to agent '{}' channel: {}",
                    agent_name, e
                );
            }
        } else {
            warn!(
                "No channel found for agent '{}', dropping message",
                agent_name
            );
        }
    }
}

impl FrontendCallbacks for DiscordFrontend {
    fn post_message(&self, agent_name: &str, text: &str) -> CallbackResult {
        let name = agent_name.to_string();
        let text = text.to_string();
        let state = self.state.clone();
        Box::pin(async move {
            let frontend = Self::new(state);
            frontend.send_to_agent(&name, &text).await;
        })
    }

    fn post_system(&self, agent_name: &str, text: &str) -> CallbackResult {
        let name = agent_name.to_string();
        let msg = format!("*System:* {text}");
        let state = self.state.clone();
        Box::pin(async move {
            let frontend = Self::new(state);
            frontend.send_to_agent(&name, &msg).await;
        })
    }

    fn on_wake(&self, agent_name: &str) -> CallbackResult {
        let name = agent_name.to_string();
        let state = self.state.clone();
        Box::pin(async move {
            info!("Agent '{}' woke up", name);
            if state.config.channel_status_enabled {
                if let Some(channel_id) = state.channel_for_agent(&name).await {
                    let _ = state
                        .discord_client
                        .edit_channel_name(
                            channel_id.get(),
                            &format!(
                                "{}-{}",
                                channels::status_emoji("working").unwrap_or(""),
                                channels::normalize_channel_name(&name)
                            ),
                        )
                        .await;
                }
            }
        })
    }

    fn on_sleep(&self, agent_name: &str) -> CallbackResult {
        let name = agent_name.to_string();
        let state = self.state.clone();
        Box::pin(async move {
            info!("Agent '{}' went to sleep", name);
            if state.config.channel_status_enabled {
                if let Some(channel_id) = state.channel_for_agent(&name).await {
                    let _ = state
                        .discord_client
                        .edit_channel_name(
                            channel_id.get(),
                            &format!(
                                "{}-{}",
                                channels::status_emoji("idle").unwrap_or(""),
                                channels::normalize_channel_name(&name)
                            ),
                        )
                        .await;
                }
            }
        })
    }

    fn on_session_id(&self, agent_name: &str, session_id: &str) -> CallbackResult {
        let name = agent_name.to_string();
        let sid = session_id.to_string();
        let state = self.state.clone();
        Box::pin(async move {
            info!("Agent '{}' session_id: {}", name, sid);
            // Update channel topic with session ID
            if let Some(channel_id) = state.channel_for_agent(&name).await {
                let hub = state.hub().await;
                let sessions = hub.sessions.lock().await;
                if let Some(session) = sessions.get(&name) {
                    let topic = channels::format_channel_topic(
                        &session.cwd,
                        Some(&sid),
                        session.system_prompt_hash.as_deref(),
                        Some(&session.agent_type),
                    );
                    drop(sessions);
                    let _ = state
                        .discord_client
                        .edit_channel_topic(channel_id.get(), &topic)
                        .await;
                }
            }
        })
    }

    fn on_spawn(&self, agent_name: &str) -> CallbackResult {
        let name = agent_name.to_string();
        let state = self.state.clone();
        Box::pin(async move {
            info!("Agent '{}' spawned", name);
            let frontend = Self::new(state);
            frontend
                .send_to_agent(&name, &format!("*System:* Agent **{name}** spawned."))
                .await;
        })
    }

    fn on_kill(&self, agent_name: &str, session_id: Option<&str>) -> CallbackResult {
        let name = agent_name.to_string();
        let sid = session_id.map(ToString::to_string);
        let state = self.state.clone();
        Box::pin(async move {
            let sid_text = sid
                .as_deref()
                .map(|s| format!(" (session: `{s}`)"))
                .unwrap_or_default();
            let msg = format!("*System:* Agent **{name}** killed.{sid_text}");
            let frontend = Self::new(state.clone());
            frontend.send_to_agent(&name, &msg).await;

            // Move channel to Killed category
            if let Some(channel_id) = state.channel_for_agent(&name).await {
                let infra = state.infra.read().await;
                if let Some(ref infra) = *infra {
                    if let Some(killed_cat) = infra.killed_category_id {
                        let _ = state
                            .discord_client
                            .edit_channel_category(channel_id.get(), killed_cat.get())
                            .await;
                    }
                }
            }
        })
    }

    fn broadcast(&self, message: &str) -> CallbackResult {
        let msg = message.to_string();
        let state = self.state.clone();
        Box::pin(async move {
            let agent_channels = state.agent_channels.read().await;
            for (_, channel_id) in agent_channels.iter() {
                let _ = state
                    .discord_client
                    .send_message(channel_id.get(), &msg)
                    .await;
            }
        })
    }

    fn schedule_rate_limit_expiry(&self, _delay_secs: f64) {
        // Rate limit expiry scheduling — noop for now, will be wired to scheduler
    }

    fn on_idle_reminder(&self, agent_name: &str, idle_minutes: f64) -> CallbackResult {
        let name = agent_name.to_string();
        let state = self.state.clone();
        Box::pin(async move {
            let msg = format!(
                "*System:* Agent **{name}** has been idle for {idle_minutes:.0} minutes."
            );
            let frontend = Self::new(state);
            frontend.send_to_agent(&name, &msg).await;
        })
    }

    fn on_reconnect(&self, agent_name: &str, was_mid_task: bool) -> CallbackResult {
        let name = agent_name.to_string();
        let state = self.state.clone();
        Box::pin(async move {
            let msg = if was_mid_task {
                format!(
                    "*System:* Agent **{name}** reconnected (was mid-task, resuming)."
                )
            } else {
                format!("*System:* Agent **{name}** reconnected.")
            };
            let frontend = Self::new(state);
            frontend.send_to_agent(&name, &msg).await;
        })
    }

    fn close_app(&self) -> CallbackResult {
        Box::pin(async move {
            info!("close_app callback: exiting with code 42 (restart)");
            std::process::exit(42);
        })
    }

    fn kill_process(&self) -> CallbackResult {
        Box::pin(async move {
            info!("kill_process callback: exiting with code 0");
            std::process::exit(0);
        })
    }

    fn send_goodbye(&self) -> CallbackResult {
        let state = self.state.clone();
        Box::pin(async move {
            // Send goodbye to master channel
            let master_name = &state.config.master_agent_name;
            if let Some(channel_id) = state.channel_for_agent(master_name).await {
                let _ = state
                    .discord_client
                    .send_message(channel_id.get(), "*System:* Bot shutting down...")
                    .await;
            }
        })
    }
}
