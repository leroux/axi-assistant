# Testing Approaches for Axi Assistant

## Context

Axi is a Discord bot built on Claude Agent SDK, maintained as two forks:
- **Axi Prime** (main branch) — production instance
- **Axi Nova** (nova branch, git worktree) — test instance / second fork

A collaborator (Axioms) maintains their own fork. Both forks share git history and merge code (whole-branch merges, not cherry-picks). This means any testing framework and refactoring needs to be coordinated — large refactors of bot.py create merge conflicts that affect both parties.

### Primary motivation

The test suite's primary purpose is **not** bug-catching — it's **defining a shared behavioral contract** between forks. If both forks pass the same tests, they can be merged. The tests are the specification.

### Design constraints

- **No large refactors of bot.py** unless both forks adopt them simultaneously. Moving functions into new modules changes every import and creates guaranteed merge conflicts.
- **Tests should verify behavior, not implementation.** A test that asserts "channel.send was called with exactly this string" breaks when either fork changes formatting. A test that asserts "the bot responded in the correct channel" survives.
- **Fork-specific features must be isolatable** — via feature flags, separate modules, or conditional logic — so the shared spec doesn't break when one fork adds something the other doesn't have.

---

## Approach Catalog

### 1. Pure Unit Tests

Test isolated, pure functions by importing them directly. Handle bot.py's import-time side effects (reads `os.environ["DISCORD_TOKEN"]` etc. at module level) with env var stubs in conftest.

**Covers:**
- `split_message`, `_normalize_channel_name`, `_format_channel_topic`, `_parse_channel_topic`
- `datetime_to_snowflake`, `format_message`, `resolve_snowflake` (discord_query.py)
- supervisor.py file helpers (`ensure_default_files`, `tail_log`, `write_crash_marker`)
- Schedule file CRUD (monkeypatch the path globals)

**Pros:** Trivial to write, fast, stable, zero production code changes
**Cons:** Only covers the boring parts. The interesting behavior — message routing, agent lifecycle, scheduling decisions — is untouched.

**Effort:** Low | **Value:** Low-medium

---

### 2. Mock-Heavy Unit Tests

Mock `discord.py` objects (Guild, Channel, Message) and `claude_agent_sdk` (ClaudeSDKClient), then call bot.py functions directly with mocked arguments.

**Covers:**
- MCP tool validation (spawn/kill argument checking, cwd restrictions, max agents)
- `on_message` routing logic (agent lookup, busy detection, inject vs queue)
- Channel management (ensure_agent_channel, move_channel_to_killed)

**Pros:** Can test any function in isolation
**Cons:** Extremely brittle. Tests become "verify we called the right mock methods" rather than testing behavior. discord.py's object hierarchy is deep and painful to mock. High maintenance cost when internals change. Actively harmful as a shared contract — forces both forks to have identical internal structure.

**Effort:** Medium | **Value:** Low (high false-positive rate, brittle)

---

### 3. Simulation / "Bot in a Box"

Build a fake environment that the real bot.py runs inside. Two fake layers:

**Fake Discord layer** — implements the subset of discord.py's interface that bot.py actually uses:
- `FakeGuild` with categories and channels
- `FakeTextChannel` that captures `.send()` calls into a list
- `FakeMessage` with author, content, channel
- Can fire `on_message` / `on_ready` events into the bot's event loop

**Fake Claude SDK layer** — implements ClaudeSDKClient's interface:
- Returns canned `StreamEvent` / `AssistantMessage` / `ResultMessage` sequences
- Can simulate timeouts, errors, slow responses
- Tracks what prompts were sent

Bot.py code runs unmodified — it just talks to fakes instead of real services.

**Covers:** Everything — full message flows, agent lifecycle, scheduling, error recovery, concurrent message handling.

**Pros:** Tests real behavior. Bot.py doesn't change. Tests survive internal refactors. Can test complex scenarios (message while busy, schedule fires during shutdown, etc.).
**Cons:** Significant upfront investment. The fakes must be accurate enough that bot.py can't tell the difference.

**Note on scope:** Bot.py's actual usage of discord.py is narrow:
- `channel.send(text)`, `channel.typing()`, `channel.edit(topic=...)`
- `channel.move(category=..., ...)`
- `guild.create_text_channel(name, category=...)`
- `guild.categories`, `category.text_channels`
- `bot.get_channel(id)`, `bot.get_guild(id)`
- `message.author.id`, `message.content`, `message.channel`, `message.guild.id`

