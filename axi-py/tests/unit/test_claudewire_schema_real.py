"""Validation tests using real bridge-stdio log samples.

Every fixture in this file is a real message captured from production bridge-stdio logs,
minimally sanitized (long strings truncated, UUIDs preserved). Add new samples here as
you encounter new message shapes in production.

To add a new sample:
1. Copy the raw JSON from bridge-stdio logs
2. Truncate large text/signature fields for readability
3. Add to the appropriate REAL_* dict
4. Add a test that validates it and checks the parsed model type
"""

from __future__ import annotations

from claudewire.schema import (
    AssistantMsg,
    ContentBlockDeltaEvent,
    ContentBlockStartEvent,
    ContentBlockStopEvent,
    ControlRequestMsg,
    ControlResponseMsg,
    MessageDeltaEvent,
    MessageStartEvent,
    MessageStopEvent,
    RateLimitEventMsg,
    ResultMsg,
    StreamEventMsg,
    SystemMsg,
    UserMsg,
    validate_inbound,
    validate_inbound_or_bare,
    validate_outbound,
)

# ---------------------------------------------------------------------------
# Real samples — stream events
# ---------------------------------------------------------------------------

REAL_STREAM_EVENT_MESSAGE_START = {
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
                "cache_creation": {
                    "ephemeral_5m_input_tokens": 0,
                    "ephemeral_1h_input_tokens": 920,
                },
                "output_tokens": 8,
                "service_tier": "standard",
                "inference_geo": "not_available",
            },
        },
    },
    "session_id": "f8185fa3-2e18-46f6-8ec4-e7d87dc9b966",
    "parent_tool_use_id": None,
    "uuid": "f31c513c-9e95-4c1a-b7a0-09e2c5e03051",
}

REAL_STREAM_EVENT_MESSAGE_DELTA = {
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
    "session_id": "f8185fa3-2e18-46f6-8ec4-e7d87dc9b966",
    "parent_tool_use_id": None,
    "uuid": "8730fe04-4087-46c7-a023-759107bf30af",
}

REAL_STREAM_EVENT_MESSAGE_STOP = {
    "type": "stream_event",
    "event": {"type": "message_stop"},
    "session_id": "f8185fa3-2e18-46f6-8ec4-e7d87dc9b966",
    "parent_tool_use_id": None,
    "uuid": "16284fa5-91e0-4e2d-b45a-ef6cce446ac7",
}

REAL_STREAM_EVENT_CB_START_TEXT = {
    "type": "stream_event",
    "event": {
        "type": "content_block_start",
        "index": 0,
        "content_block": {"type": "text", "text": ""},
    },
    "session_id": "f8185fa3-2e18-46f6-8ec4-e7d87dc9b966",
    "parent_tool_use_id": None,
    "uuid": "514dd934-64b1-4b15-8425-c790741ce342",
}

REAL_STREAM_EVENT_CB_START_TOOL_USE = {
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
    "session_id": "f8185fa3-2e18-46f6-8ec4-e7d87dc9b966",
    "parent_tool_use_id": None,
    "uuid": "cbc899bd-5222-44aa-a00b-6ea890853549",
}

REAL_STREAM_EVENT_CB_START_THINKING = {
    "type": "stream_event",
    "event": {
        "type": "content_block_start",
        "index": 0,
        "content_block": {"type": "thinking", "thinking": "", "signature": ""},
    },
    "session_id": "f8185fa3-2e18-46f6-8ec4-e7d87dc9b966",
    "parent_tool_use_id": None,
    "uuid": "687ffbc0-97dd-4edd-9512-e6f511ea4289",
}

REAL_STREAM_EVENT_CB_DELTA_TEXT = {
    "type": "stream_event",
    "event": {
        "type": "content_block_delta",
        "index": 0,
        "delta": {"type": "text_delta", "text": "Spawned **iran-news**"},
    },
    "session_id": "f8185fa3-2e18-46f6-8ec4-e7d87dc9b966",
    "parent_tool_use_id": None,
    "uuid": "1d820eb8-9a8e-4b8e-97cc-13e42abb0e6a",
}

