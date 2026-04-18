<!-- test: tests/test_emoji_checkmark_reaction_generated.py -->

# Axi Adds Checkmark Reaction

## Application Overview

When Axi processes a user message in `#axi-master`, it adds a unicode checkmark reaction (e.g. `✅`) to the user's message once the response is complete. This is a fast visual confirmation that the bot saw and finished handling the message, without the user having to read the reply.

## Scenarios

### 1. Bot reacts to a processed user message

**Steps:**
1. In `#axi-master`, send the message `Say exactly: REACTION_CHECK_<timestamp>` and capture the sent message ID.
2. Wait (up to 60 seconds) for the bot to respond in the channel.
3. After the bot replies, sleep briefly (about 2 seconds), then GET the original user message and read its `reactions` list.

**Expected Results:**
- The user message has at least one reaction whose emoji name is one of: `✅`, `☑️`, `☑`, `✓`, `white_check_mark`.
