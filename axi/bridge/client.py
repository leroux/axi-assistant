"""Bridge transport -- compatibility shim.

BridgeTransport has moved to the claudewire package.
This module re-exports it for backward compatibility.
"""

from claudewire.transport import BridgeTransport

__all__ = ["BridgeTransport"]
