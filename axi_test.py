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
    axi-test clean <name> [--force] [--keep-channel] [--keep-branch]
    axi-test logs <name>
"""

import argparse
import json
import os
import re
import subprocess
import sys
import time
from datetime import UTC, datetime
from typing import Any, cast

from dotenv import dotenv_values

from axi.worktrees import (
    cleanup_stale as _cleanup_stale,
)
from axi.worktrees import (
    execute_merge as _execute_merge,
)
from axi.worktrees import (
    find_main_repo as _find_main_repo,
)
from axi.worktrees import (
    flock as _flock,
)
from axi.worktrees import (
    get_default_branch as _get_default_branch,
)
from axi.worktrees import (
    get_worktree_branch,
)
from axi.worktrees import (
    merge_lock_file as _merge_lock_file,
)
from axi.worktrees import (
    queue_lock as _queue_lock,
)
from axi.worktrees import (
    read_queue as _read_queue,
)
from axi.worktrees import (
    remove_from_queue as _remove_from_queue,
)
from axi.worktrees import (
    remove_worktree as _remove_worktree,
)
from axi.worktrees import (
    upgrade_git_deps as _upgrade_git_deps,
)
from axi.worktrees import (
    write_queue as _write_queue,
)
from discordquery import DiscordClient
from discordquery.wait import DEFAULT_STABLE_POLLS, wait_for_messages

REPO_DIR = os.path.dirname(os.path.abspath(__file__))
TESTS_DIR = os.environ.get("AXI_TESTS_DIR", os.path.join(os.path.expanduser("~"), "axi-tests"))
CONFIG_PATH = os.path.expanduser("~/.config/axi/test-config.json")
CONFIG_DIR = os.path.expanduser("~/.config/axi")
SLOTS_FILE = os.path.join(CONFIG_DIR, ".test-slots.json")
SLOTS_LOCK = os.path.join(CONFIG_DIR, ".test-slots.lock")
SENTINEL = "Flowchart **completed**"
BOT_DIR = REPO_DIR


# --- Utilities ---


def _systemctl_env() -> dict[str, str]:
    """Return environment with XDG_RUNTIME_DIR set for systemctl --user.

    Without this, systemctl --user silently fails when called from
    environments that don't inherit the variable (e.g. sandboxed agents).
    """
    env = os.environ.copy()
    if "XDG_RUNTIME_DIR" not in env:
        env["XDG_RUNTIME_DIR"] = f"/run/user/{os.getuid()}"
    return env


def _service_units(name: str, mode: str) -> list[str]:
    """Return systemd unit name(s) for an instance, in start order."""
    if mode == "rs":
        return [f"axi-test-procmux@{name}.service", f"axi-test-bot@{name}.service"]
    return [f"axi-test@{name}.service"]


def _slot_mode(slots: dict[str, Any], name: str) -> str:
    """Return 'rs' or 'py' for a reserved instance."""
    return slots.get(name, {}).get("mode", "py")


def _install_rs_units() -> None:
    """Symlink Rust unit files into ~/.config/systemd/user/ and reload."""
    user_units = os.path.expanduser("~/.config/systemd/user")
    os.makedirs(user_units, exist_ok=True)
    src_dir = os.path.join(BOT_DIR, "axi-rs", "systemd")
    changed = False
    for unit in ("axi-test-bot@.service", "axi-test-procmux@.service"):
        src = os.path.join(src_dir, unit)
        dst = os.path.join(user_units, unit)
        if os.path.islink(dst) and os.readlink(dst) == src:
            continue
        if os.path.exists(dst):
            os.remove(dst)
        os.symlink(src, dst)
        changed = True
    if changed:
        subprocess.run(
            ["systemctl", "--user", "daemon-reload"],
            capture_output=True,
            env=_systemctl_env(),
        )


def _install_py_unit() -> None:
    """Symlink Python test unit file into ~/.config/systemd/user/ and reload."""
    user_units = os.path.expanduser("~/.config/systemd/user")
    os.makedirs(user_units, exist_ok=True)
    unit = "axi-test@.service"
    src = os.path.join(BOT_DIR, unit)
    dst = os.path.join(user_units, unit)
    if os.path.islink(dst) and os.readlink(dst) == src:
        return
    if os.path.exists(dst):
        os.remove(dst)
    os.symlink(src, dst)
    subprocess.run(
        ["systemctl", "--user", "daemon-reload"],
        capture_output=True,
        env=_systemctl_env(),
    )


def load_config() -> dict[str, Any]:
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


def is_instance_running(name: str, mode: str = "py") -> bool:
    """Check if a test instance systemd service is active.

    Fails hard if systemctl can't reach the user bus — silently returning
    False in that case would mask running instances and corrupt reservations.
    """
    unit = f"axi-test-bot@{name}" if mode == "rs" else f"axi-test@{name}"
    result = subprocess.run(
        ["systemctl", "--user", "is-active", unit],
        capture_output=True,
        text=True,
        env=_systemctl_env(),
    )
    stderr = result.stderr.strip()
    if "Failed to connect to bus" in stderr or "No medium found" in stderr:
        print(f"Error: Cannot reach systemd user bus: {stderr}", file=sys.stderr)
        print("Cannot determine instance state — refusing to continue.", file=sys.stderr)
        sys.exit(1)
    return result.stdout.strip() == "active"


def get_instance_env(name: str) -> dict[str, str | None]:
    """Read a test instance's .env file."""
    env_path = os.path.join(TESTS_DIR, name, ".env")
    if not os.path.isfile(env_path):
        return {}
    return dotenv_values(env_path)


