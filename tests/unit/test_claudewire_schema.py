"""Unit tests for claudewire.schema — pydantic-based protocol validation."""

from __future__ import annotations

import copy

import pytest

from claudewire.schema import (
    AssistantMsg,
    ContentBlockDeltaEvent,
    ContentBlockStartEvent,
    ControlRequestMsg,
    MessageStartEvent,
    RateLimitEventMsg,
    ResultMsg,
    SchemaValidationError,
    StreamEventMsg,
    SystemMsg,
    ValidationError,
    ValidationResult,
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
# Tests: valid inbound messages → returns parsed model
# ---------------------------------------------------------------------------


class TestValidInbound:
    """All valid inbound message types should produce no errors and a model."""

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
        result = validate_inbound(msg)
        assert result.errors == [], f"Unexpected errors: {result.errors}"
        assert result.model is not None
        assert result.ok


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
        result = validate_outbound(msg)
        assert result.errors == [], f"Unexpected errors: {result.errors}"
        assert result.model is not None


# ---------------------------------------------------------------------------
# Tests: parsed model types
# ---------------------------------------------------------------------------


class TestParsedModels:
    """Validation returns typed pydantic models."""

    def test_stream_event_model_type(self) -> None:
        result = validate_inbound(STREAM_EVENT_TEXT_DELTA)
        assert isinstance(result.model, StreamEventMsg)
        assert isinstance(result.model.event, ContentBlockDeltaEvent)

    def test_assistant_model_type(self) -> None:
        result = validate_inbound(ASSISTANT_MESSAGE)
        assert isinstance(result.model, AssistantMsg)
        assert result.model.message.model == "claude-opus-4-6"

    def test_result_model_type(self) -> None:
        result = validate_inbound(RESULT_MESSAGE)
        assert isinstance(result.model, ResultMsg)
        assert result.model.duration_ms == 57095
        assert result.model.total_cost_usd == 0.707125

    def test_rate_limit_model_type(self) -> None:
        result = validate_inbound(RATE_LIMIT_EVENT)
        assert isinstance(result.model, RateLimitEventMsg)
        assert result.model.rate_limit_info.status == "allowed_warning"
        assert result.model.rate_limit_info.utilization == 0.9

    def test_system_model_type(self) -> None:
        result = validate_inbound(SYSTEM_MESSAGE_INIT)
        assert isinstance(result.model, SystemMsg)
        assert result.model.subtype == "init"

    def test_control_request_model_type(self) -> None:
        result = validate_inbound(CONTROL_REQUEST_PERMISSION)
        assert isinstance(result.model, ControlRequestMsg)
        assert result.model.request.subtype == "can_use_tool"

    def test_content_block_start_access(self) -> None:
        result = validate_inbound(STREAM_EVENT_CONTENT_BLOCK_START_TOOL_USE)
        assert isinstance(result.model, StreamEventMsg)
        event = result.model.event
        assert isinstance(event, ContentBlockStartEvent)
        assert event.content_block.name == "mcp__axi__axi_spawn_agent"  # type: ignore[union-attr]

    def test_message_start_access(self) -> None:
        result = validate_inbound(STREAM_EVENT_MESSAGE_START)
        assert isinstance(result.model, StreamEventMsg)
        event = result.model.event
        assert isinstance(event, MessageStartEvent)
        assert event.message.model == "claude-opus-4-6"
        assert event.message.usage is not None
        assert event.message.usage.input_tokens == 1


# ---------------------------------------------------------------------------
# Tests: missing required keys
# ---------------------------------------------------------------------------


class TestMissingRequired:
    def test_missing_type(self) -> None:
        result = validate_inbound({"foo": "bar"})
        assert len(result.errors) == 1
        assert "type" in result.errors[0].path

    def test_stream_event_missing_uuid(self) -> None:
        msg = copy.deepcopy(STREAM_EVENT_TEXT_DELTA)
        del msg["uuid"]
        result = validate_inbound(msg)
        assert any(e.level == "error" for e in result.errors)
        assert not result.ok

    def test_stream_event_missing_session_id(self) -> None:
        msg = copy.deepcopy(STREAM_EVENT_TEXT_DELTA)
        del msg["session_id"]
        result = validate_inbound(msg)
        assert any(e.level == "error" for e in result.errors)

    def test_stream_event_missing_event(self) -> None:
        msg = copy.deepcopy(STREAM_EVENT_TEXT_DELTA)
        del msg["event"]
        result = validate_inbound(msg)
        assert any(e.level == "error" for e in result.errors)

    def test_result_missing_duration(self) -> None:
        msg = copy.deepcopy(RESULT_MESSAGE)
        del msg["duration_ms"]
        result = validate_inbound(msg)
        assert any(e.level == "error" for e in result.errors)

    def test_assistant_missing_message(self) -> None:
        msg = copy.deepcopy(ASSISTANT_MESSAGE)
        del msg["message"]
        result = validate_inbound(msg)
        assert any(e.level == "error" for e in result.errors)

    def test_control_request_missing_request_id(self) -> None:
        msg = {"type": "control_request", "request": {"subtype": "initialize"}}
        result = validate_inbound(msg)
        assert any(e.level == "error" for e in result.errors)

    def test_rate_limit_missing_info(self) -> None:
        msg = {"type": "rate_limit_event", "uuid": "x", "session_id": "y"}
        result = validate_inbound(msg)
        assert any(e.level == "error" for e in result.errors)


# ---------------------------------------------------------------------------
# Tests: unknown keys (warnings)
# ---------------------------------------------------------------------------


class TestUnknownKeys:
    def test_unknown_top_level_key(self) -> None:
        msg = copy.deepcopy(STREAM_EVENT_TEXT_DELTA)
        msg["new_field"] = "surprise"
        result = validate_inbound(msg)
        warnings = [e for e in result.errors if e.level == "warning"]
        assert any("new_field" in e.path for e in warnings)

    def test_unknown_key_in_event(self) -> None:
        msg = copy.deepcopy(STREAM_EVENT_TEXT_DELTA)
        msg["event"]["new_thing"] = 42
        result = validate_inbound(msg)
        warnings = [e for e in result.errors if e.level == "warning"]
        assert any("new_thing" in e.path for e in warnings)

    def test_system_message_allows_extra_keys(self) -> None:
        """System messages have dynamic fields — no unknown key warnings."""
        msg = copy.deepcopy(SYSTEM_MESSAGE_INIT)
        msg["brand_new_field"] = "allowed"
        result = validate_inbound(msg)
        assert not any("brand_new_field" in e.path for e in result.errors)

    def test_trace_context_always_allowed(self) -> None:
        msg = copy.deepcopy(STREAM_EVENT_TEXT_DELTA)
        msg["_trace_context"] = {"traceparent": "00-abc-def-01"}
        result = validate_inbound(msg)
        assert not any("_trace_context" in e.path for e in result.errors)

    def test_user_allows_isSynthetic(self) -> None:
        """User messages allow isSynthetic (seen in real logs)."""
        msg = copy.deepcopy(USER_MESSAGE_SIMPLE)
        msg["isSynthetic"] = True
        result = validate_inbound(msg)
        assert not any("isSynthetic" in e.path for e in result.errors)


# ---------------------------------------------------------------------------
# Tests: wrong type discriminators
# ---------------------------------------------------------------------------


class TestWrongDiscriminators:
    def test_unknown_message_type(self) -> None:
        result = validate_inbound({"type": "spaceship"})
        assert len(result.errors) >= 1
        assert any(e.level == "error" for e in result.errors)

    def test_unknown_stream_event_type(self) -> None:
        msg = copy.deepcopy(STREAM_EVENT_TEXT_DELTA)
        msg["event"]["type"] = "quantum_event"
        result = validate_inbound(msg)
        assert any(e.level == "error" for e in result.errors)

    def test_unknown_delta_type(self) -> None:
        msg = copy.deepcopy(STREAM_EVENT_TEXT_DELTA)
        msg["event"]["delta"]["type"] = "alien_delta"
        result = validate_inbound(msg)
        assert any(e.level == "error" for e in result.errors)

    def test_unknown_content_block_type(self) -> None:
        msg = copy.deepcopy(STREAM_EVENT_CONTENT_BLOCK_START_TEXT)
        msg["event"]["content_block"]["type"] = "video"
        result = validate_inbound(msg)
        assert any(e.level == "error" for e in result.errors)

    def test_wrong_result_subtype(self) -> None:
        msg = copy.deepcopy(RESULT_MESSAGE)
        msg["subtype"] = "maybe"
        result = validate_inbound(msg)
        assert any(e.level == "error" for e in result.errors)

    def test_unknown_control_request_subtype(self) -> None:
        """Control request subtypes are open-ended — any string is allowed."""
        msg = {
            "type": "control_request",
            "request_id": "x",
            "request": {"subtype": "self_destruct"},
        }
        result = validate_inbound(msg)
        # Should pass — subtype is just str now
        assert result.ok


# ---------------------------------------------------------------------------
# Tests: type mismatches
# ---------------------------------------------------------------------------


class TestTypeMismatches:
    def test_index_not_int(self) -> None:
        msg = copy.deepcopy(STREAM_EVENT_TEXT_DELTA)
        msg["event"]["index"] = "zero"
        result = validate_inbound(msg)
        assert any(e.level == "error" for e in result.errors)

    def test_is_error_not_bool(self) -> None:
        msg = copy.deepcopy(RESULT_MESSAGE)
        msg["is_error"] = [1, 2, 3]  # list is not coercible to bool
        result = validate_inbound(msg)
        assert any(e.level == "error" for e in result.errors)


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
        result = validate_inbound_or_bare(bare)
        assert result.errors == []
        assert isinstance(result.model, ContentBlockDeltaEvent)

    def test_bare_message_stop(self) -> None:
        bare = {"type": "message_stop"}
        result = validate_inbound_or_bare(bare)
        assert result.errors == []

    def test_bare_unknown_type_falls_through(self) -> None:
        """Unknown bare types should fall through to validate_inbound."""
        bare = {"type": "spaceship"}
        result = validate_inbound_or_bare(bare)
        assert any(e.level == "error" for e in result.errors)


# ---------------------------------------------------------------------------
# Tests: outbound validation
# ---------------------------------------------------------------------------


class TestOutbound:
    def test_unknown_outbound_type(self) -> None:
        result = validate_outbound({"type": "magic"})
        assert any(e.level == "error" for e in result.errors)

    def test_control_response_missing_subtype(self) -> None:
        msg = {
            "type": "control_response",
            "response": {"request_id": "x"},
        }
        result = validate_outbound(msg)
        assert any(e.level == "error" for e in result.errors)


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

    def test_validation_result_ok(self) -> None:
        result = ValidationResult(model=None, errors=[])
        assert result.ok

    def test_validation_result_warnings_only_is_ok(self) -> None:
        result = ValidationResult(
            model=None,
            errors=[ValidationError("x", "unknown key", level="warning")],
        )
        assert result.ok

    def test_validation_result_errors_not_ok(self) -> None:
        result = ValidationResult(
            model=None,
            errors=[ValidationError("x", "missing field", level="error")],
        )
        assert not result.ok


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
        result = validate_inbound(msg)
        assert result.errors == []

    def test_assistant_with_unknown_block_type(self) -> None:
        msg = copy.deepcopy(ASSISTANT_MESSAGE)
        msg["message"]["content"] = [{"type": "image", "url": "http://..."}]
        result = validate_inbound(msg)
        assert any(e.level == "error" for e in result.errors)

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
        result = validate_inbound(msg)
        assert result.errors == []


# ---------------------------------------------------------------------------
# Tests: real-world edge cases from logs
# ---------------------------------------------------------------------------


class TestRealWorldEdgeCases:
    def test_result_with_rich_usage(self) -> None:
        """Result messages have extended usage fields (iterations, server_tool_use, speed)."""
        msg = copy.deepcopy(RESULT_MESSAGE)
        msg["usage"] = {
            "input_tokens": 4,
            "output_tokens": 1016,
            "cache_creation_input_tokens": 920,
            "cache_read_input_tokens": 113835,
            "cache_creation": {"ephemeral_5m_input_tokens": 0, "ephemeral_1h_input_tokens": 920},
            "service_tier": "standard",
            "inference_geo": "not_available",
            "iterations": [{"input_tokens": 100, "output_tokens": 200}],
            "server_tool_use": {"web_search_requests": 1, "web_fetch_requests": 0},
            "speed": "fast",
        }
        result = validate_inbound(msg)
        assert result.ok
        assert isinstance(result.model, ResultMsg)
        assert result.model.usage is not None
        assert result.model.usage.speed == "fast"

    def test_rate_limit_with_overage(self) -> None:
        """Rate limit events can include overage fields."""
        msg = {
            "type": "rate_limit_event",
            "rate_limit_info": {
                "status": "rejected",
                "resetsAt": 1772766000,
                "rateLimitType": "five_hour",
                "overageStatus": "rejected",
                "overageDisabledReason": "org_level_disabled",
                "isUsingOverage": False,
            },
            "uuid": "d6797ad7",
            "session_id": "abc123",
        }
        result = validate_inbound(msg)
        assert result.ok
        assert isinstance(result.model, RateLimitEventMsg)
        assert result.model.rate_limit_info.overageStatus == "rejected"

    def test_user_message_with_isSynthetic(self) -> None:
        msg = {
            "type": "user",
            "message": {"role": "user", "content": "test"},
            "session_id": "s1",
            "isSynthetic": True,
        }
        result = validate_inbound(msg)
        assert result.ok

    def test_result_with_model_usage(self) -> None:
        """modelUsage contains per-model breakdowns."""
        msg = copy.deepcopy(RESULT_MESSAGE)
        msg["modelUsage"] = {
            "claude-opus-4-6": {
                "inputTokens": 100,
                "outputTokens": 200,
                "cacheCreationInputTokens": 50,
                "cacheReadInputTokens": 300,
                "contextWindow": 200000,
                "maxOutputTokens": 16384,
                "costUSD": 0.05,
                "webSearchRequests": 0,
            }
        }
        result = validate_inbound(msg)
        assert result.ok
