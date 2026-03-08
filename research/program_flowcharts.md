# Program Flowcharts

## Design

### The Problem

YAML flowcharts are already a bad programming language. They have assignments (variable blocks), conditionals (branch blocks), function calls (command blocks), and sequential execution. But they can't loop except by cycling the graph, can't do error handling, can't do string manipulation, can't import libraries. Once a flowchart gets complex enough, you're fighting the YAML.

### The Idea

Let users write flowcharts as actual programs (Python, shell, any language) that communicate with the flowcoder runtime via JSON-RPC over stdio. The program replaces the graph walker — it IS the control flow. The runtime provides access to the agent session.

The program only needs the runtime for things it can't do itself: talking to the coding agent. Everything else — bash, variables, loops, conditionals, error handling — it does natively in its own language.

### How It Fits the Architecture

With approach 3+6 (single crate, pure state machine walker), the `Action` enum is the public interface. The YAML `GraphWalker` is one producer of `Action` values. A program flowchart runtime adapter is a second producer — it translates JSON-RPC calls from the subprocess into the same `Action` variants. The consumer's dispatch loop doesn't change.

```
YAML flowchart  →  GraphWalker  →  Action  →  Consumer dispatches
Program flowchart  →  JSON-RPC adapter  →  Action  →  Consumer dispatches (same code)
```

### Protocol

The program is the driver. It calls the runtime (request/response). Three core operations:

```
program → runtime:
  query(prompt) → result       # send prompt to the coding agent
  clear()                      # reset agent conversation
  finish(result)               # return final output, terminate

runtime → program:
  initialize(args, variables)  # startup handshake
  notification(event)          # intermediate agent events during query (optional)
```

JSON-RPC over stdio. Notifications (no `id`) stream intermediate agent events during pending queries — the client library swallows them by default, exposes via callback for programs that care.

### Example

```python
#!/usr/bin/env python3
from flowcoder import runtime

args = runtime.start()
topic = args["topic"]

draft = runtime.query(f"Write a short story about {topic}")
critique = runtime.query(f"Critique this story:\n{draft}")
runtime.clear()  # fresh context
final = runtime.query(f"Write an improved story about {topic}. Address: {critique}")

runtime.finish(final)
```

Compare to expressing that same three-prompt-with-critique flow in YAML with variable blocks and connections.

### Discovery

`command.yaml` gains a `type` field:

```yaml
name: story-writer
description: Writes stories with revision
type: program
entrypoint: main.py
arguments:
  - name: topic
    description: The topic
    required: true
session:
  model: claude-sonnet
  permission_mode: auto
```

Arguments and session config stay in YAML — the consumer needs them before spawning the program. The graph definition moves into the program.

### Composability

YAML and program flowcharts are interchangeable at the command boundary:

- YAML `command` block calls a program flowchart → runtime spawns it, gets result
- Program calls `run_command(name, args)` → runtime runs YAML or program, returns result
- Program calls program → same mechanism, recursive

### Why This Matters

The single strongest argument: **error handling.** YAML flowcharts can't try/catch. They can't retry a failed prompt with different wording. They can't fall back to an alternative approach. Programs can.

### Impact on Current Work

None. Build the YAML walker with `Action` as the public interface. Don't couple consumer dispatch to `GraphWalker`. Program flowcharts slot in later as a second producer of the same `Action` types.

---

# Design Review

## What's Being Reviewed

The program flowchart concept described above: programs drive flowchart execution by calling into the flowcoder runtime over JSON-RPC. This review covers protocol design, lifecycle, security, error handling, streaming, observability, composability, concurrency, discovery, language support, testing, and the fundamental value proposition.

---

## 1. Protocol Design

**Direction of control.** The program is the driver. It calls the runtime, not the other way around. This means the protocol is request/response from the program's perspective: program sends `query(prompt)`, blocks until it gets a result. The runtime is a server that the program calls into.

But there's a wrinkle: **the runtime needs to push events during a query.** An agent query can take 30+ seconds. During that time, the agent is reading files, writing code, running commands. The consumer wants to see those intermediate events. The program might also want to see them (to react, to log, to display progress).

**Options for event streaming during a query:**

- **(a) Notifications on the response stream.** JSON-RPC supports notifications (no `id`). During a pending `query` call, the runtime sends notification messages for intermediate events. The program's client library buffers or ignores them. The final response arrives as the normal JSON-RPC result. This is clean — one connection, standard JSON-RPC.