# --- Slot Management ---
#
# All slot reservations are tracked in a single JSON file (~/.config/axi/.test-slots.json)
# protected by an exclusive file lock (~/.config/axi/.test-slots.lock).
#
# The .env files in worktree directories are DERIVED from this reservation state —
# they contain non-sensitive config only (no tokens). The bot resolves its token
# at startup from the slots file + test-config.json.
#
# This eliminates TOCTOU races: checking for free slots and claiming one happens
# atomically under the same lock acquisition.




def _read_slots() -> dict[str, Any]:
    """Read slots file. Returns empty dict if missing or corrupted."""
    if not os.path.isfile(SLOTS_FILE):
        return {}
    with open(SLOTS_FILE) as f:
        try:
            data: Any = json.load(f)
            return cast("dict[str, Any]", data) if isinstance(data, dict) else {}
        except json.JSONDecodeError:
            print(f"Warning: Corrupted {SLOTS_FILE}, treating as empty", file=sys.stderr)
            return {}


def _write_slots(slots: dict[str, Any]) -> None:
    """Write slots file atomically. Caller must hold slot lock."""
    os.makedirs(CONFIG_DIR, exist_ok=True)
    tmp_path = SLOTS_FILE + ".tmp"
    with open(tmp_path, "w") as f:
        json.dump(slots, f, indent=2)
    os.replace(tmp_path, SLOTS_FILE)


def _load_slots(config: dict[str, Any]) -> dict[str, Any]:
    """Load slots, migrating from .env-based tracking on first run.

    Caller must hold slot lock.
    """
    if os.path.isfile(SLOTS_FILE):
        return _read_slots()

    # First run — migrate from existing .env files
    slots = _migrate_from_env(config)
    if slots:
        print(f"Migrated {len(slots)} reservation(s) from .env files")
    _write_slots(slots)  # Create file even if empty (prevents re-migration)
    return slots


def _migrate_from_env(config: dict[str, Any]) -> dict[str, Any]:
    """Build initial slots dict from existing .env files. One-time migration."""
    slots: dict[str, Any] = {}
    if not os.path.isdir(TESTS_DIR):
        return slots

    token_to_bot: dict[str, str] = {}
    for bot_name, bot_info in config["bots"].items():
        token_to_bot[bot_info.get("token")] = bot_name

    id_to_guild: dict[str, str] = {}
    for gname, ginfo in config["guilds"].items():
        id_to_guild[ginfo["guild_id"]] = gname

    for entry in os.listdir(TESTS_DIR):
        path = os.path.join(TESTS_DIR, entry)
        if not os.path.isdir(path) or entry.endswith("-data"):
            continue
        env_path = os.path.join(path, ".env")
        if not os.path.isfile(env_path):
            continue

        env = dotenv_values(env_path)
        token = env.get("DISCORD_TOKEN")
        guild_id = env.get("DISCORD_GUILD_ID")
        if not token or not guild_id:
            continue

        bot_name = token_to_bot.get(token, "unknown")
        guild_name = id_to_guild.get(guild_id, "unknown")
        slots[entry] = {
            "guild": guild_name,
            "guild_id": guild_id,
            "token_id": bot_name,
            "reserved_at": datetime.now(UTC).isoformat(),
            "worktree": path,
        }

    return slots


