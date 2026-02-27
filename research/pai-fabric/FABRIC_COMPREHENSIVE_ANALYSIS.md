# Fabric - Comprehensive Technical Analysis

**Investigation Date:** February 27, 2026
**Focus:** Pattern system, capabilities, and integration potential with axi-assistant
**Status:** Complete Research Phase

---

## Executive Summary

Fabric is a mature, production-grade prompt engineering framework written in Go. Unlike PAI's focus on personal infrastructure and learning, Fabric is **provider-agnostic and pattern-centric**: it organizes AI capabilities as reusable, discoverable, composable prompts organized by task domain.

**Key Characteristics:**
- 250+ production patterns across 30+ domains
- Multi-provider support (20+ AI backends)
- Pattern composition and chaining ("stitching")
- Active development with release every 2-3 days
- Massive community adoption
- REST API + CLI + Web UI interfaces

---

## Section 1: Architecture Overview

### 1.1 Fabric's Philosophy

Fabric's core insight: **AI doesn't have a capabilities problem—it has an integration problem.**

```
The Problem:
├─ Thousands of AI applications exist
├─ Hundreds of excellent prompts are scattered across the internet
├─ Users don't know which to use or when
└─ No standardized way to share or discover prompts

Fabric's Solution:
├─ Organize prompts as "Patterns" by task/domain
├─ Standardize pattern format (system.md + metadata)
├─ Provide discovery mechanism (list, search)
├─ Enable composition (chain patterns together)
├─ Support custom patterns (user's own)
└─ Multi-provider compatibility (use any AI backend)
```

### 1.2 Core Architecture

```
Fabric System

┌─────────────────────────────────────────────┐
│      Fabric CLI / API Server                │
├─────────────────────────────────────────────┤
│                                             │
│  Command Interface:                         │
│  fabric -p [pattern] [options]             │
│                                             │
│  ├─ Pattern selection                      │
│  ├─ Input handling (stdin/files/URL/YT)   │
│  ├─ Provider routing                       │
│  ├─ Output streaming/saving                │
│  └─ Session/context management             │
│                                             │
├─────────────────────────────────────────────┤
│                                             │
│  Pattern Engine:                           │
│  ├─ Load pattern (system.md + user.md)    │
│  ├─ Apply variables (if any)              │
│  ├─ Inject context/input                  │
│  ├─ Send to selected provider              │
│  └─ Stream/buffer response                │
│                                             │
├─────────────────────────────────────────────┤
│                                             │
│  Provider Adapters (20+ backends):        │
│  ├─ OpenAI (GPT-4, ChatGPT)               │
│  ├─ Anthropic (Claude family)             │
│  ├─ Google (Gemini)                       │
│  ├─ Ollama (local models)                 │
│  ├─ Azure OpenAI / Bedrock / Vertex       │
│  ├─ Groq / Mistral / DeepSeek             │
│  ├─ Perplexity / Together / Venice        │
│  ├─ Custom OpenAI-compatible               │
│  └─ [10+ more providers]                   │
│                                             │
├─────────────────────────────────────────────┤
│                                             │
│  Configuration & Context:                  │
│  ├─ ~/.config/fabric/config.yaml          │
│  ├─ ~/.config/fabric/patterns/            │
│  ├─ ~/.config/fabric/strategies/          │
│  ├─ ~/.config/fabric/contexts/            │
│  └─ ~/.config/fabric/sessions/            │
│                                             │
└─────────────────────────────────────────────┘
```

---

## Section 2: Pattern System - Deep Dive

### 2.1 What Is a Pattern?

A **Pattern** is a reusable, discoverable prompt template for a specific task or domain.

**Core Properties:**
- **Discrete Task Focus** - Does one thing well (summarize, analyze, code, write)
- **Multi-Provider Compatible** - Works with OpenAI, Claude, Gemini, etc.
- **Parameterizable** - Accepts variables for customization
- **Versioned** - Can evolve over time
- **Well-Documented** - Includes examples and usage
- **Community Shared** - Can be shared publicly or kept private

### 2.2 Pattern Anatomy

```
Minimal Pattern Structure:

fabric/data/patterns/summarize/
├── system.md          # The actual prompt
├── user.md           # [Optional] User message template
├── README.md         # [Optional] Documentation
└── config.yaml       # [Optional] Pattern metadata
```

#### system.md Format

