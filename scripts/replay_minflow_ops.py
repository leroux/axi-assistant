#!/usr/bin/env python3
"""
Smart replay of MinFlow operations with ID remapping.

Replays minflow mutations from Claude session logs against the current workspace,
handling the fact that card IDs change on creation. Builds an old→new ID mapping
as it goes, remapping references in subsequent operations.

Usage:
    # Step 1: Generate the replay plan (dry run, no mutations)
    python scripts/replay_minflow_ops.py --plan > /tmp/replay-plan.txt

    # Step 2: Execute the replay
    python scripts/replay_minflow_ops.py --execute

    # Step 3: If something goes wrong, undo everything
    python scripts/replay_minflow_ops.py --undo-log /tmp/minflow-replay-undo.sh

Options:
    --since DATE     Start date (default: 2026-03-24)
    --plan           Show what would be executed (no mutations)
    --execute        Actually run the replay
    --step           Interactive mode: confirm each operation
    --stop-on-error  Stop at first failed operation (default: skip and continue)
    --undo-log FILE  Write undo commands to this file (for rollback)
"""

import argparse
import json
import os
import re
import shlex
import subprocess
import sys
from pathlib import Path
from datetime import datetime


CLAUDE_PROJECTS = Path.home() / ".claude" / "projects"

MUTATION_PATTERNS = [
    r"minflow\s+card\s+add\b",
    r"minflow\s+card\s+done\b",
    r"minflow\s+card\s+update\b",
    r"minflow\s+card\s+delete\b",
    r"minflow\s+card\s+reorder\b",
    r"minflow\s+deck\s+update\b",
    r"minflow\s+deck\s+create\b",
    r"minflow\s+deck\s+delete\b",
    r"minflow\s+undo\b",
    r"minflow\s+redo\b",
]
MUTATION_RE = re.compile("|".join(MUTATION_PATTERNS))


def is_mutation_command(cmd: str) -> bool:
    if not MUTATION_RE.search(cmd):
        return False
    stripped = cmd.strip()
    # Skip heredoc scripts that mention minflow as a pattern/string
    if stripped.startswith(("python", "node", "cat ")) and "<<" in stripped:
        heredoc_start = stripped.find("<<")
        before_heredoc = stripped[:heredoc_start]
        if MUTATION_RE.search(before_heredoc):
            return True
        return False
    # Skip grep/search commands
    if stripped.startswith(("grep", "rg ", "find ")) and "minflow" in stripped:
        return False
    return True


def resolve_deck_vars(raw_cmd: str, extracted_cmd: str) -> str:
    """Resolve $DECK / $D variables using assignments from the raw command.

    Many commands look like: DECK="mm5jypnollx4dmq2i5h" && minflow card add "$DECK" ...
    The extraction step strips the assignment, leaving $DECK unresolved.
    This function finds the assignment and substitutes it back.
    """
    # Look for DECK=<value> or D=<value> in the raw command
    for pattern in [
        r'\bDECK="([a-z0-9]+)"',
        r'\bDECK=([a-z0-9]+)\b',
        r'\bD="([a-z0-9]+)"',
        r'\bD=([a-z0-9]+)\b',
    ]:
        m = re.search(pattern, raw_cmd)
        if m:
            deck_id = m.group(1)
            # Replace all forms: "$DECK", $DECK, ${DECK}, "$D", $D, ${D}
            result = extracted_cmd
            if 'DECK' in pattern:
                result = result.replace('"$DECK"', deck_id)
                result = result.replace('$DECK', deck_id)
                result = result.replace('${DECK}', deck_id)
            else:
                result = result.replace('"$D"', deck_id)
                result = result.replace('$D ', deck_id + ' ')
                result = result.replace('${D}', deck_id)
            return result
    return extracted_cmd


