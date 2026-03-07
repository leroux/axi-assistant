//! Standalone async Discord REST client with rate-limit and retry handling.
//!
//! This exists alongside serenity's built-in `ctx.http` by design:
//!
//! - **Serenity `ctx.http`**: Required for interaction responses (`create_response`)
//!   and serenity model methods (`guild_id.channels()`) that return typed structs.
//!   Only available inside serenity event handlers.
//!
//! - **`DiscordClient`** (this module): Used everywhere serenity's Context is
//!   unavailable — the bridge/streaming layer, MCP tool servers, frontend
//!   callbacks, and the standalone `discordquery` CLI binary (critical for the
//!   test instance system: `axi_test.py msg`, `discordquery wait`).

use std::collections::HashMap;

use reqwest::multipart;
use serde_json::Value;
use tracing::warn;

const API_BASE: &str = "https://discord.com/api/v10";
const MAX_RETRIES: u32 = 3;

#[derive(Debug, thiserror::Error)]
pub enum DiscordError {
    #[error("HTTP error: {0}")]
    Http(#[from] reqwest::Error),
    #[error("Discord API error {status}: {body}")]
    Api { status: u16, body: String },
    #[error("Retries exhausted")]
    RetriesExhausted,
}

#[derive(Clone)]
pub struct DiscordClient {
    client: reqwest::Client,
    base_url: String,
}

impl DiscordClient {
    pub fn new(token: &str) -> Self {
        Self::with_base_url(token, API_BASE.to_string())
    }

    /// Create a client pointing at a custom base URL.
    ///
    /// Used for integration tests — point at a local HTTP server
    /// instead of the real Discord API.
    pub fn with_base_url(token: &str, base_url: String) -> Self {
        let client = reqwest::Client::builder()
            .timeout(std::time::Duration::from_secs(15))
            .default_headers({
                let mut headers = reqwest::header::HeaderMap::new();
                headers.insert(
                    reqwest::header::AUTHORIZATION,
                    reqwest::header::HeaderValue::from_str(&format!("Bot {token}"))
                        .expect("invalid token"),
                );
                headers
            })
            .build()
            .expect("failed to build HTTP client");

        Self { client, base_url }
    }

    /// Make a Discord API request with rate-limit and retry handling.
    pub async fn request(
        &self,
        method: reqwest::Method,
        path: &str,
        body: Option<Value>,
    ) -> Result<reqwest::Response, DiscordError> {
        let url = format!("{}{}", self.base_url, path);

        for attempt in 0..=MAX_RETRIES {
            let mut req = self.client.request(method.clone(), &url);
            if let Some(ref b) = body {
                req = req.json(b);
            }

            let resp = req.send().await?;
            let status = resp.status().as_u16();

            if status == 200 || status == 201 || status == 204 {
                return Ok(resp);
            }

            if status == 429 {
                let retry_body = resp.json::<Value>().await.unwrap_or_default();
                let retry_after = retry_body
                    .get("retry_after")
                    .and_then(Value::as_f64)
                    .unwrap_or(1.0);
                warn!(
                    "Rate limited on {} {}, waiting {:.1}s...",
                    method, path, retry_after
                );
                tokio::time::sleep(std::time::Duration::from_secs_f64(retry_after)).await;
                continue;
            }

            if status >= 500 && attempt < MAX_RETRIES {
                let wait = 1u64 << attempt;
                warn!(
                    "Server error {} on {} {}, retrying in {}s...",
                    status, method, path, wait
                );
                tokio::time::sleep(std::time::Duration::from_secs(wait)).await;
                continue;
            }

            let body_text = resp.text().await.unwrap_or_default();
            return Err(DiscordError::Api {
                status,
                body: body_text,
            });
        }

        Err(DiscordError::RetriesExhausted)
    }

    /// GET request, returning parsed JSON.
    pub async fn get(&self, path: &str) -> Result<Value, DiscordError> {
        let resp = self.request(reqwest::Method::GET, path, None).await?;
        Ok(resp.json().await?)
    }

