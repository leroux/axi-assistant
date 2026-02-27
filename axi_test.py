#!/usr/bin/env python3
"""CLI for managing Axi test instance bot/guild reservations.

Handles Discord bot token and guild slot allocation. Agents are responsible
for worktree creation, dependency installation, and service management.

Usage:
    axi-test up <name> [--guild GUILD] [--wait] [--wait-timeout SECS]
    axi-test down <name>
    axi-test restart <name>
    axi-test list
    axi-test merge [-m MSG] [--timeout SECS]
    axi-test queue [show|drop] [--all]
    axi-test msg <name> <message> [--timeout SECS]
    axi-test logs <name>
"""
import argparse
import fcntl
import json
import os
import subprocess
import sys
import time
from contextlib import contextmanager
from datetime import datetime, timezone

import httpx
from dotenv import dotenv_values

TESTS_DIR = "/home/ubuntu/axi-tests"
CONFIG_PATH = os.path.expanduser("~/.config/axi/test-config.json")
API_BASE = "https://discord.com/api/v10"
SENTINEL = "Bot has finished responding"


def _systemctl_env() -> dict[str, str]:
    """Return environment with XDG_RUNTIME_DIR set for systemctl --user.

    Without this, systemctl --user silently fails when called from
    environments that don't inherit the variable (e.g. sandboxed agents).
    """
    env = os.environ.copy()
    if "XDG_RUNTIME_DIR" not in env:
        env["XDG_RUNTIME_DIR"] = f"/run/user/{os.getuid()}"
    return env


def load_config() -> dict:
    """Load and validate test-config.json."""
    try:
        with open(CONFIG_PATH) as f:
            config = json.load(f)
    except FileNotFoundError:
        print(f"Error: Config not found: {CONFIG_PATH}", file=sys.stderr)
        sys.exit(1)
    except json.JSONDecodeError as e:
        print(f"Error: Invalid JSON in {CONFIG_PATH}: {e}", file=sys.stderr)
        sys.exit(1)

    for key in ("bots", "guilds"):
        if key not in config:
            print(f"Error: Config missing required key '{key}'", file=sys.stderr)
            sys.exit(1)
    return config


def resolve_bot_token(config: dict, guild_name: str) -> str:
    """Resolve guild name -> bot name -> token."""
    guild = config["guilds"].get(guild_name)
    if not guild:
        print(f"Error: Guild '{guild_name}' not found in config", file=sys.stderr)
        sys.exit(1)
    bot_name = guild.get("bot")
    bot = config["bots"].get(bot_name, {})
    token = bot.get("token")
    if not token:
        print(f"Error: No token found for bot '{bot_name}' (guild '{guild_name}')", file=sys.stderr)
        sys.exit(1)
    return token


def is_instance_running(name: str) -> bool:
    """Check if a test instance systemd service is active.

    Fails hard if systemctl can't reach the user bus — silently returning
    False in that case would mask running instances and corrupt reservations.
    """
    result = subprocess.run(
        ["systemctl", "--user", "is-active", f"axi-test@{name}"],
        capture_output=True, text=True, env=_systemctl_env(),
    )
    stderr = result.stderr.strip()
    if "Failed to connect to bus" in stderr or "No medium found" in stderr:
        print(f"Error: Cannot reach systemd user bus: {stderr}", file=sys.stderr)
        print("Cannot determine instance state — refusing to continue.", file=sys.stderr)
        sys.exit(1)
    return result.stdout.strip() == "active"


def cleanup_orphan_services() -> int:
    """Stop and reset orphaned axi-test@ services.

    Finds user-level axi-test@ units that are crash-looping or failed
    because their worktree directory or .env is missing. Returns count
    of services cleaned up.
    """
    # List all axi-test@ units (any state)
    env = _systemctl_env()
    result = subprocess.run(
        ["systemctl", "--user", "list-units", "--all", "--plain",
         "--no-legend", "axi-test@*"],
        capture_output=True, text=True, env=env,
    )
    cleaned = 0
    for line in result.stdout.strip().splitlines():
        if not line.strip():
            continue
        unit = line.split()[0]
        # Extract instance name: "axi-test@foo.service" -> "foo"
        if not unit.startswith("axi-test@") or not unit.endswith(".service"):
            continue
        name = unit[len("axi-test@"):-len(".service")]

        instance_path = os.path.join(TESTS_DIR, name)
        env_path = os.path.join(instance_path, ".env")

        # Orphan if: directory missing, or .env missing (no reservation)
        if os.path.isdir(instance_path) and os.path.isfile(env_path):
            continue  # Looks legitimate, skip

        # Stop and reset
        subprocess.run(
            ["systemctl", "--user", "stop", unit],
            capture_output=True, env=env,
        )
        subprocess.run(
            ["systemctl", "--user", "reset-failed", unit],
            capture_output=True, env=env,
        )
        print(f"Cleaned up orphan service: {unit}")
        cleaned += 1

    return cleaned


