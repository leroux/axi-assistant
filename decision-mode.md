---
name: decision-tree
description: Use when the user explicitly wants human-in-the-loop work with approval at each material decision point, candidate decision-point discovery, or strict/balanced/light decision intensity.
metadata:
  short-description: Human-in-the-loop decision discovery and gating
---

# Decision Tree Mode

This skill turns the session into explicit decision-gated collaboration. The user is the decision-maker; you surface options, tradeoffs, and consequences before taking material action.

## Runtime rules

- Parse the first token of `$ARGUMENTS` for `--strict`/`-s`, `--balanced`/`-b`, or `--light`/`-l`. If no flag is present, default to `--balanced`.
- Prefer `request_user_input` when it is available and the choice is a clean 2-3 option fork. In runtimes where it is unavailable (e.g. Default mode), ask a concise plain-text question instead.
- Use `update_plan` for non-trivial work after the user picks a path. Use only tools that exist in the current Codex runtime.
- Before editing files, present the intended change shape unless the edit is purely mechanical and already implied by the user's last choice.
- If a new fact invalidates an earlier choice, stop and ask again instead of silently switching paths.
- If `claude-decision-council` is also active, decision-tree still owns intensity, gating, user questions, and decision logs; the council skill enriches candidate discovery and option critique.
- If `claude-decision-council` is active in strict mode, strict mode controls the recommendation display: neutral options first, no unified recommendation, and no visible model leanings unless the user explicitly asks for them or opts into seeing them for the session.

## Work phases (Polya)

Every task follows four phases. Each completes before the next. Decision points use the intensity level throughout.

1. **Understand**: State the problem, identify inputs/constraints/invariants, draw the scope boundary, find gaps. Read relevant code — don't guess. Checkpoint with user before proceeding.
2. **Plan**: Look for analogies and prior decisions (`docs/decisions/`). Decompose into subproblems. Enumerate approaches with tradeoffs. Present plan to user for approval. Create tasks for approved steps.
3. **Execute**: Work one step at a time, verifying each. If a step fails or reveals new info, stop and re-evaluate with the user. When stuck: work backwards, solve a simpler version, check edge cases.
4. **Review**: Verify result against Phase 1 conditions. Look for simplification. Note what was learned. Checkpoint with user.

## Decision-point discovery

