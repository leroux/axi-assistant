"""Unit tests for claudewire.schema — strict JSON schema validation."""

from __future__ import annotations

import copy

import pytest

from claudewire.schema import (
    SchemaValidationError,
    ValidationError,
    validate_inbound,
    validate_inbound_or_bare,
    validate_outbound,
)

# ---------------------------------------------------------------------------
# Real message fixtures (from bridge-stdio-axi-master.log)
# ---------------------------------------------------------------------------

STREAM_EVENT_TEXT_DELTA = {
    "type": "stream_event",
    "event": {
        "type": "content_block_delta",
        "index": 0,
        "delta": {"type": "text_delta", "text": "Hello"},
    },
    "session_id": "f8185fa3-2e18-46f6-8ec4-e7d87dc9b966",
    "parent_tool_use_id": None,
    "uuid": "62305b13-5a88-47e6-acbb-1b826dffbf74",
}

STREAM_EVENT_INPUT_JSON_DELTA = {
    "type": "stream_event",
    "event": {
        "type": "content_block_delta",
        "index": 0,
        "delta": {"type": "input_json_delta", "partial_json": '{"command": "ls"}'},
    },
    "session_id": "abc123",
    "parent_tool_use_id": None,
    "uuid": "def456",
}

STREAM_EVENT_THINKING_DELTA = {
    "type": "stream_event",
    "event": {
        "type": "content_block_delta",
        "index": 0,
        "delta": {"type": "thinking_delta", "thinking": "Let me think..."},
    },
    "session_id": "abc123",
    "parent_tool_use_id": None,
    "uuid": "ghi789",
}

STREAM_EVENT_SIGNATURE_DELTA = {
    "type": "stream_event",
    "event": {
        "type": "content_block_delta",
        "index": 0,
        "delta": {"type": "signature_delta", "signature": "EpcCCk..."},
    },
    "session_id": "abc123",
    "parent_tool_use_id": None,
    "uuid": "sig001",
}

STREAM_EVENT_CONTENT_BLOCK_START_TEXT = {
    "type": "stream_event",
    "event": {
        "type": "content_block_start",
        "index": 0,
        "content_block": {"type": "text", "text": ""},
    },
    "session_id": "abc123",
    "parent_tool_use_id": None,
    "uuid": "cbs001",
}

STREAM_EVENT_CONTENT_BLOCK_START_TOOL_USE = {
    "type": "stream_event",
    "event": {
        "type": "content_block_start",
        "index": 0,
        "content_block": {
            "type": "tool_use",
            "id": "toolu_01GD1eVVh6L6xN2ohRjrJcX3",
            "name": "mcp__axi__axi_spawn_agent",
            "input": {},
            "caller": {"type": "direct"},
        },
    },
    "session_id": "abc123",
    "parent_tool_use_id": None,
    "uuid": "cbs002",
}

STREAM_EVENT_CONTENT_BLOCK_START_THINKING = {
    "type": "stream_event",
    "event": {
        "type": "content_block_start",
        "index": 0,
        "content_block": {"type": "thinking", "thinking": "", "signature": ""},
    },
    "session_id": "abc123",
    "parent_tool_use_id": None,
    "uuid": "cbs003",
}

STREAM_EVENT_CONTENT_BLOCK_STOP = {
    "type": "stream_event",
    "event": {"type": "content_block_stop", "index": 0},
    "session_id": "abc123",
    "parent_tool_use_id": None,
    "uuid": "cbs004",
}

STREAM_EVENT_MESSAGE_START = {
    "type": "stream_event",
    "event": {
        "type": "message_start",
        "message": {
            "model": "claude-opus-4-6",
            "id": "msg_01D9gA2ENvH5FBKXNYkdU68v",
            "type": "message",
            "role": "assistant",
            "content": [],
            "stop_reason": None,
            "stop_sequence": None,
            "usage": {
                "input_tokens": 1,
                "cache_creation_input_tokens": 920,
                "cache_read_input_tokens": 113835,
                "cache_creation": {"ephemeral_5m_input_tokens": 0, "ephemeral_1h_input_tokens": 920},
                "output_tokens": 8,
                "service_tier": "standard",
                "inference_geo": "not_available",
            },
        },
    },
    "session_id": "abc123",
    "parent_tool_use_id": None,
    "uuid": "ms001",
}