def get_running_instances() -> list[str]:
    """Get names of all running test instances."""
    running = []
    if not os.path.isdir(TESTS_DIR):
        return running
    for entry in os.listdir(TESTS_DIR):
        path = os.path.join(TESTS_DIR, entry)
        if os.path.isdir(path) and not entry.endswith("-data"):
            if is_instance_running(entry):
                running.append(entry)
    return running


def get_allocated_instances() -> list[str]:
    """Get names of all instances with a .env reservation (running or stopped)."""
    allocated = []
    if not os.path.isdir(TESTS_DIR):
        return allocated
    for entry in os.listdir(TESTS_DIR):
        path = os.path.join(TESTS_DIR, entry)
        if os.path.isdir(path) and not entry.endswith("-data"):
            env_path = os.path.join(path, ".env")
            if os.path.isfile(env_path):
                allocated.append(entry)
    return allocated


def get_instance_env(name: str) -> dict:
    """Read a test instance's .env file."""
    env_path = os.path.join(TESTS_DIR, name, ".env")
    if not os.path.isfile(env_path):
        return {}
    return dotenv_values(env_path)


def get_token_for_running_instance(name: str, config: dict) -> str | None:
    """Get the bot token used by a running instance."""
    env = get_instance_env(name)
    return env.get("DISCORD_TOKEN")


def pick_guild(config: dict, explicit_guild: str | None) -> str | None:
    """Pick a guild, checking for token conflicts with allocated instances.

    Returns None if no guild slot is available (all tokens in use).
    A slot is considered "in use" if any instance has a .env file with that
    bot token — even if the instance is currently stopped.
    """
    if explicit_guild:
        if explicit_guild not in config["guilds"]:
            print(f"Error: Guild '{explicit_guild}' not found in config", file=sys.stderr)
            print(f"Available guilds: {', '.join(config['guilds'].keys())}", file=sys.stderr)
            sys.exit(1)
        return explicit_guild

    # Pick first guild whose bot has no allocated instance
    allocated = get_allocated_instances()
    allocated_tokens = set()
    for inst in allocated:
        token = get_token_for_running_instance(inst, config)
        if token:
            allocated_tokens.add(token)

    for guild_name, guild_info in config["guilds"].items():
        bot_name = guild_info.get("bot")
        token = config["bots"].get(bot_name, {}).get("token")
        if token and token not in allocated_tokens:
            return guild_name

    return None


def has_token_conflict(config: dict, guild_name: str, instance_name: str) -> bool:
    """Check if another allocated instance uses the same bot token."""
    token = resolve_bot_token(config, guild_name)
    allocated = get_allocated_instances()
    for inst in allocated:
        if inst == instance_name:
            continue
        inst_token = get_token_for_running_instance(inst, config)
        if inst_token == token:
            return True
    return False


def check_token_conflict(config: dict, guild_name: str, instance_name: str) -> None:
    """Error if another allocated instance uses the same bot token."""
    if not has_token_conflict(config, guild_name, instance_name):
        return
    token = resolve_bot_token(config, guild_name)
    allocated = get_allocated_instances()
    for inst in allocated:
        if inst == instance_name:
            continue
        inst_token = get_token_for_running_instance(inst, config)
        if inst_token == token:
            bot_name = config["guilds"][guild_name].get("bot")
            print(f"Error: Bot '{bot_name}' is already allocated to instance '{inst}'", file=sys.stderr)
            print(f"Release it first: axi-test down {inst}", file=sys.stderr)
            sys.exit(1)