    /// GET request with query parameters.
    pub async fn get_with_params(
        &self,
        path: &str,
        params: &[(&str, String)],
    ) -> Result<Value, DiscordError> {
        let url = format!("{}{}", self.base_url, path);
        for attempt in 0..=MAX_RETRIES {
            let resp = self
                .client
                .get(&url)
                .query(params)
                .send()
                .await?;
            let status = resp.status().as_u16();

            if status == 200 || status == 201 || status == 204 {
                return Ok(resp.json().await?);
            }
            if status == 429 {
                let retry_body = resp.json::<Value>().await.unwrap_or_default();
                let retry_after = retry_body
                    .get("retry_after")
                    .and_then(Value::as_f64)
                    .unwrap_or(1.0);
                tokio::time::sleep(std::time::Duration::from_secs_f64(retry_after)).await;
                continue;
            }
            if status >= 500 && attempt < MAX_RETRIES {
                tokio::time::sleep(std::time::Duration::from_secs(1u64 << attempt)).await;
                continue;
            }
            let body_text = resp.text().await.unwrap_or_default();
            return Err(DiscordError::Api {
                status,
                body: body_text,
            });
        }
        Err(DiscordError::RetriesExhausted)
    }

    /// POST request with JSON body, returning parsed JSON.
    pub async fn post(&self, path: &str, body: Value) -> Result<Value, DiscordError> {
        let resp = self.request(reqwest::Method::POST, path, Some(body)).await?;
        Ok(resp.json().await?)
    }

    /// PATCH request with JSON body, returning parsed JSON.
    pub async fn patch(&self, path: &str, body: Value) -> Result<Value, DiscordError> {
        let resp = self
            .request(reqwest::Method::PATCH, path, Some(body))
            .await?;
        Ok(resp.json().await?)
    }

    /// DELETE request.
    pub async fn delete(&self, path: &str) -> Result<(), DiscordError> {
        self.request(reqwest::Method::DELETE, path, None).await?;
        Ok(())
    }

    /// PUT request (for reactions etc.)
    pub async fn put(&self, path: &str) -> Result<(), DiscordError> {
        self.request(reqwest::Method::PUT, path, None).await?;
        Ok(())
    }

    // -----------------------------------------------------------------------
    // High-level: Guilds
    // -----------------------------------------------------------------------

    pub async fn list_guilds(&self) -> Result<Value, DiscordError> {
        self.get("/users/@me/guilds").await
    }

    pub async fn list_channels(&self, guild_id: u64) -> Result<Vec<Value>, DiscordError> {
        let channels: Vec<Value> = self
            .get(&format!("/guilds/{guild_id}/channels"))
            .await?
            .as_array()
            .cloned()
            .unwrap_or_default();

        let categories: HashMap<String, String> = channels
            .iter()
            .filter(|c| c.get("type").and_then(Value::as_u64) == Some(4))
            .filter_map(|c| {
                let id = c.get("id")?.as_str()?.to_string();
                let name = c.get("name")?.as_str()?.to_string();
                Some((id, name))
            })
            .collect();

        let mut result: Vec<Value> = channels
            .iter()
            .filter(|c| {
                let t = c.get("type").and_then(Value::as_u64).unwrap_or(0);
                t == 0 || t == 5
            })
            .map(|ch| {
                let ch_type = if ch.get("type").and_then(Value::as_u64) == Some(5) {
                    "announcement"
                } else {
                    "text"
                };
                let category = ch
                    .get("parent_id")
                    .and_then(|p| p.as_str())
                    .and_then(|pid| categories.get(pid))
                    .cloned();

                serde_json::json!({
                    "id": ch.get("id").and_then(|v| v.as_str()).unwrap_or(""),
                    "name": ch.get("name").and_then(|v| v.as_str()).unwrap_or(""),
                    "type": ch_type,
                    "category": category,
                    "position": ch.get("position").and_then(Value::as_u64).unwrap_or(0),
                })
            })
            .collect();

        result.sort_by_key(|c| c.get("position").and_then(Value::as_u64).unwrap_or(0));
        Ok(result)
    }

    // -----------------------------------------------------------------------
    // High-level: Messages
    // -----------------------------------------------------------------------

    pub async fn get_messages(
        &self,
        channel_id: u64,
        limit: u32,
        before: Option<u64>,
        after: Option<u64>,
    ) -> Result<Value, DiscordError> {
        let mut params: Vec<(&str, String)> = vec![("limit", limit.min(100).to_string())];
        if let Some(b) = before {
            params.push(("before", b.to_string()));
        }
        if let Some(a) = after {
            params.push(("after", a.to_string()));
        }
        self.get_with_params(&format!("/channels/{channel_id}/messages"), &params)
            .await
    }

    pub async fn send_message(
        &self,
        channel_id: u64,
        content: &str,
    ) -> Result<Value, DiscordError> {
        self.post(
            &format!("/channels/{channel_id}/messages"),
            serde_json::json!({ "content": content }),
        )
        .await
    }

