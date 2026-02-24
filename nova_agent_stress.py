#!/usr/bin/env python3
"""Stress test: spawn many agents to find resource limits."""
import os
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
POLL_INTERVAL = 3


def get_token() -> str:
    token = os.environ.get("DISCORD_TOKEN")
    if not token:
        sys.exit("DISCORD_TOKEN not set")
    return token


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


def wait_done(client: httpx.Client, channel_id: str, after_id: str, timeout: int = 120) -> tuple[list[dict], float]:
    start = time.time()
    while time.time() - start < timeout:
        time.sleep(POLL_INTERVAL)
        resp = client.get(f"/channels/{channel_id}/messages", params={"after": after_id, "limit": 50})
        if resp.status_code != 200:
            continue
        messages = resp.json()
        for msg in messages:
            if FINISHED_SENTINEL in msg.get("content", ""):
                elapsed = time.time() - start
                return [m for m in messages if FINISHED_SENTINEL not in m.get("content", "")], elapsed
    return [], time.time() - start


def get_system_stats() -> dict:
    """Get memory stats via /proc."""
    import subprocess
    mem = subprocess.run(["free", "-b"], capture_output=True, text=True)
    lines = mem.stdout.strip().split("\n")
    parts = lines[1].split()
    total = int(parts[1])
    used = int(parts[2])
    available = int(parts[6])

    # Count claude processes
    ps = subprocess.run(["ps", "aux"], capture_output=True, text=True)
    claude_procs = [l for l in ps.stdout.split("\n") if "claude" in l.lower() and "grep" not in l]

    # Nova service memory
    nova_mem = subprocess.run(
        ["systemctl", "--user", "show", "axi-nova.service", "--property=MemoryCurrent"],
        capture_output=True, text=True
    )
    nova_bytes = nova_mem.stdout.strip().split("=")[1] if "=" in nova_mem.stdout else "?"

    return {
        "mem_total_gb": total / (1024**3),
        "mem_used_gb": used / (1024**3),
        "mem_available_gb": available / (1024**3),
        "mem_pct": used / total * 100,
        "claude_procs": len(claude_procs),
        "nova_mem_bytes": nova_bytes,
    }