def get_worktree_branch(worktree_path: str) -> str:
    """Get the branch name for a worktree."""
    result = subprocess.run(
        ["git", "branch", "--show-current"],
        cwd=worktree_path, capture_output=True, text=True,
    )
    branch = result.stdout.strip()
    if branch:
        return branch
    # Detached HEAD — show short hash
    result = subprocess.run(
        ["git", "rev-parse", "--short", "HEAD"],
        cwd=worktree_path, capture_output=True, text=True,
    )
    return result.stdout.strip() or "unknown"


# --- Merge Queue ---


def _find_main_repo() -> str:
    """Find the main repo path via git's common dir."""
    result = subprocess.run(
        ["git", "rev-parse", "--path-format=absolute", "--git-common-dir"],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        print("Error: Not inside a git repository", file=sys.stderr)
        sys.exit(1)
    return os.path.dirname(result.stdout.strip())


def _queue_file(main_repo: str) -> str:
    return os.path.join(main_repo, ".merge-queue.json")


def _queue_lock(main_repo: str) -> str:
    return os.path.join(main_repo, ".merge-queue.lock")


def _merge_lock_file(main_repo: str) -> str:
    return os.path.join(main_repo, ".merge-exec.lock")


@contextmanager
def _flock(path: str):
    """Acquire exclusive file lock, release on exit."""
    fd = os.open(path, os.O_CREAT | os.O_WRONLY, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def _read_queue(main_repo: str) -> list[dict]:
    """Read queue file. Caller must hold queue lock."""
    path = _queue_file(main_repo)
    if not os.path.isfile(path):
        return []
    with open(path) as f:
        try:
            return json.load(f)
        except json.JSONDecodeError:
            return []


def _write_queue(main_repo: str, entries: list[dict]) -> None:
    """Write queue file. Caller must hold queue lock."""
    with open(_queue_file(main_repo), "w") as f:
        json.dump(entries, f, indent=2)


def _cleanup_stale(entries: list[dict]) -> None:
    """Remove entries with dead processes. Modifies list in place."""
    now = datetime.now(timezone.utc)
    to_remove = []
    for i, entry in enumerate(entries):
        pid = entry.get("pid")
        if not pid:
            continue
        try:
            os.kill(pid, 0)
            # Process alive — check heartbeat staleness as backup
            heartbeat_s = entry.get("heartbeat", "")
            submitted_s = entry.get("submitted_at", "")
            if heartbeat_s and submitted_s:
                heartbeat = datetime.fromisoformat(heartbeat_s)
                submitted = datetime.fromisoformat(submitted_s)
                if (now - heartbeat).total_seconds() > 60 and (now - submitted).total_seconds() > 600:
                    to_remove.append(i)
        except ProcessLookupError:
            to_remove.append(i)
        except PermissionError:
            pass  # Process exists but we can't signal it
    for i in reversed(to_remove):
        removed = entries.pop(i)
        print(f"Removed stale queue entry: {removed.get('branch', '?')} (pid {removed.get('pid', '?')})")


def _remove_from_queue(main_repo: str, branch: str) -> None:
    """Remove a branch from the queue."""
    with _flock(_queue_lock(main_repo)):
        entries = _read_queue(main_repo)
        entries = [e for e in entries if e["branch"] != branch]
        _write_queue(main_repo, entries)


def _git(main_repo: str, *args: str) -> subprocess.CompletedProcess:
    """Run a git command in the main repo."""
    return subprocess.run(
        ["git", "-C", main_repo, *args],
        capture_output=True, text=True,
    )


def _execute_merge(main_repo: str, branch: str, message: str | None = None) -> tuple[str, str]:
    """Execute squash merge. Returns (status, detail).

    status: "merged", "needs_rebase", or "error"
    detail: commit SHA on success, or error message on failure.
    """
    # Verify main repo is on 'main' branch
    current = _git(main_repo, "branch", "--show-current")
    if current.stdout.strip() != "main":
        return ("error", f"main repo is on branch '{current.stdout.strip()}', expected 'main'")

    # Pre-merge cleanup: if index is dirty from interrupted merge, reset
    if _git(main_repo, "diff", "--cached", "--quiet").returncode != 0:
        print("Cleaning up dirty index from interrupted merge...")
        r = _git(main_repo, "reset", "--hard", "HEAD")
        if r.returncode != 0:
            return ("error", f"failed to clean dirty index: {r.stderr.strip()}")

    # Fast-forward check: merge-base must equal main HEAD
    merge_base_r = _git(main_repo, "merge-base", "main", branch)
    if merge_base_r.returncode != 0:
        return ("error", f"failed to compute merge-base: {merge_base_r.stderr.strip()}")

    main_head_r = _git(main_repo, "rev-parse", "main")
    if main_head_r.returncode != 0:
        return ("error", f"failed to get main HEAD: {main_head_r.stderr.strip()}")

    merge_base = merge_base_r.stdout.strip()
    main_head = main_head_r.stdout.strip()

    if merge_base != main_head:
        return ("needs_rebase", f"merge-base {merge_base[:8]} != main HEAD {main_head[:8]}")

    # Check branch has commits beyond main
    log_r = _git(main_repo, "log", "--oneline", f"main..{branch}")
    if not log_r.stdout.strip():
        return ("error", "no commits to merge — branch is identical to main")

    # Collect commit messages before squashing
    msg_r = _git(main_repo, "log", "--format=- %s", f"main..{branch}")
    commit_log = msg_r.stdout.strip()

    # Squash merge
    merge_r = _git(main_repo, "merge", "--squash", branch)
    if merge_r.returncode != 0:
        _git(main_repo, "reset", "--hard", "HEAD")
        return ("error", f"squash merge failed: {merge_r.stderr.strip()}")

    # Build commit message
    if message:
        commit_msg = message
    else:
        commit_msg = branch
        if commit_log:
            commit_msg += f"\n\n{commit_log}"

    # Commit
    commit_r = _git(main_repo, "commit", "-m", commit_msg)
    if commit_r.returncode != 0:
        _git(main_repo, "reset", "--hard", "HEAD")
        return ("error", f"commit failed: {commit_r.stderr.strip()}")

    sha_r = _git(main_repo, "rev-parse", "--short", "HEAD")
    return ("merged", sha_r.stdout.strip())


# --- Subcommands ---

def wait_for_slot(config, explicit_guild, instance_name, timeout, poll_interval=10):
    """Poll until a guild slot is available, or raise SystemExit on timeout.

    Returns the guild name once a slot is free.
    """
    deadline = time.monotonic() + timeout
    total_guilds = len(config["guilds"])
    if explicit_guild:
        bot_name = config["guilds"][explicit_guild].get("bot", "?")
        print(f"Bot '{bot_name}' (guild '{explicit_guild}') is in use. Waiting for it to free up (timeout: {timeout}s)...")
    else:
        print(f"All {total_guilds} bot token(s) are in use. Waiting for a slot (timeout: {timeout}s)...")

    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            print(f"\nCould not reserve a bot token slot after waiting {timeout}s.", file=sys.stderr)
            print("All bot tokens are still in use. Please ask the user how to proceed.", file=sys.stderr)
            sys.exit(1)

        sleep_time = min(poll_interval, remaining)
        time.sleep(sleep_time)

        guild_name = pick_guild(config, explicit_guild)
        if guild_name is not None and not has_token_conflict(config, guild_name, instance_name):
            print(f"Slot available! Using guild '{guild_name}'")
            return guild_name

        mins_left = int(remaining) // 60
        secs_left = int(remaining) % 60
        print(f"  Still waiting... ({mins_left}m {secs_left}s remaining)")


def cmd_up(args):
    # Clean up orphaned services before reserving a slot
    cleanup_orphan_services()

    config = load_config()
    name = args.name
    instance_path = os.path.join(TESTS_DIR, name)
    data_path = os.path.join(TESTS_DIR, f"{name}-data")

    if os.path.isfile(os.path.join(instance_path, ".env")):
        if is_instance_running(name):
            print(f"Error: Instance '{name}' is already running", file=sys.stderr)
            print(f"Run 'axi-test down {name}' first, or choose a different name", file=sys.stderr)
            sys.exit(1)
        else:
            print(f"Cleaning up stale reservation for '{name}' (not running, .env left behind)")
            subprocess.run(
                ["systemctl", "--user", "stop", f"axi-test@{name}"],
                capture_output=True, env=_systemctl_env(),
            )
            os.remove(os.path.join(instance_path, ".env"))

    guild_name = pick_guild(config, args.guild)

    if guild_name is None:
        if args.wait:
            guild_name = wait_for_slot(
                config, args.guild, name, timeout=args.wait_timeout,
            )
        else:
            print("Error: All bot tokens are in use by running instances", file=sys.stderr)
            print("Either stop an instance or add another bot to the config", file=sys.stderr)
            print("Hint: Use --wait to poll until a slot is available", file=sys.stderr)
            sys.exit(1)

    # For explicit guild, the token might still be in use by another instance
    if has_token_conflict(config, guild_name, name):
        if args.wait:
            guild_name = wait_for_slot(
                config, args.guild, name, timeout=args.wait_timeout,
            )
        else:
            check_token_conflict(config, guild_name, name)

    guild_info = config["guilds"][guild_name]
    guild_id = guild_info["guild_id"]
    token = resolve_bot_token(config, guild_name)
    defaults = config.get("defaults", {})

    # Write .env (instance directory must already exist)
    os.makedirs(instance_path, exist_ok=True)
    env_content = (
        f"DISCORD_TOKEN={token}\n"
        f"DISCORD_GUILD_ID={guild_id}\n"
        f"ALLOWED_USER_IDS={defaults.get('allowed_user_ids', '')}\n"
        f"SCHEDULE_TIMEZONE={defaults.get('schedule_timezone', 'UTC')}\n"
        f"DEFAULT_CWD={instance_path}\n"
        f"AXI_USER_DATA={data_path}\n"
        f"DAY_BOUNDARY_HOUR={defaults.get('day_boundary_hour', '0')}\n"
        f"SHOW_AWAITING_INPUT=true\n"
    )
    with open(os.path.join(instance_path, ".env"), "w") as f:
        f.write(env_content)

    # Create data directory with empty schedule files
    os.makedirs(data_path, exist_ok=True)
    for fname in ("schedules.json", "schedule_history.json"):
        fpath = os.path.join(data_path, fname)
        if not os.path.exists(fpath):
            with open(fpath, "w") as f:
                json.dump([], f)

    print(f"Reserved guild '{guild_name}' ({guild_id}) for instance '{name}'")
    print(f"  .env:  {instance_path}/.env")
    print(f"  Data:  {data_path}")


def cmd_down(args):
    name = args.name
    instance_path = os.path.join(TESTS_DIR, name)
    data_path = os.path.join(TESTS_DIR, f"{name}-data")
    env_path = os.path.join(instance_path, ".env")

    if not os.path.isfile(env_path):
        print(f"Error: No reservation found for '{name}' (no .env)", file=sys.stderr)
        sys.exit(1)

    # Stop the systemd service if it's still running
    if is_instance_running(name):
        print(f"Stopping axi-test@{name}...")
        subprocess.run(
            ["systemctl", "--user", "stop", f"axi-test@{name}"],
            check=True, env=_systemctl_env(),
        )

    # Remove .env to release the reservation
    os.remove(env_path)

    print(f"Released reservation for instance '{name}'")


def cmd_restart(args):
    name = args.name
    print(f"Restarting axi-test@{name}...")
    subprocess.run(
        ["systemctl", "--user", "restart", f"axi-test@{name}"],
        check=True, env=_systemctl_env(),
    )
    print("Done")


def cmd_list(args):
    config = load_config()
    if not os.path.isdir(TESTS_DIR):
        print("No test instances found")
        return

    # Build guild_id -> guild_name map
    id_to_guild = {}
    for gname, ginfo in config["guilds"].items():
        id_to_guild[ginfo["guild_id"]] = gname

    rows = []
    for entry in sorted(os.listdir(TESTS_DIR)):
        path = os.path.join(TESTS_DIR, entry)
        if not os.path.isdir(path) or entry.endswith("-data"):
            continue

        env = get_instance_env(entry)
        if not env:
            continue  # No reservation

        guild_id = env.get("DISCORD_GUILD_ID", "?")
        guild_name = id_to_guild.get(guild_id, "?")
        status = "running" if is_instance_running(entry) else "stopped"
        branch = get_worktree_branch(path) if os.path.isdir(os.path.join(path, ".git")) or os.path.isfile(os.path.join(path, ".git")) else "-"

        rows.append((entry, guild_name, branch, status, guild_id))

    if not rows:
        print("No test instances found")
        return

    # Print table
    headers = ("NAME", "GUILD", "BRANCH", "STATUS", "GUILD_ID")
    widths = [max(len(h), max(len(r[i]) for r in rows)) for i, h in enumerate(headers)]
    fmt = "  ".join(f"{{:<{w}}}" for w in widths)
    print(fmt.format(*headers))
    for row in rows:
        print(fmt.format(*row))


def api_get(client: httpx.Client, path: str, params: dict | None = None):
    """GET with rate-limit retry."""
    for attempt in range(3):
        resp = client.get(path, params=params)
        if resp.status_code == 200:
            return resp.json()
        if resp.status_code == 429:
            retry_after = float(resp.json().get("retry_after", 1.0))
            time.sleep(retry_after)
            continue
        if resp.status_code >= 500 and attempt < 2:
            time.sleep(2 ** attempt)
            continue
        print(f"Error: Discord API {resp.status_code}: {resp.text}", file=sys.stderr)
        sys.exit(1)
    print("Error: Exhausted retries.", file=sys.stderr)
    sys.exit(1)


def api_post(client: httpx.Client, path: str, json_data: dict):
    """POST with rate-limit retry."""
    for attempt in range(3):
        resp = client.post(path, json=json_data)
        if resp.status_code in (200, 201):
            return resp.json()
        if resp.status_code == 429:
            retry_after = float(resp.json().get("retry_after", 1.0))
            time.sleep(retry_after)
            continue
        if resp.status_code >= 500 and attempt < 2:
            time.sleep(2 ** attempt)
            continue
        print(f"Error: Discord API {resp.status_code}: {resp.text}", file=sys.stderr)
        sys.exit(1)
    print("Error: Exhausted retries.", file=sys.stderr)
    sys.exit(1)


def find_master_channel(client: httpx.Client, guild_id: str) -> str:
    """Find the axi-master channel (or first text channel in Active category)."""
    channels = api_get(client, f"/guilds/{guild_id}/channels")

    # Look for channel named "axi-master"
    for ch in channels:
        if ch.get("name") == "axi-master" and ch.get("type") == 0:
            return ch["id"]

    # Fall back to first text channel in a category named "Active"
    active_cat_id = None
    for ch in channels:
        if ch.get("type") == 4 and ch.get("name", "").lower() == "active":
            active_cat_id = ch["id"]
            break
    if active_cat_id:
        for ch in channels:
            if ch.get("parent_id") == active_cat_id and ch.get("type") == 0:
                return ch["id"]

    # Fall back to first text channel
    for ch in channels:
        if ch.get("type") == 0:
            return ch["id"]

    print("Error: No text channel found in guild", file=sys.stderr)
    sys.exit(1)


def format_message(msg: dict) -> str:
    """Format a Discord message for display."""
    author = msg.get("author", {})
    username = author.get("username", "unknown")
    content = msg.get("content", "")
    return f"[{username}] {content}"


def is_sentinel(msg: dict) -> bool:
    """Check if a message contains the 'finished responding' sentinel."""
    content = msg.get("content", "")
    return content.startswith("*System:*") and SENTINEL in content


def get_sender_token() -> str:
    """Get the dedicated sender bot token for test messages."""
    config = load_config()
    token = config.get("defaults", {}).get("sender_token")
    if not token:
        print("Error: No sender_token in test-config.json defaults", file=sys.stderr)
        sys.exit(1)
    return token


def cmd_msg(args):
    name = args.name
    message = args.message
    timeout = args.timeout

    env = get_instance_env(name)
    guild_id = env.get("DISCORD_GUILD_ID")
    if not guild_id:
        print(f"Error: No reservation found for '{name}' (no .env or missing DISCORD_GUILD_ID)", file=sys.stderr)
        sys.exit(1)

    sender_token = get_sender_token()

    with httpx.Client(
        base_url=API_BASE,
        headers={"Authorization": f"Bot {sender_token}"},
        timeout=httpx.Timeout(10.0),
    ) as client:
        channel_id = find_master_channel(client, guild_id)

        # Send message
        sent = api_post(client, f"/channels/{channel_id}/messages", {"content": message})
        sent_id = sent["id"]
        print(f"Sent: {message}")
        print(f"Waiting for response (timeout: {timeout}s)...\n")

        # Poll for response
        deadline = time.monotonic() + timeout
        after_id = sent_id
        collected = []

        while time.monotonic() < deadline:
            messages = api_get(
                client,
                f"/channels/{channel_id}/messages",
                {"after": after_id, "limit": 100},
            )

            if messages:
                # Update cursor to highest ID (newest first from API)
                after_id = messages[0]["id"]

                for msg in reversed(messages):
                    # Skip our own sent message
                    if msg["id"] == sent_id:
                        continue

                    if is_sentinel(msg):
                        # Print any remaining collected messages
                        for m in collected:
                            print(format_message(m))
                        collected.clear()
                        sys.exit(0)

                    # Skip system messages
                    content = msg.get("content", "")
                    if content.startswith("*System:*"):
                        continue

                    collected.append(msg)
                    print(format_message(msg))
                    collected.clear()

            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            time.sleep(min(2.0, remaining))

        # Timeout
        for m in collected:
            print(format_message(m))
        print(
            "\nWarning: timed out without sentinel — bot may still be responding, or there may be a bug",
            file=sys.stderr,
        )
        sys.exit(1)


def cmd_merge(args):
    """Queue a squash merge of the current worktree branch into main."""
    main_repo = _find_main_repo()
    cwd = os.path.realpath(os.getcwd())

    if os.path.realpath(main_repo) == cwd:
        print("Already in main repo — nothing to merge")
        return

    branch = get_worktree_branch(cwd)
    if not branch or branch == "unknown":
        print("Error: Could not determine current branch", file=sys.stderr)
        sys.exit(1)

    # Submit to queue
    entry = {
        "branch": branch,
        "worktree": cwd,
        "pid": os.getpid(),
        "submitted_at": datetime.now(timezone.utc).isoformat(),
        "heartbeat": datetime.now(timezone.utc).isoformat(),
        "status": "queued",
    }

    with _flock(_queue_lock(main_repo)):
        entries = _read_queue(main_repo)
        _cleanup_stale(entries)
        for e in entries:
            if e["branch"] == branch:
                print(f"Error: branch '{branch}' is already in the merge queue", file=sys.stderr)
                sys.exit(1)
        entries.append(entry)
        _write_queue(main_repo, entries)
        position = len(entries)

    print(f"Queued for merge (position {position})")

    timeout = getattr(args, "timeout", 300)
    deadline = time.monotonic() + timeout

    try:
        # Wait for turn
        while True:
            if time.monotonic() > deadline:
                _remove_from_queue(main_repo, branch)
                print(f"Error: timed out waiting in merge queue ({timeout}s)", file=sys.stderr)
                sys.exit(2)

            with _flock(_queue_lock(main_repo)):
                entries = _read_queue(main_repo)
                _cleanup_stale(entries)
                # Update heartbeat
                for e in entries:
                    if e["branch"] == branch:
                        e["heartbeat"] = datetime.now(timezone.utc).isoformat()
                        break
                # Check if first
                first = entries and entries[0]["branch"] == branch
                if first:
                    entries[0]["status"] = "merging"
                _write_queue(main_repo, entries)

            if first:
                break

            pos = next((i for i, e in enumerate(entries) if e["branch"] == branch), -1)
            print(f"Waiting... (position {pos + 1} of {len(entries)})")
            time.sleep(2)

        # Execute merge
        print(f"Merging {branch} into main...")
        message = getattr(args, "message", None)

        with _flock(_merge_lock_file(main_repo)):
            status, detail = _execute_merge(main_repo, branch, message)

        _remove_from_queue(main_repo, branch)

        if status == "merged":
            print(f"Squash-merged as {detail}: {branch}")
        elif status == "needs_rebase":
            print(f"Error: main has moved ahead — rebase '{branch}' onto main and resubmit", file=sys.stderr)
            sys.exit(1)
        else:
            print(f"Error: {detail}", file=sys.stderr)
            sys.exit(2)

    except KeyboardInterrupt:
        _remove_from_queue(main_repo, branch)
        print("\nInterrupted — removed from queue", file=sys.stderr)
        sys.exit(130)
    except SystemExit:
        raise
    except Exception as e:
        _remove_from_queue(main_repo, branch)
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(2)


def cmd_queue(args):
    """Show or manage the merge queue."""
    main_repo = _find_main_repo()

    if args.action == "drop":
        if args.all:
            with _flock(_queue_lock(main_repo)):
                _write_queue(main_repo, [])
            print("Queue cleared")
        else:
            cwd = os.path.realpath(os.getcwd())
            branch = get_worktree_branch(cwd)
            with _flock(_queue_lock(main_repo)):
                entries = _read_queue(main_repo)
                before = len(entries)
                entries = [e for e in entries if e["branch"] != branch]
                _write_queue(main_repo, entries)
            if len(entries) < before:
                print(f"Removed '{branch}' from queue")
            else:
                print(f"Branch '{branch}' not found in queue")
        return

    # Default: show queue
    with _flock(_queue_lock(main_repo)):
        entries = _read_queue(main_repo)
        _cleanup_stale(entries)
        _write_queue(main_repo, entries)

    if not entries:
        print("Merge queue is empty")
        return

    print(f"Merge queue ({len(entries)} entries):")
    for i, entry in enumerate(entries):
        status = entry.get("status", "queued")
        branch = entry.get("branch", "?")
        pid = entry.get("pid", "?")
        submitted = entry.get("submitted_at", "?")[:19]
        print(f"  {i + 1}. [{status}] {branch} (pid {pid}, submitted {submitted})")


def cmd_cleanup(args):
    """Stop orphaned axi-test@ services that have no worktree or .env."""
    cleaned = cleanup_orphan_services()
    if cleaned == 0:
        print("No orphan services found")
    else:
        print(f"Cleaned up {cleaned} orphan service(s)")


def cmd_logs(args):
    os.execvp("journalctl", [
        "journalctl", "--user", "-u", f"axi-test@{args.name}", "-f",
    ])


def main():
    parser = argparse.ArgumentParser(
        prog="axi-test",
        description="Manage Axi test instance bot/guild reservations",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    # up
    p_up = sub.add_parser("up", help="Reserve a bot/guild slot for a test instance")
    p_up.add_argument("name", help="Instance name")
    p_up.add_argument("--guild", help="Guild name from config (default: auto-pick)")
    p_up.add_argument("--wait", action="store_true",
                       help="Wait for a bot token slot if all are in use")
    p_up.add_argument("--wait-timeout", type=int, default=7200,
                       help="Max seconds to wait for a slot (default: 7200)")
    p_up.set_defaults(func=cmd_up)

    # down
    p_down = sub.add_parser("down", help="Release a bot/guild reservation")
    p_down.add_argument("name", help="Instance name")
    p_down.set_defaults(func=cmd_down)

    # restart
    p_restart = sub.add_parser("restart", help="Restart a test instance service")
    p_restart.add_argument("name", help="Instance name")
    p_restart.set_defaults(func=cmd_restart)

    # list
    p_list = sub.add_parser("list", help="List all test instances")
    p_list.set_defaults(func=cmd_list)

    # msg
    p_msg = sub.add_parser("msg", help="Send a message and wait for response")
    p_msg.add_argument("name", help="Instance name")
    p_msg.add_argument("message", help="Message to send")
    p_msg.add_argument("--timeout", type=float, default=120, help="Timeout in seconds (default: 120)")
    p_msg.set_defaults(func=cmd_msg)

    # merge
    p_merge = sub.add_parser("merge", help="Squash-merge current branch into main via queue")
    p_merge.add_argument("-m", "--message", help="Custom commit message (default: branch name + commit list)")
    p_merge.add_argument("--timeout", type=int, default=300, help="Max seconds to wait in queue (default: 300)")
    p_merge.set_defaults(func=cmd_merge)

    # queue
    p_queue = sub.add_parser("queue", help="Show or manage merge queue")
    p_queue.add_argument("action", nargs="?", default="show", choices=["show", "drop"],
                          help="Action: show (default) or drop")
    p_queue.add_argument("--all", action="store_true", help="Drop all entries (with 'drop')")
    p_queue.set_defaults(func=cmd_queue)

    # cleanup
    p_cleanup = sub.add_parser("cleanup", help="Stop orphaned test instance services")
    p_cleanup.set_defaults(func=cmd_cleanup)

    # logs
    p_logs = sub.add_parser("logs", help="Follow instance logs")
    p_logs.add_argument("name", help="Instance name")
    p_logs.set_defaults(func=cmd_logs)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
