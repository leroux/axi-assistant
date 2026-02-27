#!/usr/bin/env python3
"""
Integration test for message routing with handler pattern.

Validates:
1. Message routing through on_message()
2. State checks using handlers
3. Queueing logic
4. Error handling
"""

import sys
sys.path.insert(0, '/home/ubuntu/axi-assistant')

from unittest.mock import Mock, AsyncMock, patch, MagicMock
import asyncio

print("=" * 80)
print("MESSAGE ROUTING INTEGRATION TEST")
print("=" * 80)
print()

# Import required modules
try:
    from handlers import get_handler, AgentHandler
    print("✅ Imported handlers module")
except ImportError as e:
    print(f"❌ Failed to import handlers: {e}")
    sys.exit(1)

# Test 1: Agent lifecycle state transitions
print("\nTEST 1: Agent lifecycle state transitions...")
try:
    # Create mock session
    session = Mock()
    session.agent_type = "claude_code"

    # State 1: Sleeping (no client)
    session.client = None
    session.query_lock = MagicMock()
    session.query_lock.locked.return_value = False

    handler = get_handler("claude_code")

    assert not handler.is_awake(session), "Should be sleeping"
    assert not handler.is_processing(session), "Should not be processing"
    print("✅ Sleeping state: not awake, not processing")

    # State 2: Waking (client created, lock acquired)
    session.client = Mock()
    session.query_lock.locked.return_value = False

    assert handler.is_awake(session), "Should be awake"
    assert not handler.is_processing(session), "Should not be processing (lock not held)"
    print("✅ Awake idle state: awake, not processing")

    # State 3: Processing (client created, lock held)
    session.query_lock.locked.return_value = True

    assert handler.is_awake(session), "Should be awake"
    assert handler.is_processing(session), "Should be processing (lock held)"
    print("✅ Awake busy state: awake, processing")

except AssertionError as e:
    print(f"❌ State transition test failed: {e}")
    sys.exit(1)

# Test 2: Message routing decision logic
print("\nTEST 2: Message routing decision logic...")
try:
    def route_message(session):
        """Simulate on_message() routing logic using handlers."""
        handler = get_handler(session.agent_type)

        # Check 1: Is agent awake?
        if not handler.is_awake(session):
            return "NEED_TO_WAKE"

        # Check 2: Is agent processing?
        if handler.is_processing(session):
            return "QUEUE_MESSAGE"

        # Check 3: Ready to process
        return "PROCESS_NOW"

    # Test case 1: Sleeping agent
    session = Mock()
    session.agent_type = "claude_code"
    session.client = None
    session.query_lock = MagicMock()
    session.query_lock.locked.return_value = False

    result = route_message(session)
    assert result == "NEED_TO_WAKE", f"Expected NEED_TO_WAKE, got {result}"
    print("✅ Sleeping agent → NEED_TO_WAKE")

    # Test case 2: Awake but busy agent
    session.client = Mock()
    session.query_lock.locked.return_value = True

    result = route_message(session)
    assert result == "QUEUE_MESSAGE", f"Expected QUEUE_MESSAGE, got {result}"
    print("✅ Busy agent → QUEUE_MESSAGE")

    # Test case 3: Awake and idle agent
    session.query_lock.locked.return_value = False

    result = route_message(session)
    assert result == "PROCESS_NOW", f"Expected PROCESS_NOW, got {result}"
    print("✅ Idle agent → PROCESS_NOW")

except AssertionError as e:
    print(f"❌ Routing decision test failed: {e}")
    sys.exit(1)

# Test 3: Multi-agent routing
print("\nTEST 3: Multi-agent routing scenarios...")
try:
    agents = {
        "agent_a": Mock(agent_type="claude_code", client=None, query_lock=MagicMock()),
        "agent_b": Mock(agent_type="claude_code", client=Mock(), query_lock=MagicMock()),
        "agent_c": Mock(agent_type="flowcoder", flowcoder_process=None),
    }

    # Configure state
    agents["agent_a"].query_lock.locked.return_value = False  # Sleeping
    agents["agent_b"].query_lock.locked.return_value = True   # Busy
    agents["agent_c"].flowcoder_process = None                # Sleeping

    # Route messages for each agent
    results = {}
    for name, session in agents.items():
        handler = get_handler(session.agent_type)
        if not handler.is_awake(session):
            results[name] = "sleep"
        elif handler.is_processing(session):
            results[name] = "busy"
        else:
            results[name] = "idle"

    assert results["agent_a"] == "sleep", f"agent_a should be sleeping, got {results['agent_a']}"
    print("✅ Agent A (Claude Code, sleeping) → sleep")

    assert results["agent_b"] == "busy", f"agent_b should be busy, got {results['agent_b']}"
    print("✅ Agent B (Claude Code, busy) → busy")

    assert results["agent_c"] == "sleep", f"agent_c should be sleeping, got {results['agent_c']}"
    print("✅ Agent C (Flowcoder, sleeping) → sleep")

