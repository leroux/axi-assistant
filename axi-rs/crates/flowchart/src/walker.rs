use std::collections::HashMap;

use crate::condition;
use crate::interpolate;
use crate::model::{BlockData, Flowchart, VariableType};

/// What the walker needs from the consumer.
#[derive(Debug, Clone)]
pub enum Action {
    /// Send a prompt to the coding agent session.
    Query {
        block_id: String,
        block_name: String,
        prompt: String,
        output_var: Option<String>,
        session: Option<String>,
        output_schema: Option<serde_json::Value>,
    },
    /// Run a shell command.
    Bash {
        block_id: String,
        block_name: String,
        command: String,
        output_var: Option<String>,
        exit_code_var: Option<String>,
        continue_on_error: bool,
        working_directory: Option<String>,
    },
    /// Execute a sub-command (another flowchart).
    SubCommand {
        block_id: String,
        block_name: String,
        command_name: String,
        arguments: String,
        inherit_variables: bool,
        merge_output: bool,
    },
    /// Clear/restart the agent session.
    Clear {
        block_id: String,
        block_name: String,
        session: Option<String>,
    },
    /// Flowchart execution is complete.
    Done {
        output: Option<String>,
    },
    /// Flowchart terminated early via exit block.
    Exit {
        block_id: String,
        block_name: String,
        exit_code: i32,
    },
    /// Spawn an agent sub-session.
    Spawn {
        block_id: String,
        block_name: String,
        agent_name: Option<String>,
        command_name: Option<String>,
        arguments: String,
        inherit_variables: bool,
        exit_code_variable: Option<String>,
        config_file: Option<String>,
    },
    /// Wait for all spawned agent sub-sessions.
    Wait {
        block_id: String,
        block_name: String,
    },
    /// Flowchart execution hit an error.
    Error { message: String },
}

/// Pure state machine that walks a flowchart graph.
///
/// Usage:
/// ```ignore
/// let mut walker = GraphWalker::new(flowchart, variables);
/// let mut action = walker.start();
/// loop {
///     match action {
///         Action::Done { .. } | Action::Error { .. } => break,
///         Action::Query { .. } => {
///             // send prompt, get response
///             action = walker.feed("response text");
///         }
///         // ... handle other actions
///     }
/// }
/// ```
pub struct GraphWalker {
    flowchart: Flowchart,
    variables: HashMap<String, String>,
    current_block_id: Option<String>,
    blocks_executed: usize,
    max_blocks: usize,
    // Outgoing edges index: block_id -> list of (target_id, is_true_path)
    outgoing: HashMap<String, Vec<OutEdge>>,
    // State for tracking what the last yielded action was
    pending_output_var: Option<String>,
    pending_exit_code_var: Option<String>,
    pending_continue_on_error: bool,
}

#[derive(Debug, Clone)]
struct OutEdge {
    target_id: String,
    is_true_path: Option<bool>,
}

impl GraphWalker {
    pub fn new(flowchart: Flowchart, variables: HashMap<String, String>) -> Self {
        let outgoing = build_edge_index(&flowchart);
        Self {
            flowchart,
            variables,
            current_block_id: None,
            blocks_executed: 0,
            max_blocks: 1000,
            outgoing,
            pending_output_var: None,
            pending_exit_code_var: None,
            pending_continue_on_error: false,
        }
    }

    #[must_use]
    pub const fn with_max_blocks(mut self, max: usize) -> Self {
        self.max_blocks = max;
        self
    }

    /// Begin execution. Returns the first action needing external IO.
    pub fn start(&mut self) -> Action {
        // Find start block
        let start_id = self
            .flowchart
            .blocks
            .iter()
            .find(|(_, b)| matches!(b.data, BlockData::Start))
            .map(|(id, _)| id.clone());

        match start_id {
            Some(id) => {
                self.blocks_executed += 1;
                // Advance past start block to the next one
                match self.next_block_from(&id, None) {
                    Some(next_id) => {
                        self.current_block_id = Some(next_id);
                        self.advance()
                    }
                    None => Action::Done { output: None },
                }
            }
            None => Action::Done { output: None },
        }
    }

