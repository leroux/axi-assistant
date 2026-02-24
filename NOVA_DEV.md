# Bot Development Guide

## Instances

| | Prime | Nova |
|---|---|---|
| Path | `/home/ubuntu/axi-assistant` | `/home/ubuntu/axi-nova` |
| Branch | `main` | `nova` |
| Service | `axi-bot.service` | `axi-nova.service` |
| User data | `/home/ubuntu/axi-user-data` | `/home/ubuntu/axi-nova-user-data` |

Nova is a git worktree of Prime on the `nova` branch. They share git history but have separate `.env`, `.venv`, and systemd services. Nova runs in its own Discord server.

## Dev Workflow

Same for either instance — just swap the paths and service name.

### 1. Edit & commit

```bash
# edit files in the instance directory, then:
cd /home/ubuntu/<instance>
git add -p && git commit -m "description"
```

### 2. Restart

```bash
systemctl --user restart <service>
```

### 3. Check logs

```bash
journalctl --user -u <service> -f                    # live tail
journalctl --user -u <service> --since "5 min ago"   # recent
```

### 4. Verify

Interact with the bot in its Discord server and confirm the change works.

## Syncing Between Branches

```bash
# Propagate main changes to nova
cd /home/ubuntu/axi-nova && git merge main

# Promote nova changes to main (after testing)
cd /home/ubuntu/axi-assistant && git cherry-pick <hash>
# or: git merge nova
```

## Rules for Prime Self-Modification

**Prime must never modify itself directly.** When Prime (the bot) is making code changes autonomously, it must:

1. Apply changes to Nova first (`/home/ubuntu/axi-nova`, `nova` branch)
2. Restart Nova and verify the change works
3. Only then merge/cherry-pick from `nova` into `main` and restart Prime

This prevents Prime from bricking itself with a bad commit. Nova is the crash-safe sandbox — it has `DISABLE_CRASH_HANDLER=1` and its own Discord server, so a broken deploy has zero impact on Prime.

**Humans using Claude Code** can edit Prime directly — the supervisor auto-rollbacks on quick crashes, and you can always fix things manually.
