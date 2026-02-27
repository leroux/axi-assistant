# E. Skills/Patterns System

**Document Version:** 1.0
**Source:** Fabric v1.4.417 Pattern System
**Research Date:** February 27, 2026

---

## Overview

Fabric implements a comprehensive pattern system - a library of 254+ curated AI prompts organized by task domain. Rather than writing custom prompts each time, patterns are modular, composable, and discoverable via CLI or REST API.

**Key Concept:** Patterns are the fundamental units of AI augmentation. They encapsulate domain expertise and best practices in reusable, version-controlled prompt templates.

---

## Fabric Pattern Architecture

### What Is a Pattern?

A pattern is a specialized AI prompt template designed for a specific task category. Each pattern consists of:

1. **system.md** - System prompt (task definition, instructions, output format)
2. **user.md** - User input template (what user provides, variable placeholders)
3. Optional metadata (description, category, variables)

### Pattern Directory Structure

```
patterns/
├── {pattern_name}/
│   ├── system.md          # System prompt
│   ├── user.md            # User input template
│   └── (optional files)
│
├── summarize/
│   ├── system.md
│   └── user.md
│
├── explain/
│   ├── system.md
│   └── user.md
│
├── analyze_paper/
│   ├── system.md
│   └── user.md
│
└── ... (254+ more patterns)
```

### Pattern Categories (Partial List)

**Analysis Patterns (40+):**
- analyze_paper, analyze_paper_simple, analyze_bugs, analyze_comments, analyze_bill, analyze_candidates, analyze_debate, analyze_email_headers, analyze_claims, analyze_logs, analyze_malware, analyze_mistakes, analyze_patent, analyze_personality, analyze_presentation, analyze_code, analyze_incident, analyze_proposition, analyze_prose, etc.

**Content Generation (30+):**
- write_blog_post, write_poem, write_micro_essay, write_haiku, write_hackerone_report, write_act, create_aphorisms, create_conceptmap, create_openings, create_social_media, etc.

**Development (25+):**
- code_review, coding_master, debug, explain_code, generate_changelog, review_code, summarize_git_changes, etc.

**Learning & Education (20+):**
- analyze_learning_materials, create_study_guide, explain_terms_and_conditions, extract_domains, extract_recipe, extract_recommendations, etc.

**Filtering & Selection (15+):**
- extract_business_ideas, extract_predictions, extract_laws, find_hidden_message, rate_value, detect_mind_virus, etc.

**Other Specialized (130+):**
- agility_story, ai, ultimatelaw_safety, improve_document, optimize_code, send_email, sort_narrative_by_emphasis, summarize, etc.

---

## Example Pattern: summarize

### system.md
```markdown
# IDENTITY and PURPOSE

You are an expert content summarizer. You take content in and output a Markdown formatted summary using the format below.

Take a deep breath and think step by step about how to best accomplish this goal using the following steps.

# OUTPUT SECTIONS

- Combine all of your understanding of the content into a single, 20-word sentence in a section called ONE SENTENCE SUMMARY:.

- Output the 10 most important points of the content as a list with no more than 16 words per point into a section called MAIN POINTS:.

- Output a list of the 5 best takeaways from the content in a section called TAKEAWAYS:.

# OUTPUT INSTRUCTIONS

- Create the output using the formatting above.
- You only output human readable Markdown.
- Output numbered lists, not bullets.
- Do not output warnings or notes—just the requested sections.
- Do not repeat items in the output sections.
- Do not start items with the same opening words.
```

### user.md
```
INPUT:

{{input}}
```

### Usage
```bash
# Basic usage
echo "Long article text here..." | fabric --pattern summarize

# With custom variables (if pattern supports them)
fabric --pattern analyze_paper --variables '{"format":"academic","length":"brief"}'

# Via REST API
curl -X POST http://localhost:8080/chat \
  -H "Content-Type: application/json" \
  -d '{
    "prompts": [{
      "userInput": "Long article here...",
      "vendor": "openai",
      "model": "gpt-4",
      "patternName": "summarize"
    }]
  }'
```