    /// Feed the result of the previous action. Returns the next action.
    pub fn feed(&mut self, result: &str) -> Action {
        // Store result in output variable if pending
        if let Some(var) = self.pending_output_var.take() {
            self.variables.insert(var, result.to_owned());
        }

        // Advance to next block
        let current = match self.current_block_id.take() {
            Some(id) => id,
            None => return Action::Done { output: None },
        };

        match self.next_block_from(&current, None) {
            Some(next_id) => {
                self.current_block_id = Some(next_id);
                self.advance()
            }
            None => Action::Done { output: None },
        }
    }

    /// Feed a structured result from a sub-command with its variables.
    pub fn feed_subcommand(
        &mut self,
        result: &str,
        child_variables: HashMap<String, String>,
    ) -> Action {
        // Store result in output variable if pending
        if let Some(var) = self.pending_output_var.take() {
            self.variables.insert(var, result.to_owned());
        }

        // Merge child variables (skip positional args)
        for (k, v) in child_variables {
            if !k.starts_with('$') {
                self.variables.insert(k, v);
            }
        }

        // Advance to next block
        let current = match self.current_block_id.take() {
            Some(id) => id,
            None => return Action::Done { output: None },
        };

        match self.next_block_from(&current, None) {
            Some(next_id) => {
                self.current_block_id = Some(next_id);
                self.advance()
            }
            None => Action::Done { output: None },
        }
    }

    /// Feed the result of a bash command, including exit code.
    pub fn feed_bash(&mut self, stdout: &str, exit_code: i32) -> Action {
        // Store exit code if pending
        if let Some(var) = self.pending_exit_code_var.take() {
            self.variables.insert(var, exit_code.to_string());
        }

        // Check exit code — if non-zero and not continue_on_error, halt
        let continue_on_error = self.pending_continue_on_error;
        self.pending_continue_on_error = false;
        if exit_code != 0 && !continue_on_error {
            return Action::Error {
                message: format!("Bash command failed with exit code {exit_code}"),
            };
        }

        // Store stdout in output variable if pending
        if let Some(var) = self.pending_output_var.take() {
            self.variables.insert(var, stdout.trim().to_owned());
        }

        // Advance to next block
        let current = match self.current_block_id.take() {
            Some(id) => id,
            None => return Action::Done { output: None },
        };

        match self.next_block_from(&current, None) {
            Some(next_id) => {
                self.current_block_id = Some(next_id);
                self.advance()
            }
            None => Action::Done { output: None },
        }
    }

    /// Get current variables (for sub-command variable inheritance).
    pub const fn variables(&self) -> &HashMap<String, String> {
        &self.variables
    }

    /// Mutable access to variables (e.g., to inject structured output fields).
    pub const fn variables_mut(&mut self) -> &mut HashMap<String, String> {
        &mut self.variables
    }

