"""Claude Wire -- Claude CLI stream-json protocol wrapper.

Wraps the Claude Code CLI's ``--output-format stream-json`` protocol.
Has NO dependency on procmux or any specific process transport backend.
The wiring layer (e.g. agenthub) provides an adapter from a concrete
transport to the ProcessConnection protocol.

- BridgeTransport: SDK Transport implementation over any ProcessConnection
- DirectProcessConnection: local PTY subprocess ProcessConnection
- ProcessConnection: protocol that any process backend must satisfy
- Event types: StdoutEvent, StderrEvent, ExitEvent
- build_cli_spawn_args: CLI argument construction (requires claude-agent-sdk)
- Event parsing and activity tracking
- Session lifecycle helpers (disconnect, subprocess cleanup)
"""

from claudewire.direct import DirectProcessConnection, find_claude
from claudewire.events import (
    ActivityState,
    RateLimitInfo,
    as_stream,
    parse_rate_limit_event,
    update_activity,
)
from claudewire.permissions import (
    allow_all,
    compose,
    cwd_policy,
    deny_all,
    tool_allow_policy,
    tool_block_policy,
)
from claudewire.schema import (
    AssistantMessageInner,
    AssistantMsg,
    ContentBlock,
    ContentBlockDeltaEvent,
    ContentBlockStartEvent,
    ContentBlockStopEvent,
    ControlRequestMsg,
    ControlResponseMsg,
    Delta,
    InboundMsg,
    MessageDeltaEvent,
    MessageStartEvent,
    MessageStopEvent,
    OutboundMsg,
    RateLimitEventMsg,
    ResultMsg,
    SchemaValidationError,
    StreamEvent,
    StreamEventMsg,
    SystemMsg,
    TextBlock,
    ThinkingBlock,
    ToolUseBlock,
    Usage,
    UserMsg,
    ValidationError,
    ValidationResult,
    validate_inbound,
    validate_inbound_or_bare,
    validate_outbound,
)
from claudewire.session import disconnect_client, ensure_process_dead, get_subprocess_pid
from claudewire.transport import BridgeTransport
from claudewire.types import (
    CommandResult,
    ExitEvent,
    ProcessConnection,
    ProcessEvent,
    ProcessEventQueue,
    StderrEvent,
    StdoutEvent,
)


def build_cli_spawn_args(*args: object, **kwargs: object) -> tuple[list[str], dict[str, str], str]:
    """Lazy import — requires claude-agent-sdk to be installed."""
    from claudewire.cli import build_cli_spawn_args as _impl

    return _impl(*args, **kwargs)  # type: ignore[arg-type]


__all__ = [
    "ActivityState",
    "AssistantMessageInner",
    "AssistantMsg",
    "BridgeTransport",
    "CommandResult",
    "ContentBlock",
    "ContentBlockDeltaEvent",
    "ContentBlockStartEvent",
    "ContentBlockStopEvent",
    "ControlRequestMsg",
    "ControlResponseMsg",
    "Delta",
    "DirectProcessConnection",
    "ExitEvent",
    "InboundMsg",
    "MessageDeltaEvent",
    "MessageStartEvent",
    "MessageStopEvent",
    "OutboundMsg",
    "ProcessConnection",
    "ProcessEvent",
    "ProcessEventQueue",
    "RateLimitEventMsg",
    "RateLimitInfo",
    "ResultMsg",
    "SchemaValidationError",
    "StderrEvent",
    "StdoutEvent",
    "StreamEvent",
    "StreamEventMsg",
    "SystemMsg",
    "TextBlock",
    "ThinkingBlock",
    "ToolUseBlock",
    "Usage",
    "UserMsg",
    "ValidationError",
    "ValidationResult",
    "allow_all",
    "as_stream",
    "build_cli_spawn_args",
    "compose",
    "cwd_policy",
    "deny_all",
    "disconnect_client",
    "ensure_process_dead",
    "find_claude",
    "get_subprocess_pid",
    "parse_rate_limit_event",
    "tool_allow_policy",
    "tool_block_policy",
    "update_activity",
    "validate_inbound",
    "validate_inbound_or_bare",
    "validate_outbound",
]
