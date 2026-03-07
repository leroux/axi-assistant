//! Discord channel and guild management.
//!
//! Manages channel topic helpers, guild infrastructure, channel lifecycle,
//! and status prefixes. Mirrors the Python `channels.py` module.

use std::collections::HashMap;
use std::sync::LazyLock;

use serenity::all::{ChannelId, ChannelType, CreateChannel, EditChannel, GuildChannel, GuildId};
use serenity::client::Context;
use tracing::{info, warn};

use axi_config::Config;

// ---------------------------------------------------------------------------
// Constants
// ---------------------------------------------------------------------------

/// Category names used for organizing agent channels.
const AXI_CATEGORY_NAME: &str = "Axi";
const ACTIVE_CATEGORY_NAME: &str = "Active";
const KILLED_CATEGORY_NAME: &str = "Killed";

// ---------------------------------------------------------------------------
// Channel status prefixes
// ---------------------------------------------------------------------------

/// Status prefix mapping: state -> emoji.
static STATUS_PREFIXES: LazyLock<HashMap<&'static str, &'static str>> = LazyLock::new(|| {
    let mut m = HashMap::new();
    m.insert("working", "\u{26a1}");
    m.insert("plan_review", "\u{1f4cb}");
    m.insert("question", "\u{2753}");
    m.insert("done", "\u{2705}");
    m.insert("idle", "\u{1f4a4}");
    m.insert("error", "\u{26a0}\u{fe0f}");
    m.insert("custom", "\u{1f527}");
    m
});

/// Get the emoji prefix for a given status.
pub fn status_emoji(status: &str) -> Option<&'static str> {
    STATUS_PREFIXES.get(status).copied()
}

/// Strip status emoji prefix from a channel name for matching.
pub fn strip_status_prefix(name: &str) -> &str {
    for emoji in STATUS_PREFIXES.values() {
        let prefix = format!("{emoji}-");
        if let Some(stripped) = name.strip_prefix(&prefix) {
            return stripped;
        }
    }
    name
}

// ---------------------------------------------------------------------------
// Pure helpers
// ---------------------------------------------------------------------------

/// Normalize an agent name to a valid Discord channel name.
pub fn normalize_channel_name(name: &str) -> String {
    let name = name.to_lowercase().replace(' ', "-");
    let name: String = name
        .chars()
        .filter(|c| c.is_ascii_alphanumeric() || *c == '-' || *c == '_')
        .collect();
    name.chars().take(100).collect()
}

/// Format agent metadata for a Discord channel topic.
pub fn format_channel_topic(
    cwd: &str,
    session_id: Option<&str>,
    prompt_hash: Option<&str>,
    agent_type: Option<&str>,
) -> String {
    let mut parts = vec![format!("cwd: {}", cwd)];
    if let Some(sid) = session_id {
        parts.push(format!("session: {sid}"));
    }
    if let Some(hash) = prompt_hash {
        parts.push(format!("prompt_hash: {hash}"));
    }
    if let Some(atype) = agent_type {
        if atype != "flowcoder" {
            parts.push(format!("type: {atype}"));
        }
    }
    parts.join(" | ")
}

/// Parse cwd, `session_id`, `prompt_hash`, and `agent_type` from a channel topic.
pub fn parse_channel_topic(
    topic: Option<&str>,
) -> (
    Option<String>,
    Option<String>,
    Option<String>,
    Option<String>,
) {
    let topic = match topic {
        Some(t) if !t.is_empty() => t,
        _ => return (None, None, None, None),
    };

    let mut cwd = None;
    let mut session_id = None;
    let mut prompt_hash = None;
    let mut agent_type = None;

    for part in topic.split('|') {
        let part = part.trim();
        if let Some((key, value)) = part.split_once(": ") {
            let value = value.trim().to_string();
            match key.trim() {
                "cwd" => cwd = Some(value),
                "session" => session_id = Some(value),
                "prompt_hash" => prompt_hash = Some(value),
                "type" => agent_type = Some(value),
                _ => {}
            }
        }
    }

    (cwd, session_id, prompt_hash, agent_type)
}

