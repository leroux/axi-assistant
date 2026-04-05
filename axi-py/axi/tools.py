"""MCP tool definitions and server assembly for the Axi bot.

All @tool-decorated functions and MCP server objects live here.
This module imports from agents.py (one-way: agents.py does NOT import tools.py).
"""

from __future__ import annotations

__all__ = [
    "axi_master_mcp_server",
    "axi_mcp_server",
    "discord_mcp_server",
    "sdk_mcp_servers_for_cwd",
    "utils_mcp_server",
]

import json
import logging
import os
import subprocess
from typing import TYPE_CHECKING, Any

import arrow
from claude_agent_sdk import create_sdk_mcp_server, tool
from opentelemetry import trace

from axi import agents, channels, config
from axi.log_context import set_agent_context, set_trigger
from axi.schedule_tools import make_schedule_mcp_server

if TYPE_CHECKING:
    from axi.axi_types import McpArgs, McpResult

log = logging.getLogger("axi")
_tracer = trace.get_tracer(__name__)


# ---------------------------------------------------------------------------
# Master agent tools
# ---------------------------------------------------------------------------


@tool(
    "axi_spawn_agent",
    "Spawn a new Axi agent session with its own Discord channel. Returns immediately with success/error message.",
    {
        "type": "object",
        "properties": {
            "name": {
                "type": "string",
                "description": "Unique short name, no spaces (e.g. 'feature-auth', 'fix-bug-123')",
            },
            "cwd": {
                "type": "string",
                "description": "Absolute path to the working directory for the agent. Defaults to a per-agent subdirectory under user data (agents/<name>/).",
            },
            "prompt": {"type": "string", "description": "Initial task instructions for the agent"},
            "resume": {"type": "string", "description": "Optional session ID to resume a previous agent session"},
            "agent_type": {
                "type": "string",
                "enum": ["claude_code", "flowcoder"],
                "description": "Agent type. 'claude_code' (default) for interactive Claude, 'flowcoder' for flowchart executor.",
            },
            "command": {
                "type": "string",
                "description": "Flowcoder command name (required when agent_type='flowcoder')",
            },
            "command_args": {
                "type": "string",
                "description": "Arguments for the flowcoder command (shell-style string)",
            },
            "extensions": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Optional list of extension names to load into this agent's system prompt. Defaults to DEFAULT_EXTENSIONS. Pass [] to disable extensions.",
            },
            "no_worktree": {
                "type": "boolean",
                "description": "Skip auto-worktree creation and use cwd directly (default: false)",
            },
            "compact_instructions": {
                "type": "string",
                "description": "Instructions for what to preserve during context compaction (e.g. 'always preserve the bug description, current fix approach, and test results')",
            },
            "mcp_servers": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Optional list of custom MCP server names (from mcp_servers.json) to attach to this agent (e.g. ['todoist']).",
            },
        },
        "required": ["name", "prompt"],
    },
)
async def axi_spawn_agent(args: McpArgs) -> McpResult:
    agent_name = args.get("name", "").strip()
    set_agent_context(agent_name or "unknown")
    set_trigger("mcp_tool", detail="axi_spawn_agent")
    _tracer.start_span("tool.axi_spawn_agent", attributes={"agent.name": agent_name}).end()
    default_cwd = os.path.join(config.AXI_USER_DATA, "agents", agent_name) if agent_name else config.AXI_USER_DATA
    agent_cwd = os.path.realpath(os.path.expanduser(args.get("cwd", default_cwd)))
    agent_prompt = args.get("prompt", "")
    agent_resume = args.get("resume")
    agent_type = args.get("agent_type", "claude_code")
    fc_command = args.get("command", "")
    fc_command_args = args.get("command_args", "")
    agent_extensions = args.get("extensions")  # None = use defaults, [] = no extensions
    no_worktree = args.get("no_worktree", False)
    compact_instructions = args.get("compact_instructions")
    mcp_server_names: list[str] = args.get("mcp_servers") or []

    # Resolve custom MCP servers from config
    extra_mcp_servers = config.load_mcp_servers(mcp_server_names) if mcp_server_names else None

    # --- Validate name/agent before any side effects ---
    if not agent_name:
        return {
            "content": [{"type": "text", "text": "Error: 'name' is required and cannot be empty."}],
            "is_error": True,
        }
    if agent_name == config.MASTER_AGENT_NAME:
        return {
            "content": [
                {"type": "text", "text": f"Error: cannot spawn agent with reserved name '{config.MASTER_AGENT_NAME}'."}
            ],
            "is_error": True,
        }
    if agent_name in agents.agents and not agent_resume:
        return {
            "content": [
                {
                    "type": "text",
                    "text": f"Error: agent '{agent_name}' already exists. Kill it first or use 'resume' to replace it.",
                }
            ],
            "is_error": True,
        }

    # Flowcoder-specific validation
    if agent_type == "flowcoder":
        if not config.FLOWCODER_ENABLED:
            return {
                "content": [{"type": "text", "text": "Error: flowcoder integration is disabled."}],
                "is_error": True,
            }

    # Auto-create worktree if cwd points to the main repo
    if agent_cwd == os.path.realpath(config.BOT_DIR) and not no_worktree and agent_name:
        worktree_path = os.path.join(config.BOT_WORKTREES_DIR, agent_name)
        branch = f"feature/{agent_name}"
        if not os.path.isdir(worktree_path):
            result = subprocess.run(
                ["git", "-C", config.BOT_DIR, "worktree", "add", worktree_path, "-b", branch],
                capture_output=True,
                text=True,
            )
            if result.returncode != 0:
                # Branch might already exist, try without -b
                result = subprocess.run(
                    ["git", "-C", config.BOT_DIR, "worktree", "add", worktree_path, branch],
                    capture_output=True,
                    text=True,
                )
                if result.returncode != 0:
                    log.warning("Failed to create worktree for '%s': %s", agent_name, result.stderr.strip())
                else:
                    agent_cwd = worktree_path
                    log.info("Auto-created worktree for '%s' at %s (existing branch)", agent_name, worktree_path)
            else:
                agent_cwd = worktree_path
                log.info("Auto-created worktree for '%s' at %s", agent_name, worktree_path)
        else:
            agent_cwd = worktree_path
            log.info("Reusing existing worktree for '%s' at %s", agent_name, worktree_path)

    # Use global ALLOWED_CWDS (which includes ALLOWED_CWDS and ADMIN_ALLOWED_CWDS from .env)
    if not any(agent_cwd == d or agent_cwd.startswith(d + os.sep) for d in config.ALLOWED_CWDS):
        return {
            "content": [
                {
                    "type": "text",
                    "text": "Error: cwd is not in allowed directories. Check ALLOWED_CWDS or ADMIN_ALLOWED_CWDS in .env.",
                }
            ],
            "is_error": True,
        }

    async def _do_spawn() -> None:
        try:
            if agent_name in agents.agents and agent_resume:
                await agents.reclaim_agent_name(agent_name)
            await agents.spawn_agent(
                agent_name,
                agent_cwd,
                agent_prompt,
                resume=agent_resume,
                agent_type=agent_type,
                command=fc_command,
                command_args=fc_command_args,
                extensions=agent_extensions,
                compact_instructions=compact_instructions,
                extra_mcp_servers=extra_mcp_servers,
            )
        except Exception:
            channels.bot_creating_channels.discard(agents.normalize_channel_name(agent_name))
            log.exception("Error in background spawn of agent '%s'", agent_name)
            try:
                channel = await agents.get_agent_channel(agent_name)
                if channel:
                    await agents.send_system(
                        channel, f"Failed to spawn agent **{agent_name}**. Check logs for details."
                    )
            except Exception:
                pass

    log.info(
        "Spawning agent '%s' via MCP tool (type=%s, cwd=%s, resume=%s, extensions=%s)",
        agent_name,
        agent_type,
        agent_cwd,
        agent_resume,
        agent_extensions,
    )
    # Guard against on_guild_channel_create race: mark channel as bot-created
    # BEFORE the background task runs, so the guard is already set when the
    # gateway event fires.  spawn_agent will discard it after agents[name] is set.
    channels.bot_creating_channels.add(agents.normalize_channel_name(agent_name))
    agents.fire_and_forget(_do_spawn())
    return {
        "content": [
            {
                "type": "text",
                "text": f"Agent '{agent_name}' ({agent_type}) spawn initiated in {agent_cwd}. The agent's channel will be notified when it's ready.",
            }
        ]
    }


