# Spec Generation Log — axi-py

Started: 2026-03-09
Target: `axi-py`

## Decisions

| # | Timestamp | Step | Decision | Reasoning |
|---|-----------|------|----------|-----------|
| 1 | 2026-03-09 | Discovery | Scope includes axi/, packages/, tests/, and root-level test files | All Python source in axi-py/ |
| 2 | 2026-03-09 | Domains | Dropped Flowcoder domain (9) per user request | "Separate project we happen to develop in this tree" — flowcoder_engine/, flowcoder_flowchart/, flowcoder_tui/, axi/flowcoder.py excluded |
| 3 | 2026-03-09 | Domains | 14 domains confirmed after dropping Flowcoder | Renumbered: 1-8 unchanged, 9=Logging, 10=Discord UI, 11=Shutdown, 12=Config, 13=Hot Restart, 14=Interactive Gates |

## Ambiguities Resolved

| # | Question | Answer (user) | Impact |
|---|----------|---------------|--------|
| 1 | "Is Flowcoder in scope?" | "Drop flowcoder. Separate project, eventually moved out." | Removed domain 9, excluded all flowcoder packages and files |

## Commit Triage

| Hash | Message | Relevant? | Domain | Notes |
|------|---------|-----------|--------|-------|
| 855e603 | Fix smoke test failures: killed channel protection, debug mode visibility, test harness recovery | Yes | 1,2 | I1.3, I1.4, I2.2 |
| c8de2f8 | Fix file upload channel crosstalk bug | Yes | 7 | I7.2 precursor |
| 1396ad4 | Fix rate limit UX: scoped notifications, correct reactions, accurate timing | Yes | 5,10 | I5.1, I5.2, I10.2 |
| 6e33e05 | Fix bridge shutdown hang causing 15s restart delay | Yes | 6,11 | I6.6, I6.7, I11.1, I11.2 |
| c92e56f | Fix /stop and /skip to halt FlowCoder flowchart execution | Partial | 2 | Flowcoder-specific but informed I2.9 |
| 6bba9fe | Fix hot restart: init SDK client before bridge subscribe | Yes | 1,13 | I1.5, I13.1 |
| e7c62bc | Fix FlowCoder agents losing context on sleep/wake | No | — | Flowcoder-specific |
| edf457c | Fix hidden multi-step procedures and FC_QUIET_COMMANDS | No | — | Flowcoder-specific |
| b34badf | Fix /stop for flowcoder: instant stream termination | Partial | 2 | Informed I2.9 multi-layer termination |
| c7267cd | Fix dropped features: last_failed_resume_id and scheduler.release_slot | Yes | 1,3 | I1.13, I1.14, I3.1 |
| 0c726be | Fix engine slash command parsing with timestamp-prefixed messages | No | — | Flowcoder-specific |
| 7ea9ad7 | Fix whole-float rendering in templates | No | — | Flowcoder-specific |
| e21c15e | Fix record-handler getting generic prompt on restart | Yes | 12 | I12.2 |
| aba7f05 | Fix session resume failures: stale session_id cycle | Yes | 1 | I1.13 |
| e1b8466 | Fix channel placement: cwd-based category selection | Yes | 4 | I4.1 |
| b4dd2ae | Fix /stop: use bridge kill instead of SIGINT | Yes | 2 | I2.9 |
| 3d52dd4 | Fix scheduler slot tracking in end_session and reconnect | Yes | 3,13 | I3.1, I3.2, I3.3, I13.3 |
| e8dcfb8 | Fix plan file not found when agents write PLAN.md to CWD | Yes | 14 | I14.2 |
| 707544f | Fix typing indicator dying between agent turns | Yes | 2,10 | I2.4, I10.1 |
| 6ddd6ee | Fix flowcoder engine tool permission handling | No | — | Flowcoder-specific |
| 8d036d1 | Fix remaining discord_request calls from main | Yes | 4 | I4.6 |
| 3099d65 | Fix plan file posting: read from disk fallback | Yes | 14 | I14.3 |
| c7134a3 | Fix CLI token resolution for test worktrees | Yes | 4 | I4.5 |
| f536c52 | Fix agent_type default: claude_code not flowcoder | Yes | 1 | I1.12 |
| 5407b76 | Fix AskUserQuestion multi-question | Yes | 10,14 | I10.3, I14.1 |
| ae1f328 | Fix failed wake: log stderr, clean up stale client | Yes | 1 | I1.6 |
| e2e3073 | Fix record-handler cwd drift from stale channel topic | Yes | 1 | I1.10 |
| 0f15223 | Nuke agent_type/FlowcoderProcess, fix FlowCoderSession on reconnect | No | — | Flowcoder-specific |
| a9b6ce2 | Fix agent disconnect: skip SDK __aexit__ | Yes | 1 | I1.11 |
| 4e05688 | Revert broken pending timeout in record_message | Yes | 3 | I3.6 |
| 5a5e0a9 | Fix schedule system: routing, master tools, history | Yes | 8 | I8.1, I8.5, I8.6 |
| 7e04eb3 | Fix record queue processor: set event on exception | Yes | 3 | I3.7 |
| 46e867c | Fix discord_send_file: require agent_name | Yes | 7 | I7.2 |
| d9eaf35 | Fix scheduler firing all recurring schedules on startup | Yes | 8 | I8.2 |
| c35331c | Fix MCP wiring, discord_send_file, slash command error handling | Yes | 7 | I7.3 |
| 65171f7 | Fix deque.qsize() crash silently breaking all message processing | Yes | 2 | I2.1 |
| 5d15814 | Fix system prompt posting: add to wake_or_queue | Yes | 1 | I1.7 |
| a08f90e | Fix subscribe replay race + add stream tracing | Yes | 2,6 | I2.5, I6.5 |
| 507b575 | Fix system prompt posting on wake + fix interrupt | Yes | 1,2 | I1.8, I2.8 precursor |
| e96a5af | Fix: revert system_prompt on reconstructed agents | Yes | 1 | I1.9 |
| 833b0a4 | Fix reconstructed agents missing system prompt and MCP servers | Yes | 1 | I1.8 |
| 1fbe3da | Fix axi-test slot allocation checking only running instances | Yes | 4 | I4.3 |
| 4e993d0 | Fix systemctl --user failing silently without XDG_RUNTIME_DIR | Yes | 11 | Noted but not a separate invariant |
| f0500fc | Fix subscribe replay race + add stream tracing instrumentation | Yes | 6 | I6.5 |
| 7e63539 | Fix /stop, /skip, timeout: send SDK interrupt after SIGINT | Yes | 2 | I2.10 |
| 8b21e31 | Fix session_id not surviving bot crash for spawned agents | Yes | 2 | I2.7 |
| a109a15 | Fix stale lock in axi_test: auto-clean .env | Yes | 4 | I4.4 |
| cfcfe93 | Fix stop/skip/timeout to kill Task subagents via process group | Yes | 2 | I2.11 |
| 72e0093 | Fix /clear and /compact commands | Yes | 2 | I2.12 |
| 60f8713 | Fix rate limit tracking: preserve utilization | Yes | 5 | I5.3 |
| 3a303af | Fix Haiku /model command race condition | Yes | 3,12 | I3.8, I12.1 |
| 754fb40 | Fix bridge reconnection for resumed agents | Yes | 13 | I13.2 |
| 4d42a17 | Fix flowcoder integration: race condition, engine path, systemd auth | Yes | 1,3 | I1.2, I3.5 (race applies generally) |
| f15c5f5 | Fix bridge readline buffer overflow causing silent query hang | Yes | 2,6 | I2.6, I6.1, I6.2 |
| 900587f | Fix double messages: block MCP self-sends | Yes | 7 | I7.1 |
| 7b724c8 | Fix duplicate messages, bridge crash recovery, instance lock | Yes | 6,11 | I6.3, I6.4, I11.3 |
| d153a2d | Fix visibility_mode not being enforced for initial prompts | Yes | 2 | I2.3 |
| 00eb310 | Fix auto-switch delay by processing spawn signal immediately | Yes | 3 | I3.4 |
| d850d70 | Fix timezone-aware datetimes | Yes | 8 | I8.3 |
| b545ac4 | Fix morning checkin cron timezone | Yes | 8 | I8.4 |
| 3b091a9 | Fix interrupt_session: send SIGINT instead of kill | Yes | 2 | I2.8 |
| 32ede9a | Protect session continuity during flowchart execution | No | — | Flowcoder-specific |
| 9c3bed2 | Fix agent respawn failure: Discord channel-edit rate limit | Yes | 1,4 | I1.1, I4.2 |
| 1c1c04a | Fix supervisor file paths, shutdown docstring | Yes | 11,12 | I11.4, I12.3 |
| a7f952d | Fix incorrect logger variable name | Yes | 9 | I9.1 |
| ec3059e | Fix args quoting for wrapped FC commands | No | — | Flowcoder-specific |
| df2ba70 | Strip timestamp prefix before FlowCoder command check | No | — | Flowcoder-specific |
| 44ed6ac | Remove .llm_cache from tracking | No | — | Housekeeping |

## Coverage Gaps

| # | Observation | Status |
|---|-------------|--------|
| 1 | ~~No spec coverage for discordquery/~~ | Covered — added as Domain 15 |
| 2 | No spec coverage for stdio-spy/ package | Noted — debug/dev tool |
| 3 | No spec coverage for web_chat_store.py (SQLite web frontend storage) | Noted — secondary frontend, WEB_ENABLED feature flag |
| 4 | Test files not surveyed for behavioral assertions | Noted — tests validate but don't define behavior |

## Changes from Previous Spec

| # | Code | Change | Reason |
|---|------|--------|--------|
| — | All | NEW | No previous SPEC.md existed |
