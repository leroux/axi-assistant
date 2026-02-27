# PAI (Personal AI Infrastructure) - Comprehensive Technical Analysis

**Investigation Date:** February 27, 2026
**Focus:** Architecture, components, and integration patterns for axi-assistant
**Status:** Complete Research Phase

---

## Executive Summary

Personal AI Infrastructure (PAI) is a sophisticated Claude Code-native platform designed to enable continuous learning and goal-oriented AI assistance. Unlike traditional agentic systems focused on task execution, PAI is fundamentally user-centric: it captures who you are, what you're working toward, and learns from every interaction.

**Core Differentiators from Generic Agent Frameworks:**
1. **User-First Architecture** - Identity, preferences, and goals come before tools
2. **Continuous Learning System** - Every interaction generates signals (ratings, sentiment, outcomes)
3. **Modular but Unified** - Skills, memory, hooks, and security form a coherent ecosystem
4. **Self-Improving** - System modifies itself based on captured insights
5. **Persistent Memory** - Three-tier memory architecture (hot/warm/cold) with phase-based learning

---

## Section 1: The 7-Phase Foundational Algorithm

### 1.1 The Algorithm Foundation

PAI's core operation is built on the **Foundational Algorithm** - a universal problem-solving loop adapted from the scientific method:

```
OBSERVE → THINK → PLAN → BUILD → EXECUTE → VERIFY → LEARN
```

**Where it's used:**
- PAI's outer loop for task execution
- Hook system design
- Skill workflows
- Memory system organization
- Security validation patterns

### 1.2 Complete Phase Breakdown

#### Phase 1: OBSERVE
**Purpose:** Gather all relevant context and information

**In PAI Context:**
- Load user's TELOS (goals, beliefs, preferences)
- Capture current problem statement
- Scan available context from memory/LEARNING
- Identify relevant skills and previous learnings
- Check security policies

**Concrete Example (Research Task):**
```
User says: "I need to understand Vue 3 patterns"
OBSERVE collects:
- User's tech stack preferences (from TELOS)
- Previous Vue learning sessions (MEMORY/LEARNING)
- Relevant research skills available
- User's communication style (RESPONSEFORMAT)
- Security constraints
```

**Data Sources:**
- `TELOS/` - Mission, goals, beliefs, strategies
- `MEMORY/LEARNING/SIGNALS/ratings.jsonl` - Historical success patterns
- `USER/RESPONSEFORMAT.md` - Communication preferences
- `PAISECURITYSYSTEM/patterns.yaml` - Allowed tools/paths
- `skills/*/SKILL.md` - Capability inventory

---

#### Phase 2: THINK
**Purpose:** Analyze the problem deeply before committing to action

**In PAI Context:**
- Use thinking tools (CouncilOfAdvisors, RedTeam, FirstPrinciples)
- Compare against ideal state criteria (ISC)
- Identify constraints and dependencies
- Generate multiple approaches
- Flag uncertainty or insufficient knowledge

**Concrete Example (Research Task cont.):**
```
THINK phase for Vue 3 research:
- Council recommends focusing on composition API (vs Options API)
- RedTeam identifies: "Vue 3 adoption rate, breaking changes, learning curve"
- FirstPrinciples: "Why is this pattern better? What fundamental changed?"
- ISC Check: What would successful Vue 3 understanding look like?
  * Can explain composition vs options API
  * Understand reactivity system
  * Know migration path from Vue 2
```

**Tools Used:**
- `Council` - Multiple expert perspectives
- `RedTeam` - Adversarial challenge
- `FirstPrinciples` - Deep reasoning
- ISC Tracking - Ideal state criteria

---

#### Phase 3: PLAN
**Purpose:** Create structured approach before execution

**In PAI Context:**
- Define Ideal State Criteria (ISC)
- Break complex work into steps
- Route to appropriate skills
- Identify information sources needed
- Set verification gates

**Concrete Example (Research Task cont.):**
```
PLAN for Vue 3 research:
1. Verify official Vue 3 docs are most current source
2. Understand composition API (step 1)
3. Understand reactivity system (step 2)
4. Compare old/new patterns (step 3)
5. Create personal reference guide (step 4)

ISC for this work:
- Official sources consulted
- Explanations tested with code examples
- Previous Vue experience integrated
- Output organized for future reference
```

**Outputs:**
- Execution plan with sequence
- Ideal State Criteria document
- Resource inventory
- Skill routing decisions

---

#### Phase 4: BUILD
**Purpose:** Create necessary tools, templates, or scaffolding

**In PAI Context:**
- Generate scripts or templates
- Create memory artifacts
- Set up workspace
- Prepare retrieval systems
- Initialize tracking

**Concrete Example (Research Task cont.):**
```
BUILD phase for Vue 3 research:
- Create ~/RESEARCH/Vue3/ directory structure
- Generate learning template with sections for:
  * Concepts
  * Code examples
  * Comparisons to Vue 2
  * Use case patterns
- Set up @vue/composition-api test project
- Create MEMORY/LEARNING artifact for storage
```

**Artifacts Created:**
- CLI tools or helper scripts
- Directory structures
- Templates and boilerplates
- Memory initialization
- Test harnesses

---

#### Phase 5: EXECUTE
**Purpose:** Perform the actual work

**In PAI Context:**
- Run planned steps in sequence
- Execute skills with routing logic
- Collect outputs
- Handle errors gracefully
- Maintain state

**Concrete Example (Research Task cont.):**
```
EXECUTE phase for Vue 3 research:
1. WebFetch official Vue 3 docs
2. Extract key concepts via extract_wisdom pattern
3. Create code examples via create_coding_feature
4. Compare patterns with analysis_code
5. Assemble into learning doc
6. Store in MEMORY/LEARNING
```

**Pattern:**
- Sequential skill execution
- Error handling and retries
- State maintenance in current-work.json
- Intermediate output capture

---

#### Phase 6: VERIFY
**Purpose:** Confirm output meets Ideal State Criteria

**In PAI Context:**
- Test against ISC
- Run quality checks
- Verify completeness
- Check correctness
- Get explicit user rating

**Concrete Example (Research Task cont.):**
```
VERIFY phase for Vue 3 research:
- Does output explain composition API clearly? ✓
- Are code examples runnable? ✓
- Is migration path documented? ✓
- Are comparisons accurate? ✓
- User rating: 8/10 - "Great depth, needs more examples"
```

