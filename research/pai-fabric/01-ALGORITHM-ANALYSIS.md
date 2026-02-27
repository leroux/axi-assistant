# A. 7-Phase Algorithm & Verifiable Progress

**Document Version:** 1.0
**Source:** PAI v3.0 Algorithm Component v1.1.0
**Research Date:** February 27, 2026

---

## Overview

The PAI Algorithm (v1.1.0) is a scientifically grounded 7-phase execution model based on the scientific method. It provides a structured approach to problem-solving that ensures verifiable progress, quality gates at each phase, and continuous learning.

**Official Repository:** https://github.com/danielmiessler/TheAlgorithm

---

## The 7 Phases

### Phase 1: OBSERVE (Understanding)
**Purpose:** Deeply understand the problem without rushing to solutions

**Key Activities:**
- **Reverse Engineering:** Extract explicit wants, implied wants, explicit don'ts, implied don'ts, and gotchas
- **Previous Work Detection:** Identify if this task references prior work for context recovery
- **Effort Level Assessment:** Determine time budget (Instant/Fast/Standard/Extended/Advanced/Deep/Comprehensive/Loop)
- **Context Recovery (Conditional):** Search and read prior work if referenced
- **ISC Definition:** Create Ideal State Criteria (success metrics)
- **Capability Audit:** Scan all available capabilities

**Quality Gates (6 checks):**
1. **QG1 - Count & Structure:** >= 4 criteria, scale-appropriate grouping (≤16 flat, 17-32 grouped, 33+ child PRDs)
2. **QG2 - Word Count:** All criteria 8-12 words exactly
3. **QG3 - State Not Action:** No criterion starts with a verb (build, create, run, implement, add, fix, write)
4. **QG4 - Binary Testable:** Each criterion answerable YES/NO in <5 seconds with evidence
5. **QG5 - Anti-Criteria:** >= 1 anti-criterion (what must NOT happen)
6. **QG6 - Coverage:** Every explicit requirement has >= 1 criterion

**Tools Allowed in OBSERVE:**
- TaskCreate (for ISC and anti-criteria only)
- TaskList (to display working state)
- Grep/Glob/Read (ONLY in Context Recovery phase, ≤34s total)
- Voice notifications (curl commands for async feedback)

**Time Budget Allocation:**
- Instant: No phases
- Fast (<1min): OBSERVE 10s, BUILD 20s, EXECUTE 20s, VERIFY 10s
- Standard (<2min): OBSERVE 15s, THINK 15s, BUILD 30s, EXECUTE 30s, VERIFY 20s
- Extended (<8min): Full phases with 1-min checkpoints
- Advanced (<16min): Full phases with 1-min checkpoints
- Deep (<32min): Full phases with 1-min checkpoints
- Comprehensive (<120min): Full phases, no time pressure
- Loop: Unbounded, PRD-iteration driven

### Phase 2: THINK (Pressure Testing)
**Purpose:** Validate assumptions and refine understanding

**Key Activities:**
- **Pressure Test:** Challenge riskiest assumption, pre-mortem analysis, double-loop verification
- **ISC Mutations:** Document all changes to criteria since OBSERVE
- **Verification Planning:** For each criterion, define how it will be verified
- **Complexity Assessment:** If >16 ungrouped criteria, group by domain; if >32 in PRD, spawn child PRDs
- **Capability Refinement:** Add missing capabilities for task

**Quality Gate:**
Same 6 checks as OBSERVE, but with updated/refined criteria

**Tools Available:**
All tools unlocked after Quality Gate passes in OBSERVE

### Phase 3: PLAN (Strategy Development)
**Purpose:** Develop concrete execution approach

**Key Activities:**
- **Plan Mode (Extended+ effort):** Structured codebase exploration in read-only mode
- **Prerequisite Validation:** Check environment variables, dependencies, state, and file access
- **File-Edit Manifest:** List all files to change with change types (create/edit/delete)
- **Execution Strategy:** Determine if criteria can be parallelized; if so, partition to agents
- **PRD Creation:** Write Product Requirements Document with CONTEXT, PLAN, and ISC sections
- **Verification Strategy:** Finalize concrete verification commands/steps

**PRD Structure:**
```markdown
---
prd: true
id: PRD-{YYYYMMDD}-{slug}
status: CRITERIA_DEFINED
mode: interactive
effort_level: {tier}
created: {date}
updated: {date}
iteration: 0
maxIterations: 128
last_phase: PLAN
failing_criteria: []
verification_summary: "0/{N}"
---

# {Task Title}

## STATUS
| What | State |
| Progress | 0/{N} criteria passing |
| Phase | {current phase} |

## CONTEXT
### Problem Space
### Key Files
### Constraints
### Decisions Made

## PLAN
{Execution approach, technical decisions, task breakdown}

## IDEAL STATE CRITERIA
- [E] ISC-C1: {state, 8-12 words} | Verify: {method}
- [I] ISC-C2: {state, 8-12 words} | Verify: {method}
...
- [R] ISC-A1: {anti-state, 8-12 words} | Verify: {method}
```

