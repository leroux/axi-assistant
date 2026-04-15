# Tailscale Network Access

Tailscale IPs (the 100.x.x.x CGNAT range) are blocked by the Bash sandbox. Use the Tailscale-restricted wrapper commands instead:

- **`ts-ssh`** instead of `ssh` — same arguments, but only allows connections to 100.x.x.x IPs
- **`ts-curl`** instead of `curl` — same arguments, but only allows requests to 100.x.x.x IPs

These wrappers run outside the sandbox (via `excludedCommands`) but validate that every target is a Tailscale IP before executing. If the target is not in the 100.x.x.x range, the command is blocked.

## Rules

- **Run `hostname` to know which machine you are on.** Don't assume — check before trying to SSH to a machine you might already be on.
- **Always use `ts-ssh`/`ts-curl`** for Tailscale access. Do not use regular `ssh`/`curl` — they are sandboxed and will fail on network access.
- **Do not use `dangerouslyDisableSandbox`** — it is disabled and has no effect.

## Machine details

Specific Tailscale IPs, hostnames, and machine roles are in the user profile refs (`profile/refs/tech.md`), not in this extension.