**Verification Methods:**
- ISC checklist completion
- User rating (explicit 1-10 scale)
- Output inspection
- Test execution
- Expert review (if routed to specialist agent)

---

#### Phase 7: LEARN
**Purpose:** Extract insights and improve future performance

**In PAI Context:**
- Analyze user rating and sentiment
- Extract what worked/failed
- Update learnings artifacts
- Adjust future approach
- Feed back into skill routing

**Concrete Example (Research Task cont.):**
```
LEARN phase for Vue 3 research:
User gave 8/10 - what's the gap?
- Missing: Practical transition guide from Vue 2
- Working well: Deep concept explanations
- Pattern identified: Users prefer code examples early

Update MEMORY/LEARNING:
- Document: "Code examples essential for Vue topics"
- Insight: "Composition API adoption requires showing benefit"
- Next time: Lead with practical examples, then theory
```

**Outputs to MEMORY:**
- `LEARNING/INSIGHTS/` - What worked
- `SIGNALS/ratings.jsonl` - User feedback
- `LEARNING/PATTERNS/` - Identified patterns
- `WORK/[date]/[session]/` - Session summary

---

### 1.3 Phase Application in axi-assistant

**Where This Could Apply in axi:**

| Current axi Pattern | Maps to Phase | Enhancement Opportunity |
|---|---|---|
| Agent planning | THINK + PLAN | Add ISC tracking, multi-expert review |
| Tool execution | EXECUTE | Implement phase-based tool routing |
| Result verification | VERIFY | Explicit criteria + user ratings |
| No learning system | LEARN | Add persistent insights + pattern detection |

**Key Insight:** PAI's phases aren't sequential dogma—they're adaptive. A quick task might compress phases; complex work might loop through multiple times.

---

## Section 2: Memory System Architecture

### 2.1 Three-Tier Memory Structure

PAI implements a sophisticated memory architecture with three temperature zones:

```
┌─────────────────────────────────────────────────┐
│              MEMORY/ (Complete Archive)         │
├─────────────────────────────────────────────────┤
│                                                 │
│  HOT: Current session + last 7 days             │
│  ├── WORK/[today]/[session]/                   │
│  ├── current-work.json (active state)          │
│  └── STATE/ (transient)                        │
│                                                 │
│  WARM: Learning signals + recent insights      │
│  ├── LEARNING/INSIGHTS/ (recent patterns)      │
│  ├── LEARNING/SIGNALS/ratings.jsonl            │
│  ├── LEARNING/PATTERNS/ (by domain)            │
│  └── SECURITY/security-events.jsonl            │
│                                                 │
│  COLD: Historical archive + reference          │
│  ├── WORK/[older-dates]/[session]/             │
│  ├── LEARNING/ARCHIVE/                         │
│  └── Legacy context                            │
│                                                 │
└─────────────────────────────────────────────────┘
```

### 2.2 Physical Structure (Directory Layout)

```
~/.claude/MEMORY/
├── WORK/                           # Session work artifacts
│   ├── 2026-02-27/                # Date directories
│   │   ├── session-abc-123/       # Per-session folder
│   │   │   ├── transcript.jsonl   # Full session transcript
│   │   │   ├── summary.md         # User-friendly summary
│   │   │   ├── artifacts/         # Generated files
│   │   │   │   ├── research.md
│   │   │   │   ├── analysis.json
│   │   │   │   └── code-samples/
│   │   │   └── completion.json    # Session metadata
│   │   └── session-def-456/
│   └── 2026-02-26/
│
├── LEARNING/                       # Insights + patterns
│   ├── SIGNALS/
│   │   ├── ratings.jsonl          # All user ratings over time
│   │   └── sentiment.jsonl        # Implicit mood detection
│   │
│   ├── INSIGHTS/                  # Domain learnings
│   │   ├── [domain]-insights.md
│   │   └── effectiveness.md       # What's working
│   │
│   ├── PATTERNS/                  # Pattern extraction
│   │   ├── user-preferences.md
│   │   ├── common-failures.md
│   │   └── skill-effectiveness.md
│   │
│   └── ARCHIVE/                   # Old learnings (reference)
│
├── STATE/                          # Active session state
│   ├── current-work.json          # What we're working on NOW
│   ├── trending-cache.json        # Recent topics
│   └── model-cache.txt            # Selected model for current task
│
├── SECURITY/                       # Security events
│   └── security-events.jsonl      # All validation checks
│
└── SESSIONS/                       # Session summaries
    ├── recent-sessions.jsonl      # Quick access
    └── work-type-index.jsonl      # Quick lookup by type
```

### 2.3 Data Formats and Schemas

#### ratings.jsonl Format
```json
{
  "timestamp": "2026-02-27T14:23:45Z",
  "session_id": "abc-123-uuid",
  "explicit_rating": 8,
  "implicit_sentiment": 0.78,
  "rating_source": "explicit",
  "context": {
    "task_type": "research",
    "tools_used": ["WebFetch", "Grep", "Read"],
    "duration_seconds": 1245,
    "output_tokens": 3200
  },
  "feedback_text": "Great depth on Vue 3, needs more examples",
  "ai_assessment": "User wanted more practical code samples"
}
```

#### Learning Insight Format
```json
{
  "id": "insight-vue3-001",
  "date_discovered": "2026-02-27",
  "insight": "Code examples essential for Vue 3 adoption topics",
  "pattern": "when_users_rate_7_or_below_on_technical_topics",
  "source": "learned_from_ratings",
  "confidence": 0.85,
  "action": "Route Vue/React topics through code_example_first pattern",
  "tested": true,
  "effectiveness": 0.92
}
```

### 2.4 Learning Capture - How It Works

**Automatic Learning System:**

```
User sends message
    │
    ├─► Hook: ExplicitRatingCapture
    │   └─► Detects "8/10", "great", "loved it"
    │       └─► Write to ratings.jsonl
    │
    ├─► Hook: ImplicitSentimentCapture
    │   └─► Analyzes sentiment in feedback
    │       └─► Infer 0-100 confidence score
    │           └─► Write to sentiment.jsonl
    │
    └─► Hook: WorkCompletionLearning (on SessionEnd)
        └─► Analyze completed work
            ├─► Extract what was successful
            ├─► Identify patterns in feedback
            └─► Update LEARNING/INSIGHTS/
```