/// Check if a channel name matches a normalized agent name, ignoring status prefix.
pub fn match_channel_name(ch_name: &str, normalized: &str, status_enabled: bool) -> bool {
    if ch_name == normalized {
        return true;
    }
    if status_enabled {
        return strip_status_prefix(ch_name) == normalized;
    }
    false
}

// ---------------------------------------------------------------------------
// Guild infrastructure
// ---------------------------------------------------------------------------

/// Category IDs discovered during guild setup.
#[derive(Debug, Clone, Default)]
pub struct GuildInfrastructure {
    pub guild_id: GuildId,
    pub axi_category_id: Option<ChannelId>,
    pub active_category_id: Option<ChannelId>,
    pub killed_category_id: Option<ChannelId>,
}

/// Ensure all required categories exist in the guild.
pub async fn ensure_guild_infrastructure(
    ctx: &Context,
    config: &Config,
) -> anyhow::Result<GuildInfrastructure> {
    let guild_id = GuildId::new(config.discord_guild_id);

    let channels = guild_id.channels(&ctx.http).await?;

    let mut infra = GuildInfrastructure {
        guild_id,
        ..Default::default()
    };

    // Find existing categories
    for (id, channel) in &channels {
        if channel.kind == ChannelType::Category {
            match channel.name.as_str() {
                name if name == AXI_CATEGORY_NAME => infra.axi_category_id = Some(*id),
                name if name == ACTIVE_CATEGORY_NAME => infra.active_category_id = Some(*id),
                name if name == KILLED_CATEGORY_NAME => infra.killed_category_id = Some(*id),
                _ => {}
            }
        }
    }

    // Create missing categories
    if infra.axi_category_id.is_none() {
        let cat = guild_id
            .create_channel(
                &ctx.http,
                CreateChannel::new(AXI_CATEGORY_NAME).kind(ChannelType::Category),
            )
            .await?;
        info!("Created category: {}", AXI_CATEGORY_NAME);
        infra.axi_category_id = Some(cat.id);
    }

    if infra.active_category_id.is_none() {
        let cat = guild_id
            .create_channel(
                &ctx.http,
                CreateChannel::new(ACTIVE_CATEGORY_NAME).kind(ChannelType::Category),
            )
            .await?;
        info!("Created category: {}", ACTIVE_CATEGORY_NAME);
        infra.active_category_id = Some(cat.id);
    }

    if infra.killed_category_id.is_none() {
        let cat = guild_id
            .create_channel(
                &ctx.http,
                CreateChannel::new(KILLED_CATEGORY_NAME).kind(ChannelType::Category),
            )
            .await?;
        info!("Created category: {}", KILLED_CATEGORY_NAME);
        infra.killed_category_id = Some(cat.id);
    }

    info!(
        "Guild infrastructure ready: axi={:?}, active={:?}, killed={:?}",
        infra.axi_category_id, infra.active_category_id, infra.killed_category_id
    );

    Ok(infra)
}

/// Find or create an agent's text channel.
pub async fn ensure_agent_channel(
    ctx: &Context,
    guild_id: GuildId,
    agent_name: &str,
    category_id: Option<ChannelId>,
    status_enabled: bool,
) -> anyhow::Result<GuildChannel> {
    let normalized = normalize_channel_name(agent_name);
    let channels = guild_id.channels(&ctx.http).await?;

    // Look for existing channel
    for channel in channels.values() {
        if channel.kind == ChannelType::Text
            && match_channel_name(&channel.name, &normalized, status_enabled)
        {
            return Ok(channel.clone());
        }
    }

    // Create new channel
    let mut builder = CreateChannel::new(&normalized).kind(ChannelType::Text);
    if let Some(cat_id) = category_id {
        builder = builder.category(cat_id);
    }

    let channel = guild_id.create_channel(&ctx.http, builder).await?;
    info!("Created channel #{} for agent '{}'", normalized, agent_name);
    Ok(channel)
}

/// Move a channel to the Killed category.
pub async fn move_channel_to_killed(
    ctx: &Context,
    channel_id: ChannelId,
    killed_category_id: ChannelId,
) -> anyhow::Result<()> {
    channel_id
        .edit(
            &ctx.http,
            EditChannel::new().category(Some(killed_category_id)),
        )
        .await?;
    info!(
        "Moved channel {} to Killed category",
        channel_id
    );
    Ok(())
}

