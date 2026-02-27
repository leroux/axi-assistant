# B. Memory System Architecture

**Document Version:** 1.0
**Source:** PAI v3.0 Memory System v7.0
**Research Date:** February 27, 2026

---

## Overview

PAI's memory system (v7.0) is a sophisticated multi-tier architecture that leverages Claude Code's native `projects/` storage as the source of truth, with specialized hooks capturing domain-specific events into organized directories.

**Key Insight:** Rather than duplicating session data, PAI uses Claude Code's built-in 30-day transcript retention and augments it with domain-specific learning captures.

---

## Core Architecture

```
User Request
    ↓
Claude Code projects/ (native transcript storage - 30-day retention)
    ↓
Hook Events trigger domain-specific captures:
    ├── AutoWorkCreation → WORK/
    ├── ResponseCapture → WORK/, LEARNING/
    ├── RatingCapture → LEARNING/SIGNALS/
    ├── WorkCompletionLearning → LEARNING/
    ├── AgentOutputCapture → RESEARCH/
    └── SecurityValidator → SECURITY/
    ↓
Harvesting (periodic):
    ├── SessionHarvester → LEARNING/ (extracts corrections, errors, insights)
    └── LearningPatternSynthesis → LEARNING/SYNTHESIS/ (aggregates ratings)
```

**Critical Principle:** No intermediate "firehose" layer. Claude Code's `projects/` serves that purpose natively.

---

## Directory Structure

```
~/.claude/MEMORY/
├── WORK/                      # PRIMARY work tracking
│   └── {work_id}/
│       ├── META.yaml          # Status, session, lineage
│       ├── ISC.json           # Ideal State Criteria (versions + current)
│       ├── items/             # Individual work items
│       │   └── {timestamp}_item.json
│       ├── agents/            # Sub-agent work (parallel execution)
│       ├── research/          # Research findings from agents
│       ├── scratch/           # Iterative artifacts (diagrams, prototypes)
│       ├── verification/      # Evidence artifacts
│       └── children/          # Nested work (child PRDs)
│
├── LEARNING/                  # Learnings (derived insights, not raw events)
│   ├── SYSTEM/                # PAI/tooling learnings
│   │   └── YYYY-MM/
│   ├── ALGORITHM/             # Task execution learnings
│   │   └── YYYY-MM/
│   ├── FAILURES/              # Full context for low ratings (1-3)
│   │   └── YYYY-MM/
│   │       └── {timestamp}_{8-word-description}/
│   │           ├── CONTEXT.md
│   │           ├── transcript.jsonl
│   │           ├── sentiment.json
│   │           └── tool-calls.json
│   ├── SYNTHESIS/             # Aggregated pattern analysis
│   │   └── YYYY-MM/
│   │       └── weekly-patterns.md
│   ├── REFLECTIONS/           # Algorithm reflections
│   │   └── algorithm-reflections.jsonl
│   └── SIGNALS/               # User satisfaction ratings
│       └── ratings.jsonl
│
├── RESEARCH/                  # Agent output captures
│   └── YYYY-MM/
│       └── YYYY-MM-DD-HHMMSS_AGENT-type_description.md
│
├── SECURITY/                  # Security audit events
│   └── security-events.jsonl
│
├── STATE/                     # Fast operational state (ephemeral)
│   ├── algorithms/            # Per-session algorithm state
│   │   └── {sessionId}.json
│   ├── kitty-sessions/        # Terminal environment
│   │   └── {sessionId}.json
│   ├── tab-titles/            # Terminal tab state
│   │   └── {windowId}.json
│   ├── session-names.json     # Auto-generated session names
│   ├── current-work.json      # Active work directory pointer
│   ├── format-streak.json     # Format consistency metrics
│   ├── algorithm-streak.json  # Algorithm performance metrics
│   ├── trending-cache.json    # Cached analysis (TTL-based)
│   ├── progress/              # Multi-session project tracking
│   └── integrity/             # System health checks
│
├── PAISYSTEMUPDATES/          # Architecture change history
│   ├── index.json
│   ├── CHANGELOG.md
│   └── YYYY/MM/
│
└── README.md
```

---

## Directory Details

### Claude Code projects/ - Native Session Storage

**Location:** `~/.claude/projects/-Users-{username}--claude/`

**What populates it:** Claude Code automatically (every conversation)

