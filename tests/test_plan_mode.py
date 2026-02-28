"""Plan mode tests — Tier 5: enter, approve, reject, feedback."""

from .helpers import Discord
from .llm_judge import llm_assert


def test_enter_plan_mode(discord: Discord, master_channel: str):
    """Test 31: Agent enters plan mode and posts plan for approval."""
    msgs = discord.send_and_wait(
        master_channel,
        "I want you to plan how to create a hello world Python script. Use EnterPlanMode to plan this.",
        timeout=120.0,
    )
    text = discord.bot_response_text(msgs)

    # Should show plan with approval instructions
    passed, reason = llm_assert(
        text,
        "The response contains a plan or proposal, and mentions approving, rejecting, or providing feedback",
        context="Asked agent to enter plan mode",
    )
    assert passed, f"Plan mode not entered: {reason}"


def test_approve_plan(discord: Discord, master_channel: str):
    """Test 32: Approving a plan lets the agent proceed."""
    # The previous test should have left us in plan approval state
    msgs = discord.send_and_wait(
        master_channel,
        "approve",
        timeout=120.0,
    )
    text = discord.bot_response_text(msgs)

    # Agent should proceed with implementation
    passed, reason = llm_assert(
        text,
        "The response shows the agent proceeding with implementation, executing the plan, or acknowledging the approval",
        context="Approved a plan in plan mode",
    )
    assert passed, f"Plan not approved/executed: {reason}"


def test_reject_plan(discord: Discord, master_channel: str):
    """Test 33: Rejecting a plan stops the agent."""
    # Enter plan mode
    discord.send_and_wait(
        master_channel,
        "Plan how to write a fizzbuzz function. Use EnterPlanMode.",
        timeout=120.0,
    )

    # Reject
    msgs = discord.send_and_wait(
        master_channel,
        "reject",
        timeout=60.0,
    )
    text = discord.bot_response_text(msgs)

    passed, reason = llm_assert(
        text,
        "The response acknowledges the rejection or asks what changes the user wants",
        context="Rejected a plan in plan mode",
    )
    assert passed, f"Rejection not handled: {reason}"


def test_feedback_on_plan(discord: Discord, master_channel: str):
    """Test 34: Custom feedback revises the plan."""
    # Enter plan mode
    discord.send_and_wait(
        master_channel,
        "Plan how to write a sorting algorithm. Use EnterPlanMode.",
        timeout=120.0,
    )

    # Give feedback (not approve/reject)
    msgs = discord.send_and_wait(
        master_channel,
        "Use quicksort instead of mergesort, and add type hints",
        timeout=120.0,
    )
    text = discord.bot_response_text(msgs)

    # Agent should revise plan or acknowledge feedback
    passed, reason = llm_assert(
        text,
        "The response shows the agent revising its plan based on the feedback, or re-submitting an updated plan that incorporates quicksort and type hints",
        context="Gave feedback on a plan in plan mode",
    )
    assert passed, f"Feedback not incorporated: {reason}"

    # Approve the revised plan to clean up
    discord.send_and_wait(master_channel, "approve", timeout=120.0)