REAL_STREAM_EVENT_CB_DELTA_INPUT_JSON = {
    "type": "stream_event",
    "event": {
        "type": "content_block_delta",
        "index": 0,
        "delta": {"type": "input_json_delta", "partial_json": "al lean on I"},
    },
    "session_id": "f8185fa3-2e18-46f6-8ec4-e7d87dc9b966",
    "parent_tool_use_id": None,
    "uuid": "62305b13-5a88-47e6-acbb-1b826dffbf74",
}

REAL_STREAM_EVENT_CB_DELTA_THINKING = {
    "type": "stream_event",
    "event": {
        "type": "content_block_delta",
        "index": 0,
        "delta": {"type": "thinking_delta", "thinking": "The"},
    },
    "session_id": "f8185fa3-2e18-46f6-8ec4-e7d87dc9b966",
    "parent_tool_use_id": None,
    "uuid": "8fbc0c70-610b-4f9b-8aef-195a5b71e1e0",
}

REAL_STREAM_EVENT_CB_DELTA_SIGNATURE = {
    "type": "stream_event",
    "event": {
        "type": "content_block_delta",
        "index": 0,
        "delta": {
            "type": "signature_delta",
            "signature": "EpcCCkYICxgCKkDh+dgfSCFrItiBkzHsh6mbS1O6tB7g",
        },
    },
    "session_id": "f8185fa3-2e18-46f6-8ec4-e7d87dc9b966",
    "parent_tool_use_id": None,
    "uuid": "e6e6d181-be1f-41d1-946a-95a9804ea748",
}

REAL_STREAM_EVENT_CB_STOP = {
    "type": "stream_event",
    "event": {"type": "content_block_stop", "index": 0},
    "session_id": "f8185fa3-2e18-46f6-8ec4-e7d87dc9b966",
    "parent_tool_use_id": None,
    "uuid": "fc146c4e-2447-47fe-b694-578446968bfe",
}


# ---------------------------------------------------------------------------
# Real samples — assistant messages
# ---------------------------------------------------------------------------

REAL_ASSISTANT_TEXT = {
    "type": "assistant",
    "message": {
        "model": "claude-opus-4-6",
        "id": "msg_01D9gA2ENvH5FBKXNYkdU68v",
        "type": "message",
        "role": "assistant",
        "content": [
            {
                "type": "text",
                "text": "Spawned **iran-news** agent. It'll build a news fetcher.",
            }
        ],
        "stop_reason": None,
        "stop_sequence": None,
        "usage": {
            "input_tokens": 1,
            "cache_creation_input_tokens": 920,
            "cache_read_input_tokens": 113835,
            "cache_creation": {
                "ephemeral_5m_input_tokens": 0,
                "ephemeral_1h_input_tokens": 920,
            },
            "output_tokens": 8,
            "service_tier": "standard",
            "inference_geo": "not_available",
        },
        "context_management": None,
    },
    "parent_tool_use_id": None,
    "session_id": "f8185fa3-2e18-46f6-8ec4-e7d87dc9b966",
    "uuid": "84846d6e-7b53-4947-b58f-cccdfbee5db2",
}

REAL_ASSISTANT_TOOL_USE = {
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
                "input": {
                    "name": "iran-news",
                    "cwd": "/home/ubuntu/axi-user-data/agents/iran-news",
                    "prompt": "Build a comprehensive Iran news monitoring system.",
                },
                "caller": {"type": "direct"},
            }
        ],
        "stop_reason": None,
        "stop_sequence": None,
        "usage": {
            "input_tokens": 3,
            "cache_creation_input_tokens": 97760,
            "cache_read_input_tokens": 16075,
            "cache_creation": {
                "ephemeral_5m_input_tokens": 0,
                "ephemeral_1h_input_tokens": 97760,
            },
            "output_tokens": 30,
            "service_tier": "standard",
            "inference_geo": "not_available",
        },
        "context_management": None,
    },
    "parent_tool_use_id": None,
    "session_id": "f8185fa3-2e18-46f6-8ec4-e7d87dc9b966",
    "uuid": "a9ddb181-0826-4ac2-a585-fac7af38cda1",
}

