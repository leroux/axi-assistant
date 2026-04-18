<!-- test: tests/test_basic_echo_master_generated.py -->

# Axi Master Basic Echo

## Application Overview

Axi's master agent lives in `#axi-master` and answers natural-language prompts directly. The simplest possible liveness check is that the master echoes a deterministic sentinel back when asked to. This guards the end-to-end path from Discord → bot → Claude Code agent → Discord and is the foundation other tests build on.

## Scenarios

### 1. Master echoes a deterministic sentinel

**Steps:**
1. In `#axi-master`, send the message: `Say exactly: ECHO_OK_<timestamp>` where `<timestamp>` is a unique millisecond-resolution marker.
2. Wait (up to 120 seconds) for the master to produce a response and drop its "awaiting input" sentinel.

**Expected Results:**
- The master's response text contains the exact sentinel `ECHO_OK_<timestamp>` that was requested.
- No exception or error banner appears in the response.
