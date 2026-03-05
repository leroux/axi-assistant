"""Strict JSON schema validation for Claude CLI stream-json protocol messages.

Pydantic models for every message type in the Claude CLI stream-json protocol.
Validates structure, flags unknown keys as warnings, and returns parsed models
on success. Models are defined from real bridge-stdio log data.

Usage:
    result = validate_inbound(raw_dict)
    if result.errors:
        handle_errors(result.errors)
    else:
        msg = result.model  # typed pydantic model
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter
from pydantic import ValidationError as PydanticValidationError

log = logging.getLogger(__name__)

# When true, validation errors raise instead of just being returned.
STRICT_MODE = os.environ.get("SCHEMA_STRICT_MODE", "").lower() in ("1", "true", "yes")


# ---------------------------------------------------------------------------
# Validation result types
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class ValidationError:
    """A single schema validation issue."""

    path: str  # e.g. "event.delta.type"
    message: str  # e.g. "unexpected value 'foo', expected one of: ..."
    raw_value: Any = None  # the actual value found
    level: str = "error"  # "error" or "warning"

    def __str__(self) -> str:
        if self.raw_value is not None:
            return f"[{self.level}] {self.path}: {self.message} (got {self.raw_value!r})"
        return f"[{self.level}] {self.path}: {self.message}"


class SchemaValidationError(Exception):
    """Raised in strict mode when validation fails."""

    def __init__(self, errors: list[ValidationError], raw: dict[str, Any]):
        self.errors = errors
        self.raw = raw
        super().__init__(f"Schema validation failed: {errors[0]}")


@dataclass(slots=True)
class ValidationResult:
    """Result of validating a message — either a parsed model or errors."""

    model: BaseModel | None
    errors: list[ValidationError]

    @property
    def ok(self) -> bool:
        return not any(e.level == "error" for e in self.errors)


# ---------------------------------------------------------------------------
# Base configs
# ---------------------------------------------------------------------------


class _Strict(BaseModel):
    """Default: reject unknown keys (surfaced as warnings)."""
    model_config = ConfigDict(extra="forbid")


class _Permissive(BaseModel):
    """Allow extra keys silently (for dynamic/polymorphic payloads)."""
    model_config = ConfigDict(extra="allow")


# ---------------------------------------------------------------------------
# Content blocks (assistant message content, content_block_start)
# ---------------------------------------------------------------------------


class TextBlock(_Strict):
    type: Literal["text"]
    text: str


class ToolUseBlock(_Strict):
    type: Literal["tool_use"]
    id: str
    name: str
    input: dict[str, Any]
    caller: dict[str, Any] | None = None


class ThinkingBlock(_Strict):
    type: Literal["thinking"]
    thinking: str
    signature: str


ContentBlock = Annotated[
    TextBlock | ToolUseBlock | ThinkingBlock,
    Field(discriminator="type"),
]


# ---------------------------------------------------------------------------
# Deltas (inside content_block_delta)
# ---------------------------------------------------------------------------


class TextDelta(_Strict):
    type: Literal["text_delta"]
    text: str


class InputJsonDelta(_Strict):
    type: Literal["input_json_delta"]
    partial_json: str


class ThinkingDelta(_Strict):
    type: Literal["thinking_delta"]
    thinking: str


class SignatureDelta(_Strict):
    type: Literal["signature_delta"]
    signature: str


Delta = Annotated[
    TextDelta | InputJsonDelta | ThinkingDelta | SignatureDelta,
    Field(discriminator="type"),
]


# ---------------------------------------------------------------------------
# Shared models
# ---------------------------------------------------------------------------


class Usage(_Permissive):
    """Token usage — extra="allow" because new fields appear frequently."""
    input_tokens: int | None = None
    output_tokens: int | None = None
    cache_creation_input_tokens: int | None = None
    cache_read_input_tokens: int | None = None
    cache_creation: dict[str, Any] | None = None
    service_tier: str | None = None
    inference_geo: str | None = None
    # Fields seen in result.usage but not in stream events:
    iterations: list[Any] | None = None
    server_tool_use: dict[str, Any] | None = None
    speed: str | None = None


class ContextManagement(_Strict):
    applied_edits: list[Any] | None = None


class MessageDeltaDelta(_Strict):
    stop_reason: Literal["end_turn", "tool_use", "max_tokens"] | None = None
    stop_sequence: str | None = None


# ---------------------------------------------------------------------------
# Inner Anthropic API stream events
# ---------------------------------------------------------------------------


class MessageInner(_Strict):
    """The message object inside message_start and assistant messages."""
    model: str
    id: str
    type: Literal["message"]
    role: Literal["assistant"]
    content: list[Any]
    stop_reason: Any = None
    stop_sequence: Any = None
    usage: Usage | None = None
    context_management: Any = None


class MessageStartEvent(_Strict):
    type: Literal["message_start"]
    message: MessageInner


class MessageDeltaEvent(_Strict):
    type: Literal["message_delta"]
    delta: MessageDeltaDelta
    usage: Usage | None = None
    context_management: ContextManagement | None = None


class MessageStopEvent(_Strict):
    type: Literal["message_stop"]


class ContentBlockStartEvent(_Strict):
    type: Literal["content_block_start"]
    index: int
    content_block: ContentBlock


class ContentBlockDeltaEvent(_Strict):
    type: Literal["content_block_delta"]
    index: int
    delta: Delta


class ContentBlockStopEvent(_Strict):
    type: Literal["content_block_stop"]
    index: int


StreamEvent = Annotated[
    MessageStartEvent | MessageDeltaEvent | MessageStopEvent | ContentBlockStartEvent | ContentBlockDeltaEvent | ContentBlockStopEvent,
    Field(discriminator="type"),
]

_stream_event_adapter: TypeAdapter[StreamEvent] = TypeAdapter(StreamEvent)


# ---------------------------------------------------------------------------
# User message content blocks
# ---------------------------------------------------------------------------


class ToolResultBlock(_Permissive):
    """Tool result — extra="allow" for the many tool-specific fields."""
    type: Literal["tool_result"]
    tool_use_id: str
    content: Any = None
    is_error: bool | None = None


UserContentBlock = Annotated[
    TextBlock | ToolUseBlock | ToolResultBlock,
    Field(discriminator="type"),
]


class UserMessageInner(_Strict):
    role: Literal["user"]
    content: str | list[UserContentBlock]


class AssistantMessageInner(_Strict):
    model: str
    id: str
    type: Literal["message"]
    role: Literal["assistant"]
    content: list[ContentBlock]
    stop_reason: Any = None
    stop_sequence: Any = None
    usage: Usage | None = None
    context_management: Any = None


# ---------------------------------------------------------------------------
# Control protocol
# ---------------------------------------------------------------------------


class ControlRequestInner(_Permissive):
    """Control request payload — subtypes vary, so allow extra fields."""
    subtype: str  # open-ended: can_use_tool, mcp_message, initialize, hook_callback, etc.


class ControlResponseInner(_Permissive):
    """Control response — allow extra for response payload variants."""
    subtype: Literal["success", "error"]
    request_id: str
    response: Any = None
    error: str | None = None


# ---------------------------------------------------------------------------
# Rate limit
# ---------------------------------------------------------------------------


class RateLimitInfoSchema(_Permissive):
    """Rate limit info — extra="allow" for new fields like overage."""
    status: Literal["allowed", "allowed_warning", "rejected"]
    resetsAt: int | float | None = None
    rateLimitType: str | None = None
    utilization: int | float | None = None
    isUsingOverage: bool | None = None
    surpassedThreshold: int | float | None = None
    overageStatus: str | None = None
    overageDisabledReason: str | None = None


# ---------------------------------------------------------------------------
# Top-level inbound messages (CLI → us)
# ---------------------------------------------------------------------------


class StreamEventMsg(_Strict):
    type: Literal["stream_event"]
    uuid: str
    session_id: str
    parent_tool_use_id: str | None = None
    event: StreamEvent


class AssistantMsg(_Strict):
    type: Literal["assistant"]
    message: AssistantMessageInner
    parent_tool_use_id: str | None = None
    session_id: str
    uuid: str
    error: str | None = None


class UserMsg(_Permissive):
    """User message — extra="allow" for isSynthetic and future fields."""
    type: Literal["user"]
    message: UserMessageInner | None = None
    content: str | list[UserContentBlock] | None = None
    session_id: str | None = None
    parent_tool_use_id: str | None = None
    uuid: str | None = None
    tool_use_result: Any = None


class SystemMsg(_Permissive):
    """System messages have dynamic extra fields depending on subtype."""
    type: Literal["system"]
    subtype: str


class ResultMsg(_Permissive):
    """Result message — extra="allow" for evolving fields."""
    type: Literal["result"]
    subtype: Literal["success", "error"]
    is_error: bool
    duration_ms: int
    duration_api_ms: int
    num_turns: int
    session_id: str
    uuid: str
    result: str | None = None
    stop_reason: Any = None
    total_cost_usd: int | float | None = None
    usage: Usage | None = None
    modelUsage: dict[str, Any] | None = None
    permission_denials: list[Any] | None = None
    fast_mode_state: str | None = None
    structured_output: Any = None


class ControlRequestMsg(_Strict):
    type: Literal["control_request"]
    request_id: str
    request: ControlRequestInner


class ControlResponseMsg(_Strict):
    type: Literal["control_response"]
    response: ControlResponseInner


class RateLimitEventMsg(_Strict):
    type: Literal["rate_limit_event"]
    rate_limit_info: RateLimitInfoSchema
    uuid: str
    session_id: str


InboundMsg = Annotated[
    StreamEventMsg | AssistantMsg | UserMsg | SystemMsg | ResultMsg | ControlRequestMsg | ControlResponseMsg | RateLimitEventMsg,
    Field(discriminator="type"),
]

_inbound_adapter: TypeAdapter[InboundMsg] = TypeAdapter(InboundMsg)


# ---------------------------------------------------------------------------
# Top-level outbound messages (us → CLI)
# ---------------------------------------------------------------------------


class UserOutMsg(_Strict):
    type: Literal["user"]
    content: str | list[UserContentBlock] | None = None
    session_id: str | None = None
    message: UserMessageInner | None = None
    parent_tool_use_id: str | None = None


class ControlRequestOutMsg(_Strict):
    type: Literal["control_request"]
    request_id: str
    request: ControlRequestInner


class ControlResponseOutMsg(_Strict):
    type: Literal["control_response"]
    response: ControlResponseInner


OutboundMsg = Annotated[
    UserOutMsg | ControlRequestOutMsg | ControlResponseOutMsg,
    Field(discriminator="type"),
]

_outbound_adapter: TypeAdapter[OutboundMsg] = TypeAdapter(OutboundMsg)


# ---------------------------------------------------------------------------
# Bare stream event types (procmux replay emits without wrapper)
# ---------------------------------------------------------------------------

_BARE_STREAM_TYPES = frozenset(_stream_event_adapter.core_schema.get("choices", {}).keys()) if False else frozenset({
    "message_start", "message_delta", "message_stop",
    "content_block_start", "content_block_delta", "content_block_stop",
})


# ---------------------------------------------------------------------------
# Error classification
# ---------------------------------------------------------------------------


def _classify_pydantic_errors(exc: PydanticValidationError) -> list[ValidationError]:
    """Convert pydantic ValidationError into our ValidationError list.

    extra_forbidden errors → warning level (unknown keys from protocol updates).
    Everything else → error level.
    """
    errors: list[ValidationError] = []
    for err in exc.errors():
        path = ".".join(str(p) for p in err["loc"])
        msg = err["msg"]
        err_type = err["type"]

        if err_type == "extra_forbidden":
            errors.append(ValidationError(
                path=path,
                message="unknown key",
                raw_value=err.get("input"),
                level="warning",
            ))
        else:
            errors.append(ValidationError(
                path=path,
                message=msg,
                raw_value=err.get("input"),
                level="error",
            ))
    return errors


def _strip_trace_context(data: dict[str, Any]) -> dict[str, Any]:
    """Remove _trace_context before validation (OTel injection, always allowed)."""
    if "_trace_context" in data:
        return {k: v for k, v in data.items() if k != "_trace_context"}
    return data


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def validate_inbound(msg: dict[str, Any]) -> ValidationResult:
    """Validate and parse a message received from the CLI.

    Returns a ValidationResult with either the parsed model or errors.
    """
    msg_type = msg.get("type")
    if msg_type is None:
        return ValidationResult(
            model=None,
            errors=[ValidationError("type", "missing required 'type' field")],
        )

    cleaned = _strip_trace_context(msg)
    try:
        model = _inbound_adapter.validate_python(cleaned)
    except PydanticValidationError as exc:
        return ValidationResult(model=None, errors=_classify_pydantic_errors(exc))
    return ValidationResult(model=model, errors=[])


def validate_outbound(msg: dict[str, Any]) -> ValidationResult:
    """Validate and parse a message being sent to the CLI.

    Returns a ValidationResult with either the parsed model or errors.
    """
    msg_type = msg.get("type")
    if msg_type is None:
        return ValidationResult(
            model=None,
            errors=[ValidationError("type", "missing required 'type' field")],
        )

    cleaned = _strip_trace_context(msg)
    try:
        model = _outbound_adapter.validate_python(cleaned)
    except PydanticValidationError as exc:
        return ValidationResult(model=None, errors=_classify_pydantic_errors(exc))
    return ValidationResult(model=model, errors=[])


def validate_inbound_or_bare(msg: dict[str, Any]) -> ValidationResult:
    """Like validate_inbound, but also accepts bare Anthropic stream events.

    Some code paths (procmux replay) emit content_block_delta, message_start,
    etc. as top-level messages without the stream_event wrapper.
    """
    msg_type = msg.get("type")
    if msg_type in _BARE_STREAM_TYPES:
        cleaned = _strip_trace_context(msg)
        try:
            model = _stream_event_adapter.validate_python(cleaned)
        except PydanticValidationError as exc:
            return ValidationResult(model=None, errors=_classify_pydantic_errors(exc))
        return ValidationResult(model=model, errors=[])
    return validate_inbound(msg)