REAL_ASSISTANT_THINKING = {
    "type": "assistant",
    "message": {
        "model": "claude-opus-4-6",
        "id": "msg_01GnhcqoUMGmLTzKUofyGfYo",
        "type": "message",
        "role": "assistant",
        "content": [
            {
                "type": "thinking",
                "thinking": "The user is asking about the usage history file.",
                "signature": "EpcCCkYICxgCKkDh+dgfSCFrItiBkzHsh6mbS1O6tB7g",
            }
        ],
        "stop_reason": None,
        "stop_sequence": None,
        "usage": {
            "input_tokens": 3,
            "cache_creation_input_tokens": 4805,
            "cache_read_input_tokens": 127052,
            "cache_creation": {
                "ephemeral_5m_input_tokens": 0,
                "ephemeral_1h_input_tokens": 4805,
            },
            "output_tokens": 8,
            "service_tier": "standard",
            "inference_geo": "not_available",
        },
        "context_management": None,
    },
    "parent_tool_use_id": None,
    "session_id": "f8185fa3-2e18-46f6-8ec4-e7d87dc9b966",
    "uuid": "9cdf6bea-1f10-4f5f-800e-249ba3c8c125",
}


# ---------------------------------------------------------------------------
# Real samples — user messages
# ---------------------------------------------------------------------------

REAL_USER_SIMPLE = {
    "type": "user",
    "content": "Task: Add a command to restart a single agent session.",
}

REAL_USER_TOOL_RESULT = {
    "type": "user",
    "message": {
        "role": "user",
        "content": [
            {
                "tool_use_id": "toolu_01JRZMoGStk2TJgF4mWczUGH",
                "type": "tool_result",
                "content": [
                    {
                        "type": "text",
                        "text": "Agent 'iran-news' (claude_code) spawn initiated in /home/ubuntu/axi-user-data/agents/iran-news.",
                    }
                ],
            }
        ],
    },
    "parent_tool_use_id": None,
    "session_id": "f8185fa3-2e18-46f6-8ec4-e7d87dc9b966",
    "uuid": "b2728ccd-c244-4429-98df-8957a34ae9ee",
    "tool_use_result": [
        {
            "type": "text",
            "text": "Agent 'iran-news' (claude_code) spawn initiated in /home/ubuntu/axi-user-data/agents/iran-news.",
        }
    ],
}


# ---------------------------------------------------------------------------
# Real samples — system messages
# ---------------------------------------------------------------------------

REAL_SYSTEM_INIT = {
    "type": "system",
    "subtype": "init",
    "cwd": "/home/ubuntu/axi-assistant",
    "session_id": "f8185fa3-2e18-46f6-8ec4-e7d87dc9b966",
    "tools": ["TaskOutput", "Bash", "Glob", "Grep", "Read", "Write", "Edit"],
    "mcp_servers": [
        {"name": "axi", "status": "connected"},
        {"name": "utils", "status": "connected"},
        {"name": "schedule", "status": "connected"},
        {"name": "discord", "status": "connected"},
    ],
    "model": "claude-opus-4-6",
    "permissionMode": "default",
    "slash_commands": ["keybindings-help", "debug", "simplify"],
    "apiKeySource": "none",
    "claude_code_version": "2.1.63",
    "output_style": "default",
    "agents": ["general-purpose", "Explore", "Plan"],
    "skills": ["keybindings-help", "debug", "simplify"],
    "plugins": [],
    "uuid": "1e4cfc40-e5d8-49c2-aa93-802f45b28be5",
    "fast_mode_state": "off",
}

REAL_SYSTEM_STATUS_COMPACTING = {
    "type": "system",
    "subtype": "status",
    "status": "compacting",
    "session_id": "5cec1b3c-bcd8-4241-8601-e5c9c57e821c",
    "uuid": "c577ab9d-c5cb-4400-9db1-02dcdd7418f4",
}

