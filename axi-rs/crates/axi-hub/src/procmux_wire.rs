//! Adapter wiring procmux to claudewire's process connection protocol.
//!
//! Wraps a ProcmuxConnection so claudewire's BridgeTransport can use it
//! without importing procmux directly.

use std::collections::HashMap;
use std::sync::Arc;

use claudewire::types::{CommandResult, ExitEvent, ProcessEvent, StderrEvent, StdoutEvent};
use procmux::client::{ProcessMsg, ProcmuxConnection};

/// Helper: extract agents list from procmux's Option<Value> to Vec<String>.
fn extract_agents(agents: &Option<serde_json::Value>) -> Vec<String> {
    agents
        .as_ref()
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
            replayed: result.replayed,
            status: result.status,
            idle: result.idle,
            agents: extract_agents(&result.agents),
        })
    }

    pub async fn subscribe(&self, name: &str) -> anyhow::Result<CommandResult> {
        let result = self.conn.send_simple_command("subscribe", name).await?;
        Ok(CommandResult {
            ok: result.ok,
            error: result.error,
            already_running: result.already_running.unwrap_or(false),
            replayed: result.replayed,
            status: result.status,
            idle: result.idle,
            agents: extract_agents(&result.agents),
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
            agents: extract_agents(&result.agents),
        })
    }

    pub async fn close(&self) {
        // Arc prevents consuming self — just drop the reference
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

/// Translate a procmux ProcessMsg to a claudewire ProcessEvent.
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