@tool(
    "axi_kill_agent",
    "Kill an Axi agent session and move its Discord channel to the Killed category. "
    "Returns the session ID (for resuming later) or an error message.",
    {
        "type": "object",
        "properties": {
            "name": {"type": "string", "description": "Name of the agent to kill"},
        },
        "required": ["name"],
    },
)
async def axi_kill_agent(args: McpArgs) -> McpResult:
    agent_name = args.get("name", "").strip()
    set_agent_context(agent_name or "unknown")
    set_trigger("mcp_tool", detail="axi_kill_agent")
    _tracer.start_span("tool.axi_kill_agent", attributes={"agent.name": agent_name}).end()

    if not agent_name:
        return {
            "content": [{"type": "text", "text": "Error: 'name' is required and cannot be empty."}],
            "is_error": True,
        }
    if agent_name == config.MASTER_AGENT_NAME:
        return {
            "content": [{"type": "text", "text": f"Error: cannot kill reserved agent '{config.MASTER_AGENT_NAME}'."}],
            "is_error": True,
        }
    if agent_name not in agents.agents:
        return {"content": [{"type": "text", "text": f"Error: agent '{agent_name}' not found."}], "is_error": True}

    session = agents.agents[agent_name]
    session_id = session.session_id

    # Remove from agents dict immediately so the name is freed for respawn
    agents.agents.pop(agent_name, None)

    async def _do_kill() -> None:
        try:
            agent_ch = await agents.get_agent_channel(agent_name)
            if agent_ch:
                if session_id:
                    await agents.send_system(
                        agent_ch,
                        f"Agent **{agent_name}** moved to Killed.\n"
                        f"Session ID: `{session_id}` — use this to resume later.",
                    )
                else:
                    await agents.send_system(agent_ch, f"Agent **{agent_name}** moved to Killed.")
            await agents.sleep_agent(session, force=True)
            await agents.move_channel_to_killed(agent_name)
        except Exception:
            log.exception("Error in background kill of agent '%s'", agent_name)

    log.info("Killing agent '%s' via MCP tool (session=%s)", agent_name, session_id)
    agents.fire_and_forget(_do_kill())

    if session_id:
        return {"content": [{"type": "text", "text": f"Agent '{agent_name}' killed. Session ID: {session_id}"}]}
    return {"content": [{"type": "text", "text": f"Agent '{agent_name}' killed (no session ID available)."}]}


