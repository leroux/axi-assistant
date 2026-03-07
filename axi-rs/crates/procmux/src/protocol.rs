//! Wire protocol for procmux.
//!
//! Messages between the procmux server and its clients are typed serde structs.
//! Subprocess payloads (relayed via stdin/stdout) are kept as opaque JSON values.

use std::collections::HashMap;

use serde::{Deserialize, Serialize};

// -- Client -> Server --------------------------------------------------------

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(tag = "type")]
pub enum ClientMsg {
    #[serde(rename = "cmd")]
    Cmd(CmdMsg),
    #[serde(rename = "stdin")]
    Stdin(StdinMsg),
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct CmdMsg {
    #[serde(skip_serializing)]
    pub r#type: Option<String>,
    pub cmd: String,
    #[serde(default)]
    pub name: String,
    #[serde(default)]
    pub cli_args: Vec<String>,
    #[serde(default)]
    pub env: HashMap<String, String>,
    #[serde(default)]
    pub cwd: Option<String>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct StdinMsg {
    #[serde(skip_serializing)]
    pub r#type: Option<String>,
    pub name: String,
    pub data: serde_json::Value,
}

// -- Server -> Client --------------------------------------------------------

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(tag = "type")]
pub enum ServerMsg {
    #[serde(rename = "result")]
    Result(ResultMsg),
    #[serde(rename = "stdout")]
    Stdout(StdoutMsg),
    #[serde(rename = "stderr")]
    Stderr(StderrMsg),
    #[serde(rename = "exit")]
    Exit(ExitMsg),
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ResultMsg {
    #[serde(skip_serializing)]
    pub r#type: Option<String>,
    pub ok: bool,
    #[serde(default)]
    pub name: String,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub error: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub pid: Option<u32>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub already_running: Option<bool>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub replayed: Option<usize>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub status: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub exit_code: Option<i32>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub idle: Option<bool>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub agents: Option<serde_json::Value>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub uptime_seconds: Option<u64>,
}

impl ResultMsg {
    pub fn ok(name: impl Into<String>) -> Self {
        Self {
            r#type: None,
            ok: true,
            name: name.into(),
            error: None,
            pid: None,
            already_running: None,
            replayed: None,
            status: None,
            exit_code: None,
            idle: None,
            agents: None,
            uptime_seconds: None,
        }
    }

    pub fn err(name: impl Into<String>, error: impl Into<String>) -> Self {
        Self {
            r#type: None,
            ok: false,
            name: name.into(),
            error: Some(error.into()),
            pid: None,
            already_running: None,
            replayed: None,
            status: None,
            exit_code: None,
            idle: None,
            agents: None,
            uptime_seconds: None,
        }
    }
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct StdoutMsg {
    #[serde(skip_serializing)]
    pub r#type: Option<String>,
    pub name: String,
    pub data: serde_json::Value,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct StderrMsg {
    #[serde(skip_serializing)]
    pub r#type: Option<String>,
    pub name: String,
    pub text: String,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ExitMsg {
    #[serde(skip_serializing)]
    pub r#type: Option<String>,
    pub name: String,
    pub code: Option<i32>,
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn parse_cmd_spawn() {
        let json = r#"{"type":"cmd","cmd":"spawn","name":"agent-1","cli_args":["claude","--json"],"env":{"HOME":"/home/test"},"cwd":"/tmp"}"#;
        let msg: ClientMsg = serde_json::from_str(json).unwrap();
        match msg {
            ClientMsg::Cmd(cmd) => {
                assert_eq!(cmd.cmd, "spawn");
                assert_eq!(cmd.name, "agent-1");
                assert_eq!(cmd.cli_args, vec!["claude", "--json"]);
            }
            _ => panic!("expected Cmd"),
        }
    }

    #[test]
    fn parse_stdin() {
        let json = r#"{"type":"stdin","name":"agent-1","data":{"type":"user","content":"hello"}}"#;
        let msg: ClientMsg = serde_json::from_str(json).unwrap();
        match msg {
            ClientMsg::Stdin(stdin) => {
                assert_eq!(stdin.name, "agent-1");
                assert_eq!(stdin.data["type"], "user");
            }
            _ => panic!("expected Stdin"),
        }
    }

    #[test]
    fn serialize_result() {
        let msg = ServerMsg::Result(ResultMsg::ok("agent-1"));
        let json = serde_json::to_string(&msg).unwrap();
        assert!(json.contains(r#""type":"result"#));
        assert!(json.contains(r#""ok":true"#));
    }

    #[test]
    fn serialize_stdout() {
        let msg = ServerMsg::Stdout(StdoutMsg {
            r#type: None,
            name: "agent-1".into(),
            data: serde_json::json!({"type": "stream_event", "uuid": "123"}),
        });
        let json = serde_json::to_string(&msg).unwrap();
        assert!(json.contains(r#""type":"stdout"#));
    }
}
