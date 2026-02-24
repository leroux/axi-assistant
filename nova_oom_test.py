#!/usr/bin/env python3
"""OOM stress test: spawn many agents that stay awake via sleep 300."""
import os
import subprocess
import sys
import time
from datetime import datetime, timezone

import httpx
from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))

API_BASE = "https://discord.com/api/v10"
NOVA_GUILD_ID = "1475631458243710977"
MASTER_CHANNEL = "1475637434736840877"
FINISHED_SENTINEL = "Bot has finished responding"


def get_token() -> str:
    return os.environ["DISCORD_TOKEN"]


def make_client(token: str) -> httpx.Client:
    return httpx.Client(
        base_url=API_BASE,
        headers={"Authorization": f"Bot {token}"},
        timeout=httpx.Timeout(30.0),
    )


def send(client: httpx.Client, channel_id: str, content: str) -> str:
    resp = client.post(f"/channels/{channel_id}/messages", json={"content": content})
    if resp.status_code not in (200, 201):
        print(f"  !! Send failed: {resp.status_code}", file=sys.stderr)
        return ""
    return resp.json()["id"]


def wait_done(client: httpx.Client, channel_id: str, after_id: str, timeout: int = 120) -> float:
    start = time.time()
    while time.time() - start < timeout:
        time.sleep(3)
        resp = client.get(f"/channels/{channel_id}/messages", params={"after": after_id, "limit": 50})
        if resp.status_code != 200:
            continue
        for msg in resp.json():
            if FINISHED_SENTINEL in msg.get("content", ""):
                return time.time() - start
    return time.time() - start


def get_stats() -> dict:
    mem = subprocess.run(["free", "-b"], capture_output=True, text=True)
    parts = mem.stdout.strip().split("\n")[1].split()
    total, used, available = int(parts[1]), int(parts[2]), int(parts[6])

    ps = subprocess.run(["ps", "aux"], capture_output=True, text=True)
    claude_lines = [l for l in ps.stdout.split("\n")
                    if "claude" in l.lower() and "grep" not in l and "oom" not in l and "stress" not in l]

    nova_mem = subprocess.run(
        ["systemctl", "--user", "show", "axi-nova.service", "--property=MemoryCurrent"],
        capture_output=True, text=True
    )
    nova_bytes_str = nova_mem.stdout.strip().split("=")[1] if "=" in nova_mem.stdout else "0"
    try:
        nova_mb = int(nova_bytes_str) / (1024**2)
    except ValueError:
        nova_mb = 0

    return {
        "mem_total_gb": total / (1024**3),
        "mem_used_gb": used / (1024**3),
        "mem_avail_gb": available / (1024**3),
        "mem_pct": used / total * 100,
        "claude_procs": len(claude_lines),
        "nova_mb": nova_mb,
    }


