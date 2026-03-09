"""Discord UI handlers — plan approval, question gates, todo display.

Extracted from agents.py (Phase 0b) to isolate Discord-specific interactive UI
from core agent logic. Uses init() pattern for dependency injection.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import pathlib
import time
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Callable

from claude_agent_sdk.types import PermissionResultAllow, PermissionResultDeny

from axi import config
from axi.axi_types import AgentSession, discord_state
from axi.channels import schedule_status_update

log = logging.getLogger(__name__)

# Explicit exports for re-export from agents.py (suppresses pyright reportPrivateUsage)
__all__ = [
    "_handle_ask_user_question",
    "_handle_exit_plan_mode",
    "_post_todo_list",
    "_read_latest_plan_file",
    "format_todo_list",
    "load_todo_items",
    "parse_question_answer",
    "resolve_reaction_answer",
]


# ---------------------------------------------------------------------------
# Dependency injection (same pattern as discord_stream.py / channels.py)
# ---------------------------------------------------------------------------

_user_mentions_fn: Callable[[], str] | None = None


def init(*, user_mentions_fn: Callable[[], str]) -> None:
    """Inject dependencies from agents.py. Called once during bot setup."""
    global _user_mentions_fn
    _user_mentions_fn = user_mentions_fn


def _user_mentions() -> str:
    """Get Discord @mention string for allowed users."""
    assert _user_mentions_fn is not None, "discord_ui.init() not called"
    return _user_mentions_fn()


# ---------------------------------------------------------------------------
# Plan file discovery
# ---------------------------------------------------------------------------

_PLAN_FILE_MAX_AGE_SECS = 300  # Only consider plan files modified within the last 5 minutes

# Common plan file names agents write to their CWD
_CWD_PLAN_FILENAMES = ("PLAN.md", "plan.md")


def _read_latest_plan_file(cwd: str | None = None) -> str | None:
    """Read the most recently modified plan file.

    Searches two locations (returns the most recently modified match):
    1. The agent's CWD for PLAN.md / plan.md
    2. ~/.claude/plans/ for Claude Code's auto-generated plan files

    Claude Code writes plans to ~/.claude/plans/<random-name>.md when running
    in a terminal, but SDK agents often write PLAN.md to their CWD instead.
    """
    now = time.time()
    best: tuple[float, pathlib.Path] | None = None  # (mtime, path)

    # Check CWD for PLAN.md / plan.md
    if cwd:
        cwd_path = pathlib.Path(cwd)
        for name in _CWD_PLAN_FILENAMES:
            p = cwd_path / name
            try:
                mtime = p.stat().st_mtime
                if now - mtime <= _PLAN_FILE_MAX_AGE_SECS:
                    if best is None or mtime > best[0]:
                        best = (mtime, p)
            except OSError:
                continue

    # Check ~/.claude/plans/
    plans_dir = pathlib.Path.home() / ".claude" / "plans"
    if plans_dir.is_dir():
        try:
            candidates = sorted(
                plans_dir.glob("*.md"),
                key=lambda p: p.stat().st_mtime,
                reverse=True,
            )
            for path in candidates[:3]:
                try:
                    mtime = path.stat().st_mtime
                    if now - mtime > _PLAN_FILE_MAX_AGE_SECS:
                        break  # Sorted by mtime desc, no point checking older files
                    if best is None or mtime > best[0]:
                        best = (mtime, path)
                except OSError:
                    continue
        except OSError:
            pass

    if best is None:
        return None
    try:
        content = best[1].read_text(encoding="utf-8").strip()
        return content or None
    except OSError:
        return None


# ---------------------------------------------------------------------------
# ExitPlanMode — plan approval gate
# ---------------------------------------------------------------------------


async def _handle_exit_plan_mode(
    session: AgentSession | None,
    tool_input: dict[str, Any],
) -> PermissionResultAllow | PermissionResultDeny:
    """Handle ExitPlanMode by posting the plan to Discord and waiting for user approval."""
    if session is None:
        return PermissionResultAllow()
    ds = discord_state(session)
    if ds.channel_id is None:
        return PermissionResultAllow()

    channel_id = ds.channel_id

    async def _send_plan_msg(content: str) -> None:
        await config.discord_client.send_message(channel_id, content)

    plan_content = (tool_input.get("plan") or "").strip() or None

    # Heuristic fallback: the LLM doesn't always include the plan in tool_input
    # (the "plan" key is an additionalProperty, not a defined schema field).
    # Claude Code writes plans to ~/.claude/plans/<name>.md (terminal) or
    # PLAN.md in the agent's CWD (SDK agents).  We check both locations.
    used_heuristic = False
    if not plan_content:
        plan_content = _read_latest_plan_file(cwd=session.cwd)
        if plan_content:
            used_heuristic = True
            log.info("Read plan from disk for '%s' (tool_input had no plan key)", session.name)

    header = f"\U0001f4cb **Plan from {session.name}** \u2014 waiting for approval"
    try:
        if plan_content:
            plan_bytes = plan_content.encode("utf-8")
            heuristic_note = (
                "\n*(Plan recovered from disk via heuristic — Claude Code bug omitted it from tool input)*"
                if used_heuristic
                else ""
            )
            await config.discord_client.send_file(channel_id, "plan.txt", plan_bytes, content=header + heuristic_note)
        else:
            await _send_plan_msg(
                f"{header}\n\n*(Plan file not found \u2014 the agent should have described the plan in its messages above.)*"
            )

        resp = await config.discord_client.send_message(
            channel_id,
            f"React with \u2705 to approve or \u274c to reject, or type feedback to revise the plan. {_user_mentions()}",
        )
        approval_msg_id = resp["id"]

        # Pre-react with approval/rejection emojis so the user can click them
        for emoji in ("\u2705", "\u274c"):
            await config.discord_client.add_reaction(channel_id, approval_msg_id, emoji)
        discord_state(session).plan_approval_message_id = int(approval_msg_id)
    except Exception:
        log.exception("_handle_exit_plan_mode: failed to post plan to Discord \u2014 denying")
        return PermissionResultDeny(message="Could not post plan to Discord for approval. Try again.")

    loop = asyncio.get_running_loop()
    future: asyncio.Future[dict[str, Any]] = loop.create_future()
    discord_state(session).plan_approval_future = future  # type: ignore[assignment]
    schedule_status_update()

    log.info("Agent '%s' paused waiting for plan approval", session.name)

    try:
        result = await future
    finally:
        discord_state(session).plan_approval_future = None
        discord_state(session).plan_approval_message_id = None
        schedule_status_update()

    # Remove the unchosen reaction so the result is visually clear
    remove_emoji = "\u274c" if result.get("approved") else "\u2705"
    try:
        await config.discord_client.remove_reaction(channel_id, approval_msg_id, remove_emoji)
    except Exception:
        log.debug("Failed to remove reaction from plan approval message", exc_info=True)

    if result.get("approved"):
        log.info("Agent '%s' plan approved by user", session.name)
        if session.plan_mode:
            session.plan_mode = False
            if session.client:
                try:
                    await session.client.set_permission_mode("default")
                    log.info("Agent '%s' permission mode reset to default after plan approval", session.name)
                except Exception:
                    log.exception("Failed to reset permission mode for '%s'", session.name)
        return PermissionResultAllow()
    else:
        message = result.get("message", "User rejected the plan.")
        log.info("Agent '%s' plan rejected: %s", session.name, message)
        return PermissionResultDeny(message=json.dumps(message) if not isinstance(message, str) else message)


# ---------------------------------------------------------------------------
# AskUserQuestion
# ---------------------------------------------------------------------------

# Keycap emoji for options 1-9
_NUMBER_EMOJI = [
    "1\ufe0f\u20e3",
    "2\ufe0f\u20e3",
    "3\ufe0f\u20e3",
    "4\ufe0f\u20e3",
    "5\ufe0f\u20e3",
    "6\ufe0f\u20e3",
    "7\ufe0f\u20e3",
    "8\ufe0f\u20e3",
    "9\ufe0f\u20e3",
]
_CUSTOM_EMOJI = "\U0001f4dd"  # 📝 for "Other"


def _format_question_for_discord(q: dict[str, Any], index: int, total: int) -> str:
    """Format a single AskUserQuestion question for Discord display."""
    prefix = f"**Question {index + 1}/{total}:** " if total > 1 else ""
    header = q.get("header", "")
    question_text = q.get("question", "")
    multi = q.get("multiSelect", False)

    lines: list[str] = []
    if header:
        lines.append(f"{prefix}[{header}] {question_text}")
    else:
        lines.append(f"{prefix}{question_text}")

    options = q.get("options", [])
    for i, opt in enumerate(options):
        emoji = _NUMBER_EMOJI[i] if i < len(_NUMBER_EMOJI) else f"**{i + 1}.**"
        label = opt.get("label", "")
        desc = opt.get("description", "")
        if desc:
            lines.append(f"  {emoji} {label} — {desc}")
        else:
            lines.append(f"  {emoji} {label}")

    lines.append(f"  {_CUSTOM_EMOJI} Other (type your own answer)")

    if multi:
        lines.append("\n*React to choose, or type a custom answer.*")
    else:
        lines.append("\n*React to choose, or type a custom answer.*")

    return "\n".join(lines)


def parse_question_answer(raw: str, question: dict[str, Any]) -> str:
    """Parse a user's text reply into an answer string for one question."""
    options = question.get("options", [])
    multi = question.get("multiSelect", False)
    stripped = raw.strip()

    if multi:
        parts = [p.strip() for p in stripped.split(",")]
        selected: list[str] = []
        for part in parts:
            try:
                idx = int(part)
                if 1 <= idx <= len(options):
                    selected.append(options[idx - 1].get("label", part))
                else:
                    selected.append(part)
            except ValueError:
                selected.append(part)
        return ", ".join(selected) if selected else stripped
    else:
        try:
            idx = int(stripped)
            if 1 <= idx <= len(options):
                return options[idx - 1].get("label", stripped)
        except ValueError:
            pass
        return stripped