@tool(
    "axi_restart_agent",
    "Restart a single agent's CLI process with a fresh system prompt. "
    "Preserves session context (conversation history). The agent will pick up "
    "any changes to SYSTEM_PROMPT.md, extensions, or the core prompt on next wake.",
    {
        "type": "object",
        "properties": {
            "name": {"type": "string", "description": "Name of the agent to restart"},
        },
        "required": ["name"],
    },
)
async def axi_restart_agent(args: McpArgs) -> McpResult:
    agent_name = args.get("name", "").strip()
    set_agent_context(agent_name or "unknown")
    set_trigger("mcp_tool", detail="axi_restart_agent")
    _tracer.start_span("tool.axi_restart_agent", attributes={"agent.name": agent_name}).end()

    if not agent_name:
        return {"content": [{"type": "text", "text": "Error: 'name' is required."}], "is_error": True}
    if agent_name == config.MASTER_AGENT_NAME:
        return {
            "content": [{"type": "text", "text": "Error: use axi_restart to restart the master agent."}],
            "is_error": True,
        }
    if agent_name not in agents.agents:
        return {"content": [{"type": "text", "text": f"Error: agent '{agent_name}' not found."}], "is_error": True}

    session = await agents.restart_agent(agent_name)
    return {
        "content": [
            {
                "type": "text",
                "text": f"Agent '{agent_name}' restarted. System prompt refreshed, session '{session.session_id or 'none'}' preserved.",
            }
        ]
    }