```markdown
# IDENTITY and PURPOSE

You are an expert content summarizer who...

# INSTRUCTIONS

1. Read the content carefully
2. Identify the most important points
3. Format output as requested

# OUTPUT FORMAT

## ONE SENTENCE SUMMARY
[Summarize in 20 words]

## MAIN POINTS
1. [Point 1]
2. [Point 2]
...

# INPUT:
INPUT:
```

**Key Observations:**
- Uses Markdown for maximum readability
- Clear sections (IDENTITY → INSTRUCTIONS → OUTPUT → INPUT)
- System message (not user message) contains the prompt
- Variables marked as ALL_CAPS or #variable
- INPUT marker signals where user content goes

#### user.md (Optional Template)

```markdown
# Analysis Context

You are analyzing:
- Domain: #domain
- Focus: #focus
- Audience: #audience

# Content to Analyze

#INPUT
```

#### Metadata (Optional config.yaml)

```yaml
name: summarize
description: Create a summary of any content
author: Daniel Miessler
version: 1.0
tags:
  - extraction
  - summary
  - writing
requires_input: true
default_model: gpt-4
strategies:
  - cot          # Can use Chain of Thought
  - tod          # Can use Tree of Draft
```

### 2.3 Complete Pattern Inventory (250+ Patterns)

#### By Category

**EXTRACTION (30+ patterns):**
```
├── summarize               # Create summary
├── extract_interesting     # Find key parts
├── extract_wisdom          # Find wisdom in long content
├── create_conceptmap       # Visual knowledge representation
├── extract_main_points     # Bullet-point summary
├── extract_articles        # From web content
├── create_book_excerpt     # Craft book summaries
├── summary_questions       # Generate Q&A from content
├── haiku_summary           # Ultra-brief summary
└── [21 more extraction patterns]
```

**ANALYSIS (50+ patterns):**
```
├── analyze_code            # Evaluate code
├── analyze_claims          # Fact-check claims
├── analyze_debate          # Examine argument
├── analyze_malware         # Security analysis
├── analyze_threat_report   # Threat intel analysis
├── analyze_prose           # Literary analysis
├── analyze_paper           # Academic paper review
├── analyze_patent          # Patent analysis
├── analyze_logs            # Log analysis
├── analyze_mistakes        # Postmortem analysis
├── analyze_personality     # Character analysis
├── analyze_product_feedback # User feedback analysis
├── improve_writing         # Text enhancement
├── explain_code            # Code walkthrough
└── [36 more analysis patterns]
```

**WRITING (40+ patterns):**
```
├── write_essay             # Long-form writing
├── write_latex             # Generate LaTeX documents
├── write_legal             # Legal document generation
├── create_social_media     # Social posts
├── create_social_media_thread # Thread composition
├── create_tweet            # Tweet generation
├── create_headline         # Headline writing
├── write_story             # Fiction generation
├── write_poem              # Poetry creation
├── improve_writing         # Text refinement
├── fix_english             # Grammar/spelling
├── proofread               # Comprehensive review
├── create_reading_recommendations # Book suggestions
└── [27 more writing patterns]
```

**CODING (25+ patterns):**
```
├── create_coding_feature   # Generate code for feature
├── code_review             # Evaluate code quality
├── explain_code            # Break down code
├── convert_markdown_to_json # Structured data extraction
├── convert_json_to_yaml    # Format conversion
├── create_design_doc       # Technical specification
├── create_readme           # Documentation
├── create_cli_command      # CLI tool generation
├── refactor_code           # Code improvement
├── optimize_code           # Performance optimization
├── document_code           # Add documentation
├── find_bugs               # Bug identification
├── suggest_patterns        # Design patterns
└── [12 more coding patterns]
```

**RESEARCH (20+ patterns):**
```
├── create_reading_recommendations # Paper/book suggestions
├── identify_bias_and_propaganda   # Detect manipulation
├── investigate_security_report    # Threat analysis
├── research_topic                 # Topic exploration
├── research_academic_papers       # Literature search
├── research_historical            # Historical analysis
├── find_citations                 # Citation finding
├── evidence_synthesis             # Evidence compilation
├── hypothesis_generation          # Theory development
└── [11 more research patterns]
```