STREAM_EVENT_MESSAGE_DELTA = {
    "type": "stream_event",
    "event": {
        "type": "message_delta",
        "delta": {"stop_reason": "tool_use", "stop_sequence": None},
        "usage": {
            "input_tokens": 3,
            "cache_creation_input_tokens": 97760,
            "cache_read_input_tokens": 16075,
            "output_tokens": 861,
        },
        "context_management": {"applied_edits": []},
    },
    "session_id": "abc123",
    "parent_tool_use_id": None,
    "uuid": "md001",
}

STREAM_EVENT_MESSAGE_STOP = {
    "type": "stream_event",
    "event": {"type": "message_stop"},
    "session_id": "abc123",
    "parent_tool_use_id": None,
    "uuid": "mstop001",
}

ASSISTANT_MESSAGE = {
    "type": "assistant",
    "message": {
        "model": "claude-opus-4-6",
        "id": "msg_01S5FvQjykmgkFGXVvo7dGnt",
        "type": "message",
        "role": "assistant",
        "content": [
            {
                "type": "tool_use",
                "id": "toolu_01JRZMoGStk2TJgF4mWczUGH",
                "name": "mcp__axi__axi_spawn_agent",
                "input": {"name": "test"},
            },
        ],
        "stop_reason": None,
        "stop_sequence": None,
        "usage": {"input_tokens": 3, "output_tokens": 30},
    },
    "parent_tool_use_id": None,
    "session_id": "abc123",
    "uuid": "am001",
}

ASSISTANT_MESSAGE_TEXT = {
    "type": "assistant",
    "message": {
        "model": "claude-opus-4-6",
        "id": "msg_01D9gA2ENvH5FBKXNYkdU68v",
        "type": "message",
        "role": "assistant",
        "content": [{"type": "text", "text": "Done."}],
        "stop_reason": None,
        "stop_sequence": None,
        "usage": {"input_tokens": 1, "output_tokens": 8},
    },
    "parent_tool_use_id": None,
    "session_id": "abc123",
    "uuid": "am002",
}

USER_MESSAGE_SIMPLE = {
    "type": "user",
    "content": "Hello world",
}

USER_MESSAGE_FULL = {
    "type": "user",
    "session_id": "",
    "message": {"role": "user", "content": "Hello world"},
    "parent_tool_use_id": None,
}

SYSTEM_MESSAGE_INIT = {
    "type": "system",
    "subtype": "init",
    "cwd": "/home/ubuntu/axi-assistant",
    "session_id": "abc123",
    "tools": ["Bash", "Read", "Write"],
    "model": "claude-opus-4-6",
    "uuid": "sys001",
}

RESULT_MESSAGE = {
    "type": "result",
    "subtype": "success",
    "is_error": False,
    "duration_ms": 57095,
    "duration_api_ms": 31197,
    "num_turns": 2,
    "result": "Done.",
    "stop_reason": None,
    "session_id": "abc123",
    "total_cost_usd": 0.707125,
    "usage": {"input_tokens": 4, "output_tokens": 1016},
    "modelUsage": {},
    "permission_denials": [],
    "fast_mode_state": "off",
    "uuid": "res001",
}

CONTROL_REQUEST_PERMISSION = {
    "type": "control_request",
    "request_id": "9965550a-2068-4ad0-b129-46aa10dfabbe",
    "request": {
        "subtype": "can_use_tool",
        "tool_name": "Bash",
        "input": {"command": "ls"},
        "permission_suggestions": [],
        "tool_use_id": "toolu_01JRZMoGStk2TJgF4mWczUGH",
    },
}

CONTROL_REQUEST_MCP = {
    "type": "control_request",
    "request_id": "07044b58",
    "request": {
        "subtype": "mcp_message",
        "server_name": "axi",
        "message": {"method": "tools/call", "jsonrpc": "2.0", "id": 2},
    },
}

CONTROL_RESPONSE_SUCCESS = {
    "type": "control_response",
    "response": {
        "subtype": "success",
        "request_id": "abc123",
        "response": {"behavior": "allow"},
    },
}

