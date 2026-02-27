# G. Other Interesting Ideas

**Document Version:** 1.0
**Research Date:** February 27, 2026

---

## Overview

Beyond the main 6 investigation areas, both PAI and Fabric contain numerous innovative features and patterns worth considering for axi-assistant enhancement.

---

## 1. Constraint Extraction & Build Drift Prevention

**Source:** PAI v3.0 Release Notes (v3.0.0 - Jan 2026)

### What Is It?

Constraint extraction automatically identifies and enforces project constraints (backwards compatibility, performance budgets, API contracts) during BUILD phase.

Build drift prevention detects when implementation deviates from stated constraints.

### Implementation Concept

**During PLAN Phase:**
```
Constraint Discovery:
  - "Must maintain API v2.0 backwards compatibility"
  - "Page load time must stay under 2 seconds"
  - "Database queries must use prepared statements (security)"
  - "No breaking changes to public methods"
```

**During BUILD Phase:**
```
Constraint Enforcement:
  FOR EACH constraint:
    IF (new code violates constraint):
      ✗ DRIFT DETECTED
      → Flag implementation review
      → Suggest constraint-respecting alternatives
      → Option to update constraint if intentional
```

### Benefits
- Prevents subtle breaking changes
- Maintains performance contracts
- Enforces security constraints automatically
- Documents implicit constraints explicitly

### Implementation for axi

```typescript
interface Constraint {
  type: "backwards_compat" | "performance" | "security" | "custom"
  description: string
  metric?: string  // "API response time < 100ms"
  detector: (code: string) => boolean  // Check if violated
  severity: "critical" | "warning" | "info"
}

// Constraint detection during BUILD
async function detectConstraintDrift(
  newCode: string,
  constraints: Constraint[]
): Promise<ConstraintViolation[]> {
  const violations: ConstraintViolation[] = []

  for (const constraint of constraints) {
    if (constraint.detector(newCode)) {
      violations.push({
        constraint: constraint.description,
        severity: constraint.severity,
        suggestion: generateConstraintFixSuggestion(constraint),
      })
    }
  }

  return violations
}
```

---

## 2. Persistent PRDs (Product Requirements Documents)

**Source:** PAI v3.0 Algorithm with PRD integration

### What Is It?

PRDs serve as persistent state for multi-session work. They survive session boundaries and enable resumption from where you left off.

### PRD as Work Contract

**Between Sessions:**
```
Session 1 (20260227):
  OBSERVE: Created ISC with 12 criteria
  THINK: Refined to 8 focused criteria
  PLAN: Wrote execution strategy
  Status: CRITERIA_DEFINED

Session 2 (20260228):
  Read: Load PRD-20260227-auth-system.md
  Last phase: PLAN
  Failing criteria: None
  → Resume directly to BUILD
  No need to re-run OBSERVE/THINK/PLAN
```

**PRD Structure:**
```markdown
---
prd: true
id: PRD-20260227-auth-system
status: IN_PROGRESS
last_phase: PLAN
failing_criteria: []
---

# Build Authentication System

## CONTEXT
[Problem space, key files, constraints]

## IDEAL STATE CRITERIA
- [x] ISC-C1: Users can register with email
- [ ] ISC-C2: Email verification required
...

## PLAN
[Execution approach from last session]

## PROGRESS LOG
- **20260227:** Defined 8 criteria, planned architecture
- **20260228:** Starting BUILD phase
```

### Benefits
- **Work Continuity:** Pick up exactly where you left off
- **Requirements Stability:** ISC doesn't drift between sessions
- **Handoff Contract:** If delegating to another agent, PRD is the contract
- **Audit Trail:** Complete history of what was decided and why

### Implementation for axi

```typescript
interface PRD {
  id: string
  status: "DRAFT" | "CRITERIA_DEFINED" | "PLANNED" | "IN_PROGRESS" | "VERIFYING" | "COMPLETE"
  lastPhase: Phase
  createdAt: Date
  updatedAt: Date
  iteration: number
  maxIterations: number
  failingCriteria: string[]

  context: {
    problemSpace: string
    keyFiles: string[]
    constraints: string[]
  }

  idealStateCriteria: ISC[]
  plan: string
  progressLog: ProgressEntry[]
}

// Load PRD and resume work
async function resumeWorkFromPRD(prdPath: string): Promise<void> {
  const prd = await readPRD(prdPath)

  if (prd.status === "CRITERIA_DEFINED") {
    // Skip OBSERVE/THINK, jump to PLAN
    await runPlanPhase(prd.idealStateCriteria)
  } else if (prd.status === "PLANNED") {
    // Skip to BUILD
    await runBuildPhase(prd)
  } else if (prd.status === "IN_PROGRESS") {
    // Continue from last known state
    const lastWork = prd.progressLog[prd.progressLog.length - 1]
    console.log(`Resuming ${lastWork.description}`)
    // Load context from last session
  }
}
```

