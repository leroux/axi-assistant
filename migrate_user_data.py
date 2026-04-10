#!/usr/bin/env python3
"""One-time migration: consolidate user data into ~/app-user-data/axi-assistant/.

Moves:
  ~/axi-user-data/*           -> ~/app-user-data/axi-assistant/
  {repo}/profile/             -> ~/app-user-data/axi-assistant/profile/
  {repo}/schedules.json       -> merged into ~/app-user-data/axi-assistant/schedules.json

Run from the repo root:
  python migrate_user_data.py [--dry-run]
"""

import json
import shutil
import sys
from pathlib import Path

DRY_RUN = "--dry-run" in sys.argv
REPO = Path(__file__).resolve().parent
OLD_USER_DATA = Path.home() / "axi-user-data"
NEW_USER_DATA = Path.home() / "app-user-data" / "axi-assistant"
REPO_PROFILE = REPO / "profile"
REPO_SCHEDULES = REPO / "schedules.json"


def log(msg: str) -> None:
    prefix = "[DRY RUN] " if DRY_RUN else ""
    print(f"{prefix}{msg}")


def move_item(src: Path, dst: Path) -> None:
    if not src.exists():
        log(f"  SKIP (not found): {src}")
        return
    log(f"  {src} -> {dst}")
    if not DRY_RUN:
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(src), str(dst))


def merge_schedules() -> None:
    """Merge repo schedules.json entries into the user-data schedules.json."""
    if not REPO_SCHEDULES.exists():
        log("  SKIP (not found): repo schedules.json")
        return

    repo_entries = json.loads(REPO_SCHEDULES.read_text())
    ud_schedules = NEW_USER_DATA / "schedules.json"

    if ud_schedules.exists():
        existing = json.loads(ud_schedules.read_text())
    else:
        existing = []

    existing_names = {e["name"] for e in existing}
    added = []
    for entry in repo_entries:
        if entry["name"] not in existing_names:
            added.append(entry["name"])
            if not DRY_RUN:
                existing.append(entry)

    if added:
        log(f"  Adding {len(added)} schedule entries: {', '.join(added)}")
        if not DRY_RUN:
            ud_schedules.write_text(json.dumps(existing, indent=2) + "\n")
    else:
        log("  All repo schedule entries already present — nothing to merge")


def main() -> None:
    if DRY_RUN:
        print("=== DRY RUN — no changes will be made ===\n")

    # Step 1: Create new directory
    log(f"Creating {NEW_USER_DATA}")
    if not DRY_RUN:
        NEW_USER_DATA.mkdir(parents=True, exist_ok=True)

    # Step 2: Move contents from old user-data
    if OLD_USER_DATA.exists():
        log(f"\nMoving contents from {OLD_USER_DATA}:")
        for item in sorted(OLD_USER_DATA.iterdir()):
            # Skip dotfiles that are shell/git config artifacts
            if item.name.startswith("."):
                log(f"  SKIP (dotfile): {item.name}")
                continue
            # Skip empty placeholder files (0 bytes)
            if item.is_file() and item.stat().st_size == 0:
                log(f"  SKIP (empty placeholder): {item.name}")
                continue
            move_item(item, NEW_USER_DATA / item.name)
    else:
        log(f"\nSKIP: {OLD_USER_DATA} does not exist")

    # Step 3: Move profile from repo
    if REPO_PROFILE.exists():
        log(f"\nMoving profile from {REPO_PROFILE}:")
        move_item(REPO_PROFILE, NEW_USER_DATA / "profile")
    else:
        log(f"\nSKIP: {REPO_PROFILE} does not exist")

    # Step 4: Merge schedule entries
    log("\nMerging schedule entries:")
    merge_schedules()

    # Step 5: Delete dead repo schedules.json
    if REPO_SCHEDULES.exists():
        log(f"\nDeleting dead {REPO_SCHEDULES}")
        if not DRY_RUN:
            REPO_SCHEDULES.unlink()

    # Step 6: Clean up old directory
    if OLD_USER_DATA.exists():
        remaining = [p for p in OLD_USER_DATA.iterdir()]
        real_remaining = [p for p in remaining if not p.name.startswith(".") and not (p.is_file() and p.stat().st_size == 0)]
        if not real_remaining:
            log(f"\nOld directory {OLD_USER_DATA} has only dotfiles/empty placeholders remaining.")
            log("Safe to remove manually: rm -rf ~/axi-user-data")
        else:
            log(f"\nWARNING: {OLD_USER_DATA} still has items: {[p.name for p in real_remaining]}")
            log("Review before removing.")

    # Verify
    if not DRY_RUN:
        log(f"\n=== Verification ===")
        log(f"Contents of {NEW_USER_DATA}:")
        for item in sorted(NEW_USER_DATA.iterdir()):
            kind = "dir" if item.is_dir() else "file"
            log(f"  [{kind}] {item.name}")

    print("\nDone!")


if __name__ == "__main__":
    main()