A fake Discord layer covering this surface is probably ~100-150 lines, not the monster it seems.

**Effort:** High upfront, low ongoing | **Value:** Very high

---

### 4. Protocol / Interface Tests

Formalize the bot's behavior as a protocol:

```
Given: [preconditions — agents running, schedules loaded, etc.]
When:  [event — user message, cron tick, slash command]
Then:  [observable effects — messages sent, agents spawned, files written]
```

Tests only assert on observable outputs, never on internal state. This is the "black box" approach — essentially Approach 3 but with a more disciplined assertion style.

**Pros:** Most robust to refactoring. Could survive a complete rewrite of bot.py. Tests serve as a behavioral specification that both forks agree on. **This is the best fit for the shared-contract use case.**
**Cons:** Same upfront cost as Simulation. Harder to diagnose failures since you can't peek at internals.

**Effort:** High upfront | **Value:** Very high, especially as shared contract

---

### 5. E2E Against Real Discord (with Nova)

Nova is already a running test instance. Write tests that send real Discord messages and assert on responses.

**Covers:** The actual user experience end-to-end.

**Pros:** Highest fidelity. Tests exactly what users see. No fakes to maintain.
**Cons:** Slow (network + Claude API latency). Expensive (real API calls per test run). Flaky (network, rate limits, API availability). Can't test crash recovery or timing-sensitive behavior. Hard to set up deterministic preconditions.

**Effort:** Low to write, high to maintain | **Value:** High for smoke tests, poor for regression suite

---

### 6. Subprocess / Harness Tests

Launch `supervisor.py` as a subprocess with a controlled environment, observe behavior from the outside. Don't import anything.

**Covers:** Supervisor crash recovery logic, exit code handling, rollback behavior.

**Pros:** Tests the actual process management. No import issues.
**Cons:** Slow (process startup). Only practical for supervisor.py.

**Effort:** Medium | **Value:** Medium (supervisor is 230 lines but its logic is critical)

---

### 7. Property-Based Testing (Hypothesis)

Generate random inputs, verify invariants:
- `split_message(text, limit)` — every chunk <= limit, join reconstructs original
- `_normalize_channel_name(name)` — always lowercase, <=100 chars, only valid chars
- `datetime_to_snowflake` — monotonically increasing with time

**Pros:** Finds edge cases humans miss
**Cons:** Only works for pure functions with clear invariants

**Effort:** Low | **Value:** Medium (great complement to example-based tests)

---

### 8. LLM-Driven Testing

The bot's own infrastructure (agent spawning, Discord interaction, Nova) enables AI-driven testing.

**8a: LLM as test oracle** — An agent reads bot.py, generates test scenarios, judges whether behavior is correct. Useful for fuzzy/behavioral stuff hard to write assertions for.

**8b: LLM as chaos monkey** — An agent continuously pokes Nova: sends messages, spawns/kills agents, edits schedules, sends messages to busy agents. Monitors for crashes, hangs, nonsensical responses. Adversarial exploration of the state space.

**8c: LLM as scenario player** — Describe scenarios in natural language ("send a message while the agent is busy, then kill it, then resume"), an agent executes them against Nova and reports results. Scenarios are the tests.

**8d: Self-testing loop** — Before Axi restarts with new code, it spawns an agent to verify Nova still works. CI using the bot's own infrastructure.

**Pros:** Explores behavioral edge cases that are tedious to encode as deterministic tests. Tests "soft" aspects (communication style, ambiguous input handling). 8d makes testing part of the self-modification workflow.
**Cons:** Non-deterministic. Expensive (API calls per run). Slow. "Test passed" might mean "the LLM didn't notice the bug." Not a replacement for deterministic tests.

**Effort:** Low to prototype (infrastructure exists), medium to make reliable | **Value:** High for exploratory/chaos testing, low for regression

---

## Comparison Matrix

| Approach | Deterministic | Coverage | Upfront Cost | Ongoing Cost | Shared Contract Fit |
|---|---|---|---|---|---|
| 1. Pure unit tests | Fully | Low | Low | Low | Poor (tests internals) |
| 2. Mock-heavy units | Fully | Medium | Medium | High (brittle) | Poor (forces same structure) |
| 3. Simulation | Fully | High | High | Low | Good |
| 4. Protocol/interface | Fully | High | High | Low | **Best** |
| 5. E2E real Discord | Mostly | High | Low | High (flaky) | OK (but expensive) |
| 6. Subprocess harness | Fully | Supervisor only | Medium | Low | Good (for supervisor) |
| 7. Property-based | Fully | Pure fns only | Low | Low | OK |
| 8. LLM-driven | None | Exploratory | Low | High (API cost) | Novel/complementary |

