# Axi Assistant — Project Rules

## Test Instance Safety

- **NEVER tear down or stop a test instance created by others without explicit user approval.** Instances may be in active use by other agents or the user. Always ask first.
- When `axi_test.py up` fails because all bot tokens are in use, **do not** automatically tear down an existing instance to free a slot. Ask the user how to proceed.
- **Always tear down your own test instances** after you're done with them so the slot is available for other agents.