REAL_SYSTEM_COMPACT_BOUNDARY = {
    "type": "system",
    "subtype": "compact_boundary",
    "session_id": "5cec1b3c-bcd8-4241-8601-e5c9c57e821c",
    "uuid": "88c1ace9-9fc0-4792-8f8d-3212f7f4d751",
    "compact_metadata": {"trigger": "auto", "pre_tokens": 172779},
}


# ---------------------------------------------------------------------------
# Real samples — result messages
# ---------------------------------------------------------------------------

REAL_RESULT_SUCCESS = {
    "type": "result",
    "subtype": "success",
    "is_error": False,
    "duration_ms": 57095,
    "duration_api_ms": 31197,
    "num_turns": 2,
    "result": "Spawned **iran-news** agent.",
    "stop_reason": None,
    "session_id": "f8185fa3-2e18-46f6-8ec4-e7d87dc9b966",
    "total_cost_usd": 0.707125,
    "usage": {
        "input_tokens": 4,
        "cache_creation_input_tokens": 98680,
        "cache_read_input_tokens": 129910,
        "output_tokens": 1016,
        "server_tool_use": {"web_search_requests": 0, "web_fetch_requests": 0},
        "service_tier": "standard",
        "cache_creation": {
            "ephemeral_1h_input_tokens": 98680,
            "ephemeral_5m_input_tokens": 0,
        },
        "inference_geo": "",
        "iterations": [],
        "speed": "standard",
    },
    "modelUsage": {
        "claude-opus-4-6": {
            "inputTokens": 4,
            "outputTokens": 1016,
            "cacheReadInputTokens": 129910,
            "cacheCreationInputTokens": 98680,
            "webSearchRequests": 0,
            "costUSD": 0.707125,
            "contextWindow": 200000,
            "maxOutputTokens": 32000,
        }
    },
    "permission_denials": [],
    "fast_mode_state": "off",
    "uuid": "2c4a9a27-b478-4095-a204-f5bb2a67d826",
}


# ---------------------------------------------------------------------------
# Real samples — control messages
# ---------------------------------------------------------------------------

REAL_CONTROL_REQUEST_CAN_USE_TOOL = {
    "type": "control_request",
    "request_id": "9965550a-2068-4ad0-b129-46aa10dfabbe",
    "request": {
        "subtype": "can_use_tool",
        "tool_name": "mcp__axi__axi_spawn_agent",
        "input": {
            "name": "iran-news",
            "cwd": "/home/ubuntu/axi-user-data/agents/iran-news",
            "prompt": "Build a comprehensive Iran news monitoring system.",
        },
        "permission_suggestions": [
            {
                "type": "addRules",
                "rules": [{"toolName": "mcp__axi__axi_spawn_agent"}],
                "behavior": "allow",
                "destination": "localSettings",
            }
        ],
        "tool_use_id": "toolu_01JRZMoGStk2TJgF4mWczUGH",
    },
}

REAL_CONTROL_REQUEST_MCP_MESSAGE = {
    "type": "control_request",
    "request_id": "07044b58-2aca-437f-b7dd-697eea217164",
    "request": {
        "subtype": "mcp_message",
        "server_name": "axi",
        "message": {
            "method": "tools/call",
            "params": {
                "name": "axi_spawn_agent",
                "arguments": {"name": "iran-news"},
                "_meta": {
                    "claudecode/toolUseId": "toolu_01JRZMoGStk2TJgF4mWczUGH",
                    "progressToken": 2,
                },
            },
            "jsonrpc": "2.0",
            "id": 2,
        },
    },
}

REAL_CONTROL_REQUEST_INITIALIZE = {
    "type": "control_request",
    "request_id": "req_1_a5d97f32",
    "request": {"subtype": "initialize", "hooks": None},
}

REAL_CONTROL_RESPONSE_ALLOW = {
    "type": "control_response",
    "response": {
        "subtype": "success",
        "request_id": "9965550a-2068-4ad0-b129-46aa10dfabbe",
        "response": {
            "behavior": "allow",
            "updatedInput": {
                "name": "iran-news",
                "cwd": "/home/ubuntu/axi-user-data/agents/iran-news",
                "prompt": "Build a comprehensive Iran news monitoring system.",
            },
        },
    },
}