**DOMAIN-SPECIFIC (60+ patterns):**
```
WELLNESS:
├── analyze_psychological_impact   # Mental health analysis
├── analyze_spiritual_text         # Religious/spiritual analysis
├── wellness_therapy_guidance      # Mental health support
├── create_wellness_plan           # Health planning

FINANCE:
├── analyze_bill                   # Bill analysis
├── analyze_financial_report       # Financial analysis
├── investment_analysis            # Investment evaluation

SALES:
├── handle_objections              # Sales technique
├── sales_pitch                    # Pitch improvement

MARKETING:
├── analyze_marketing_campaign     # Campaign analysis
├── create_marketing_copy          # Marketing text

MILITARY/SECURITY:
├── analyze_military_strategy      # Strategy analysis
├── analyze_threat_report          # Threat assessment

And more: Legal, Academic, Business, Tech, Medicine, etc.
```

### 2.4 Pattern Discovery and Usage

#### Listing Patterns
```bash
# List all patterns
fabric --listpatterns

# Output:
# summarize               Extract key information from content
# analyze_code            Examine code for issues and improvements
# write_essay             Create a well-structured essay
# ... [247 more]

# List with descriptions
fabric -l | head -50
```

#### Using a Pattern
```bash
# Basic usage
echo "Content to summarize" | fabric -p summarize

# With streaming
echo "Content" | fabric -p summarize -s

# Save to file
echo "Content" | fabric -p summarize -o output.md

# From URL (uses Jina AI to scrape)
fabric -u https://example.com/article -p summarize

# From YouTube
fabric -y "https://youtube.com/watch?v=..." -p extract_wisdom

# From file
cat article.txt | fabric -p summarize

# With strategy
echo "Complex problem" | fabric -p analyze_code --strategy cot
```

#### Pattern Variables
```bash
# Define variables for parameterized patterns
echo "Review this code" | fabric -p code_review \
  -v="#language:Python" \
  -v="#focus:performance"

# Or inline in pattern
fabric -p custom_pattern --input-has-vars \
  "Analyze this for #focus with #style"
```

---

## Section 3: Pattern Strategies

### 3.1 What Are Strategies?

**Strategies** are JSON files that modify a prompt to improve reasoning quality. They wrap a pattern with different reasoning techniques.

### 3.2 Available Strategies

```
cot   (Chain of Thought)
├─ Let's think step by step
├─ Show intermediate reasoning
├─ Works well for analysis
└─ Use: fabric -p analyze_code --strategy cot

cod   (Chain of Draft)
├─ Iterative drafting with minimal notes
├─ Each step: max 5 words
├─ Best for: Writing
└─ Use: fabric -p write_essay --strategy cod

tot   (Tree of Thought)
├─ Generate multiple reasoning paths
├─ Evaluate each path
├─ Select best path
├─ Best for: Decision making
└─ Use: fabric -p analyze_claims --strategy tot

aot   (Atom of Thought)
├─ Break into atomic sub-problems
├─ Solve independently
├─ Compose solutions
├─ Best for: Hard technical problems
└─ Use: fabric -p create_coding_feature --strategy aot

ltm   (Least-to-Most)
├─ Solve easy sub-problems first
├─ Use solutions to solve harder ones
├─ Best for: Learning/Teaching
└─ Use: fabric -p explain_code --strategy ltm

self-consistent (Self-Consistency)
├─ Multiple independent reasoning paths
├─ Consensus voting
├─ Best for: Accuracy critical
└─ Use: fabric -p analyze_malware --strategy self-consistent

self-refine (Self-Refinement)
├─ Answer → Critique → Refine
├─ Multiple iterations
├─ Best for: Iterative improvement
└─ Use: fabric -p improve_writing --strategy self-refine

reflexion (Reflexion)
├─ Answer → Brief critique → Refine
├─ Faster than self-refine
├─ Best for: Quick iteration
└─ Use: fabric -p write_essay --strategy reflexion

standard (Standard)
├─ Direct answer without explanation
├─ Fastest execution
├─ Best for: Simple tasks
└─ Use: fabric -p summarize --strategy standard
```

### 3.3 Strategy Implementation

**Strategy file structure (cot.json):**
```json
{
  "name": "chain_of_thought",
  "description": "Let's think step by step",
  "instructions": "When answering, think step by step and show your reasoning.",
  "prompt_prefix": "Let's think step by step.\n\n",
  "prompt_suffix": "\n\nNow let's think through this carefully..."
}
```

---

## Section 4: Advanced Features

### 4.1 Pattern Stitching (Composition)

**Stitching** chains patterns together to create complex workflows.