def extract_minflow_command(full_cmd: str) -> str:
    """Extract clean minflow command from shell noise."""
    parts = re.split(r'\s*&&\s*|\s*;\s*', full_cmd)
    for part in parts:
        lines = part.strip().split('\n')
        combined = []
        for line in lines:
            stripped = line.strip()
            if stripped.startswith('#'):
                continue
            combined.append(line)

        rejoined = '\n'.join(combined).strip()
        if not MUTATION_RE.search(rejoined):
            continue

        match = MUTATION_RE.search(rejoined)
        if not match:
            continue

        cmd_start = rejoined.rfind("minflow", 0, match.end())
        if cmd_start == -1:
            cmd_start = 0
        remainder = rejoined[cmd_start:]

        # Find unquoted pipe
        in_single = False
        in_double = False
        escaped = False
        pipe_pos = None
        for i, ch in enumerate(remainder):
            if escaped:
                escaped = False
                continue
            if ch == '\\':
                escaped = True
                continue
            if ch == "'" and not in_double:
                in_single = not in_single
            elif ch == '"' and not in_single:
                in_double = not in_double
            elif ch == '|' and not in_single and not in_double:
                pipe_pos = i
                break

        if pipe_pos is not None:
            remainder = remainder[:pipe_pos]

        remainder = re.sub(r'\s*2>&1\s*$', '', remainder)
        remainder = re.sub(r'\s*2>/dev/null\s*$', '', remainder)
        remainder = re.sub(r'\s*>\s*/dev/null\s*$', '', remainder)
        remainder = remainder.strip()

        # Truncate trailing script lines, but only outside quoted strings.
        # Without quote tracking, a --notes value containing "...done..." on a
        # new line would be truncated mid-quote, producing broken shell syntax.
        lines = remainder.split('\n')
        clean_lines = []
        in_sq = False
        in_dq = False
        for line in lines:
            stripped = line.strip()
            if clean_lines and not in_sq and not in_dq and (
                stripped.startswith('echo ')
                or stripped.startswith('done')
                or stripped.startswith('for ')
                or re.match(r'^[A-Z_]+=', stripped)
            ):
                break
            clean_lines.append(line)
            # Track quote state across lines
            esc = False
            for ch in line:
                if esc:
                    esc = False
                    continue
                if ch == '\\':
                    esc = True
                    continue
                if ch == "'" and not in_dq:
                    in_sq = not in_sq
                elif ch == '"' and not in_sq:
                    in_dq = not in_dq
        remainder = '\n'.join(clean_lines).strip()
        return remainder

    return full_cmd.strip()


def extract_result_id(result_text: str) -> str | None:
    """Extract the 'id' field from a minflow CLI result.

    Handles multiple output formats the CLI has used over time:
    1. JSON object: {"id": "mn...", ...}
    2. "Added: mn..." format
    3. Bare ID string (just the ID, no wrapper)
    4. ID as first token in multi-line output
    """
    if not result_text:
        return None
    # Strategy 1: JSON parsing
    try:
        data = json.loads(result_text)
        if isinstance(data, dict):
            return data.get("id")
    except (json.JSONDecodeError, TypeError):
        pass
    # Strategy 2: "id" field in partial/malformed JSON
    m = re.search(r'"id"\s*:\s*"([^"]+)"', result_text)
    if m:
        return m.group(1)
    # Strategy 3: "Added: <id>" format
    m = re.search(r'Added:\s+([a-z0-9]{15,25})', result_text)
    if m:
        return m.group(1)
    # Strategy 4: bare ID (entire output is just an ID)
    stripped = result_text.strip()
    if re.match(r'^[a-z0-9]{15,25}$', stripped):
        return stripped
    # Strategy 5: ID-like string as first token (e.g. multi-line output
    # where the first line is the new card ID)
    m = re.match(r'^(m[a-z0-9]{14,24})\b', stripped)
    if m:
        return m.group(1)
    return None


def has_unresolved_var(cmd: str) -> bool:
    """Check if command has shell variables in structural positions."""
    notes_pos = cmd.find("--notes")
    done_pos = cmd.find("--done")
    check_end = min(
        notes_pos if notes_pos >= 0 else len(cmd),
        done_pos if done_pos >= 0 else len(cmd)
    )
    return bool(re.search(r'(?<!\\)\$[A-Z_{\[]', cmd[:check_end]))


