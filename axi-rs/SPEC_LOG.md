# Spec Generation Log — axi-rs

Started: 2026-03-09
Target: `axi-rs`

## Decisions

| # | Timestamp | Step | Decision | Reasoning |
|---|-----------|------|----------|-----------|
| 1 | 2026-03-09 | Discovery | Scope is all crates under axi-rs/crates/, excluding vendor/songbird | Vendor is a third-party dependency, not project code |
| 2 | 2026-03-09 | Domains | Match axi-py SPEC.md domain numbering for cross-spec traceability | User preference; both codebases implement same bot |
| 3 | 2026-03-09 | Domains | Skip domain 9 (Logging & Observability) — only telemetry init in main.rs | Trivial in Rust; no behavioral content |
| 4 | 2026-03-09 | Domains | Skip domain 15 (Discord Query Client) — single-file utility | Trivial standalone tool |
| 5 | 2026-03-09 | Domains | Add domain 16 (MCP Tools & Protocol) — larger in Rust than Python | Rust has dedicated mcp_tools.rs, mcp_protocol.rs, mcp_schedule.rs |
| 6 | 2026-03-09 | Domains | Add domain 17 (Flowchart Execution) — 4 Rust crates, no Python equivalent | flowchart, flowchart-runner, flowcoder-engine, flowcoder |
| 7 | 2026-03-09 | Domains | Add domain 18 (Voice I/O) — axi-voice crate, WIP but has fix commits | Includes TTS, STT, playback, voice gateway |

## Ambiguities Resolved

| # | Question | Answer (user) | Impact |
|---|----------|---------------|--------|
| 1 | Should axi-rs domains align with axi-py SPEC.md? | Yes, match numbering | Domain numbers 1-14 match axi-py; 16-18 are Rust-only additions |

## Commit Triage

| Hash | Message | Relevant? | Domain | Notes |
|------|---------|-----------|--------|-------|
| 855e603 | Fix smoke test failures (killed channel, debug mode, wake_or_queue race, startup drain) | Yes | 1,2,6,7,17 | 7 distinct behavioral fixes in one commit |
| f35e47d | Fix stream_event unwrapping in bridge stream handler | Yes | 2 | Streaming content silently dropped due to missing unwrap |
| c16f599 | Fix procmux service to use AXI_RS_BINARY | Yes | 6 | Hardcoded path broke debug builds |
| 6fad00c | Fix systemd ExecStart needs shell wrapper | Yes | 6 | Systemd doesn't expand env vars |
| 628262e | Prevent interrupts during context compaction | Yes | 2 | Compaction is atomic; interrupts corrupt it |
| 4b06483 | Collapse crates + security hardening | Yes | 7 | Permission timeout deny, name validation, token redaction, path traversal |
| 9a0dc4e | Replace supervisor with systemd + bridge monitor | Yes | 6,11 | Bridge monitor exits code 42 on procmux death |
| 29af831 | Wire SDK MCP servers to Claude CLI via control protocol | Yes | 7,16 | MCP servers survive session rebuilds |
| c2475a9 | Decouple axi-voice: channel-based API | Yes | 18 | Voice library has no host knowledge |
| 339015c | Add Piper TTS + fix voice pipeline deadlocks | Yes | 18,2 | Transcript drops, double-lock deadlock, TTS overlap |
| 221849d | Add missing protocol types to claudewire schema | Yes | 6 | Forward-compatible deserialization |
| 64831ba | Replace BridgeTransport with CliSession, add awaiting-input sentinel | Yes | 1 | Sentinel message for ready-for-input detection |
| e6f8f75 | Multi-frontend architecture | Yes | 10,14 | Interactive gates race across frontends |
| 6de3211 | Scheduler and crash handler | Yes | 8 | Recurring schedules skip first firing; crash markers routed |
| 82503f7 | Add 32 integration tests, extract pure functions | Yes | 2 | Auto-compact threshold logic validated |
| 8fcff49 | Fix stale docs in claudewire | No | — | Documentation only |

## Coverage Gaps

| # | Observation | Status |
|---|-------------|--------|
| 1 | No fix commits found for Concurrency & Slot Management (domain 3) | No invariants to mine |
| 2 | No fix commits found for Channel & Guild Management (domain 4) | No invariants to mine |
| 3 | No fix commits found for Rate Limiting (domain 5) | No invariants to mine |
| 4 | Domain 10 anchors reference frontend_router.rs / discord_frontend.rs / web_frontend.rs but agent read frontend.rs — file may have been split | Anchors may need updating |
| 5 | Some invariants appear in multiple domains (I7.6/I16.1, I6.6/I16.2/I17.3) — intentional cross-referencing | Documented in both domains |

## Changes from Previous Spec

No previous SPEC.md found — generating from scratch.

## Final Stats

- **16 domains** covered (skipped 9, 15)
- **170 behaviors** (B-codes)
- **41 invariants** (I-codes) from 15 fix commits
- **8 domain subagents** ran in parallel
- Completed: 2026-03-09
