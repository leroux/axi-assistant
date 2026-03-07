//! MCP tool definitions for the Axi bot.
//!
//! Implements the JSON-RPC based MCP protocol for tool definitions and
//! handlers. Tools are organized into servers that get attached to different
//! agent types:
//!
//! - **utils**: date/time, file upload, status — available to all agents
//! - **discord**: cross-channel messaging — available to all agents
//! - **axi**: spawn/kill/restart-agent — available to admin agents
//! - **axi-master**: spawn/kill/restart/send-message — master agent only
//! - **schedule**: per-agent schedule CRUD — dynamically scoped per agent

pub mod protocol;
pub mod tools;
pub mod schedule;
