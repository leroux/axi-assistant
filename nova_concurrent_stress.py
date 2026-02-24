#!/usr/bin/env python3
"""Stress test: spawn many agents that are ALL awake concurrently."""
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
    """Wait for sentinel. Returns elapsed seconds."""
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
    claude_lines = [l for l in ps.stdout.split("\n") if "claude" in l.lower() and "grep" not in l and "stress" not in l]

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
        "mem_used_gb": used / (1024**3),
        "mem_avail_gb": available / (1024**3),
        "mem_pct": used / total * 100,
        "claude_procs": len(claude_lines),
        "nova_mb": nova_mb,
    }


def get_channel_id(client: httpx.Client, name: str) -> str | None:
    resp = client.get(f"/guilds/{NOVA_GUILD_ID}/channels")
    if resp.status_code == 200:
        for ch in resp.json():
            if ch["name"] == name and ch["type"] == 0:
                return ch["id"]
    return None


def main():
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--max", type=int, default=5, help="Number of concurrent agents")
    parser.add_argument("--output", default="/home/ubuntu/axi-user-data/nova-concurrent-stress.md")
    args = parser.parse_args()

    token = get_token()
    count = args.max
    names = [f"conc-{i:02d}" for i in range(1, count + 1)]
    snapshots = []  # (event, stats)

    print("=" * 60)
    print(f"  CONCURRENT AGENT STRESS TEST ({count} agents)")
    print(f"  {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}")
    print("=" * 60)

    s = get_stats()
    print(f"\n📊 Baseline: {s['mem_used_gb']:.1f}GB used, {s['nova_mb']:.0f}MB nova, {s['claude_procs']} claude procs")
    snapshots.append(("baseline", s))

    with make_client(token) as client:
        send(client, MASTER_CHANNEL,
             f"⚠️ Concurrent stress test: spawning {count} agents with long-running tasks.")

        # Step 1: Spawn all agents quickly with long tasks so they stay awake
        print(f"\n🚀 Spawning {count} agents with long-running prompts...")
        spawn_ids = {}  # name -> master msg id
        for name in names:
            mid = send(client, MASTER_CHANNEL,
                       f"Spawn agent {name}. Prompt: 'Write a detailed 500-word essay about the history of {name}. "
                       f"Include specific dates and events. Take your time and be thorough.'")
            if mid:
                spawn_ids[name] = mid
                print(f"  → Spawn request sent for {name}")
            time.sleep(1)  # small gap so Nova can process each

        # Wait for Nova to acknowledge all spawns
        print(f"\n⏳ Waiting for Nova to acknowledge spawns...")
        if spawn_ids:
            last_mid = list(spawn_ids.values())[-1]
            wait_done(client, MASTER_CHANNEL, last_mid, timeout=120)

        # Step 2: Monitor while agents are working
        print(f"\n📊 Monitoring resource usage while agents work...")
        for tick in range(10):
            time.sleep(10)
            s = get_stats()
            # Count how many agent channels have activity
            active_count = 0
            finished_count = 0
            for name in names:
                ch_id = get_channel_id(client, name)
                if ch_id:
                    resp = client.get(f"/channels/{ch_id}/messages", params={"limit": 5})
                    if resp.status_code == 200:
                        msgs = resp.json()
                        has_content = any(m["content"] and "System" not in m["content"] and FINISHED_SENTINEL not in m["content"]
                                         for m in msgs if m["author"].get("username") != "Axi Prime")
                        has_finished = any(FINISHED_SENTINEL in m.get("content", "") for m in msgs)
                        if has_finished:
                            finished_count += 1
                        elif has_content:
                            active_count += 1

            label = f"t+{(tick+1)*10}s"
            print(f"  [{label}] mem={s['mem_used_gb']:.1f}GB ({s['mem_pct']:.0f}%), "
                  f"nova={s['nova_mb']:.0f}MB, procs={s['claude_procs']}, "
                  f"active={active_count}, finished={finished_count}/{count}")
            snapshots.append((label, s))

            if finished_count >= count:
                print(f"  ✅ All agents finished!")
                break

        # Final snapshot
        s = get_stats()
        snapshots.append(("final", s))
        print(f"\n📊 Final: {s['mem_used_gb']:.1f}GB used, {s['nova_mb']:.0f}MB nova, {s['claude_procs']} claude procs")

        # Check each agent's output
        print(f"\n📋 Agent results:")
        agent_results = []
        for name in names:
            ch_id = get_channel_id(client, name)
            if not ch_id:
                print(f"  ❌ {name}: channel not found")
                agent_results.append((name, False, "no channel"))
                continue
            resp = client.get(f"/channels/{ch_id}/messages", params={"limit": 20})
            if resp.status_code != 200:
                print(f"  ❌ {name}: can't read channel")
                agent_results.append((name, False, "read error"))
                continue
            msgs = resp.json()
            content_msgs = [m for m in msgs if m["author"].get("username") != "Axi Prime"
                           and "System" not in m.get("content", "")
                           and FINISHED_SENTINEL not in m.get("content", "")]
            finished = any(FINISHED_SENTINEL in m.get("content", "") or "finished initial task" in m.get("content", "")
                          for m in msgs)
            total_chars = sum(len(m.get("content", "")) for m in content_msgs)
            if finished and total_chars > 100:
                print(f"  ✅ {name}: finished, {total_chars} chars output")
                agent_results.append((name, True, f"{total_chars} chars"))
            elif total_chars > 0:
                print(f"  ⚠️  {name}: {total_chars} chars but not finished")
                agent_results.append((name, False, f"incomplete, {total_chars} chars"))
            else:
                print(f"  ❌ {name}: no output")
                agent_results.append((name, False, "no output"))

        # Cleanup
        print(f"\n🧹 Killing all test agents...")
        mid = send(client, MASTER_CHANNEL, f"Kill all these agents: {', '.join(names)}")
        if mid:
            wait_done(client, MASTER_CHANNEL, mid, timeout=120)

        send(client, MASTER_CHANNEL, "✅ Concurrent stress test complete.")

    # Save results
    passed = sum(1 for _, ok, _ in agent_results if ok)
    with open(args.output, "w") as f:
        f.write(f"# Concurrent Agent Stress Test\n\n")
        f.write(f"**Date:** {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}\n")
        f.write(f"**Target:** {count} concurrent agents\n\n")
        f.write(f"## Resource Timeline\n\n")
        f.write(f"| Time | Mem Used | Mem % | Nova Service | Claude Procs |\n")
        f.write(f"|------|----------|-------|-------------|-------------|\n")
        for label, s in snapshots:
            f.write(f"| {label} | {s['mem_used_gb']:.1f}GB | {s['mem_pct']:.0f}% | {s['nova_mb']:.0f}MB | {s['claude_procs']} |\n")
        f.write(f"\n## Agent Results\n\n")
        f.write(f"| Agent | Status | Detail |\n")
        f.write(f"|-------|--------|--------|\n")
        for name, ok, detail in agent_results:
            f.write(f"| {name} | {'✅' if ok else '❌'} | {detail} |\n")
        f.write(f"\n**{passed}/{count} agents completed successfully**\n")

    print(f"\n{'='*60}")
    print(f"  RESULT: {passed}/{count} completed")
    print(f"  Saved to {args.output}")
    print(f"{'='*60}")


if __name__ == "__main__":
    main()