@tool(
    "axi_restart",
    "Restart the Axi bot. Waits for busy agents to finish first (graceful). "
    "Only use when the user explicitly asks you to restart.",
    {"type": "object", "properties": {}, "required": []},
)
async def axi_restart(args: McpArgs) -> McpResult:
    set_agent_context(config.MASTER_AGENT_NAME)
    set_trigger("mcp_tool", detail="axi_restart")
    _tracer.start_span("tool.axi_restart").end()
    log.info("Restart requested via MCP tool")
    if agents.shutdown_coordinator is None:
        return {"content": [{"type": "text", "text": "Bot is not fully initialized yet."}]}
    agents.fire_and_forget(
        agents.shutdown_coordinator.graceful_shutdown("MCP tool", skip_agent=config.MASTER_AGENT_NAME)
    )
    return {"content": [{"type": "text", "text": "Graceful restart initiated. Waiting for busy agents to finish..."}]}


@tool(
    "axi_send_message",
    "Send a message to a spawned agent. The message appears in the agent's Discord channel "
    "(with your name as sender) and is processed like a user message. If the agent is busy, "
    "its current query is interrupted and your message is processed next. If sleeping, the "
    "agent wakes up. User-queued messages are preserved and process after yours.",
    {
        "type": "object",
        "properties": {
            "agent_name": {
                "type": "string",
                "description": "Name of the target agent to message",
            },
            "content": {
                "type": "string",
                "description": "The message content to send to the agent",
            },
            "sender": {
                "type": "string",
                "description": "Your agent name (defaults to master agent name)",
            },
        },
        "required": ["agent_name", "content"],
    },
)
async def axi_send_message(args: McpArgs) -> McpResult:
    target_name = args.get("agent_name", "").strip()
    set_agent_context(target_name or "unknown")
    set_trigger("mcp_tool", detail="axi_send_message")
    _tracer.start_span("tool.axi_send_message", attributes={"target.agent": target_name}).end()
    content = args.get("content", "").strip()

    if not target_name:
        return {
            "content": [{"type": "text", "text": "Error: agent_name is required."}],
            "is_error": True,
        }
    if not content:
        return {
            "content": [{"type": "text", "text": "Error: content is required."}],
            "is_error": True,
        }
    if target_name == config.MASTER_AGENT_NAME:
        return {
            "content": [{"type": "text", "text": "Error: cannot send messages to yourself."}],
            "is_error": True,
        }

    target_session = agents.agents.get(target_name)
    if target_session is None:
        return {
            "content": [{"type": "text", "text": f"Error: agent '{target_name}' not found."}],
            "is_error": True,
        }

    sender_name = args.get("sender", "").strip() or config.MASTER_AGENT_NAME
    log.info("Inter-agent message: '%s' -> '%s': %s", sender_name, target_name, content[:200])

    result = await agents.deliver_inter_agent_message(sender_name, target_session, content)
    return {"content": [{"type": "text", "text": result}]}


# ---------------------------------------------------------------------------
# Utility tools (available to all agents)
# ---------------------------------------------------------------------------


