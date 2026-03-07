//! Bot-local state shared across event handlers.

use std::sync::Arc;
use std::time::Instant;

use serenity::prelude::TypeMapKey;

use axi_config::{Config, DiscordClient};

/// Bot state stored in serenity's TypeMap.
pub struct BotState {
    pub config: Config,
    pub discord_client: DiscordClient,
    pub startup_complete: std::sync::atomic::AtomicBool,
    pub start_time: Instant,
}

impl BotState {
    pub fn new(config: Config, discord_client: DiscordClient) -> Self {
        Self {
            config,
            discord_client,
            startup_complete: std::sync::atomic::AtomicBool::new(false),
            start_time: Instant::now(),
        }
    }
}

impl TypeMapKey for BotState {
    type Value = Arc<BotState>;
}