**Content:** Complete session transcripts in JSONL format
- **Format:** `{uuid}.jsonl` - one file per session
- **Retention:** 30 days (Claude Code manages cleanup)
- **Purpose:** Source of truth for all session data

**Key Point:** This is the actual "firehose" - every message, tool call, and response. PAI leverages this native storage rather than duplicating it.

### WORK/ - Primary Work Tracking

**What populates it:**
- `AutoWorkCreation.hook.ts` on UserPromptSubmit (creates work dir)
- `WorkCompletionLearning.hook.ts` on Stop (updates work items)
- `SessionSummary.hook.ts` on SessionEnd (marks COMPLETED)

**Content:** Work directories with metadata, items, verification artifacts

**Format:** `WORK/{work_id}/` with META.yaml, items/, verification/, etc.

**Work Directory Lifecycle:**
1. `UserPromptSubmit` → AutoWorkCreation creates work dir + first item
2. `Stop` → WorkCompletionLearning updates item with response summary + captures ISC
3. `SessionEnd` → SessionSummary marks work COMPLETED, clears state

#### META.yaml Format

```yaml
# Work metadata file
work_id: "20260118-auth-reset-flow"
session_id: "sess-abc123"
created_at: "2026-01-18T10:30:00Z"
updated_at: "2026-01-18T10:45:00Z"
status: "COMPLETED"  # CREATED, IN_PROGRESS, COMPLETED, FAILED, BLOCKED
effort_level: "Standard"  # Instant, Fast, Standard, Extended, Advanced, Deep, Comprehensive, Loop
title: "Implement password reset flow"
description: "Add email-based password reset to authentication system"
user_rating: 8  # 1-10 rating if provided
failure_reason: null  # If FAILED, explanation
iterations: 1
lineage:
  parent_work_id: null
  child_work_ids: []
  related_work_ids: []
tags:
  - "authentication"
  - "feature-request"
  - "security"
```

#### ISC.json Format

Captures the Ideal State Criteria from PAI Algorithm execution:

```json
{
  "workId": "20260118-auth-reset",
  "effortTier": "STANDARD",
  "current": {
    "criteria": [
      {
        "id": "ISC-C1",
        "text": "User can request password reset via email",
        "confidence": "E",
        "verificationMethod": "CLI",
        "status": "PASS"
      },
      {
        "id": "ISC-C2",
        "text": "Reset email contains unique reset token",
        "confidence": "E",
        "verificationMethod": "Read",
        "status": "PASS"
      }
    ],
    "antiCriteria": [
      {
        "id": "ISC-A1",
        "text": "Reset token not exposed in logs or database plaintext",
        "confidence": "E",
        "verificationMethod": "Grep",
        "status": "PASS"
      }
    ],
    "phase": "BUILD",
    "timestamp": "2026-01-18T10:35:00Z"
  },
  "history": [
    {
      "version": 1,
      "phase": "OBSERVE",
      "criteria": [...],
      "timestamp": "2026-01-18T10:30:00Z"
    },
    {
      "version": 2,
      "phase": "THINK",
      "updates": ["Added ISC-C3 for token expiry"],
      "timestamp": "2026-01-18T10:32:00Z"
    }
  ],
  "satisfaction": {
    "satisfied": 8,
    "partial": 0,
    "failed": 0,
    "total": 8
  }
}
```

**Why JSON over JSONL:** ISC is bounded versioned state (<10KB), not an unbounded log. JSON with `current` + `history` explicitly models what verification tools need (current criteria) vs debugging needs (history).

#### items/ Subdirectory

Each work item represents a discrete task or output:

```
items/
├── 20260118-101530_initial-requirements.json
├── 20260118-102000_database-schema.json
├── 20260118-102230_endpoint-implementation.json
└── 20260118-102500_verification-report.json
```

**Item JSON Format:**
```json
{
  "timestamp": "2026-01-18T10:23:00Z",
  "description": "Database schema for password reset tokens",
  "type": "artifact",  # artifact, verification, discovery
  "status": "COMPLETE",
  "outputSummary": "Created reset_tokens table with 24h expiry",
  "toolsCalled": ["Bash", "Edit"],
  "relatedISCCriteria": ["ISC-C2", "ISC-C3"],
  "verificationStatus": {
    "criteria_checked": 2,
    "criteria_passed": 2,
    "criteria_failed": 0
  }
}
```

