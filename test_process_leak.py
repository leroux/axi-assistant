"""
Reproducer: ClaudeSDKClient leaks subprocesses when multiple clients
share an asyncio event loop.

Bug summary
-----------
When multiple ClaudeSDKClient instances exist concurrently in the same
asyncio event loop (e.g. a bot managing multiple agent sessions),
calling disconnect() / __aexit__() on one client leaks the underlying
Claude Code CLI subprocess.

The root cause is that Query.close() cancels its internal anyio
TaskGroup, but the CancelledError escapes into the asyncio context:

  RuntimeError: Attempted to exit a cancel scope that isn't the
  current task's current cancel scope

This prevents SubprocessCLITransport.close() from executing
process.terminate(), so the subprocess survives.  Over time this
causes unbounded process accumulation.

A single client disconnecting in isolation works fine.  The bug
requires multiple concurrent clients in the same event loop.

Environment
-----------
- claude-agent-sdk  0.1.39  (Python)
- Claude Code CLI   2.1.50
- Python            3.12.3
- Linux             6.8.0-100-generic

Usage
-----
    pip install claude-agent-sdk
    python test_process_leak.py
"""

import asyncio
import os
import subprocess
import sys
import time

# Allow running inside an existing Claude Code session
os.environ.pop("CLAUDECODE", None)

from claude_agent_sdk import ClaudeAgentOptions, ClaudeSDKClient, create_sdk_mcp_server, tool
from claude_agent_sdk._errors import MessageParseError
from claude_agent_sdk._internal.message_parser import parse_message
from claude_agent_sdk.types import ResultMessage
from claudewire.permissions import Allow

# ── Helpers ───────────────────────────────────────────────────────────


def count_claude_procs() -> tuple[int, list[int]]:
    """Return (count, [pids]) of SDK-spawned claude processes."""
    result = subprocess.run(
        ["pgrep", "-u", str(os.getuid()), "-f", "claude.*--output-format"],
        capture_output=True,
        text=True,
    )
    pids = [int(p) for p in result.stdout.strip().splitlines() if p.strip()]
    return len(pids), pids


def kill_pids(pids):
    for pid in pids:
        try:
            os.kill(pid, 15)
        except ProcessLookupError:
            pass


async def _as_stream(text: str):
    yield {
        "type": "user",
        "session_id": "",
        "message": {"role": "user", "content": text},
        "parent_tool_use_id": None,
    }


async def allow_all(tool_name, tool_input):
    return Allow()


def make_mcp_server():
    @tool("test_noop", "A no-op tool for testing", {})
    async def test_noop(args, extra):
        from mcp.types import CallToolResult, TextContent

        return CallToolResult(content=[TextContent(type="text", text="ok")])

    return create_sdk_mcp_server(name="test", tools=[test_noop])


async def drain_response(client: ClaudeSDKClient) -> None:
    async for data in client._query.receive_messages():
        try:
            parsed = parse_message(data)
        except MessageParseError:
            continue
        if isinstance(parsed, ResultMessage):
            return


async def create_client() -> ClaudeSDKClient:
    options = ClaudeAgentOptions(
        model="sonnet",
        system_prompt="Reply with exactly: OK",
        max_turns=1,
        can_use_tool=allow_all,
        include_partial_messages=True,
        sandbox={"enabled": True, "autoAllowBashIfSandboxed": True},
        mcp_servers={"test": make_mcp_server()},
    )
    client = ClaudeSDKClient(options=options)
    await client.__aenter__()
    return client


# ── Test ──────────────────────────────────────────────────────────────

# Store baseline globally so the CancelledError handler can access it.
_baseline_pids: list[int] = []


async def test() -> None:
    global _baseline_pids
    baseline_count, _baseline_pids = count_claude_procs()
    n = 3

    print(f"Baseline: {baseline_count} claude process(es)\n")

    # Step 1: create N concurrent clients
    print(f"1. Creating {n} concurrent clients...")
    clients = []
    for _i in range(n):
        c = await create_client()
        clients.append(c)
    count_after_create, _ = count_claude_procs()
    print(f"   {count_after_create} procs (+{count_after_create - baseline_count})\n")

    # Step 2: query each
    print("2. Querying each client...")
    for c in clients:
        await c.query(_as_stream("Say OK"))
        await drain_response(c)
    print("   All queries complete\n")

    # Step 3: let them idle (simulates agents waiting for user input)
    print("3. Idling 3 seconds...")
    await asyncio.sleep(3)
    count_after_idle, _ = count_claude_procs()
    print(f"   {count_after_idle} procs (+{count_after_idle - baseline_count})\n")

    # Step 4: disconnect each
    #   This is the step that triggers the bug.  The first client's
    #   __aexit__ cancels its anyio TaskGroup, and the CancelledError
    #   escapes into the asyncio event loop, typically crashing the
    #   entire program before the remaining clients (or even the
    #   first client's transport.close()) can clean up.
    print("4. Disconnecting each client...")
    for i, c in enumerate(clients):
        try:
            await asyncio.wait_for(c.__aexit__(None, None, None), timeout=5.0)
            print(f"   Client {i + 1}: clean")
        except Exception as e:
            print(f"   Client {i + 1}: {type(e).__name__}: {e}")

    report(_baseline_pids)


def report(baseline_pids: list[int]) -> None:
    """Check for leaked processes and print final report."""
    time.sleep(3)
    final_count, final_pids = count_claude_procs()
    leaked = set(final_pids) - set(baseline_pids)

    print(f"\n{'=' * 60}")
    print(f"Final:    {final_count} procs")
    print(f"Baseline: {len(baseline_pids)} procs")
    print(f"Leaked:   {len(leaked)} subprocess(es)")

    if leaked:
        print(f"PIDs:     {sorted(leaked)}")
        kill_pids(leaked)
        time.sleep(2)
        post, _ = count_claude_procs()
        print(f"\nAfter manual SIGTERM: {post} procs (back to baseline: {post == len(baseline_pids)})")
        print(f"\nBUG CONFIRMED: disconnect() leaked {len(leaked)} subprocess(es).")
        print("Manual SIGTERM kills them — the processes are terminable,")
        print("they are just not being terminated by the SDK.")
        sys.exit(1)
    else:
        print("\nNo leaked processes. Bug not reproduced.")
        sys.exit(0)


if __name__ == "__main__":
    try:
        asyncio.run(test())
    except (asyncio.CancelledError, RuntimeError) as e:
        # The anyio cancel scope leak typically crashes us here.
        # This itself is part of the bug: an internal cleanup should
        # never propagate CancelledError to the caller.
        print(f"\n*** {type(e).__name__} escaped to top level ***")
        print(f"    {e}")
        print("\n(This confirms the anyio cancel scope leak.)\n")
        report(_baseline_pids)