**Pattern Detection Example:**

```
ANALYSIS OF LAST 30 RATINGS:
├── Python coding tasks: avg 8.4/10
├── JavaScript coding tasks: avg 6.2/10
├── Writing tasks: avg 7.1/10
└─► INSIGHT: "Python explanations more effective than JS"
    └─► ACTION: "Prefer Python examples, link to JS equivalents"
```

### 2.5 How Learning Feeds Back Into Performance

1. **Memory Retrieval in OBSERVE phase:**
   ```
   User: "I need to learn Vue 3"

   OBSERVE queries MEMORY:
   - Check LEARNING/PATTERNS/framework-learning.md
   - Find: "Code examples essential (0.92 effectiveness)"
   - Adjust approach: Lead with practical examples
   ```

2. **Skill Routing Adjustments:**
   ```
   LEARNING shows: When user asks about auth,
   previous extract_wisdom + code_review combo worked 9.1/10

   System now routes similar requests → [extract_wisdom, code_review]
   instead of generic analysis
   ```

3. **Response Format Tuning:**
   ```
   LEARNING shows: Users give higher ratings when
   examples come before theory (0.87 vs 0.64)

   Format updater modifies response template:
   OLD: Theory → Examples → Summary
   NEW: Examples → Theory → Variations → Summary
   ```

---

## Section 3: TELOS Framework & Interview System

### 3.1 What is TELOS?

**TELOS** (Greek: "purpose" or "ultimate aim") is PAI's identity and purpose framework. It answers:
- Who am I?
- What am I working toward?
- What do I believe in?
- What have I learned?
- What patterns guide me?

**Why It Matters for AI:**
- Enables context-aware decisions (not generic responses)
- Captures non-technical constraints (values, beliefs, ethics)
- Provides explanation for why certain outputs matter
- Powers self-improvement (AI learns what YOU care about)

### 3.2 The 10 TELOS Dimensions

| Dimension | Icon | Purpose | Examples |
|-----------|------|---------|----------|
| **MISSION** | M# | Ultimate life purpose(s) | "Activate human potential through AI" |
| **GOALS** | G# | Specific objectives | "Ship axi v2 by Q2", "Master distributed systems" |
| **CHALLENGES** | C# | Current obstacles | "Limited context windows", "Scaling to teams" |
| **STRATEGIES** | S# | Approaches to challenges | "Implement persistent memory", "Design modular skills" |
| **PROBLEMS** | P# | World problems to solve | "AI displacement", "Information overload" |
| **NARRATIVES** | N# | Key talking points | "AI should magnify, not replace humans" |
| **BELIEFS** | B# | Core convictions | "First principles > hype", "Unix philosophy" |
| **FRAMES** | FR# | Mental models | "Systems thinking", "Constraint-based design" |
| **MODELS** | MO# | How things work | "Learning model: feedback → insight → action" |
| **TRAUMAS/LESSONS** | TR#/L# | Formative experiences | "Early career lesson: architecture > technology choice" |

### 3.3 The TELOS Interview - Step by Step

The TELOS Interview is a structured conversation to help users articulate their complete identity framework.

#### Interview Structure (90-120 minutes)

**Phase 1: Foundation (15 mins)**
```
Q: "What's your one-sentence mission in life?"
Context: This question clarifies purpose before diving into specifics

Example answers:
- "Activate people's creative potential"
- "Build systems that scale"
- "Help the underserved"
→ User writes M0

Q: "What are you working on right now?"
→ User identifies top 2-3 goals (G0, G1, G2)
```

**Phase 2: Challenge Mapping (20 mins)**
```
Q: "What's holding you back from achieving those goals?"
→ User identifies C0, C1, C2, C3

Q: "For each challenge, what's your strategy?"
→ User maps S0, S1, S2, etc.

This creates a CHALLENGE → STRATEGY matrix
```

**Phase 3: World Problems (10 mins)**
```
Q: "If you had unlimited resources, what problem would you solve?"
→ Identifies passion (P0, P1, P2)

Q: "How does that connect to your mission?"
→ Shows alignment or reveals misalignment
```

**Phase 4: Communication Identity (15 mins)**
```
Q: "What do you find yourself telling people repeatedly?"
→ Captures NARRATIVES (N0, N1, N2, N3)

Q: "What are you famous for / known for?"
→ Reinforces identity
```

**Phase 5: Core Beliefs (20 mins)**
```
Q: "Name 3 beliefs that guide your decisions"
→ Captures B0, B1, B2

Q: "What do you see as the biggest misconception about [your field]?"
→ Captures differentiated thinking
```

**Phase 6: Mental Models (15 mins)**
```
Q: "How do you understand [success/human nature/systems]?"
→ Captures FR0, FR1, MO0, MO1, MO2

Q: "What model helped you succeed in the past?"
→ Operationalizes thinking
```

**Phase 7: Lessons & Traumas (10 mins)**
```
Q: "What's the hardest thing you've learned?"
→ Captures wisdom + pain points

Q: "What experience shaped who you are?"
→ Captures TR0, TR1 (formative experiences)
```

### 3.4 Sample TELOS Output for an AI Developer