@tool(
    "get_date_and_time",
    "Get the current date and time with logical day/week calculations. "
    "Accounts for the user's configured day boundary (the hour when a new 'day' starts). "
    "Always call this first to orient yourself before working with plans.",
    {"type": "object", "properties": {}, "required": []},
)
async def get_date_and_time(args: McpArgs) -> McpResult:
    tz = os.environ.get("SCHEDULE_TIMEZONE", "UTC")
    boundary = config.DAY_BOUNDARY_HOUR

    now = arrow.now(tz)

    # Logical date: if before boundary hour, it's still "yesterday"
    if now.hour < boundary:
        logical = now.shift(days=-1)
    else:
        logical = now

    # Logical week start (Sunday)
    # arrow weekday(): Monday=0 ... Sunday=6
    days_since_sunday = (logical.weekday() + 1) % 7
    week_start = logical.shift(days=-days_since_sunday).floor("day")
    week_end = week_start.shift(days=6)

    # Format day boundary display
    if boundary == 0:
        boundary_display = "12:00 AM (midnight)"
    elif boundary < 12:
        boundary_display = f"{boundary}:00 AM"
    elif boundary == 12:
        boundary_display = "12:00 PM (noon)"
    else:
        boundary_display = f"{boundary - 12}:00 PM"

    result = {
        "now": now.isoformat(),
        "now_display": now.format("dddd, MMM D, YYYY h:mm A"),
        "logical_date": logical.format("YYYY-MM-DD"),
        "logical_date_display": logical.format("dddd, MMM D, YYYY"),
        "logical_day_of_week": logical.format("dddd"),
        "logical_week_start": week_start.format("YYYY-MM-DD"),
        "logical_week_display": f"Week of {week_start.format('MMM D')} \u2013 {week_end.format('MMM D, YYYY')}",
        "timezone": tz,
        "day_boundary": boundary_display,
    }

    return {"content": [{"type": "text", "text": json.dumps(result, indent=2)}]}


@tool(
    "discord_send_file",
    "Send a file as a Discord message attachment to your own channel or another channel. "
    "If channel_id is omitted, the file is sent to your own agent channel.",
    {
        "type": "object",
        "properties": {
            "channel_id": {
                "type": "string",
                "description": "The Discord channel ID. Omit to send to your own channel.",
            },
            "file_path": {"type": "string", "description": "Absolute path to the file to upload"},
            "content": {"type": "string", "description": "Optional text message to include with the file"},
        },
        "required": ["file_path"],
    },
)
async def discord_send_file(args: McpArgs) -> McpResult:
    file_path = args["file_path"]
    _tracer.start_span("tool.discord_send_file", attributes={"file.path": file_path}).end()
    content = args.get("content", "")
    channel_id = args.get("channel_id")
    if not channel_id:
        # Auto-resolve: find calling agent's channel via query_lock
        for ch_id, name in agents.channel_to_agent.items():
            session = agents.agents.get(name)
            if session and session.client is not None and session.query_lock.locked():
                channel_id = str(ch_id)
                break
    if not channel_id:
        return {
            "content": [{"type": "text", "text": "Error: could not determine channel. Provide channel_id explicitly."}],
            "is_error": True,
        }
    if not os.path.isfile(file_path):
        return {"content": [{"type": "text", "text": f"Error: file not found: {file_path}"}], "is_error": True}
    filename = os.path.basename(file_path)
    try:
        with open(file_path, "rb") as f:
            file_data = f.read()
        msg = await config.discord_client.send_file(channel_id, filename, file_data, content=content or None)
        return {"content": [{"type": "text", "text": f"File '{filename}' sent (msg id: {msg['id']})"}]}
    except Exception as e:
        return {"content": [{"type": "text", "text": f"Error: {e}"}], "is_error": True}


# ---------------------------------------------------------------------------
# Discord REST tools (cross-channel messaging)
# ---------------------------------------------------------------------------