def scan_all_sessions(since_ts: str) -> list[dict]:
    """Scan all JSONL files and extract operations with result IDs."""
    jsonl_files = []
    if CLAUDE_PROJECTS.exists():
        for root, dirs, files in os.walk(CLAUDE_PROJECTS):
            for fname in files:
                if fname.endswith(".jsonl"):
                    jsonl_files.append(Path(root) / fname)

    print(f"Scanning {len(jsonl_files)} session files...", file=sys.stderr)

    # Collect tool_uses and tool_results across all files
    all_tool_uses = {}  # tool_use_id -> {timestamp, command, raw_command, file}
    all_tool_results = {}  # tool_use_id -> {is_error, output}

    # Two-pass scan: first pass finds tool_uses, second pass finds their results.
    # tool_result JSONL lines often don't contain "minflow" (they contain the JSON
    # output of the command, not the command itself), so a single-pass "minflow"
    # filter misses them.

    # Pass 1: find all minflow mutation tool_uses
    for i, fpath in enumerate(jsonl_files):
        if (i + 1) % 200 == 0:
            print(f"  {i+1}/{len(jsonl_files)}...", file=sys.stderr)

        try:
            with open(fpath) as f:
                for line in f:
                    if "minflow" not in line:
                        continue
                    try:
                        obj = json.loads(line)
                    except json.JSONDecodeError:
                        continue

                    ts = obj.get("timestamp", "")
                    if ts < since_ts:
                        continue

                    msg = obj.get("message", {})
                    content = msg.get("content", [])
                    if not isinstance(content, list):
                        continue

                    for block in content:
                        if not isinstance(block, dict):
                            continue

                        if block.get("type") == "tool_use":
                            inp = block.get("input", {})
                            cmd = inp.get("command", "")
                            if is_mutation_command(cmd):
                                tid = block.get("id", "")
                                extracted = extract_minflow_command(cmd)
                                resolved = resolve_deck_vars(cmd, extracted)
                                all_tool_uses[tid] = {
                                    "timestamp": ts,
                                    "raw_command": cmd,
                                    "command": resolved,
                                    "file": str(fpath),
                                }

                        # Also capture tool_results in lines that happen to contain "minflow"
                        elif block.get("type") == "tool_result":
                            tid = block.get("tool_use_id", "")
                            if tid in all_tool_uses:
                                is_err = block.get("is_error", False)
                                rc = block.get("content", "")
                                if isinstance(rc, list):
                                    rc = "".join(
                                        item.get("text", "")
                                        for item in rc
                                        if isinstance(item, dict)
                                    )
                                has_error = (
                                    is_err
                                    or "Error:" in str(rc)
                                    or "Traceback" in str(rc)
                                )
                                all_tool_results[tid] = {
                                    "is_error": has_error,
                                    "output": rc if isinstance(rc, str) else str(rc),
                                }
        except (OSError, UnicodeDecodeError):
            continue

    # Pass 2: find tool_results for tool_uses that are still missing results.
    # These are in JSONL lines that don't contain "minflow" (e.g. JSON output
    # from deck create / card add).
    missing_results = {tid for tid in all_tool_uses if tid not in all_tool_results}
    if missing_results:
        print(f"  Pass 2: looking for {len(missing_results)} missing tool_results...", file=sys.stderr)
        # Only scan files that contained tool_uses (tool_results are in the same file)
        files_with_uses = {all_tool_uses[tid]["file"] for tid in missing_results}
        for fpath_str in files_with_uses:
            if not missing_results:
                break
            fpath = Path(fpath_str)
            try:
                with open(fpath) as f:
                    for line in f:
                        # Quick pre-filter: tool_result lines contain "tool_use_id"
                        if "tool_use_id" not in line:
                            continue
                        try:
                            obj = json.loads(line)
                        except json.JSONDecodeError:
                            continue

                        msg = obj.get("message", {})
                        content = msg.get("content", [])
                        if not isinstance(content, list):
                            continue

                        for block in content:
                            if not isinstance(block, dict):
                                continue
                            if block.get("type") == "tool_result":
                                tid = block.get("tool_use_id", "")
                                if tid in missing_results:
                                    is_err = block.get("is_error", False)
                                    rc = block.get("content", "")
                                    if isinstance(rc, list):
                                        rc = "".join(
                                            item.get("text", "")
                                            for item in rc
                                            if isinstance(item, dict)
                                        )
                                    has_error = (
                                        is_err
                                        or "Error:" in str(rc)
                                        or "Traceback" in str(rc)
                                    )
                                    all_tool_results[tid] = {
                                        "is_error": has_error,
                                        "output": rc if isinstance(rc, str) else str(rc),
                                    }
                                    missing_results.discard(tid)
            except (OSError, UnicodeDecodeError):
                continue
        if missing_results:
            print(f"  {len(missing_results)} tool_results still not found", file=sys.stderr)

    # Build operation list
    ops = []
    for tid, info in all_tool_uses.items():
        result = all_tool_results.get(tid)
        success = True
        result_output = ""
        if result:
            success = not result["is_error"]
            result_output = result["output"]

        # Extract result ID for card add / deck create
        result_id = None
        cmd = info["command"]
        if re.search(r"minflow\s+card\s+add\b", cmd) or re.search(r"minflow\s+deck\s+create\b", cmd):
            result_id = extract_result_id(result_output)

        ops.append({
            "timestamp": info["timestamp"],
            "command": cmd,
            "tool_use_id": tid,
            "success": success,
            "result_id": result_id,
            "has_unresolved_var": has_unresolved_var(cmd),
        })

    # Deduplicate and sort
    seen = set()
    unique = []
    for op in ops:
        if op["tool_use_id"] not in seen:
            seen.add(op["tool_use_id"])
            unique.append(op)
    unique.sort(key=lambda x: x["timestamp"])

    # Filter to successful only
    successful = [op for op in unique if op["success"]]
    failed = len(unique) - len(successful)
    print(f"Found {len(unique)} ops, {len(successful)} successful, {failed} failed", file=sys.stderr)

    return successful