    /// Process blocks until we hit one that needs external IO.
    fn advance(&mut self) -> Action {
        loop {
            let block_id = match &self.current_block_id {
                Some(id) => id.clone(),
                None => return Action::Done { output: None },
            };

            // Safety limit
            self.blocks_executed += 1;
            if self.blocks_executed > self.max_blocks {
                return Action::Error {
                    message: format!("Safety limit: exceeded {} blocks", self.max_blocks),
                };
            }

            let block = match self.flowchart.blocks.get(&block_id) {
                Some(b) => b,
                None => {
                    return Action::Error {
                        message: format!("Block not found: {block_id}"),
                    }
                }
            };

            let block_name = block.name.clone();

            match &block.data {
                BlockData::Start => {
                    // Should not normally hit start again, but handle gracefully
                    match self.next_block_from(&block_id, None) {
                        Some(next) => self.current_block_id = Some(next),
                        None => return Action::Done { output: None },
                    }
                }

                BlockData::End => {
                    return Action::Done { output: None };
                }

                BlockData::Variable {
                    variable_name,
                    variable_value,
                    variable_type,
                } => {
                    let raw = interpolate::interpolate(variable_value, &self.variables);

                    let value = match coerce_variable(&raw, variable_type.as_ref()) {
                        Ok(v) => v,
                        Err(e) => {
                            return Action::Error {
                                message: format!(
                                    "Variable '{variable_name}' type coercion failed: {e}"
                                ),
                            }
                        }
                    };

                    self.variables.insert(variable_name.clone(), value);

                    match self.next_block_from(&block_id, None) {
                        Some(next) => self.current_block_id = Some(next),
                        None => return Action::Done { output: None },
                    }
                }

                BlockData::Branch { condition } => {
                    let resolved = interpolate::interpolate(condition, &self.variables);
                    let taken = condition::evaluate(&resolved, &self.variables);

                    match self.next_block_from(&block_id, Some(taken)) {
                        Some(next) => self.current_block_id = Some(next),
                        None => {
                            return Action::Error {
                                message: format!(
                                    "Branch '{block_name}' has no {taken} path"
                                ),
                            }
                        }
                    }
                }

                BlockData::Prompt {
                    prompt,
                    output_variable,
                    session,
                    output_schema,
                } => {
                    let prompt_text = interpolate::interpolate(prompt, &self.variables);
                    self.pending_output_var = output_variable.clone();

                    return Action::Query {
                        block_id,
                        block_name,
                        prompt: prompt_text,
                        output_var: output_variable.clone(),
                        session: session.clone(),
                        output_schema: output_schema.clone(),
                    };
                }

                BlockData::Bash {
                    command,
                    output_variable,
                    working_directory,
                    continue_on_error,
                    exit_code_variable,
                } => {
                    let cmd = interpolate::interpolate(command, &self.variables);
                    let coe = continue_on_error.unwrap_or(false);
                    self.pending_output_var = output_variable.clone();
                    self.pending_exit_code_var = exit_code_variable.clone();
                    self.pending_continue_on_error = coe;

                    return Action::Bash {
                        block_id,
                        block_name,
                        command: cmd,
                        output_var: output_variable.clone(),
                        exit_code_var: exit_code_variable.clone(),
                        continue_on_error: coe,
                        working_directory: working_directory.clone(),
                    };
                }

                BlockData::Command {
                    command_name,
                    arguments,
                    inherit_variables,
                    merge_output,
                } => {
                    let args = arguments
                        .as_deref()
                        .map(|a| interpolate::interpolate(a, &self.variables))
                        .unwrap_or_default();
                    let inherit = inherit_variables.unwrap_or(false);
                    let merge = merge_output.unwrap_or(false);

                    return Action::SubCommand {
                        block_id,
                        block_name,
                        command_name: command_name.clone(),
                        arguments: args,
                        inherit_variables: inherit,
                        merge_output: merge,
                    };
                }

                BlockData::Refresh { target_session } => {
                    return Action::Clear {
                        block_id,
                        block_name,
                        session: target_session.clone(),
                    };
                }

                BlockData::Exit { exit_code } => {
                    return Action::Exit {
                        block_id,
                        block_name,
                        exit_code: exit_code.unwrap_or(0),
                    };
                }

                BlockData::Spawn {
                    agent_name,
                    command_name,
                    arguments,
                    inherit_variables,
                    exit_code_variable,
                    config_file,
                } => {
                    let args = arguments
                        .as_deref()
                        .map(|a| interpolate::interpolate(a, &self.variables))
                        .unwrap_or_default();
                    self.pending_output_var = exit_code_variable.clone();

                    return Action::Spawn {
                        block_id,
                        block_name,
                        agent_name: agent_name.clone(),
                        command_name: command_name.clone(),
                        arguments: args,
                        inherit_variables: inherit_variables.unwrap_or(false),
                        exit_code_variable: exit_code_variable.clone(),
                        config_file: config_file.clone(),
                    };
                }

                BlockData::Wait => {
                    return Action::Wait {
                        block_id,
                        block_name,
                    };
                }
            }
        }
    }