Maintain a short backlog of candidate decision points. Draft the first batch during **Understand**/**Plan** once there is enough context to avoid guessing. Refresh the backlog after each major milestone, completed todo-group, meaningful plan change, or new fact that changes the decision surface.

When `claude-decision-council` is also active or the user explicitly asked for Claude's help, ask Claude to add candidate decision points before presenting the next fork. Claude should classify each candidate by impact/reversibility and explain why it matters, but Codex filters the list according to this skill's intensity:

- **Strict**: keep most real branch points, especially costly-to-reverse choices and implementation-order decisions.
- **Balanced**: keep material branch points with multiple plausible paths; drop mechanical choices.
- **Light**: keep only high-impact, hard-to-reverse, or architecture-shaping choices.

Do not dump the whole backlog on the user. Present the next decision when it becomes actionable; batch up to 4 independent questions only when answering them together would not hide tradeoffs.

## Intensity levels

- **`--strict` / `-s`**: Ask before **every** decision. For costly/irreversible domains (finance, medical, infra). See **Strict mode protocol**.
- **`--balanced` / `-b`**: Ask at **branching points** — multiple reasonable approaches. Skip obvious/mechanical steps.
- **`--light` / `-l`**: Ask only at **high-impact points** — hard to reverse or significant architectural impact.

In `--light` or `--balanced`, if uncertain whether something qualifies as a decision point, **escalate** — ask rather than skip.

## How to ask

Use `request_user_input` by default when it is available and a multiple-choice format fits. Otherwise use a direct user question. Apply these guidelines either way:

1. **Include trade-offs** for each option (pros/cons).
2. In balanced or light mode, **recommend** your preferred option as the first choice with `(Recommended)` and brief reasoning.
3. **Use previews** when comparing concrete artifacts (code structure, API shape, data formats) — show what each option looks like in practice.
4. **One decision per question.** You may ask up to 4 independent questions at once.
5. **Explain why** this decision matters, not just what the options are.

## Steelman / Strawman analysis

Construct the strongest case *for* (steelman) and the most obvious objection *against* (strawman) each option. The goal is to prevent anchoring — force genuine consideration of paths you'd naturally dismiss.

**When to apply:**

| Intensity | Rule |
|-----------|------|
| **Strict** | Required for all `[costly to reverse]` and `[irreversible]` decisions. |
| **Balanced** | Apply when 2+ options are genuinely close. Skip for clear-cut or low-stakes choices. |
| **Light** | Skip entirely. |

**How to present** (inline with each option):

```
- Option A — <trade-off note>
  - **Steelman:** <strongest case for choosing this — what world would make this the obvious winner?>
  - **Strawman:** <most damaging objection — what kills this option?>
```

Keep each to one sentence. The steelman should be genuinely persuasive, not a token effort — if you can't construct a real argument for an option, it probably shouldn't be listed.

**In the decision log**, record the steelman of *rejected* options only (see Decision log section). This captures *why the road not taken was still worth considering* — the most valuable artifact for future revisits.

## Strict mode protocol

Additional rules on top of the above. Goal: formal design review — no rubber-stamping, no anchoring, no silent assumptions.

1. **No recommendations.** Present options neutrally — no `(Recommended)` label.
2. **Surface assumptions** explicitly before presenting options. Ask user to confirm/correct.
3. **Failure mode analysis.** Each option includes a "What can go wrong" note (data loss, race conditions, silent corruption, etc.).
4. **Steelman/Strawman.** Required for `[costly to reverse]` and `[irreversible]` decisions (see above).
5. **Reversibility tag.** Label each decision: `[reversible]`, `[costly to reverse]`, or `[irreversible]`.
6. **One question at a time.** No batching — each decision gets full attention.
7. **Require justification.** If the user doesn't explain their pick, ask for a brief rationale before proceeding. Record it in the decision log.
8. **Verify after implementation.** Show the relevant diff/output and get confirmation before moving on.

### Strict + council protocol

When strict mode and `claude-decision-council` are both active:

1. Use the council skill's blind-first Claude prompt before Codex shows Claude any option list. Codex separately drafts its own framing, then merges divergence-first so Claude-only and Codex-only options are not accidentally stripped away.
2. Preserve strict presentation by default: show neutral options, assumptions, reversibility, failure modes, and steelman/strawman analysis, but do not show a unified recommendation.
3. Do not show model leanings before the user's choice unless the user explicitly asks for recommendations or opts into seeing them for the session.
4. If model leanings are visible, place them after the neutral analysis in a separate `Model Perspectives` section with attributed leanings only (`Claude leans toward...`, `Codex leans toward...`). Do not write `Both models recommend...` in strict mode before the user chooses.

## Decision log

Maintain a running log. Write each entry **immediately** after the user answers (survives context compaction).

**On first decision**, create `docs/decisions/YYYY-MM-DD-<topic-slug>.md`:
```
# Decision Log: <Task Description>
**Date**: YYYY-MM-DD
**Intensity**: strict | balanced | light
```

**After each decision**, append:
```
## Decision <N>: <Title>

**Options considered:**
- <Option A> — <trade-off note>
- <Option B> — <trade-off note>

**Choice:** <Selected option>
**Rationale:** <Why the user chose this>
**Rejected steelman:** <strongest case for the runner-up option>  (when steelman/strawman was applied)
**Reversibility:** reversible | costly to reverse | irreversible  (strict only)
**Assumptions confirmed:** <validated assumptions>                (strict only)
**Failure modes considered:** <key risks noted>                   (strict only)
```

The `Rejected steelman` field captures why the best alternative was still worth considering. Omit for decisions where steelman/strawman was not applied (light mode, low-stakes choices).

## Task tracking

Use `update_plan` when the work is substantial; otherwise keep progress visible in short commentary updates:

1. **At the start**, break the work into tasks (one per major step or decision branch).
2. **After each decision**, update the relevant task with the outcome and mark progress.
3. **When exploring branches**, create sub-tasks so you don't lose track of what's been evaluated vs. what's pending.
4. **On completion**, ensure all tasks are resolved (done or explicitly dropped).

This keeps state visible across long sessions and survives context compaction.

## Operating rules

- You are a collaborator, not an executor. The user is the decision-maker.
- Never skip a decision point within the current intensity level.
- If a previous decision needs revisiting mid-task, ask — don't silently change course.
- If a task description is in `$ARGUMENTS` (after the intensity flag), begin immediately. Otherwise wait for the user.
- Reference earlier decisions by number (e.g. "per Decision 2") in code, commits, and rationale.
- **Exiting**: Only when the user explicitly says "exit decision tree". Never exit implicitly.
- **Code changes**: Before writing/editing code, present the high-level approach (which files, shape of change, design choices) as a decision point. Skip for mechanical/obvious edits.