#### verification/ Subdirectory

Stores evidence of verification:

```
verification/
├── ISC-C1_endpoint-response.json
├── ISC-C2_email-content.txt
├── ISC-A1_log-audit.txt
└── verification-summary.md
```

### LEARNING/ - Categorized Learnings

**What populates it:**
- `RatingCapture.hook.ts` (explicit ratings + implicit sentiment + low-rating learnings)
- `WorkCompletionLearning.hook.ts` (significant work session completions)
- `SessionHarvester.ts` (periodic extraction from projects/ transcripts)
- `LearningPatternSynthesis.ts` (aggregates ratings into pattern reports)

**Structure:**
- `LEARNING/SYSTEM/YYYY-MM/` - PAI/tooling learnings (infrastructure issues)
- `LEARNING/ALGORITHM/YYYY-MM/` - Task execution learnings (approach errors)
- `LEARNING/SYNTHESIS/YYYY-MM/` - Aggregated pattern analysis (weekly/monthly reports)
- `LEARNING/REFLECTIONS/algorithm-reflections.jsonl` - Algorithm performance reflections
- `LEARNING/SIGNALS/ratings.jsonl` - User satisfaction ratings

#### LEARNING/SIGNALS/ratings.jsonl

One JSON object per line:

```jsonl
{"timestamp": "2026-01-18T10:47:00Z", "work_id": "20260118-auth-reset", "rating": 8, "sentiment": "positive", "confidence": 0.92}
{"timestamp": "2026-01-18T11:03:00Z", "work_id": "20260118-config-bug", "rating": 3, "sentiment": "negative", "confidence": 0.87, "reason": "took too long, final result wrong"}
```

#### LEARNING/FAILURES/ - Full Context for Low Ratings

**What populates it:** RatingCapture.hook.ts via FailureCapture.ts (for ratings 1-3)

**Format:** `FAILURES/YYYY-MM/{timestamp}_{8-word-description}/`

**Each failure directory contains:**

```
{timestamp}_failure-description/
├── CONTEXT.md          # Human-readable analysis with metadata and root cause
├── transcript.jsonl    # Full raw conversation up to the failure point
├── sentiment.json      # Sentiment analysis output
└── tool-calls.json     # Extracted tool calls with inputs/outputs
```

**CONTEXT.md Example:**
```markdown
# Failure Analysis

**Work ID:** 20260118-config-bug
**Rating:** 2 (negative)
**Timestamp:** 2026-01-18 11:03 PST

## Summary
User was frustrated with configuration validation that reported errors it shouldn't have.

## Root Cause Analysis
- Algorithm didn't adequately pressure-test the validation logic in THINK phase
- Missed edge case with empty config sections
- Verification steps didn't catch the issue until user ran in production

## What Went Wrong
1. OBSERVE: Failed to identify edge case with optional config sections
2. THINK: Pressure test didn't cover all config formats
3. VERIFY: Test coverage incomplete

## Recommended Improvements
- Add config format examples to reverse engineering step
- Create test matrix for all config variants in PLAN phase
- Extend verification to cover real-world config samples
```

#### LEARNING/SYNTHESIS/ - Weekly Pattern Reports

```
SYNTHESIS/2026-02/
├── 2026-02-27-weekly-patterns.md
└── 2026-02-13-weekly-patterns.md
```

**Format:**
```markdown
# Weekly Learning Synthesis
**Period:** Feb 27, 2026 - Mar 4, 2026

## Ratings Summary
- Total sessions: 12
- Average rating: 7.2/10
- Low ratings (1-3): 2 sessions
- High ratings (8-10): 8 sessions

## Common Failure Patterns
1. Incomplete ISC definition (3 instances)
   - Impact: Criteria drift during execution
   - Recommendation: Extend OBSERVE pressure testing

2. Missing verification methods (2 instances)
   - Impact: Can't definitively verify criteria
   - Recommendation: Define verification in THINK, not VERIFY phase

## Execution Speed Trends
- Standard efforts: avg 1:45 (budget 2:00) ✓
- Extended efforts: avg 6:30 (budget 8:00) ✓
- Fast efforts: avg 0:55 (budget 1:00) ✓

## Improvement Opportunities
1. OBSERVE phase optimization - currently taking 20% longer than budgeted
2. Pattern-based ISC templates for common task types
3. Capability audit caching to speed up repeated task patterns
```