# ---------------------------------------------------------------------------
# Real samples — rate limit events
# ---------------------------------------------------------------------------

REAL_RATE_LIMIT_ALLOWED = {
    "type": "rate_limit_event",
    "rate_limit_info": {
        "status": "allowed",
        "resetsAt": 1772766000,
        "rateLimitType": "five_hour",
        "overageStatus": "rejected",
        "overageDisabledReason": "org_level_disabled",
        "isUsingOverage": False,
    },
    "uuid": "d6797ad7-bfb7-41a8-b1b8-af2c1176b91c",
    "session_id": "4eefc928-9e56-42c0-a759-54705723dd6a",
}

REAL_RATE_LIMIT_WARNING = {
    "type": "rate_limit_event",
    "rate_limit_info": {
        "status": "allowed_warning",
        "resetsAt": 1772456400,
        "rateLimitType": "five_hour",
        "utilization": 0.9,
        "isUsingOverage": False,
        "surpassedThreshold": 0.9,
    },
    "uuid": "48dc656e-426b-44d5-bcf7-d45c07539d2d",
    "session_id": "f8185fa3-2e18-46f6-8ec4-e7d87dc9b966",
}

REAL_RATE_LIMIT_MINIMAL = {
    "type": "rate_limit_event",
    "rate_limit_info": {"status": "allowed", "isUsingOverage": False},
    "uuid": "9c3e4282-a077-4329-a2a2-3a46171230c0",
    "session_id": "f8185fa3-2e18-46f6-8ec4-e7d87dc9b966",
}


# ---------------------------------------------------------------------------
# Real bare stream events (procmux replay — no stream_event wrapper)
# ---------------------------------------------------------------------------

REAL_BARE_CB_DELTA = {
    "type": "content_block_delta",
    "index": 0,
    "delta": {"type": "input_json_delta", "partial_json": "tori"},
}


# ===========================================================================
# Tests
# ===========================================================================


class TestRealStreamEvents:
    """Every real stream_event sample validates without errors."""

    def test_message_start(self) -> None:
        result = validate_inbound(REAL_STREAM_EVENT_MESSAGE_START)
        assert result.ok
        assert result.errors == []
        assert isinstance(result.model, StreamEventMsg)
        event = result.model.event
        assert isinstance(event, MessageStartEvent)
        assert event.message.model == "claude-opus-4-6"
        assert event.message.usage is not None
        assert event.message.usage.cache_creation is not None

    def test_message_delta(self) -> None:
        result = validate_inbound(REAL_STREAM_EVENT_MESSAGE_DELTA)
        assert result.ok
        assert result.errors == []
        assert isinstance(result.model, StreamEventMsg)
        event = result.model.event
        assert isinstance(event, MessageDeltaEvent)
        assert event.delta.stop_reason == "tool_use"
        assert event.context_management is not None

    def test_message_stop(self) -> None:
        result = validate_inbound(REAL_STREAM_EVENT_MESSAGE_STOP)
        assert result.ok
        assert result.errors == []
        assert isinstance(result.model, StreamEventMsg)
        assert isinstance(result.model.event, MessageStopEvent)

    def test_content_block_start_text(self) -> None:
        result = validate_inbound(REAL_STREAM_EVENT_CB_START_TEXT)
        assert result.ok
        assert result.errors == []
        assert isinstance(result.model, StreamEventMsg)
        event = result.model.event
        assert isinstance(event, ContentBlockStartEvent)
        assert event.content_block.type == "text"  # type: ignore[union-attr]

    def test_content_block_start_tool_use(self) -> None:
        result = validate_inbound(REAL_STREAM_EVENT_CB_START_TOOL_USE)
        assert result.ok
        assert result.errors == []
        assert isinstance(result.model, StreamEventMsg)
        event = result.model.event
        assert isinstance(event, ContentBlockStartEvent)
        assert event.content_block.name == "mcp__axi__axi_spawn_agent"  # type: ignore[union-attr]
        assert event.content_block.caller == {"type": "direct"}  # type: ignore[union-attr]

    def test_content_block_start_thinking(self) -> None:
        result = validate_inbound(REAL_STREAM_EVENT_CB_START_THINKING)
        assert result.ok
        assert result.errors == []

    def test_content_block_delta_text(self) -> None:
        result = validate_inbound(REAL_STREAM_EVENT_CB_DELTA_TEXT)
        assert result.ok
        assert result.errors == []
        assert isinstance(result.model, StreamEventMsg)
        assert isinstance(result.model.event, ContentBlockDeltaEvent)
        assert result.model.event.delta.text == "Spawned **iran-news**"  # type: ignore[union-attr]

    def test_content_block_delta_input_json(self) -> None:
        result = validate_inbound(REAL_STREAM_EVENT_CB_DELTA_INPUT_JSON)
        assert result.ok
        assert result.errors == []

    def test_content_block_delta_thinking(self) -> None:
        result = validate_inbound(REAL_STREAM_EVENT_CB_DELTA_THINKING)
        assert result.ok
        assert result.errors == []

    def test_content_block_delta_signature(self) -> None:
        result = validate_inbound(REAL_STREAM_EVENT_CB_DELTA_SIGNATURE)
        assert result.ok
        assert result.errors == []

    def test_content_block_stop(self) -> None:
        result = validate_inbound(REAL_STREAM_EVENT_CB_STOP)
        assert result.ok
        assert result.errors == []
        assert isinstance(result.model, StreamEventMsg)
        assert isinstance(result.model.event, ContentBlockStopEvent)


