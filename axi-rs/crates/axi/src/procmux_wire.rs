//! Adapter wiring procmux to claudewire's process connection protocol.
//!
//! Wraps a `ProcmuxConnection` so claudewire's `BridgeTransport` can use it
//! without importing procmux directly.

use std::collections::HashMap;
use std::sync::Arc;

use claudewire::types::{ExitEvent, ProcessEvent, StderrEvent, StdoutEvent};

/// Result of a procmux command (spawn, subscribe, kill, list, interrupt).
pub struct CommandResult {
    pub ok: bool,
    pub error: Option<String>,
    pub already_running: bool,
    pub replayed: Option<u64>,
    pub status: Option<String>,
    pub idle: Option<bool>,
    pub agents: Vec<String>,
}
use procmux::client::{ProcessMsg, ProcmuxConnection};

/// Helper: extract agents list from procmux's Option<Value> to Vec<String>.
fn extract_agents(agents: Option<&serde_json::Value>) -> Vec<String> {
    agents
        .and_then(|v| v.as_object())
        .map(|obj| obj.keys().cloned().collect())
        .unwrap_or_default()
}

#[derive(Clone)]
pub struct ProcmuxProcessConnection {
    conn: Arc<ProcmuxConnection>,
}

impl ProcmuxProcessConnection {
    pub async fn connect(socket_path: &str) -> anyhow::Result<Self> {
        let conn = ProcmuxConnection::connect(socket_path).await?;
        Ok(Self {
            conn: Arc::new(conn),
        })
    }

    pub fn is_alive(&self) -> bool {
        self.conn.is_alive()
    }

    pub async fn spawn(
        &self,
        name: &str,
        cli_args: Vec<String>,
        env: HashMap<String, String>,
        cwd: Option<String>,
    ) -> anyhow::Result<CommandResult> {
        let result = self
            .conn
            .send_command("spawn", name, cli_args, env, cwd)
            .await?;
        Ok(CommandResult {
            ok: result.ok,
            error: result.error,
            already_running: result.already_running.unwrap_or(false),
            replayed: result.replayed.map(|n| n as u64),
            status: result.status,
            idle: result.idle,
            agents: extract_agents(result.agents.as_ref()),
        })
    }

    pub async fn subscribe(&self, name: &str) -> anyhow::Result<CommandResult> {
        let result = self.conn.send_simple_command("subscribe", name).await?;
        Ok(CommandResult {
            ok: result.ok,
            error: result.error,
            already_running: result.already_running.unwrap_or(false),
            replayed: result.replayed.map(|n| n as u64),
            status: result.status,
            idle: result.idle,
            agents: extract_agents(result.agents.as_ref()),
        })
    }

    pub async fn kill(&self, name: &str) -> anyhow::Result<CommandResult> {
        let result = self.conn.send_simple_command("kill", name).await?;
        Ok(CommandResult {
            ok: result.ok,
            error: result.error,
            already_running: false,
            replayed: None,
            status: None,
            idle: None,
            agents: vec![],
        })
    }

    pub async fn send_stdin(&self, name: &str, data: serde_json::Value) -> anyhow::Result<()> {
        self.conn.send_stdin(name, data).await
    }

    pub async fn list_agents(&self) -> anyhow::Result<CommandResult> {
        let result = self.conn.send_simple_command("list", "").await?;
        Ok(CommandResult {
            ok: result.ok,
            error: result.error,
            already_running: false,
            replayed: None,
            status: None,
            idle: None,
            agents: extract_agents(result.agents.as_ref()),
        })
    }

    pub async fn close(&self) {
        // Arc prevents consuming self — just drop the reference
    }

    pub async fn register_process(
        &self,
        name: &str,
    ) -> tokio::sync::mpsc::UnboundedReceiver<ProcessMsg> {
        self.conn.register_process(name).await
    }

    pub async fn unregister_process(&self, name: &str) {
        self.conn.unregister_process(name).await;
    }

    pub async fn interrupt(&self, name: &str) -> anyhow::Result<CommandResult> {
        let result = self.conn.send_simple_command("interrupt", name).await?;
        Ok(CommandResult {
            ok: result.ok,
            error: result.error,
            already_running: false,
            replayed: None,
            status: None,
            idle: None,
            agents: vec![],
        })
    }
}

/// Translate a procmux `ProcessMsg` to a claudewire `ProcessEvent`.
pub fn translate_process_msg(msg: ProcessMsg) -> Option<ProcessEvent> {
    match msg {
        ProcessMsg::Stdout(m) => Some(ProcessEvent::Stdout(StdoutEvent {
            name: m.name,
            data: m.data,
        })),
        ProcessMsg::Stderr(m) => Some(ProcessEvent::Stderr(StderrEvent {
            name: m.name,
            text: m.text,
        })),
        ProcessMsg::Exit(m) => Some(ProcessEvent::Exit(ExitEvent {
            name: m.name,
            code: m.code,
        })),
        ProcessMsg::ConnectionLost => None,
    }
}