### RESEARCH/ - Agent Outputs

**What populates it:** Agent tasks write directly to this directory

**Format:** `RESEARCH/YYYY-MM/YYYY-MM-DD-HHMMSS_AGENT-type_description.md`

**Example:**
```
RESEARCH/2026-02/
├── 2026-02-27-092034_Researcher_payment-integration-architecture.md
├── 2026-02-27-105500_Architect_database-optimization-plan.md
└── 2026-02-27-113022_RedTeam_security-vulnerability-analysis.md
```

### SECURITY/ - Security Events

**What populates it:** `SecurityValidator.hook.ts` on tool validation

**Format:** `SECURITY/security-events.jsonl`

```jsonl
{"timestamp": "2026-01-18T10:30:00Z", "event": "FILE_READ", "path": "/etc/passwd", "allowed": false, "reason": "system file"}
{"timestamp": "2026-01-18T10:31:00Z", "event": "BASH_COMMAND", "command": "rm -rf /", "allowed": false, "reason": "destructive"}
{"timestamp": "2026-01-18T10:32:00Z", "event": "API_CALL", "endpoint": "https://api.example.com", "allowed": true}
```

### STATE/ - Fast Runtime Data

**What populates it:** Various tools and hooks

**Content:** High-frequency read/write JSON files for runtime state

**Key Property:** Ephemeral - can be rebuilt from other sources. Optimized for speed, not permanence.

**Key contents:**
- `algorithms/` - Per-session algorithm state files
- `kitty-sessions/` - Per-session Kitty terminal env
- `tab-titles/` - Per-window tab state
- `session-names.json` - Auto-generated session names
- `current-work.json` - Active work directory pointer
- `format-streak.json`, `algorithm-streak.json` - Performance metrics
- `progress/` - Multi-session project tracking
- `integrity/` - System health check results

### PAISYSTEMUPDATES/ - Change History

**What populates it:** Manual via CreateUpdate.ts tool

**Content:** Canonical tracking of all system changes

**Purpose:** Track architectural decisions and system changes over time

---

## Hook Integration

| Hook | Trigger | Writes To | Purpose |
|------|---------|-----------|---------|
| AutoWorkCreation | UserPromptSubmit | WORK/, STATE/current-work.json | Create work tracking directory |
| WorkCompletionLearning | Stop | WORK/items, LEARNING/ | Capture work completion, ISC, learnings |
| SessionSummary | SessionEnd | WORK/META.yaml, clears STATE | Mark work COMPLETED, cleanup |
| RatingCapture | UserPromptSubmit | LEARNING/SIGNALS/, LEARNING/, FAILURES/ | Capture ratings, low-rating analysis |
| SecurityValidator | PreToolUse | SECURITY/ | Audit security decisions |

---

## Data Flow Diagram

```
User Request
    ↓
Claude Code → projects/{uuid}.jsonl (native transcript, autosaved)
    ↓
AutoWorkCreation → WORK/{id}/ + STATE/current-work.json
    ↓
[Work happens - all tool calls captured in projects/ by Claude Code]
    ↓
WorkCompletionLearning → Updates WORK/items, optionally LEARNING/
    ↓
RatingCapture → LEARNING/SIGNALS/ + LEARNING/ (if warranted)
    ├→ Low ratings (1-3) also trigger FailureCapture → LEARNING/FAILURES/
    ↓
SessionSummary → WORK/META.yaml marked COMPLETED, clears STATE/current-work.json

[Periodic harvesting - runs hourly/daily]
    ↓
SessionHarvester → Scans projects/ → Writes LEARNING/
    ↓
LearningPatternSynthesis → Analyzes SIGNALS/ → Writes LEARNING/SYNTHESIS/
```

---

## Implementation for axi-assistant

### Phase 1: Work Tracking System

**Create:**
1. WORK directory structure
2. AutoWorkCreation hook (on new user prompt)
3. META.yaml schema
4. ISC.json schema
5. items/ tracking

**Components:**
- `WorkManager` class to create/update work directories
- `ISCTracker` to persist ISC.json with version history
- Hook system integration

### Phase 2: Learning Capture

**Create:**
1. LEARNING directory structure
2. RatingCapture hook (on user ratings)
3. FailureCapture for low ratings (1-3)
4. SessionHarvester tool
5. LearningPatternSynthesis tool

