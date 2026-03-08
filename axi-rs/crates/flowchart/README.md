# flowchart

A flowchart execution engine. Define a graph of blocks — prompts, bash commands, branches, variables — and the walker traverses the graph, telling you what to execute at each step.

Agent-agnostic. Application-agnostic. No async runtime required.

```toml
[dependencies]
flowchart = { path = "crates/flowchart" }
```

## Quick Start

Define a flowchart in JSON:

```json
{
  "name": "story",
  "description": "Write a story about a topic",
  "arguments": [
    { "name": "topic", "description": "What to write about", "required": true }
  ],
  "flowchart": {
    "blocks": {
      "start_1": { "name": "Start", "type": "start" },
      "prompt_1": {
        "name": "Write Story",
        "type": "prompt",
        "prompt": "Write a short story about {{topic}}.",
        "output_variable": "story"
      },
      "end_1": { "name": "End", "type": "end" }
    },
    "connections": [
      { "source_id": "start_1", "target_id": "prompt_1" },
      { "source_id": "prompt_1", "target_id": "end_1" }
    ]
  }
}
```

Run it:

```rust
use flowchart::{GraphWalker, Action, parse};
use std::collections::HashMap;

let command = parse::from_file("commands/story.json")?;
let mut vars = HashMap::from([("topic".into(), "dragons".into())]);
let mut walker = GraphWalker::new(command.flowchart, vars);
let mut action = walker.start();

loop {
    action = match action {
        Action::Query { prompt, session, .. } => {
            let result = my_agent.query(&prompt, session.as_deref()).await;
            walker.feed(&result)
        }
        Action::Bash { command, .. } => {
            let output = run_shell(&command).await;
            walker.feed(&output)
        }
        Action::SubCommand { command_name, arguments, .. } => {
            let result = run_subcommand(&command_name, &arguments).await;
            walker.feed(&result)
        }
        Action::Clear { session, .. } => {
            my_agent.clear(session.as_deref()).await;
            walker.feed("")
        }
        Action::Done { output } => break println!("{}", output.unwrap_or_default()),
        Action::Error { message } => break eprintln!("error: {message}"),
    };
}
```

You provide the agent and bash implementations. The walker handles graph traversal, variable interpolation, branch evaluation, and block sequencing.

## Block Types

| Type | Description |
|---|---|
| `start` | Entry point. Every flowchart has exactly one. |
| `end` | Exit point. Returns the value of a variable as the flowchart output. |
| `prompt` | Sends a prompt to a coding agent. Stores the response in a variable. |
| `branch` | Evaluates a condition and follows the true or false edge. |
| `variable` | Sets a variable to an interpolated value. |
| `bash` | Runs a shell command. Optionally stores stdout and exit code in variables. |
| `command` | Executes another flowchart as a sub-command. |
| `refresh` | Clears/restarts an agent session. |

Blocks that don't need external IO (`start`, `end`, `variable`, `branch`) are resolved internally by the walker. Your dispatch loop only sees `Query`, `Bash`, `SubCommand`, and `Clear` actions.

## Variables and Interpolation

Templates use `{{variable_name}}`:

```json
{ "prompt": "Review this code:\n{{code}}\n\nFocus on: {{focus_area}}" }
```

Positional arguments use `$1`, `$2`, etc. Variables are populated by:
- Arguments passed to `GraphWalker::new`
- `variable` blocks
- Output of `prompt` and `bash` blocks (`output_variable` field)

## Branch Conditions

Branch blocks evaluate a condition string and follow the matching edge:

```json
{ "type": "branch", "name": "Check Status", "condition": "status == \"done\"" }
```

Supported expressions:
- Truthiness: `flag` — true if non-empty and not `"false"`
- Negation: `!flag`
- Comparisons: `==`, `!=`, `>`, `<`, `>=`, `<=`
- Quoted values: `status == "done"`
- Numeric coercion when both sides parse as numbers

## Multiple Sessions

Prompt and refresh blocks accept a `session` field to target a named agent session:

```json
{
  "type": "prompt",
  "prompt": "Review this code: {{code}}",
  "output_variable": "review",
  "session": "reviewer"
}
```

Sessions are declared in the flowchart config and created by your application. The walker includes the session name in each action — your dispatch loop routes to the right agent.

## Execution Limits

The walker enforces a maximum block count (default 1000) to prevent runaway execution in cyclic graphs:

```rust
let mut walker = GraphWalker::new(flowchart, vars)
    .with_max_blocks(500);
```

When the limit is reached, the walker returns `Action::Error`.

## Command Resolution

Resolve commands by name from a set of search paths:

```rust
use flowchart::resolve;

let paths = vec![PathBuf::from("./commands"), PathBuf::from("~/.flowchart/commands")];
let command = resolve::resolve_command("story", &paths)?;
let commands = resolve::list_commands(&paths);
```

## Errors

- **`ParseError`** — invalid JSON or missing required fields
- **`ValidationError`** — structural problems (no start block, dangling connections, branch without two edges)
- **`WalkerError`** — runtime issues (missing variable, block limit exceeded, no outgoing connection)

Validate before execution:

```rust
use flowchart::validate;

if let Err(errors) = validate::validate(&flowchart) {
    for e in &errors {
        eprintln!("{e}");
    }
}
```
