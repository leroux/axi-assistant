//! Bot-local state shared across event handlers.

use std::collections::HashMap;
use std::sync::Arc;
use std::time::Instant;

use serenity::all::ChannelId;
use serenity::prelude::TypeMapKey;
use tokio::sync::RwLock;

use axi_config::{Config, DiscordClient};
use axi_hub::AgentHub;

use crate::bridge;
use crate::channels::GuildInfrastructure;

/// Bot state stored in serenity's TypeMap.
pub struct BotState {
    pub config: Config,
    pub discord_client: DiscordClient,
    pub startup_complete: std::sync::atomic::AtomicBool,
    pub start_time: Instant,

    /// AgentHub — initialized during on_ready.
    pub hub: RwLock<Option<Arc<AgentHub>>>,

    /// Channel ID → agent name mapping, rebuilt on startup and updated on spawn/kill.
    pub channel_map: RwLock<HashMap<ChannelId, String>>,

    /// Agent name → channel ID (reverse lookup).
    pub agent_channels: RwLock<HashMap<String, ChannelId>>,

    /// Guild infrastructure (categories), set during on_ready.
    pub infra: RwLock<Option<GuildInfrastructure>>,

    /// Per-agent BridgeTransport storage.
    pub transports: bridge::TransportMap,
}

impl BotState {
    pub fn new(config: Config, discord_client: DiscordClient) -> Self {
        Self {
            config,
            discord_client,
            startup_complete: std::sync::atomic::AtomicBool::new(false),
            start_time: Instant::now(),
            hub: RwLock::new(None),
            channel_map: RwLock::new(HashMap::new()),
            agent_channels: RwLock::new(HashMap::new()),
            infra: RwLock::new(None),
            transports: bridge::new_transport_map(),
        }
    }

    /// Look up which agent owns a channel.
    pub async fn agent_for_channel(&self, channel_id: ChannelId) -> Option<String> {
        let map = self.channel_map.read().await;
        map.get(&channel_id).cloned()
    }

    /// Look up the channel for an agent.
    pub async fn channel_for_agent(&self, agent_name: &str) -> Option<ChannelId> {
        let map = self.agent_channels.read().await;
        map.get(agent_name).copied()
    }

    /// Register a channel-to-agent mapping.
    pub async fn register_channel(&self, channel_id: ChannelId, agent_name: &str) {
        let mut ch_map = self.channel_map.write().await;
        ch_map.insert(channel_id, agent_name.to_string());
        drop(ch_map);

        let mut ag_map = self.agent_channels.write().await;
        ag_map.insert(agent_name.to_string(), channel_id);
    }

    /// Remove a channel-to-agent mapping.
    pub async fn unregister_channel(&self, agent_name: &str) {
        let channel_id = {
            let ag_map = self.agent_channels.read().await;
            ag_map.get(agent_name).copied()
        };

        if let Some(ch_id) = channel_id {
            let mut ch_map = self.channel_map.write().await;
            ch_map.remove(&ch_id);
        }

        let mut ag_map = self.agent_channels.write().await;
        ag_map.remove(agent_name);
    }

    /// Get the hub, panics if not initialized.
    pub async fn hub(&self) -> Arc<AgentHub> {
        self.hub
            .read()
            .await
            .clone()
            .expect("AgentHub not initialized")
    }
}

impl TypeMapKey for BotState {
    type Value = Arc<BotState>;
}
