#!/usr/bin/env python3
"""CLI for managing disposable Axi test instances.

Each test instance is a git worktree with its own .env, venv, and systemd service.
Managed via systemd template units (axi-test@<name>.service).

Usage:
    axi-test up <name> [--branch BRANCH] [--guild GUILD]
    axi-test down <name> [--keep]
    axi-test restart <name>
    axi-test list
    axi-test msg <name> <message> [--timeout SECS]
    axi-test logs <name>
"""
import argparse
import json
import os
import shutil
import subprocess
import sys
import time

import httpx
from dotenv import dotenv_values

TESTS_DIR = "/home/ubuntu/axi-tests"
CONFIG_PATH = os.path.expanduser("~/.config/axi/test-config.json")
REPO_DIR = "/home/ubuntu/axi-assistant"
API_BASE = "https://discord.com/api/v10"
SENTINEL = "Bot has finished responding"


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
    """Resolve guild name → bot name → token."""
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
    """Check if a test instance systemd service is active."""
    result = subprocess.run(
        ["systemctl", "--user", "is-active", f"axi-test@{name}"],
        capture_output=True, text=True,
    )
    return result.stdout.strip() == "active"


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


def pick_guild(config: dict, explicit_guild: str | None) -> str:
    """Pick a guild, checking for token conflicts with running instances."""
    if explicit_guild:
        if explicit_guild not in config["guilds"]:
            print(f"Error: Guild '{explicit_guild}' not found in config", file=sys.stderr)
            print(f"Available guilds: {', '.join(config['guilds'].keys())}", file=sys.stderr)
            sys.exit(1)
        return explicit_guild

    # Pick first guild whose bot has no running instance
    running = get_running_instances()
    running_tokens = set()
    for inst in running:
        token = get_token_for_running_instance(inst, config)
        if token:
            running_tokens.add(token)

    for guild_name, guild_info in config["guilds"].items():
        bot_name = guild_info.get("bot")
        token = config["bots"].get(bot_name, {}).get("token")
        if token and token not in running_tokens:
            return guild_name

    print("Error: All bot tokens are in use by running instances", file=sys.stderr)
    print("Either stop an instance or add another bot to the config", file=sys.stderr)
    sys.exit(1)


def check_token_conflict(config: dict, guild_name: str, instance_name: str) -> None:
    """Error if another running instance uses the same bot token."""
    token = resolve_bot_token(config, guild_name)
    running = get_running_instances()
    for inst in running:
        if inst == instance_name:
            continue
        inst_token = get_token_for_running_instance(inst, config)
        if inst_token == token:
            bot_name = config["guilds"][guild_name].get("bot")
            print(f"Error: Bot '{bot_name}' is already in use by running instance '{inst}'", file=sys.stderr)
            print(f"Stop it first: axi-test down {inst}", file=sys.stderr)
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


# --- Subcommands ---

def cmd_up(args):
    config = load_config()
    name = args.name
    branch = args.branch or "HEAD"
    worktree_path = os.path.join(TESTS_DIR, name)
    data_path = os.path.join(TESTS_DIR, f"{name}-data")

    if os.path.exists(worktree_path):
        print(f"Error: Worktree already exists: {worktree_path}", file=sys.stderr)
        print(f"Run 'axi-test down {name}' first, or choose a different name", file=sys.stderr)
        sys.exit(1)

    guild_name = pick_guild(config, args.guild)
    check_token_conflict(config, guild_name, name)

    guild_info = config["guilds"][guild_name]
    guild_id = guild_info["guild_id"]
    token = resolve_bot_token(config, guild_name)
    defaults = config.get("defaults", {})

    # Create worktree
    os.makedirs(TESTS_DIR, exist_ok=True)
    print(f"Creating worktree from {branch}...")
    result = subprocess.run(
        ["git", "worktree", "add", worktree_path, branch],
        cwd=REPO_DIR, capture_output=True, text=True,
    )
    if result.returncode != 0:
        print(f"Error creating worktree: {result.stderr.strip()}", file=sys.stderr)
        sys.exit(1)

    # Write .env
    env_content = (
        f"DISCORD_TOKEN={token}\n"
        f"DISCORD_GUILD_ID={guild_id}\n"
        f"ALLOWED_USER_IDS={defaults.get('allowed_user_ids', '')}\n"
        f"SCHEDULE_TIMEZONE={defaults.get('schedule_timezone', 'UTC')}\n"
        f"DEFAULT_CWD={worktree_path}\n"
        f"AXI_USER_DATA={data_path}\n"
        f"DAY_BOUNDARY_HOUR={defaults.get('day_boundary_hour', '0')}\n"
    )
    with open(os.path.join(worktree_path, ".env"), "w") as f:
        f.write(env_content)

    # Create data directory with empty schedule files
    os.makedirs(data_path, exist_ok=True)
    for fname in ("schedules.json", "schedule_history.json"):
        fpath = os.path.join(data_path, fname)
        if not os.path.exists(fpath):
            with open(fpath, "w") as f:
                json.dump([], f)

    # uv sync
    print("Syncing dependencies...")
    result = subprocess.run(
        ["uv", "sync"], cwd=worktree_path, capture_output=True, text=True,
    )
    if result.returncode != 0:
        print(f"Warning: uv sync failed: {result.stderr.strip()}", file=sys.stderr)

    # Start service
    print("Starting service...")
    subprocess.run(
        ["systemctl", "--user", "start", f"axi-test@{name}"],
        check=True,
    )

    actual_branch = get_worktree_branch(worktree_path)
    print(f"\nInstance '{name}' created")
    print(f"  Guild:    {guild_name} ({guild_id})")
    print(f"  Branch:   {actual_branch}")
    print(f"  Worktree: {worktree_path}")
    print(f"  Data:     {data_path}")
    print(f"  Service:  axi-test@{name}")