def _health_check(slots: dict[str, Any], config: dict[str, Any]) -> None:
    """Validate reservations, remove orphans. Mutates slots in place.

    Caller must hold slot lock.
    """
    to_remove: list[str] = []
    for name, slot in slots.items():
        worktree = slot.get("worktree", os.path.join(TESTS_DIR, name))

        # Worktree directory gone → definitely orphaned
        if not os.path.isdir(worktree):
            mode = _slot_mode(slots, name)
            if is_instance_running(name, mode):
                all_stopped = True
                for unit in reversed(_service_units(name, mode)):
                    result = subprocess.run(
                        ["systemctl", "--user", "stop", unit],
                        capture_output=True,
                        env=_systemctl_env(),
                    )
                    if result.returncode != 0:
                        all_stopped = False
                        stderr = result.stderr.decode("utf-8", errors="replace").strip()
                        print(
                            f"ERROR: Failed to stop {unit} for orphaned '{name}' "
                            f"(rc={result.returncode}): {stderr}. Keeping slot to avoid dual-allocation.",
                            file=sys.stderr,
                        )
                if not all_stopped:
                    # Service still alive — slot correctly represents that.
                    # Do NOT drop it; another holder could claim the same token.
                    continue
            to_remove.append(name)

    for name in to_remove:
        del slots[name]
        print(f"Cleaned up orphaned reservation: '{name}' (worktree removed)")


def _find_free_guild(
    slots: dict[str, Any], config: dict[str, Any], instance_name: str, explicit_guild: str | None
) -> str | None:
    """Find a free guild whose bot token is not in use. Caller must hold slot lock."""
    used_tokens: set[str] = set()
    for name, slot in slots.items():
        if name != instance_name:
            used_tokens.add(slot["token_id"])

    if explicit_guild:
        if explicit_guild not in config["guilds"]:
            print(f"Error: Guild '{explicit_guild}' not found in config", file=sys.stderr)
            print(f"Available guilds: {', '.join(config['guilds'].keys())}", file=sys.stderr)
            sys.exit(1)
        bot_name = config["guilds"][explicit_guild].get("bot")
        if bot_name not in used_tokens:
            return explicit_guild
        return None

    for guild_name, guild_info in config["guilds"].items():
        bot_name = guild_info.get("bot")
        if bot_name not in used_tokens:
            return guild_name

    return None


def _make_slot(guild_name: str, config: dict[str, Any], worktree: str, mode: str = "py") -> dict[str, Any]:
    """Create a slot reservation record."""
    guild_info = config["guilds"][guild_name]
    return {
        "guild": guild_name,
        "guild_id": guild_info["guild_id"],
        "token_id": guild_info.get("bot"),
        "reserved_at": datetime.now(UTC).isoformat(),
        "worktree": worktree,
        "mode": mode,
    }


def _write_env(
    guild_name: str,
    config: dict[str, Any],
    instance_path: str,
    data_path: str,
    rs_binary: str | None = None,
) -> None:
    """Generate .env and data dir from reservation data."""
    guild_info = config["guilds"][guild_name]
    guild_id = guild_info["guild_id"]
    defaults = config.get("defaults", {})
    bot_name = guild_info.get("bot")
    bot_token = config["bots"][bot_name]["token"]

    os.makedirs(instance_path, exist_ok=True)
    env_content = (
        f"DISCORD_TOKEN={bot_token}\n"
        f"DISCORD_GUILD_ID={guild_id}\n"
        f"ALLOWED_USER_IDS={defaults.get('allowed_user_ids', '')}\n"
        f"SCHEDULE_TIMEZONE={defaults.get('schedule_timezone', 'UTC')}\n"
        f"DEFAULT_CWD={instance_path}\n"
        f"AXI_USER_DATA={data_path}\n"
        f"HTTP_API_HOST=0.0.0.0\n"
        f"HTTP_API_PORT=8787\n"
        f"DAY_BOUNDARY_HOUR={defaults.get('day_boundary_hour', '0')}\n"
        f"AXI_MODEL=haiku\n"
    )
    if rs_binary:
        env_content += f"AXI_RS_BINARY={rs_binary}\n"
    with open(os.path.join(instance_path, ".env"), "w") as f:
        f.write(env_content)

    os.makedirs(data_path, exist_ok=True)
    for fname in ("schedules.json", "schedule_history.json"):
        fpath = os.path.join(data_path, fname)
        if not os.path.exists(fpath):
            with open(fpath, "w") as f:
                json.dump([], f)


