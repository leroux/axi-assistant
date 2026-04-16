---
name: decision-council
description: Use when the user wants Codex to collaborate with Claude on decision-making, candidate decision-point discovery, architecture/design tradeoffs, human-in-the-loop option generation, blind-first independent model framing, model uniqueness before synthesis, or "both AI reasonings" before presenting choices. Trigger when the user asks to use Claude, Opus, Claude Opus 1M, a second AI opinion, a debate/council process, or a reusable Codex+Claude decision workflow.
---

# Claude Decision Council

## Purpose

Use this skill to run a real two-model decision loop. Codex is the facilitator and final synthesizer; Claude is a co-designer that may critique the framing, add options not in Codex's initial list, recommend differently, and participate in a short back-and-forth debate when it would materially improve the decision.

When this skill is used with `decision-mode`, treat it as an enhancement layer. `decision-mode` owns intensity, gating, user questions, and decision logs; this skill owns Claude-assisted critique, option generation, and candidate decision-point discovery.

## Claude Invocation

Use Claude Opus 1M explicitly:

```sh
claude -p --model 'opus[1m]' --resume <session-id> '<prompt>'
```

Use `--resume <session-id>` only to continue a previous Claude session UUID. To mint a new named Claude session, generate a fresh UUID, for example with `uuidgen`, and use:

```sh
claude -p --model 'opus[1m]' --session-id <new-random-uuid> '<prompt>'
```

If the user supplied a Claude session id, prefer `--resume <session-id>`. If no session id is available and continuity matters, generate a random UUID and use `--session-id <new-random-uuid>`. If continuity does not matter, omit both `--resume` and `--session-id`. If Claude rejects `opus[1m]` or the CLI is unavailable, stop and tell the user; do not silently fall back to regular `opus`, Sonnet, or another model.

Ask Claude to discuss only unless the user explicitly wants Claude to edit or run tools. Include a sentence like:

```text
Do not edit files or run tools; just critique, add options, and recommend.
```

If shell quoting is tricky, use a quoted heredoc:

```sh
claude -p --model 'opus[1m]' --resume <session-id> <<'PROMPT'
...
PROMPT
```

## Decision Workflow

For each material decision:

1. Decide whether to use the normal critique path or the blind-first path:
   - Use **blind-first** for strict-mode decisions, high-impact or costly-to-reverse choices, close calls where anchoring would erase model differences, or when the user asks to maximize model uniqueness.
   - Use the normal critique path for lower-stakes balanced/light decisions where latency matters more than independent framing.
2. Normal critique path: Codex drafts the decision point, assumptions, and initial options, then asks Claude to critique the decision framing, identify missing assumptions, add/remove/merge options, and recommend one option.
3. Blind-first path: Codex first drafts its own tentative framing before seeing Claude's answer, then asks Claude for independent framing using only the decision context, constraints, and accepted prior decisions. Do **not** include Codex's option list or recommendation in the first Claude prompt. After Claude returns, compare the two framings before merging.
4. Codex evaluates Claude's response independently. Do not rubber-stamp it.
5. Merge divergence-first: preserve options, assumptions, or risk frames that came from only one model when they represent real tradeoffs. Tag origins internally as `Claude`, `Codex`, or `both` while merging so the independent signal is not lost.
6. If Claude adds a strong option, changes the framing, or disagrees in a way that could affect the user's choice, Codex should do one short rebuttal/clarification round with Claude before presenting options. Ask Claude to respond to Codex's objections and to say whether its recommendation changed.
7. Stop the debate after the option set and tradeoffs are clear, or after two Claude calls for the same decision unless the user explicitly asks for more debate. Do not turn the process into an open-ended model conversation.
8. Codex presents the revised options to the user. Include both recommendations explicitly for every option, except when strict `decision-mode` mode suppresses or defers visible recommendations:
   - `Claude recommendation: Yes/No/Mixed - <reason>`
   - `Codex recommendation: Yes/No/Mixed - <reason>`
9. Outside strict mode, explicitly state whether Claude and Codex agree:
   - `Both Claude and Codex recommend Option B.`
   - or `Claude and Codex disagree: Claude recommends B; Codex recommends C.`
10. Ask the user to choose. Do not choose for them unless they explicitly delegated the decision.
11. After the user chooses, record the decision if the task has a decision log or other durable artifact.
12. If the user asked to auto-continue, immediately proceed to the next material decision using the same loop.

## Strict Mode Conflict

When this skill is active with `decision-mode --strict`, strict mode controls the user-facing recommendation display:

