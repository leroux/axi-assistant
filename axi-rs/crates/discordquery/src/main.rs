//! Discord message history query CLI.
//!
//! Rust equivalent of the Python `discordquery` package. Provides subcommands
//! for querying guilds, channels, message history, and searching messages.
//!
//! Usage:
//!   discordquery query guilds
//!   discordquery query channels <guild_id>
//!   discordquery query history <channel> [options]
//!   discordquery query search <guild_id> <query> [options]
//!   discordquery wait <channel_id> [options]

use std::collections::HashSet;
use std::fmt::Write;

use chrono::{DateTime, Utc};
use clap::{Parser, Subcommand};
use serde_json::{json, Value};

use axi_config::DiscordClient;

const DISCORD_EPOCH_MS: i64 = 1_420_070_400_000;
const MAX_PER_REQUEST: u32 = 100;
const DEFAULT_LIMIT: u32 = 50;
const MAX_LIMIT: u32 = 500;
const DEFAULT_MAX_SCAN: u32 = 500;
const DEFAULT_TIMEOUT: f64 = 120.0;
const POLL_INTERVAL_SECS: f64 = 2.0;

// ---------------------------------------------------------------------------
// CLI definition
// ---------------------------------------------------------------------------

#[derive(Parser)]
#[command(name = "discordquery", about = "Query Discord message history")]
struct Cli {
    #[command(subcommand)]
    command: Commands,
}

#[derive(Subcommand)]
enum Commands {
    /// Query Discord message history
    Query {
        #[command(subcommand)]
        subcommand: QuerySubcommand,
    },
    /// Wait for new messages in a channel
    Wait {
        /// Discord channel ID to watch
        channel_id: String,
        /// Wait for messages after this message ID or ISO datetime
        #[arg(long)]
        after: Option<String>,
        /// Max seconds to wait
        #[arg(long, default_value_t = DEFAULT_TIMEOUT)]
        timeout: f64,
        /// Ignore messages from these author IDs
        #[arg(long = "ignore-author-id")]
        ignore_author_ids: Vec<String>,
        /// Include system messages (default: skip *System:* messages)
        #[arg(long)]
        include_system: bool,
        /// Seconds between polls
        #[arg(long, default_value_t = POLL_INTERVAL_SECS)]
        poll_interval: f64,
        /// Don't emit cursor line at end of output
        #[arg(long)]
        no_cursor: bool,
    },
}

#[derive(Subcommand)]
enum QuerySubcommand {
    /// List guilds (servers) the bot is a member of
    Guilds,
    /// List text channels in a guild
    Channels {
        /// Discord guild (server) ID
        guild_id: String,
    },
    /// Fetch message history from a channel
    History {
        /// Channel ID, or `guild_id:channel_name`
        channel: String,
        /// Number of messages (default 50, max 500)
        #[arg(long, default_value_t = DEFAULT_LIMIT)]
        limit: u32,
        /// Fetch messages before this point (ISO datetime or snowflake ID)
        #[arg(long)]
        before: Option<String>,
        /// Fetch messages after this point (ISO datetime or snowflake ID)
        #[arg(long)]
        after: Option<String>,
        /// Output format
        #[arg(long, default_value = "jsonl")]
        format: String,
    },
    /// Search messages by content substring
    Search {
        /// Discord guild (server) ID
        guild_id: String,
        /// Search term (case-insensitive substring match)
        query: String,
        /// Limit search to this channel (ID or `guild_id:channel_name`)
        #[arg(long)]
        channel: Option<String>,
        /// Filter by author username
        #[arg(long)]
        author: Option<String>,
        /// Max results
        #[arg(long, default_value_t = DEFAULT_LIMIT)]
        limit: u32,
        /// Max messages to scan per channel
        #[arg(long, default_value_t = DEFAULT_MAX_SCAN)]
        max_scan: u32,
        /// Output format
        #[arg(long, default_value = "jsonl")]
        format: String,
    },
}

// ---------------------------------------------------------------------------
// Snowflake / datetime helpers
// ---------------------------------------------------------------------------