```markdown
# TELOS - Alex Chen (AI Developer)

## MISSIONS
- M0: Advance agentic AI systems that understand and learn from individual users
- M1: Lower the barrier to building sophisticated AI infrastructure
- M2: Demonstrate that personal AI (not consumer AI) is the future

## GOALS (2026)
- G0: Ship axi-assistant v2 with persistent learning
- G1: Publish paper on memory system architecture for LLMs
- G2: Build and train a team of 3-4 engineers on PAI-style infrastructure

## CHALLENGES
- C0: Context window limitations force continuous memory decisions
- C1: User expectations still based on ChatGPT (stateless) not personal AI
- C2: Scalability from 1 person to small team increases complexity
- C3: Balancing open-source community with commercial considerations

## STRATEGIES
- S0: Implement three-tier memory (hot/warm/cold) for context efficiency
- S1: Create educational content showing personal AI != traditional agents
- S2: Hire for system thinking, not just coding skills
- S3: Release roadmap publicly to build alignment with community

## PROBLEMS
- P0: Most AI assistants don't learn → staying dumb forever
- P1: Enterprise AI treats humans like API consumers (not partners)
- P2: AI development focused on scale, not personalization

## NARRATIVES
- N0: "Personal AI infrastructure lets humans + AI co-evolve"
- N1: "The best AI assistant is the one that knows YOU"
- N2: "Scale without soul is just more noise"

## BELIEFS
- B0: User-centric architecture beats fancy models every time
- B1: Memory and learning are non-optional for real AI
- B2: Open source is more trustworthy than black boxes

## FRAMES
- FR0: "Every system is a constraint satisfaction problem"
- FR1: "If you can't measure it, you don't understand it"

## MODELS
- MO0: Learning loop: Observe → Verify → Learn → Improve
- MO1: Scaling follows pattern: 1 person → small team → distributed
- MO2: Value comes from understanding individual, not serving billions

## LESSONS
- L0: Architecture decisions at scale are hard to undo (vs code changes)
- L1: User trust earned through transparency, lost through one surprise
- L2: The best feature is the one that becomes invisible

## TRAUMAS
- TR0: Built system that scaled to 100s before thinking about privacy
  - Learned: Privacy = feature, not afterthought
- TR1: User abandoned tool because "it never learned what I needed"
  - Learned: Stateless AI is insufficient for human partnerships
```

### 3.5 How TELOS Is Used in Practice

**During axi Session:**

```
User: "I need a system architecture for distributed caching"

axi's THINK phase references TELOS:
├─ Mission: "Understand + learn from users"
│  → Use caching patterns user has used before
│
├─ Beliefs: "User-centric > generic"
│  → Explain architecture in user's domain language
│
├─ Challenges: "Scaling is hard"
│  → Ensure recommendations account for team growth
│
└─ Lessons: "Architecture decisions hard to change"
   → Show migration path, not just final state
```

**In Memory Learning:**

```
INSIGHT STORED:
- User's mission emphasizes individual customization
- When recommendations feel generic, ratings drop 20%
- Ratings highest when options frame is "how to customize"

NEXT TIME:
- Same user asks about auth
- System: Lead with "Here's the baseline, now let's customize for your needs"
  (vs generic "here's the best way")
```

---

## Section 4: Hook System Deep Dive

### 4.1 What Are Hooks?

**Hooks** are TypeScript scripts that execute at specific lifecycle events in Claude Code. They enable:
- Voice feedback
- Memory capture
- Security validation
- Observability
- Context injection

Think of them as "event handlers for your AI assistant's lifecycle."

### 4.2 Hook Lifecycle Events

```
SESSION STARTS
    │
    ├─ SessionStart Hooks
    │  ├─ StartupGreeting (display banner)
    │  ├─ LoadContext (inject CORE skill)
    │  └─ CheckVersion (notify updates)
    │
    ├─ User sends message
    │  │
    │  ├─ UserPromptSubmit Hooks
    │  │  ├─ FormatEnforcer (inject response spec)
    │  │  ├─ AutoWorkCreation (setup work dir)
    │  │  ├─ ExplicitRatingCapture (detect "8/10")
    │  │  ├─ ImplicitSentimentCapture (mood analysis)
    │  │  └─ UpdateTabTitle (show task name)
    │  │
    │  ├─ AI uses a tool (Bash/Edit/Write/Read)
    │  │  │
    │  │  ├─ PreToolUse Hooks
    │  │  │  ├─ SecurityValidator (allow/block/ask)
    │  │  │  └─ SetQuestionTab (visual feedback)
    │  │  │
    │  │  └─ [Tool executes]
    │  │
    │  ├─ AI finishes response
    │  │  │
    │  │  ├─ Stop Hooks (via StopOrchestrator)
    │  │  │  ├─ ResponseCapture (save output)
    │  │  │  ├─ TabTitleReset (visual reset)
    │  │  │  └─ VoiceCompletion (speak "done")
    │  │  │
    │  │  └─ [Back to user message loop]
    │
    └─ Session Ends
       │
       ├─ SessionEnd Hooks
       │  ├─ WorkCompletionLearning (extract insights)
       │  └─ SessionSummary (mark complete, archive)
       │
       └─ SESSION ENDS
```

### 4.3 Key Hooks and Their Purposes

#### SessionStart Hooks

**StartupGreeting** (Non-blocking)
```typescript
// On session start, displays:
// ╔═══════════════════════════════════════╗
// ║  Welcome back, Alex!                  ║
// ║  PAI v2.3.0                           ║
// ║                                       ║
// ║  Last session: Today 2:30 PM          ║
// ║  Learning signals: 42 insights        ║
// ║  Upcoming: Design review at 4 PM      ║
// ╚═══════════════════════════════════════╝
```

**LoadContext** (Blocking stdout)
```typescript
// Outputs CORE skill to stdout
// This gets injected into Claude's context automatically
// CORE contains: your identity, preferences, available skills
```

#### UserPromptSubmit Hooks

**FormatEnforcer** (Blocking stdout)
```typescript
// Injects response format spec
// Example:
// "RESPOND USING:
//  📋 SUMMARY: [one sentence]
//  🔍 ANALYSIS: [key findings]
//  ⚡ ACTIONS: [steps taken]
//  ✅ RESULTS: [outcomes]"
```

**ExplicitRatingCapture**
```typescript
// Watches for patterns like:
// - "8/10"
// - "great work"
// - "loved it"
// - "disappointing"
// Writes to ratings.jsonl with timestamp + context
```

**ImplicitSentimentCapture**
```typescript
// Analyzes tone if no explicit rating detected
// Example:
// User: "You've gone above and beyond, exactly what I needed!"
// → Inferred sentiment: 0.92 (92% positive)
// → Recorded as: implicit_rating_9
```

**UpdateTabTitle**
```typescript
// Sets terminal tab title to current task
// Uses inference to summarize user's request
// Updates color as task progresses:
// PURPLE (#5B21B6) = "Processing..." (when user asks)
// ORANGE (#B35A00) = "Fixing auth..." (when AI summarizes intent)
// GREEN (#16A34A) = "Done" (when complete)
//
// Also sends to Voice Server: "Fixing auth bug"
```

#### PreToolUse Hooks

