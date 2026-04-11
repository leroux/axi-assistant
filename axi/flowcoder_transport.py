"""FlowcoderBridgeTransport — BridgeTransport subclass for flowcoder agents.

The flowcoder engine wraps inner CLI messages in system envelopes:
- session_message: wraps stream_event, assistant, result, etc.
- stderr: forwards inner CLI's stderr lines

This subclass unwraps those envelopes so the SDK parses them normally.
"""

from __future__ import annotations

import logging

import asyncio
from typing import Any

from claudewire import BridgeTransport, StdoutEvent
from claudewire.permissions import Allow, to_control_response

log = logging.getLogger(__name__)


class FlowcoderBridgeTransport(BridgeTransport):
    """BridgeTransport that unwraps flowcoder session_message envelopes."""

    async def _handle_can_use_tool(self, msg: dict[str, Any]) -> None:
        """Override to fix updatedInput Zod validation.

        The CLI requires updatedInput on Allow responses. Upstream
        BridgeTransport omits it when Allow has no updated_input.
        Match SDK Query behavior: fall back to original tool_input.
        """
        assert self._can_use_tool is not None
        request = msg.get("request", {})
        request_id = msg.get("request_id", "")
        tool_name = request.get("tool_name", "")
        tool_input = request.get("input", {})

        log.debug("[read][%s] can_use_tool: %s", self._name, tool_name)
        result = self._can_use_tool(tool_name, tool_input)
        if asyncio.iscoroutine(result):
            result = await result

        if isinstance(result, Allow) and result.updated_input is None:
            result = Allow(updated_input=tool_input)

        response = to_control_response(request_id, result)
        if self._stdio_logger:
            import json
            self._stdio_logger.debug(">>> STDIN  %s (auto)", json.dumps(response))
        await self._conn.send_stdin(self._name, response)

    async def read_messages(self):
        async for msg_data in super().read_messages():
            msg_type = msg_data.get("type", "?")

            # Unwrap session_message: the flowcoder engine wraps inner SDK
            # messages (stream_event, assistant, etc.) in a system envelope.
            # Yield the inner message so the SDK parses it normally.
            if (
                msg_type == "system"
                and msg_data.get("subtype") == "session_message"
                and "message" in msg_data.get("data", {})
            ):
                envelope = msg_data["data"]
                inner = envelope["message"]
                inner["_session_context"] = {
                    "session": envelope.get("session", ""),
                    "block_id": envelope.get("block_id", ""),
                    "block_name": envelope.get("block_name", ""),
                }
                msg_data = inner
                msg_type = msg_data.get("type", "?")

            # The engine forwards inner CLI stderr as stdout system
            # messages with subtype "stderr". Extract the line and
            # call the stderr callback so autocompact parsing works.
            if (
                msg_type == "system"
                and msg_data.get("subtype") == "stderr"
                and self._stderr_callback
            ):
                line = msg_data.get("data", {}).get("line", "")
                if line:
                    self._stderr_callback(line)
                continue

            yield msg_data