const fn datetime_to_snowflake(dt: &DateTime<Utc>) -> u64 {
    let ms = dt.timestamp_millis();
    ((ms - DISCORD_EPOCH_MS) as u64) << 22
}

fn resolve_snowflake(value: &str) -> u64 {
    if let Ok(id) = value.parse::<u64>() {
        return id;
    }
    if let Ok(dt) = DateTime::parse_from_rfc3339(value) {
        return datetime_to_snowflake(&dt.with_timezone(&Utc));
    }
    if let Ok(dt) = chrono::NaiveDateTime::parse_from_str(value, "%Y-%m-%dT%H:%M:%S") {
        let utc = dt.and_utc();
        return datetime_to_snowflake(&utc);
    }
    eprintln!("Error: Cannot parse '{value}' as snowflake or datetime.");
    std::process::exit(1);
}

// ---------------------------------------------------------------------------
// Message formatting
// ---------------------------------------------------------------------------

fn format_message(msg: &Value, fmt: &str) -> String {
    let ts = msg["timestamp"].as_str().unwrap_or("");
    let author = &msg["author"];
    let author_name = author["username"].as_str().unwrap_or("unknown");
    let author_id = author["id"].as_str().unwrap_or("");
    let content = msg["content"].as_str().unwrap_or("");
    let attachments = msg["attachments"].as_array().map_or(0, Vec::len);
    let embeds = msg["embeds"].as_array().map_or(0, Vec::len);

    if fmt == "text" {
        let ts_str = DateTime::parse_from_rfc3339(ts).map_or_else(|_| ts.to_string(), |dt| dt.format("%Y-%m-%d %H:%M UTC").to_string());
        let mut line = format!("[{ts_str}] {author_name}: {content}");
        let mut extras = Vec::new();
        if attachments > 0 {
            extras.push(format!("{attachments} attachment(s)"));
        }
        if embeds > 0 {
            extras.push(format!("{embeds} embed(s)"));
        }
        if !extras.is_empty() {
            let _ = write!(line, "  [{}]", extras.join(", "));
        }
        line
    } else {
        serde_json::to_string(&json!({
            "id": msg["id"].as_str().unwrap_or(""),
            "ts": ts,
            "author": author_name,
            "author_id": author_id,
            "content": content,
            "attachments": attachments,
            "embeds": embeds,
        }))
        .unwrap_or_default()
    }
}

fn format_wait_message(msg: &Value) -> String {
    let ts = msg["timestamp"].as_str().unwrap_or("");
    let author = &msg["author"];
    serde_json::to_string(&json!({
        "id": msg["id"].as_str().unwrap_or(""),
        "ts": ts,
        "author": author["username"].as_str().unwrap_or("unknown"),
        "author_id": author["id"].as_str().unwrap_or(""),
        "content": msg["content"].as_str().unwrap_or(""),
    }))
    .unwrap_or_default()
}

fn is_system_message(msg: &Value) -> bool {
    msg["content"]
        .as_str()
        .is_some_and(|c| c.starts_with("*System:*"))
}

// ---------------------------------------------------------------------------
// Channel resolution
// ---------------------------------------------------------------------------

async fn resolve_channel(client: &DiscordClient, channel_arg: &str) -> u64 {
    if let Ok(id) = channel_arg.parse::<u64>() {
        return id;
    }

    let Some((guild_str, channel_name)) = channel_arg.split_once(':') else {
        eprintln!(
            "Error: '{channel_arg}' is not a valid channel ID or guild_id:channel_name pair."
        );
        std::process::exit(1);
    };

    let guild_id: u64 = guild_str.parse().unwrap_or_else(|_| {
        eprintln!("Error: '{guild_str}' is not a valid guild ID.");
        std::process::exit(1);
    });

    let channels = client
        .get(&format!("/guilds/{guild_id}/channels"))
        .await
        .unwrap_or_else(|e| {
            eprintln!("Error fetching channels: {e}");
            std::process::exit(1);
        });

    let lower_name = channel_name.to_lowercase();
    for ch in channels.as_array().unwrap_or(&Vec::new()) {
        let ch_type = ch["type"].as_u64().unwrap_or(0);
        if (ch_type == 0 || ch_type == 5)
            && ch["name"]
                .as_str()
                .unwrap_or("")
                .to_lowercase()
                == lower_name
        {
            return ch["id"]
                .as_str()
                .and_then(|s| s.parse().ok())
                .unwrap_or(0);
        }
    }

    eprintln!(
        "Error: No text channel named '{channel_name}' in guild {guild_id}."
    );
    std::process::exit(1);
}

