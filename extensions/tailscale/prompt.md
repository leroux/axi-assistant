# Tailscale Network Access — Sandbox Policy

Tailscale IPs (the 100.x.x.x CGNAT range) are blocked by the Claude Code sandbox by default. This affects any command that needs to reach a machine on the Tailscale network.

## When to disable the sandbox

When a command targets a Tailscale IP — SSH, HTTP requests, ping, curl, scp, rsync, or any other network call to a 100.x.x.x address — you **must** use `dangerouslyDisableSandbox: true` on that Bash tool call.

## Rules

- **Always explain why.** Before or alongside the tool call, tell the user: "Disabling sandbox to reach Tailscale IP." Do not silently disable it.
- **Scope it narrowly.** Only disable the sandbox for the specific command that needs Tailscale access. Re-enable it (default behavior) for subsequent commands that don't need it.
- **No blanket overrides.** Do not disable the sandbox preemptively for an entire sequence of commands. Each command that needs Tailscale access gets its own explicit override.

## What this applies to

- `ssh`, `scp`, `rsync` to Tailscale hosts
- `curl`, `wget`, `httpx` to Tailscale IPs/hostnames
- `ping`, `nc`, `nmap` to Tailscale addresses
- Any tool or script that opens a network connection to a 100.x.x.x address

## Machine details

Specific Tailscale IPs, hostnames, and machine roles are in the user profile refs (`profile/refs/tech.md`), not in this extension.