def main():
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--max", type=int, default=15, help="Number of agents to spawn")
    parser.add_argument("--output", default="/home/ubuntu/axi-user-data/nova-oom-stress.md")
    args = parser.parse_args()

    token = get_token()
    count = args.max

    print("=" * 60)
    print(f"  OOM STRESS TEST — {count} agents with sleep 300")
    print(f"  {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}")
    print("=" * 60)

    s = get_stats()
    print(f"\n📊 Baseline: {s['mem_used_gb']:.1f}/{s['mem_total_gb']:.1f}GB "
          f"({s['mem_pct']:.0f}%), nova={s['nova_mb']:.0f}MB, procs={s['claude_procs']}")
    snapshots = [("baseline", 0, s)]

    with make_client(token) as client:
        send(client, MASTER_CHANNEL,
             f"⚠️ OOM stress test: spawning {count} agents that run `sleep 300` to stay alive.")

        start_time = time.time()

        # Spawn agents one at a time, checking resources after each
        for i in range(1, count + 1):
            name = f"oom-{i:02d}"
            print(f"\n🚀 [{i}/{count}] Spawning {name}...")

            mid = send(client, MASTER_CHANNEL,
                       f"Spawn agent {name}. Prompt: 'Run this bash command: sleep 300. "
                       f"Do not do anything else before or after, just run sleep 300.'")

            if not mid:
                print(f"  ❌ Failed to send spawn command")
                break

            # Wait for Nova to acknowledge
            elapsed = wait_done(client, MASTER_CHANNEL, mid, timeout=90)
            print(f"  ✅ Acknowledged in {elapsed:.0f}s")

            # Give agent a moment to start its sleep command
            time.sleep(8)

            # Check resources
            s = get_stats()
            wall = time.time() - start_time
            snapshots.append((f"agent-{i}", wall, s))
            print(f"  📊 mem={s['mem_used_gb']:.1f}GB ({s['mem_pct']:.0f}%), "
                  f"nova={s['nova_mb']:.0f}MB, procs={s['claude_procs']}, "
                  f"avail={s['mem_avail_gb']:.1f}GB")

            # Safety check: abort if memory is critically low
            if s['mem_avail_gb'] < 0.5:
                print(f"\n🛑 ABORTING — available memory below 500MB!")
                snapshots.append(("ABORT", time.time() - start_time, s))
                break

            if s['mem_pct'] > 90:
                print(f"\n🛑 ABORTING — memory usage above 90%!")
                snapshots.append(("ABORT", time.time() - start_time, s))
                break

        # Final snapshot
        s = get_stats()
        snapshots.append(("peak", time.time() - start_time, s))
        print(f"\n📊 Peak: {s['mem_used_gb']:.1f}/{s['mem_total_gb']:.1f}GB "
              f"({s['mem_pct']:.0f}%), nova={s['nova_mb']:.0f}MB, procs={s['claude_procs']}")

        # Cleanup: kill all agents
        print(f"\n🧹 Killing all oom-* agents...")
        # Kill in batches of 5
        spawned = [f"oom-{j:02d}" for j in range(1, i + 1)]
        for batch_start in range(0, len(spawned), 5):
            batch = spawned[batch_start:batch_start + 5]
            mid = send(client, MASTER_CHANNEL, f"Kill these agents: {', '.join(batch)}")
            if mid:
                wait_done(client, MASTER_CHANNEL, mid, timeout=120)
            time.sleep(2)

        # Post-cleanup stats
        time.sleep(5)
        s = get_stats()
        snapshots.append(("cleanup", time.time() - start_time, s))
        print(f"\n📊 After cleanup: {s['mem_used_gb']:.1f}GB ({s['mem_pct']:.0f}%), procs={s['claude_procs']}")

        send(client, MASTER_CHANNEL, "✅ OOM stress test complete.")

    # Save results
    with open(args.output, "w") as f:
        f.write(f"# OOM Stress Test Results\n\n")
        f.write(f"**Date:** {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}\n")
        f.write(f"**Machine:** {snapshots[0][2]['mem_total_gb']:.1f}GB RAM\n")
        f.write(f"**Target:** {count} agents running `sleep 300`\n\n")
        f.write(f"## Resource Timeline\n\n")
        f.write(f"| Event | Wall Time | Mem Used | Mem % | Available | Nova MB | Claude Procs |\n")
        f.write(f"|-------|-----------|----------|-------|-----------|---------|--------------|\n")
        for label, wall, s in snapshots:
            f.write(f"| {label} | {wall:.0f}s | {s['mem_used_gb']:.1f}GB | {s['mem_pct']:.0f}% "
                    f"| {s['mem_avail_gb']:.1f}GB | {s['nova_mb']:.0f}MB | {s['claude_procs']} |\n")
        f.write(f"\n## Summary\n\n")
        f.write(f"- Spawned {i} agents before stopping\n")
        peak_snap = max(snapshots, key=lambda x: x[2]['mem_pct'])
        f.write(f"- Peak memory: {peak_snap[2]['mem_used_gb']:.1f}GB ({peak_snap[2]['mem_pct']:.0f}%) at {peak_snap[0]}\n")
        f.write(f"- Peak claude procs: {max(s[2]['claude_procs'] for s in snapshots)}\n")
        if any(l[0] == "ABORT" for l in snapshots):
            f.write(f"- **ABORTED due to resource limits**\n")

    print(f"\n  Saved to {args.output}")


if __name__ == "__main__":
    main()