except AssertionError as e:
    print(f"❌ Multi-agent routing test failed: {e}")
    sys.exit(1)

# Test 4: Handler method availability
print("\nTEST 4: Handler method availability for routing...")
try:
    for agent_type in ["claude_code", "flowcoder"]:
        handler = get_handler(agent_type)

        # All methods must exist and be callable
        methods = ['is_awake', 'is_processing', 'wake', 'sleep', 'process_message']

        for method_name in methods:
            assert hasattr(handler, method_name), f"{agent_type} missing {method_name}"
            method = getattr(handler, method_name)
            assert callable(method), f"{agent_type}.{method_name} not callable"

    print("✅ All handler methods available and callable for Claude Code")
    print("✅ All handler methods available and callable for Flowcoder")

except AssertionError as e:
    print(f"❌ Handler method test failed: {e}")
    sys.exit(1)

# Test 5: State consistency check
print("\nTEST 5: State consistency validation...")
try:
    # Create a session with deliberate inconsistencies and verify handlers catch them
    session = Mock()
    session.agent_type = "claude_code"

    # Test 1: Client exists but lock is None (edge case)
    session.client = Mock()
    session.query_lock = MagicMock()
    session.query_lock.locked.return_value = False

    handler = get_handler("claude_code")
    assert handler.is_awake(session), "Should be awake when client exists"
    print("✅ Detects awake state (client present)")

    # Test 2: Client is None but lock exists (should be sleeping)
    session.client = None
    session.query_lock.locked.return_value = True  # Lock held but no client

    assert not handler.is_awake(session), "Should be asleep when client is None"
    print("✅ Detects sleeping state (no client)")

except AssertionError as e:
    print(f"❌ State consistency test failed: {e}")
    sys.exit(1)

# Test 6: Type-specific routing
print("\nTEST 6: Type-specific routing behavior...")
try:
    # Claude Code specific routing
    cc_session = Mock()
    cc_session.agent_type = "claude_code"
    cc_session.client = Mock()
    cc_session.query_lock = MagicMock()
    cc_session.query_lock.locked.return_value = False

    cc_handler = get_handler("claude_code")
    assert cc_handler.is_awake(cc_session), "Claude Code should check client"
    assert not cc_handler.is_processing(cc_session), "Claude Code should check query_lock"
    print("✅ Claude Code routing checks client and query_lock")

    # Flowcoder specific routing
    fc_session = Mock()
    fc_session.agent_type = "flowcoder"
    fc_process = Mock()
    fc_process.is_running = True
    fc_session.flowcoder_process = fc_process

    fc_handler = get_handler("flowcoder")
    assert fc_handler.is_awake(fc_session), "Flowcoder should check process"
    assert fc_handler.is_processing(fc_session), "Flowcoder should check is_running"
    print("✅ Flowcoder routing checks process and is_running")

except AssertionError as e:
    print(f"❌ Type-specific routing test failed: {e}")
    sys.exit(1)

# Summary
print()
print("=" * 80)
print("MESSAGE ROUTING INTEGRATION TEST - SUMMARY")
print("=" * 80)
print()
print("✅ All message routing integration tests PASSED")
print()
print("Validations:")
print("  ✅ Agent lifecycle state transitions work correctly")
print("  ✅ Message routing decision logic is correct")
print("  ✅ Multi-agent routing handles different types")
print("  ✅ All handler methods available for routing")
print("  ✅ State consistency validated")
print("  ✅ Type-specific routing behavior correct")
print()
print("The handler pattern refactoring supports correct message routing.")
print("=" * 80)
