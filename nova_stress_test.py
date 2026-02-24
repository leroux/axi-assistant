#!/usr/bin/env python3
"""Stress test suite for Axi Nova. Runs various load scenarios."""
import argparse
import json
import os
import sys
import time
import threading
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
        print("Error: DISCORD_TOKEN not set", file=sys.stderr)
        sys.exit(1)
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
        print(f"  !! Send failed: {resp.status_code} {resp.text[:200]}", file=sys.stderr)
        return ""
    return resp.json()["id"]


def wait_for_response(client: httpx.Client, channel_id: str, after_id: str, timeout: int = 120) -> tuple[list[dict], float]:
    """Wait for response. Returns (messages, elapsed_seconds)."""
    start = time.time()
    while time.time() - start < timeout:
        time.sleep(POLL_INTERVAL)
        resp = client.get(f"/channels/{channel_id}/messages", params={"after": after_id, "limit": 50})
        if resp.status_code != 200:
            continue
        messages = resp.json()
        messages.reverse()
        for msg in messages:
            if FINISHED_SENTINEL in msg.get("content", ""):
                elapsed = time.time() - start
                return [m for m in messages if FINISHED_SENTINEL not in m.get("content", "")], elapsed
    return [], time.time() - start


def list_channels(client: httpx.Client) -> list[dict]:
    resp = client.get(f"/guilds/{NOVA_GUILD_ID}/channels")
    if resp.status_code == 200:
        return [c for c in resp.json() if c["type"] == 0]
    return []


def get_channel_by_name(client: httpx.Client, name: str) -> str | None:
    for ch in list_channels(client):
        if ch["name"] == name:
            return ch["id"]
    return None


def print_result(test_name: str, passed: bool, detail: str, elapsed: float = 0):
    icon = "✅" if passed else "❌"
    time_str = f" ({elapsed:.1f}s)" if elapsed else ""
    print(f"  {icon} {test_name}{time_str}: {detail}")


# ─── Test Scenarios ──────────────────────────────────────────


def test_rapid_fire(client: httpx.Client, results: list):
    """Send 5 messages in rapid succession to master."""
    print("\n🔥 TEST: Rapid-fire messages (5 messages in <2s)")
    msg_ids = []
    for i in range(5):
        mid = send(client, MASTER_CHANNEL, f"Rapid fire #{i+1}: reply with just the number {i+1}")
        msg_ids.append(mid)
        time.sleep(0.3)

    if not msg_ids[0]:
        print_result("rapid-fire", False, "Failed to send messages")
        results.append(("rapid-fire", False, "send failed"))
        return

    # Wait for all responses (use first msg as anchor, long timeout)
    msgs, elapsed = wait_for_response(client, MASTER_CHANNEL, msg_ids[0], timeout=180)
    nova_msgs = [m for m in msgs if m["author"]["username"] != "Axi Prime" and FINISHED_SENTINEL not in m.get("content", "")]

    # Check for injection messages
    injected = sum(1 for m in msgs if "will process after current turn" in m.get("content", ""))
    queued = sum(1 for m in msgs if "message queued" in m.get("content", "").lower())

    print_result("rapid-fire", True,
                 f"{len(nova_msgs)} responses, {injected} injected, {queued} queued",
                 elapsed)
    results.append(("rapid-fire", True, f"{len(nova_msgs)} responses, {injected} injected, {queued} queued, {elapsed:.1f}s"))


