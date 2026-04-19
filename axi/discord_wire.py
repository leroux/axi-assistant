"""Append-only Discord wire audit log for inbound and outbound bot traffic."""

from __future__ import annotations

import io
import logging
import os
import threading
import time
import uuid
from datetime import UTC, datetime
from itertools import count
from typing import TYPE_CHECKING, Any, Literal

from pydantic import BaseModel, Field

from axi.egress_filter import scrub_secrets

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Sequence

    import discord

log = logging.getLogger("axi")

DEFAULT_WIRE_LOG_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "logs",
    "discord-wire.jsonl",
)


class DiscordAttachmentMeta(BaseModel):
    filename: str | None = None
    content_type: str | None = None
    size: int | None = None
    spoiler: bool | None = None


class DiscordRequestMeta(BaseModel):
    method: str | None = None
    path: str | None = None
    params: dict[str, str] | None = None
    json_body: dict[str, Any] | None = None
    data: dict[str, str] | None = None
    files: list[DiscordAttachmentMeta] = Field(default_factory=list)


class DiscordResponseMeta(BaseModel):
    status_code: int | None = None
    body_type: str | None = None
    item_count: int | None = None
    discord_id: str | None = None
    message_id: str | None = None
    channel_id: str | None = None
    guild_id: str | None = None


class DiscordWireEvent(BaseModel):
    seq: int
    run_id: str
    created_at: datetime
    created_unix_ns: int
    monotonic_ns: int
    direction: Literal["inbound", "outbound"]
    transport: str
    operation: str
    outcome: Literal["success", "error"]
    guild_id: str | None = None
    channel_id: str | None = None
    channel_name: str | None = None
    message_id: str | None = None
    user_id: str | None = None
    user_name: str | None = None
    message_type: str | None = None
    emoji: str | None = None
    content_text: str | None = None
    content_preview: str | None = None
    content_length: int | None = None
    attachments: list[DiscordAttachmentMeta] = Field(default_factory=list)
    request: DiscordRequestMeta | None = None
    response: DiscordResponseMeta | None = None
    details: dict[str, Any] | None = None
    error: str | None = None


class DiscordWireLogger:
    def __init__(self, path: str = DEFAULT_WIRE_LOG_PATH, *, run_id: str | None = None) -> None:
        self.path = path
        self.run_id = run_id or f"discord-wire-{os.getpid()}-{uuid.uuid4().hex[:12]}"
        self._seq = count(1)
        self._lock = threading.Lock()

    def next_seq(self) -> int:
        with self._lock:
            return next(self._seq)

    def make_event(self, **fields: Any) -> DiscordWireEvent:
        now = datetime.now(UTC)
        return DiscordWireEvent(
            seq=self.next_seq(),
            run_id=self.run_id,
            created_at=now,
            created_unix_ns=time.time_ns(),
            monotonic_ns=time.monotonic_ns(),
            **fields,
        )

    def append(self, event: DiscordWireEvent) -> None:
        try:
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            with self._lock, open(self.path, "a", encoding="utf-8") as f:
                f.write(event.model_dump_json() + "\n")
        except Exception:
            log.warning("Failed to append Discord wire event", exc_info=True)

    def emit(self, **fields: Any) -> DiscordWireEvent:
        event = self.make_event(**fields)
        self.append(event)
        return event


_default_logger = DiscordWireLogger()


def get_default_logger() -> DiscordWireLogger:
    return _default_logger


def _safe_text(text: str | None) -> str | None:
    if text is None:
        return None
    return scrub_secrets(text)


def _preview(text: str | None, limit: int = 200) -> str | None:
    if not text:
        return text
    return text[:limit]


def _stringify_mapping(value: dict[str, Any] | None) -> dict[str, str] | None:
    if not value:
        return None
    result: dict[str, str] = {}
    for key, item in value.items():
        if item is None:
            result[str(key)] = ""
        else:
            result[str(key)] = str(item)
    return result