def _try_reserve(
    config: dict[str, Any], name: str, instance_path: str, explicit_guild: str | None, mode: str = "py"
) -> str | None:
    """Attempt to reserve a slot atomically. Returns guild name or None."""
    with _flock(SLOTS_LOCK):
        slots = _load_slots(config)
        _health_check(slots, config)

        if name in slots:
            old_mode = _slot_mode(slots, name)
            if is_instance_running(name, old_mode):
                print(f"Error: Instance '{name}' is already running", file=sys.stderr)
                print(f"Run 'axi-test down {name}' first, or choose a different name", file=sys.stderr)
                sys.exit(1)
            else:
                print(f"Cleaning up stale reservation for '{name}' (not running)")
                for unit in reversed(_service_units(name, old_mode)):
                    subprocess.run(
                        ["systemctl", "--user", "stop", unit],
                        capture_output=True,
                        env=_systemctl_env(),
                    )
                env_path = os.path.join(instance_path, ".env")
                if os.path.isfile(env_path):
                    os.remove(env_path)
                del slots[name]

        guild_name = _find_free_guild(slots, config, name, explicit_guild)
        if guild_name is not None:
            slots[name] = _make_slot(guild_name, config, instance_path, mode)
            _write_slots(slots)
            return guild_name

        _write_slots(slots)  # persist health check cleanup
        return None


def _wait_and_reserve(
    config: dict[str, Any],
    name: str,
    instance_path: str,
    explicit_guild: str | None,
    timeout: int,
    poll_interval: int = 10,
    mode: str = "py",
) -> str:
    """Poll until a slot is available and reserve it atomically."""
    deadline = time.monotonic() + timeout
    total = len(config["guilds"])

    if explicit_guild:
        bot_name = config["guilds"][explicit_guild].get("bot", "?")
        print(
            f"Bot '{bot_name}' (guild '{explicit_guild}') is in use. Waiting for it to free up (timeout: {timeout}s)..."
        )
    else:
        print(f"All {total} bot token(s) are in use. Waiting for a slot (timeout: {timeout}s)...")

    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            print(f"\nCould not reserve a bot token slot after waiting {timeout}s.", file=sys.stderr)
            print("All bot tokens are still in use. Please ask the user how to proceed.", file=sys.stderr)
            sys.exit(1)

        time.sleep(min(poll_interval, remaining))

        with _flock(SLOTS_LOCK):
            slots = _load_slots(config)
            _health_check(slots, config)
            guild_name = _find_free_guild(slots, config, name, explicit_guild)
            if guild_name is not None:
                slots[name] = _make_slot(guild_name, config, instance_path, mode)
                _write_slots(slots)
                print(f"Slot available! Using guild '{guild_name}'")
                return guild_name
            _write_slots(slots)  # persist health check cleanup

        mins_left = int(remaining) // 60
        secs_left = int(remaining) % 60
        print(f"  Still waiting... ({mins_left}m {secs_left}s remaining)")


# --- Orphan Service Cleanup ---


def cleanup_orphan_services() -> int:
    """Stop and reset orphaned axi-test@ services.

    Finds user-level axi-test@, axi-test-bot@, and axi-test-procmux@
    units that have no reservation in the slots file and no .env file
    (pre-migration fallback).
    """
    slots = _read_slots()
    env = _systemctl_env()

    # Scan all three unit patterns
    prefixes = ("axi-test@", "axi-test-bot@", "axi-test-procmux@")
    patterns = [f"{p}*" for p in prefixes]
    result = subprocess.run(
        ["systemctl", "--user", "list-units", "--all", "--plain", "--no-legend", *patterns],
        capture_output=True,
        text=True,
        env=env,
    )

    cleaned = 0
    for line in result.stdout.strip().splitlines():
        if not line.strip():
            continue
        unit = line.split()[0]
        if not unit.endswith(".service"):
            continue

        # Extract instance name from unit
        name: str | None = None
        for prefix in prefixes:
            if unit.startswith(prefix):
                name = unit[len(prefix) : -len(".service")]
                break
        if name is None:
            continue

        # Has a reservation → legitimate
        if name in slots:
            continue

        # Fallback: check .env file (pre-migration compat)
        instance_path = os.path.join(TESTS_DIR, name)
        if os.path.isdir(instance_path) and os.path.isfile(os.path.join(instance_path, ".env")):
            continue

        subprocess.run(
            ["systemctl", "--user", "stop", unit],
            capture_output=True,
            env=env,
        )
        subprocess.run(
            ["systemctl", "--user", "reset-failed", unit],
            capture_output=True,
            env=env,
        )
        print(f"Cleaned up orphan service: {unit}")
        cleaned += 1

    return cleaned


# --- Subcommands ---