def test_concurrent_agents(client: httpx.Client, results: list):
    """Spawn 3 agents simultaneously."""
    print("\n🤖 TEST: Concurrent agent spawn (3 agents)")
    names = ["stress-a", "stress-b", "stress-c"]

    # Spawn all 3 quickly
    mid = send(client, MASTER_CHANNEL,
               f"Spawn 3 agents at once: {', '.join(names)}. "
               "Give each the prompt: 'Say your agent name and the current time, then stop.'")

    if not mid:
        print_result("concurrent-spawn", False, "Failed to send")
        results.append(("concurrent-spawn", False, "send failed"))
        return

    msgs, elapsed = wait_for_response(client, MASTER_CHANNEL, mid, timeout=120)
    print_result("concurrent-spawn-request", True, f"Master responded", elapsed)

    # Wait for agents to finish
    time.sleep(30)

    # Check each agent channel
    spawned = 0
    for name in names:
        ch_id = get_channel_by_name(client, name)
        if ch_id:
            spawned += 1
            resp = client.get(f"/channels/{ch_id}/messages", params={"limit": 10})
            if resp.status_code == 200:
                agent_msgs = [m for m in resp.json() if "finished initial task" in m.get("content", "")]
                if agent_msgs:
                    print_result(f"  agent-{name}", True, "Spawned and completed")
                else:
                    print_result(f"  agent-{name}", False, "Channel exists but no completion")
            else:
                print_result(f"  agent-{name}", False, f"Can't read channel: {resp.status_code}")
        else:
            print_result(f"  agent-{name}", False, "Channel not found")

    passed = spawned == 3
    print_result("concurrent-spawn", passed, f"{spawned}/3 agents spawned", elapsed)
    results.append(("concurrent-spawn", passed, f"{spawned}/3 agents"))

    # Cleanup
    print("  🧹 Cleaning up agents...")
    mid = send(client, MASTER_CHANNEL,
               f"Kill all these agents: {', '.join(names)}")
    if mid:
        wait_for_response(client, MASTER_CHANNEL, mid, timeout=60)


def test_large_input(client: httpx.Client, results: list):
    """Send a very large message (near Discord's 2000 char limit)."""
    print("\n📏 TEST: Large input message (~1900 chars)")
    filler = "The quick brown fox jumps over the lazy dog. " * 40  # ~1800 chars
    msg = f"Summarize this text in one sentence: {filler}"
    msg = msg[:1900]

    mid = send(client, MASTER_CHANNEL, msg)
    if not mid:
        print_result("large-input", False, "Failed to send")
        results.append(("large-input", False, "send failed"))
        return

    msgs, elapsed = wait_for_response(client, MASTER_CHANNEL, mid, timeout=60)
    nova_msgs = [m for m in msgs if m["author"]["username"] != "Axi Prime"]
    passed = len(nova_msgs) > 0
    preview = nova_msgs[0]["content"][:100] if nova_msgs else "(no response)"
    print_result("large-input", passed, preview, elapsed)
    results.append(("large-input", passed, f"{elapsed:.1f}s"))


def test_rapid_spawn_kill(client: httpx.Client, results: list):
    """Spawn and immediately kill an agent — race condition test."""
    print("\n⚡ TEST: Rapid spawn then kill")
    mid = send(client, MASTER_CHANNEL,
               "Spawn an agent called stress-rapid with prompt 'Write a 500-word essay about space.'")
    if not mid:
        print_result("rapid-spawn-kill", False, "Failed to send spawn")
        results.append(("rapid-spawn-kill", False, "send failed"))
        return

    msgs, elapsed = wait_for_response(client, MASTER_CHANNEL, mid, timeout=60)
    print_result("  spawn", True, "Spawned", elapsed)

    # Immediately kill it
    time.sleep(3)
    mid2 = send(client, MASTER_CHANNEL, "Kill agent stress-rapid right now")
    if mid2:
        msgs2, elapsed2 = wait_for_response(client, MASTER_CHANNEL, mid2, timeout=60)
        nova_msgs = [m for m in msgs2 if m["author"]["username"] != "Axi Prime" and "System" not in m.get("content", "")]
        killed = any("killed" in m.get("content", "").lower() for m in msgs2)
        print_result("rapid-spawn-kill", killed, "Killed while running" if killed else "Kill response unclear", elapsed2)
        results.append(("rapid-spawn-kill", killed, f"spawn {elapsed:.1f}s, kill {elapsed2:.1f}s"))
    else:
        print_result("rapid-spawn-kill", False, "Failed to send kill")
        results.append(("rapid-spawn-kill", False, "kill send failed"))


def test_empty_message(client: httpx.Client, results: list):
    """Send edge-case messages."""
    print("\n🫥 TEST: Edge-case messages")

    # Unicode-heavy message
    mid = send(client, MASTER_CHANNEL, "Reply with exactly: 🎉🔥💀🤖 ñ café résumé naïve")
    if not mid:
        print_result("unicode", False, "Failed to send")
        results.append(("unicode", False, "send failed"))
        return

    msgs, elapsed = wait_for_response(client, MASTER_CHANNEL, mid, timeout=60)
    nova_msgs = [m for m in msgs if m["author"]["username"] != "Axi Prime"]
    has_unicode = any("🎉" in m.get("content", "") or "café" in m.get("content", "") for m in nova_msgs)
    print_result("unicode", has_unicode, nova_msgs[0]["content"][:100] if nova_msgs else "(no response)", elapsed)
    results.append(("unicode", has_unicode, f"{elapsed:.1f}s"))