def cmd_down(args):
    name = args.name
    worktree_path = os.path.join(TESTS_DIR, name)
    data_path = os.path.join(TESTS_DIR, f"{name}-data")

    # Stop service (don't error if already stopped)
    print(f"Stopping axi-test@{name}...")
    subprocess.run(
        ["systemctl", "--user", "stop", f"axi-test@{name}"],
        capture_output=True,
    )

    if args.keep:
        print(f"Instance stopped. Worktree kept at {worktree_path}")
        return

    # Remove worktree
    if os.path.isdir(worktree_path):
        print(f"Removing worktree...")
        result = subprocess.run(
            ["git", "worktree", "remove", "--force", worktree_path],
            cwd=REPO_DIR, capture_output=True, text=True,
        )
        if result.returncode != 0:
            print(f"Warning: git worktree remove failed: {result.stderr.strip()}", file=sys.stderr)
            print(f"Removing directory manually...", file=sys.stderr)
            shutil.rmtree(worktree_path, ignore_errors=True)

    # Remove data directory
    if os.path.isdir(data_path):
        shutil.rmtree(data_path)

    print(f"Instance '{name}' removed")


def cmd_restart(args):
    name = args.name
    print(f"Restarting axi-test@{name}...")
    subprocess.run(
        ["systemctl", "--user", "restart", f"axi-test@{name}"],
        check=True,
    )
    print("Done")


def cmd_list(args):
    config = load_config()
    if not os.path.isdir(TESTS_DIR):
        print("No test instances found")
        return

    # Build guild_id → guild_name map
    id_to_guild = {}
    for gname, ginfo in config["guilds"].items():
        id_to_guild[ginfo["guild_id"]] = gname

    rows = []
    for entry in sorted(os.listdir(TESTS_DIR)):
        path = os.path.join(TESTS_DIR, entry)
        if not os.path.isdir(path) or entry.endswith("-data"):
            continue

        env = get_instance_env(entry)
        guild_id = env.get("DISCORD_GUILD_ID", "?")
        guild_name = id_to_guild.get(guild_id, "?")
        status = "running" if is_instance_running(entry) else "stopped"
        branch = get_worktree_branch(path)

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
    """Get Prime's bot token for sending test messages.

    Uses the main repo's .env — Prime is in ALLOWED_USER_IDS on test instances,
    so messages from Prime's bot are processed by the test bot.
    """
    prime_env = dotenv_values(os.path.join(REPO_DIR, ".env"))
    token = prime_env.get("DISCORD_TOKEN")
    if not token:
        print(f"Error: Could not read DISCORD_TOKEN from {REPO_DIR}/.env", file=sys.stderr)
        sys.exit(1)
    return token


def cmd_msg(args):
    name = args.name
    message = args.message
    timeout = args.timeout
    worktree_path = os.path.join(TESTS_DIR, name)

    if not os.path.isdir(worktree_path):
        print(f"Error: Instance '{name}' not found", file=sys.stderr)
        sys.exit(1)

    env = get_instance_env(name)
    guild_id = env.get("DISCORD_GUILD_ID")
    if not guild_id:
        print(f"Error: Could not read DISCORD_GUILD_ID from instance .env", file=sys.stderr)
        sys.exit(1)

    # Send as Prime's bot (test bot ignores its own messages)
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


def cmd_logs(args):
    os.execvp("journalctl", [
        "journalctl", "--user", "-u", f"axi-test@{args.name}", "-f",
    ])


def main():
    parser = argparse.ArgumentParser(
        prog="axi-test",
        description="Manage disposable Axi test instances",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    # up
    p_up = sub.add_parser("up", help="Create and start a test instance")
    p_up.add_argument("name", help="Instance name (used in paths and service name)")
    p_up.add_argument("--branch", help="Git branch or ref (default: HEAD)")
    p_up.add_argument("--guild", help="Guild name from config (default: auto-pick)")
    p_up.set_defaults(func=cmd_up)

    # down
    p_down = sub.add_parser("down", help="Stop and remove a test instance")
    p_down.add_argument("name", help="Instance name")
    p_down.add_argument("--keep", action="store_true", help="Keep worktree and data after stopping")
    p_down.set_defaults(func=cmd_down)

    # restart
    p_restart = sub.add_parser("restart", help="Restart a test instance")
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

    # logs
    p_logs = sub.add_parser("logs", help="Follow instance logs")
    p_logs.add_argument("name", help="Instance name")
    p_logs.set_defaults(func=cmd_logs)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