**Components:**
- `LearningCapture` class for SIGNALS/ratings.jsonl
- `FailureAnalyzer` for FAILURES/ directories
- `HarvestingScheduler` for periodic extraction

### Phase 3: State Management

**Create:**
1. STATE directory for ephemeral data
2. current-work.json tracking
3. Algorithm state persistence
4. Session performance metrics

**Components:**
- `StateManager` for fast read/write JSON operations
- Cleanup routines for ephemeral data
- Recovery from incomplete sessions

### Integration Points with Algorithm

1. **OBSERVE Phase:** AutoWorkCreation creates work dir, initializes ISC.json
2. **THINK Phase:** Updates ISC.json with mutations
3. **PLAN Phase:** Stores execution strategy in WORK/META.yaml
4. **BUILD Phase:** Each artifact goes to WORK/items/ or WORK/scratch/
5. **EXECUTE Phase:** Continuous verification results stored in WORK/verification/
6. **VERIFY Phase:** Final verification artifacts to WORK/verification/, ISC.json marked PASS/FAIL
7. **LEARN Phase:** Rating captured, reflection stored, learnings appended

---

## Comparison with Current axi Memory

| Aspect | Current axi | Enhanced with Memory System |
|--------|------------|---------------------------|
| Session Data | Lost between sessions | Persistent across sessions in WORK/ |
| Success Criteria | Implicit | Explicit ISC with versioning |
| Learning | Ad-hoc notes | Structured LEARNING/SIGNALS and FAILURES/ |
| Work Continuity | Manual context passing | ISC-aware resumption from WORK/META.yaml |
| Verification Evidence | Not saved | Stored in WORK/verification/ |
| Performance Metrics | None | Tracked in STATE/ and SIGNALS/ |
| Failure Analysis | Not captured | Full context in LEARNING/FAILURES/ |
| Pattern Recognition | Manual | Automated via LearningPatternSynthesis |

---

## Quick Reference Commands

```bash
# Current work
cat ~/.claude/MEMORY/STATE/current-work.json

# Recent work directories
ls -lt ~/.claude/MEMORY/WORK/ | head -5

# Recent ratings
tail ~/.claude/MEMORY/LEARNING/SIGNALS/ratings.jsonl

# View session transcripts
# Replace {username} with your system username
ls -lt ~/.claude/projects/-Users-{username}--claude/*.jsonl | head -5

# Check learnings
ls ~/.claude/MEMORY/LEARNING/SYSTEM/
ls ~/.claude/MEMORY/LEARNING/ALGORITHM/
ls ~/.claude/MEMORY/LEARNING/SYNTHESIS/

# Check failures
# List recent failure captures
ls -lt ~/.claude/MEMORY/LEARNING/FAILURES/$(date +%Y-%m)/ 2>/dev/null | head -10

# View a specific failure
cat ~/.claude/MEMORY/LEARNING/FAILURES/2026-02/*/CONTEXT.md | head -100

# Check multi-session progress
ls ~/.claude/MEMORY/STATE/progress/

# Run harvesting tools
# Harvest learnings from recent sessions
bun run ~/.claude/skills/PAI/Tools/SessionHarvester.ts --recent 10

# Generate pattern synthesis
bun run ~/.claude/skills/PAI/Tools/LearningPatternSynthesis.ts --week
```

---

## Critical Design Decisions

1. **Use Claude Code's projects/ as source of truth** - Don't duplicate session data
2. **Hook-driven event capture** - Don't poll or manually trigger captures
3. **Ephemeral STATE directory** - Keep fast operations separate from durable learning
4. **ISC versioning in JSON** - Track all mutations for debugging and learning
5. **Full failure context in FAILURES/** - Enable ML-powered root cause analysis
6. **JSONL for streams, JSON for bounded state** - Choose format based on data characteristics

---

## Resources

- **PAI Memory System:** /tmp/pai-fabric-research/Personal_AI_Infrastructure/Releases/v3.0/.claude/MEMORY/
- **Memory System Documentation:** MEMORYSYSTEM.md
- **Hook System Documentation:** THEHOOKSYSTEM.md

---

**Document Complete**
**Recommendation:** Implement WORK and LEARNING directories first (foundation), then add STATE for performance tracking, finally integrate Learning harvesting tools