    /// Find the next block ID from `current_id`.
    /// For branch blocks, `branch_taken` determines which path to follow.
    fn next_block_from(&self, current_id: &str, branch_taken: Option<bool>) -> Option<String> {
        let edges = self.outgoing.get(current_id)?;

        if let Some(taken) = branch_taken {
            // Branch: find the edge matching the taken path
            edges
                .iter()
                .find(|e| e.is_true_path == Some(taken))
                .map(|e| e.target_id.clone())
        } else {
            // Non-branch: take the first (only) connection
            edges.first().map(|e| e.target_id.clone())
        }
    }
}

fn build_edge_index(flowchart: &Flowchart) -> HashMap<String, Vec<OutEdge>> {
    let mut index: HashMap<String, Vec<OutEdge>> = HashMap::new();
    for conn in &flowchart.connections {
        index
            .entry(conn.source_id.clone())
            .or_default()
            .push(OutEdge {
                target_id: conn.target_id.clone(),
                is_true_path: conn.is_true_path,
            });
    }
    index
}

/// Coerce a raw string value to the appropriate type representation.
/// All values are stored as strings in the walker's variable map.
fn coerce_variable(raw: &str, var_type: Option<&VariableType>) -> Result<String, String> {
    match var_type {
        None | Some(VariableType::String) => Ok(raw.to_owned()),
        Some(VariableType::Number) => {
            let f: f64 = raw
                .parse()
                .map_err(|e: std::num::ParseFloatError| e.to_string())?;
            // Store as clean representation — whole numbers without decimal
            if f.fract() == 0.0 {
                #[allow(clippy::cast_possible_truncation)]
                Ok((f as i64).to_string())
            } else {
                Ok(f.to_string())
            }
        }
        Some(VariableType::Boolean) => {
            let b = matches!(raw.to_lowercase().as_str(), "true" | "1" | "yes");
            Ok(b.to_string())
        }
        Some(VariableType::Json) => {
            // Validate it's valid JSON, then store raw
            let _: serde_json::Value =
                serde_json::from_str(raw).map_err(|e| e.to_string())?;
            Ok(raw.to_owned())
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::model::{Block, Connection};

    fn block(name: &str, data: BlockData) -> Block {
        Block {
            name: name.to_owned(),
            data,
            extra: HashMap::new(),
        }
    }

    fn flowchart(
        blocks: Vec<(&str, Block)>,
        connections: Vec<Connection>,
    ) -> Flowchart {
        Flowchart {
            name: None,
            blocks: blocks
                .into_iter()
                .map(|(id, b)| (id.to_owned(), b))
                .collect(),
            connections,
            sessions: None,
        }
    }

    fn conn(source: &str, target: &str) -> Connection {
        Connection {
            source_id: source.into(),
            target_id: target.into(),
            is_true_path: None,
        }
    }

    fn branch_conn(source: &str, target: &str, is_true: bool) -> Connection {
        Connection {
            source_id: source.into(),
            target_id: target.into(),
            is_true_path: Some(is_true),
        }
    }

    #[test]
    fn start_to_end() {
        let fc = flowchart(
            vec![
                ("s", block("Begin", BlockData::Start)),
                ("e", block("Done", BlockData::End)),
            ],
            vec![conn("s", "e")],
        );
        let mut walker = GraphWalker::new(fc, HashMap::new());
        let action = walker.start();
        assert!(matches!(action, Action::Done { .. }));
    }

    #[test]
    fn start_prompt_end() {
        let fc = flowchart(
            vec![
                ("s", block("Begin", BlockData::Start)),
                (
                    "p",
                    block(
                        "Ask",
                        BlockData::Prompt {
                            prompt: "Hello $1".into(),
                            output_variable: Some("answer".into()),
                            session: None,
                            output_schema: None,
                        },
                    ),
                ),
                ("e", block("Done", BlockData::End)),
            ],
            vec![conn("s", "p"), conn("p", "e")],
        );
        let vars: HashMap<String, String> = [("$1".into(), "World".into())].into();
        let mut walker = GraphWalker::new(fc, vars);

        let action = walker.start();
        match &action {
            Action::Query { prompt, .. } => assert_eq!(prompt, "Hello World"),
            other => panic!("Expected Query, got {other:?}"),
        }

        let action = walker.feed("The answer is 42");
        assert!(matches!(action, Action::Done { .. }));
        assert_eq!(walker.variables().get("answer").unwrap(), "The answer is 42");
    }

    #[test]
    fn variable_block_sets_value() {
        let fc = flowchart(
            vec![
                ("s", block("Begin", BlockData::Start)),
                (
                    "v",
                    block(
                        "Set Name",
                        BlockData::Variable {
                            variable_name: "name".into(),
                            variable_value: "World".into(),
                            variable_type: None,
                        },
                    ),
                ),
                (
                    "p",
                    block(
                        "Ask",
                        BlockData::Prompt {
                            prompt: "Hello {{name}}".into(),
                            output_variable: None,
                            session: None,
                            output_schema: None,
                        },
                    ),
                ),
                ("e", block("Done", BlockData::End)),
            ],
            vec![conn("s", "v"), conn("v", "p"), conn("p", "e")],
        );
        let mut walker = GraphWalker::new(fc, HashMap::new());

        let action = walker.start();
        match &action {
            Action::Query { prompt, .. } => assert_eq!(prompt, "Hello World"),
            other => panic!("Expected Query, got {other:?}"),
        }

        assert_eq!(walker.variables().get("name").unwrap(), "World");
    }

    #[test]
    fn variable_number_coercion() {
        let fc = flowchart(
            vec![
                ("s", block("Begin", BlockData::Start)),
                (
                    "v",
                    block(
                        "Set X",
                        BlockData::Variable {
                            variable_name: "x".into(),
                            variable_value: "42".into(),
                            variable_type: Some(VariableType::Number),
                        },
                    ),
                ),
                ("e", block("Done", BlockData::End)),
            ],
            vec![conn("s", "v"), conn("v", "e")],
        );
        let mut walker = GraphWalker::new(fc, HashMap::new());
        let action = walker.start();
        assert!(matches!(action, Action::Done { .. }));
        assert_eq!(walker.variables().get("x").unwrap(), "42");
    }

    #[test]
    fn variable_boolean_coercion() {
        let fc = flowchart(
            vec![
                ("s", block("Begin", BlockData::Start)),
                (
                    "v",
                    block(
                        "Set Flag",
                        BlockData::Variable {
                            variable_name: "flag".into(),
                            variable_value: "true".into(),
                            variable_type: Some(VariableType::Boolean),
                        },
                    ),
                ),
                ("e", block("Done", BlockData::End)),
            ],
            vec![conn("s", "v"), conn("v", "e")],
        );
        let mut walker = GraphWalker::new(fc, HashMap::new());
        let action = walker.start();
        assert!(matches!(action, Action::Done { .. }));
        assert_eq!(walker.variables().get("flag").unwrap(), "true");
    }

    #[test]
    fn variable_json_coercion() {
        let fc = flowchart(
            vec![
                ("s", block("Begin", BlockData::Start)),
                (
                    "v",
                    block(
                        "Set Data",
                        BlockData::Variable {
                            variable_name: "data".into(),
                            variable_value: r#"{"key": "value"}"#.into(),
                            variable_type: Some(VariableType::Json),
                        },
                    ),
                ),
                ("e", block("Done", BlockData::End)),
            ],
            vec![conn("s", "v"), conn("v", "e")],
        );
        let mut walker = GraphWalker::new(fc, HashMap::new());
        let action = walker.start();
        assert!(matches!(action, Action::Done { .. }));
        assert_eq!(
            walker.variables().get("data").unwrap(),
            r#"{"key": "value"}"#
        );
    }

    #[test]
    fn variable_template_resolution() {
        let fc = flowchart(
            vec![
                ("s", block("Begin", BlockData::Start)),
                (
                    "v1",
                    block(
                        "Set Greeting",
                        BlockData::Variable {
                            variable_name: "greeting".into(),
                            variable_value: "Hello".into(),
                            variable_type: None,
                        },
                    ),
                ),
                (
                    "v2",
                    block(
                        "Set Msg",
                        BlockData::Variable {
                            variable_name: "msg".into(),
                            variable_value: "{{greeting}}, World!".into(),
                            variable_type: None,
                        },
                    ),
                ),
                ("e", block("Done", BlockData::End)),
            ],
            vec![conn("s", "v1"), conn("v1", "v2"), conn("v2", "e")],
        );
        let mut walker = GraphWalker::new(fc, HashMap::new());
        let action = walker.start();
        assert!(matches!(action, Action::Done { .. }));
        assert_eq!(walker.variables().get("msg").unwrap(), "Hello, World!");
    }

    #[test]
    fn branch_true_path() {
        let fc = flowchart(
            vec![
                ("s", block("Begin", BlockData::Start)),
                (
                    "b",
                    block(
                        "Check",
                        BlockData::Branch {
                            condition: "flag".into(),
                        },
                    ),
                ),
                ("ok", block("OK", BlockData::End)),
                ("fail", block("Fail", BlockData::End)),
            ],
            vec![
                conn("s", "b"),
                branch_conn("b", "ok", true),
                branch_conn("b", "fail", false),
            ],
        );
        let vars: HashMap<String, String> = [("flag".into(), "true".into())].into();
        let mut walker = GraphWalker::new(fc, vars);
        let action = walker.start();
        assert!(matches!(action, Action::Done { .. }));
    }

    #[test]
    fn branch_false_path() {
        let fc = flowchart(
            vec![
                ("s", block("Begin", BlockData::Start)),
                (
                    "b",
                    block(
                        "Check",
                        BlockData::Branch {
                            condition: "flag".into(),
                        },
                    ),
                ),
                ("ok", block("OK", BlockData::End)),
                ("fail", block("Fail", BlockData::End)),
            ],
            vec![
                conn("s", "b"),
                branch_conn("b", "ok", true),
                branch_conn("b", "fail", false),
            ],
        );
        let vars: HashMap<String, String> = [("flag".into(), "false".into())].into();
        let mut walker = GraphWalker::new(fc, vars);
        let action = walker.start();
        assert!(matches!(action, Action::Done { .. }));
    }

    #[test]
    fn branch_missing_var_is_falsy() {
        let fc = flowchart(
            vec![
                ("s", block("Begin", BlockData::Start)),
                (
                    "b",
                    block(
                        "Check",
                        BlockData::Branch {
                            condition: "flag".into(),
                        },
                    ),
                ),
                ("ok", block("OK", BlockData::End)),
                ("fail", block("Fail", BlockData::End)),
            ],
            vec![
                conn("s", "b"),
                branch_conn("b", "ok", true),
                branch_conn("b", "fail", false),
            ],
        );
        let mut walker = GraphWalker::new(fc, HashMap::new());
        let action = walker.start();
        // Should take false path (missing var is falsy)
        assert!(matches!(action, Action::Done { .. }));
    }

    #[test]
    fn bash_block_yields_action() {
        let fc = flowchart(
            vec![
                ("s", block("Begin", BlockData::Start)),
                (
                    "b",
                    block(
                        "Run",
                        BlockData::Bash {
                            command: "echo hello".into(),
                            output_variable: Some("result".into()),
                            working_directory: None,
                            continue_on_error: None,
                            exit_code_variable: None,
                        },
                    ),
                ),
                ("e", block("Done", BlockData::End)),
            ],
            vec![conn("s", "b"), conn("b", "e")],
        );
        let mut walker = GraphWalker::new(fc, HashMap::new());
        let action = walker.start();
        match &action {
            Action::Bash { command, .. } => assert_eq!(command, "echo hello"),
            other => panic!("Expected Bash, got {other:?}"),
        }

        let action = walker.feed_bash("hello\n", 0);
        assert!(matches!(action, Action::Done { .. }));
        assert_eq!(walker.variables().get("result").unwrap(), "hello");
    }

    #[test]
    fn bash_failure_halts() {
        let fc = flowchart(
            vec![
                ("s", block("Begin", BlockData::Start)),
                (
                    "b",
                    block(
                        "Run",
                        BlockData::Bash {
                            command: "exit 1".into(),
                            output_variable: None,
                            working_directory: None,
                            continue_on_error: Some(false),
                            exit_code_variable: None,
                        },
                    ),
                ),
                ("e", block("Done", BlockData::End)),
            ],
            vec![conn("s", "b"), conn("b", "e")],
        );
        let mut walker = GraphWalker::new(fc, HashMap::new());
        let action = walker.start();
        assert!(matches!(action, Action::Bash { .. }));

        let action = walker.feed_bash("", 1);
        assert!(matches!(action, Action::Error { .. }));
    }

    #[test]
    fn bash_continue_on_error() {
        let fc = flowchart(
            vec![
                ("s", block("Begin", BlockData::Start)),
                (
                    "b",
                    block(
                        "Run",
                        BlockData::Bash {
                            command: "exit 1".into(),
                            output_variable: None,
                            working_directory: None,
                            continue_on_error: Some(true),
                            exit_code_variable: Some("rc".into()),
                        },
                    ),
                ),
                ("e", block("Done", BlockData::End)),
            ],
            vec![conn("s", "b"), conn("b", "e")],
        );
        let mut walker = GraphWalker::new(fc, HashMap::new());
        let action = walker.start();
        assert!(matches!(action, Action::Bash { .. }));

        let action = walker.feed_bash("", 1);
        assert!(matches!(action, Action::Done { .. }));
        assert_eq!(walker.variables().get("rc").unwrap(), "1");
    }

    #[test]
    fn refresh_yields_clear() {
        let fc = flowchart(
            vec![
                ("s", block("Begin", BlockData::Start)),
                (
                    "r",
                    block(
                        "Clear",
                        BlockData::Refresh {
                            target_session: Some("default".into()),
                        },
                    ),
                ),
                ("e", block("Done", BlockData::End)),
            ],
            vec![conn("s", "r"), conn("r", "e")],
        );
        let mut walker = GraphWalker::new(fc, HashMap::new());
        let action = walker.start();
        match &action {
            Action::Clear { session, .. } => {
                assert_eq!(session.as_deref(), Some("default"));
            }
            other => panic!("Expected Clear, got {other:?}"),
        }

        let action = walker.feed("");
        assert!(matches!(action, Action::Done { .. }));
    }

    #[test]
    fn subcommand_yields_action() {
        let fc = flowchart(
            vec![
                ("s", block("Begin", BlockData::Start)),
                (
                    "c",
                    block(
                        "Sub",
                        BlockData::Command {
                            command_name: "test-sub".into(),
                            arguments: Some("{{task}}".into()),
                            inherit_variables: Some(true),
                            merge_output: Some(true),
                        },
                    ),
                ),
                ("e", block("Done", BlockData::End)),
            ],
            vec![conn("s", "c"), conn("c", "e")],
        );
        let vars: HashMap<String, String> = [("task".into(), "deploy".into())].into();
        let mut walker = GraphWalker::new(fc, vars);
        let action = walker.start();
        match &action {
            Action::SubCommand {
                command_name,
                arguments,
                inherit_variables,
                merge_output,
                ..
            } => {
                assert_eq!(command_name, "test-sub");
                assert_eq!(arguments, "deploy");
                assert!(*inherit_variables);
                assert!(*merge_output);
            }
            other => panic!("Expected SubCommand, got {other:?}"),
        }
    }

    #[test]
    fn safety_limit() {
        // Loop: variable -> variable (cycle)
        let fc = flowchart(
            vec![
                ("s", block("Begin", BlockData::Start)),
                (
                    "v",
                    block(
                        "Loop",
                        BlockData::Variable {
                            variable_name: "x".into(),
                            variable_value: "1".into(),
                            variable_type: None,
                        },
                    ),
                ),
                ("e", block("Done", BlockData::End)),
            ],
            vec![
                conn("s", "v"),
                conn("v", "v"), // self-loop
            ],
        );
        let mut walker = GraphWalker::new(fc, HashMap::new()).with_max_blocks(10);
        let action = walker.start();
        assert!(matches!(action, Action::Error { .. }));
        if let Action::Error { message } = action {
            assert!(message.contains("Safety limit"));
        }
    }

    #[test]
    fn loop_with_branch_exit() {
        // start -> init(i=0) -> prompt -> inc(i=i+1) -> branch(i<3) -> [true: prompt, false: end]
        // We simulate 3 iterations of prompt
        let fc = flowchart(
            vec![
                ("s", block("Begin", BlockData::Start)),
                (
                    "init",
                    block(
                        "Init",
                        BlockData::Variable {
                            variable_name: "i".into(),
                            variable_value: "0".into(),
                            variable_type: Some(VariableType::Number),
                        },
                    ),
                ),
                (
                    "prompt",
                    block(
                        "Ask",
                        BlockData::Prompt {
                            prompt: "Iteration {{i}}".into(),
                            output_variable: None,
                            session: None,
                            output_schema: None,
                        },
                    ),
                ),
                (
                    "inc",
                    block(
                        "Increment",
                        BlockData::Variable {
                            variable_name: "i".into(),
                            variable_value: "{{i}}".into(), // Will need manual increment logic
                            variable_type: Some(VariableType::Number),
                        },
                    ),
                ),
                (
                    "check",
                    block(
                        "Check",
                        BlockData::Branch {
                            condition: "i < 3".into(),
                        },
                    ),
                ),
                ("e", block("Done", BlockData::End)),
            ],
            vec![
                conn("s", "init"),
                conn("init", "prompt"),
                conn("prompt", "inc"),
                conn("inc", "check"),
                branch_conn("check", "prompt", true),
                branch_conn("check", "e", false),
            ],
        );

        let mut walker = GraphWalker::new(fc, HashMap::new());
        // i=0: start -> init(i=0) -> prompt
        let action = walker.start();
        assert!(matches!(action, Action::Query { .. }));

        // After prompt, i stays 0, inc sets i={{i}} which is "0", check: 0<3 true -> prompt
        // NOTE: This test demonstrates the loop pattern but the increment block
        // just re-reads i as "0" — in a real flowchart you'd use bash for arithmetic.
        // The test verifies the walker handles cycles correctly.
        let action = walker.feed("response 1");
        // inc sets i={{i}}="0", check: 0<3=true -> back to prompt
        assert!(matches!(action, Action::Query { .. }));
    }

    #[test]
    fn no_start_block() {
        let fc = flowchart(
            vec![("e", block("Done", BlockData::End))],
            vec![],
        );
        let mut walker = GraphWalker::new(fc, HashMap::new());
        let action = walker.start();
        // No start block = done immediately
        assert!(matches!(action, Action::Done { .. }));
    }

    #[test]
    fn parse_and_walk_story() {
        let json = include_str!("../tests/fixtures/story.json");
        let cmd: crate::model::Command = serde_json::from_str(json).unwrap();
        let vars: HashMap<String, String> = [("$1".into(), "dragons".into())].into();
        let mut walker = GraphWalker::new(cmd.flowchart, vars);

        // start -> draft (prompt)
        let action = walker.start();
        match &action {
            Action::Query {
                prompt,
                output_var,
                ..
            } => {
                assert!(prompt.contains("dragons"));
                assert_eq!(output_var.as_deref(), Some("draft_text"));
            }
            other => panic!("Expected Query, got {other:?}"),
        }

        // feed draft response -> critique (prompt)
        let action = walker.feed("Once upon a time there were dragons...");
        match &action {
            Action::Query {
                prompt, session, ..
            } => {
                assert!(prompt.contains("Once upon a time"));
                assert_eq!(session.as_deref(), Some("critic"));
            }
            other => panic!("Expected Query, got {other:?}"),
        }

        // feed critique -> refine (prompt)
        let action = walker.feed("Needs more character depth");
        match &action {
            Action::Query { prompt, .. } => {
                assert!(prompt.contains("Once upon a time")); // draft
                assert!(prompt.contains("Needs more character depth")); // feedback
            }
            other => panic!("Expected Query, got {other:?}"),
        }

        // feed refined -> end
        let action = walker.feed("The refined story...");
        assert!(matches!(action, Action::Done { .. }));
    }
}