---

## Interface Boundaries (Draft)

These are the natural seams where both forks must remain compatible for merges to work.

### Discord boundary

Events in: `on_message`, `on_ready`, slash commands (`/list-agents`, `/kill-agent`, `/stop`, `/reset-context`, `/restart`)
Effects out: `channel.send()`, `channel.edit()`, `channel.move()`, `guild.create_text_channel()`

Both forks talk to Discord the same way. A shared test harness can inject events and observe effects at this boundary.

### Claude SDK boundary

Bot sends: `client.query()`, `client.interrupt()`
Bot receives: streams of `StreamEvent`, `AssistantMessage`, `ResultMessage`

Both forks use the same SDK. The fake Claude layer intercepts at this boundary.

### File contracts

These files have implicit schemas. Both forks must agree on the schema:
- `schedules.json` — array of schedule entries (name, prompt, schedule|at, optional: agent, cwd, session, reset_context)
- `schedule_history.json` — array of {name, prompt, fired_at}
- `schedule_skips.json` — array of {name, skip_date}

If both forks agree on the schema, a schedule created by one works on the other.

### Agent lifecycle states

The state machine and transitions both forks must implement:
```
(not exists) --spawn--> awake --idle--> sleeping
                 |                         |
                 +--busy--+                +--message--> awake
                 |        |                |
                 v        v                +--kill--> (killed)
              awake    awake
                 |
                 +--kill--> (killed)
```

States: not-exists, awake (idle), awake (busy/locked), sleeping (client=None), killed (removed from registry, channel in Killed category)

Transitions: spawn, message received, query completes, idle timeout, kill command, wake on message

### Fork-specific territory (out of shared spec)

- System prompt content and personality
- Crash handler prompt wording
- MCP tools beyond the core set (spawn, kill, restart, get_date_and_time)
- Message formatting details (exact wording of system notifications)
- Extra env vars or config specific to one fork
- Any features gated behind fork-specific env vars

---

## Recommended Phasing

### Phase 0 — Cheap wins (no coordination needed)

- Add pytest to pyproject.toml
- Pure unit tests (Approach 1) for shared pure functions
- Property-based tests (Approach 7) for split_message, normalize_channel_name, snowflake conversion
- Subprocess tests (Approach 6) for supervisor crash classification
- **~40 tests, zero production code changes, both forks can adopt independently**

### Phase 1 — Agree on the contract

- Document the shared interfaces (above, but more rigorous)
- Agree on file schemas
- Agree on agent lifecycle state machine
- Define behavioral spec in Given/When/Then format for core scenarios
- **This is a conversation with Axioms, not a code deliverable**

### Phase 2 — Build the simulation layer

- Fake Discord layer (~100-150 lines)
- Fake Claude SDK layer
- Test harness that wires fakes to bot.py
- **Both forks adopt the test harness. This is the coordination bottleneck.**

### Phase 3 — Behavioral tests against the harness

- Protocol/interface tests (Approach 4) for core scenarios:
  - User sends message → correct agent responds in correct channel
  - Cron schedule fires → agent spawned (or prompt routed to existing session)
  - Agent killed → channel moved to Killed, session cleaned up
  - Message to busy agent → injected or queued
  - Graceful shutdown → waits for busy agents
- **These tests ARE the shared spec. Both forks must pass them.**

### Phase 4 — LLM-driven exploration (ongoing)

- Chaos monkey agent poking Nova (Approach 8b)
- Pre-restart self-testing (Approach 8d)
- **Independent per-fork, no coordination needed**

---

## Open Questions

- Is Axioms on board with a shared behavioral spec? This only works if both parties agree to it.
- How do we handle the import-time side effects in bot.py for testing? Options: env var stubs in conftest, or a small shim that patches before import.
- Should the fake Discord layer live in a shared `tests/` directory that both forks maintain, or as a separate package?
- How strict should the behavioral spec be? "Bot sends a message in the channel" vs "Bot sends a message containing the agent name in the channel" vs "Bot sends exactly this string."
- Should fork-specific features be feature-flagged in shared code, or live in separate files that only exist in one fork?