```bash
# Basic stitching: Filter sensitive data then summarize
cat raw-output.txt | \
  fabric -p filter_sensitive_data | \
  fabric -p summarize -s

# Complex stitch: Extract → Analyze → Improve
cat article.txt | \
  fabric -p extract_wisdom | \
  fabric -p analyze_claims | \
  fabric -p improve_writing
```

**Use Cases:**
- Clean data locally (Ollama) before sending to cloud (OpenAI)
- Multi-stage analysis (extract → validate → summarize)
- Iterative refinement (draft → critique → polish)

### 4.2 Contexts and Sessions

**Contexts:** Pre-loaded information for patterns

```bash
# Create context
fabric --setup

# Use context in pattern
fabric -p analyze_code -C "my_codebase"

# Save outputs as context for reuse
fabric -p extract_wisdom -C "research_topic"
```

**Sessions:** Maintain state across pattern invocations

```bash
# Start session
fabric --session "project_alpha"

# Invocations in session share state
fabric -p analyze_code --session "project_alpha"
fabric -p code_review --session "project_alpha"

# Query session
fabric --listsessions
```

### 4.3 Custom Patterns

Users can create private patterns:

```bash
# Create custom pattern directory
mkdir ~/.config/fabric/custom_patterns

# Create pattern
mkdir ~/.config/fabric/custom_patterns/my_analyzer
echo "You are an expert analyzer..." > \
  ~/.config/fabric/custom_patterns/my_analyzer/system.md

# Use custom pattern
echo "Content" | fabric -p my_analyzer
```

### 4.4 REST API Server

Fabric can run as a REST API server:

```bash
# Start server
fabric --serve

# API endpoints:
POST /chat
POST /pattern/{pattern_name}
GET /patterns
GET /contexts
POST /youtube
POST /scrape_url
```

### 4.5 Web Interface

Fabric includes a web UI for non-CLI users:

```
Features:
├─ Pattern selection dropdown
├─ Text input area
├─ Streaming output
├─ File upload/download
├─ Context management
└─ Session history
```

---

## Section 5: Provider Support

### 5.1 Native Integrations (Full SDK Support)

```
OpenAI:
├─ GPT-4, GPT-4 Turbo, GPT-3.5
├─ Vision support
├─ Web search
└─ Advanced reasoning/thinking

Anthropic (Claude):
├─ Claude 3.5 Opus, Sonnet, Haiku
├─ Extended context (100K+ tokens)
├─ Tool use
└─ Vision support

Google Gemini:
├─ Gemini Pro 2.0, 1.5
├─ Search tool
├─ Image generation
└─ Function calling

Ollama (Local):
├─ Run models locally
├─ llama, mistral, neural-chat
├─ No API key needed
└─ Privacy respecting

Azure OpenAI:
├─ Enterprise support
├─ Entra ID authentication
├─ Managed deployment
└─ Compliance features
```

### 5.2 OpenAI-Compatible Providers (25+)

```
├─ Groq (fast inference)
├─ Mistral (open models)
├─ DeepSeek (Chinese LLMs)
├─ Together AI
├─ Replicate
├─ Perplexity
├─ Cohere
├─ LiteLLM
├─ OpenRouter
├─ [15+ more]
```

### 5.3 Enterprise Backends

```
├─ AWS Bedrock
├─ Google Vertex AI
├─ Microsoft 365 Copilot (data grounding)
├─ Digital Ocean GenAI
├─ Azure AI Gateway
└─ Langdock
```

---

## Section 6: Recent Feature Additions (2025-2026)

### 6.1 Recent Releases

```
Feb 2026:
├─ Azure Entra ID Authentication
├─ Azure AI Gateway Plugin (multi-backend)
├─ [v1.4.417 - v1.4.416]

Jan 2026:
├─ Claude Opus 4.5 Support
├─ Microsoft 365 Copilot Integration
└─ Digital Ocean GenAI Support

Dec 2025:
├─ Complete i18n (10 languages)
├─ Interactive API docs (Swagger)
└─ Abacus AI Support

Aug-Nov 2025:
├─ Desktop Notifications
├─ Claude 4.1 Added
├─ Speech-to-Text Support
├─ Image Generation Support
├─ Web Search for OpenAI/Gemini
├─ Thinking/Reasoning Support
└─ Extended Context (1M tokens)
```

### 6.2 Major Capabilities Added in Recent Months

