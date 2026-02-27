# D. Hooks & Event System

**Document Version:** 1.0
**Source:** PAI v3.0 Hook System
**Research Date:** February 27, 2026

---

## Overview

PAI's hooks system is an event-driven architecture that automatically captures domain-specific events during execution. Hooks act as passive observers that trigger when specific conditions occur, writing captured data to appropriate memory directories without explicit prompting.

**Key Concept:** Let events flow naturally; hooks intercept and capture them automatically.

**Note:** While hooks are less directly applicable to axi-assistant (which doesn't manage its own lifecycle), the event-driven pattern and learning capture mechanisms are highly valuable.

---

## Core Hooks in PAI

### 1. AutoWorkCreation Hook
**Trigger:** UserPromptSubmit event (when user sends a new prompt)

**Purpose:** Automatically create a work tracking directory for the incoming request

**Actions:**
- Create `WORK/{work_id}/` directory with subdirectories (items/, agents/, scratch/, verification/)
- Generate `META.yaml` with initial metadata
- Create `ISC.json` (empty, populated during OBSERVE)
- Update `STATE/current-work.json` with active work pointer
- Initialize work start timestamp

**Example Trigger:**
```
User sends prompt: "Build authentication system for my app"
    ↓
AutoWorkCreation fires
    ↓
Creates: WORK/20260227-auth-system/
         WORK/20260227-auth-system/META.yaml
         WORK/20260227-auth-system/ISC.json
         WORK/20260227-auth-system/items/
         WORK/20260227-auth-system/agents/
         WORK/20260227-auth-system/scratch/
         WORK/20260227-auth-system/verification/
```

### 2. WorkCompletionLearning Hook
**Trigger:** Stop event (when work phase completes or user says "stop")

**Purpose:** Capture learnings and completion state

**Actions:**
- Update `WORK/{id}/items/` with response summary
- Append to `WORK/{id}/ISC.json` any mutations discovered during execution
- Extract learnings to `LEARNING/ALGORITHM/YYYY-MM/` if significant work completed
- Record execution time vs budget
- Identify new patterns

**Example:**
```
Work completes: Authentication system finished
    ↓
WorkCompletionLearning fires
    ↓
Updates: WORK/20260227-auth-system/items/20260227-182500_completion.json
         WORK/20260227-auth-system/ISC.json (final satisfaction)
         LEARNING/ALGORITHM/2026-02/auth-system-learning.md
         STATE/algorithm-streak.json (performance tracking)
```

### 3. SessionSummary Hook
**Trigger:** SessionEnd event (when entire session closes)

**Purpose:** Finalize work state and clean up ephemeral state

**Actions:**
- Mark all active work items as COMPLETED in `META.yaml`
- Set final status (COMPLETED, FAILED, BLOCKED)
- Archive session data (if applicable)
- Clear `STATE/current-work.json`
- Clear session-specific STATE files
- Trigger backup of WORK/ if configured

**Example:**
```
Session ends (user closes Claude Code)
    ↓
SessionSummary fires
    ↓
Updates: WORK/20260227-auth-system/META.yaml (status: COMPLETED)
Clears: STATE/current-work.json
         STATE/algorithms/{sessionId}.json
         STATE/kitty-sessions/{sessionId}.json
```

### 4. RatingCapture Hook
**Trigger:** UserRating event (when user rates their satisfaction 1-10)

**Purpose:** Capture satisfaction feedback and trigger learning for low ratings

**Actions:**
- Append `LEARNING/SIGNALS/ratings.jsonl` with rating metadata
- If rating 1-3 (low satisfaction):
  - Trigger FailureCapture for full context dump
  - Append learning analysis to `LEARNING/FAILURES/`
  - Flag for pattern synthesis
- If rating 4-7 (neutral):
  - Optional learning capture if noteworthy
- If rating 8-10 (positive):
  - No automatic capture (positive outcomes don't need analysis)

**Example:**
```
User rates work: 3/10 (disappointed)
    ↓
RatingCapture fires
    ↓
Appends: LEARNING/SIGNALS/ratings.jsonl
Triggers: FailureCapture → LEARNING/FAILURES/2026-02/20260227-182500_auth-bug-prevented-login/
Includes: CONTEXT.md, transcript.jsonl, sentiment.json, tool-calls.json
```

### 5. SecurityValidator Hook
**Trigger:** PreToolUse event (before any tool/skill invocation)

**Purpose:** Audit security decisions and maintain security event log

**Actions:**
- Validate tool request against security policy
- Allow/block decision
- Log decision with reason to `SECURITY/security-events.jsonl`
- Generate alert if suspicious pattern detected

**Example:**
```
Agent attempts: bash rm -rf /
    ↓
SecurityValidator fires
    ↓
Decision: BLOCK (destructive command)
Logs: SECURITY/security-events.jsonl
Reason: "Destructive command matches block pattern"
Alert: User notified of attempted destructive action
```

---

## Hook Data Structures

### Hook Event Format

All hooks receive events in this format:

```typescript
interface HookEvent {
  event_type: "UserPromptSubmit" | "Stop" | "SessionEnd" | "UserRating" | "PreToolUse"
  timestamp: string  // ISO 8601
  session_id: string
  work_id?: string  // Present for work-related events

  // Event-specific payload
  payload: {
    // UserPromptSubmit
    user_prompt?: string
    effort_level?: string

    // Stop
    work_summary?: string
    new_criteria_discovered?: boolean

    // SessionEnd
    total_work_units?: number
    total_time_seconds?: number

    // UserRating
    rating?: number  // 1-10
    rating_comment?: string

    // PreToolUse
    tool_name?: string
    tool_args?: Record<string, unknown>
  }
}
```

### Hook Response Format

Hooks return success/failure:

```typescript
interface HookResponse {
  success: boolean
  hook_name: string
  action: string
  created_files?: string[]
  error?: string
  timestamp: string
}
```

---

## Event-Driven Architecture

### Event Flow

```
User Action
    ↓
Event Generated
    ├→ Hook 1 (triggered if conditions match)
    ├→ Hook 2 (triggered if conditions match)
    ├→ Hook 3 (triggered if conditions match)
    └→ (All run in parallel, don't block)
    ↓
Continue Execution
(Hooks write asynchronously to disk)
```

**Critical:** Hooks are non-blocking. If a hook fails, execution continues. User is notified of hook failures but not blocked.

### Hook Implementation Pattern

```typescript
// Pseudo-code for hook implementation
class AutoWorkCreationHook implements Hook {
  name = "AutoWorkCreation"
  triggers = ["UserPromptSubmit"]

  async handle(event: HookEvent): Promise<HookResponse> {
    if (event.event_type !== "UserPromptSubmit") {
      return { success: false, ... }
    }

    const workId = generateWorkId()
    const workDir = `${MEMORY_ROOT}/WORK/${workId}`

    // Create directory structure
    await createDirectories([
      `${workDir}`,
      `${workDir}/items`,
      `${workDir}/agents`,
      `${workDir}/scratch`,
      `${workDir}/verification`
    ])

    // Create META.yaml
    await writeYAML(`${workDir}/META.yaml`, {
      work_id: workId,
      created_at: new Date().toISOString(),
      status: "CREATED",
      title: "Extracting from prompt...",
      session_id: event.session_id,
      ...
    })

    // Create ISC.json
    await writeJSON(`${workDir}/ISC.json`, {
      workId: workId,
      current: { criteria: [], antiCriteria: [], phase: "OBSERVE" },
      history: [],
      satisfaction: { satisfied: 0, partial: 0, failed: 0, total: 0 }
    })

    // Update current-work
    await writeJSON(`${STATE_ROOT}/current-work.json`, {
      current_work_id: workId,
      timestamp: new Date().toISOString()
    })

    return {
      success: true,
      hook_name: "AutoWorkCreation",
      action: "Created work directory",
      created_files: [
        `${workDir}/META.yaml`,
        `${workDir}/ISC.json`,
        ...
      ]
    }
  }
}
```

---

## Integration with axi-assistant

### Modified Hook System for axi

Since axi doesn't manage its own lifecycle, use event emitter pattern with hooks on:

1. **UserPromptSubmit** → Create work directory, initialize ISC
2. **TaskCreate** → Add criterion to ISC.json
3. **TaskUpdate** → Update criterion status, track mutations
4. **PhaseTransition** (OBSERVE→THINK, etc.) → Update STATE, capture ISC snapshot
5. **WorkCompletion** → Capture learnings, update META
6. **UserRating** → Capture satisfaction, trigger failure analysis for low ratings

### Implementation Approach

```typescript
// Event system for axi
class AIAssistantEventBus {
  private hooks: Map<string, Hook[]> = new Map()

  // Register a hook
  onEvent(event: string, hook: Hook) {
    if (!this.hooks.has(event)) {
      this.hooks.set(event, [])
    }
    this.hooks.get(event)!.push(hook)
  }

  // Emit event
  async emit(event: HookEvent) {
    const hooks = this.hooks.get(event.event_type) || []

    // Fire all hooks in parallel (non-blocking)
    Promise.all(hooks.map(h => h.handle(event).catch(err => {
      console.error(`Hook ${h.name} failed:`, err)
      // Continue execution
    })))
  }
}

// Usage
const eventBus = new AIAssistantEventBus()
eventBus.onEvent("UserPromptSubmit", new AutoWorkCreationHook())
eventBus.onEvent("UserPromptSubmit", new RatingCaptureHook())
eventBus.onEvent("WorkCompletion", new SessionHarvesterHook())
eventBus.onEvent("UserRating", new FailureCaptureHook())
```

---

## Learning Capture Hooks

### RatingCapture Hook Implementation

**Purpose:** Capture user satisfaction and trigger learning for low ratings

```typescript
async handle(event: HookEvent): Promise<HookResponse> {
  const { rating, rating_comment } = event.payload

  // Append to ratings.jsonl
  const ratingEntry = {
    timestamp: event.timestamp,
    work_id: event.work_id,
    rating: rating,
    sentiment: inferSentiment(rating_comment),
    confidence: calculateConfidence(rating_comment),
    comment: rating_comment
  }
  await appendJSONL(
    `${LEARNING_ROOT}/SIGNALS/ratings.jsonl`,
    ratingEntry
  )

  // If low rating, trigger failure analysis
  if (rating <= 3) {
    const failureId = generateFailureId()
    const failureDir = `${LEARNING_ROOT}/FAILURES/YYYY-MM/${failureId}`

    await createDirectories([failureDir])

    // Get full session transcript
    const transcript = await readSessionTranscript(event.session_id)
    await writeFile(`${failureDir}/transcript.jsonl`, transcript)

    // Analyze failure
    const context = await analyzeFull Failure(event.work_id, transcript)
    await writeFile(`${failureDir}/CONTEXT.md`, context)

    // Extract sentiment
    const sentiment = await analyzeSentiment(rating_comment, transcript)
    await writeJSON(`${failureDir}/sentiment.json`, sentiment)

    // Extract tool calls
    const toolCalls = await extractToolCalls(transcript)
    await writeJSON(`${failureDir}/tool-calls.json`, toolCalls)
  }

  return { success: true, ... }
}
```

---

## Applicability to axi

### High Priority Hooks (Implement First)

1. **AutoWorkCreation** - Create work tracking on user request
2. **RatingCapture** - Capture satisfaction feedback
3. **FailureCapture** - Deep analysis of low-rating sessions
4. **PhaseTransition** - Track algorithm phase changes

### Medium Priority Hooks

5. **WorkCompletionLearning** - Extract learnings from completed work
6. **CapabilityAudit** - Log which capabilities were considered/selected
7. **VerificationEvent** - Log verification results

### Lower Priority Hooks

8. **SecurityValidator** - Audit security decisions (less critical for axi)
9. **AgentOutputCapture** - Archive agent/sub-task results
10. **SessionAnalysis** - Periodic session reviews

---

## Hook Reliability

### Error Handling

Hooks should never block main execution:

```typescript
try {
  await hook.handle(event)
} catch (error) {
  logger.error(`Hook ${hook.name} failed:`, error)
  // Notify user but continue
  notifyUser(`Learning capture failed: ${error.message}`, "warning")
}
```

### Retry Strategy

For failed hook operations:

```typescript
async function executeWithRetry(fn: () => Promise<T>, maxRetries = 3) {
  for (let i = 0; i < maxRetries; i++) {
    try {
      return await fn()
    } catch (error) {
      if (i === maxRetries - 1) throw error
      await delay(1000 * (i + 1))  // Exponential backoff
    }
  }
}
```

---

## Hook Configuration

### Hook Registry

```yaml
# ~/.claude/MEMORY/hooks/config.yaml
hooks:
  - name: AutoWorkCreation
    enabled: true
    triggers: [UserPromptSubmit]
    priority: 100  # Run first

  - name: RatingCapture
    enabled: true
    triggers: [UserRating]
    priority: 90

  - name: FailureCapture
    enabled: true
    triggers: [UserRating]
    condition: "rating <= 3"
    priority: 85

  - name: SessionHarvester
    enabled: true
    triggers: [SessionEnd]
    priority: 50
    schedule: "hourly"  # Also run periodically

  - name: LearningPatternSynthesis
    enabled: true
    triggers: [SessionEnd]
    schedule: "daily 2:00 AM"
    priority: 40
```

### Enable/Disable Hooks

Users can control which hooks are active:

```bash
# List all hooks
axi hooks list

# Enable/disable
axi hooks enable RatingCapture
axi hooks disable SecurityValidator

# View hook logs
axi hooks logs --tail 50
axi hooks logs RatingCapture --since 1h
```

---

## Monitoring Hooks

### Hook Health Dashboard

```
Hook Status:
  AutoWorkCreation: ✅ 145 triggers, 145 successes, 0 failures
  RatingCapture: ✅ 23 triggers, 23 successes, 0 failures
  FailureCapture: ⚠️ 5 triggers, 4 successes, 1 failure
  WorkCompletionLearning: ✅ 23 triggers, 23 successes, 0 failures
  SessionHarvester: ✅ 7 triggers, 7 successes, 0 failures
  LearningPatternSynthesis: ✅ 7 triggers, 7 successes, 0 failures

Last Hook Failure:
  Hook: FailureCapture
  Time: 2026-02-27 10:45:00 UTC
  Error: "Transcript file not found for session xyz"
  Status: Auto-retry at next session end
```

### Hook Metrics

Track over time:

```json
{
  "hook_name": "RatingCapture",
  "period": "2026-02-01 to 2026-02-28",
  "trigger_count": 23,
  "success_rate": 1.0,
  "average_execution_time_ms": 45,
  "captured_ratings_count": 23,
  "low_ratings_detected": 3,
  "failure_captures_created": 3
}
```

---

## Key Insights for axi

1. **Event-driven learning** - Don't require users to manually save learnings; capture automatically
2. **Non-blocking design** - Hooks never interrupt main execution
3. **Granular event types** - More event types = more learning opportunities
4. **Async capture** - Write learning asynchronously while user continues
5. **Failure analysis** - Low ratings trigger detailed context preservation
6. **Pattern synthesis** - Periodic aggregation reveals trends humans miss

---

## Resources

- **PAI Hooks Documentation:** /tmp/pai-fabric-research/Personal_AI_Infrastructure/Releases/v3.0/.claude/skills/PAI/THEHOOKSYSTEM.md
- **Event-Driven Architecture Patterns:** Enterprise Integration Patterns (Gregor Hohpe, Bobby Woolf)

---

**Document Complete**
**Recommendation:** Start with AutoWorkCreation and RatingCapture hooks (highest ROI), then add FailureCapture for low ratings, finally implement harvesting hooks for pattern synthesis
