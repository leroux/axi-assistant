"""Agent Hub -- multi-agent session orchestration.

Manages N concurrent LLM agent sessions: lifecycle, concurrency, message
queuing, rate limits, hot restart, and graceful shutdown. No UI dependency.
The frontend (Discord, CLI, web) plugs in via FrontendCallbacks.

AgentHub is Claude-specific — it depends on Claude Wire (the stream-json
protocol wrapper), not the Claude SDK directly.
"""

from agenthub.agent_log import AgentLog, LogEvent, make_agent_log, make_event
from agenthub.callbacks import FrontendCallbacks
from agenthub.frontend import Frontend, PlanApprovalResult
from agenthub.frontend_router import FrontendRouter
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
    ReceiveResult,
    StreamHandlerFn,
    deliver_inter_agent_message,
    handle_query_timeout,
    interrupt_session,
    process_message,
    process_message_queue,
    receive_user_message,
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
from agenthub.stream_types import StreamOutput
from agenthub.streaming import receive_response_safe, stream_response
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
    "AgentLog",
    "AgentSession",
    "BackgroundTaskSet",
    "ConcurrencyLimitError",
    "Frontend",
    "FrontendCallbacks",
    "FrontendRouter",
    "LogEvent",
    "MessageContent",
    "PlanApprovalResult",
    "ProcmuxProcessConnection",
    "RateLimitQuota",
    "RateLimitTracker",
    "ReceiveResult",
    "Scheduler",
    "SessionUsage",
    "ShutdownCoordinator",
    "StreamHandlerFn",
    "StreamOutput",
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
    "make_agent_log",
    "make_event",
    "process_message",
    "process_message_queue",
    "rebuild_session",
    "receive_response_safe",
    "receive_user_message",
    "reclaim_agent_name",
    "reconnect_single",
    "register_session",
    "reset_activity",
    "reset_session",
    "run_initial_prompt",
    "sleep_agent",
    "spawn_agent",
    "stream_response",
    "unregister_session",
    "wake_agent",
    "wake_or_queue",
]