---

## 3. Parallel Loop Execution

**Source:** PAI v3.0 Release Notes (v3.0.0 - Jan 2026)

### What Is It?

When a task can be decomposed into independent work streams, spawn parallel agents to work on them simultaneously, then merge results.

### Example

**Task:** Build complete e-commerce platform (150+ ISC)

**Decomposition:**
```
├─ Domain 1: User Management (30 criteria)
│  └─ Agent 1 (Engineer)
├─ Domain 2: Product Catalog (35 criteria)
│  └─ Agent 2 (Engineer)
├─ Domain 3: Shopping Cart (25 criteria)
│  └─ Agent 3 (Engineer)
├─ Domain 4: Payment Processing (30 criteria)
│  └─ Agent 4 (Engineer)
└─ Domain 5: Integration & Verification (30 criteria)
   └─ Main Agent (Coordinator)
```

**Parallel Execution:**
```
Time 0:   Agents 1-4 start simultaneously
Time 15min: Agent 1 completes, submits for review
Time 18min: Agent 2 completes, submits for review
Time 22min: Agent 3 completes, submits for review
Time 25min: Agent 4 completes, submits for review
Time 30min: Main agent (Coordinator) merges all work
Time 35min: Verify all ISC pass
```

**Sequential Alternative:**
```
Agent 1: 20 min
Agent 2: 20 min
Agent 3: 20 min
Agent 4: 20 min
Integration: 15 min
Total: 95 min
```

**Parallel vs Sequential: 35 min vs 95 min (63% faster)**

### Implementation for axi

```typescript
async function executeParallel(
  mainISC: ISC[],
  domains: TaskDomain[]
): Promise<WorkResult> {
  // Create child PRDs for each domain
  const childPRDs = domains.map(domain => ({
    id: generatePRDId(),
    idealStateCriteria: mainISC.filter(c => c.domain === domain.name),
    parentPRDId: mainISC[0].prdId,
    domain: domain.name,
  }))

  // Spawn parallel agents
  const agentPromises = childPRDs.map(prd =>
    spawnEngineerAgent({
      prd: prd,
      effortLevel: "Extended",
      callback: onAgentComplete,
    })
  )

  // Wait for all to complete
  const results = await Promise.all(agentPromises)

  // Merge results
  const mergedWork = mergeAgentResults(results)

  // Verify no conflicts
  await verifyMergedWork(mergedWork, mainISC)

  return mergedWork
}
```

---

## 4. Context Window Optimization

**Source:** Fabric v1.4.285+ (Extended Context Support)

### What Is It?

Intelligent context truncation and prioritization when working with large codebases and long histories.

### Strategies

**1. Semantic Chunking**
```
Instead of reading entire file:
  READ: /src/auth.ts (2000 lines)

Do semantic chunking:
  - Section 1: Imports & types (100 lines)
  - Section 2: AuthService class (500 lines)
  - Section 3: OAuth integration (400 lines)
  - Section 4: Tests (1000 lines)

When building password reset:
  Load: Section 1 + Section 2
  Skip: Section 3 (not relevant)
  Skip: Section 4 (not needed for understanding)
  Total loaded: 600 lines instead of 2000
```

**2. LRU Context Caching**
```
Track most relevant files for this work:
  Current work: "Auth system"
  Most relevant:
    1. /src/auth.ts (loaded 5 times, last 2min ago)
    2. /src/types.ts (loaded 3 times, last 5min ago)
    3. /tests/auth.test.ts (loaded 1 time, last 10min ago)

When context pressure (>90% used):
  Keep:  Files 1-2 (frequently accessed)
  Cache: File 3 (can reload if needed)
  Drop:  Files last accessed >30min ago
```

**3. Task-Aware Summarization**
```
Task: "Fix authentication timeout"

Relevant context:
  - Auth flow (HIGH)
  - Timeout handling (HIGH)
  - Session management (MEDIUM)
  - UI components (LOW)

Summarize low-relevance sections:
  "UI components: LoginForm, RegistrationForm, ProfilePage
   No changes needed here."

Full context for high-relevance:
  [Complete session management code]
  [Complete timeout logic]
```

### Benefits
- Keep more context in window
- Avoid context thrashing
- Faster loading of relevant sections
- Better reasoning about code changes

---

## 5. Versioned Capability Selection

**Source:** Fabric's per-pattern model mapping