### Phase 4: BUILD (Artifact Creation)
**Purpose:** Create work artifacts while continuously testing

**Key Activities:**
- **Test-First:** Write or run verification checks alongside artifacts, not after
- **Decision Documentation:** Non-obvious decisions appended to PRD DECISIONS section
- **Requirement Discovery:** New requirements trigger TaskCreate + PRD ISC append
- **ISC Mutations:** Document all changes during BUILD

**Critical Rule:** Verification runs parallel to building, not after.

### Phase 5: EXECUTE (Work Running)
**Purpose:** Run the work using selected capabilities

**Key Activities:**
- **Continuous Verification:** Run verification checks after each significant change
- **Edge Case Discovery:** Edge cases trigger TaskCreate + PRD ISC append
- **ISC Mutations:** Document all changes during EXECUTE

**Critical Rule:** Verify continuously, don't batch to end.

### Phase 6: VERIFY (Systematic Validation)
**Purpose:** Systematically validate all criteria

**Key Activities:**
- **Drift Check:** Did execution stay on-criteria? Any requirements discovered but not captured?
- **Per-Criterion Verification:** For EACH criterion (including anti-criteria):
  1. State the evidence (what proves YES or NO)
  2. TaskUpdate to mark completed (with evidence) or failed (with reason)
- **PRD Update:** Update ISC checkboxes and STATUS table
- **Evidence Citation:** [CRITICAL] criteria require evidence citation in verification output

**Verification Method Categories:**
| Category | When | Example |
|----------|------|---------|
| `CLI:` | Deterministic command with exit code | `Verify: CLI: curl -f http://localhost:3000/health` |
| `Test:` | Test runner execution | `Verify: Test: bun test auth.test.ts` |
| `Static:` | Type check or lint | `Verify: Static: tsc --noEmit` |
| `Browser:` | Visual verification via screenshot | `Verify: Browser: screenshot login page` |
| `Grep:` | Content pattern match | `Verify: Grep: "mode:" in PRD frontmatter` |
| `Read:` | File content inspection | `Verify: Read: check CONTEXT section exists` |
| `Custom:` | Human judgment required | `Verify: Custom: evaluate naming consistency` |

**Critical Rule:** Custom verification criteria block loop mode — loop cannot proceed if only Custom-category criteria remain.

### Phase 7: LEARN (Reflection & Memory)
**Purpose:** Extract learnings and capture memory for future

**Key Activities:**
- **PRD Log:** Append session entry with work done, criteria passed/failed, context
- **Learning Documentation:** What to improve next time?
- **Algorithm Reflection (Standard+ effort only):** Three focused questions about algorithm performance:
  - **Q1 (Self):** What would I have done differently in this Algorithm run?
  - **Q2 (Algorithm):** What would a smarter algorithm have done differently?
  - **Q3 (AI):** What would a fundamentally smarter AI have done differently?
- **Reflection Storage:** JSONL entry to MEMORY/LEARNING/REFLECTIONS/algorithm-reflections.jsonl

**Reflection Format (JSON):**
```json
{
  "timestamp": "2026-02-27T00:49:43Z",
  "effort_level": "Standard",
  "task_description": "Fix authentication bug in login flow",
  "criteria_count": 12,
  "criteria_passed": 11,
  "criteria_failed": 1,
  "prd_id": "PRD-20260227-auth-fix",
  "implied_sentiment": 7,
  "reflection_q1": "Should have invoked security review in THINK phase",
  "reflection_q2": "Should have created test cases before implementation",
  "reflection_q3": "Should have anticipated edge cases with token expiry",
  "within_budget": true
}
```

**Critical:** Focus reflection on ALGORITHM PERFORMANCE, not task subject matter.

---

## Ideal State Criteria (ISC)

### Core Requirements

**8-12 Word Requirement:** Each criterion is exactly 8-12 words (not fewer, not more)

**State, Not Action:** Describe the CONDITION that must be true
- CORRECT: "Session persists correctly across browser tab refreshes" (9 words, state)
- INCORRECT: "Run tests to ensure session persistence" (6 words, action)

**Binary Testable:** Must be answerable YES or NO in <5 seconds with evidence
- CORRECT: "JWT middleware rejects expired tokens with 401 status"
- INCORRECT: "JWT middleware properly handles token expiration" (vague)