RATE_LIMIT_EVENT = {
    "type": "rate_limit_event",
    "rate_limit_info": {
        "status": "allowed_warning",
        "resetsAt": 1772456400,
        "rateLimitType": "five_hour",
        "utilization": 0.9,
        "isUsingOverage": False,
        "surpassedThreshold": 0.9,
    },
    "uuid": "48dc656e",
    "session_id": "abc123",
}


# ---------------------------------------------------------------------------
# Tests: valid inbound messages
# ---------------------------------------------------------------------------


class TestValidInbound:
    """All valid inbound message types should produce no errors."""

    @pytest.mark.parametrize(
        "msg",
        [
            STREAM_EVENT_TEXT_DELTA,
            STREAM_EVENT_INPUT_JSON_DELTA,
            STREAM_EVENT_THINKING_DELTA,
            STREAM_EVENT_SIGNATURE_DELTA,
            STREAM_EVENT_CONTENT_BLOCK_START_TEXT,
            STREAM_EVENT_CONTENT_BLOCK_START_TOOL_USE,
            STREAM_EVENT_CONTENT_BLOCK_START_THINKING,
            STREAM_EVENT_CONTENT_BLOCK_STOP,
            STREAM_EVENT_MESSAGE_START,
            STREAM_EVENT_MESSAGE_DELTA,
            STREAM_EVENT_MESSAGE_STOP,
            ASSISTANT_MESSAGE,
            ASSISTANT_MESSAGE_TEXT,
            USER_MESSAGE_SIMPLE,
            USER_MESSAGE_FULL,
            SYSTEM_MESSAGE_INIT,
            RESULT_MESSAGE,
            CONTROL_REQUEST_PERMISSION,
            CONTROL_REQUEST_MCP,
            CONTROL_RESPONSE_SUCCESS,
            RATE_LIMIT_EVENT,
        ],
        ids=[
            "stream_event_text_delta",
            "stream_event_input_json_delta",
            "stream_event_thinking_delta",
            "stream_event_signature_delta",
            "content_block_start_text",
            "content_block_start_tool_use",
            "content_block_start_thinking",
            "content_block_stop",
            "message_start",
            "message_delta",
            "message_stop",
            "assistant",
            "assistant_text",
            "user_simple",
            "user_full",
            "system_init",
            "result",
            "control_request_permission",
            "control_request_mcp",
            "control_response",
            "rate_limit",
        ],
    )
    def test_valid_message(self, msg: dict) -> None:
        errors = validate_inbound(msg)
        assert errors == [], f"Unexpected errors: {errors}"


class TestValidOutbound:
    """All valid outbound message types should produce no errors."""

    @pytest.mark.parametrize(
        "msg",
        [
            USER_MESSAGE_SIMPLE,
            USER_MESSAGE_FULL,
            CONTROL_RESPONSE_SUCCESS,
            {
                "type": "control_request",
                "request_id": "req_1",
                "request": {"subtype": "initialize", "hooks": None},
            },
        ],
        ids=["user_simple", "user_full", "control_response", "control_request_init"],
    )
    def test_valid_message(self, msg: dict) -> None:
        errors = validate_outbound(msg)
        assert errors == [], f"Unexpected errors: {errors}"


# ---------------------------------------------------------------------------
# Tests: missing required keys
# ---------------------------------------------------------------------------