### What Is It?

Each skill/pattern can specify which LLM models work best for it, with version tracking.

### Implementation

```yaml
# ~/.claude/skills/code_review/config.yaml

patterns:
  primary:
    description: "Comprehensive code review"
    recommended_models:
      - model: claude-opus-4.6
        version: "1.0.0"
        rationale: "Best at architectural analysis"
        performance_metric: "8.7/10 user satisfaction"
      - model: claude-sonnet-4.5
        version: "1.0.0"
        rationale: "Good for speed, slightly less detail"
        performance_metric: "7.9/10 user satisfaction"
      - model: gpt-4-turbo
        version: "1.0.0"
        rationale: "Alternative if Claude unavailable"
        performance_metric: "7.2/10 user satisfaction"

  security:
    description: "Security-focused code review"
    recommended_models:
      - model: claude-opus-4.6
        version: "1.0.0"
        rationale: "Best at finding subtle bugs"
        performance_metric: "9.1/10 user satisfaction"

  performance:
    description: "Performance optimization review"
    recommended_models:
      - model: claude-opus-4.6
        version: "1.0.0"
        rationale: "Strong algorithmic analysis"
        performance_metric: "8.5/10 user satisfaction"
```

### Benefits
- Data-driven model selection
- Capability-specific optimization
- Performance tracking per pattern+model combo
- Easy to fallback or try alternatives

---

## 6. Declarative Markdown-Based Specs

**Source:** PAI's PRD system, Fabric's pattern structure

### What Is It?

Specifications written in Markdown (human-readable, version-controllable) rather than JSON/YAML.

### Example

```markdown
# User Authentication System Specification

## Overview
User authentication with email/password, social login, and 2FA.

## Requirements

### Functional
- User registration with email validation
- Password reset flow
- Social login (Google, GitHub)
- Two-factor authentication (TOTP)
- Session management with refresh tokens

### Non-Functional
- Login must complete in <500ms
- Support 10,000 concurrent sessions
- Backwards compatible with API v2.0
- Zero data loss on restarts

## Constraints
- GDPR compliance required
- Passwords never logged
- No plaintext storage anywhere

## Success Criteria
- [ ] All functional requirements implemented
- [ ] Performance tests pass (<500ms login)
- [ ] Security audit passes
- [ ] 100 concurrent users tested successfully
- [ ] Backwards compatibility verified

## API Changes
- None (maintains API v2.0)

## Timeline
- Week 1: Core auth implementation
- Week 2: Social login integration
- Week 3: 2FA and testing
- Week 4: Security audit and hardening
```

### Benefits
- Readable by non-technical stakeholders
- Version control friendly (git diffs are human-readable)
- Can be automatically parsed into structured data
- Encourages clear thinking (writing forces clarity)

---

## 7. Skill Composition & Chaining

**Source:** Fabric's pattern composability, PAI's agent delegation

### What Is It?

Skills can invoke other skills, creating composed, higher-level capabilities.

### Example

**Base Skills:**
- `summarize` - Extract summary from text
- `extract_domains` - Find key domains/concepts
- `create_outline` - Create structured outline

**Composed Skill:**
```typescript
class DocumentAnalysisSkill extends Skill {
  async execute(params: SkillParams): Promise<SkillResult> {
    // 1. Create outline of document
    const outline = await this.invoke("create_outline", {
      input: params.document,
      depth: 3,
    })

    // 2. For each section, extract domains
    const domainsBySection = {}
    for (const section of outline.sections) {
      domainsBySection[section.title] = await this.invoke(
        "extract_domains",
        { input: section.content }
      )
    }

    // 3. Create summary highlighting top domains
    const summary = await this.invoke("summarize", {
      input: params.document,
      focus_domains: this.topDomains(domainsBySection),
    })

    return {
      outline: outline,
      domains_by_section: domainsBySection,
      summary: summary,
    }
  }
}
```