**SecurityValidator**
```typescript
// Checks Bash/Edit/Write/Read commands against patterns
// Pattern matching flow:
// 1. Check command against "blocked" patterns → exit(2) hard block
// 2. Check against "confirm" patterns → ask user yes/no
// 3. Check path against "zeroAccess" → block
// 4. Log to security-events.jsonl for audit trail
//
// Example blocked:
// Command: rm -rf /
// Pattern: "rm -rf /"
// Action: Hard block, log, explain why
```

#### Stop Hooks

**StopOrchestrator** (Coordinates all Stop handlers)
```typescript
// On AI response completion:
// 1. ResponseCapture saves output to WORK/[date]/[session]/
// 2. TabTitleReset returns terminal tab to default
// 3. VoiceCompletion speaks: "Search complete. Found 3 relevant papers."
// 4. Update current-work.json with completion status
```

#### SessionEnd Hooks

**WorkCompletionLearning**
```typescript
// Analyzes completed session:
// 1. Aggregate ratings from entire session
// 2. Extract patterns from responses
// 3. Identify successful approach
// 4. Generate LEARNING/INSIGHTS/ entry
// 5. Update pattern effectiveness scores
```

### 4.4 Hook Data Flows

**Memory System Integration:**

```
HOOKS write to MEMORY:
├─ ExplicitRatingCapture → LEARNING/SIGNALS/ratings.jsonl
├─ ImplicitSentimentCapture → LEARNING/SIGNALS/sentiment.jsonl
├─ AutoWorkCreation → MEMORY/STATE/current-work.json
├─ ResponseCapture → WORK/[date]/[session]/transcript.jsonl
├─ WorkCompletionLearning → LEARNING/INSIGHTS/
└─ SecurityValidator → MEMORY/SECURITY/security-events.jsonl

HOOKS read from MEMORY:
├─ LoadContext → reads TELOS/, USER/, CORE/
├─ SecurityValidator → reads patterns.yaml
└─ UpdateTabTitle → reads recent-sessions.jsonl for context
```

### 4.5 Hook Inter-Dependencies

**Critical Coordination:**

```
Rating System:
ExplicitRatingCapture runs FIRST
├─ If explicit rating found → write and exit
└─ If not → allow ImplicitSentimentCapture to run

Work Tracking:
AutoWorkCreation (UserPromptSubmit)
    → Creates current-work.json
    → ResponseCapture (Stop) updates it
    → SessionSummary (SessionEnd) clears it

Security:
SecurityValidator (PreToolUse)
    → Blocks dangerous commands immediately
    → Asks for confirm patterns
    → Allows by default (fail-open)
    → Logs everything to audit trail
```

---

## Section 5: Skills and Patterns System

### 5.1 PAI Skills Architecture

**Skills** are modular, self-contained capabilities with consistent structure.

#### Skill Anatomy

```
skills/CreateSkill/
├── SKILL.md                    # Capability description + routing rules
├── Workflows/                  # Step-by-step execution patterns
│   ├── Start.md               # How to initiate
│   ├── Brainstorm.md          # Generate options
│   ├── Evaluate.md            # Assess quality
│   └── Finalize.md            # Polish + deliver
├── Templates/                 # Reusable templates
│   ├── skill-template.md      # For new skill creation
│   └── workflow-template.md   # For new workflow
├── Examples/                  # Sample usage
│   └── example-skill.md       # Real example
└── lib/                       # Shared helpers
    └── helpers.ts             # Skill-specific utilities
```

#### Skill Routing (SKILL.md Format)

```markdown
# CreateSkill

## ROUTING RULES
| User says | Maps to | Workflow | Model |
|-----------|---------|----------|-------|
| "create a skill" | CreateSkill | Start → Brainstorm → Evaluate → Finalize | opus |
| "I need a new workflow" | CreateSkill | Brainstorm → Evaluate | sonnet |
| "review this skill" | Review | Review.md | opus |

## IDEAL STATE CRITERIA
When complete, skill should:
- Be tested with at least one example
- Have clear documentation
- Follow PAI skill structure
- Have error handling

## EXAMPLES
User: "Create a skill for analyzing code architecture"
→ Routes to Brainstorm workflow first
→ Generates option: "Use ISC + architectural principles"
→ Evaluates against past code-analysis successes
→ Finalizes with testing

## PREREQUISITES
- Understand skill structure
- Have example use case
- Access to system tools
```

### 5.2 Fabric Patterns - Complete Architecture

**Patterns** are refined prompts organized by task/domain. Fabric has ~250 patterns.

#### Pattern Directory Structure

```
fabric/data/patterns/
├── summarize/                          # Extraction category
│   ├── system.md                      # Prompt (system message)
│   ├── user.md                        # User message template
│   └── README.md                      # Documentation
│
├── extract_wisdom/
│   ├── system.md
│   ├── user.md
│   └── README.md
│
├── analyze_code/
├── create_coding_feature/
├── code_review/
├── write_latex/
├── explain_code/
├── improve_writing/
├── analyze_claims/
└── [245 more patterns across categories]
```

#### Pattern Categories in Fabric

```
EXTRACTION (30+ patterns):
├── summarize
├── extract_wisdom
├── create_conceptmap
├── extract_interesting_parts
└── [26 more extraction patterns]

ANALYSIS (50+ patterns):
├── analyze_claims
├── analyze_code
├── analyze_debate
├── analyze_malware
├── analyze_threat_report
└── [45 more analysis patterns]

WRITING (40+ patterns):
├── write_latex
├── write_essay
├── improve_writing
├── create_social_media
└── [36 more writing patterns]

CODING (25+ patterns):
├── create_coding_feature
├── code_review
├── explain_code
├── convert_markdown_to_json
└── [21 more coding patterns]

RESEARCH (20+ patterns):
├── create_reading_recommendations
├── identify_bias_and_propaganda
├── investigate_security_report
└── [17 more research patterns]

DOMAIN-SPECIFIC (60+ patterns):
├── WELLNESS: psychological analysis, therapy guidance
├── FINANCE: analysis, investment decisions
├── SALES: objection handling, pitch improvement
├── MARKETING: campaign analysis, audience research
└── [40+ more domain patterns]
```

#### Pattern Structure (system.md Example)

