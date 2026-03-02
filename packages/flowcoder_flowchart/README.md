# flowcoder-flowchart

Pure data library for defining flowchart workflows. No I/O, no async — just Pydantic models, serialization, and validation.

## Purpose

Defines the data model for flowchart-based AI workflows: blocks (prompt, branch, bash, variable, command, refresh), connections between them, sessions, and commands. Used by flowcoder-engine for execution and flowcoder-tui for display.

## Usage

### Defining a flowchart

```python
from flowcoder_flowchart import (
    Flowchart, Connection, StartBlock, PromptBlock, EndBlock, BlockType
)

flowchart = Flowchart(
    name="greet",
    blocks={
        "start": StartBlock(id="start", name="Start"),
        "ask": PromptBlock(id="ask", name="Ask", prompt="Say hello to $1"),
        "end": EndBlock(id="end", name="End"),
    },
    connections=[
        Connection(source_id="start", target_id="ask"),
        Connection(source_id="ask", target_id="end"),
    ],
)
```

### I/O

```python
from flowcoder_flowchart import load, dump, save, load_command, save_command

# JSON string / dict / file path
flowchart = load("flowchart.json")
json_str = dump(flowchart)
save(flowchart, "output.json")

# Commands (named flowcharts with metadata)
cmd = load_command("commands/story.json")
save_command(cmd, "commands/story.json")
```

### Validation

```python
from flowcoder_flowchart import validate

result = validate(flowchart)
if not result.valid:
    for error in result.errors:
        print(error)
```

### Templates

```python
from flowcoder_flowchart import parse_template

parts = parse_template("Write a story about $1 featuring {{hero}}")
# [Literal("Write a story about "), ArgRef(1), Literal(" featuring "), VarRef("hero")]
```

## Block Types

| Block | Description |
|---|---|
| `StartBlock` | Entry point (exactly one per flowchart) |
| `EndBlock` | Exit point |
| `PromptBlock` | Send prompt to a Claude session, optionally capture output |
| `BranchBlock` | Evaluate condition, follow true/false path |
| `VariableBlock` | Set/coerce a variable (string, number, boolean, json) |
| `BashBlock` | Run shell command, capture stdout |
| `CommandBlock` | Execute a sub-flowchart (recursion) |
| `RefreshBlock` | Clear session history (restart subprocess) |

## API

### Models

`Flowchart`, `Connection`, `Argument`, `SessionConfig`, `Command`, `CommandMetadata`, `Position`

### Blocks

`StartBlock`, `EndBlock`, `PromptBlock`, `BranchBlock`, `VariableBlock`, `BashBlock`, `CommandBlock`, `RefreshBlock`, `Block` (union), `BlockBase`, `BlockType` (enum), `VariableType` (enum)

### I/O

`load()`, `dump()`, `save()`, `load_command()`, `dump_command()`, `save_command()`

### Templates

`parse_template()`, `Literal`, `ArgRef`, `VarRef`, `TemplatePart`

### Validation

`validate()`, `ValidationResult`

## Dependencies

- `pydantic>=2.0`

Requires Python 3.12+.