### Benefits
- Code reuse (don't rewrite summarization logic)
- Composability (mix and match skills)
- Testability (test components independently)
- Clarity (flow is explicit)

---

## 8. Pattern Versioning & A/B Testing

**Source:** Fabric's version management capabilities

### What Is It?

Test different prompt versions to see which performs better, then promote winners.

### Workflow

```
Pattern: code_review
Versions:
  v1.0 (current): "Analyze code for quality issues"
  v1.1 (test): "Analyze code using SOLID principles checklist"
  v1.2 (test): "Analyze code with focus on maintainability"

Split traffic:
  v1.0: 50% of requests (baseline)
  v1.1: 25% of requests
  v1.2: 25% of requests

Track metrics:
  v1.0: 7.2/10 avg satisfaction, 45s avg time
  v1.1: 7.6/10 avg satisfaction, 52s avg time
  v1.2: 7.4/10 avg satisfaction, 48s avg time

Decision: Promote v1.1 (better quality, acceptable time cost)
  v1.1 becomes new v1.0
  v1.0 becomes v0.9 (deprecated)
  Plan next iteration
```

### Implementation

```typescript
async function selectPatternVersion(
  patternName: string,
  context: ExecutionContext
): Promise<PatternVersion> {
  const activeVersions = await getActiveVersions(patternName)

  // Route to versions based on A/B test weights
  const selectedVersion = await routeToVersion(
    activeVersions,
    context.userId || "anonymous"
  )

  return selectedVersion
}

// After execution, record outcome
async function recordOutcome(
  patternName: string,
  version: string,
  outcome: {
    duration: number
    userRating: number
    qualityMetrics: Record<string, number>
  }
): Promise<void> {
  await appendToMetrics({
    pattern: patternName,
    version: version,
    timestamp: new Date(),
    ...outcome,
  })

  // If v1.1 is winning significantly, promote it
  const stats = await analyzeVersionStats(patternName)
  if (shouldPromoteVersion(stats, version)) {
    await promoteVersion(patternName, version)
  }
}
```

---

## 9. Contextual Skill Selection

**Source:** PAI's capability audit system

### What Is It?

Automatically select the right skill based on detected context, not explicit user request.

### Examples

**User Message:** "Check if this code has security bugs"

**Detection Logic:**
```
User signal: "security bugs"
Context: Code snippet provided
Available skills:
  ├─ code_review (general)
  ├─ code_review_security (specialized)
  ├─ code_review_performance
  └─ analyze_code_for_vulnerabilities

Decision: Use code_review_security
  Confidence: 95%
  Rationale: User explicitly mentioned "security"
  Alternative: analyze_code_for_vulnerabilities would also work (92% confidence)
```

**User Message:** "This is taking forever to load"

**Detection Logic:**
```
User signal: "performance issue" (inferred)
Context: Application complaint
Available skills:
  ├─ code_review_performance (for code)
  ├─ analyze_logs_for_bottlenecks (for runtime)
  ├─ profile_application (for instrumentation)
  └─ optimize_database_queries (for database)

Decision: Ask clarifying question
  "I can help with performance. Is the slowness in:
   A) The code (needs review)
   B) The database (query optimization)
   C) The infrastructure (resource analysis)
   D) Not sure (run broad diagnostic)"
```

### Benefits
- More intuitive user experience
- Users don't need to know skill names
- Contextual intelligence
- Fallback to clarifying questions when unsure

---

## 10. Observability & Structured Logging

**Source:** PAI's observability system

### What Is It?

Every algorithm phase emits structured logs that can be queried for insights.

### Structured Log Example

```json
{
  "timestamp": "2026-02-27T10:30:45.123Z",
  "session_id": "sess-abc123",
  "work_id": "20260227-auth",
  "phase": "OBSERVE",
  "event_type": "phase_transition",
  "effort_level": "Standard",
  "duration_ms": 18500,
  "metrics": {
    "context_recovery_time_ms": 250,
    "isc_criteria_count": 8,
    "capability_audit_duration_ms": 3200,
    "quality_gate_status": "PASS"
  },
  "artifacts_created": [
    "WORK/20260227-auth/ISC.json",
    "WORK/20260227-auth/META.yaml"
  ],
  "errors": [],
  "warnings": []
}
```

### Query Examples

```bash
# Find slow OBSERVE phases
logs query 'phase="OBSERVE" AND duration_ms > 30000'

# Find failed quality gates
logs query 'phase="OBSERVE" AND metrics.quality_gate_status="BLOCKED"'

# Performance trend for specific effort level
logs query 'effort_level="Standard"' | analyze-duration-trend

# User satisfaction impact of ISC count
logs query 'event_type="LEARN"' | correlate-with 'metrics.isc_criteria_count'
```

---

## 11. Knowledge Graph Building

**Source:** Fabric's conceptmap pattern

### What Is It?

Automatically build a knowledge graph of domain concepts from sessions.

### Example

```
From sessions analyzing React architecture:

Concepts:
  - React Component (core concept)
  - JSX (syntax, related to React Component)
  - Hooks (React feature, extends Component)
  - useState (specific Hook)
  - useEffect (specific Hook)
  - Virtual DOM (React mechanism)
  - Reconciliation (React algorithm)

Relationships:
  React Component --uses--> JSX
  React Component --enhanced-by--> Hooks
  Hooks --includes--> useState
  Hooks --includes--> useEffect
  React Component --optimized-by--> Virtual DOM
  Virtual DOM --implements--> Reconciliation

Knowledge stored in:
  ~/.claude/MEMORY/LEARNING/KNOWLEDGE-GRAPHS/react.json
```

### Benefits
- Identify gaps in understanding
- Suggest connections user hasn't made
- Track domain expertise over time
- Guide curriculum/learning path

---

## 12. Time-Bounded Work Contracts

**Source:** PAI's effort level system

### What Is It?

Explicit time budgets for work, with automatic compression if exceeding budget.

### Implementation

```typescript
enum EffortLevel {
  INSTANT = { budget: 10_000, name: "Instant" },
  FAST = { budget: 60_000, name: "Fast (<1min)" },
  STANDARD = { budget: 120_000, name: "Standard (<2min)" },
  EXTENDED = { budget: 480_000, name: "Extended (<8min)" },
  ADVANCED = { budget: 960_000, name: "Advanced (<16min)" },
  DEEP = { budget: 1_920_000, name: "Deep (<32min)" },
  COMPREHENSIVE = { budget: 7_200_000, name: "Comprehensive (<2hrs)" },
}

// Auto-compress if over budget
function checkTimeAndCompress(
  phase: Phase,
  elapsedMs: number,
  effortLevel: EffortLevel
): { canContinue: boolean; newEffortLevel?: EffortLevel } {
  const budgetMs = effortLevel.budget
  const percentUsed = (elapsedMs / budgetMs) * 100

  if (percentUsed > 150) {
    // Over 150% of budget: compress to next lower tier
    const newTier = downgradeEffortLevel(effortLevel)
    console.warn(
      `OVER BUDGET: ${percentUsed.toFixed(0)}% | Compressing to ${newTier.name}`
    )
    return { canContinue: true, newEffortLevel: newTier }
  } else if (percentUsed > 100) {
    // 100-150%: warn but continue
    console.warn(
      `APPROACHING LIMIT: ${percentUsed.toFixed(0)}% of budget used`
    )
    return { canContinue: true }
  }

  return { canContinue: true }
}
```

---

## 13. Explicit Permission to Not Know

**Source:** PAI Principles (#16)

### What Is It?

Clear allowance to say "I don't know" prevents hallucination.

### Implementation

```typescript
// If confidence below threshold, say so
async function executeWithConfidenceThreshold(
  request: UserRequest,
  confidenceThreshold: number = 0.7
): Promise<Response> {
  const response = await generateResponse(request)
  const confidence = await assessConfidence(response)

  if (confidence < confidenceThreshold) {
    return {
      type: "UNCERTAIN",
      message: `I'm not confident enough to provide an answer on this topic.
                Confidence: ${(confidence * 100).toFixed(0)}%.
                What I know: [partial information].
                What I'm uncertain about: [gaps].
                I'd recommend: [alternative approaches].`,
      confidence: confidence,
    }
  }

  return response
}
```

### Benefits
- Prevents harmful hallucinations
- Builds trust (honest about limitations)
- Guides user to other resources
- Identifies learning opportunities

---

## Summary of Other Ideas

| Idea | Difficulty | Impact | Recommended Timing |
|------|-----------|--------|-------------------|
| Constraint Extraction | Medium | High | Medium priority (Q2) |
| Persistent PRDs | Medium | High | High priority (Q1) |
| Parallel Execution | High | High | Q3+ |
| Context Optimization | Medium | Medium | Q2 |
| Versioned Capabilities | Medium | Medium | Q2 |
| Markdown Specs | Low | Medium | Q1 |
| Skill Composition | Medium | High | Q2 |
| Pattern A/B Testing | Medium | Medium | Q3 |
| Contextual Selection | High | High | Q3 |
| Observability | Medium | High | Q2 |
| Knowledge Graphs | High | Medium | Q4+ |
| Time-Bounded Work | Low | High | Q1 (with algorithm) |
| Permission to Not Know | Low | High | Q1 |

---

## Resources

- **PAI Release Notes:** https://github.com/danielmiessler/Personal_AI_Infrastructure/releases/tag/v3.0.0
- **Fabric Documentation:** https://github.com/danielmiessler/fabric/tree/main/docs
- **Structured Logging:** https://www.splunk.com/en_us/blog/learn/structured-logging.html
- **Knowledge Graphs:** https://en.wikipedia.org/wiki/Knowledge_graph

---

**Document Complete**
**Recommendation:** Implement "Time-Bounded Work" and "Permission to Not Know" immediately as they have low implementation cost with high user impact. Add others as part of quarterly roadmap planning.
