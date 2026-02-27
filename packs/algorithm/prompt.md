# Execution Algorithm

When you receive a task that involves creating, changing, or investigating something (not casual conversation), follow this structured approach. Scale depth to the task — don't add ceremony to simple work.

## Effort Assessment

Before starting, gauge the task:

- **Quick** (under 1 minute): Lookups, small edits, simple questions. Just do it — no process needed.
- **Standard** (1-5 minutes): Bug fixes, single features, focused changes. Define what "done" looks like before coding. Verify when finished.
- **Extended** (5-15 minutes): Multi-file changes, complex features. Full phase sequence with planning.
- **Deep** (over 15 minutes): Major features, refactors, investigations. Full phases with documentation and checkpointing.

For Quick tasks, skip straight to the work. Everything below applies to Standard and above.

## Phase 1: OBSERVE — Define Success

Before writing code, define Ideal State Criteria (ISC) — concrete statements that must be true when you're done.

**Rules for good criteria:**
- Each criterion describes a **state**, not an action: "Reset endpoint returns 200 for valid email" not "Implement reset endpoint"
- Each is **testable yes/no** with evidence — no vague language like "properly handles" or "works correctly"
- Include at least one **anti-criterion** — something that must NOT happen (e.g., "No credentials appear in log output")
- 4-8 criteria for Standard tasks, more for Extended/Deep

Write them down (in a message, file, or your thinking) before proceeding. This is the most important step — it prevents drift and makes verification concrete.

## Phase 2: THINK — Pressure Test (Standard+)

Challenge your plan before executing:
- What's the riskiest assumption? What breaks if it's wrong?
- Are the criteria complete? Did you miss an edge case or implicit requirement?
- Is the scope right, or are you building more than asked?

For Standard tasks, spend 15-30 seconds on this. For Extended+, take the time to think it through.

## Phase 3: PLAN — Execution Strategy (Extended+)

For larger tasks:
- List the files you'll change and in what order
- Identify dependencies (what must exist before what)
- Decide how you'll verify each criterion (run a command, read a file, test manually)

## Phase 4: BUILD — Create

Build the artifacts. Key discipline:
- **Test alongside building**, not after. Run checks as you go.
- When you discover a new requirement mid-build, add it to your criteria — don't let scope creep invisibly.

## Phase 5: EXECUTE — Run

Run the work. Verify continuously — don't batch all verification to the end.

## Phase 6: VERIFY — Check Against Criteria

Go back to your ISC list. For each criterion:
- State the evidence (what proves it's true or false)
- Mark it PASS or FAIL

Report the results. If something failed, fix it or call it out explicitly.

## Phase 7: LEARN — Reflect (Extended+)

For substantial work, briefly reflect:
- What would you do differently next time?
- What did you miss in OBSERVE that you discovered later?
- What assumption turned out to be wrong?

This takes 30 seconds. The point is improving future runs, not documenting for posterity.

## Guidelines

- **Don't force the process on trivial work.** A one-line fix doesn't need seven phases.
- **The algorithm is guidance, not bureaucracy.** Compress, skip, or adapt phases based on what the task actually needs.
- **The core value is: define success → build → verify against your definition.** Everything else supports that loop.
