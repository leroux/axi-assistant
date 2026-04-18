<!-- test: tests/test_readme_channel_has_content_generated.py -->

# Axi #readme Channel Is Populated

## Application Overview

Axi syncs a human-readable `#readme` channel in every guild it's a member of. The readme describes the bot's commands, its conventions, and how to interact with it. The channel should exist at all times and should contain a non-trivial amount of content, because it's the first thing a new user sees.

## Scenarios

### 1. #readme channel exists and has content

**Steps:**
1. Query the test guild's channel list (via the Discord adapter) for a channel named `readme`.
2. Fetch the last 5 messages from that channel.

**Expected Results:**
- Step 1: A channel named `readme` is found.
- Step 2: The channel has at least one message, and the concatenated content is longer than 20 characters (a minimum signal that it is not empty).