class TestMissingRequired:
    def test_missing_type(self) -> None:
        errors = validate_inbound({"foo": "bar"})
        assert len(errors) == 1
        assert "type" in errors[0].path
        assert "missing" in errors[0].message

    def test_stream_event_missing_uuid(self) -> None:
        msg = copy.deepcopy(STREAM_EVENT_TEXT_DELTA)
        del msg["uuid"]
        errors = validate_inbound(msg)
        assert any(e.path == "uuid" and e.level == "error" for e in errors)

    def test_stream_event_missing_session_id(self) -> None:
        msg = copy.deepcopy(STREAM_EVENT_TEXT_DELTA)
        del msg["session_id"]
        errors = validate_inbound(msg)
        assert any(e.path == "session_id" and e.level == "error" for e in errors)

    def test_stream_event_missing_event(self) -> None:
        msg = copy.deepcopy(STREAM_EVENT_TEXT_DELTA)
        del msg["event"]
        errors = validate_inbound(msg)
        assert any(e.path == "event" and e.level == "error" for e in errors)

    def test_result_missing_duration(self) -> None:
        msg = copy.deepcopy(RESULT_MESSAGE)
        del msg["duration_ms"]
        errors = validate_inbound(msg)
        assert any(e.path == "duration_ms" for e in errors)

    def test_assistant_missing_message(self) -> None:
        msg = copy.deepcopy(ASSISTANT_MESSAGE)
        del msg["message"]
        errors = validate_inbound(msg)
        assert any(e.path == "message" for e in errors)

    def test_control_request_missing_request_id(self) -> None:
        msg = {"type": "control_request", "request": {"subtype": "initialize"}}
        errors = validate_inbound(msg)
        assert any(e.path == "request_id" for e in errors)

    def test_rate_limit_missing_info(self) -> None:
        msg = {"type": "rate_limit_event", "uuid": "x", "session_id": "y"}
        errors = validate_inbound(msg)
        assert any(e.path == "rate_limit_info" for e in errors)


# ---------------------------------------------------------------------------
# Tests: unknown keys (warnings)
# ---------------------------------------------------------------------------


class TestUnknownKeys:
    def test_unknown_top_level_key(self) -> None:
        msg = copy.deepcopy(STREAM_EVENT_TEXT_DELTA)
        msg["new_field"] = "surprise"
        errors = validate_inbound(msg)
        warnings = [e for e in errors if e.level == "warning"]
        assert any("new_field" in e.path for e in warnings)

    def test_unknown_key_in_event(self) -> None:
        msg = copy.deepcopy(STREAM_EVENT_TEXT_DELTA)
        msg["event"]["new_thing"] = 42
        errors = validate_inbound(msg)
        warnings = [e for e in errors if e.level == "warning"]
        assert any("new_thing" in e.path for e in warnings)

    def test_system_message_allows_extra_keys(self) -> None:
        """System messages have dynamic fields — no unknown key warnings."""
        msg = copy.deepcopy(SYSTEM_MESSAGE_INIT)
        msg["brand_new_field"] = "allowed"
        errors = validate_inbound(msg)
        # Should have no warnings about brand_new_field
        assert not any("brand_new_field" in e.path for e in errors)

    def test_trace_context_always_allowed(self) -> None:
        msg = copy.deepcopy(STREAM_EVENT_TEXT_DELTA)
        msg["_trace_context"] = {"traceparent": "00-abc-def-01"}
        errors = validate_inbound(msg)
        assert not any("_trace_context" in e.path for e in errors)


# ---------------------------------------------------------------------------
# Tests: wrong type discriminators
# ---------------------------------------------------------------------------


class TestWrongDiscriminators:
    def test_unknown_message_type(self) -> None:
        errors = validate_inbound({"type": "spaceship"})
        assert len(errors) == 1
        assert "unknown inbound message type" in errors[0].message

    def test_unknown_stream_event_type(self) -> None:
        msg = copy.deepcopy(STREAM_EVENT_TEXT_DELTA)
        msg["event"]["type"] = "quantum_event"
        errors = validate_inbound(msg)
        assert any("unknown stream event type" in e.message for e in errors)

    def test_unknown_delta_type(self) -> None:
        msg = copy.deepcopy(STREAM_EVENT_TEXT_DELTA)
        msg["event"]["delta"]["type"] = "alien_delta"
        errors = validate_inbound(msg)
        assert any("expected one of" in e.message for e in errors)

    def test_unknown_content_block_type(self) -> None:
        msg = copy.deepcopy(STREAM_EVENT_CONTENT_BLOCK_START_TEXT)
        msg["event"]["content_block"]["type"] = "video"
        errors = validate_inbound(msg)
        assert any("expected one of" in e.message for e in errors)

    def test_wrong_result_subtype(self) -> None:
        msg = copy.deepcopy(RESULT_MESSAGE)
        msg["subtype"] = "maybe"
        errors = validate_inbound(msg)
        assert any("expected one of" in e.message and "subtype" in e.path for e in errors)

    def test_unknown_control_request_subtype(self) -> None:
        msg = {
            "type": "control_request",
            "request_id": "x",
            "request": {"subtype": "self_destruct"},
        }
        errors = validate_inbound(msg)
        assert any("expected one of" in e.message for e in errors)