```markdown
# IDENTITY and PURPOSE

You are an expert content summarizer. You take content and output
structured summaries using the format below.

# INSTRUCTIONS

1. Read the content carefully
2. Identify the 10 most important points
3. Extract the 5 best takeaways
4. Create one sentence summary

# OUTPUT FORMAT

## ONE SENTENCE SUMMARY
[20-word summary]

## MAIN POINTS
1. [Point 1 - max 16 words]
2. [Point 2 - max 16 words]
...

## TAKEAWAYS
1. [Takeaway 1]
...

# INPUT:
INPUT:
```

### 5.3 Fabric Pattern Strategies

Fabric supports "strategies" that modify prompts for better reasoning:

| Strategy | Approach | Best For |
|----------|----------|----------|
| **Chain of Thought (cot)** | Step-by-step reasoning | Complex analysis |
| **Chain of Draft (cod)** | Iterative drafting (5-word notes) | Long-form writing |
| **Tree of Thought (tot)** | Multiple reasoning paths | Decision making |
| **Atom of Thought (aot)** | Break into atomic problems | Hard technical problems |
| **Least-to-Most (ltm)** | Easy to hard sub-problems | Learning/teaching |
| **Self-Consistent** | Multiple paths, consensus | Accuracy critical |
| **Self-Refine** | Answer → Critique → Refine | Iterative improvement |
| **Reflexion** | Answer → Brief critique → Refine | Fast iteration |

### 5.4 PAI Skills vs Fabric Patterns

**Key Differences:**

| Aspect | PAI Skills | Fabric Patterns |
|--------|-----------|-----------------|
| **Purpose** | Full capability with workflow | Focused prompt for single task |
| **Structure** | Multiple workflows + templates | Single system.md prompt |
| **Statefulness** | Can track across calls | Stateless per invocation |
| **Learning** | Can be improved by memory insights | Static until manually updated |
| **Routing** | Intelligent selection based on context | Manual selection by user |
| **Integration** | Deep hooks + security | Independent invocation |

**Complementary:** A PAI Skill could use Fabric patterns internally. For example:
```
Research Skill workflow:
1. Use Fabric: extract_wisdom on source
2. Use Fabric: analyze_claims on findings
3. Use PAI learning: compare to previous research
4. Store insights in MEMORY/LEARNING
5. Improve future routing based on user ratings
```

---

## Section 6: Identity and Personality System

### 6.1 PAI Identity Model

Every PAI system has identity dimensions:

```
IDENTITY
├── Digital Assistant Name
│   └── "Meridian" (example)
├── Personality/Archetype
│   └── "Architect", "Engineer", "Researcher"
├── Communication Style
│   └── "Professional", "Casual", "Academic"
├── Voice Characteristics
│   └── ElevenLabs Voice ID (spoken output)
├── Visual Identity
│   └── Tab color, emoji, icons
└── Agent Team Composition
    └── Multiple specialized agents with different skills
```

### 6.2 Agent Archetypes (Multi-Agent System)

PAI v2.3+ supports specialized agent teams:

```
Available Agents:
├── Architect
│   └── System design, constitutional principles, strategic specs
├── Engineer
│   └── Implementation, code quality, technical execution
├── Designer
│   └── UX, visual systems, user experience
├── Artist
│   └── Creative work, design, aesthetics
├── QATester
│   └── Quality assurance, test design, edge cases
├── Pentester
│   └── Security analysis, attack simulation
├── ClaudeResearcher
│   └── Use Claude-specific tools and models
├── GeminiResearcher
│   └── Use Google Gemini
├── CodexResearcher
│   └── Use OpenAI Codex
└── Intern
    └── Learning-focused, asks clarifying questions
```

Each agent has:
- **MANDATORY STARTUP**: Load context from knowledge base
- **MANDATORY OUTPUT FORMAT**: PAI format with summary/analysis/actions/results
- **MANDATORY VOICE**: Send voice notification before response
- **Unique Permissions**: Some tools only available to certain agents
- **Different Models**: Architect uses opus, Engineer uses sonnet
- **Color Identity**: Purple for Architect, Blue for Engineer, etc.

### 6.3 Personality Implementation

Example: **Architect Agent** personality

```yaml
name: Architect
description: "Elite system design specialist with PhD-level distributed systems knowledge"
model: opus                          # Uses most capable model
color: purple                        # Visual identity
voiceId: muZKMsIDGYtIkjjiUS82       # Unique voice
permissions:
  allow:
    - Bash                          # Can run commands
    - Read(*)                       # Can read any file
    - Write(*)                      # Can create files
    - Grep(*)                       # Can search
    - WebFetch(domain:*)            # Can browse web
    - MCP                          # Can use MCP servers
    - SlashCommand                 # Can use slash commands
```

**Personality Core Identity:**
```
You are an elite system architect with:
- PhD-Level Expertise in distributed systems
- Fortune 10 Architecture Experience
- Academic Rigor (understand principles, not practices)
- Technology Cycle Wisdom (timeless vs trendy)
- Strategic Vision (bridge technical + business)

You think in PRINCIPLES and CONSTRAINTS.
You've seen patterns recur. You understand what's fundamental.

MANDATORY VOICE NOTIFICATIONS:
Send voice before every response:
curl -X POST http://localhost:8888/notify \
  -H "Content-Type: application/json" \
  -d '{"message":"[your 8-16 word summary]",
       "voice_id":"muZKMsIDGYtIkjjiUS82",
       "title":"Architect Agent"}'

MANDATORY OUTPUT FORMAT:
📋 SUMMARY: [One sentence what this is about]
🔍 ANALYSIS: [Key findings/insights]
⚡ ACTIONS: [Steps/tools used]
✅ RESULTS: [Outcomes accomplished]
📊 STATUS: [Current system state]
📁 CAPTURE: [Context worth preserving]
➡️ NEXT: [Recommended next steps]
📖 STORY: [8-point narrative breakdown]
```

---

## Section 7: Security System

### 7.1 Security Architecture

PAI's security system is **fail-open but validated**: normal workflows aren't blocked, dangerous operations are.