```
Vision:
├─ Image generation (DALL-E, etc.)
├─ Image analysis (Claude, GPT-4 Vision)
└─ Screenshot input

Audio:
├─ Speech-to-Text (OpenAI)
├─ Text-to-Speech (ElevenLabs, Google)
└─ Voice cloning

Advanced Reasoning:
├─ Extended thinking (Claude, OpenAI)
├─ Multi-step reasoning
└─ Tool use integration

Content Input:
├─ YouTube transcripts + comments + metadata
├─ Web scraping (Jina AI integration)
├─ PDF processing
└─ Various media formats

Accessibility:
├─ i18n (10 languages)
├─ Shell completions (zsh, bash, fish)
├─ Multiple shells supported
└─ Comprehensive documentation
```

---

## Section 7: Comparison: Fabric vs Similar Tools

### 7.1 Fabric vs OpenAI's Cookbook

| Aspect | Fabric | Cookbook |
|--------|--------|----------|
| **Format** | Markdown (.md) | Jupyter notebooks (.ipynb) |
| **Discoverability** | Built-in pattern list | Repository browsing |
| **Composition** | CLI piping (stitching) | Python code required |
| **Customization** | Variables + strategies | Full code modification |
| **Provider Support** | 20+ backends | OpenAI focus |
| **CLI Integration** | Excellent | N/A |
| **Community** | Large + growing | Moderate |

### 7.2 Fabric vs LangChain/LlamaIndex

| Aspect | Fabric | LangChain |
|--------|--------|-----------|
| **Use Case** | Task-focused prompts | Application framework |
| **Learning Curve** | Low (just markdown) | High (Python required) |
| **Customization** | Pattern variables | Full programming |
| **Deployment** | CLI or REST API | Python applications |
| **Community Patterns** | 250+ built-in | Community via npm |
| **State Management** | Sessions | Complex RAG setup |
| **Best For** | Quick tasks | Complex applications |

---

## Section 8: Integration Recommendations for axi-assistant

### 8.1 High-Value Fabric Adoption Areas

#### 1. Pattern Library as Core Capability

**Recommendation:** Integrate 50-100 top Fabric patterns into axi

```
Implementation approach:
├─ Copy pattern definitions to axi-patterns/
├─ Create skill wrappers around patterns
├─ Build pattern discovery via SkillSearch
├─ Enable pattern composition (stitching)
└─ Track pattern effectiveness via ratings
```

**High-Priority Pattern Categories (by impact):**

```
Tier 1 (Must Have):
├─ summarize (general purpose)
├─ analyze_code (developer focus)
├─ improve_writing (all users)
├─ explain_code (teaching/learning)
├─ code_review (quality focus)
└─ create_coding_feature (building)

Tier 2 (Should Have):
├─ extract_wisdom (research)
├─ write_essay (writing)
├─ analyze_claims (verification)
├─ create_conceptmap (visualization)
├─ create_reading_recommendations (discovery)
└─ identify_bias_and_propaganda (critical thinking)

Tier 3 (Nice to Have):
├─ 40+ domain-specific patterns
├─ Language-specific patterns
└─ Specialized analysis patterns
```

**Implementation Effort:** 4-5 weeks
**Value Added:** Instant library of proven patterns with community validation

#### 2. Provider Abstraction (Multi-Backend Support)

**Recommendation:** Adopt Fabric's provider abstraction

```
Current axi:
└─ Hardcoded to Claude Code / Anthropic

Enhanced axi:
├─ Anthropic (default)
├─ OpenAI (user choice)
├─ Google Gemini (user choice)
├─ Ollama (local models)
├─ Azure OpenAI (enterprise)
└─ Groq / Mistral / Others

Implementation:
├─ Provider selection in settings
├─ Fallback routing (if primary unavailable)
├─ Per-pattern provider hints
└─ Cost optimization (use cheaper model for simple tasks)
```

**Implementation Effort:** 3-4 weeks
**Value Added:** Flexibility + cost optimization + vendor independence

#### 3. Strategies for Advanced Reasoning

**Recommendation:** Embed Fabric's strategy system

```
Integration:
├─ Detect task complexity in OBSERVE phase
├─ Automatically select strategy:
│  ├─ Simple tasks → standard
│  ├─ Analysis tasks → cot (Chain of Thought)
│  ├─ Writing tasks → self-refine
│  └─ Complex logic → aot (Atom of Thought)
├─ Allow user override via prompts
└─ Learn which strategies work best for each user
```

**Implementation Effort:** 2-3 weeks
**Value Added:** Better reasoning for complex tasks + automatic optimization

#### 4. Content Input Handling

**Recommendation:** Leverage Fabric's input capabilities