// ---------------------------------------------------------------------------
// Query commands
// ---------------------------------------------------------------------------

async fn cmd_guilds(client: &DiscordClient) {
    let guilds = client.list_guilds().await.unwrap_or_else(|e| {
        eprintln!("Error: {e}");
        std::process::exit(1);
    });

    for g in guilds.as_array().unwrap_or(&Vec::new()) {
        println!(
            "{}",
            serde_json::to_string(&json!({
                "id": g["id"].as_str().unwrap_or(""),
                "name": g["name"].as_str().unwrap_or(""),
            }))
            .unwrap_or_default()
        );
    }
}

async fn cmd_channels(client: &DiscordClient, guild_id: &str) {
    let gid: u64 = guild_id.parse().unwrap_or_else(|_| {
        eprintln!("Error: Invalid guild ID.");
        std::process::exit(1);
    });

    let channels = client.list_channels(gid).await.unwrap_or_else(|e| {
        eprintln!("Error: {e}");
        std::process::exit(1);
    });

    for ch in &channels {
        println!("{}", serde_json::to_string(ch).unwrap_or_default());
    }
}

async fn cmd_history(
    client: &DiscordClient,
    channel: &str,
    limit: u32,
    before: Option<&str>,
    after: Option<&str>,
    fmt: &str,
) {
    let channel_id = resolve_channel(client, channel).await;
    let limit = limit.min(MAX_LIMIT);

    let mut collected: u32 = 0;
    let mut before_id = before.map(resolve_snowflake);
    let mut after_id = after.map(resolve_snowflake);
    let use_after = after_id.is_some();
    let mut messages: Vec<Value> = Vec::new();

    while collected < limit {
        let batch_size = MAX_PER_REQUEST.min(limit - collected);
        let result = client
            .get_messages(channel_id, batch_size, before_id, after_id)
            .await
            .unwrap_or_else(|e| {
                eprintln!("Error: {e}");
                std::process::exit(1);
            });

        let batch = result.as_array().cloned().unwrap_or_default();
        if batch.is_empty() {
            break;
        }

        collected += batch.len() as u32;

        if use_after {
            after_id = batch.last().and_then(|m| m["id"].as_str()).and_then(|s| s.parse().ok());
        } else {
            before_id = batch.last().and_then(|m| m["id"].as_str()).and_then(|s| s.parse().ok());
        }

        messages.extend(batch.iter().cloned());

        if (batch.len() as u32) < batch_size {
            break;
        }
    }

    for msg in &messages {
        println!("{}", format_message(msg, fmt));
    }
}

