use std::collections::HashMap;

use serde::Deserialize;

/// A command file — the top-level JSON structure.
#[derive(Debug, Clone, Deserialize)]
pub struct Command {
    pub name: String,
    #[serde(default)]
    pub description: Option<String>,
    #[serde(default)]
    pub arguments: Vec<Argument>,
    pub flowchart: Flowchart,
}

/// A flowchart — a directed graph of blocks with connections.
#[derive(Debug, Clone, Deserialize)]
pub struct Flowchart {
    #[serde(default)]
    pub name: Option<String>,
    pub blocks: HashMap<String, Block>,
    #[serde(default)]
    pub connections: Vec<Connection>,
    #[serde(default)]
    pub sessions: Option<HashMap<String, SessionConfig>>,
}

/// A block in the flowchart. The `name` field is shared; `data` holds type-specific fields.
#[derive(Debug, Clone, Deserialize)]
pub struct Block {
    #[serde(default)]
    pub name: String,
    #[serde(flatten)]
    pub data: BlockData,
    /// Catch-all for fields we don't model (position, id, etc.)
    #[serde(flatten)]
    pub extra: HashMap<String, serde_json::Value>,
}

/// Type-specific block data, discriminated by the `type` field in JSON.
#[derive(Debug, Clone, Deserialize)]
#[serde(tag = "type", rename_all = "snake_case")]
pub enum BlockData {
    Start,
    End,
    Prompt {
        prompt: String,
        #[serde(default)]
        output_variable: Option<String>,
        #[serde(default)]
        session: Option<String>,
        #[serde(default)]
        output_schema: Option<serde_json::Value>,
    },
    Branch {
        condition: String,
    },
    Variable {
        variable_name: String,
        #[serde(default)]
        variable_value: String,
        #[serde(default)]
        variable_type: Option<VariableType>,
    },
    Bash {
        command: String,
        #[serde(default)]
        output_variable: Option<String>,
        #[serde(default)]
        working_directory: Option<String>,
        #[serde(default)]
        continue_on_error: Option<bool>,
        #[serde(default)]
        exit_code_variable: Option<String>,
    },
    Command {
        command_name: String,
        #[serde(default)]
        arguments: Option<String>,
        #[serde(default)]
        inherit_variables: Option<bool>,
        #[serde(default)]
        merge_output: Option<bool>,
    },
    Refresh {
        #[serde(default)]
        target_session: Option<String>,
    },
    /// Early termination with an exit code. Kills all spawned sub-sessions.
    Exit {
        #[serde(default)]
        exit_code: Option<i32>,
    },
    /// Spawn an agent sub-session to run a command concurrently.
    Spawn {
        #[serde(default)]
        agent_name: Option<String>,
        #[serde(default)]
        command_name: Option<String>,
        #[serde(default)]
        arguments: Option<String>,
        #[serde(default)]
        inherit_variables: Option<bool>,
        #[serde(default)]
        exit_code_variable: Option<String>,
        #[serde(default)]
        config_file: Option<String>,
    },
    /// Wait for spawned agent sub-sessions to complete.
    Wait,
}

#[derive(Debug, Clone, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "snake_case")]
pub enum VariableType {
    String,
    #[serde(alias = "int", alias = "float")]
    Number,
    Boolean,
    Json,
}

/// A connection between two blocks.
#[derive(Debug, Clone, Deserialize)]
pub struct Connection {
    #[serde(alias = "source_block_id")]
    pub source_id: String,
    #[serde(alias = "target_block_id")]
    pub target_id: String,
    #[serde(default)]
    pub is_true_path: Option<bool>,
}

/// A command argument declaration.
#[derive(Debug, Clone, Deserialize)]
pub struct Argument {
    pub name: String,
    #[serde(default)]
    pub description: Option<String>,
    #[serde(default)]
    pub required: Option<bool>,
    #[serde(default)]
    pub default: Option<String>,
}

/// Session configuration for multi-session flowcharts.
#[derive(Debug, Clone, Deserialize)]
pub struct SessionConfig {
    #[serde(default)]
    pub model: Option<String>,
    #[serde(default)]
    pub system_prompt: Option<String>,
    #[serde(default)]
    pub tools: Option<Vec<String>>,
}