def cmd_up(args: argparse.Namespace) -> None:
    cleanup_orphan_services()

    config = load_config()
    name = args.name
    mode = "rs" if args.rs else "py"
    instance_path = os.path.join(TESTS_DIR, name)
    data_path = os.path.join(TESTS_DIR, f"{name}-data")

    rs_binary = None
    if mode == "rs":
        _install_rs_units()
        profile = "release" if args.release else "debug"
        rs_binary = os.path.join(instance_path, "axi-rs", "target", profile, "axi")
    else:
        _install_py_unit()

    guild_name = _try_reserve(config, name, instance_path, args.guild, mode)

    if guild_name is None:
        if args.wait:
            guild_name = _wait_and_reserve(
                config,
                name,
                instance_path,
                args.guild,
                args.wait_timeout,
                mode=mode,
            )
        else:
            total = len(config["guilds"])
            print(f"Error: All {total} bot token(s) are in use", file=sys.stderr)
            print("Either stop an instance or add another bot to the config", file=sys.stderr)
            print("Hint: Use --wait to poll until a slot is available", file=sys.stderr)
            sys.exit(1)

    # Write .env and create data dir (outside lock — derived from reservation)
    _write_env(guild_name, config, instance_path, data_path, rs_binary)

    guild_id = config["guilds"][guild_name]["guild_id"]
    print(f"Reserved guild '{guild_name}' ({guild_id}) for instance '{name}' (mode: {mode})")
    print(f"  .env:  {instance_path}/.env")
    print(f"  Data:  {data_path}")
    if rs_binary:
        print(f"  Binary: {rs_binary}")


def cmd_down(args: argparse.Namespace) -> None:
    config = load_config()
    name = args.name
    instance_path = os.path.join(TESTS_DIR, name)
    env_path = os.path.join(instance_path, ".env")

    with _flock(SLOTS_LOCK):
        slots = _load_slots(config)

        if name not in slots:
            print(f"Error: No reservation found for '{name}'", file=sys.stderr)
            sys.exit(1)

        mode = _slot_mode(slots, name)
        if is_instance_running(name, mode):
            units = _service_units(name, mode)
            print(f"Stopping {units[-1]}...")
            for unit in reversed(units):
                subprocess.run(
                    ["systemctl", "--user", "stop", unit],
                    capture_output=True,
                    check=True,
                    env=_systemctl_env(),
                )

        if os.path.isfile(env_path):
            os.remove(env_path)

        del slots[name]
        _write_slots(slots)

    print(f"Released reservation for instance '{name}'")


def cmd_restart(args: argparse.Namespace) -> None:
    name = args.name
    slots = _read_slots()
    if name not in slots:
        print(f"Warning: No reservation found for '{name}' in slots file", file=sys.stderr)

    mode = _slot_mode(slots, name)
    if mode == "rs":
        _install_rs_units()
    else:
        _install_py_unit()
    units = _service_units(name, mode)
    print(f"Restarting {units[-1]}...")
    for unit in units:
        subprocess.run(
            ["systemctl", "--user", "restart", unit],
            capture_output=True,
            check=True,
            env=_systemctl_env(),
        )
    print("Done")


def cmd_list(args: argparse.Namespace) -> None:
    config = load_config()

    with _flock(SLOTS_LOCK):
        slots = _load_slots(config)
        _health_check(slots, config)
        _write_slots(slots)

    if not slots:
        print("No test instances found")
        return

    rows: list[tuple[str, str, str, str, str, str]] = []
    for name in sorted(slots):
        slot = slots[name]
        guild_name = slot.get("guild", "?")
        mode = _slot_mode(slots, name)
        worktree = slot.get("worktree", os.path.join(TESTS_DIR, name))
        status = "running" if is_instance_running(name, mode) else "stopped"

        is_git = os.path.isdir(os.path.join(worktree, ".git")) or os.path.isfile(os.path.join(worktree, ".git"))
        branch = get_worktree_branch(worktree) if is_git else "-"

        reserved_at = slot.get("reserved_at", "?")[:19]
        rows.append((name, str(guild_name), mode, branch, status, str(reserved_at)))

    headers = ("NAME", "GUILD", "MODE", "BRANCH", "STATUS", "RESERVED_AT")
    widths = [max(len(h), max(len(str(r[i])) for r in rows)) for i, h in enumerate(headers)]
    fmt = "  ".join(f"{{:<{w}}}" for w in widths)
    print(fmt.format(*headers))
    for row in rows:
        print(fmt.format(*row))


# --- Discord API helpers ---