```
SECURITY VALIDATION

PreToolUse Hook (on every Bash/Edit/Write/Read call)
    │
    ├─► Load patterns.yaml
    │   ├─ USER/PAISECURITYSYSTEM/patterns.yaml (user's rules)
    │   └─ PAISECURITYSYSTEM/patterns.example.yaml (defaults)
    │
    ├─► Pattern Matching
    │   ├─ BLOCKED patterns
    │   │  └─ "rm -rf /" → exit(2) hard block
    │   │
    │   ├─ CONFIRM patterns
    │   │  └─ "git push --force" → ask user yes/no
    │   │
    │   └─ ALERT patterns
    │      └─ "curl | sh" → log warning but allow
    │
    ├─► Path Protection
    │   ├─ zeroAccess: Complete denial (e.g., /sys, /proc)
    │   ├─ restrictedAccess: Limited operations (e.g., /etc)
    │   └─ monitoredAccess: Log everything (e.g., ~/.ssh)
    │
    └─► Output
        ├─ exit(0) + {"continue": true} → Allow
        ├─ exit(0) + {"decision": "ask", "message": "..."} → Prompt
        └─ exit(2) → Hard block, log, explain
```

### 7.2 Pattern Definition (YAML)

```yaml
bash:
  blocked:
    - pattern: "rm -rf /"
      reason: "Filesystem destruction"
    - pattern: ":(){ :|:& };:"
      reason: "Fork bomb attack"
    - pattern: "sudo.*NOPASSWD"
      reason: "Privilege escalation"

  confirm:
    - pattern: "git push --force"
      reason: "Force push can lose commits"
    - pattern: "docker rm -f.*"
      reason: "Forceful container deletion"

  alert:
    - pattern: "curl.*\\|.*sh"
      reason: "Piping curl output to shell"
    - pattern: "eval.*input"
      reason: "Dynamic code execution"

paths:
  zeroAccess:
    - /sys
    - /proc
    - /boot
    - /root/.ssh          # Prevent accidentally exposing keys

  restrictedAccess:
    - path: /etc
      allow: Read
      deny: Write
    - path: ~/.aws
      allow: Read
      deny: Write

  monitoredAccess:
    - ~/.ssh              # Log all access
    - ~/MEMORY/SECURITY   # Log system security changes
```

### 7.3 Security Event Logging

Every security check logged to audit trail:

```json
{
  "timestamp": "2026-02-27T14:23:45Z",
  "session_id": "abc-123-uuid",
  "event_type": "blocked",
  "tool_name": "Bash",
  "command": "rm -rf /important/data",
  "pattern_matched": "rm -rf /",
  "reason": "Filesystem destruction",
  "user_notification": "Command blocked: This pattern would destroy files. Avoid 'rm -rf' on root paths.",
  "action_taken": "blocked_hard"
}
```

---

## Section 8: Other Innovative Features

### 8.1 Notification System

```
Multi-Channel Notifications:

Voice (Primary):
├─ ElevenLabs TTS for spoken feedback
├─ Custom voice for each agent
└─ Context-aware messages

Desktop Notifications:
├─ macOS: osascript (native notification center)
├─ Linux: notify-send (libnotify)
└─ Smart escalation for long-running tasks

Discord Integration:
├─ Team notifications for shared work
├─ Async status updates
└─ Long-running task completion alerts

ntfy.sh Integration:
├─ Mobile push notifications
├─ Cross-platform compatibility
└─ Lightweight + privacy-respecting
```

### 8.2 Observability Features

**Terminal UI Integration:**
- Tab titles show current task name + visual progress
- Status line shows learning signals + context usage
- Tab colors indicate session state (purple=thinking, orange=executing, green=complete)
- Duration-aware escalation (long tasks get more notifications)

### 8.3 Voice Server

Dedicated service for voice generation:
```
Voice Features:
├─ Per-agent voice identity
├─ ElevenLabs TTS backend
├─ Prosody enhancement (natural-sounding speech)
├─ Non-blocking (fire-and-forget design)
└─ Configurable via settings.json

REST API:
POST /notify
{
  "message": "Research complete, found 3 papers",
  "voice_id": "muZKMsIDGYtIkjjiUS82",
  "title": "Research Skill"
}
```

### 8.4 Versioning and Self-Upgrade

```
Version Tracking:
├─ Semantic versioning (v2.3.0, v3.0.0)
├─ Release notes with change summaries
├─ Upgrade-safe architecture
│  └─ USER/ customizations never overwritten
└─ SYSTEM/ can be safely replaced

Self-Improvement:
├─ PAI system can update itself
├─ Skills can be regenerated via CreateSkill
├─ Memory patterns used to optimize workflows
└─ User feedback drives versioning priorities
```

---

## Section 9: Integration Recommendations for axi-assistant

### 9.1 High-Impact Adoptions (Priority 1)

#### 1. The 7-Phase Cycle as Execution Framework

**Current State:** axi executes tasks sequentially
**Enhancement:** Map execution to 7-phase cycle

```
OBSERVE (context gathering):
├─ Load axi's user profile (TELOS equivalent)
├─ Scan previous learnings
└─ Check available skills

THINK (deep analysis):
├─ Use thinking tools (multi-expert review)
└─ Compare against ideal state

PLAN (structured approach):
├─ Break into steps
├─ Route to appropriate agents
└─ Define success criteria

BUILD (tool preparation):
├─ Create workspace
└─ Prepare execution environment

EXECUTE (actual work):
├─ Run planned steps
└─ Track intermediate outputs

VERIFY (quality checks):
├─ Test against criteria
└─ Get user feedback (1-10 rating)

LEARN (improve future):
├─ Extract insights
└─ Update skill routing
```

**Implementation:** 3-4 weeks | **Impact:** 25% improvement in task completion quality

#### 2. Three-Tier Memory System

**Current State:** axi has basic session memory
**Enhancement:** Implement hot/warm/cold memory with learning

```
HOT (active context):
├─ Current session work (WORK/[today]/[session]/)
├─ Active state (current-work.json)
└─ Last 7 days of sessions

WARM (learning signals):
├─ User ratings (ratings.jsonl)
├─ Detected patterns (PATTERNS/)
├─ Recent insights (INSIGHTS/)
└─ Skill effectiveness scores

COLD (archive):
├─ Historical sessions
├─ Long-term patterns
└─ Legacy context
```

**Implementation:** 6-8 weeks | **Impact:** axi learns from every interaction, gets better over time

#### 3. Hook System for Event-Driven Extensibility

**Current State:** axi executes tasks linearly
**Enhancement:** Add hooks for lifecycle events