- Present the options neutrally first, with assumptions, reversibility, failure modes, and steelman/strawman analysis as required by `decision-mode`.
- Do not show a unified recommendation or mark an option as recommended.
- By default, do not show Claude/Codex model leanings before the user's choice. If the user explicitly asks for recommendations or has opted into seeing them for the session, show them after the neutral options in a separate `Model Perspectives` section with attributed leanings only:
  - `Claude leans toward: <option> - <reason>`
  - `Codex leans toward: <option> - <reason>`
- Never collapse strict+council output into "both models recommend X" before the user has either asked for recommendations or chosen to make model leanings visible.

## Decision-Point Discovery

When the user asks Claude to help find decision points, or when this skill is active alongside `decision-mode`, use Claude to draft a candidate backlog before the first user-facing fork and refresh it periodically. Good refresh triggers:

- after the initial context read or plan draft
- after each completed todo-group or major milestone
- after a meaningful plan change
- when a new fact invalidates or expands the decision surface

Ask Claude for candidates, not commands. Codex must filter, merge, and order the candidates before asking the user. Do not present a long raw list unless the user explicitly asks for the full backlog. When `decision-mode` is active, filter through its intensity level: strict keeps more branch points, balanced keeps material forks, light keeps only high-impact or costly-to-reverse choices.

## Claude Prompt Shape

Use prompts that invite a real debate, not just validation:

```text
We are deciding <decision>. Current assumptions: <assumptions>.

Codex's initial options:
A. ...
B. ...
C. ...

Please critique the framing, point out missing assumptions, add any option that should exist even if it is not in this list, merge/remove weak options if needed, and recommend one. Keep it concise and decision-oriented. Do not edit files or run tools; just discuss.
```

When continuing an existing process, include the accepted prior decisions so Claude can critique the new fork against them.

### Decision-Point Discovery Prompt

Use this before presenting the first fork and during refresh triggers:

```text
We are working on <task>. Current context: <brief context>. Accepted prior decisions: <prior decisions or none>. Current plan/backlog: <plan>.

Please identify candidate decision points we may need to ask the user about soon. For each, include: title, why it matters, likely options if obvious, impact/reversibility, and when it becomes actionable. Add missing candidates and merge/remove weak ones. Keep it concise and decision-oriented. Do not edit files or run tools; just discuss.
```

### Blind-First Decision Prompt

Use this before Codex sends Claude any option list for strict, high-impact, close-call, or model-uniqueness-sensitive decisions:

```text
We are deciding <decision>. Current context: <brief context>. Constraints and assumptions: <constraints>. Accepted prior decisions: <prior decisions or none>.

Please independently frame this decision without seeing Codex's option list. Identify the key decision, missing assumptions, 2-4 viable options, strongest tradeoffs, your recommendation, and what evidence would change your mind. Keep it concise and decision-oriented. Do not edit files or run tools; just discuss.
```

After receiving Claude's blind response, Codex should draft or reveal its own independent option set, then merge divergence-first:

- Preserve options unique to Claude or Codex when they represent real tradeoffs.
- Name important framing disagreements before naming easy agreements.
- Avoid prematurely synthesizing a compromise option unless it clearly dominates the originals.
- In non-strict mode, show origin labels when useful: `[Claude]`, `[Codex]`, or `[both]`.

### Debate Follow-Up Prompt

Use a follow-up when the first Claude response introduced a meaningful new option, challenged the framing, or disagreed with Codex:

```text
Codex response to your critique:
- I agree with <points>.
- I disagree or am uncertain about <points> because <reason>.
- I am considering presenting options <revised options>.

Please respond to these objections, say whether any option should be added/removed/merged, and state whether your recommendation changes. Keep it concise and decision-oriented. Do not edit files or run tools; just discuss.
```

## Guardrails

- Treat Claude as a collaborator, not an authority. Codex must still reason independently.
- Prefer fewer, sharper options. Merge options that differ only cosmetically.
- Preserve a strong rejected option when it represents a real tradeoff; do not collapse the decision into a single obvious path.
- Let Claude introduce a new option. If it is better than Codex's original options, present it.
- If Claude invalidates the decision framing, revise the fork before presenting it to the user.
- Use a back-and-forth debate only when it changes the decision quality. Skip it when Claude simply agrees or the critique is minor.
- If the user's newest message changes the process, follow the newest message.
- If an interrupted Claude process may still be running, verify or stop it before starting a new prompt when practical.
- Do not make Claude edit files or run commands through its CLI unless the user explicitly requests that behavior.