def _get_prime_env(main_repo: str) -> dict[str, str | None]:
    """Read Prime bot's .env for Discord token and guild ID."""
    env_path = os.path.join(main_repo, ".env")
    return dotenv_values(env_path) if os.path.isfile(env_path) else {}


def _normalize_channel_name(name: str) -> str:
    """Normalize an agent name to a Discord channel name."""
    name = name.lower().replace(" ", "-")
    return re.sub(r"[^a-z0-9\-_]", "", name)


def _find_channel_by_name(client: DiscordClient, guild_id: str, name: str) -> dict[str, Any] | None:
    """Find a text channel by name in the Active category."""
    normalized = _normalize_channel_name(name)
    channels: list[dict[str, Any]] = client.get(f"/guilds/{guild_id}/channels")
    # Find Active category ID
    active_cat_id = None
    for ch in channels:
        if ch.get("type") == 4 and ch.get("name", "").lower() == "active":
            active_cat_id = ch["id"]
            break
    for ch in channels:
        if (
            ch.get("name") == normalized
            and ch.get("type") == 0
            and (active_cat_id is None or ch.get("parent_id") == active_cat_id)
        ):
            return ch
    return None


def _find_killed_category(client: DiscordClient, guild_id: str) -> str | None:
    """Find a Killed category with room (<50 channels) in a guild.

    Checks primary "Killed" and overflow categories ("Killed 2", etc.).
    Returns the ID of the first category with room, or None if all are full.
    """
    all_channels: list[dict[str, Any]] = client.get(f"/guilds/{guild_id}/channels")
    killed_cats: list[tuple[int, dict[str, Any]]] = []
    for ch in all_channels:
        if ch.get("type") == 4:
            name = ch.get("name", "")
            if name.lower() == "killed":
                killed_cats.append((1, ch))
            elif re.match(r"^killed\s+\d+$", name.lower()):
                killed_cats.append((int(name.split()[-1]), ch))
    killed_cats.sort(key=lambda x: x[0])

    for _, cat in killed_cats:
        count = sum(1 for c in all_channels if c.get("parent_id") == cat["id"] and c.get("type") == 0)
        if count < 50:
            return cat["id"]
    return None


def find_master_channel(client: DiscordClient, guild_id: str) -> str:
    """Find the axi-master channel (or first text channel in Active category)."""
    channels: list[dict[str, Any]] = client.get(f"/guilds/{guild_id}/channels")

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


def format_message(msg: dict[str, Any]) -> str:
    """Format a Discord message for display."""
    author = msg.get("author", {})
    username = author.get("username", "unknown")
    content = msg.get("content", "")
    return f"[{username}] {content}"


def is_sentinel(msg: dict[str, Any]) -> bool:
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


def cmd_msg(args: argparse.Namespace) -> None:
    name = args.name
    message = args.message
    timeout = args.timeout
    wait_mode = args.wait_mode
    check = args.check
    poll_interval = args.poll_interval

    # Read guild_id from slots file (source of truth)
    slots = _read_slots()
    slot = slots.get(name)
    guild_id = slot.get("guild_id") if slot else None

    if not guild_id:
        # Fallback to .env (pre-migration compat)
        env = get_instance_env(name)
        guild_id = env.get("DISCORD_GUILD_ID")

    if not guild_id:
        print(f"Error: No reservation found for '{name}'", file=sys.stderr)
        sys.exit(1)

    sender_token = get_sender_token()

    with DiscordClient(sender_token, timeout=10.0) as client:
        channel_id = find_master_channel(client, guild_id)

        # Send message
        sent = client.post(f"/channels/{channel_id}/messages", json={"content": message})
        sent_id = sent["id"]
        print(f"Sent: {message}")
        print(f"Waiting for response (timeout: {timeout}s)...\n")

        # Poll for response
        if wait_mode == "sentinel":
            deadline = time.monotonic() + timeout
            after_id = sent_id
            collected: list[dict[str, Any]] = []

            while time.monotonic() < deadline:
                messages = client.get_messages(channel_id, limit=100, after=after_id)

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
                time.sleep(min(poll_interval, remaining))

            # Timeout
            for m in collected:
                print(format_message(m))
            print(
                "\nWarning: timed out without sentinel — bot may still be responding, or there may be a bug",
                file=sys.stderr,
            )
            sys.exit(1)

        stable_polls = DEFAULT_STABLE_POLLS if wait_mode == "stable" else 0
        substring = check if wait_mode == "substring" else None
        sender_me = client.get("/users/@me")
        sender_user_id = str(sender_me["id"])
        messages, _cursor = wait_for_messages(
            client,
            channel_id,
            sent_id,
            timeout,
            ignore_author_ids={sender_user_id},
            ignore_system=True,
            poll_interval=poll_interval,
            substring=substring,
            stable_polls=stable_polls,
        )
        if messages:
            for msg in messages:
                print(format_message(msg))
            sys.exit(0)

        if wait_mode == "substring" and check:
            print(f"\nWarning: timed out without seeing substring: {check}", file=sys.stderr)
        else:
            print("\nWarning: timed out waiting for response", file=sys.stderr)
        sys.exit(1)


