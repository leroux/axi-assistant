"""Strict JSON schema validation for Claude CLI stream-json protocol messages.

Validates every message exchanged with the CLI against known schemas.
Unknown keys are flagged (protocol additions), missing required keys
and type mismatches are errors. Validation is non-blocking — errors are
collected and reported, never silently swallowed.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Any

log = logging.getLogger(__name__)

# When true, validation errors raise instead of just being returned.
STRICT_MODE = os.environ.get("SCHEMA_STRICT_MODE", "").lower() in ("1", "true", "yes")


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


# ---------------------------------------------------------------------------
# Schema definition helpers
# ---------------------------------------------------------------------------

# A schema is a dict mapping key names to specs.
# Each spec is a tuple: (required: bool, type_check, nested_schema | None)
# type_check is one of:
#   - A Python type (str, int, float, bool, list, dict)
#   - A set of allowed literal values: {"text", "tool_use", "thinking"}
#   - None means "any type" (skip type check)
#   - A callable (validator function) for complex cases

type Spec = tuple[bool, Any, dict[str, Any] | None]
type Schema = dict[str, Spec]

# Sentinel for "any type is fine"
ANY = None


def _check(
    data: dict[str, Any],
    schema: Schema,
    path: str,
    errors: list[ValidationError],
) -> None:
    """Validate a dict against a schema, appending errors."""
    if not isinstance(data, dict):
        errors.append(ValidationError(path, f"expected dict, got {type(data).__name__}", data))
        return

    schema_keys = set(schema.keys())
    data_keys = set(data.keys())

    # Check for unknown keys
    for key in sorted(data_keys - schema_keys):
        # _trace_context is injected by OTel, always allow
        if key == "_trace_context":
            continue
        errors.append(ValidationError(
            f"{path}.{key}" if path else key,
            "unknown key",
            data[key],
            level="warning",
        ))

    # Check each schema key
    for key, (required, type_check, nested) in schema.items():
        full_path = f"{path}.{key}" if path else key

        if key not in data:
            if required:
                errors.append(ValidationError(full_path, "required key missing"))
            continue

        value = data[key]

        # None values are allowed for optional fields
        if value is None and not required:
            continue

        # Type checking
        if type_check is not ANY:
            if isinstance(type_check, set):
                # Literal values check
                if value not in type_check:
                    errors.append(ValidationError(
                        full_path,
                        f"expected one of {sorted(type_check)}, got {value!r}",
                        value,
                    ))
            elif isinstance(type_check, tuple):
                # Union of types
                if not isinstance(value, type_check):
                    errors.append(ValidationError(
                        full_path,
                        f"expected {' | '.join(t.__name__ for t in type_check)}, got {type(value).__name__}",
                        type(value).__name__,
                    ))
            elif isinstance(type_check, type):
                if not isinstance(value, type_check):
                    errors.append(ValidationError(
                        full_path,
                        f"expected {type_check.__name__}, got {type(value).__name__}",
                        type(value).__name__,
                    ))
            elif callable(type_check):
                type_check(value, full_path, errors)

        # Nested schema
        if nested and isinstance(value, dict):
            _check(value, nested, full_path, errors)


# ---------------------------------------------------------------------------
# Schema for inner Anthropic API stream events (inside stream_event.event)
# ---------------------------------------------------------------------------

_USAGE_SCHEMA: Schema = {
    "input_tokens": (False, int, None),
    "output_tokens": (False, int, None),
    "cache_creation_input_tokens": (False, int, None),
    "cache_read_input_tokens": (False, int, None),
    "cache_creation": (False, dict, None),
    "service_tier": (False, str, None),
    "inference_geo": (False, str, None),
}

_CONTEXT_MANAGEMENT_SCHEMA: Schema = {
    "applied_edits": (False, list, None),
}


def _validate_content_block(value: Any, path: str, errors: list[ValidationError]) -> None:
    """Validate a content_block inside content_block_start."""
    if not isinstance(value, dict):
        errors.append(ValidationError(path, f"expected dict, got {type(value).__name__}"))
        return
    block_type = value.get("type")
    if block_type == "text":
        _check(value, {
            "type": (True, {"text"}, None),
            "text": (True, str, None),
        }, path, errors)
    elif block_type == "tool_use":
        _check(value, {
            "type": (True, {"tool_use"}, None),
            "id": (True, str, None),
            "name": (True, str, None),
            "input": (True, dict, None),
            "caller": (False, dict, None),
        }, path, errors)
    elif block_type == "thinking":
        _check(value, {
            "type": (True, {"thinking"}, None),
            "thinking": (True, str, None),
            "signature": (True, str, None),
        }, path, errors)
    else:
        errors.append(ValidationError(
            f"{path}.type",
            f"expected one of ['text', 'thinking', 'tool_use'], got {block_type!r}",
            block_type,
        ))


def _validate_delta(value: Any, path: str, errors: list[ValidationError]) -> None:
    """Validate a delta inside content_block_delta."""
    if not isinstance(value, dict):
        errors.append(ValidationError(path, f"expected dict, got {type(value).__name__}"))
        return
    delta_type = value.get("type")
    if delta_type == "text_delta":
        _check(value, {
            "type": (True, {"text_delta"}, None),
            "text": (True, str, None),
        }, path, errors)
    elif delta_type == "input_json_delta":
        _check(value, {
            "type": (True, {"input_json_delta"}, None),
            "partial_json": (True, str, None),
        }, path, errors)
    elif delta_type == "thinking_delta":
        _check(value, {
            "type": (True, {"thinking_delta"}, None),
            "thinking": (True, str, None),
        }, path, errors)
    elif delta_type == "signature_delta":
        _check(value, {
            "type": (True, {"signature_delta"}, None),
            "signature": (True, str, None),
        }, path, errors)
    else:
        errors.append(ValidationError(
            f"{path}.type",
            f"expected one of ['input_json_delta', 'signature_delta', 'text_delta', 'thinking_delta'], got {delta_type!r}",
            delta_type,
        ))


def _validate_message_delta_delta(value: Any, path: str, errors: list[ValidationError]) -> None:
    """Validate the delta inside a message_delta event."""
    if not isinstance(value, dict):
        errors.append(ValidationError(path, f"expected dict, got {type(value).__name__}"))
        return
    _check(value, {
        "stop_reason": (False, {"end_turn", "tool_use", "max_tokens"}, None),
        "stop_sequence": (False, (str, type(None)), None),
    }, path, errors)


_STREAM_EVENT_SCHEMAS: dict[str, Schema] = {
    "message_start": {
        "type": (True, {"message_start"}, None),
        "message": (True, dict, {
            "model": (True, str, None),
            "id": (True, str, None),
            "type": (True, {"message"}, None),
            "role": (True, {"assistant"}, None),
            "content": (True, list, None),
            "stop_reason": (False, ANY, None),
            "stop_sequence": (False, ANY, None),
            "usage": (False, dict, _USAGE_SCHEMA),
            "context_management": (False, ANY, None),
        }),
    },
    "message_delta": {
        "type": (True, {"message_delta"}, None),
        "delta": (True, _validate_message_delta_delta, None),
        "usage": (False, dict, _USAGE_SCHEMA),
        "context_management": (False, dict, _CONTEXT_MANAGEMENT_SCHEMA),
    },
    "message_stop": {
        "type": (True, {"message_stop"}, None),
    },
    "content_block_start": {
        "type": (True, {"content_block_start"}, None),
        "index": (True, int, None),
        "content_block": (True, _validate_content_block, None),
    },
    "content_block_delta": {
        "type": (True, {"content_block_delta"}, None),
        "index": (True, int, None),
        "delta": (True, _validate_delta, None),
    },
    "content_block_stop": {
        "type": (True, {"content_block_stop"}, None),
        "index": (True, int, None),
    },
}


def _validate_stream_event_inner(value: Any, path: str, errors: list[ValidationError]) -> None:
    """Validate the inner event dict of a stream_event message."""
    if not isinstance(value, dict):
        errors.append(ValidationError(path, f"expected dict, got {type(value).__name__}"))
        return
    event_type = value.get("type")
    schema = _STREAM_EVENT_SCHEMAS.get(event_type)  # type: ignore[arg-type]
    if schema is None:
        errors.append(ValidationError(
            f"{path}.type",
            f"unknown stream event type: {event_type!r}",
            event_type,
        ))
        return
    _check(value, schema, path, errors)


# ---------------------------------------------------------------------------
# Schemas for assistant message content blocks
# ---------------------------------------------------------------------------

def _validate_assistant_content(value: Any, path: str, errors: list[ValidationError]) -> None:
    """Validate the content array inside an assistant message."""
    if not isinstance(value, list):
        errors.append(ValidationError(path, f"expected list, got {type(value).__name__}"))
        return
    for i, block in enumerate(value):
        _validate_content_block(block, f"{path}[{i}]", errors)


def _validate_assistant_message_inner(value: Any, path: str, errors: list[ValidationError]) -> None:
    """Validate the message object inside an assistant message."""
    if not isinstance(value, dict):
        errors.append(ValidationError(path, f"expected dict, got {type(value).__name__}"))
        return
    _check(value, {
        "model": (True, str, None),
        "id": (True, str, None),
        "type": (True, {"message"}, None),
        "role": (True, {"assistant"}, None),
        "content": (True, _validate_assistant_content, None),
        "stop_reason": (False, ANY, None),
        "stop_sequence": (False, ANY, None),
        "usage": (False, dict, _USAGE_SCHEMA),
        "context_management": (False, ANY, None),
    }, path, errors)


# ---------------------------------------------------------------------------
# Schemas for user message content
# ---------------------------------------------------------------------------

def _validate_user_content(value: Any, path: str, errors: list[ValidationError]) -> None:
    """Validate user message content (string or list of blocks)."""
    if isinstance(value, str):
        return
    if isinstance(value, list):
        for i, block in enumerate(value):
            if not isinstance(block, dict):
                errors.append(ValidationError(f"{path}[{i}]", f"expected dict, got {type(block).__name__}"))
                continue
            block_type = block.get("type")
            if block_type == "text":
                _check(block, {"type": (True, {"text"}, None), "text": (True, str, None)}, f"{path}[{i}]", errors)
            elif block_type == "tool_use":
                _check(block, {
                    "type": (True, {"tool_use"}, None),
                    "id": (True, str, None),
                    "name": (True, str, None),
                    "input": (True, dict, None),
                }, f"{path}[{i}]", errors)
            elif block_type == "tool_result":
                _check(block, {
                    "type": (True, {"tool_result"}, None),
                    "tool_use_id": (True, str, None),
                    "content": (False, ANY, None),
                    "is_error": (False, bool, None),
                }, f"{path}[{i}]", errors)
            elif block_type is not None:
                errors.append(ValidationError(
                    f"{path}[{i}].type",
                    f"unknown content block type: {block_type!r}",
                    block_type,
                ))
        return
    errors.append(ValidationError(path, f"expected str or list, got {type(value).__name__}"))


def _validate_user_message_inner(value: Any, path: str, errors: list[ValidationError]) -> None:
    """Validate the message object inside a user message."""
    if not isinstance(value, dict):
        errors.append(ValidationError(path, f"expected dict, got {type(value).__name__}"))
        return
    _check(value, {
        "role": (True, {"user"}, None),
        "content": (True, _validate_user_content, None),
    }, path, errors)


# ---------------------------------------------------------------------------
# Control protocol schemas
# ---------------------------------------------------------------------------

_CONTROL_REQUEST_SUBTYPES = {
    "can_use_tool", "mcp_message", "initialize", "hook_callback",
    "interrupt", "set_permission_mode", "rewind_files",
}

_CONTROL_RESPONSE_SCHEMA: Schema = {
    "subtype": (True, {"success", "error"}, None),
    "request_id": (True, str, None),
    "response": (False, ANY, None),
    "error": (False, str, None),
}


def _validate_control_request_inner(value: Any, path: str, errors: list[ValidationError]) -> None:
    """Validate the request object inside a control_request."""
    if not isinstance(value, dict):
        errors.append(ValidationError(path, f"expected dict, got {type(value).__name__}"))
        return
    subtype = value.get("subtype")
    if subtype not in _CONTROL_REQUEST_SUBTYPES:
        errors.append(ValidationError(
            f"{path}.subtype",
            f"expected one of {sorted(_CONTROL_REQUEST_SUBTYPES)}, got {subtype!r}",
            subtype,
        ))


# ---------------------------------------------------------------------------
# Rate limit event schema
# ---------------------------------------------------------------------------

_RATE_LIMIT_INFO_SCHEMA: Schema = {
    "status": (True, {"allowed", "allowed_warning", "rejected"}, None),
    "resetsAt": (True, (int, float), None),
    "rateLimitType": (True, str, None),
    "utilization": (False, (int, float), None),
    "isUsingOverage": (False, bool, None),
    "surpassedThreshold": (False, (int, float), None),
}


# ---------------------------------------------------------------------------
# Top-level inbound message schemas
# ---------------------------------------------------------------------------

_INBOUND_SCHEMAS: dict[str, Schema] = {
    "stream_event": {
        "type": (True, {"stream_event"}, None),
        "uuid": (True, str, None),
        "session_id": (True, str, None),
        "parent_tool_use_id": (False, (str, type(None)), None),
        "event": (True, _validate_stream_event_inner, None),
    },
    "assistant": {
        "type": (True, {"assistant"}, None),
        "message": (True, _validate_assistant_message_inner, None),
        "parent_tool_use_id": (False, (str, type(None)), None),
        "session_id": (True, str, None),
        "uuid": (True, str, None),
        "error": (False, str, None),
    },
    "user": {
        "type": (True, {"user"}, None),
        "message": (False, _validate_user_message_inner, None),
        "content": (False, _validate_user_content, None),
        "session_id": (False, str, None),
        "parent_tool_use_id": (False, (str, type(None)), None),
        "uuid": (False, str, None),
        "tool_use_result": (False, ANY, None),
    },
    "system": {
        "type": (True, {"system"}, None),
        "subtype": (True, str, None),
        # system messages carry arbitrary extra fields depending on subtype
        # (init has tools, cwd, session_id, etc.)
        # We validate subtype exists but allow extra keys without warning
    },
    "result": {
        "type": (True, {"result"}, None),
        "subtype": (True, {"success", "error"}, None),
        "is_error": (True, bool, None),
        "duration_ms": (True, int, None),
        "duration_api_ms": (True, int, None),
        "num_turns": (True, int, None),
        "session_id": (True, str, None),
        "result": (False, (str, type(None)), None),
        "stop_reason": (False, ANY, None),
        "total_cost_usd": (False, (int, float, type(None)), None),
        "usage": (False, dict, None),
        "modelUsage": (False, dict, None),
        "permission_denials": (False, list, None),
        "fast_mode_state": (False, str, None),
        "uuid": (True, str, None),
        "structured_output": (False, ANY, None),
    },
    "control_request": {
        "type": (True, {"control_request"}, None),
        "request_id": (True, str, None),
        "request": (True, _validate_control_request_inner, None),
    },
    "control_response": {
        "type": (True, {"control_response"}, None),
        "response": (True, dict, _CONTROL_RESPONSE_SCHEMA),
    },
    "rate_limit_event": {
        "type": (True, {"rate_limit_event"}, None),
        "rate_limit_info": (True, dict, _RATE_LIMIT_INFO_SCHEMA),
        "uuid": (True, str, None),
        "session_id": (True, str, None),
    },
}

# system messages have dynamic extra fields — suppress unknown key warnings
_SUPPRESS_UNKNOWN_KEYS = {"system"}


# ---------------------------------------------------------------------------
# Outbound message schemas
# ---------------------------------------------------------------------------

_OUTBOUND_SCHEMAS: dict[str, Schema] = {
    "user": {
        "type": (True, {"user"}, None),
        "content": (False, _validate_user_content, None),
        "session_id": (False, str, None),
        "message": (False, _validate_user_message_inner, None),
        "parent_tool_use_id": (False, (str, type(None)), None),
    },
    "control_request": {
        "type": (True, {"control_request"}, None),
        "request_id": (True, str, None),
        "request": (True, _validate_control_request_inner, None),
    },
    "control_response": {
        "type": (True, {"control_response"}, None),
        "response": (True, dict, _CONTROL_RESPONSE_SCHEMA),
    },
}


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def validate_inbound(msg: dict[str, Any]) -> list[ValidationError]:
    """Validate a message received from the CLI.

    Returns a list of validation errors/warnings. Empty list means valid.
    """
    errors: list[ValidationError] = []
    msg_type = msg.get("type")

    if msg_type is None:
        errors.append(ValidationError("type", "missing required 'type' field"))
        return errors

    schema = _INBOUND_SCHEMAS.get(msg_type)
    if schema is None:
        errors.append(ValidationError("type", f"unknown inbound message type: {msg_type!r}", msg_type))
        return errors

    _check(msg, schema, "", errors)

    # Suppress unknown key warnings for message types with dynamic fields
    if msg_type in _SUPPRESS_UNKNOWN_KEYS:
        errors = [e for e in errors if e.level != "warning" or "unknown key" not in e.message]

    return errors


def validate_outbound(msg: dict[str, Any]) -> list[ValidationError]:
    """Validate a message being sent to the CLI.

    Returns a list of validation errors/warnings. Empty list means valid.
    """
    errors: list[ValidationError] = []
    msg_type = msg.get("type")

    if msg_type is None:
        errors.append(ValidationError("type", "missing required 'type' field"))
        return errors

    schema = _OUTBOUND_SCHEMAS.get(msg_type)
    if schema is None:
        errors.append(ValidationError("type", f"unknown outbound message type: {msg_type!r}", msg_type))
        return errors

    _check(msg, schema, "", errors)
    return errors


# Also expose the "raw" inner stream event validators — these are also
# emitted directly as top-level messages in some paths (procmux replay).
# content_block_delta, message_delta, etc. appear bare (without stream_event
# wrapper) in the procmux buffer replay path.

_BARE_STREAM_TYPES = set(_STREAM_EVENT_SCHEMAS.keys())


def validate_inbound_or_bare(msg: dict[str, Any]) -> list[ValidationError]:
    """Like validate_inbound, but also accepts bare Anthropic stream events.

    Some code paths (procmux replay) emit content_block_delta, message_start,
    etc. as top-level messages without the stream_event wrapper.
    """
    msg_type = msg.get("type")
    if msg_type in _BARE_STREAM_TYPES:
        errors: list[ValidationError] = []
        schema = _STREAM_EVENT_SCHEMAS[msg_type]
        _check(msg, schema, "", errors)
        return errors
    return validate_inbound(msg)