- **(b) Separate event channel.** Second pipe or fd for events. Adds complexity for no real gain over (a).

- **(c) Polling.** Program calls `get_events()` periodically. Terrible — adds latency, complexity, and the program is blocked waiting on `query` anyway.

**Recommendation: (a).** Standard JSON-RPC notifications. Simple programs ignore them (client library swallows notifications by default). Advanced programs register a callback.

**Protocol versioning.** Include a `handshake` as the first exchange — program sends its protocol version, runtime responds with its version and capabilities. Allows evolution. MCP and LSP both do this, proven pattern.

---

## 2. Process Lifecycle

The program is a subprocess spawned by the consumer (axi, GUI, etc.). The runtime is the parent process (or a thread/task in the parent).

**Startup:** Consumer spawns program, connects stdio, sends `initialize` with arguments and variables. Program does its work, calling runtime methods as needed.

**Normal termination:** Program exits with code 0. Its final `emit("result", value)` call (or stdout? or return value of a `finish()` call?) provides the output.

**Crash:** Program exits non-zero or gets killed. Consumer detects broken pipe / exit code and treats it as a failed flowchart execution. Any in-flight agent query should be cancelled.

**Timeout:** Consumer can enforce a wall-clock timeout. Kills the program, cancels agent session.

**Key question: how does the program return its result?**

Options:
- **(a) Explicit `finish(result)` RPC call.** Program calls `runtime.finish("the output")` as its last action. Clean, explicit.
- **(b) Exit code + last emitted value.** Implicit. Fragile.
- **(c) The program's stdout IS the result.** But stdout is used for JSON-RPC. So no.

**Recommendation: (a).** Explicit `finish(result)`. The program must call it. If the program exits without calling `finish`, the consumer treats it as an error. If the program calls `finish` and then keeps running, the runtime ignores further calls (or errors).

---

## 3. Security & Trust Model

**Who writes program flowcharts?** Same people who write YAML flowcharts — the user, command authors, potentially third parties. The program runs with the same permissions as the parent process. It's an arbitrary executable.

**This is not meaningfully different from YAML flowcharts with bash blocks.** A YAML flowchart with a bash block already runs arbitrary code. A program flowchart just makes it more explicit. The trust boundary is the same: you trust the flowchart author.

**Sandboxing:** Out of scope for the SDK. The consumer can sandbox the subprocess if they want (containers, seccomp, etc.). The SDK just spawns a process and talks to it.

**Agent permissions:** The program doesn't get direct agent access. It goes through the runtime, which goes through the consumer's agent session. The consumer controls what the agent can do (permission mode, allowed tools, etc.). The program can't bypass this.

**No new attack surface beyond what bash blocks already provide.** This is important — program flowcharts don't open a new security hole. They're just a more ergonomic way to do what was already possible.

---

## 4. Error Semantics

**Agent query fails.** The runtime returns a JSON-RPC error to the program. The program decides what to do — retry, skip, abort. This is a major advantage over YAML flowcharts, which have no error handling.

**Program sends malformed JSON-RPC.** Runtime returns a parse error. If the program keeps sending garbage, the runtime kills it.

**Protocol mismatch.** Caught at handshake. Runtime refuses to proceed if versions are incompatible.

**Key insight: error handling is why program flowcharts are valuable.** YAML flowcharts can't try/catch. They can't retry a failed prompt with different wording. They can't fall back to an alternative approach. Programs can. This is the single strongest argument for the feature.

---

## 5. Streaming & Long-Running Queries

An agent `query` can take minutes. During execution, the agent performs many actions. Three concerns:

**5a. Can the program cancel a running query?**

Yes — the program sends a `cancel` notification (JSON-RPC notification, no response expected). The runtime cancels the in-flight agent query. The pending `query` call returns an error. The program decides what to do next.

But: the program is blocked waiting for the `query` response. To send a `cancel`, it needs a separate thread/async task. This is fine for Python (threading) but awkward for shell scripts. Accept this limitation — shell scripts get simple blocking calls, Python/Node get cancellation if they want it.

**5b. Does the program see intermediate events?**

Via JSON-RPC notifications during the pending query (see §1). The program's client library can expose these via a callback:

```python
def on_event(event):
    if event["type"] == "tool_use":
        print(f"Agent is using: {event['tool']}")

result = runtime.query("fix the bug", on_event=on_event)
```

Or ignore them entirely:
```python
result = runtime.query("fix the bug")  # blocks, ignores events
```

**5c. Does the consumer see intermediate events?**