```
SessionStart hooks:
├─ Load user profile
├─ Check for new features

UserPromptSubmit hooks:
├─ Format enforcement
├─ Sentiment capture

PreToolUse hooks:
├─ Security validation
└─ Capability routing

Stop hooks:
├─ Response capture
├─ Voice notification
└─ Learning extraction

SessionEnd hooks:
├─ Work completion analysis
└─ Session summary
```

**Implementation:** 4-6 weeks | **Impact:** Enables voice, observability, security, learning systems

### 9.2 Medium-Impact Adoptions (Priority 2)

#### 4. TELOS Framework for User Identity

**Enhancement:** Create axi TELOS interview system

```
Implement interview flow:
├─ Mission/Purpose questions
├─ Goal articulation
├─ Challenge mapping
├─ Belief capture
└─ Model extraction

Store in TELOS/ directory:
├─ MISSIONS.md
├─ GOALS.md
├─ CHALLENGES.md
├─ BELIEFS.md
└─ MODELS.md

Use in agent routing:
├─ Reference user's mission in decisions
├─ Explain outputs relative to goals
└─ Suggest paths aligned with values
```

**Implementation:** 3-4 weeks | **Impact:** Personalization + strategic alignment

#### 5. Multi-Agent Personality System

**Enhancement:** Support specialized agents with unique voice/style

```
Implement agent archetypes:
├─ Researcher (deep investigation)
├─ Engineer (implementation focus)
├─ Designer (creative solutions)
├─ Debugger (problem solver)
└─ Teacher (explanation focus)

Each agent has:
├─ Unique model selection
├─ Voice identity
├─ Permission levels
├─ Output format
└─ Specialty workflows
```

**Implementation:** 4-5 weeks | **Impact:** Better task-agent matching, improved outputs

### 9.3 Lower-Impact Adoptions (Priority 3)

#### 6. Fabric Pattern Integration

**Enhancement:** Adopt 50-100 highest-value Fabric patterns

```
Categories to integrate:
├─ Summarization (5-10 patterns)
├─ Code analysis (10-15 patterns)
├─ Writing improvement (10 patterns)
├─ Research/investigation (10 patterns)
└─ Domain-specific (10-20 patterns)

Integration approach:
├─ Map patterns to axi skills
├─ Use via skill workflows
├─ Learn which are most effective
└─ Customize over time
```

**Implementation:** 2-3 weeks | **Impact:** Rich capability without building from scratch

#### 7. Security Validation Patterns

**Enhancement:** Implement pattern-based security (like PAI)

```
Pattern matching:
├─ Blocked operations (rm -rf /)
├─ Confirm operations (git push --force)
├─ Alert patterns (suspicious commands)
└─ Path protection (sensitive directories)

Audit trail:
├─ Log all security checks
├─ User notification on blocks
└─ Recovery suggestions
```

**Implementation:** 2-3 weeks | **Impact:** Safe operation without restrictive permissions

---

## Section 10: Implementation Roadmap

### Phase 1: Foundation (Weeks 1-4)
- Implement 7-phase cycle in agent execution
- Create basic three-tier memory structure (hot/warm/cold)
- Set up hook system for key lifecycle events

### Phase 2: Learning (Weeks 5-10)
- Implement rating capture (explicit + implicit)
- Create learning extraction system
- Build pattern detection engine
- Set up skill routing based on effectiveness

### Phase 3: Identity & Personalization (Weeks 11-14)
- Implement TELOS interview system
- Create user profile store
- Add TELOS-aware context injection
- Support multi-agent personality system

### Phase 4: Integration & Polish (Weeks 15-18)
- Integrate top Fabric patterns
- Implement notification system (voice + desktop)
- Add security validation hooks
- Complete observability features

### Phase 5: Scale & Optimize (Weeks 19+)
- Performance optimization
- Team collaboration features
- Remote access capabilities
- Community pattern sharing

---

## Section 11: Critical Success Factors

### What PAI Does Really Well

1. **User-Centric Design Philosophy**
   - Not another tool harness
   - Built around understanding the person, not optimizing for generic tasks
   - Architecture reflects this priority

2. **Continuous Learning**
   - Every interaction generates signals
   - Systematic pattern detection
   - Feedback loops are built into the system
   - Not bolted on as afterthought

3. **Security Without Friction**
   - Patterns allow flexible rules
   - Fail-open but validated
   - Audit trail for transparency
   - User stays in control

4. **Modular Architecture**
   - Skills can be independently updated
   - Hooks allow extensibility without core changes
   - USER/ / SYSTEM/ separation enables upgrades
   - Clear data contracts between components

5. **Sophisticated Memory**
   - Three-tier structure matches access patterns
   - Phase-based organization (current, recent, archive)
   - Learning signals drive system improvement
   - Artifact storage + metadata for discovery

### What's Most Challenging About PAI

1. **Complexity of the Learning System**
   - Signal generation requires careful design
   - Pattern detection is non-trivial
   - Feedback loops can be unstable
   - Requires tuning to specific user

2. **Hook Coordination**
   - 14+ hooks with inter-dependencies
   - Ordering matters
   - Performance impact if not careful
   - Failure in one hook affects whole system

3. **Scale Questions**
   - Memory grows unbounded over time
   - Query performance on large datasets
   - Team collaboration isn't fully solved
   - Privacy considerations with learning

4. **Personality/Identity Balance**
   - Multi-agent system adds complexity
   - Need clear identity boundaries
   - Agent selection (routing) is non-trivial
   - Could become "too specialized" (fragmentation)

---

## Key Takeaways for axi-assistant

1. **Philosophy Over Technology**
   - PAI's power comes from user-centric philosophy, not any specific tech
   - Technology should serve that philosophy

2. **Memory is Infrastructure**
   - Not an afterthought, not optional
   - Core to everything PAI does
   - Enables continuous learning and personalization

3. **Learning Systems Are Hard**
   - Signal generation design is crucial
   - Feedback loops need careful tuning
   - But payoff is enormous (system that gets better)

4. **Hooks Enable Everything**
   - Decoupling mechanism that prevents fragility
   - Can add capabilities without touching core
   - Event-driven architecture scales better

5. **Identity Framework is Powerful**
   - TELOS captures non-technical but crucial information
   - Enables strategic decision-making
   - More valuable than any single skill

---

*Research compiled: February 27, 2026*
*Worktree: /home/ubuntu/axi-assistant/.claude/worktrees/research-pai-fabric*
