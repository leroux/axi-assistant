---
name: polya
description: Use only when the user explicitly asks for Polya's method, phased problem solving, or a step-by-step process with checkpoints before implementation.
metadata:
  short-description: Structured four-phase problem solving
---

# Polya Method

This is an explicit mode. Do not use it unless the user asks for Polya-style work.

## Runtime rules

- Follow the four phases in order: Understand, Plan, Execute, Review.
- At the end of Understand and Plan, pause and ask the user directly for confirmation before proceeding.
- Ask checkpoints in the normal conversation. Do not rely on runtime-specific UI tools.
- Use `update_plan` after the plan is approved when the work is non-trivial.
- If the task turns out to be trivial during Understand, say so and ask whether to skip the full method and just do the fix.
- If execution reveals a material scope change or invalidates the approved plan, stop and return to Plan instead of silently adapting.

## Phase 1: Understand the Problem

Before anything else, build a complete picture of the problem.

1. **State the problem** in your own words. What is the unknown? What are we trying to produce or change?
2. **Identify the data.** What do we have? What inputs, constraints, invariants, existing code?
3. **Identify the condition.** What must the solution satisfy? Performance targets, API contracts, backwards compatibility, correctness properties?
4. **Draw the boundary.** What is in scope and what is NOT? Call out adjacent concerns you're intentionally ignoring.
5. **Find the gaps.** What do you NOT know yet? What needs investigation before you can plan?

Read the relevant code. Don't guess at structure or behavior — verify it.

**Checkpoint:** Summarize your understanding and ask the user to confirm, correct, or fill gaps. Do not proceed until the problem is understood.

## Phase 2: Devise a Plan

Find the connection between the data and the unknown.

1. **Have you seen this before?** Look for analogies — similar problems in this codebase, known patterns, prior decisions (check `docs/decisions/`).
2. **Can you decompose it?** Break the problem into subproblems. Identify which are independent (parallelizable) and which have ordering constraints.
3. **Can you simplify it?** If the full problem is complex, solve a reduced version first. Drop a constraint, handle fewer cases, ignore an optimization — then generalize.
4. **What are the approaches?** Enumerate concrete strategies. For each, note the key tradeoff.
5. **Pick an approach.** Recommend one with reasoning.

**Checkpoint:** Present the plan to the user, including the decomposition into steps. Do not proceed until the user approves the plan.

## Phase 3: Execute the Plan

Carry out the plan, checking each step.

1. **Work one step at a time.** Complete each step fully before moving to the next.
2. **Verify each step.** Can you see clearly that this step is correct? Run tests, check types, read the output. Don't defer verification to the end.
3. **Track progress** as you complete each step. Use `update_plan` when it helps, otherwise keep progress visible in short commentary updates.
4. **If a step fails or reveals new information**, stop. Re-evaluate the plan. If the change is material, go back to the user — don't silently adapt.
5. **If you get stuck**, try Polya's heuristics:
   - Work backwards from the desired result
   - Solve an analogous but simpler problem
   - Check boundary/edge cases — they often reveal the structure
   - Re-examine the data — is there something you're not using?

## Phase 4: Look Back

After the solution works, review it. This phase is where learning happens.

1. **Verify the result.** Does it satisfy all the conditions from Phase 1? Check the boundary you drew.
2. **Check the argument.** Can you prove correctness, or at least identify what would break it? Are there edge cases you haven't tested?
3. **Can you simplify?** Now that you see the full solution, is there a more direct path? Unnecessary complexity that crept in? Dead code from abandoned approaches?
4. **Can you generalize?** Does this solution suggest a pattern that applies elsewhere? (Don't act on this — just note it for the user.)
5. **What did you learn?** Note any surprises, wrong assumptions, or useful discoveries.

**Checkpoint:** Present the review to the user. Include what was learned and any simplification opportunities.

## Operating rules

- Keep phase labels explicit in commentary, e.g. `Phase 1 - Understand`.
- Each phase must be completed before moving to the next. The checkpoints are hard gates.
- If the problem turns out to be trivial during Phase 1, say so — the user can exit and just do it.
- Reference Polya phases by name in your communication.
- **Exiting**: User says "exit/stop/done polya". Summarize current phase and any open decisions.
- If the user stops responding at a checkpoint, stay in the current phase instead of forging ahead.