---

## Pattern System Components

### 1. Pattern Loader
Responsible for discovering and loading patterns from disk or remote sources:

```go
type Pattern struct {
  Name        string
  Description string
  Pattern     string
}

// Load pattern by name
func (p *PatternsEntity) loadPattern(source string) (*Pattern, error) {
  // Check if it's a file path
  if isFilePath(source) {
    return getFromFile(source)
  }
  // Otherwise, get from pattern database
  return getFromDB(source)
}

// Load from installed patterns directory
func (p *PatternsEntity) getFromDB(name string) (*Pattern, error) {
  patternPath := filepath.Join(p.Dir, name, "system.md")
  pattern, err := os.ReadFile(patternPath)
  if err != nil {
    return nil, fmt.Errorf("pattern not found: %s", name)
  }
  return &Pattern{
    Name:    name,
    Pattern: string(pattern),
  }, nil
}
```

### 2. Variable Substitution
Patterns support dynamic variables:

```markdown
# Custom pattern with variables

You are an expert {{role}} who specializes in {{domain}}.

Your task: {{task}}

Format your response as {{format}}.
```

**Variables are supplied at runtime:**
```bash
fabric --pattern custom_task \
  --variables '{"role":"data scientist","domain":"machine learning","task":"analyze this model","format":"executive summary"}'
```

**Variable Application Logic:**
1. Protect `{{input}}` with a sentinel token
2. Apply all other variables using template.ApplyTemplate
3. Replace sentinel with actual user input
4. This prevents recursive variable resolution on input

```go
func (p *PatternsEntity) applyVariables(
  pattern *Pattern,
  variables map[string]string,
  input string,
) error {
  // Protect {{input}}
  withSentinel := strings.ReplaceAll(
    pattern.Pattern,
    "{{input}}",
    template.InputSentinel,
  )

  // Apply variables
  processed, err := template.ApplyTemplate(withSentinel, variables, input)
  if err != nil {
    return err
  }

  // Restore {{input}} as actual input
  pattern.Pattern = strings.ReplaceAll(
    processed,
    template.InputSentinel,
    input,
  )
  return nil
}
```

### 3. Pattern Discovery
List available patterns:

```bash
# List all patterns
fabric --list

# Search for pattern
fabric --search code

# Show pattern description
fabric --show summarize
```

**REST API:**
```bash
# List pattern names
curl http://localhost:8080/patterns/names

# Get pattern metadata
curl http://localhost:8080/patterns/describe/summarize
```

### 4. Custom Patterns
Users can create custom patterns:

```bash
# Create custom pattern
mkdir ~/.config/fabric/patterns/my_custom_pattern
cat > ~/.config/fabric/patterns/my_custom_pattern/system.md << EOF
# My Custom Task

You are an expert {{role}}.

Task: {{task}}

Respond in {{format}}.
EOF

# Use it
fabric --pattern my_custom_pattern \
  --variables '{"role":"architect","task":"review this design","format":"detailed critique"}'
```

---

## Comparison: Fabric Patterns vs axi Skills

### Current axi Skills

**Current Approach:**
- Skills are Python classes with execute() methods
- Each skill is a discrete module
- Limited templating/parameterization
- Manual invocation via `skill` system

**Current Structure:**
```python
class SkillName(Skill):
  """One-line description"""

  async def execute(self, params: SkillParams) -> SkillResult:
    # Implementation
    return SkillResult(...)
```

### Fabric Patterns Approach

**Advantages:**
- **Declarative:** Prompt templates, not code
- **Versionable:** Plain text files in git
- **Composable:** Patterns can reference other patterns
- **Discoverable:** Full pattern library searchable
- **User-customizable:** Non-programmers can create patterns
- **API-first:** Works via REST, CLI, or libraries
- **Language-agnostic:** Works with any LLM provider

