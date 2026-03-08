use std::collections::HashMap;
use std::path::PathBuf;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::Arc;
use std::time::Instant;

use tokio_util::sync::CancellationToken;

use flowchart::{Action, Command, GraphWalker, resolve_command};

use crate::error::ExecutionError;
use crate::json_extract::extract_json;
use crate::protocol::{ExecutionStatus, FlowchartResult, Protocol};
use crate::session::Session;
use crate::variables::build_variables;

/// Configuration for flowchart execution.
pub struct ExecutorConfig {
    /// Maximum blocks before safety halt (default 1000).
    pub max_blocks: usize,
    /// Maximum sub-command recursion depth (default 10).
    pub max_depth: usize,
    /// Directories to search for command JSON files.
    pub search_paths: Vec<PathBuf>,
    /// Warn if a single block exceeds this duration in seconds (default 300).
    pub soft_timeout_secs: u64,
    /// Optional pause mechanism: set flag to pause between blocks.
    pub pause_flag: Option<Arc<AtomicBool>>,
    /// Notify to resume after pause.
    pub pause_signal: Option<Arc<tokio::sync::Notify>>,
}

impl Default for ExecutorConfig {
    fn default() -> Self {
        Self {
            max_blocks: 1000,
            max_depth: 10,
            search_paths: Vec::new(),
            soft_timeout_secs: 300,
            pause_flag: None,
            pause_signal: None,
        }
    }
}

