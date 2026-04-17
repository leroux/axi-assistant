# Decision Log: Move frontend concerns out of agenthub
**Date**: 2026-04-16
**Intensity**: balanced

## Decision 1: Initial AgentHub boundary shape

**Options considered:**
- Option A — keep one frontend protocol in AgentHub but trim obvious app-specific methods
- Option B1 — minimal split now: keep runtime semantics and human gates in AgentHub, move channel/persistence/shutdown concerns back to Axi
- Option B2 — stronger split now: also move more presentation-ish hooks out of AgentHub immediately
- Option C — redesign around typed events and pending interactions

**Choice:** Option B1
**Rationale:** Preserve the current async/runtime model and serialize by agent while making the boundary clearer with minimal migration risk.

## Decision 2: First-step method split

**Options considered:**
- 2A — move channel management, persistence/reconstruction, and shutdown out of AgentHub now; keep event sink, human gates, and todo updates for this step
- 2B — same as 2A, but also move todo updates out now
- 2C — move channel and shutdown concerns now, but keep persistence/reconstruction in AgentHub for one more step

**Choice:** 2A
**Rationale:** Remove the clearest app-specific concerns now while keeping the current async/orchestration model and minimizing migration risk.

## Decision 3: First-step interface shape

**Options considered:**
- 3A — keep one trimmed AgentHub interface for the first step and revisit a further split later
- 3B — split immediately into EventSink + HumanGate
- 3C — split immediately into three narrower interfaces

**Choice:** 3A
**Rationale:** Minimize migration churn while moving the clearly app-specific concerns out now. Preserve runtime semantics first, then reassess whether a further interface split is still needed.

## Decision 4: Whether to split the trimmed interface further in this branch

**Options considered:**
- 4A — stop at the trimmed interface for this branch step and treat it as the transitional boundary
- 4B — do a soft-split in naming/comments/grouping only
- 4C — introduce explicit EventSink/HumanGate-style ports now

**Choice:** 4A
**Rationale:** Most of the architectural value came from removing the clearly wrong concerns. A further hard split in this branch would add churn without enough immediate semantic payoff.

## Decision 5: Live test depth for this branch step

**Options considered:**
- 5A — focused live matrix on both haiku and codex
- 5B — full stress matrix on both haiku and codex
- 5C — full stress matrix on one model and focused matrix on the other

**Choice:** 5C
**Rationale:** Get one deep stress pass and one lighter comparison pass without turning this branch step into an excessively long soak run.

## Decision 6: Which model gets the full stress pass

**Options considered:**
- 6A — full stress on codex, focused matrix on haiku
- 6B — full stress on haiku, focused matrix on codex
- 6C — choose adaptively during the run

**Choice:** 6A
**Rationale:** Codex exercises the higher-risk proxy-model routing path, so it gets the deeper pass.

## Decision 7: Next validation step after the initial live matrix

**Options considered:**
- 7A — retest the live problematic parts and add headless Hypothesis integration tests
- 7B — retest the live problematic parts only
- 7C — try Hypothesis against the live harness too

**Choice:** 7A
**Rationale:** Combine direct validation of the observed live problems with stronger shrinkable integration coverage at the AgentHub runtime layer.