**Structure:**
```
pattern_name/
├── system.md      # Task definition, output format
├── user.md        # Input template with {{variables}}
└── README.md      # (optional) Documentation
```

---

## Proposed Integration: Hybrid Skills+Patterns

### Hybrid Model for axi

**Combine best of both approaches:**

1. **Skills** remain as higher-level orchestrators
2. **Patterns** become the prompt layer (user.md, system.md)
3. **Pattern variables** provide parameterization
4. **Pattern library** grows organically

### File Structure

```
~/.claude/skills/
├── {skill_name}/
│   ├── SKILL.md           # Skill documentation
│   ├── patterns/          # Associated patterns
│   │   ├── primary/
│   │   │   ├── system.md
│   │   │   └── user.md
│   │   ├── analysis/
│   │   │   ├── system.md
│   │   │   └── user.md
│   │   └── ...
│   ├── execute.ts         # Skill orchestration logic
│   └── config.json        # Skill configuration
│
├── code_review/
│   ├── SKILL.md
│   ├── patterns/
│   │   ├── primary/
│   │   │   ├── system.md  # "Review this code for quality"
│   │   │   └── user.md
│   │   ├── security/
│   │   │   ├── system.md  # "Review this code for security"
│   │   │   └── user.md
│   │   └── performance/
│   │       ├── system.md  # "Review this code for performance"
│   │       └── user.md
│   └── execute.ts
│
└── ...
```

### Hybrid Skill Implementation

```typescript
// Skill orchestrator
class CodeReviewSkill extends Skill {
  async execute(params: CodeReviewParams): Promise<SkillResult> {
    const reviewType = params.reviewType || 'primary'

    // Load appropriate pattern
    const pattern = await this.patternEngine.load(
      `code_review/${reviewType}`
    )

    // Apply variables
    const renderedPrompt = await this.patternEngine.applyVariables(
      pattern,
      {
        language: params.language,
        standards: params.standards,
        format: params.format,
      },
      params.code
    )

    // Invoke LLM with rendered prompt
    const response = await this.llm.complete({
      system: pattern.system,
      user: renderedPrompt.user,
      model: params.model,
    })

    return new SkillResult(response)
  }

  // Capability declaration
  getCapabilities(): string[] {
    return [
      'code-review-quality',
      'code-review-security',
      'code-review-performance',
    ]
  }
}
```

---

## Pattern-Based Skill Library

### Recommended Initial Patterns

**Analysis Patterns (for capability audit):**
- analyze_code_quality
- analyze_security_risks
- analyze_performance_bottlenecks
- analyze_documentation_gaps

**Generation Patterns:**
- generate_documentation
- generate_test_cases
- generate_api_documentation
- generate_commit_messages

**Development Patterns:**
- review_code
- debug_error
- explain_code
- optimize_code

**Content Patterns:**
- summarize
- extract_key_points
- create_outline
- write_documentation

### Pattern Organization

```
patterns/
├── analysis/
│   ├── analyze_code_quality/
│   ├── analyze_security/
│   └── analyze_performance/
├── development/
│   ├── code_review/
│   ├── debug/
│   └── explain_code/
├── content/
│   ├── summarize/
│   ├── create_outline/
│   └── extract_points/
└── generation/
    ├── generate_documentation/
    ├── generate_tests/
    └── generate_commits/
```

---

## REST API Integration

### Pattern Endpoints

**List patterns:**
```bash
GET /patterns/names
GET /patterns/describe/{pattern_name}
GET /patterns/list?category=analysis
```

**Invoke pattern:**
```bash
POST /chat
{
  "prompts": [{
    "userInput": "code to review",
    "patternName": "code_review",
    "variables": {"language": "TypeScript", "standards": "airbnb"}
  }]
}
```

**Create custom pattern:**
```bash
POST /patterns/custom
{
  "name": "my_analysis",
  "system": "...",
  "user": "...",
  "category": "analysis"
}
```