Yes — the runtime forwards events from the agent session to the consumer's event channel as usual. The program is not in this path. The consumer always gets events, regardless of whether the program cares about them.

---

## 6. Events & Observability (the "blocks" problem)

YAML flowcharts have blocks. Each block has a name, type, start/end events. The consumer can show "Executing block: Write Draft" in a UI. Progress is structured.

Program flowcharts don't have blocks. They're arbitrary code. How does the consumer know what's happening?

**Options:**

- **(a) The program emits custom events.** `runtime.emit("status", "Writing draft...")`. Totally freeform. No structure guarantees.

- **(b) The runtime infers events from calls.** Every `query()` call becomes a "block" event. The runtime emits `query_started` / `query_completed`. But these are just RPC calls, not semantic blocks.

- **(c) The program declares phases.** `runtime.begin_phase("draft")` / `runtime.end_phase("draft")`. Structured, but voluntary.

- **(d) Don't solve this.** Program flowcharts sacrifice structured observability for flexibility. That's the tradeoff. The consumer gets `query_started` / `query_completed` events from the runtime (because it mediates all agent calls), but no semantic block structure. Accept it.

**Recommendation: (d) with (a) available.** The runtime automatically emits events for every RPC call (query started/completed, clear, etc.). The program can optionally emit custom events for richer progress reporting. No mandatory structure — that's YAML's job.

This is an honest tradeoff. YAML flowcharts: structured, visual, limited. Program flowcharts: unstructured, flexible, powerful. The consumer's UI adapts to what it gets.

---

## 7. Composability (YAML ↔ program interop)

**Can a YAML flowchart call a program flowchart?**

Yes — via `command` blocks. The `command` block resolves a sub-command. If that sub-command is a program flowchart, the runtime spawns it, runs it, gets the result, feeds it back to the YAML walker. The YAML walker doesn't know or care that the sub-command was a program.

**Can a program flowchart call a YAML flowchart?**

Add a `run_command(name, args)` RPC method. The runtime resolves the command, runs it (YAML walker or nested program subprocess), returns the result. The program doesn't know or care that it was YAML.

**Can a program flowchart call another program flowchart?**

Same mechanism — `run_command(name, args)`. The runtime spawns a nested subprocess. Works recursively.

**This all composes cleanly because the boundary is the `Action` enum / RPC protocol, not the implementation.** YAML and program flowcharts are interchangeable at the command boundary. The consumer sees the same interface regardless.

**Variable passing:** `run_command` takes args and returns a result string. No shared mutable variable map across command boundaries — that would be a mess. Each sub-command gets input args, returns a result. Clean.

---

## 8. The Bash Question

Programs can run bash natively. `subprocess.run("ls")` in Python. Should the runtime mediate bash execution?

**Arguments for mediating:**
- Logging/auditing: the consumer can see every command the program runs
- Permission control: the consumer can block dangerous commands
- Consistency: same bash handling as YAML bash blocks

**Arguments against:**
- The program already runs as a subprocess with full OS access
- Mediating bash is security theater — the program can call `os.system()` directly
- Adds protocol complexity for no real security gain
- Slower (round-trip through JSON-RPC vs direct syscall)

**Recommendation: don't mediate bash.** The program runs bash however it wants. If the consumer needs to audit/sandbox, they sandbox the entire subprocess (container, seccomp, etc.). Pretending the runtime can control bash access when the program has a full OS environment is dishonest.

The runtime provides `query` (agent access) because the program CAN'T do that itself. It doesn't provide `bash` because the program can.

---

## 9. Concurrency

**Can the program make concurrent agent queries?**

The runtime has one agent session. Concurrent queries would need multiple sessions or queuing. For now: **no.** One query at a time. The program blocks on each `query()` call. If the program sends a second `query` while one is pending, the runtime returns an error.

This matches YAML flowchart semantics (sequential block execution) and avoids complexity. If concurrent queries are needed later, add a `spawn_session` RPC that creates an additional session, returning a session ID that subsequent `query` calls can target.

**Can the program use threads internally?**

Yes — for its own computation. But agent calls are serialized. The program can parse, transform, compute in parallel — it just can't talk to the agent in parallel.

---

## 10. Discovery & Configuration (command.yaml changes)

Currently: each command is a directory with `command.yaml` containing the flowchart definition.

For program flowcharts, `command.yaml` becomes metadata-only:

```yaml
name: story-writer
description: Writes stories with revision
type: program              # new field, default "flowchart"
entrypoint: main.py        # path to executable, relative to command dir
arguments:
  - name: topic
    description: The topic
    required: true
session:
  model: claude-sonnet
  permission_mode: auto
```

