//! MCP protocol types — JSON-RPC messages for tool definitions and results.
//!
//! The Claude Agent SDK communicates with MCP servers via JSON-RPC over stdio.
//! This module defines the wire types for tool registration and invocation.

use std::collections::HashMap;
use std::future::Future;
use std::pin::Pin;
use std::sync::Arc;

use serde::{Deserialize, Serialize};

// ---------------------------------------------------------------------------
// JSON-RPC envelope
// ---------------------------------------------------------------------------

/// A JSON-RPC 2.0 request.
#[derive(Debug, Deserialize)]
pub struct JsonRpcRequest {
    pub jsonrpc: String,
    pub id: Option<serde_json::Value>,
    pub method: String,
    pub params: Option<serde_json::Value>,
}

/// A JSON-RPC 2.0 response.
#[derive(Debug, Serialize)]
pub struct JsonRpcResponse {
    pub jsonrpc: String,
    pub id: Option<serde_json::Value>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub result: Option<serde_json::Value>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub error: Option<JsonRpcError>,
}

/// A JSON-RPC 2.0 error object.
#[derive(Debug, Serialize)]
pub struct JsonRpcError {
    pub code: i32,
    pub message: String,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub data: Option<serde_json::Value>,
}

// ---------------------------------------------------------------------------
// MCP tool types
// ---------------------------------------------------------------------------

/// An MCP tool definition (sent during tools/list).
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ToolDefinition {
    pub name: String,
    pub description: String,
    #[serde(rename = "inputSchema")]
    pub input_schema: serde_json::Value,
}

/// Arguments passed to a tool handler.
pub type ToolArgs = serde_json::Map<String, serde_json::Value>;

/// Content block in a tool result.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ContentBlock {
    #[serde(rename = "type")]
    pub content_type: String,
    pub text: String,
}

/// Result returned by a tool handler.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ToolResult {
    pub content: Vec<ContentBlock>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub is_error: Option<bool>,
}

impl ToolResult {
    /// Create a successful text result.
    pub fn text(msg: impl Into<String>) -> Self {
        Self {
            content: vec![ContentBlock {
                content_type: "text".to_string(),
                text: msg.into(),
            }],
            is_error: None,
        }
    }

    /// Create an error result.
    pub fn error(msg: impl Into<String>) -> Self {
        Self {
            content: vec![ContentBlock {
                content_type: "text".to_string(),
                text: msg.into(),
            }],
            is_error: Some(true),
        }
    }
}

// ---------------------------------------------------------------------------
// Tool handler trait
// ---------------------------------------------------------------------------

/// Async tool handler function type.
pub type ToolHandler = Arc<
    dyn Fn(ToolArgs) -> Pin<Box<dyn Future<Output = ToolResult> + Send>> + Send + Sync,
>;

/// An MCP server definition — a named collection of tools.
#[derive(Clone)]
pub struct McpServer {
    pub name: String,
    pub version: String,
    pub tools: Vec<ToolDefinition>,
    pub handlers: HashMap<String, ToolHandler>,
}

impl McpServer {
    pub fn new(name: impl Into<String>, version: impl Into<String>) -> Self {
        Self {
            name: name.into(),
            version: version.into(),
            tools: Vec::new(),
            handlers: HashMap::new(),
        }
    }

    /// Register a tool with its definition and handler.
    pub fn add_tool<F, Fut>(
        &mut self,
        name: impl Into<String>,
        description: impl Into<String>,
        input_schema: serde_json::Value,
        handler: F,
    ) where
        F: Fn(ToolArgs) -> Fut + Send + Sync + 'static,
        Fut: Future<Output = ToolResult> + Send + 'static,
    {
        let name = name.into();
        self.tools.push(ToolDefinition {
            name: name.clone(),
            description: description.into(),
            input_schema,
        });
        self.handlers
            .insert(name, Arc::new(move |args| Box::pin(handler(args))));
    }

    /// Handle a tool call by name.
    pub async fn call_tool(&self, name: &str, args: ToolArgs) -> ToolResult {
        match self.handlers.get(name) {
            Some(handler) => handler(args).await,
            None => ToolResult::error(format!("Unknown tool: {name}")),
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
    fn tool_result_text() {
        let result = ToolResult::text("hello");
        assert!(result.is_error.is_none());
        assert_eq!(result.content[0].text, "hello");
    }

    #[test]
    fn tool_result_error() {
        let result = ToolResult::error("bad input");
        assert_eq!(result.is_error, Some(true));
        assert_eq!(result.content[0].text, "bad input");
    }

    #[tokio::test]
    async fn mcp_server_call() {
        let mut server = McpServer::new("test", "1.0.0");
        server.add_tool(
            "echo",
            "Echo back the input",
            serde_json::json!({"type": "object", "properties": {}}),
            |args| async move {
                let text = args
                    .get("text")
                    .and_then(|v| v.as_str())
                    .unwrap_or("no input");
                ToolResult::text(text)
            },
        );

        let mut args = ToolArgs::new();
        args.insert("text".to_string(), serde_json::json!("hello"));
        let result = server.call_tool("echo", args).await;
        assert_eq!(result.content[0].text, "hello");
    }

    #[tokio::test]
    async fn mcp_server_unknown_tool() {
        let server = McpServer::new("test", "1.0.0");
        let result = server.call_tool("nonexistent", ToolArgs::new()).await;
        assert_eq!(result.is_error, Some(true));
    }
}