/// Update channel name with a status prefix.
pub async fn set_channel_status(
    ctx: &Context,
    channel_id: ChannelId,
    agent_name: &str,
    status: &str,
) -> anyhow::Result<()> {
    let normalized = normalize_channel_name(agent_name);
    let new_name = if let Some(emoji) = status_emoji(status) {
        format!("{emoji}-{normalized}")
    } else {
        normalized
    };

    if let Err(e) = channel_id
        .edit(&ctx.http, EditChannel::new().name(&new_name))
        .await
    {
        warn!(
            "Failed to set channel status for #{}: {}",
            agent_name, e
        );
    }

    Ok(())
}

/// Reconstruct channel-to-agent mapping from guild channels.
pub async fn reconstruct_channel_map(
    ctx: &Context,
    guild_id: GuildId,
    active_category_id: Option<ChannelId>,
    axi_category_id: Option<ChannelId>,
    killed_category_id: Option<ChannelId>,
    status_enabled: bool,
) -> anyhow::Result<HashMap<ChannelId, String>> {
    let mut map = HashMap::new();
    let channels = guild_id.channels(&ctx.http).await?;

    for (id, channel) in &channels {
        if channel.kind != ChannelType::Text {
            continue;
        }

        // Only process channels in our categories
        let in_managed_category = channel.parent_id.is_some_and(|parent| {
            Some(parent) == active_category_id
                || Some(parent) == axi_category_id
                || Some(parent) == killed_category_id
        });

        if !in_managed_category {
            continue;
        }

        let agent_name = if status_enabled {
            strip_status_prefix(&channel.name).to_string()
        } else {
            channel.name.clone()
        };

        map.insert(*id, agent_name);
    }

    info!("Reconstructed {} channel-to-agent mappings", map.len());
    Ok(map)
}

/// Reorder a channel to position 0 within its category (most recent on top).
///
/// This is debounced by the caller — only called when an agent becomes active.
pub async fn mark_channel_active(
    discord_client: &axi_config::DiscordClient,
    channel_id: u64,
) {
    if let Err(e) = discord_client.edit_channel_position(channel_id, 0).await {
        warn!("Failed to reorder channel {}: {}", channel_id, e);
    }
}

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_normalize_channel_name() {
        assert_eq!(normalize_channel_name("My Agent"), "my-agent");
        assert_eq!(normalize_channel_name("test_123"), "test_123");
        assert_eq!(normalize_channel_name("hello@world!"), "helloworld");
    }

    #[test]
    fn test_format_parse_topic() {
        let topic = format_channel_topic(
            "/home/user/project",
            Some("abc123"),
            Some("hash456"),
            None,
        );
        assert_eq!(
            topic,
            "cwd: /home/user/project | session: abc123 | prompt_hash: hash456"
        );

        let (cwd, sid, hash, atype) = parse_channel_topic(Some(&topic));
        assert_eq!(cwd.as_deref(), Some("/home/user/project"));
        assert_eq!(sid.as_deref(), Some("abc123"));
        assert_eq!(hash.as_deref(), Some("hash456"));
        assert_eq!(atype, None);
    }

    #[test]
    fn test_parse_empty_topic() {
        let (cwd, sid, hash, atype) = parse_channel_topic(None);
        assert!(cwd.is_none());
        assert!(sid.is_none());
        assert!(hash.is_none());
        assert!(atype.is_none());
    }

    #[test]
    fn test_strip_status_prefix() {
        // No prefix
        assert_eq!(strip_status_prefix("my-agent"), "my-agent");
        // With prefix
        assert_eq!(strip_status_prefix("\u{26a1}-my-agent"), "my-agent");
        assert_eq!(strip_status_prefix("\u{1f4a4}-sleeping-agent"), "sleeping-agent");
    }

    #[test]
    fn test_match_channel_name() {
        assert!(match_channel_name("my-agent", "my-agent", false));
        assert!(!match_channel_name("other", "my-agent", false));
        assert!(match_channel_name("\u{26a1}-my-agent", "my-agent", true));
        assert!(!match_channel_name("\u{26a1}-my-agent", "my-agent", false));
    }
}