def get_existing_card_ids() -> set[str]:
    """Get all card IDs currently in the workspace."""
    try:
        result = subprocess.run(
            ["minflow", "deck", "list"],
            capture_output=True, text=True, timeout=10
        )
        if result.returncode != 0:
            print(f"WARNING: minflow deck list failed: {result.stderr}", file=sys.stderr)
            return set()

        decks = json.loads(result.stdout)
        ids = set()
        for deck in decks:
            ids.add(deck["id"])
            for card in deck.get("cards", []):
                ids.add(card["id"])
        return ids
    except Exception as e:
        print(f"WARNING: couldn't get existing IDs: {e}", file=sys.stderr)
        return set()


def get_existing_deck_ids() -> set[str]:
    """Get all deck IDs currently in the workspace."""
    try:
        result = subprocess.run(
            ["minflow", "deck", "list"],
            capture_output=True, text=True, timeout=10
        )
        if result.returncode != 0:
            return set()
        decks = json.loads(result.stdout)
        return {d["id"] for d in decks}
    except Exception:
        return set()


def remap_ids(cmd: str, id_map: dict[str, str]) -> str:
    """Replace old IDs with new IDs in a command."""
    for old_id, new_id in id_map.items():
        cmd = cmd.replace(old_id, new_id)
    return cmd


def classify_op(cmd: str) -> str:
    """Classify the operation type."""
    if re.search(r"minflow\s+card\s+add\b", cmd):
        return "card_add"
    elif re.search(r"minflow\s+card\s+done\b", cmd):
        return "card_done"
    elif re.search(r"minflow\s+card\s+update\b", cmd):
        return "card_update"
    elif re.search(r"minflow\s+card\s+delete\b", cmd):
        return "card_delete"
    elif re.search(r"minflow\s+card\s+reorder\b", cmd):
        return "card_reorder"
    elif re.search(r"minflow\s+deck\s+update\b", cmd):
        return "deck_update"
    elif re.search(r"minflow\s+deck\s+create\b", cmd):
        return "deck_create"
    elif re.search(r"minflow\s+deck\s+delete\b", cmd):
        return "deck_delete"
    elif re.search(r"minflow\s+undo\b", cmd):
        return "undo"
    elif re.search(r"minflow\s+redo\b", cmd):
        return "redo"
    return "unknown"