def cmd_merge(args: argparse.Namespace) -> None:
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
        "submitted_at": datetime.now(UTC).isoformat(),
        "heartbeat": datetime.now(UTC).isoformat(),
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
                        e["heartbeat"] = datetime.now(UTC).isoformat()
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
        default_branch = _get_default_branch(main_repo)
        print(f"Merging {branch} into {default_branch}...")
        message = getattr(args, "message", None)

        with _flock(_merge_lock_file(main_repo)):
            status, detail = _execute_merge(main_repo, branch, message)

        _remove_from_queue(main_repo, branch)

        if status == "merged":
            print(f"Squash-merged as {detail}: {branch}")
            _upgrade_git_deps(main_repo)
            _remove_worktree(main_repo, cwd, branch)
            print(f"Cleaned up worktree and branch: {branch}")
        elif status == "needs_rebase":
            print(f"Error: {default_branch} has moved ahead — rebase '{branch}' onto {default_branch} and resubmit", file=sys.stderr)
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


def cmd_queue(args: argparse.Namespace) -> None:
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


def cmd_cleanup(args: argparse.Namespace) -> None:
    """Stop orphaned axi-test@ services that have no reservation."""
    cleaned = cleanup_orphan_services()
    if cleaned == 0:
        print("No orphan services found")
    else:
        print(f"Cleaned up {cleaned} orphan service(s)")


def cmd_clean(args: argparse.Namespace) -> None:
    """Clean up a worktree: check for uncommitted changes, remove worktree, kill channel."""
    name = args.name
    worktree_path = os.path.join(TESTS_DIR, name)
    force = args.force

    if not os.path.isdir(worktree_path):
        print(f"Error: Worktree not found: {worktree_path}", file=sys.stderr)
        sys.exit(1)

    # 1. Check for uncommitted changes
    result = subprocess.run(
        ["git", "-C", worktree_path, "status", "--porcelain"],
        capture_output=True,
        text=True,
    )
    if result.stdout.strip():
        if not force:
            print("Error: Worktree has uncommitted changes:", file=sys.stderr)
            for line in result.stdout.strip().splitlines():
                print(f"  {line}", file=sys.stderr)
            print("\nUse --force to clean anyway", file=sys.stderr)
            sys.exit(1)
        else:
            print("Warning: Discarding uncommitted changes (--force)")

    # 2. Get branch name before removing worktree
    branch = get_worktree_branch(worktree_path)

    # 3. Stop service and release slot if reserved
    config = load_config()
    with _flock(SLOTS_LOCK):
        slots = _load_slots(config)
        if name in slots:
            mode = _slot_mode(slots, name)
            if is_instance_running(name, mode):
                units = _service_units(name, mode)
                print(f"Stopping {units[-1]}...")
                for unit in reversed(units):
                    subprocess.run(
                        ["systemctl", "--user", "stop", unit],
                        capture_output=True,
                        env=_systemctl_env(),
                    )
            env_path = os.path.join(worktree_path, ".env")
            if os.path.isfile(env_path):
                os.remove(env_path)
            del slots[name]
            _write_slots(slots)
            print("Released slot reservation")

    # 4. Remove git worktree
    main_repo_result = subprocess.run(
        ["git", "-C", worktree_path, "rev-parse", "--path-format=absolute", "--git-common-dir"],
        capture_output=True,
        text=True,
    )
    if main_repo_result.returncode != 0:
        print("Error: Cannot determine main repo from worktree", file=sys.stderr)
        sys.exit(1)
    main_repo = os.path.dirname(main_repo_result.stdout.strip())

    remove_cmd = ["git", "-C", main_repo, "worktree", "remove", worktree_path]
    if force:
        remove_cmd.append("--force")
    result = subprocess.run(remove_cmd, capture_output=True, text=True)
    if result.returncode != 0:
        print(f"Error removing worktree: {result.stderr.strip()}", file=sys.stderr)
        sys.exit(1)
    print(f"Removed worktree: {worktree_path}")

    # 5. Delete branch if it was a feature branch (unless --keep-branch)
    if not args.keep_branch and branch and branch.startswith("feature/"):
        # Check if branch is merged into the default branch
        default_branch = _get_default_branch(main_repo)
        result = subprocess.run(
            ["git", "-C", main_repo, "branch", "--merged", default_branch],
            capture_output=True,
            text=True,
        )
        merged_branches = [b.strip().removeprefix("* ") for b in result.stdout.splitlines()]
        if branch in merged_branches:
            subprocess.run(
                ["git", "-C", main_repo, "branch", "-d", branch],
                capture_output=True,
                text=True,
            )
            print(f"Deleted merged branch: {branch}")
        else:
            print(f"Kept unmerged branch: {branch}")

    # 6. Kill Discord channel (move to Killed category)
    if not args.keep_channel:
        prime_env = _get_prime_env(main_repo)
        token = prime_env.get("DISCORD_TOKEN")
        guild_id = prime_env.get("DISCORD_GUILD_ID")
        if token and guild_id:
            with DiscordClient(token, timeout=10.0) as client:
                ch = _find_channel_by_name(client, guild_id, name)
                if ch:
                    killed_cat = _find_killed_category(client, guild_id)
                    if killed_cat:
                        client.request("PATCH", f"/channels/{ch['id']}", json={"parent_id": killed_cat})
                        print(f"Moved channel #{ch['name']} to Killed")
                    else:
                        client.request("DELETE", f"/channels/{ch['id']}")
                        print(f"Deleted channel #{ch['name']}")
                else:
                    print(f"No Discord channel found for '{name}'")
        else:
            print("Warning: Could not read Prime .env — skipping channel cleanup")

    print(f"Clean complete: {name}")