def resolve_reaction_answer(emoji_str: str, question: dict[str, Any]) -> str | None:
    """Map a reaction emoji to an answer string. Returns None if unrecognized."""
    options = question.get("options", [])
    for i, e in enumerate(_NUMBER_EMOJI):
        if emoji_str == e and i < len(options):
            return options[i].get("label", str(i + 1))
    if emoji_str == _CUSTOM_EMOJI:
        return "Other"
    return None


async def _handle_ask_user_question(
    session: AgentSession | None,
    tool_input: dict[str, Any],
) -> PermissionResultAllow | PermissionResultDeny:
    """Handle AskUserQuestion by posting questions one at a time and waiting for each answer."""
    if session is None:
        return PermissionResultAllow()
    ds = discord_state(session)
    if ds.channel_id is None:
        return PermissionResultAllow()

    channel_id = ds.channel_id
    questions = tool_input.get("questions", [])
    if not questions:
        return PermissionResultAllow()

    loop = asyncio.get_running_loop()
    answers: dict[str, str] = {}

    try:
        header = f"\u2753 **{session.name}** is asking you a question {_user_mentions()}"
        await config.discord_client.send_message(channel_id, header)
    except Exception:
        log.exception("_handle_ask_user_question: failed to post header — denying")
        return PermissionResultDeny(message="Could not post question to Discord.")

    for i, q in enumerate(questions):
        # Post the question and get message ID
        try:
            formatted = _format_question_for_discord(q, i, len(questions))
            msg = await config.discord_client.send_message(channel_id, formatted)
            msg_id = int(msg["id"])
        except Exception:
            log.exception("_handle_ask_user_question: failed to post question %d — denying", i)
            return PermissionResultDeny(message="Could not post question to Discord.")

        # Pre-add reaction emojis for each option
        options = q.get("options", [])
        for j in range(min(len(options), len(_NUMBER_EMOJI))):
            try:
                await config.discord_client.add_reaction(channel_id, msg_id, _NUMBER_EMOJI[j])
            except Exception:
                log.debug("Failed to add reaction %d to question message", j + 1)

        # Set session state for this question
        discord_state(session).question_message_id = msg_id
        discord_state(session).question_data = q

        future: asyncio.Future[str] = loop.create_future()
        discord_state(session).question_future = future

        log.info("Agent '%s' waiting for answer to question %d/%d", session.name, i + 1, len(questions))

        try:
            answer = await future
        finally:
            discord_state(session).question_future = None
            discord_state(session).question_message_id = None
            discord_state(session).question_data = None

        # Empty answer means interrupted (e.g. /stop)
        if not answer:
            break

        answers[q.get("question", "")] = answer

    log.info("Agent '%s' got answers: %s", session.name, answers)

    updated = dict(tool_input)
    updated["answers"] = answers
    return PermissionResultAllow(updated_input=updated)