class TestRealAssistantMessages:
    """Real assistant messages with text, tool_use, and thinking blocks."""

    def test_text_message(self) -> None:
        result = validate_inbound(REAL_ASSISTANT_TEXT)
        assert result.ok
        assert result.errors == []
        assert isinstance(result.model, AssistantMsg)
        assert result.model.message.content[0].type == "text"  # type: ignore[union-attr]
        assert result.model.message.usage is not None
        assert result.model.message.usage.cache_creation is not None

    def test_tool_use_message(self) -> None:
        result = validate_inbound(REAL_ASSISTANT_TOOL_USE)
        assert result.ok
        assert result.errors == []
        assert isinstance(result.model, AssistantMsg)
        block = result.model.message.content[0]
        assert block.type == "tool_use"
        assert block.name == "mcp__axi__axi_spawn_agent"  # type: ignore[union-attr]
        assert block.caller == {"type": "direct"}  # type: ignore[union-attr]

    def test_thinking_message(self) -> None:
        result = validate_inbound(REAL_ASSISTANT_THINKING)
        assert result.ok
        assert result.errors == []
        assert isinstance(result.model, AssistantMsg)
        assert result.model.message.content[0].type == "thinking"  # type: ignore[union-attr]


class TestRealUserMessages:
    """Real user messages — simple prompts and tool results."""

    def test_simple_prompt(self) -> None:
        result = validate_inbound(REAL_USER_SIMPLE)
        assert result.ok
        assert isinstance(result.model, UserMsg)

    def test_tool_result(self) -> None:
        result = validate_inbound(REAL_USER_TOOL_RESULT)
        assert result.ok
        assert result.errors == []
        assert isinstance(result.model, UserMsg)
        # tool_use_result is preserved as extra field
        assert result.model.tool_use_result is not None


class TestRealSystemMessages:
    """Real system messages — init, status, compact_boundary."""

    def test_init(self) -> None:
        result = validate_inbound(REAL_SYSTEM_INIT)
        assert result.ok
        assert isinstance(result.model, SystemMsg)
        assert result.model.subtype == "init"

    def test_status_compacting(self) -> None:
        result = validate_inbound(REAL_SYSTEM_STATUS_COMPACTING)
        assert result.ok
        assert isinstance(result.model, SystemMsg)
        assert result.model.subtype == "status"

    def test_compact_boundary(self) -> None:
        result = validate_inbound(REAL_SYSTEM_COMPACT_BOUNDARY)
        assert result.ok
        assert isinstance(result.model, SystemMsg)
        assert result.model.subtype == "compact_boundary"