def cmd_logs(args: argparse.Namespace) -> None:
    name = args.name
    mode = _slot_mode(_read_slots(), name)
    if mode == "rs":
        os.execvp(
            "journalctl",
            [
                "journalctl",
                "--user",
                "-u",
                f"axi-test-procmux@{name}",
                "-u",
                f"axi-test-bot@{name}",
                "-f",
            ],
        )
    else:
        os.execvp(
            "journalctl",
            [
                "journalctl",
                "--user",
                "-u",
                f"axi-test@{name}",
                "-f",
            ],
        )


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
    p_up.add_argument("--wait", action="store_true", help="Wait for a bot token slot if all are in use")
    p_up.add_argument("--wait-timeout", type=int, default=7200, help="Max seconds to wait for a slot (default: 7200)")
    p_up.add_argument("--rs", action="store_true", help="Use Rust bot (debug build by default)")
    p_up.add_argument("--release", action="store_true", help="Use release build (requires --rs)")
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
    p_msg.add_argument(
        "--wait-mode",
        choices=["sentinel", "stable", "substring"],
        default="sentinel",
        help="How to wait for the bot response (default: sentinel)",
    )
    p_msg.add_argument("--check", help="Required substring when using --wait-mode substring")
    p_msg.add_argument("--poll-interval", type=float, default=2.0, help="Polling interval in seconds (default: 2.0)")
    p_msg.set_defaults(func=cmd_msg)

    # merge
    p_merge = sub.add_parser("merge", help="Squash-merge current branch into main via queue")
    p_merge.add_argument("-m", "--message", help="Custom commit message (default: branch name + commit list)")
    p_merge.add_argument("--timeout", type=int, default=300, help="Max seconds to wait in queue (default: 300)")
    p_merge.set_defaults(func=cmd_merge)

    # queue
    p_queue = sub.add_parser("queue", help="Show or manage merge queue")
    p_queue.add_argument(
        "action", nargs="?", default="show", choices=["show", "drop"], help="Action: show (default) or drop"
    )
    p_queue.add_argument("--all", action="store_true", help="Drop all entries (with 'drop')")
    p_queue.set_defaults(func=cmd_queue)

    # clean
    p_clean = sub.add_parser("clean", help="Remove worktree, release slot, kill channel")
    p_clean.add_argument("name", help="Worktree/agent name")
    p_clean.add_argument("--force", action="store_true", help="Clean even with uncommitted changes")
    p_clean.add_argument("--keep-channel", action="store_true", help="Don't move Discord channel to Killed")
    p_clean.add_argument("--keep-branch", action="store_true", help="Don't delete merged feature branch")
    p_clean.set_defaults(func=cmd_clean)

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