def test_back_to_back_queries(client: httpx.Client, results: list):
    """Send messages back-to-back waiting for each response — sustained load."""
    print("\n🔁 TEST: Back-to-back queries (5 sequential)")
    times = []
    all_passed = True
    for i in range(5):
        mid = send(client, MASTER_CHANNEL, f"Quick: what is {(i+1) * 7}? Just the number.")
        if not mid:
            all_passed = False
            break
        msgs, elapsed = wait_for_response(client, MASTER_CHANNEL, mid, timeout=60)
        nova_msgs = [m for m in msgs if m["author"]["username"] != "Axi Prime"]
        if nova_msgs:
            times.append(elapsed)
            print_result(f"  query-{i+1}", True, f"{nova_msgs[0]['content'][:50]}", elapsed)
        else:
            all_passed = False
            print_result(f"  query-{i+1}", False, "No response", elapsed)

    if times:
        avg = sum(times) / len(times)
        print_result("back-to-back", all_passed, f"avg {avg:.1f}s, min {min(times):.1f}s, max {max(times):.1f}s")
        results.append(("back-to-back", all_passed, f"avg {avg:.1f}s, {len(times)}/5 responded"))
    else:
        results.append(("back-to-back", False, "no responses"))


# ─── Main ────────────────────────────────────────────────────


ALL_TESTS = {
    "rapid-fire": test_rapid_fire,
    "concurrent-agents": test_concurrent_agents,
    "large-input": test_large_input,
    "rapid-spawn-kill": test_rapid_spawn_kill,
    "unicode": test_empty_message,
    "back-to-back": test_back_to_back_queries,
}


def main():
    parser = argparse.ArgumentParser(description="Stress test Axi Nova")
    parser.add_argument("tests", nargs="*", default=list(ALL_TESTS.keys()),
                        help=f"Tests to run (default: all). Options: {', '.join(ALL_TESTS.keys())}")
    parser.add_argument("--output", default="/home/ubuntu/axi-user-data/nova-stress-results.md",
                        help="Output file for results")
    args = parser.parse_args()

    token = get_token()
    results = []

    print("=" * 60)
    print("  AXI NOVA STRESS TEST")
    print(f"  {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}")
    print("=" * 60)

    with make_client(token) as client:
        # Announce
        send(client, MASTER_CHANNEL, "⚠️ Starting stress test run. Expect rapid messages.")

        for test_name in args.tests:
            if test_name in ALL_TESTS:
                try:
                    ALL_TESTS[test_name](client, results)
                except Exception as e:
                    print(f"\n💥 Test {test_name} crashed: {e}")
                    results.append((test_name, False, f"CRASH: {e}"))
            else:
                print(f"\n⚠️ Unknown test: {test_name}")

        # Announce done
        send(client, MASTER_CHANNEL, "✅ Stress test run complete.")

    # Summary
    print("\n" + "=" * 60)
    print("  RESULTS SUMMARY")
    print("=" * 60)
    passed = sum(1 for _, p, _ in results if p)
    failed = sum(1 for _, p, _ in results if not p)
    for name, p, detail in results:
        icon = "✅" if p else "❌"
        print(f"  {icon} {name}: {detail}")
    print(f"\n  Total: {passed} passed, {failed} failed out of {len(results)}")

    # Save results
    with open(args.output, "w") as f:
        f.write(f"# Nova Stress Test Results\n\n")
        f.write(f"**Date:** {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}\n\n")
        f.write(f"| Test | Status | Detail |\n")
        f.write(f"|------|--------|--------|\n")
        for name, p, detail in results:
            icon = "✅" if p else "❌"
            f.write(f"| {name} | {icon} | {detail} |\n")
        f.write(f"\n**Total: {passed}/{len(results)} passed**\n")
    print(f"\n  Results saved to {args.output}")


if __name__ == "__main__":
    main()
