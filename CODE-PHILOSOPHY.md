# Code Philosophy

This document captures the software development philosophy, principles, and mental models that guide decisions in this codebase.

## Core Influences

- **Data-Oriented Design** (Mike Acton) — Understanding the problem domain first, structure data around actual usage patterns
- **Mechanical Sympathy** (Martin Thompson) — Code that understands hardware constraints: cache lines, memory layout, CPU behavior
- **Handmade Philosophy** (Casey Muratori) — Deep understanding of systems, explicit implementation, avoiding unnecessary abstractions
- **Linear/Algorithmic Thinking** — Preference for straightforward, predictable algorithms over complex patterns
- **Functional Programming Ideas** (pragmatic, not dogmatic) — Use FP concepts when they serve the problem

## What We Value

- **Explicitness over convention** — Code should be clear about what it does, not hidden behind abstractions
- **Performance awareness** — Not premature optimization, but understanding the cost of your choices
- **Simplicity as a first principle** — Small functions are fine *when needed*, not as a default pattern
- **Clear data flow** — How data moves through the system should be obvious
- **Pragmatism over purity** — Use FP concepts (immutability, composition, pure functions) when they serve the problem, not as dogma

## What We Reject

- **OO's hidden state** — Polymorphism, inheritance hierarchies that obscure what's actually happening
- **Over-abstraction** — Ten layers of indirection to avoid repeating three lines of code
- **Function proliferation** — Small functions for their own sake; functions should earn their existence
- **Convention-driven design** — "We do it this way because that's the pattern" without understanding why

## Our Approach

### Understand the Data First
What does it look like? How is it accessed? What are the hot paths? Data structure informs everything else.

### Organize by Concern/Domain
Not by OO classes, but by what the code actually *does*. Vertical slicing preferred over layer-based organization.

### Make Tradeoffs Visible
If you're trading memory for speed, or clarity for performance, *say so*. Explicit choices are better than hidden ones.

### Linear, Readable Code
Prefer following a straightforward path over clever abstractions. Code should tell a story.

### Composability Without Ceremony
Functions and modules work together, but without unnecessary boilerplate or design pattern overhead.

## Related Principles

- **Locality of Reference** — Related code and data should be near each other (physically and conceptually)
- **Testability Through Simplicity** — If code is hard to test, the design might be wrong
- **Explicit Error Handling** — Not exceptions hiding control flow
- **YAGNI (You Aren't Gonna Need It)** — Don't build for hypothetical futures
- **Vertical Slicing** — Features organized end-to-end, not by architectural layer

## Guidelines for This Codebase

When making design decisions, ask:

1. **Do I understand the problem domain?** (Mike Acton's first principle)
2. **Is this choice visible and explicit?** (Not hiding complexity behind abstraction)
3. **Does this serve the actual problem?** (Not pattern-driven)
4. **Am I aware of the hardware/performance implications?** (Mechanical sympathy)
5. **Could I explain this to someone without jargon?** (Simplicity test)

If a design choice requires defending "because that's the pattern," reconsider it.