The graph definition moves from YAML into the program. Arguments and session config stay in YAML — they're metadata the consumer needs before spawning the program.

**Why keep arguments in YAML?** The consumer needs to validate arguments before launching the program. It also needs argument metadata for help text, tab completion, etc. The program shouldn't need to be spawned just to learn what arguments it takes.

**Why keep session config in YAML?** The consumer creates the agent session before the program runs. The program doesn't choose its own agent — the consumer provides one based on the session config.

---

## 11. Language Support & Client Libraries

The protocol is JSON-RPC over stdio. Any language that can read/write JSON lines to stdin/stdout works.

**Do we need client libraries?**

For Python: yes, a thin one. `pip install flowcoder` gives you `from flowcoder import runtime`. It handles JSON-RPC framing, blocking calls, optional event callbacks. ~100 lines of code.

For shell scripts: provide a `flowcoder` CLI helper that wraps individual RPC calls. `result=$(flowcoder query "fix the bug")`. Each invocation connects to the parent's runtime via inherited file descriptors or a Unix socket. More complex to implement but makes shell scripts viable.

For other languages: just publish the protocol spec. A JSON-RPC client in any language can talk to the runtime. No library needed.

**Start with Python only.** It's the obvious first target. Shell and others come later if there's demand.

---

## 12. Testing

**How do you test a program flowchart without a real agent?**

Provide a mock runtime. The Python client library includes a test mode:

```python
from flowcoder.testing import MockRuntime

mock = MockRuntime()
mock.on_query("Write a story*", returns="Once upon a time...")

# Run the program against the mock
result = mock.run("./my_flowchart.py", args={"topic": "dragons"})
assert "Once upon a time" in result
```

The mock speaks the same JSON-RPC protocol. The program doesn't know it's being tested.

**Can you test without the runtime at all?**

If the program is well-structured (logic separated from runtime calls), the logic is testable with standard unit tests. The runtime calls are the IO boundary — mock them like any other IO.

---

## 13. The Fundamental Question

**Is this just "agent session as an RPC service"?**

Kind of. Strip away the flowcoder framing and you have: a subprocess that can talk to a coding agent via JSON-RPC. That's useful on its own, independent of flowcharts.

**What does the flowcoder framing add?**

1. **Discovery.** Commands are discoverable from search paths. `/story dragons` resolves to a program flowchart just like a YAML one. The user doesn't care about the implementation.

2. **Composability.** Program flowcharts compose with YAML flowcharts via `run_command`. A YAML flowchart can delegate complex logic to a program. A program can call simple YAML sub-flows.

3. **Session management.** The consumer creates and owns the agent session. The program doesn't deal with agent lifecycle, credentials, connection management. It just calls `query()`.

4. **Consistent interface for the consumer.** The consumer's event handling, UI, logging, etc. work the same regardless of whether the flowchart is YAML or a program. No special-casing.

Without the flowcoder framing, the user would be writing a Python script that spawns Claude Code as a subprocess and parses its output. That's fragile, undiscoverable, and doesn't compose. The SDK wrapping makes it a first-class citizen.

---

## Summary of Decisions

| Question | Decision |
|---|---|
| Protocol | JSON-RPC over stdio, notifications for events during queries |
| Handshake | Version + capabilities exchange at startup |
| Result delivery | Explicit `finish(result)` RPC call |
| Event structure | Auto-events for RPC calls + optional custom events. No mandatory block structure |
| Bash mediation | No. Programs run bash directly |
| Concurrency | Serial queries only (one at a time) |
| Error handling | JSON-RPC errors, program decides how to handle |
| Cancellation | `cancel` notification, requires threading in program |
| Discovery | `command.yaml` with `type: program`, `entrypoint: main.py` |
| Arguments/session | Stay in `command.yaml` metadata |
| Composability | `run_command` RPC, bidirectional YAML ↔ program |
| Client libraries | Python first, protocol spec for others |
| Security | Same trust model as bash blocks. Sandbox externally if needed |

## Impact on Current Design

None required. Build the YAML walker with `Action` as the public interface. Program flowcharts produce the same `Action` variants when viewed from the consumer side. The consumer dispatches identically. Program flowcharts are a second "frontend" to the same execution model.

The only thing to keep clean now: **don't couple the consumer's dispatch logic to `GraphWalker` specifically.** Keep it generic over "something that produces Actions."