# ---------------------------------------------------------------------------
# Tests: type mismatches
# ---------------------------------------------------------------------------


class TestTypeMismatches:
    def test_index_not_int(self) -> None:
        msg = copy.deepcopy(STREAM_EVENT_TEXT_DELTA)
        msg["event"]["index"] = "zero"
        errors = validate_inbound(msg)
        assert any("expected int" in e.message for e in errors)

    def test_is_error_not_bool(self) -> None:
        msg = copy.deepcopy(RESULT_MESSAGE)
        msg["is_error"] = "false"
        errors = validate_inbound(msg)
        assert any("expected bool" in e.message for e in errors)


# ---------------------------------------------------------------------------
# Tests: bare stream events (procmux replay)
# ---------------------------------------------------------------------------


class TestBareStreamEvents:
    def test_bare_content_block_delta(self) -> None:
        bare = {
            "type": "content_block_delta",
            "index": 0,
            "delta": {"type": "text_delta", "text": "Hi"},
        }
        errors = validate_inbound_or_bare(bare)
        assert errors == []

    def test_bare_message_stop(self) -> None:
        bare = {"type": "message_stop"}
        errors = validate_inbound_or_bare(bare)
        assert errors == []

    def test_bare_unknown_type_falls_through(self) -> None:
        """Unknown bare types should fall through to validate_inbound."""
        bare = {"type": "spaceship"}
        errors = validate_inbound_or_bare(bare)
        assert any("unknown" in e.message for e in errors)


# ---------------------------------------------------------------------------
# Tests: outbound validation
# ---------------------------------------------------------------------------


class TestOutbound:
    def test_unknown_outbound_type(self) -> None:
        errors = validate_outbound({"type": "magic"})
        assert any("unknown outbound message type" in e.message for e in errors)

    def test_control_response_missing_subtype(self) -> None:
        msg = {
            "type": "control_response",
            "response": {"request_id": "x"},
        }
        errors = validate_outbound(msg)
        assert any("subtype" in e.path for e in errors)


# ---------------------------------------------------------------------------
# Tests: ValidationError formatting
# ---------------------------------------------------------------------------


class TestValidationErrorFormatting:
    def test_str_with_value(self) -> None:
        e = ValidationError("foo.bar", "bad value", raw_value=42)
        assert "foo.bar" in str(e)
        assert "42" in str(e)

    def test_str_without_value(self) -> None:
        e = ValidationError("foo", "missing")
        assert "foo" in str(e)
        assert "missing" in str(e)

    def test_schema_validation_error(self) -> None:
        errors = [ValidationError("x", "bad")]
        exc = SchemaValidationError(errors, {"type": "test"})
        assert "Schema validation failed" in str(exc)
        assert exc.errors == errors
        assert exc.raw == {"type": "test"}


# ---------------------------------------------------------------------------
# Tests: nested content block validation
# ---------------------------------------------------------------------------


class TestNestedContentBlocks:
    def test_assistant_with_thinking_block(self) -> None:
        msg = copy.deepcopy(ASSISTANT_MESSAGE)
        msg["message"]["content"] = [
            {"type": "thinking", "thinking": "hmm", "signature": "sig123"},
            {"type": "text", "text": "The answer is 42."},
        ]
        errors = validate_inbound(msg)
        assert errors == []

    def test_assistant_with_unknown_block_type(self) -> None:
        msg = copy.deepcopy(ASSISTANT_MESSAGE)
        msg["message"]["content"] = [{"type": "image", "url": "http://..."}]
        errors = validate_inbound(msg)
        assert any("expected one of" in e.message for e in errors)

    def test_user_message_with_tool_result(self) -> None:
        msg = {
            "type": "user",
            "message": {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "toolu_abc",
                        "content": "output text",
                    }
                ],
            },
            "session_id": "s1",
            "parent_tool_use_id": "toolu_parent",
        }
        errors = validate_inbound(msg)
        assert errors == []
