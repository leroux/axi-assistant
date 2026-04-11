"""FlowcoderBridgeTransport — BridgeTransport subclass for flowcoder agents.

The flowcoder engine wraps inner CLI messages in system envelopes:
- session_message: wraps stream_event, assistant, result, etc.
- stderr: forwards inner CLI's stderr lines

This subclass unwraps those envelopes so the SDK parses them normally.
"""

from __future__ import annotations

import logging

from claudewire import BridgeTransport, StdoutEvent

log = logging.getLogger(__name__)


class FlowcoderBridgeTransport(BridgeTransport):
    """BridgeTransport that unwraps flowcoder session_message envelopes."""

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
                msg_data = msg_data["data"]["message"]
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