def execute_command(cmd: str) -> tuple[bool, str]:
    """Execute a minflow command and return (success, output)."""
    try:
        # Use shlex to parse the command into tokens, avoiding shell=True quoting
        # issues with embedded quotes and special characters in --notes/--done text.
        try:
            args = shlex.split(cmd)
        except ValueError:
            # Fallback to shell=True if shlex can't parse (malformed quoting)
            args = cmd
        result = subprocess.run(
            args, shell=isinstance(args, str),
            capture_output=True, text=True, timeout=300
        )
        success = result.returncode == 0 and "Error" not in result.stderr
        # Return stdout on success (for ID extraction), stderr+stdout on failure (for diagnostics)
        if success:
            return True, result.stdout
        else:
            output = result.stdout
            if result.stderr:
                output = f"STDERR: {result.stderr.strip()}\nSTDOUT: {result.stdout.strip()}"
            return False, output
    except subprocess.TimeoutExpired:
        return False, "TIMEOUT"
    except Exception as e:
        return False, str(e)


def main():
    parser = argparse.ArgumentParser(description="Smart replay of MinFlow operations")
    parser.add_argument("--since", default="2026-03-24", help="Start date (default: 2026-03-24)")
    parser.add_argument("--plan", action="store_true", help="Show replay plan without executing")
    parser.add_argument("--execute", action="store_true", help="Execute the replay")
    parser.add_argument("--step", action="store_true", help="Interactive: confirm each op")
    parser.add_argument("--stop-on-error", action="store_true", help="Stop at first failure")
    parser.add_argument("--undo-log", help="Write undo commands to this file")
    parser.add_argument("--skip-reorders", action="store_true",
                        help="Skip card reorder ops (often use shell vars)")
    parser.add_argument("--skip-undo-redo", action="store_true", default=True,
                        help="Skip undo/redo ops (default: true, they're positional)")
    args = parser.parse_args()

    if not args.plan and not args.execute:
        print("ERROR: specify --plan or --execute", file=sys.stderr)
        sys.exit(1)

    since_ts = f"{args.since}T00:00:00.000Z"

    # Scan sessions
    ops = scan_all_sessions(since_ts)

    # Get existing IDs from current workspace
    print("Getting current workspace state...", file=sys.stderr)
    existing_ids = get_existing_card_ids()
    existing_decks = get_existing_deck_ids()
    print(f"  {len(existing_ids)} existing IDs (cards + decks)", file=sys.stderr)

    # Build replay plan
    id_map = {}  # old_id -> new_id
    skipped = 0
    planned = 0
    executed = 0
    failed = 0
    undo_cmds = []

    for i, op in enumerate(ops):
        cmd = op["command"]
        op_type = classify_op(cmd)
        ts = op["timestamp"]

        # Skip undo/redo (positional, can't replay)
        if op_type in ("undo", "redo") and args.skip_undo_redo:
            if args.plan:
                print(f"[SKIP:undo/redo] [{ts}] {cmd}")
            skipped += 1
            continue

        # Skip unresolved variables
        if op["has_unresolved_var"]:
            if args.plan:
                print(f"[SKIP:unresolved_var] [{ts}] {cmd}")
            skipped += 1
            continue

        # Skip reorders if requested
        if op_type == "card_reorder" and args.skip_reorders:
            if args.plan:
                print(f"[SKIP:reorder] [{ts}] {cmd}")
            skipped += 1
            continue

        # Remap IDs
        remapped_cmd = remap_ids(cmd, id_map)

        # Auto-fix card add missing --top/--bottom (required since Feb 28 2026,
        # but older sessions didn't include it; default was bottom)
        if op_type == "card_add" and "--top" not in remapped_cmd and "--bottom" not in remapped_cmd:
            remapped_cmd = remapped_cmd.rstrip() + " --bottom"

        # For card_done/update/delete/reorder: check if the card ID exists
        # (either in workspace already or in our id_map)
        if op_type in ("card_done", "card_update", "card_delete", "card_reorder"):
            # Extract the card ID from the command (2nd positional after deck-id)
            parts = remapped_cmd.split()
            card_id = None
            for j, p in enumerate(parts):
                if j >= 3 and re.match(r'^[a-z0-9]{10,}$', p):
                    card_id = p
                    break

            if card_id and card_id not in existing_ids and card_id not in id_map.values():
                # Check if the ORIGINAL id was a result_id we know about
                # (it was created in an earlier card_add that succeeded)
                original_found = any(
                    prev_op["result_id"] == card_id
                    for prev_op in ops[:i]
                    if prev_op.get("result_id")
                )
                if not original_found and card_id not in existing_ids:
                    if args.plan:
                        print(f"[SKIP:unknown_id:{card_id}] [{ts}] {remapped_cmd}")
                    skipped += 1
                    continue

        if args.plan:
            if remapped_cmd != cmd:
                print(f"[REMAP] [{ts}] {remapped_cmd}")
                print(f"  (original: {cmd})")
            else:
                print(f"[OK] [{ts}] {remapped_cmd}")
            planned += 1

        elif args.execute:
            if args.step:
                print(f"\n[{i+1}/{len(ops)}] {remapped_cmd}")
                resp = input("  Execute? [y/n/q]: ").strip().lower()
                if resp == 'q':
                    print("Aborted by user.")
                    break
                if resp != 'y':
                    skipped += 1
                    continue

            success, output = execute_command(remapped_cmd)

            if success:
                executed += 1
                # If this was a card_add, capture the new ID
                if op_type == "card_add" and op.get("result_id"):
                    new_id = extract_result_id(output)
                    if new_id and new_id != op["result_id"]:
                        id_map[op["result_id"]] = new_id
                        existing_ids.add(new_id)
                        print(f"  [MAP] {op['result_id']} → {new_id}", file=sys.stderr)
                    elif new_id:
                        existing_ids.add(new_id)

                elif op_type == "deck_create":
                    new_id = extract_result_id(output)
                    if new_id and op.get("result_id") and new_id != op["result_id"]:
                        id_map[op["result_id"]] = new_id
                        existing_ids.add(new_id)
                        print(f"  [MAP] {op['result_id']} → {new_id}", file=sys.stderr)

                # Track undo
                if op_type == "card_add":
                    new_id = extract_result_id(output)
                    if new_id:
                        # Extract deck_id from command
                        parts = remapped_cmd.split()
                        deck_id = parts[3] if len(parts) > 3 else "?"
                        undo_cmds.append(f"minflow card delete {deck_id} {new_id}")

                if not args.step:
                    print(f"  [{executed}] OK: {remapped_cmd[:80]}", file=sys.stderr)
            else:
                failed += 1
                print(f"  [FAIL] {remapped_cmd[:80]}", file=sys.stderr)
                print(f"    output: {output[:200]}", file=sys.stderr)
                if args.stop_on_error:
                    print("Stopping due to --stop-on-error", file=sys.stderr)
                    break

    # Summary
    print(f"\n{'='*60}", file=sys.stderr)
    if args.plan:
        print(f"PLAN: {planned} ops to execute, {skipped} skipped", file=sys.stderr)
    else:
        print(f"DONE: {executed} executed, {failed} failed, {skipped} skipped", file=sys.stderr)
        print(f"ID mappings created: {len(id_map)}", file=sys.stderr)

    if id_map:
        print(f"\nID Mapping (old → new):", file=sys.stderr)
        for old, new in list(id_map.items())[:20]:
            print(f"  {old} → {new}", file=sys.stderr)
        if len(id_map) > 20:
            print(f"  ... and {len(id_map) - 20} more", file=sys.stderr)

    # Write undo log (non-fatal — don't crash after all ops succeeded)
    if args.undo_log and undo_cmds:
        try:
            with open(args.undo_log, 'w') as f:
                f.write("#!/bin/bash\n")
                f.write("# Undo log — run this to reverse the replay\n")
                f.write("# (only covers card additions, not updates/completions)\n\n")
                for cmd in reversed(undo_cmds):
                    f.write(f"{cmd}\n")
            print(f"Undo log written to: {args.undo_log}", file=sys.stderr)
        except OSError as e:
            print(f"WARNING: Could not write undo log to {args.undo_log}: {e}", file=sys.stderr)
            print(f"  (All {executed} operations were already executed successfully)", file=sys.stderr)

    return 0


if __name__ == "__main__":
    sys.exit(main())