---

## CLI for Pattern Management

### Commands

```bash
# List patterns
axi patterns list
axi patterns list --category analysis
axi patterns search code

# Show pattern
axi patterns show summarize
axi patterns show code_review --detailed

# Create pattern
axi patterns create my_task
# Opens editor for system.md and user.md

# Test pattern
axi patterns test summarize --input "article text"

# Export pattern
axi patterns export summarize --output ~/my_patterns/

# Import pattern
axi patterns import ~/my_patterns/my_task/

# Delete pattern
axi patterns delete my_task
```

---

## Pattern Versioning & Git Integration

### Versioning Strategy

Patterns live in git:

```bash
git log -- patterns/code_review/system.md
# Shows all versions of code_review system prompt

git diff HEAD~1 -- patterns/code_review/
# Shows what changed in the pattern
```

### Version Promotion

```bash
# Mark pattern as stable
axi patterns promote code_review --version 1.2.0

# Use specific version
axi patterns use code_review@1.2.0

# See version history
axi patterns history code_review
```

---

## Learning from Pattern Usage

### Track Pattern Performance

```json
{
  "pattern": "code_review",
  "usage_count": 47,
  "average_satisfaction": 7.8,
  "last_used": "2026-02-27T10:30:00Z",
  "top_variables": {
    "language": {"typescript": 25, "python": 15, "go": 7},
    "standard": {"airbnb": 20, "google": 15, "custom": 12}
  },
  "low_ratings": [
    {"rating": 3, "reason": "too verbose output", "date": "2026-02-20"},
    {"rating": 2, "reason": "missed critical bug", "date": "2026-02-15"}
  ]
}
```

### Pattern Improvement Workflow

1. User rates pattern result (1-10)
2. Low ratings trigger analysis
3. Pattern author reviews feedback
4. Pattern updated based on learnings
5. New version promoted when stable

---

## Comparison Table

| Aspect | axi Skills | Fabric Patterns | Hybrid |
|--------|-----------|-----------------|--------|
| Implementation | TypeScript classes | Markdown files | Both |
| Templating | Limited | Full variable support | Full |
| Versioning | Via code repo | Via code repo | Via code repo |
| User-customizable | Requires coding | Markdown editing | Markdown editing |
| Discoverability | Manual listing | Full search/browse | Full search/browse |
| Composability | High (code) | Medium (refs) | High (code) |
| Performance | Fast (compiled) | Medium (template) | Medium |
| Learning capture | Limited | Excellent (patterns) | Excellent |

---

## Implementation Roadmap

### Phase 1: Pattern Framework (2 weeks)
1. Create pattern loader (mirroring Fabric's approach)
2. Implement variable substitution
3. Build pattern discovery API
4. Create CLI commands for pattern management

### Phase 2: Skill Refactoring (3 weeks)
1. Decouple prompts from skill code
2. Extract prompts → patterns/
3. Update skill execute() to use pattern engine
4. Test with 5-10 core skills

### Phase 3: Pattern Library (2-3 weeks)
1. Create 50+ initial patterns
2. Organize by category
3. Add documentation for each
4. Test with users

### Phase 4: Learning Integration (2 weeks)
1. Track pattern usage
2. Capture satisfaction per pattern
3. Analyze low-rating patterns
4. Suggest pattern improvements

---

## Resources

- **Fabric Repository:** https://github.com/danielmiessler/Fabric
- **Fabric Patterns:** /tmp/pai-fabric-research/Fabric/data/patterns/ (254+ examples)
- **Pattern Loader Code:** /tmp/pai-fabric-research/Fabric/internal/tools/patterns_loader.go
- **REST API Docs:** /tmp/pai-fabric-research/Fabric/docs/rest-api.md

---

**Document Complete**
**Recommendation:** Implement pattern framework first (foundation), then refactor existing skills to use patterns, finally build rich pattern library with learning integration