#[allow(clippy::too_many_arguments)]
async fn cmd_search(
    client: &DiscordClient,
    guild_id: &str,
    query: &str,
    channel: Option<&str>,
    author: Option<&str>,
    limit: u32,
    max_scan: u32,
    fmt: &str,
) {
    let gid: u64 = guild_id.parse().unwrap_or_else(|_| {
        eprintln!("Error: Invalid guild ID.");
        std::process::exit(1);
    });

    let limit = limit.min(MAX_LIMIT);
    let query_lower = query.to_lowercase();
    let author_lower = author.map(str::to_lowercase);

    let channel_ids: Vec<u64> = if let Some(ch) = channel {
        vec![resolve_channel(client, ch).await]
    } else {
        let channels = client.list_channels(gid).await.unwrap_or_else(|e| {
            eprintln!("Error: {e}");
            std::process::exit(1);
        });
        channels
            .iter()
            .filter_map(|ch| ch["id"].as_str().and_then(|s| s.parse().ok()))
            .collect()
    };

    let mut found: u32 = 0;

    for ch_id in channel_ids {
        if found >= limit {
            break;
        }

        let mut scanned: u32 = 0;
        let mut before_id: Option<u64> = None;

        while scanned < max_scan && found < limit {
            let batch_size = MAX_PER_REQUEST.min(max_scan - scanned);
            let result = match client
                .get_messages(ch_id, batch_size, before_id, None)
                .await
            {
                Ok(r) => r,
                Err(_) => break,
            };

            let batch = result.as_array().cloned().unwrap_or_default();
            if batch.is_empty() {
                break;
            }

            for msg in &batch {
                let content = msg["content"]
                    .as_str()
                    .unwrap_or("")
                    .to_lowercase();
                let author_name = msg["author"]["username"]
                    .as_str()
                    .unwrap_or("")
                    .to_lowercase();

                if content.contains(&query_lower) {
                    if let Some(ref af) = author_lower {
                        if !author_name.contains(af.as_str()) {
                            continue;
                        }
                    }
                    println!("{}", format_message(msg, fmt));
                    found += 1;
                    if found >= limit {
                        break;
                    }
                }
            }

            scanned += batch.len() as u32;
            if (batch.len() as u32) < batch_size {
                break;
            }
            before_id = batch.last().and_then(|m| m["id"].as_str()).and_then(|s| s.parse().ok());
        }
    }

    if found == 0 {
        eprintln!("No messages found.");
    }
}

// ---------------------------------------------------------------------------
// Wait command
// ---------------------------------------------------------------------------

#[allow(clippy::too_many_arguments)]
async fn cmd_wait(
    client: &DiscordClient,
    channel_id_str: &str,
    after: Option<&str>,
    timeout: f64,
    ignore_author_ids: &HashSet<String>,
    ignore_system: bool,
    poll_interval: f64,
    no_cursor: bool,
) {
    let channel_id: u64 = channel_id_str.parse().unwrap_or_else(|_| {
        eprintln!("Error: Invalid channel ID.");
        std::process::exit(1);
    });

    let mut after_id: u64 = if let Some(a) = after {
        resolve_snowflake(a)
    } else {
        // Get latest message as baseline
        let result = client
            .get_messages(channel_id, 1, None, None)
            .await
            .unwrap_or_else(|e| {
                eprintln!("Error: {e}");
                std::process::exit(1);
            });
        let msgs = result.as_array().cloned().unwrap_or_default();
        if msgs.is_empty() {
            eprintln!("Error: Channel has no messages.");
            std::process::exit(1);
        }
        msgs[0]["id"]
            .as_str()
            .and_then(|s| s.parse().ok())
            .unwrap_or(0)
    };

    let deadline = tokio::time::Instant::now()
        + std::time::Duration::from_secs_f64(timeout);
    let mut cursor = after_id;

    loop {
        if tokio::time::Instant::now() >= deadline {
            break;
        }

        let result = client
            .get_messages(channel_id, 100, None, Some(after_id))
            .await
            .unwrap_or(Value::Array(Vec::new()));

        let messages = result.as_array().cloned().unwrap_or_default();

        if !messages.is_empty() {
            // Track highest ID (newest first)
            if let Some(id) = messages[0]["id"]
                .as_str()
                .and_then(|s| s.parse::<u64>().ok())
            {
                cursor = id;
            }

            // Filter and collect in chronological order
            let mut matching: Vec<&Value> = Vec::new();
            for msg in messages.iter().rev() {
                let author_id = msg["author"]["id"]
                    .as_str()
                    .unwrap_or("");
                if ignore_author_ids.contains(author_id) {
                    continue;
                }
                if ignore_system && is_system_message(msg) {
                    continue;
                }
                matching.push(msg);
            }

            if !matching.is_empty() {
                for msg in &matching {
                    println!("{}", format_wait_message(msg));
                }
                if !no_cursor {
                    println!("{}", serde_json::to_string(&json!({"cursor": cursor.to_string()})).unwrap_or_default());
                }
                return;
            }

            // All filtered — advance baseline
            after_id = cursor;
        }

        let remaining = deadline - tokio::time::Instant::now();
        if remaining.is_zero() {
            break;
        }
        tokio::time::sleep(std::time::Duration::from_secs_f64(
            poll_interval.min(remaining.as_secs_f64()),
        ))
        .await;
    }

    // Timed out
    if !no_cursor {
        println!(
            "{}",
            serde_json::to_string(&json!({"cursor": cursor.to_string()})).unwrap_or_default()
        );
    }
    eprintln!("Error: Timed out waiting for message.");
    std::process::exit(2);
}

