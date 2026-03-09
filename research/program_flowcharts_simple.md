# Program Flowcharts — The Simple Version

## The Problem with JSON Flowcharts

JSON flowcharts are secretly a bad programming language. They have variables, conditionals, function calls, and sequential execution — but they're missing everything that makes a real language useful:

- No loops (except cycling the graph)
- No error handling (no try/catch, no retries)
- No string manipulation
- No imports or libraries

Once a flowchart gets complex, you're fighting the JSON instead of solving the problem.

### A Real Example

Say you want a flowchart that writes a story, critiques it, then rewrites it. In JSON, that's ~50 lines of blocks, connections, and variable wiring:

```json
{
  "flowchart": {
    "blocks": {
      "start": {
        "type": "start",
        "name": "Begin"
      },
      "write_draft": {
        "type": "prompt",
        "name": "Write Draft",
        "prompt": "Write a short story about $1",
        "output_variable": "draft"
      },
      "critique": {
        "type": "prompt",
        "name": "Critique",
        "prompt": "Critique this story:\n{{draft}}",
        "output_variable": "feedback"
      },
      "clear_context": {
        "type": "refresh",
        "name": "Clear Context"
      },
      "rewrite": {
        "type": "prompt",
        "name": "Rewrite",
        "prompt": "Write an improved story about $1. Address: {{feedback}}"
      },
      "end": {
        "type": "end",
        "name": "Done"
      }
    },
    "connections": [
      {"source_id": "start", "target_id": "write_draft"},
      {"source_id": "write_draft", "target_id": "critique"},
      {"source_id": "critique", "target_id": "clear_context"},
      {"source_id": "clear_context", "target_id": "rewrite"},
      {"source_id": "rewrite", "target_id": "end"}
    ]
  }
}
```

Now the same thing as a program:

```python
from flowcoder import runtime

args = runtime.start()
topic = args["topic"]

draft = runtime.query(f"Write a short story about {topic}")
critique = runtime.query(f"Critique this story:\n{draft}")
runtime.clear()
final = runtime.query(f"Write an improved story about {topic}. Address: {critique}")

runtime.finish(final)
```

7 lines vs 50+. And the program version is instantly readable to anyone who knows Python.

---

## What Changes

**Nothing breaks.** JSON flowcharts keep working exactly as they do now. Program flowcharts are a second option — users pick whichever fits the task.

Under the hood, both produce the same `Action` values. The consumer (axi, a GUI, whatever) doesn't know or care which one is running. Same events, same interface, same everything from the outside.

```
JSON flowchart    -->  GraphWalker     -->  Action  -->  consumer handles it
Program flowchart -->  JSON-RPC adapter -->  Action  -->  consumer handles it (same code)
```

---

## Why Programs Win for Complex Flows

### 1. Error Handling

JSON flowcharts can't handle errors. If a prompt fails, the flowchart fails. Period.

Programs can retry, fall back, or adapt:

```python
args = runtime.start()

# Try the fast model first, fall back to the big one
try:
    result = runtime.query("Solve this math problem: " + args["problem"])
except RuntimeError:
    runtime.clear()
    result = runtime.query(
        "This is a hard problem. Think step by step.\n" + args["problem"]
    )

runtime.finish(result)
```

There's no way to express this in JSON. You'd need a "try block" block type, an "on error" connection type, and suddenly you're inventing a programming language inside JSON.

### 2. Loops

Want to iteratively refine output until it meets a quality bar? Easy in code, impossible in JSON:

```python
args = runtime.start()

draft = runtime.query(f"Write a poem about {args['topic']}")

for i in range(3):
    review = runtime.query(f"Rate this poem 1-10 and explain why:\n{draft}")
    if '"score": 8' in review or '"score": 9' in review or '"score": 10' in review:
        break
    runtime.clear()
    draft = runtime.query(
        f"Rewrite this poem incorporating the feedback:\n{review}"
    )

runtime.finish(draft)
```

### 3. String Manipulation and Logic

Programs can parse, transform, split, regex, whatever. JSON gives you `{{variable}}` templates and that's it.

```python
args = runtime.start()

# Generate 5 test cases, parse them, run each one
test_plan = runtime.query("List 5 edge cases for this function, one per line")
cases = [line.strip() for line in test_plan.strip().split("\n") if line.strip()]

results = []
for case in cases:
    runtime.clear()
    result = runtime.query(f"Write a test for this edge case: {case}")
    results.append(result)

runtime.finish("\n\n---\n\n".join(results))
```

### 4. Conditional Logic That Isn't Painful

JSON branch blocks evaluate simple conditions. Anything beyond `== "yes"` or `contains "error"` gets ugly fast. Programs just use `if`:

```python
args = runtime.start()

analysis = runtime.query(f"Analyze this code for bugs:\n{args['code']}")

if "no bugs found" in analysis.lower():
    runtime.finish("Code looks clean. No changes needed.")
else:
    runtime.clear()
    fix = runtime.query(f"Fix these bugs:\n{analysis}\n\nOriginal code:\n{args['code']}")
    runtime.finish(fix)
```

---

## They Compose Together

JSON and program flowcharts are interchangeable at the command boundary:

- A JSON flowchart's `command` block can call a program flowchart (it just gets the result back)
- A program can call `run_command("some-json-flow", args)` to run a JSON flowchart
- A program can call another program the same way

The user invoking `/story dragons` doesn't know or care if it's JSON or Python underneath. Discovery, argument validation, session config — all of that stays in `command.json` either way:

```json
{
  "name": "story-writer",
  "description": "Writes stories with revision",
  "type": "program",
  "entrypoint": "main.py",
  "arguments": [
    {"name": "topic", "description": "The topic", "required": true}
  ],
  "session": {
    "model": "claude-sonnet",
    "permission_mode": "auto"
  }
}
```

---

## How It Works Technically

The program talks to the flowcoder runtime over JSON-RPC on stdin/stdout. Three core operations:

| Call | What it does |
|---|---|
| `runtime.start()` | Receive arguments and variables, begin execution |
| `runtime.query(prompt)` | Send a prompt to the coding agent, get the result back |
| `runtime.clear()` | Reset the agent's conversation (fresh context) |
| `runtime.finish(result)` | Return the final output, done |

That's it. The program calls these when it needs the agent. Everything else — loops, conditions, string ops, error handling, bash commands — it does natively in its own language.

The runtime also streams intermediate events (agent reading files, writing code, etc.) as JSON-RPC notifications during `query()` calls. Simple programs ignore them. Advanced programs can hook into them:

```python
def on_event(event):
    if event["type"] == "tool_use":
        print(f"Agent is using: {event['tool']}")

result = runtime.query("fix the bug", on_event=on_event)
```

---

## What About Security?

JSON flowcharts with bash blocks already run arbitrary code. Program flowcharts don't open any new security hole — they're just a more explicit way to do what was already possible. The trust boundary is the same: you trust the flowchart author.

The program doesn't get direct agent access. It goes through the runtime, which goes through the consumer's agent session with whatever permission controls are in place.

---

## The Pitch in One Paragraph

JSON flowcharts are great for simple, visual, linear flows. But the moment you need error handling, loops, string parsing, or complex conditionals, you're fighting the format. Program flowcharts let users write control flow in a real language while keeping everything else the same — discovery, composability, session management, and the consumer interface. Both produce the same `Action` values. Both are first-class citizens. Use whichever fits the job.