def main():
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--max", type=int, default=8, help="Max agents to spawn")
    parser.add_argument("--output", default="/home/ubuntu/axi-user-data/nova-agent-stress-results.md")
    args = parser.parse_args()

    token = get_token()
    results = []

    print("=" * 60)
    print("  AGENT COUNT STRESS TEST")
    print(f"  Target: {args.max} concurrent agents")
    print(f"  {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}")
    print("=" * 60)

    stats_before = get_system_stats()
    print(f"\n📊 Before: {stats_before['mem_used_gb']:.1f}GB used / {stats_before['mem_total_gb']:.1f}GB total "
          f"({stats_before['mem_pct']:.0f}%), {stats_before['claude_procs']} claude procs")

    with make_client(token) as client:
        send(client, MASTER_CHANNEL, f"⚠️ Agent stress test starting — will spawn up to {args.max} agents.")

        agent_names = []
        spawn_times = []

        for i in range(1, args.max + 1):
            name = f"stress-{i:02d}"
            print(f"\n🚀 Spawning agent {i}/{args.max}: {name}")

            stats = get_system_stats()
            print(f"   📊 Memory: {stats['mem_used_gb']:.1f}GB used ({stats['mem_pct']:.0f}%), "
                  f"{stats['claude_procs']} claude procs, nova service: {stats['nova_mem_bytes']}")

            start = time.time()
            mid = send(client, MASTER_CHANNEL,
                       f"Spawn an agent called {name}. Prompt: 'Say hello, state your name ({name}), "
                       f"then immediately stop. Do not do anything else.'")

            if not mid:
                print(f"   ❌ Failed to send spawn command")
                results.append((name, False, "send failed", 0, stats))
                break

            msgs, elapsed = wait_done(client, MASTER_CHANNEL, mid, timeout=120)
            spawn_time = time.time() - start

            # Check for errors in response
            error = False
            for m in msgs:
                content = m.get("content", "").lower()
                if "error" in content or "maximum" in content or "max agents" in content:
                    error = True
                    print(f"   ❌ Error: {m['content'][:150]}")
                    results.append((name, False, m["content"][:150], spawn_time, stats))
                    break

            if not error:
                agent_names.append(name)
                spawn_times.append(spawn_time)
                print(f"   ✅ Spawned in {spawn_time:.1f}s")
                results.append((name, True, f"spawned in {spawn_time:.1f}s", spawn_time, stats))

            # Wait a beat for agent to finish its prompt and settle
            time.sleep(5)

        # Final stats
        stats_after = get_system_stats()
        print(f"\n📊 After {len(agent_names)} agents: {stats_after['mem_used_gb']:.1f}GB used "
              f"({stats_after['mem_pct']:.0f}%), {stats_after['claude_procs']} claude procs")

        # Now test responsiveness with all agents alive
        if agent_names:
            print(f"\n⏱ Testing master responsiveness with {len(agent_names)} agents alive...")
            mid = send(client, MASTER_CHANNEL, "What's 1+1? Just the number.")
            if mid:
                msgs, elapsed = wait_done(client, MASTER_CHANNEL, mid, timeout=60)
                print(f"   Master response time: {elapsed:.1f}s")

        # Cleanup
        print(f"\n🧹 Cleaning up {len(agent_names)} agents...")
        if agent_names:
            # Kill in batches
            batch_size = 5
            for i in range(0, len(agent_names), batch_size):
                batch = agent_names[i:i+batch_size]
                mid = send(client, MASTER_CHANNEL, f"Kill these agents: {', '.join(batch)}")
                if mid:
                    wait_done(client, MASTER_CHANNEL, mid, timeout=120)
                time.sleep(2)

        send(client, MASTER_CHANNEL, "✅ Agent stress test complete.")

    # Summary
    print("\n" + "=" * 60)
    print("  RESULTS")
    print("=" * 60)
    succeeded = sum(1 for _, ok, _, _, _ in results if ok)
    print(f"  Spawned: {succeeded}/{len(results)}")
    if spawn_times:
        print(f"  Spawn time: avg {sum(spawn_times)/len(spawn_times):.1f}s, "
              f"min {min(spawn_times):.1f}s, max {max(spawn_times):.1f}s")
    print(f"  Memory: {stats_before['mem_used_gb']:.1f}GB → {stats_after['mem_used_gb']:.1f}GB "
          f"(+{stats_after['mem_used_gb'] - stats_before['mem_used_gb']:.1f}GB)")
    print(f"  Claude procs: {stats_before['claude_procs']} → {stats_after['claude_procs']}")

    # Save
    with open(args.output, "w") as f:
        f.write(f"# Agent Count Stress Test\n\n")
        f.write(f"**Date:** {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}\n")
        f.write(f"**Machine:** {stats_before['mem_total_gb']:.1f}GB RAM, 4 vCPUs\n\n")
        f.write(f"## Results\n\n")
        f.write(f"| # | Agent | Status | Time | Mem Used | Mem % | Claude Procs |\n")
        f.write(f"|---|-------|--------|------|----------|-------|-------------|\n")
        for i, (name, ok, detail, t, s) in enumerate(results, 1):
            icon = "✅" if ok else "❌"
            f.write(f"| {i} | {name} | {icon} | {t:.1f}s | {s['mem_used_gb']:.1f}GB | {s['mem_pct']:.0f}% | {s['claude_procs']} |\n")
        f.write(f"\n## Summary\n\n")
        f.write(f"- **Spawned:** {succeeded}/{len(results)}\n")
        if spawn_times:
            f.write(f"- **Spawn time:** avg {sum(spawn_times)/len(spawn_times):.1f}s, "
                    f"min {min(spawn_times):.1f}s, max {max(spawn_times):.1f}s\n")
        f.write(f"- **Memory:** {stats_before['mem_used_gb']:.1f}GB → {stats_after['mem_used_gb']:.1f}GB "
                f"(+{stats_after['mem_used_gb'] - stats_before['mem_used_gb']:.1f}GB)\n")
        f.write(f"- **Claude procs:** {stats_before['claude_procs']} → {stats_after['claude_procs']}\n")
    print(f"\n  Saved to {args.output}")


if __name__ == "__main__":
    main()
