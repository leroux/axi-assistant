#!/usr/bin/env python3
"""Migrate Axi agent channels from one Discord guild to another.

Copies channel structure (names, topics, categories) so the bot can
resume sessions in the new guild via reconstruct_agents_from_channels().

Usage:
    uv run python migrate_guild.py TARGET_GUILD_ID [--dry-run]

Reads DISCORD_TOKEN and DISCORD_GUILD_ID (source) from .env.
"""

from __future__ import annotations

import argparse
import os
import sys
import time

from dotenv import load_dotenv

load_dotenv()

from packages.discordquery.discordquery.client import DiscordClient

CATEGORY_NAMES = {"Axi", "Active", "Killed"}
# Match overflow categories like "Killed 2", "Active 3"
def _is_axi_category(name: str) -> bool:
    for base in CATEGORY_NAMES:
        if name == base:
            return True
        if name.startswith(base + " ") and name[len(base) + 1:].isdigit():
            return True
    return False


def migrate(token: str, source_guild: str, target_guild: str, *, dry_run: bool = False) -> None:
    with DiscordClient(token) as client:
        # Fetch all channels from both guilds (raw API, includes categories)
        source_channels: list[dict] = client.get(f"/guilds/{source_guild}/channels")
        target_channels: list[dict] = client.get(f"/guilds/{target_guild}/channels")

        # Map source categories
        source_cats = {c["id"]: c["name"] for c in source_channels if c["type"] == 4}
        # Filter to Axi-managed categories
        axi_cat_ids = {cid for cid, name in source_cats.items() if _is_axi_category(name)}

        # Map existing target categories and channels by name
        target_cats_by_name = {
            c["name"]: c for c in target_channels if c["type"] == 4
        }
        target_text_by_name = {
            c["name"]: c for c in target_channels if c["type"] == 0
        }

        # Identify killed categories to exclude from migration
        killed_cat_ids = {cid for cid, name in source_cats.items()
                         if name == "Killed" or (name.startswith("Killed ") and name[7:].isdigit())}

        # Step 1: Ensure categories exist in target
        cat_id_map: dict[str, str] = {}  # source_cat_id -> target_cat_id
        for source_cat_id, cat_name in sorted(source_cats.items(), key=lambda x: x[1]):
            if not _is_axi_category(cat_name):
                continue
            if source_cat_id in killed_cat_ids:
                print(f"  Skipping killed category '{cat_name}'")
                continue
            if cat_name in target_cats_by_name:
                cat_id_map[source_cat_id] = target_cats_by_name[cat_name]["id"]
                print(f"  Category '{cat_name}' already exists in target")
            elif dry_run:
                print(f"  [DRY RUN] Would create category '{cat_name}'")
                cat_id_map[source_cat_id] = "dry-run"
            else:
                result = client.post(f"/guilds/{target_guild}/channels", json={
                    "name": cat_name,
                    "type": 4,  # GUILD_CATEGORY
                })
                cat_id_map[source_cat_id] = result["id"]
                print(f"  Created category '{cat_name}' (id={result['id']})")
                time.sleep(1)  # rate limit courtesy

        # Step 2: Copy text channels (with topics)
        # Include channels in Axi/Active categories + uncategorized axi-master
        # Skip killed channels — they're dead sessions, no need to migrate
        channels_to_copy = []
        for ch in source_channels:
            if ch["type"] != 0:  # text channels only
                continue
            parent = ch.get("parent_id")
            if parent in axi_cat_ids and parent not in killed_cat_ids:
                channels_to_copy.append(ch)
            elif parent is None and ch["name"] in ("axi-master",):
                # Uncategorized master channel
                channels_to_copy.append(ch)

        print(f"\nChannels to migrate: {len(channels_to_copy)}")

        for ch in sorted(channels_to_copy, key=lambda c: c.get("position", 0)):
            name = ch["name"]
            topic = ch.get("topic") or ""
            parent = ch.get("parent_id")
            target_parent = cat_id_map.get(parent) if parent else None

            if name in target_text_by_name:
                existing = target_text_by_name[name]
                existing_topic = existing.get("topic") or ""
                if existing_topic == topic:
                    print(f"  #{name} — already exists with matching topic, skipping")
                else:
                    if dry_run:
                        print(f"  [DRY RUN] #{name} — exists but topic differs, would update")
                    else:
                        client.request("PATCH", f"/channels/{existing['id']}", json={"topic": topic})
                        print(f"  #{name} — updated topic")
                        time.sleep(1)
                continue

            payload: dict = {
                "name": name,
                "type": 0,  # GUILD_TEXT
            }
            if topic:
                payload["topic"] = topic
            if target_parent and target_parent != "dry-run":
                payload["parent_id"] = target_parent

            if dry_run:
                cat_label = f" (in {source_cats.get(parent, 'uncategorized')})" if parent else " (uncategorized)"
                print(f"  [DRY RUN] Would create #{name}{cat_label}")
                if topic:
                    print(f"            topic: {topic}")
            else:
                result = client.post(f"/guilds/{target_guild}/channels", json=payload)
                print(f"  Created #{name} (id={result['id']})")
                time.sleep(1)  # rate limit courtesy

    print("\nDone. Next steps:")
    print(f"  1. Update .env: DISCORD_GUILD_ID={target_guild}")
    print("  2. Restart the bot")
    print("  3. Bot will auto-discover channels and resume sessions")


def main() -> None:
    parser = argparse.ArgumentParser(description="Migrate Axi channels to a new guild")
    parser.add_argument("target_guild", help="Target guild ID")
    parser.add_argument("--dry-run", action="store_true", help="Show what would be done without making changes")
    args = parser.parse_args()

    token = os.environ.get("DISCORD_TOKEN")
    source_guild = os.environ.get("DISCORD_GUILD_ID")

    if not token:
        print("Error: DISCORD_TOKEN not set", file=sys.stderr)
        sys.exit(1)
    if not source_guild:
        print("Error: DISCORD_GUILD_ID not set", file=sys.stderr)
        sys.exit(1)
    if args.target_guild == source_guild:
        print("Error: target guild is the same as source guild", file=sys.stderr)
        sys.exit(1)

    print(f"Source guild: {source_guild}")
    print(f"Target guild: {args.target_guild}")
    print(f"Mode: {'DRY RUN' if args.dry_run else 'LIVE'}\n")

    migrate(token, source_guild, args.target_guild, dry_run=args.dry_run)


if __name__ == "__main__":
    main()