class TestRealResultMessages:
    """Real result messages with full usage and modelUsage."""

    def test_success(self) -> None:
        result = validate_inbound(REAL_RESULT_SUCCESS)
        assert result.ok
        assert result.errors == []
        assert isinstance(result.model, ResultMsg)
        assert result.model.duration_ms == 57095
        assert result.model.total_cost_usd == 0.707125
        assert result.model.num_turns == 2
        # Rich usage fields
        assert result.model.usage is not None
        assert result.model.usage.speed == "standard"
        assert result.model.usage.server_tool_use is not None
        assert result.model.usage.iterations == []
        # modelUsage
        assert result.model.modelUsage is not None
        assert "claude-opus-4-6" in result.model.modelUsage


class TestRealControlMessages:
    """Real control_request and control_response messages."""

    def test_can_use_tool(self) -> None:
        result = validate_inbound(REAL_CONTROL_REQUEST_CAN_USE_TOOL)
        assert result.ok
        assert result.errors == []
        assert isinstance(result.model, ControlRequestMsg)
        assert result.model.request.subtype == "can_use_tool"

    def test_mcp_message(self) -> None:
        result = validate_inbound(REAL_CONTROL_REQUEST_MCP_MESSAGE)
        assert result.ok
        assert result.errors == []
        assert isinstance(result.model, ControlRequestMsg)
        assert result.model.request.subtype == "mcp_message"

    def test_initialize(self) -> None:
        result = validate_inbound(REAL_CONTROL_REQUEST_INITIALIZE)
        assert result.ok
        assert result.errors == []
        assert isinstance(result.model, ControlRequestMsg)
        assert result.model.request.subtype == "initialize"

    def test_response_allow(self) -> None:
        # control_response is both inbound (rare) and outbound
        result = validate_inbound(REAL_CONTROL_RESPONSE_ALLOW)
        assert result.ok
        assert result.errors == []
        assert isinstance(result.model, ControlResponseMsg)

    def test_response_outbound(self) -> None:
        result = validate_outbound(REAL_CONTROL_RESPONSE_ALLOW)
        assert result.ok
        assert result.errors == []


class TestRealRateLimitEvents:
    """Real rate_limit_event messages — various statuses and field combos."""

    def test_allowed_with_overage(self) -> None:
        result = validate_inbound(REAL_RATE_LIMIT_ALLOWED)
        assert result.ok
        assert result.errors == []
        assert isinstance(result.model, RateLimitEventMsg)
        assert result.model.rate_limit_info.status == "allowed"
        assert result.model.rate_limit_info.overageStatus == "rejected"
        assert result.model.rate_limit_info.overageDisabledReason == "org_level_disabled"

    def test_warning(self) -> None:
        result = validate_inbound(REAL_RATE_LIMIT_WARNING)
        assert result.ok
        assert result.errors == []
        assert isinstance(result.model, RateLimitEventMsg)
        assert result.model.rate_limit_info.status == "allowed_warning"
        assert result.model.rate_limit_info.utilization == 0.9
        assert result.model.rate_limit_info.surpassedThreshold == 0.9

    def test_minimal(self) -> None:
        """Minimal rate_limit_event — missing resetsAt and rateLimitType."""
        result = validate_inbound(REAL_RATE_LIMIT_MINIMAL)
        assert result.ok
        assert result.errors == []
        assert isinstance(result.model, RateLimitEventMsg)
        assert result.model.rate_limit_info.resetsAt is None
        assert result.model.rate_limit_info.rateLimitType is None


class TestRealBareStreamEvents:
    """Bare stream events from procmux replay (no stream_event wrapper)."""

    def test_bare_content_block_delta(self) -> None:
        result = validate_inbound_or_bare(REAL_BARE_CB_DELTA)
        assert result.ok
        assert result.errors == []
        assert isinstance(result.model, ContentBlockDeltaEvent)