**Granular:** One concern per criterion. If it has "and", split it.
- CORRECT: Two criteria:
  1. "Login returns JWT to client successfully"
  2. "Login returns refresh token for future sessions"
- INCORRECT: "Login returns JWT and refresh token" (two concerns)

**Confidence Tags:**
- `[E]` = Explicit (user stated)
- `[I]` = Inferred (implied by context)
- `[R]` = Reverse-engineered (intuited ideal state)

THINK phase focuses pressure testing on `[I]` and `[R]` criteria.

### ISC Scale Tiers

| Tier | Count | Structure | When |
|------|-------|-----------|------|
| **Simple** | 4-8 | Flat list | Single-file fix, skill invocation |
| **Medium** | 12-40 | Grouped by domain (### headers) | Multi-file feature, API endpoint |
| **Large** | 40-150 | Grouped domains + child PRDs | Multi-system feature, major refactor |
| **Massive** | 150-500+ | Multi-level hierarchy, team decomposition | Platform redesign, full product build |

**Structure Rules:**
- ≤16 criteria = flat list
- 17-32 = group under `### Domain` headers
- 33+ = decompose into child PRDs (one per domain)
- 100+ = multi-level hierarchy with agent teams

### Anti-Criteria

Capture what must NOT happen. Same 8-12 word rule:
- Prefix with `ISC-A` instead of `ISC-C`
- Example: `ISC-A1: No credentials exposed in repository commit history` (8 words)
- Minimum 1 anti-criterion per task; most tasks have 2-4

---

## Implementation for axi-assistant

### Phase 1: Algorithm Loop Restructuring

**Current axi Flow:**
```
Request → Execute → Verify → Output
```

**Enhanced axi Flow:**
```
Request → OBSERVE (ISC definition, capability audit)
       → THINK (pressure testing, capability refinement)
       → PLAN (execution strategy, PRD creation)
       → BUILD (artifact creation, test-first)
       → EXECUTE (run work, continuous verification)
       → VERIFY (systematic validation)
       → LEARN (reflection, memory capture)
```

### Phase 2: Core Components Needed

1. **ISC Manager**
   - TaskCreate wrapper for criterion creation
   - TaskList for displaying current ISC
   - Quality Gate validator (6 checks)
   - ISC mutation tracking

2. **Algorithm Engine**
   - Phase orchestrator with time budget tracking
   - Time check enforcement with auto-compression
   - Phase transition gating
   - Effort level assessment

3. **PRD System**
   - PRD template generation
   - PRD persistence to disk
   - PRD status progression tracking
   - Child PRD spawning for large tasks

4. **Verification Engine**
   - Verification method categorization
   - Evidence capture system
   - Per-criterion verification tracking
   - Custom criterion handling (loop mode blocking)

### Phase 3: Integration Patterns

**For Short Tasks (Fast/Standard):**
- Compress OBSERVE and THINK (15s each instead of full phase)
- Use flat ISC (4-8 criteria)
- Skip Plan Mode
- Direct to BUILD

**For Medium Tasks (Extended/Advanced):**
- Full phases with 1-min checkpoints
- Grouped ISC (12-40 criteria)
- Use Plan Mode for exploration
- Potential multi-track execution

**For Large Tasks (Deep/Comprehensive):**
- Full phases, no time pressure
- Child PRD spawning
- Multi-agent teams
- Comprehensive testing

---

## Quality Metrics

The Algorithm enables measurement of:

1. **Execution Quality:** Criteria passed/failed ratio
2. **Requirement Coverage:** User's explicit requirements → Criteria mapping
3. **Assumption Quality:** Pressure-tested ISC changes in THINK
4. **Verification Completeness:** All criteria have verification method
5. **Time Efficiency:** Actual time vs budget at each phase
6. **Learning Quality:** Algorithm reflection depth and applicability

---

## Critical Success Factors

1. **OBSERVE Quality Gates:** Do NOT proceed to THINK with blocked gates
2. **Time Budget Adherence:** Auto-compress if > 150% of phase budget
3. **ISC Precision:** 8-12 word requirement is non-negotiable
4. **Verification First:** Design verification method DURING criteria creation, not after
5. **Continuous Testing:** Run tests parallel to building, not after completion
6. **Evidence Citation:** [CRITICAL] criteria require explicit evidence in VERIFY phase

---

## Example Algorithm Run (Standard Effort)

**Task:** "Add password reset flow to authentication system"
**Budget:** <2 minutes total

### OBSERVE (15s)
```
Reverse Engineering:
- Want: Password reset flow with email validation
- Implied: Secure token generation, token expiry, UI for reset
- Don't want: SMS-based reset (email only per context)
- Gotchas: Token expiry, email delivery reliability

Effort Level: Standard (2min budget)

ISC Definition:
[E] ISC-C1: User can request password reset via email
[E] ISC-C2: Reset email contains unique reset token
[I] ISC-C3: Reset token expires after 24 hours
[I] ISC-C4: User can set new password with valid token
[R] ISC-C5: Invalid token returns 401 with helpful message
[R] ISC-C6: Old password doesn't grant access after reset
[E] ISC-A1: Reset token not exposed in logs or database plaintext
[E] ISC-A2: Reset flow requires email verification before password change

Quality Gate: PASS (8 criteria, all 8-12 words, all state-based, all binary testable)
```

### THINK (15s)
```
Pressure Test:
- Assumption: Email delivery is reliable → Evidence: Retry logic in queue
- Pre-mortem: What if token expires mid-reset? → Add timeout messaging
- Double-loop: If all criteria pass, does user actually get reset? → YES

ISC Mutations: NONE - OBSERVE criteria held under pressure test

Verification Plan:
- ISC-C1: CLI: POST /auth/password-reset → returns 200
- ISC-C2: Read: Email contains token field
- ISC-C3: Test: Token expires after 24h
- ISC-C4: CLI: POST /auth/reset with token → password changes
- ISC-C5: CLI: POST /auth/reset with bad token → 401
- ISC-C6: CLI: Login with old password → 401
- ISC-A1: Grep: No tokens in logs
- ISC-A2: Browser: Reset flow requires email confirmation
```

### PLAN (Not used for Standard effort - skip to BUILD)

### BUILD (30s)
```
Create:
- /auth/password-reset.endpoint.ts (request handler)
- /auth/password-reset.test.ts (test cases)
- /emails/password-reset.template.html (email template)
- /database/migrations/add_reset_tokens_table.sql

Test-first:
- Write test for token generation
- Write test for token expiry
- Write test for reset validation
```

### EXECUTE (30s)
```
Implement:
1. Endpoint that validates email, generates token, queues email
2. Token storage with 24h expiry
3. Reset endpoint that validates token, validates new password, updates database
4. Email template generation

Continuous Verify:
- After each piece: run relevant test
- Edge case: What if user requests reset twice? → Invalidate old token
- Edge case: What about SQL injection in reset form? → Use prepared statements
```

### VERIFY (20s)
```
For each criterion:
✅ ISC-C1: POST /auth/password-reset returns 200 - PASS (verified CLI)
✅ ISC-C2: Email contains token field - PASS (verified read)
✅ ISC-C3: Token expires after 24h - PASS (test passes)
✅ ISC-C4: POST /auth/reset with token changes password - PASS (verified CLI)
✅ ISC-C5: POST /auth/reset with bad token → 401 - PASS (verified CLI)
✅ ISC-C6: Login with old password → 401 - PASS (verified CLI)
✅ ISC-A1: No tokens in logs - PASS (verified grep)
✅ ISC-A2: Reset flow requires email confirmation - PASS (verified browser)

All criteria: PASS (8/8)
```

### LEARN
```
PRD Log:
- Session: 20260227-auth-reset
- Work: Password reset flow implementation
- Result: 8/8 criteria passing
- Time: 1:58 (within budget)

Reflection:
Q1: Should have created email template earlier (it blocked testing)
Q2: Should have done security review of token generation
Q3: Should have anticipated email delivery delay scenarios
```

---

## Comparison with axi Current Flow

| Aspect | Current axi | Enhanced with Algorithm |
|--------|------------|----------------------|
| Requirements | Implicit, discovered during execution | Explicit ISC, defined in OBSERVE |
| Verification | Ad-hoc, at the end | Systematic, per-phase, continuous |
| Quality Gates | None | 6-point gate at OBSERVE/THINK/PLAN |
| Learning | Implicit | Explicit reflection, stored as JSON |
| Error Recovery | Manual restart | ISC-aware resumption from last phase |
| Time Tracking | None | Budget-based with auto-compression |
| Work Continuity | Lost between sessions | PRD provides handoff contract |
| Complexity Scaling | Not designed for large tasks | ISC decomposition and child PRDs |

---

## Resources

- **Official Algorithm:** https://github.com/danielmiessler/TheAlgorithm
- **PAI Implementation:** /tmp/pai-fabric-research/Personal_AI_Infrastructure/Releases/v3.0/.claude/skills/PAI/Components/Algorithm/
- **Algorithm Version:** v1.1.0
- **Source Documentation:** 20-the-algorithm.md in PAI repository

---

**Document Complete**
**Recommendation:** Start with basic algorithm loop (OBSERVE → THINK → PLAN → BUILD → EXECUTE → VERIFY → LEARN) as foundation for all other PAI features
