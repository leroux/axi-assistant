"""Agent Hub -- multi-agent session orchestration.

Manages N concurrent LLM agent sessions: lifecycle, concurrency, message
queuing, rate limits, hot restart, and graceful shutdown. No UI dependency.
The frontend (Discord, CLI, web) plugs in via FrontendCallbacks.

AgentHub is Claude-specific — it depends on Claude Wire (the stream-json
protocol wrapper), not the Claude SDK directly.
"""

from agenthub.callbacks import FrontendCallbacks
from agenthub.hub import AgentHub
from agenthub.lifecycle import (
    count_awake,
    is_awake,
    is_processing,
    reset_activity,
    sleep_agent,
    wake_agent,
    wake_or_queue,
)
from agenthub.messaging import (
    StreamHandlerFn,
    deliver_inter_agent_message,
    handle_query_timeout,
    interrupt_session,
    process_message,
    process_message_queue,
    run_initial_prompt,
)
from agenthub.permissions import build_permission_callback, compute_allowed_paths
from agenthub.procmux_wire import ProcmuxProcessConnection
from agenthub.rate_limits import RateLimitTracker
from agenthub.reconnect import connect_procmux, reconnect_single
from agenthub.registry import (
    end_session,
    get_session,
    rebuild_session,
    reclaim_agent_name,
    register_session,
    reset_session,
    spawn_agent,
    unregister_session,
)
from agenthub.scheduler import Scheduler
from agenthub.shutdown import ShutdownCoordinator, exit_for_restart, kill_supervisor
from agenthub.tasks import BackgroundTaskSet
from agenthub.types import (
    AgentSession,
    ConcurrencyLimitError,
    MessageContent,
    RateLimitQuota,
    SessionUsage,
)

__all__ = [
    "AgentHub",
    "AgentSession",
    "BackgroundTaskSet",
    "ConcurrencyLimitError",
    "FrontendCallbacks",
    "MessageContent",
    "ProcmuxProcessConnection",
    "RateLimitQuota",
    "RateLimitTracker",
    "Scheduler",
    "SessionUsage",
    "ShutdownCoordinator",
    "StreamHandlerFn",
    "build_permission_callback",
    "compute_allowed_paths",
    "connect_procmux",
    "count_awake",
    "deliver_inter_agent_message",
    "end_session",
    "exit_for_restart",
    "get_session",
    "handle_query_timeout",
    "interrupt_session",
    "is_awake",
    "is_processing",
    "kill_supervisor",
    "process_message",
    "process_message_queue",
    "rebuild_session",
    "reclaim_agent_name",
    "reconnect_single",
    "register_session",
    "reset_activity",
    "reset_session",
    "run_initial_prompt",
    "sleep_agent",
    "spawn_agent",
    "unregister_session",
    "wake_agent",
    "wake_or_queue",
]
