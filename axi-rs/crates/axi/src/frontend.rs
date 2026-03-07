//! Discord frontend — direct functions for agent lifecycle notifications.
//!
//! Each function takes `&BotState` and performs Discord REST calls directly.
//! No traits, no boxing, no indirection.

use tracing::{info, warn};

use crate::channels;
use crate::state::BotState;

/// Send a message to an agent's channel.
async fn send_to_agent(state: &BotState, agent_name: &str, text: &str) {
    if let Some(channel_id) = state.channel_for_agent(agent_name).await {
        if let Err(e) = state
            .discord_client
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

pub async fn post_message(state: &BotState, agent_name: &str, text: &str) {
    send_to_agent(state, agent_name, text).await;
}

pub async fn post_system(state: &BotState, agent_name: &str, text: &str) {
    let msg = format!("*System:* {text}");
    send_to_agent(state, agent_name, &msg).await;
}

pub async fn on_wake(state: &BotState, agent_name: &str) {
    info!("Agent '{}' woke up", agent_name);
    if state.config.channel_status_enabled {
        if let Some(channel_id) = state.channel_for_agent(agent_name).await {
            let _ = state
                .discord_client
                .edit_channel_name(
                    channel_id.get(),
                    &format!(
                        "{}-{}",
                        channels::status_emoji("working").unwrap_or(""),
                        channels::normalize_channel_name(agent_name)
                    ),
                )
                .await;
        }
    }
}

pub async fn on_sleep(state: &BotState, agent_name: &str) {
    info!("Agent '{}' went to sleep", agent_name);
    if state.config.channel_status_enabled {
        if let Some(channel_id) = state.channel_for_agent(agent_name).await {
            let _ = state
                .discord_client
                .edit_channel_name(
                    channel_id.get(),
                    &format!(
                        "{}-{}",
                        channels::status_emoji("idle").unwrap_or(""),
                        channels::normalize_channel_name(agent_name)
                    ),
                )
                .await;
        }
    }
}

pub async fn on_session_id(state: &BotState, agent_name: &str, session_id: &str) {
    info!("Agent '{}' session_id: {}", agent_name, session_id);
    if let Some(channel_id) = state.channel_for_agent(agent_name).await {
        let sessions = state.sessions.lock().await;
        if let Some(session) = sessions.get(agent_name) {
            let topic = channels::format_channel_topic(
                &session.cwd,
                Some(session_id),
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
}

pub async fn on_spawn(state: &BotState, agent_name: &str) {
    info!("Agent '{}' spawned", agent_name);
    send_to_agent(
        state,
        agent_name,
        &format!("*System:* Agent **{agent_name}** spawned."),
    )
    .await;
}

pub async fn on_kill(state: &BotState, agent_name: &str, session_id: Option<&str>) {
    let sid_text = session_id
        .map(|s| format!(" (session: `{s}`)"))
        .unwrap_or_default();
    let msg = format!("*System:* Agent **{agent_name}** killed.{sid_text}");
    send_to_agent(state, agent_name, &msg).await;

    // Move channel to Killed category
    if let Some(channel_id) = state.channel_for_agent(agent_name).await {
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
}

pub async fn broadcast(state: &BotState, message: &str) {
    let agent_channels = state.agent_channels.read().await;
    for channel_id in agent_channels.values() {
        let _ = state
            .discord_client
            .send_message(channel_id.get(), message)
            .await;
    }
}

pub async fn on_idle_reminder(state: &BotState, agent_name: &str, idle_minutes: f64) {
    let msg = format!(
        "*System:* Agent **{agent_name}** has been idle for {idle_minutes:.0} minutes."
    );
    send_to_agent(state, agent_name, &msg).await;
}

pub async fn on_reconnect(state: &BotState, agent_name: &str, was_mid_task: bool) {
    let msg = if was_mid_task {
        format!("*System:* Agent **{agent_name}** reconnected (was mid-task, resuming).")
    } else {
        format!("*System:* Agent **{agent_name}** reconnected.")
    };
    send_to_agent(state, agent_name, &msg).await;
}

pub async fn send_goodbye(state: &BotState) {
    let master_name = &state.config.master_agent_name;
    if let Some(channel_id) = state.channel_for_agent(master_name).await {
        let _ = state
            .discord_client
            .send_message(channel_id.get(), "*System:* Bot shutting down...")
            .await;
    }
}

pub fn close_app() {
    info!("close_app: exiting with code 42 (restart)");
    std::process::exit(42);
}

pub fn kill_process() {
    info!("kill_process: exiting with code 0");
    std::process::exit(0);
}