def _sanitize_json(value: Any) -> Any:
    if isinstance(value, str):
        return scrub_secrets(value)
    if isinstance(value, dict):
        return {str(key): _sanitize_json(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_sanitize_json(item) for item in value]
    return value


def _attachment_from_discord_attachment(attachment: discord.Attachment) -> DiscordAttachmentMeta:
    return DiscordAttachmentMeta(
        filename=attachment.filename,
        content_type=attachment.content_type,
        size=attachment.size,
        spoiler=attachment.is_spoiler(),
    )


def _file_size(file: discord.File) -> int | None:
    fp = getattr(file, "fp", None)
    if fp is None:
        return None
    if isinstance(fp, io.BytesIO):
        return fp.getbuffer().nbytes
    if not hasattr(fp, "tell") or not hasattr(fp, "seek"):
        return None
    try:
        pos = fp.tell()
        fp.seek(0, os.SEEK_END)
        size = fp.tell()
        fp.seek(pos)
        return int(size)
    except Exception:
        return None


def _attachment_from_file(file: discord.File) -> DiscordAttachmentMeta:
    return DiscordAttachmentMeta(
        filename=getattr(file, "filename", None),
        content_type=None,
        size=_file_size(file),
        spoiler=getattr(file, "spoiler", None),
    )


def _attachment_metas_from_send_inputs(
    *,
    file: discord.File | None = None,
    files: Sequence[discord.File] | None = None,
) -> list[DiscordAttachmentMeta]:
    attachments: list[DiscordAttachmentMeta] = []
    if file is not None:
        attachments.append(_attachment_from_file(file))
    if files:
        attachments.extend(_attachment_from_file(item) for item in files)
    return attachments


def _attachments_from_files(files: Any) -> list[DiscordAttachmentMeta]:
    if not files:
        return []

    if isinstance(files, dict):
        items = files.values()
    else:
        items = files

    attachments: list[DiscordAttachmentMeta] = []
    for item in items:
        filename: str | None = None
        size: int | None = None
        if isinstance(item, tuple):
            if item:
                filename = str(item[0]) if item[0] is not None else None
            if len(item) > 1:
                data = item[1]
                if isinstance(data, (bytes, bytearray)):
                    size = len(data)
        elif isinstance(item, dict):
            filename = str(item.get("filename")) if item.get("filename") else None
            raw_size = item.get("size")
            size = int(raw_size) if isinstance(raw_size, int) else None
        attachments.append(DiscordAttachmentMeta(filename=filename, size=size))
    return attachments


def _interaction_fields(interaction: Any, message: Any | None = None) -> dict[str, str | None]:
    channel = getattr(message, "channel", None) or getattr(interaction, "channel", None)
    guild = getattr(message, "guild", None) or getattr(channel, "guild", None)
    return {
        "guild_id": str(getattr(guild, "id", "")) or (str(interaction.guild_id) if getattr(interaction, "guild_id", None) is not None else None),
        "channel_id": str(getattr(channel, "id", "")) or (str(interaction.channel_id) if getattr(interaction, "channel_id", None) is not None else None),
        "channel_name": getattr(channel, "name", None),
    }


def _channel_fields(channel: Any) -> dict[str, str | None]:
    guild = getattr(channel, "guild", None)
    return {
        "guild_id": str(guild.id) if guild is not None else None,
        "channel_id": str(getattr(channel, "id", "")) or None,
        "channel_name": getattr(channel, "name", None),
    }


def _response_meta(status_code: int | None, body: Any) -> DiscordResponseMeta:
    body_type = type(body).__name__ if body is not None else "empty"
    item_count = len(body) if isinstance(body, list) else None
    message_id = None
    channel_id = None
    guild_id = None
    discord_id = None
    if isinstance(body, dict):
        discord_id = str(body.get("id")) if body.get("id") is not None else None
        message_id = str(body.get("id")) if body.get("id") is not None else None
        channel_id = str(body.get("channel_id")) if body.get("channel_id") is not None else None
        guild_id = str(body.get("guild_id")) if body.get("guild_id") is not None else None
    return DiscordResponseMeta(
        status_code=status_code,
        body_type=body_type,
        item_count=item_count,
        discord_id=discord_id,
        message_id=message_id,
        channel_id=channel_id,
        guild_id=guild_id,
    )


def _ids_from_path(path: str) -> tuple[str | None, str | None, str | None]:
    parts = [part for part in path.strip("/").split("/") if part]
    guild_id = None
    channel_id = None
    message_id = None
    for index, part in enumerate(parts):
        if part == "guilds" and index + 1 < len(parts):
            guild_id = parts[index + 1]
        elif part == "channels" and index + 1 < len(parts):
            channel_id = parts[index + 1]
        elif part == "messages" and index + 1 < len(parts):
            message_id = parts[index + 1]
    return guild_id, channel_id, message_id

def _operation_from_rest(method: str, path: str) -> str:
    normalized = path.strip()
    if normalized.endswith("/messages") and method == "POST":
        return "message.create"
    if "/messages/" in normalized and method == "PATCH":
        return "message.edit"
    if "/messages/" in normalized and method == "DELETE":
        return "message.delete"
    if "/reactions/" in normalized and method == "PUT":
        return "reaction.add"
    if "/reactions/" in normalized and method == "DELETE":
        return "reaction.remove"
    if normalized.startswith("/guilds/") and normalized.endswith("/channels") and method == "POST":
        return "channel.create"
    if normalized.startswith("/guilds/") and normalized.endswith("/channels") and method == "PATCH":
        return "guild.channels.bulk_update"
    return f"rest.{method.lower()}"


def log_inbound_message(message: discord.Message) -> None:
    text = _safe_text(message.content)
    get_default_logger().emit(
        direction="inbound",
        transport="gateway",
        operation="message.receive",
        outcome="success",
        guild_id=str(message.guild.id) if message.guild else None,
        channel_id=str(message.channel.id),
        channel_name=getattr(message.channel, "name", None),
        message_id=str(message.id),
        user_id=str(message.author.id),
        user_name=str(message.author),
        message_type=message.type.name,
        content_text=text,
        content_preview=_preview(text),
        content_length=len(text) if text is not None else None,
        attachments=[_attachment_from_discord_attachment(a) for a in message.attachments],
        details={
            "author_bot": message.author.bot,
            "attachment_count": len(message.attachments),
        },
    )


def log_inbound_reaction(payload: discord.RawReactionActionEvent) -> None:
    get_default_logger().emit(
        direction="inbound",
        transport="gateway",
        operation="reaction.add.receive",
        outcome="success",
        guild_id=str(payload.guild_id) if payload.guild_id is not None else None,
        channel_id=str(payload.channel_id),
        message_id=str(payload.message_id),
        user_id=str(payload.user_id),
        emoji=str(payload.emoji),
    )


def log_discordpy_reaction(
    message: discord.Message,
    emoji: str,
    *,
    operation: str,
    outcome: Literal["success", "error"],
    error: str | None = None,
) -> None:
    get_default_logger().emit(
        direction="outbound",
        transport="discordpy",
        operation=operation,
        outcome=outcome,
        guild_id=str(message.guild.id) if message.guild else None,
        channel_id=str(message.channel.id),
        channel_name=getattr(message.channel, "name", None),
        message_id=str(message.id),
        emoji=emoji,
        response=DiscordResponseMeta(status_code=200 if outcome == "success" else None, message_id=str(message.id)),
        error=error,
    )


async def audited_channel_send(
    channel: Any,
    content: str | None = None,
    *,
    file: discord.File | None = None,
    retry_fn: Callable[..., Awaitable[Any]] | None = None,
    operation: str = "message.create",
    details: dict[str, Any] | None = None,
    **kwargs: Any,
) -> discord.Message:
    attachments = [_attachment_from_file(file)] if file is not None else []
    scrubbed = _safe_text(content)
    fields = _channel_fields(channel)
    send_kwargs = dict(kwargs)
    if file is not None:
        send_kwargs["file"] = file
    try:
        if retry_fn is not None:
            message = await retry_fn(channel.send, content, **send_kwargs)
        else:
            message = await channel.send(content, **send_kwargs)
        get_default_logger().emit(
            direction="outbound",
            transport="discordpy",
            operation=operation,
            outcome="success",
            content_text=scrubbed,
            content_preview=_preview(scrubbed),
            content_length=len(scrubbed) if scrubbed is not None else None,
            attachments=attachments,
            response=DiscordResponseMeta(
                status_code=200,
                message_id=str(message.id),
                channel_id=str(getattr(message.channel, "id", "")) or None,
                guild_id=str(message.guild.id) if message.guild else None,
            ),
            details=details,
            **fields,
        )
        return message
    except Exception as exc:
        get_default_logger().emit(
            direction="outbound",
            transport="discordpy",
            operation=operation,
            outcome="error",
            content_text=scrubbed,
            content_preview=_preview(scrubbed),
            content_length=len(scrubbed) if scrubbed is not None else None,
            attachments=attachments,
            error=f"{type(exc).__name__}: {exc}",
            details=details,
            **fields,
        )
        raise


async def audited_message_edit(
    message: discord.Message,
    *,
    content: str,
    operation: str = "message.edit",
    details: dict[str, Any] | None = None,
    **kwargs: Any,
) -> discord.Message:
    scrubbed = _safe_text(content)
    try:
        updated = await message.edit(content=content, **kwargs)
        get_default_logger().emit(
            direction="outbound",
            transport="discordpy",
            operation=operation,
            outcome="success",
            guild_id=str(updated.guild.id) if updated.guild else None,
            channel_id=str(updated.channel.id),
            channel_name=getattr(updated.channel, "name", None),
            message_id=str(updated.id),
            content_text=scrubbed,
            content_preview=_preview(scrubbed),
            content_length=len(scrubbed),
            response=DiscordResponseMeta(
                status_code=200,
                message_id=str(updated.id),
                channel_id=str(updated.channel.id),
                guild_id=str(updated.guild.id) if updated.guild else None,
            ),
            details=details,
        )
        return updated
    except Exception as exc:
        get_default_logger().emit(
            direction="outbound",
            transport="discordpy",
            operation=operation,
            outcome="error",
            guild_id=str(message.guild.id) if message.guild else None,
            channel_id=str(message.channel.id),
            channel_name=getattr(message.channel, "name", None),
            message_id=str(message.id),
            content_text=scrubbed,
            content_preview=_preview(scrubbed),
            content_length=len(scrubbed),
            error=f"{type(exc).__name__}: {exc}",
            details=details,
        )
        raise


async def audited_interaction_response_send(
    interaction: Any,
    content: str | None = None,
    *,
    file: discord.File | None = None,
    files: Sequence[discord.File] | None = None,
    operation: str = "interaction.response.send_message",
    details: dict[str, Any] | None = None,
    **kwargs: Any,
) -> Any:
    attachments = _attachment_metas_from_send_inputs(file=file, files=files)
    scrubbed = _safe_text(content)
    response_kwargs = dict(kwargs)
    if file is not None:
        response_kwargs["file"] = file
    if files is not None:
        response_kwargs["files"] = files
    try:
        callback = await interaction.response.send_message(content, **response_kwargs)
        message = getattr(callback, "resource", None)
        fields = _interaction_fields(interaction, message)
        get_default_logger().emit(
            direction="outbound",
            transport="discord-interaction",
            operation=operation,
            outcome="success",
            message_id=(str(getattr(message, "id", "")) or None) or (str(callback.message_id) if getattr(callback, "message_id", None) else None),
            content_text=scrubbed,
            content_preview=_preview(scrubbed),
            content_length=len(scrubbed) if scrubbed is not None else None,
            attachments=attachments,
            response=DiscordResponseMeta(
                status_code=200,
                message_id=(str(getattr(message, "id", "")) or None) or (str(callback.message_id) if getattr(callback, "message_id", None) else None),
                channel_id=fields["channel_id"],
                guild_id=fields["guild_id"],
            ),
            details={
                **(details or {}),
                "ephemeral": bool(response_kwargs.get("ephemeral", False)),
                "response_type": getattr(getattr(callback, "type", None), "name", None),
            },
            **fields,
        )
        return callback
    except Exception as exc:
        fields = _interaction_fields(interaction)
        get_default_logger().emit(
            direction="outbound",
            transport="discord-interaction",
            operation=operation,
            outcome="error",
            content_text=scrubbed,
            content_preview=_preview(scrubbed),
            content_length=len(scrubbed) if scrubbed is not None else None,
            attachments=attachments,
            error=f"{type(exc).__name__}: {exc}",
            details={**(details or {}), "ephemeral": bool(response_kwargs.get("ephemeral", False))},
            **fields,
        )
        raise


async def audited_interaction_followup_send(
    interaction: Any,
    content: str | None = None,
    *,
    file: discord.File | None = None,
    files: Sequence[discord.File] | None = None,
    operation: str = "interaction.followup.send",
    details: dict[str, Any] | None = None,
    **kwargs: Any,
) -> Any:
    attachments = _attachment_metas_from_send_inputs(file=file, files=files)
    scrubbed = _safe_text(content)
    followup_kwargs = dict(kwargs)
    if file is not None:
        followup_kwargs["file"] = file
    if files is not None:
        followup_kwargs["files"] = files
    try:
        message = await interaction.followup.send(content, **followup_kwargs)
        fields = _interaction_fields(interaction, message)
        get_default_logger().emit(
            direction="outbound",
            transport="discord-interaction",
            operation=operation,
            outcome="success",
            message_id=str(getattr(message, "id", "")) or None,
            content_text=scrubbed,
            content_preview=_preview(scrubbed),
            content_length=len(scrubbed) if scrubbed is not None else None,
            attachments=attachments,
            response=DiscordResponseMeta(
                status_code=200,
                message_id=str(getattr(message, "id", "")) or None,
                channel_id=fields["channel_id"],
                guild_id=fields["guild_id"],
            ),
            details={**(details or {}), "ephemeral": bool(followup_kwargs.get("ephemeral", False))},
            **fields,
        )
        return message
    except Exception as exc:
        fields = _interaction_fields(interaction)
        get_default_logger().emit(
            direction="outbound",
            transport="discord-interaction",
            operation=operation,
            outcome="error",
            content_text=scrubbed,
            content_preview=_preview(scrubbed),
            content_length=len(scrubbed) if scrubbed is not None else None,
            attachments=attachments,
            error=f"{type(exc).__name__}: {exc}",
            details={**(details or {}), "ephemeral": bool(followup_kwargs.get("ephemeral", False))},
            **fields,
        )
        raise


def emit_rest_audit_event(raw_event: dict[str, Any]) -> None:
    method = str(raw_event.get("method") or "")
    path = str(raw_event.get("path") or "")
    request_json = _sanitize_json(raw_event.get("json"))
    request_data = _stringify_mapping(raw_event.get("data"))
    request_params = _stringify_mapping(raw_event.get("params"))
    files = _attachments_from_files(raw_event.get("files"))
    content = None
    if isinstance(request_json, dict) and isinstance(request_json.get("content"), str):
        content = request_json["content"]
    elif request_data and request_data.get("content"):
        content = scrub_secrets(request_data["content"])
    response_json = raw_event.get("response_json")
    response = _response_meta(raw_event.get("status_code"), response_json)
    path_guild_id, path_channel_id, path_message_id = _ids_from_path(path)

    event_fields = {
        "direction": "outbound",
        "transport": "discord-rest",
        "operation": _operation_from_rest(method, path),
        "outcome": raw_event.get("outcome", "success"),
        "guild_id": str(raw_event.get("guild_id")) if raw_event.get("guild_id") is not None else path_guild_id,
        "channel_id": str(raw_event.get("channel_id")) if raw_event.get("channel_id") is not None else path_channel_id,
        "message_id": str(raw_event.get("message_id")) if raw_event.get("message_id") is not None else path_message_id,
        "content_text": content,
        "content_preview": _preview(content),
        "content_length": len(content) if isinstance(content, str) else None,
        "attachments": files,
        "request": DiscordRequestMeta(
            method=method,
            path=path,
            params=request_params,
            json_body=request_json if isinstance(request_json, dict) else None,
            data=request_data,
            files=files,
        ),
        "response": response,
        "details": {
            "ratelimit_retries": raw_event.get("ratelimit_retries", 0),
            "server_error_retries": raw_event.get("server_error_retries", 0),
        },
        "error": raw_event.get("error"),
    }
    get_default_logger().emit(**event_fields)