// ---------------------------------------------------------------------------
// Main
// ---------------------------------------------------------------------------

#[tokio::main]
async fn main() {
    dotenvy::dotenv().ok();

    let token = std::env::var("DISCORD_TOKEN").unwrap_or_else(|_| {
        eprintln!("Error: DISCORD_TOKEN environment variable not set.");
        std::process::exit(1);
    });

    let client = DiscordClient::new(&token);
    let cli = Cli::parse();

    match cli.command {
        Commands::Query { subcommand } => match subcommand {
            QuerySubcommand::Guilds => cmd_guilds(&client).await,
            QuerySubcommand::Channels { guild_id } => {
                cmd_channels(&client, &guild_id).await;
            }
            QuerySubcommand::History {
                channel,
                limit,
                before,
                after,
                format,
            } => {
                cmd_history(
                    &client,
                    &channel,
                    limit,
                    before.as_deref(),
                    after.as_deref(),
                    &format,
                )
                .await;
            }
            QuerySubcommand::Search {
                guild_id,
                query,
                channel,
                author,
                limit,
                max_scan,
                format,
            } => {
                cmd_search(
                    &client,
                    &guild_id,
                    &query,
                    channel.as_deref(),
                    author.as_deref(),
                    limit,
                    max_scan,
                    &format,
                )
                .await;
            }
        },
        Commands::Wait {
            channel_id,
            after,
            timeout,
            ignore_author_ids,
            include_system,
            poll_interval,
            no_cursor,
        } => {
            let ignore_set: HashSet<String> = ignore_author_ids.into_iter().collect();
            cmd_wait(
                &client,
                &channel_id,
                after.as_deref(),
                timeout,
                &ignore_set,
                !include_system,
                poll_interval,
                no_cursor,
            )
            .await;
        }
    }
}

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn snowflake_roundtrip() {
        // Known Discord epoch: 2015-01-01T00:00:00Z
        let dt = DateTime::parse_from_rfc3339("2025-01-01T00:00:00Z")
            .unwrap()
            .with_timezone(&Utc);
        let sf = datetime_to_snowflake(&dt);
        assert!(sf > 0);
    }

    #[test]
    fn resolve_snowflake_id() {
        assert_eq!(resolve_snowflake("123456789"), 123456789);
    }

    #[test]
    fn format_jsonl() {
        let msg = json!({
            "id": "123",
            "timestamp": "2025-01-01T00:00:00+00:00",
            "author": {"username": "test", "id": "456"},
            "content": "hello world",
            "attachments": [],
            "embeds": [],
        });
        let out = format_message(&msg, "jsonl");
        let parsed: Value = serde_json::from_str(&out).unwrap();
        assert_eq!(parsed["author"], "test");
        assert_eq!(parsed["content"], "hello world");
    }

    #[test]
    fn format_text() {
        let msg = json!({
            "id": "123",
            "timestamp": "2025-01-01T00:00:00+00:00",
            "author": {"username": "test", "id": "456"},
            "content": "hello",
            "attachments": [{}],
            "embeds": [],
        });
        let out = format_message(&msg, "text");
        assert!(out.contains("test: hello"));
        assert!(out.contains("1 attachment(s)"));
    }

    #[test]
    fn system_message_detection() {
        let sys = json!({"content": "*System:* Bot started."});
        let normal = json!({"content": "hello"});
        assert!(is_system_message(&sys));
        assert!(!is_system_message(&normal));
    }
}
