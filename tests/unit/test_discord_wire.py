from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from axi.discord_wire import (
    DiscordWireLogger,
    audited_interaction_followup_send,
    audited_interaction_response_send,
    emit_rest_audit_event,
)


def test_discord_wire_logger_serializes_and_persists_jsonl(tmp_path) -> None:
    logger = DiscordWireLogger(path=str(tmp_path / "discord-wire.jsonl"), run_id="run-1")

    event = logger.emit(
        direction="outbound",
        transport="discordpy",
        operation="message.create",
        outcome="success",
        channel_id="123",
        content_text="hello",
        content_preview="hello",
        content_length=5,
    )

    assert event.seq == 1
    assert event.run_id == "run-1"
    assert event.created_at.tzinfo is not None
    assert event.created_unix_ns > 0
    assert event.monotonic_ns > 0

    lines = (tmp_path / "discord-wire.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    parsed = json.loads(lines[0])
    assert parsed["seq"] == 1
    assert parsed["operation"] == "message.create"
    assert parsed["channel_id"] == "123"


def test_discord_wire_seq_increases(tmp_path) -> None:
    logger = DiscordWireLogger(path=str(tmp_path / "discord-wire.jsonl"), run_id="run-2")

    first = logger.make_event(
        direction="inbound",
        transport="gateway",
        operation="message.receive",
        outcome="success",
    )
    second = logger.make_event(
        direction="outbound",
        transport="discord-rest",
        operation="message.create",
        outcome="success",
    )

    assert first.seq == 1
    assert second.seq == 2
    assert second.monotonic_ns >= first.monotonic_ns


def test_emit_rest_audit_event_scrubs_content(monkeypatch) -> None:
    captured: list[object] = []

    class StubLogger:
        def emit(self, **fields):
            captured.append(fields)
            return fields

    monkeypatch.setattr("axi.discord_wire._default_logger", StubLogger())

    emit_rest_audit_event(
        {
            "method": "POST",
            "path": "/channels/123/messages",
            "json": {"content": "DISCORD_TOKEN=MTIzNDU2Nzg5MDEyMzQ1Njc4.GwPQ4g.abcdefghijklmnopqrstuvwxyz1"},
            "outcome": "success",
            "status_code": 200,
            "response_json": {"id": "456", "channel_id": "123"},
            "ratelimit_retries": 0,
            "server_error_retries": 0,
        }
    )

    assert len(captured) == 1
    fields = captured[0]
    assert fields["operation"] == "message.create"
    assert fields["channel_id"] == "123"
    assert fields["content_text"] == "[REDACTED:secret]"
    assert fields["response"].message_id == "456"


def test_emit_rest_audit_event_records_files(monkeypatch) -> None:
    captured: list[object] = []

    class StubLogger:
        def emit(self, **fields):
            captured.append(fields)
            return fields

    monkeypatch.setattr("axi.discord_wire._default_logger", StubLogger())

    emit_rest_audit_event(
        {
            "method": "POST",
            "path": "/channels/123/messages",
            "data": {"content": "hello"},
            "files": {"files[0]": ("report.txt", b"abc")},
            "outcome": "success",
            "status_code": 201,
            "response_json": {"id": "789", "channel_id": "123"},
        }
    )

    fields = captured[0]
    assert fields["attachments"][0].filename == "report.txt"
    assert fields["attachments"][0].size == 3
    assert fields["response"].status_code == 201


@pytest.mark.asyncio
async def test_audited_interaction_response_send_logs_message_id(monkeypatch) -> None:
    captured: list[object] = []

    class StubLogger:
        def emit(self, **fields):
            captured.append(fields)
            return fields

    message = SimpleNamespace(id=456, channel=SimpleNamespace(id=123, name="ops"), guild=SimpleNamespace(id=789))
    callback = SimpleNamespace(message_id=456, resource=message, type=SimpleNamespace(name="channel_message"))
    interaction = SimpleNamespace(
        channel_id=123,
        guild_id=789,
        channel=SimpleNamespace(id=123, name="ops", guild=SimpleNamespace(id=789)),
        response=SimpleNamespace(send_message=AsyncMock(return_value=callback)),
    )

    monkeypatch.setattr("axi.discord_wire._default_logger", StubLogger())

    result = await audited_interaction_response_send(interaction, "hello", ephemeral=True)

    assert result is callback
    fields = captured[0]
    assert fields["transport"] == "discord-interaction"
    assert fields["operation"] == "interaction.response.send_message"
    assert fields["message_id"] == "456"
    assert fields["channel_id"] == "123"
    assert fields["guild_id"] == "789"
    assert fields["details"]["ephemeral"] is True


@pytest.mark.asyncio
async def test_audited_interaction_followup_send_logs_message_id(monkeypatch) -> None:
    captured: list[object] = []

    class StubLogger:
        def emit(self, **fields):
            captured.append(fields)
            return fields

    message = SimpleNamespace(id=654, channel=SimpleNamespace(id=321, name="ops"), guild=SimpleNamespace(id=987))
    interaction = SimpleNamespace(
        channel_id=321,
        guild_id=987,
        channel=SimpleNamespace(id=321, name="ops", guild=SimpleNamespace(id=987)),
        followup=SimpleNamespace(send=AsyncMock(return_value=message)),
    )

    monkeypatch.setattr("axi.discord_wire._default_logger", StubLogger())

    result = await audited_interaction_followup_send(interaction, "world", ephemeral=False)

    assert result is message
    fields = captured[0]
    assert fields["transport"] == "discord-interaction"
    assert fields["operation"] == "interaction.followup.send"
    assert fields["message_id"] == "654"
    assert fields["channel_id"] == "321"
    assert fields["guild_id"] == "987"
    assert fields["details"]["ephemeral"] is False
