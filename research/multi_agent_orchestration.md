# Multi-Agent Orchestration — Design Analysis

## Question

Can program flowcharts (and the Action enum / state machine walker design) support multi-agent orchestration patterns like concurrent agents, voting, supervisor/worker, dynamic teams?

## Patterns Analyzed

### 1. Parallel Dispatch (fan-out/fan-in)

Ask N agents the same question concurrently, collect all results.

```python
import asyncio
from flowcoder import runtime

args = runtime.start()

tasks = [
    runtime.query_async("Analyze this code for bugs", session="agent_a"),
    runtime.query_async("Analyze this code for bugs", session="agent_b"),
    runtime.query_async("Analyze this code for bugs", session="agent_c"),
]
results = await asyncio.gather(*tasks)

summary = runtime.query(f"Synthesize these analyses:\n{results}", session="supervisor")
runtime.finish(summary)
```

**Requires one protocol change:** constraint must be "one query per session at a time" instead of "one query at a time." Each session is an independent agent — no reason to serialize across sessions. The runtime already knows which session each query targets.

### 2. Voting / Consensus

N agents answer, majority wins.

```python
results = []
for session in ["a", "b", "c"]:
    r = runtime.query("Is this code safe to deploy? Answer YES or NO.", session=session)
    results.append(r.strip().upper())

yes_count = sum(1 for r in results if "YES" in r)
decision = "APPROVED" if yes_count > len(results) / 2 else "REJECTED"
runtime.finish(decision)
```

**Works today.** Sequential queries, pure Python logic for voting. Could be parallelized with the concurrent-per-session change but doesn't require it.

### 3. Supervisor/Worker with Retry

Supervisor reviews work, sends it back if not good enough.

```python
args = runtime.start()
task = args["task"]

for attempt in range(3):
    code = runtime.query(f"Implement: {task}", session="coder")
    review = runtime.query(f"Review this code. Production ready?\n{code}", session="reviewer")

    if "approved" in review.lower():
        runtime.finish(code)
        break

    task = f"{task}\n\nPrevious attempt rejected. Feedback:\n{review}"
else:
    runtime.finish(f"Failed after 3 attempts. Last review:\n{review}")
```

**Works today.** Loop + branch + multi-session.

### 4. Pipeline (Assembly Line)

Each agent does one stage, passes to the next.

```python
spec = runtime.query("Write a technical spec for: " + args["feature"], session="architect")
code = runtime.query(f"Implement this spec:\n{spec}", session="coder")
tests = runtime.query(f"Write tests for this code:\n{code}", session="tester")
docs = runtime.query(f"Write docs for this code:\n{code}", session="writer")
runtime.finish(f"## Code\n{code}\n## Tests\n{tests}\n## Docs\n{docs}")
```

**Works today.** Sequential queries to different sessions.

### 5. Dynamic Team Sizing

Spawn agents based on the problem.

```python
plan = runtime.query("Break this into subtasks: " + args["task"], session="planner")
subtasks = json.loads(plan)

for i, subtask in enumerate(subtasks):
    runtime.spawn_session(f"worker_{i}", model="claude-sonnet",
                          system_prompt=f"You specialize in: {subtask['domain']}")

results = {}
for i, subtask in enumerate(subtasks):
    results[subtask['name']] = runtime.query(subtask['prompt'], session=f"worker_{i}")

integration = runtime.query(f"Integrate these results:\n{json.dumps(results)}", session="planner")
runtime.finish(integration)
```

**Needs `spawn_session` RPC method.** Current design only has statically-declared sessions. Additive change: new RPC method, new `Action::SpawnSession` variant, consumer handles it.

### 6. Agent Debate / Iterative Dialogue

Two agents argue until they agree.

```python
pos_a = runtime.query(f"Argue FOR: {topic}", session="advocate")
pos_b = runtime.query(f"Argue AGAINST: {topic}", session="critic")

for round in range(5):
    rebuttal_a = runtime.query(f"Respond to this critique:\n{pos_b}", session="advocate")
    rebuttal_b = runtime.query(f"Respond to this argument:\n{rebuttal_a}", session="critic")

    judgment = runtime.query(
        f"Have these positions converged? Answer CONVERGED or CONTINUING.\n"
        f"Position A: {rebuttal_a}\nPosition B: {rebuttal_b}",
        session="judge"
    )
    if "CONVERGED" in judgment:
        break
    pos_a, pos_b = rebuttal_a, rebuttal_b

runtime.finish(f"A: {rebuttal_a}\nB: {rebuttal_b}\nJudgment: {judgment}")
```

**Works today.** Loop + multi-session + convergence check.

### 7. Reactive / Event-Driven Coordination (THE HARD ONE)

Agent A is coding. While it's working, Agent B monitors its file changes and provides real-time feedback. Agent C watches for security issues.

```python
# This does NOT work with the current design.
# The program blocks on each query() call.
# You can't "monitor" an in-flight query from another agent.
```

**Genuine gap.** The request/response model can't do reactive, event-driven multi-agent coordination. Would need:

- Non-blocking queries that return a handle
- Event streaming from in-flight queries to the program
- The program acting on events while queries are still running

This is a fundamentally different execution model — from "call and wait" to "subscribe and react." Possible over JSON-RPC (notifications mechanism supports it), but the client library and programming model change significantly.

**Verdict: out of scope.** Flowcoder executes structured workflows with a start and an end. Reactive coordination is an event loop with no defined end. Trying to make flowcoder handle both muddies the abstraction.

---

## Gap Summary

| Gap | Severity | Fix |
|---|---|---|
| Concurrent queries across sessions | Medium | Change "one query at a time" to "one per session." Protocol change only |
| Dynamic session creation | Medium | Add `spawn_session` RPC method + `Action::SpawnSession` variant |
| Reactive/event-driven agents | Low (different problem) | Out of scope. Fundamentally different programming model |
| Structured output from queries | Low | Already in the model (`output_schema`). Consumer passes to agent |
| Inter-flowchart communication beyond return values | Low | Shared variable store or message passing. Only for very complex orchestration |

## Conclusion

Program flowcharts handle patterns 1-6 well — either today or with small additive changes (concurrent-per-session, `spawn_session`). These cover the vast majority of multi-agent use cases.

Pattern 7 (reactive agents) is a genuine limitation but also a different problem. Flowcharts are workflows. Reactive coordination is an event loop. Keep them separate.

The `Action` enum remains the right boundary. Multi-agent adds new variants (`SpawnSession`, maybe `ParallelQuery`) but doesn't change the fundamental model: the walker/program produces actions, the consumer dispatches them.