@tool(
    "discord_list_channels",
    "List text channels in a Discord guild/server. Returns channel id, name, and category.",
    {
        "type": "object",
        "properties": {
            "guild_id": {"type": "string", "description": "The Discord guild (server) ID"},
        },
        "required": ["guild_id"],
    },
)
async def discord_list_channels(args: McpArgs) -> McpResult:
    guild_id = args["guild_id"]
    _tracer.start_span("tool.discord_list_channels", attributes={"discord.guild_id": guild_id}).end()
    try:
        text_channels = await config.discord_client.list_channels(guild_id)
        return {"content": [{"type": "text", "text": json.dumps(text_channels, indent=2)}]}
    except Exception as e:
        return {"content": [{"type": "text", "text": f"Error: {e}"}], "is_error": True}


@tool(
    "discord_read_messages",
    "Read recent messages from a Discord channel. Returns formatted message history.",
    {
        "type": "object",
        "properties": {
            "channel_id": {"type": "string", "description": "The Discord channel ID"},
            "limit": {"type": "integer", "description": "Number of messages to fetch (default 20, max 100)"},
        },
        "required": ["channel_id"],
    },
)
async def discord_read_messages(args: McpArgs) -> McpResult:
    channel_id = args["channel_id"]
    limit = min(args.get("limit", 20), 100)
    _tracer.start_span("tool.discord_read_messages", attributes={"discord.channel_id": channel_id, "limit": limit}).end()
    try:
        messages = await config.discord_client.get_messages(channel_id, limit=limit)
        # Messages come newest-first; reverse for chronological order
        messages.reverse()
        formatted: list[str] = []
        for msg in messages:
            author = msg.get("author", {}).get("username", "unknown")
            content = msg.get("content", "")
            timestamp = msg.get("timestamp", "")
            formatted.append(f"[{timestamp}] {author}: {content}")
        return {"content": [{"type": "text", "text": "\n".join(formatted)}]}
    except Exception as e:
        return {"content": [{"type": "text", "text": f"Error: {e}"}], "is_error": True}


@tool(
    "discord_send_message",
    "Send a message to a Discord channel OTHER than your own. Your text responses are automatically delivered to your own channel — do NOT use this tool for that. This tool is only for cross-channel messaging.",
    {
        "type": "object",
        "properties": {
            "channel_id": {"type": "string", "description": "The Discord channel ID"},
            "content": {"type": "string", "description": "The message content to send"},
        },
        "required": ["channel_id", "content"],
    },
)
async def discord_send_message(args: McpArgs) -> McpResult:
    channel_id = args["channel_id"]
    content = args["content"]
    _tracer.start_span("tool.discord_send_message", attributes={"discord.channel_id": channel_id}).end()
    # Prevent agents from sending to their own channel (responses are streamed automatically)
    agent_name = agents.channel_to_agent.get(int(channel_id))
    if agent_name:
        return {
            "content": [
                {
                    "type": "text",
                    "text": f"Error: Cannot send to agent channel #{agent_name}. "
                    f"Your text responses are automatically sent to your own channel. "
                    f"Just write your response as normal text instead of using this tool. "
                    f"This tool is only for sending messages to OTHER channels.",
                }
            ],
            "is_error": True,
        }
    try:
        msg = await config.discord_client.send_message(channel_id, content)
        return {"content": [{"type": "text", "text": f"Message sent (id: {msg['id']})"}]}
    except Exception as e:
        return {"content": [{"type": "text", "text": f"Error: {e}"}], "is_error": True}


# ---------------------------------------------------------------------------
# Channel status tools (available to all agents)
# ---------------------------------------------------------------------------