/// Execute a flowchart command end-to-end.
///
/// Builds variables from `args`, creates a `GraphWalker`, and drives it
/// by dispatching each `Action` through the session.
pub async fn run_flowchart<S: Session>(
    session: &mut S,
    protocol: &mut (dyn Protocol + '_),
    command: &Command,
    args: &str,
    config: &ExecutorConfig,
    cancel: CancellationToken,
) -> Result<FlowchartResult, ExecutionError> {
    let variables = build_variables(args, &command.arguments)?;
    let block_count = command.flowchart.blocks.len();
    let command_name = command.name.clone();

    protocol.on_flowchart_start(&command_name, args, block_count);

    let call_stack = vec![command_name];
    let result = run_walker(
        session,
        protocol,
        command.flowchart.clone(),
        variables,
        config,
        cancel,
        call_stack,
    )
    .await;

    match &result {
        Ok(r) => protocol.on_flowchart_complete(r),
        Err(e) => {
            let err_result = FlowchartResult {
                variables: HashMap::new(),
                status: ExecutionStatus::Error(e.to_string()),
                duration_ms: 0,
                blocks_executed: 0,
                cost_usd: session.total_cost(),
            };
            protocol.on_flowchart_complete(&err_result);
        }
    }

    result
}

/// Inner execution loop — also used for sub-command recursion.
///
/// Free function to avoid self-borrow conflicts when the executor
/// needs to recurse into sub-commands.
async fn run_walker<S: Session>(
    session: &mut S,
    protocol: &mut (dyn Protocol + '_),
    flowchart: flowchart::Flowchart,
    variables: HashMap<String, String>,
    config: &ExecutorConfig,
    cancel: CancellationToken,
    call_stack: Vec<String>,
) -> Result<FlowchartResult, ExecutionError> {
    if call_stack.len() > config.max_depth {
        return Err(ExecutionError::MaxDepth(config.max_depth));
    }

    let overall_start = Instant::now();
    let mut blocks_executed: usize = 0;
    let mut walker = GraphWalker::new(flowchart, variables).with_max_blocks(config.max_blocks);
    let mut action = walker.start();

    loop {
        // Check cancellation between every action
        if cancel.is_cancelled() {
            let _ = session.interrupt().await;
            return Ok(FlowchartResult {
                variables: walker.variables().clone(),
                status: ExecutionStatus::Interrupted,
                duration_ms: overall_start.elapsed().as_millis() as u64,
                blocks_executed,
                cost_usd: session.total_cost(),
            });
        }

        // Check pause flag
        if let Some(flag) = &config.pause_flag
            && flag.load(Ordering::Relaxed)
                && let Some(signal) = &config.pause_signal {
                    signal.notified().await;
                }

        match action {
            Action::Done { .. } => {
                return Ok(FlowchartResult {
                    variables: walker.variables().clone(),
                    status: ExecutionStatus::Completed,
                    duration_ms: overall_start.elapsed().as_millis() as u64,
                    blocks_executed,
                    cost_usd: session.total_cost(),
                });
            }

            Action::Exit { exit_code, .. } => {
                return Ok(FlowchartResult {
                    variables: walker.variables().clone(),
                    status: ExecutionStatus::Halted { exit_code },
                    duration_ms: overall_start.elapsed().as_millis() as u64,
                    blocks_executed,
                    cost_usd: session.total_cost(),
                });
            }

            Action::Error { message } => {
                return Ok(FlowchartResult {
                    variables: walker.variables().clone(),
                    status: ExecutionStatus::Error(message),
                    duration_ms: overall_start.elapsed().as_millis() as u64,
                    blocks_executed,
                    cost_usd: session.total_cost(),
                });
            }

            Action::Query {
                ref block_id,
                ref block_name,
                ref prompt,
                ref output_schema,
                ..
            } => {
                blocks_executed += 1;
                let block_id = block_id.clone();
                let block_name = block_name.clone();
                let block_start = Instant::now();
                protocol.on_block_start(&block_id, &block_name, "prompt");

                // If output_schema is set, append JSON format instructions
                let full_prompt = if let Some(schema) = output_schema {
                    format!(
                        "{prompt}\n\nRespond with a JSON object matching this schema:\n```json\n{}\n```",
                        serde_json::to_string_pretty(schema).unwrap_or_default()
                    )
                } else {
                    prompt.clone()
                };

                let result = session.query(&full_prompt, &block_id, &block_name, protocol).await;
                let elapsed = block_start.elapsed();
                let elapsed_ms = elapsed.as_millis() as u64;

                if elapsed_ms > config.soft_timeout_secs * 1000 {
                    protocol.on_log(&format!(
                        "Warning: block '{}' took {:.1}s (soft timeout: {}s)",
                        block_name,
                        elapsed_ms as f64 / 1000.0,
                        config.soft_timeout_secs
                    ));
                }

                match result {
                    Ok(qr) => {
                        protocol.on_block_complete(&block_id, &block_name, true, elapsed_ms);

                        // If output_schema is set, extract JSON fields into variables
                        if output_schema.is_some()
                            && let Some(obj) = extract_json(&qr.response_text) {
                                for (k, v) in &obj {
                                    let val = match v {
                                        serde_json::Value::String(s) => s.clone(),
                                        other => other.to_string(),
                                    };
                                    walker.variables_mut().insert(k.clone(), val);
                                }
                            }

                        action = walker.feed(&qr.response_text);
                    }
                    Err(e) => {
                        protocol.on_block_complete(&block_id, &block_name, false, elapsed_ms);
                        return Err(e);
                    }
                }
            }

            Action::Bash {
                ref block_id,
                ref block_name,
                ref command,
                ref working_directory,
                ..
            } => {
                blocks_executed += 1;
                let block_id = block_id.clone();
                let block_name = block_name.clone();
                let command = command.clone();
                let working_dir = working_directory.clone();
                let block_start = Instant::now();
                protocol.on_block_start(&block_id, &block_name, "bash");

                let bash_result = run_bash(&command, working_dir.as_deref()).await;
                let elapsed_ms = block_start.elapsed().as_millis() as u64;

                match bash_result {
                    Ok((stdout, exit_code)) => {
                        protocol.on_block_complete(
                            &block_id,
                            &block_name,
                            exit_code == 0,
                            elapsed_ms,
                        );
                        action = walker.feed_bash(&stdout, exit_code);
                    }
                    Err(e) => {
                        protocol.on_block_complete(&block_id, &block_name, false, elapsed_ms);
                        return Err(ExecutionError::Bash(e.to_string()));
                    }
                }
            }

            Action::SubCommand {
                ref block_id,
                ref block_name,
                ref command_name,
                ref arguments,
                inherit_variables,
                merge_output,
            } => {
                blocks_executed += 1;
                let block_id = block_id.clone();
                let block_name = block_name.clone();
                let cmd_name = command_name.clone();
                let args_str = arguments.clone();
                let block_start = Instant::now();
                protocol.on_block_start(&block_id, &block_name, "command");

                // Check for direct recursion
                if call_stack.last().is_some_and(|last| *last == cmd_name) {
                    protocol.on_block_complete(&block_id, &block_name, false, 0);
                    return Err(ExecutionError::Other(format!(
                        "Direct recursion detected: '{cmd_name}' calling itself"
                    )));
                }

                // Resolve the sub-command
                let sub_command = resolve_command(&cmd_name, &config.search_paths)?;

                // Build child variables
                let mut child_vars = build_variables(&args_str, &sub_command.arguments)?;
                if inherit_variables {
                    // Parent variables are inherited, but child's own args take precedence
                    for (k, v) in walker.variables() {
                        child_vars.entry(k.clone()).or_insert_with(|| v.clone());
                    }
                }

                // Extend call stack
                let mut child_stack = call_stack.clone();
                child_stack.push(cmd_name);

                // Recurse — Box::pin required for recursive async
                let child_result = Box::pin(run_walker::<S>(
                    session,
                    protocol,
                    sub_command.flowchart,
                    child_vars,
                    config,
                    cancel.clone(),
                    child_stack,
                ))
                .await?;

                let elapsed_ms = block_start.elapsed().as_millis() as u64;
                let success = matches!(child_result.status, ExecutionStatus::Completed);
                protocol.on_block_complete(&block_id, &block_name, success, elapsed_ms);
                blocks_executed += child_result.blocks_executed;

                // Merge output variables if requested
                let merge_vars = if merge_output {
                    child_result
                        .variables
                        .into_iter()
                        .filter(|(k, _)| !k.starts_with('$'))
                        .collect()
                } else {
                    HashMap::new()
                };

                // Get last output for feed
                let output = String::new();
                action = walker.feed_subcommand(&output, merge_vars);
            }

            Action::Clear {
                ref block_id,
                ref block_name,
                ..
            } => {
                blocks_executed += 1;
                let block_id = block_id.clone();
                let block_name = block_name.clone();
                let block_start = Instant::now();
                protocol.on_block_start(&block_id, &block_name, "refresh");

                match session.clear().await {
                    Ok(()) => {
                        let elapsed_ms = block_start.elapsed().as_millis() as u64;
                        protocol.on_block_complete(&block_id, &block_name, true, elapsed_ms);
                        action = walker.feed("");
                    }
                    Err(e) => {
                        let elapsed_ms = block_start.elapsed().as_millis() as u64;
                        protocol.on_block_complete(&block_id, &block_name, false, elapsed_ms);
                        return Err(e);
                    }
                }
            }

            Action::Spawn {
                ref block_id,
                ref block_name,
                ..
            } => {
                let block_id = block_id.clone();
                let block_name = block_name.clone();
                protocol.on_log(&format!(
                    "Warning: Spawn block '{block_name}' not implemented, skipping"
                ));
                protocol.on_block_start(&block_id, &block_name, "spawn");
                protocol.on_block_complete(&block_id, &block_name, true, 0);
                action = walker.feed("");
            }

            Action::Wait {
                ref block_id,
                ref block_name,
            } => {
                let block_id = block_id.clone();
                let block_name = block_name.clone();
                protocol.on_log(&format!(
                    "Warning: Wait block '{block_name}' not implemented, skipping"
                ));
                protocol.on_block_start(&block_id, &block_name, "wait");
                protocol.on_block_complete(&block_id, &block_name, true, 0);
                action = walker.feed("");
            }
        }
    }
}

/// Run a bash command via `sh -c`, capture stdout and exit code.
async fn run_bash(command: &str, working_dir: Option<&str>) -> Result<(String, i32), std::io::Error> {
    let mut cmd = tokio::process::Command::new("sh");
    cmd.arg("-c").arg(command);

    if let Some(dir) = working_dir {
        cmd.current_dir(dir);
    }

    // Capture stdout, let stderr pass through
    cmd.stdout(std::process::Stdio::piped());
    cmd.stderr(std::process::Stdio::piped());

    let output = cmd.output().await?;
    let stdout = String::from_utf8_lossy(&output.stdout).to_string();
    let exit_code = output.status.code().unwrap_or(-1);

    Ok((stdout, exit_code))
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::protocol::Protocol;
    use crate::session::{QueryResult, Session};

    // ---- Mock implementations ----

    struct MockSession {
        responses: Vec<String>,
        idx: usize,
        cost: f64,
    }

    impl MockSession {
        fn new(responses: Vec<&str>) -> Self {
            Self {
                responses: responses.into_iter().map(String::from).collect(),
                idx: 0,
                cost: 0.0,
            }
        }
    }

    impl Session for MockSession {
        async fn query(
            &mut self,
            _prompt: &str,
            _block_id: &str,
            _block_name: &str,
            _protocol: &mut dyn Protocol,
        ) -> Result<QueryResult, ExecutionError> {
            let text = self
                .responses
                .get(self.idx)
                .cloned()
                .unwrap_or_default();
            self.idx += 1;
            self.cost += 0.001;
            Ok(QueryResult {
                response_text: text,
                cost_usd: 0.001,
                duration_ms: 100,
                session_id: Some("mock-session".into()),
            })
        }

        async fn clear(&mut self) -> Result<(), ExecutionError> {
            Ok(())
        }

        async fn stop(&mut self) {}

        async fn interrupt(&mut self) -> Result<(), ExecutionError> {
            Ok(())
        }

        fn total_cost(&self) -> f64 {
            self.cost
        }
    }

    /// Slow mock that takes >1s per query (for timeout tests).
    struct SlowMockSession;

    impl Session for SlowMockSession {
        async fn query(
            &mut self,
            _prompt: &str,
            _block_id: &str,
            _block_name: &str,
            _protocol: &mut dyn Protocol,
        ) -> Result<QueryResult, ExecutionError> {
            tokio::time::sleep(std::time::Duration::from_millis(50)).await;
            Ok(QueryResult {
                response_text: "slow response".into(),
                cost_usd: 0.001,
                duration_ms: 50,
                session_id: None,
            })
        }

        async fn clear(&mut self) -> Result<(), ExecutionError> {
            Ok(())
        }
        async fn stop(&mut self) {}
        async fn interrupt(&mut self) -> Result<(), ExecutionError> {
            Ok(())
        }
        fn total_cost(&self) -> f64 {
            0.0
        }
    }

    #[derive(Debug, Clone)]
    enum ProtocolEvent {
        BlockStart {
            block_id: String,
            block_name: String,
            block_type: String,
        },
        BlockComplete {
            block_id: String,
            block_name: String,
            success: bool,
        },
        StreamText(String),
        FlowchartStart {
            command: String,
            block_count: usize,
        },
        FlowchartComplete,
        Log(String),
    }

    struct MockProtocol {
        events: Vec<ProtocolEvent>,
    }

    impl MockProtocol {
        fn new() -> Self {
            Self { events: Vec::new() }
        }
    }

    impl Protocol for MockProtocol {
        fn on_block_start(&mut self, block_id: &str, block_name: &str, block_type: &str) {
            self.events.push(ProtocolEvent::BlockStart {
                block_id: block_id.into(),
                block_name: block_name.into(),
                block_type: block_type.into(),
            });
        }

        fn on_block_complete(
            &mut self,
            block_id: &str,
            block_name: &str,
            success: bool,
            _duration_ms: u64,
        ) {
            self.events.push(ProtocolEvent::BlockComplete {
                block_id: block_id.into(),
                block_name: block_name.into(),
                success,
            });
        }

        fn on_stream_text(&mut self, text: &str) {
            self.events.push(ProtocolEvent::StreamText(text.into()));
        }

        fn on_flowchart_start(&mut self, command: &str, _args: &str, block_count: usize) {
            self.events.push(ProtocolEvent::FlowchartStart {
                command: command.into(),
                block_count,
            });
        }

        fn on_flowchart_complete(&mut self, _result: &FlowchartResult) {
            self.events.push(ProtocolEvent::FlowchartComplete);
        }

        fn on_forwarded_message(
            &mut self,
            _msg: &serde_json::Value,
            _block_id: &str,
            _block_name: &str,
        ) {
        }

        fn on_log(&mut self, message: &str) {
            self.events.push(ProtocolEvent::Log(message.into()));
        }
    }

    fn make_command(
        name: &str,
        blocks: Vec<(&str, flowchart::Block)>,
        connections: Vec<flowchart::Connection>,
    ) -> Command {
        Command {
            name: name.into(),
            description: None,
            arguments: Vec::new(),
            flowchart: flowchart::Flowchart {
                name: None,
                blocks: blocks
                    .into_iter()
                    .map(|(id, b)| (id.to_owned(), b))
                    .collect(),
                connections,
                sessions: None,
            },
        }
    }

    fn block(name: &str, data: flowchart::BlockData) -> flowchart::Block {
        flowchart::Block {
            name: name.into(),
            data,
            extra: HashMap::new(),
        }
    }

    fn conn(source: &str, target: &str) -> flowchart::Connection {
        flowchart::Connection {
            source_id: source.into(),
            target_id: target.into(),
            is_true_path: None,
        }
    }

    fn branch_conn(source: &str, target: &str, is_true: bool) -> flowchart::Connection {
        flowchart::Connection {
            source_id: source.into(),
            target_id: target.into(),
            is_true_path: Some(is_true),
        }
    }

    fn default_config() -> ExecutorConfig {
        ExecutorConfig::default()
    }

    // ---- Tests ----

    #[tokio::test]
    async fn linear_flowchart() {
        let cmd = make_command(
            "test",
            vec![
                ("s", block("Begin", flowchart::BlockData::Start)),
                (
                    "p",
                    block(
                        "Ask",
                        flowchart::BlockData::Prompt {
                            prompt: "Hello".into(),
                            output_variable: Some("answer".into()),
                            session: None,
                            output_schema: None,
                        },
                    ),
                ),
                ("e", block("Done", flowchart::BlockData::End)),
            ],
            vec![conn("s", "p"), conn("p", "e")],
        );

        let mut session = MockSession::new(vec!["world"]);
        let mut protocol = MockProtocol::new();
        let cancel = CancellationToken::new();

        let result = run_flowchart(&mut session, &mut protocol, &cmd, "", &default_config(), cancel)
            .await
            .unwrap();

        assert!(matches!(result.status, ExecutionStatus::Completed));
        assert_eq!(result.variables["answer"], "world");
        assert_eq!(result.blocks_executed, 1);
        assert!(result.cost_usd > 0.0);
    }

    #[tokio::test]
    async fn branch_evaluation() {
        let cmd = make_command(
            "test",
            vec![
                ("s", block("Begin", flowchart::BlockData::Start)),
                (
                    "v",
                    block(
                        "Set",
                        flowchart::BlockData::Variable {
                            variable_name: "flag".into(),
                            variable_value: "true".into(),
                            variable_type: None,
                        },
                    ),
                ),
                (
                    "b",
                    block(
                        "Check",
                        flowchart::BlockData::Branch {
                            condition: "flag".into(),
                        },
                    ),
                ),
                (
                    "yes",
                    block(
                        "Yes",
                        flowchart::BlockData::Variable {
                            variable_name: "result".into(),
                            variable_value: "took_true".into(),
                            variable_type: None,
                        },
                    ),
                ),
                (
                    "no",
                    block(
                        "No",
                        flowchart::BlockData::Variable {
                            variable_name: "result".into(),
                            variable_value: "took_false".into(),
                            variable_type: None,
                        },
                    ),
                ),
                ("e", block("Done", flowchart::BlockData::End)),
            ],
            vec![
                conn("s", "v"),
                conn("v", "b"),
                branch_conn("b", "yes", true),
                branch_conn("b", "no", false),
                conn("yes", "e"),
                conn("no", "e"),
            ],
        );

        let mut session = MockSession::new(vec![]);
        let mut protocol = MockProtocol::new();
        let cancel = CancellationToken::new();

        let result = run_flowchart(&mut session, &mut protocol, &cmd, "", &default_config(), cancel)
            .await
            .unwrap();

        assert!(matches!(result.status, ExecutionStatus::Completed));
        assert_eq!(result.variables["result"], "took_true");
    }

    #[tokio::test]
    async fn bash_block_real_subprocess() {
        let cmd = make_command(
            "test",
            vec![
                ("s", block("Begin", flowchart::BlockData::Start)),
                (
                    "b",
                    block(
                        "Echo",
                        flowchart::BlockData::Bash {
                            command: "echo hello".into(),
                            output_variable: Some("output".into()),
                            working_directory: None,
                            continue_on_error: None,
                            exit_code_variable: Some("rc".into()),
                        },
                    ),
                ),
                ("e", block("Done", flowchart::BlockData::End)),
            ],
            vec![conn("s", "b"), conn("b", "e")],
        );

        let mut session = MockSession::new(vec![]);
        let mut protocol = MockProtocol::new();
        let cancel = CancellationToken::new();

        let result = run_flowchart(&mut session, &mut protocol, &cmd, "", &default_config(), cancel)
            .await
            .unwrap();

        assert!(matches!(result.status, ExecutionStatus::Completed));
        assert_eq!(result.variables["output"], "hello");
        assert_eq!(result.variables["rc"], "0");
    }

    #[tokio::test]
    async fn variable_building_with_args() {
        let mut cmd = make_command(
            "test",
            vec![
                ("s", block("Begin", flowchart::BlockData::Start)),
                (
                    "p",
                    block(
                        "Ask",
                        flowchart::BlockData::Prompt {
                            prompt: "Write about $1 in {{style}} style".into(),
                            output_variable: None,
                            session: None,
                            output_schema: None,
                        },
                    ),
                ),
                ("e", block("Done", flowchart::BlockData::End)),
            ],
            vec![conn("s", "p"), conn("p", "e")],
        );

        cmd.arguments = vec![
            flowchart::Argument {
                name: "topic".into(),
                description: None,
                required: Some(true),
                default: None,
            },
            flowchart::Argument {
                name: "style".into(),
                description: None,
                required: Some(false),
                default: Some("formal".into()),
            },
        ];

        let mut session = MockSession::new(vec!["ok"]);
        let mut protocol = MockProtocol::new();
        let cancel = CancellationToken::new();

        let result =
            run_flowchart(&mut session, &mut protocol, &cmd, "dragons", &default_config(), cancel)
                .await
                .unwrap();

        assert!(matches!(result.status, ExecutionStatus::Completed));
        // The prompt should have been interpolated with $1=dragons, style=formal
        assert_eq!(result.variables["topic"], "dragons");
        assert_eq!(result.variables["style"], "formal");
    }

    #[tokio::test]
    async fn json_extraction_with_output_schema() {
        let schema = serde_json::json!({
            "type": "object",
            "properties": {
                "title": {"type": "string"},
                "rating": {"type": "number"}
            }
        });

        let cmd = make_command(
            "test",
            vec![
                ("s", block("Begin", flowchart::BlockData::Start)),
                (
                    "p",
                    block(
                        "Ask",
                        flowchart::BlockData::Prompt {
                            prompt: "Rate this".into(),
                            output_variable: Some("raw".into()),
                            session: None,
                            output_schema: Some(schema),
                        },
                    ),
                ),
                ("e", block("Done", flowchart::BlockData::End)),
            ],
            vec![conn("s", "p"), conn("p", "e")],
        );

        let mut session =
            MockSession::new(vec![r#"Here's my rating: {"title": "Great", "rating": 9}"#]);
        let mut protocol = MockProtocol::new();
        let cancel = CancellationToken::new();

        let result = run_flowchart(&mut session, &mut protocol, &cmd, "", &default_config(), cancel)
            .await
            .unwrap();

        assert!(matches!(result.status, ExecutionStatus::Completed));
        assert_eq!(result.variables["title"], "Great");
        assert_eq!(result.variables["rating"], "9");
        // Raw output also stored
        assert!(result.variables["raw"].contains("rating"));
    }

    #[tokio::test]
    async fn block_count_safety_limit() {
        // Cyclic graph: variable points back to itself
        let cmd = make_command(
            "test",
            vec![
                ("s", block("Begin", flowchart::BlockData::Start)),
                (
                    "v",
                    block(
                        "Loop",
                        flowchart::BlockData::Variable {
                            variable_name: "x".into(),
                            variable_value: "1".into(),
                            variable_type: None,
                        },
                    ),
                ),
                ("e", block("Done", flowchart::BlockData::End)),
            ],
            vec![conn("s", "v"), conn("v", "v")],
        );

        let mut session = MockSession::new(vec![]);
        let mut protocol = MockProtocol::new();
        let config = ExecutorConfig {
            max_blocks: 10,
            ..Default::default()
        };
        let cancel = CancellationToken::new();

        let result = run_flowchart(&mut session, &mut protocol, &cmd, "", &config, cancel)
            .await
            .unwrap();

        assert!(matches!(result.status, ExecutionStatus::Error(_)));
    }

    #[tokio::test]
    async fn cancellation_mid_execution() {
        let cmd = make_command(
            "test",
            vec![
                ("s", block("Begin", flowchart::BlockData::Start)),
                (
                    "p1",
                    block(
                        "First",
                        flowchart::BlockData::Prompt {
                            prompt: "first".into(),
                            output_variable: None,
                            session: None,
                            output_schema: None,
                        },
                    ),
                ),
                (
                    "p2",
                    block(
                        "Second",
                        flowchart::BlockData::Prompt {
                            prompt: "second".into(),
                            output_variable: None,
                            session: None,
                            output_schema: None,
                        },
                    ),
                ),
                ("e", block("Done", flowchart::BlockData::End)),
            ],
            vec![conn("s", "p1"), conn("p1", "p2"), conn("p2", "e")],
        );

        let session = MockSession::new(vec!["first response", "second response"]);
        let mut protocol = MockProtocol::new();
        let cancel = CancellationToken::new();

        // Cancel after the first query completes
        // We need a custom session that cancels after first query
        struct CancellingSession {
            inner: MockSession,
            cancel: CancellationToken,
            queries: usize,
        }

        impl Session for CancellingSession {
            async fn query(
                &mut self,
                prompt: &str,
                block_id: &str,
                block_name: &str,
                protocol: &mut dyn Protocol,
            ) -> Result<QueryResult, ExecutionError> {
                self.queries += 1;
                if self.queries > 1 {
                    self.cancel.cancel();
                }
                self.inner.query(prompt, block_id, block_name, protocol).await
            }

            async fn clear(&mut self) -> Result<(), ExecutionError> {
                self.inner.clear().await
            }
            async fn stop(&mut self) {
                self.inner.stop().await;
            }
            async fn interrupt(&mut self) -> Result<(), ExecutionError> {
                self.inner.interrupt().await
            }
            fn total_cost(&self) -> f64 {
                self.inner.total_cost()
            }
        }

        let cancel2 = cancel.clone();
        let mut cancel_session = CancellingSession {
            inner: session,
            cancel: cancel2,
            queries: 0,
        };

        let result =
            run_flowchart(&mut cancel_session, &mut protocol, &cmd, "", &default_config(), cancel)
                .await
                .unwrap();

        assert!(matches!(result.status, ExecutionStatus::Interrupted));
    }

    #[tokio::test]
    async fn soft_timeout_warning() {
        let cmd = make_command(
            "test",
            vec![
                ("s", block("Begin", flowchart::BlockData::Start)),
                (
                    "p",
                    block(
                        "Slow",
                        flowchart::BlockData::Prompt {
                            prompt: "slow query".into(),
                            output_variable: None,
                            session: None,
                            output_schema: None,
                        },
                    ),
                ),
                ("e", block("Done", flowchart::BlockData::End)),
            ],
            vec![conn("s", "p"), conn("p", "e")],
        );

        let mut session = SlowMockSession;
        let mut protocol = MockProtocol::new();
        // Set soft timeout to 0 so even a fast query triggers the warning
        let config = ExecutorConfig {
            soft_timeout_secs: 0,
            ..Default::default()
        };
        let cancel = CancellationToken::new();

        let result = run_flowchart(&mut session, &mut protocol, &cmd, "", &config, cancel)
            .await
            .unwrap();

        assert!(matches!(result.status, ExecutionStatus::Completed));
        // Check that a warning was logged
        let has_warning = protocol
            .events
            .iter()
            .any(|e| matches!(e, ProtocolEvent::Log(msg) if msg.contains("Warning")));
        assert!(has_warning);
    }

    #[tokio::test]
    async fn exit_block() {
        let cmd = make_command(
            "test",
            vec![
                ("s", block("Begin", flowchart::BlockData::Start)),
                (
                    "x",
                    block(
                        "Bail",
                        flowchart::BlockData::Exit {
                            exit_code: Some(42),
                        },
                    ),
                ),
                ("e", block("Done", flowchart::BlockData::End)),
            ],
            vec![conn("s", "x"), conn("x", "e")],
        );

        let mut session = MockSession::new(vec![]);
        let mut protocol = MockProtocol::new();
        let cancel = CancellationToken::new();

        let result = run_flowchart(&mut session, &mut protocol, &cmd, "", &default_config(), cancel)
            .await
            .unwrap();

        assert!(matches!(result.status, ExecutionStatus::Halted { exit_code: 42 }));
    }

    #[tokio::test]
    async fn clear_block() {
        let cmd = make_command(
            "test",
            vec![
                ("s", block("Begin", flowchart::BlockData::Start)),
                (
                    "r",
                    block(
                        "Refresh",
                        flowchart::BlockData::Refresh {
                            target_session: None,
                        },
                    ),
                ),
                (
                    "p",
                    block(
                        "Ask",
                        flowchart::BlockData::Prompt {
                            prompt: "hello".into(),
                            output_variable: None,
                            session: None,
                            output_schema: None,
                        },
                    ),
                ),
                ("e", block("Done", flowchart::BlockData::End)),
            ],
            vec![conn("s", "r"), conn("r", "p"), conn("p", "e")],
        );

        let mut session = MockSession::new(vec!["ok"]);
        let mut protocol = MockProtocol::new();
        let cancel = CancellationToken::new();

        let result = run_flowchart(&mut session, &mut protocol, &cmd, "", &default_config(), cancel)
            .await
            .unwrap();

        assert!(matches!(result.status, ExecutionStatus::Completed));
        // Should have both clear and query events
        let block_types: Vec<_> = protocol
            .events
            .iter()
            .filter_map(|e| match e {
                ProtocolEvent::BlockStart { block_type, .. } => Some(block_type.as_str()),
                _ => None,
            })
            .collect();
        assert!(block_types.contains(&"refresh"));
        assert!(block_types.contains(&"prompt"));
    }
}