    pub async fn edit_message(
        &self,
        channel_id: u64,
        message_id: u64,
        content: &str,
    ) -> Result<Value, DiscordError> {
        self.patch(
            &format!("/channels/{channel_id}/messages/{message_id}"),
            serde_json::json!({ "content": content }),
        )
        .await
    }

    pub async fn delete_message(
        &self,
        channel_id: u64,
        message_id: u64,
    ) -> Result<(), DiscordError> {
        self.delete(&format!(
            "/channels/{channel_id}/messages/{message_id}"
        ))
        .await
    }

    pub async fn send_file(
        &self,
        channel_id: u64,
        filename: &str,
        file_data: Vec<u8>,
        content: Option<&str>,
    ) -> Result<Value, DiscordError> {
        let file_part = multipart::Part::bytes(file_data).file_name(filename.to_string());
        let mut form = multipart::Form::new().part("files[0]", file_part);
        if let Some(c) = content {
            form = form.text("content", c.to_string());
        }

        let url = format!("{}/channels/{}/messages", self.base_url, channel_id);
        let resp = self.client.post(&url).multipart(form).send().await?;
        let status = resp.status().as_u16();
        if status == 200 || status == 201 {
            Ok(resp.json().await?)
        } else {
            let body = resp.text().await.unwrap_or_default();
            Err(DiscordError::Api {
                status,
                body,
            })
        }
    }

    // -----------------------------------------------------------------------
    // High-level: Reactions
    // -----------------------------------------------------------------------

    pub async fn add_reaction(
        &self,
        channel_id: u64,
        message_id: u64,
        emoji: &str,
    ) -> Result<(), DiscordError> {
        let encoded = urlencoding::encode(emoji);
        self.put(&format!(
            "/channels/{channel_id}/messages/{message_id}/reactions/{encoded}/@me"
        ))
        .await
    }

    pub async fn remove_reaction(
        &self,
        channel_id: u64,
        message_id: u64,
        emoji: &str,
    ) -> Result<(), DiscordError> {
        let encoded = urlencoding::encode(emoji);
        self.delete(&format!(
            "/channels/{channel_id}/messages/{message_id}/reactions/{encoded}/@me"
        ))
        .await
    }

    // -----------------------------------------------------------------------
    // High-level: Channels
    // -----------------------------------------------------------------------

    pub async fn create_channel(
        &self,
        guild_id: u64,
        name: &str,
        channel_type: u32,
    ) -> Result<Value, DiscordError> {
        self.post(
            &format!("/guilds/{guild_id}/channels"),
            serde_json::json!({ "name": name, "type": channel_type }),
        )
        .await
    }

    pub async fn edit_channel_name(
        &self,
        channel_id: u64,
        name: &str,
    ) -> Result<Value, DiscordError> {
        self.patch(
            &format!("/channels/{channel_id}"),
            serde_json::json!({ "name": name }),
        )
        .await
    }

    pub async fn edit_channel_topic(
        &self,
        channel_id: u64,
        topic: &str,
    ) -> Result<Value, DiscordError> {
        self.patch(
            &format!("/channels/{channel_id}"),
            serde_json::json!({ "topic": topic }),
        )
        .await
    }

    pub async fn edit_channel_category(
        &self,
        channel_id: u64,
        category_id: u64,
    ) -> Result<Value, DiscordError> {
        self.patch(
            &format!("/channels/{channel_id}"),
            serde_json::json!({ "parent_id": category_id.to_string() }),
        )
        .await
    }

    pub async fn edit_channel_position(
        &self,
        channel_id: u64,
        position: u32,
    ) -> Result<Value, DiscordError> {
        self.patch(
            &format!("/channels/{channel_id}"),
            serde_json::json!({ "position": position }),
        )
        .await
    }

    pub async fn find_channel(
        &self,
        guild_id: u64,
        name: &str,
    ) -> Result<Option<Value>, DiscordError> {
        let channels: Vec<Value> = self
            .get(&format!("/guilds/{guild_id}/channels"))
            .await?
            .as_array()
            .cloned()
            .unwrap_or_default();

        let lower_name = name.to_lowercase();
        Ok(channels.into_iter().find(|ch| {
            let t = ch.get("type").and_then(Value::as_u64).unwrap_or(0);
            let ch_name = ch
                .get("name")
                .and_then(|v| v.as_str())
                .unwrap_or("")
                .to_lowercase();
            (t == 0 || t == 5) && ch_name == lower_name
        }))
    }
}