@tool(
    "set_channel_status",
    "Set the user's to-do type emoji on your Discord channel name. "
    "The emoji represents what type of action the user needs to take next. "
    "Pass an empty string to clear.",
    {
        "type": "object",
        "properties": {
            "emoji": {
                "type": "string",
                "description": (
                    "A single emoji representing the user's to-do type. "
                    "Examples: \u2753 (awaiting direction), "
                    "\U0001f4ac (user should respond), \U0001f4da (user should read), "
                    "\U0001f52c (user should test). Never use \u2705 (checkmark). "
                    "Pass empty string to clear."
                ),
            },
        },
        "required": ["emoji"],
    },
)
async def set_channel_status(args: McpArgs) -> McpResult:
    emoji = args.get("emoji", "").strip()
    _tracer.start_span("tool.set_channel_status", attributes={"emoji": emoji}).end()
    if not config.CHANNEL_STATUS_ENABLED:
        return {"content": [{"type": "text", "text": "Channel status prefixes are not enabled."}]}

    # Find calling agent by checking which agent is currently processing
    caller: str | None = None
    for name, session in agents.agents.items():
        if session.client is not None and session.query_lock.locked():
            caller = name
            break
    if not caller:
        return {"content": [{"type": "text", "text": "Error: could not determine calling agent."}], "is_error": True}

    if emoji:
        channels.set_status_override(caller, emoji)
        channels.schedule_status_update()
        return {"content": [{"type": "text", "text": f"Status set to '{emoji}'. Channel will update shortly."}]}
    else:
        channels.set_status_override(caller, None)
        channels.schedule_status_update()
        return {"content": [{"type": "text", "text": "Custom status cleared. Channel will revert to auto-detected status."}]}


@tool(
    "clear_channel_status",
    "Clear your custom channel status and revert to auto-detected status.",
    {"type": "object", "properties": {}, "required": []},
)
async def clear_channel_status(args: McpArgs) -> McpResult:
    _tracer.start_span("tool.clear_channel_status").end()
    if not config.CHANNEL_STATUS_ENABLED:
        return {"content": [{"type": "text", "text": "Channel status prefixes are not enabled."}]}

    caller: str | None = None
    for name, session in agents.agents.items():
        if session.client is not None and session.query_lock.locked():
            caller = name
            break
    if not caller:
        return {"content": [{"type": "text", "text": "Error: could not determine calling agent."}], "is_error": True}

    channels.set_status_override(caller, None)
    channels.schedule_status_update()
    return {"content": [{"type": "text", "text": "Custom status cleared. Channel will revert to auto-detected status."}]}


# ---------------------------------------------------------------------------
# MCP server assembly
# ---------------------------------------------------------------------------

# Utility tools (shared by all agents)
utils_mcp_server = create_sdk_mcp_server(
    name="utils",
    version="1.0.0",
    tools=[get_date_and_time, discord_send_file, set_channel_status, clear_channel_status],
)

# Spawned agents get spawn+kill+restart-agent (no bot restart — they tell the parent)
axi_mcp_server = create_sdk_mcp_server(
    name="axi",
    version="1.0.0",
    tools=[axi_spawn_agent, axi_kill_agent, axi_restart_agent],
)

# Master agent gets the full set including bot restart + agent restart + send_message
axi_master_mcp_server = create_sdk_mcp_server(
    name="axi",
    version="1.0.0",
    tools=[axi_spawn_agent, axi_kill_agent, axi_restart, axi_restart_agent, axi_send_message],
)

# Discord REST tools for cross-server messaging
discord_mcp_server = create_sdk_mcp_server(
    name="discord",
    version="1.0.0",
    tools=[discord_list_channels, discord_read_messages, discord_send_message],
)


def sdk_mcp_servers_for_cwd(cwd: str, agent_name: str | None = None) -> dict[str, Any]:
    """Return the appropriate SDK MCP servers for a given working directory.

    All agents get the axi MCP server (spawn/kill).  The master agent overrides
    with the master version (which adds restart).  Admin agents (cwd in BOT_DIR)
    additionally get Discord MCP tools and see all schedules.
    """
    servers: dict[str, Any] = {"utils": utils_mcp_server}
    if agent_name:
        servers["schedule"] = make_schedule_mcp_server(
            agent_name,
            config.SCHEDULES_PATH,
            cwd,
        )
    servers["playwright"] = {
        "command": "npx",
        "args": ["@playwright/mcp@latest", "--headless"],
    }
    # All agents get spawn/kill; master overrides with restart version
    servers["axi"] = axi_mcp_server
    return servers