# ---------------------------------------------------------------------------
# TodoWrite display
# ---------------------------------------------------------------------------

_TODO_STATUS = {"completed": "\u2705", "in_progress": "\U0001f504", "pending": "\u23f3"}


def format_todo_list(todos: list[dict[str, Any]]) -> str:
    """Format a todo list for Discord display."""
    lines: list[str] = []
    for item in todos:
        status = item.get("status", "pending")
        icon = _TODO_STATUS.get(status, "\u2b1c")
        content = item.get("content", "???")
        lines.append(f"{icon} {content}")
    return "\n".join(lines) or "*Empty todo list*"


def _todo_path(agent_name: str) -> str:
    """Path to the persisted todo state file for an agent."""
    return os.path.join(config.LOG_DIR, f"{agent_name}.todo.json")


def _save_todo_items(agent_name: str, todos: list[dict[str, Any]]) -> None:
    """Persist todo items to disk."""
    try:
        with open(_todo_path(agent_name), "w") as f:
            json.dump(todos, f)
    except OSError:
        log.warning("Failed to save todo state for '%s'", agent_name, exc_info=True)


def load_todo_items(agent_name: str) -> list[dict[str, Any]]:
    """Load persisted todo items from disk."""
    try:
        with open(_todo_path(agent_name)) as f:
            data: list[dict[str, Any]] = json.load(f)
        return data
    except (OSError, json.JSONDecodeError):
        pass
    return []


async def _post_todo_list(session: AgentSession, tool_input: dict[str, Any]) -> None:
    """Post the updated todo list as a new message in Discord."""
    todos = tool_input.get("todos", [])
    ds = discord_state(session)
    ds.todo_items = todos
    _save_todo_items(session.name, todos)
    body = f"**Todo List**\n{format_todo_list(todos)}"
    channel_id = ds.channel_id
    if channel_id is None:
        return

    try:
        await config.discord_client.send_message(channel_id, body)
    except Exception:
        log.exception("Failed to post todo list for agent '%s'", session.name)
