"""Local API for the UI shell (pywebview / any webview frontend).

Layering, same philosophy as the protocol codecs:
  rpc.py     - pure JSON-RPC dispatch + event bus (no I/O, no deps)
  service.py - binds the Logic bridge + DAW source into RPC methods + event topics
  ws.py      - thin WebSocket transport (optional `websockets` dependency)

The UI is a pure client: requests in, subscribed events out. It never touches
serial/MIDI directly.
"""
from .rpc import EventBus, JsonRpcApi, RpcError  # noqa: F401
from .service import BridgeService  # noqa: F401