```
Fabric already handles:
├─ stdin/pipe input
├─ File input
├─ URL scraping (Jina AI)
├─ YouTube (transcript + comments + metadata)
├─ PDF processing

axi enhancement:
├─ Adopt same input flexibility
├─ Pre-process content consistently
├─ Cache scraped content
└─ Support batch processing
```

**Implementation Effort:** 2 weeks
**Value Added:** Universal input handling without rebuilding

---

## Section 9: Why Fabric Matters for axi

### 9.1 What Fabric Does Extraordinarily Well

1. **Pattern Curation**
   - Community-vetted patterns
   - Real-world tested
   - Well-documented
   - Actively maintained

2. **Provider Abstraction**
   - 20+ backends supported
   - Abstraction is clean
   - Fallback mechanisms
   - Easy to extend

3. **Community Momentum**
   - 250+ patterns in main repo
   - Custom pattern support
   - Active contributions
   - Regular feature additions

4. **Simplicity**
   - No complex framework needed
   - Plain markdown patterns
   - Excellent CLI
   - Easy to understand

### 9.2 What Fabric is Not

- **Not a learning system** - Patterns don't improve over time
- **Not a memory system** - No persistent context between uses
- **Not personalized** - Generic patterns for all users
- **Not goal-oriented** - Tasks, not strategies toward objectives
- **Not identity-aware** - Doesn't model who the user is

**This is where PAI + Fabric complement each other perfectly:**
- Fabric provides the pattern library
- PAI provides the learning and personalization layer
- Together: rich capabilities + continuous improvement

---

## Section 10: Implementation Roadmap

### Phase 1: Pattern Integration (Weeks 1-3)
- Select top 50 Fabric patterns
- Create axi skill wrappers
- Build pattern discovery
- Document pattern usage

### Phase 2: Provider Support (Weeks 4-6)
- Implement provider abstraction
- Add OpenAI + Gemini backends
- Create provider selection UI
- Build fallback routing

### Phase 3: Strategy System (Weeks 7-8)
- Integrate strategy framework
- Auto-select based on task type
- Allow user override
- Track strategy effectiveness

### Phase 4: Advanced Features (Weeks 9-10)
- Add URL/YouTube input handling
- Implement pattern composition
- Build context/session system
- Add batch pattern processing

### Phase 5: Community Integration (Weeks 11+)
- Enable custom pattern creation
- Pattern sharing mechanism
- Community pattern registry
- Integration with Fabric community

---

## Critical Success Factors

### What Makes Fabric Powerful

1. **Radical Simplicity**
   - Markdown is universal
   - No complex framework
   - Anyone can create patterns
   - Easy to integrate

2. **Community Validation**
   - Patterns tested in production
   - Real-world use cases
   - Continuous improvement
   - User feedback drives development

3. **Provider Agnosticism**
   - Not tied to one company
   - Use best tool for each task
   - Cost optimization
   - Future-proof

4. **Composability**
   - Patterns can chain together
   - Create complex workflows from simple blocks
   - Unix philosophy (do one thing well)
   - Transparent data flow

### Risks and Considerations

1. **Pattern Quality Varies**
   - Community patterns may not be vetted
   - Need curated subset for axi
   - Regular reviews needed

2. **Learning Curve for Patterns**
   - Users need to understand available patterns
   - Discovery mechanism important
   - Integration must be seamless

3. **Provider Lock-in Risk**
   - Even with abstraction, behaviors differ
   - Model capabilities vary
   - Cost differences significant

---

## Key Takeaways

1. **Fabric is a Pattern Library, Not an AI System**
   - Excellent for: prompts, tasks, composition
   - Not suitable for: learning, memory, personalization
   - Complements but doesn't replace PAI concepts

2. **Patterns are the Unit of Capability**
   - 250+ proven patterns available
   - Community-tested and continuously improving
   - Easy to understand and customize

3. **Provider Abstraction is Mature**
   - 20+ backends supported cleanly
   - Fabric's implementation is solid
   - Can be borrowed/adapted for axi

4. **Strategies Improve Reasoning**
   - Different tasks need different approaches
   - Strategies are well-researched and effective
   - Auto-selection possible based on context

5. **Community is Asset**
   - Large active community
   - Continuous pattern contributions
   - Learning resource for axi users

---

*Research compiled: February 27, 2026*
*Worktree: /home/ubuntu/axi-assistant/.claude/worktrees/research-pai-fabric*
