"""Pure JSON-RPC 2.0-style dispatch and a tiny thread-safe event bus.

No I/O here: `JsonRpcApi.handle` maps a request dict to a response dict, and
`EventBus` fans published events out to subscribers. Transports (WebSocket,
in-process pywebview js_api, tests) sit on top.
"""
from __future__ import annotations

import logging
import threading
from typing import Any, Callable, Dict, Optional

log = logging.getLogger(__name__)

METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603


class RpcError(Exception):
    def __init__(self, message: str, code: int = INTERNAL_ERROR):
        super().__init__(message)
        self.code = code


class JsonRpcApi:
    def __init__(self) -> None:
        self._methods: Dict[str, Callable] = {}

    def register(self, name: str, fn: Callable) -> None:
        self._methods[name] = fn

    def method(self, name: str) -> Callable:
        def deco(fn: Callable) -> Callable:
            self.register(name, fn)
            return fn
        return deco

    def handle(self, msg: dict) -> dict:
        """One request dict in -> one response dict out. Never raises."""
        req_id = msg.get("id")
        name = msg.get("method")
        params = msg.get("params") or {}
        fn = self._methods.get(name)
        if fn is None:
            return _err(req_id, METHOD_NOT_FOUND, f"unknown method: {name!r}")
        try:
            result = fn(**params) if isinstance(params, dict) else fn(*params)
            return {"jsonrpc": "2.0", "id": req_id, "result": result}
        except RpcError as exc:
            return _err(req_id, exc.code, str(exc))
        except TypeError as exc:
            return _err(req_id, INVALID_PARAMS, str(exc))
        except Exception as exc:  # noqa: BLE001 - API boundary
            log.exception("rpc method %s failed", name)
            return _err(req_id, INTERNAL_ERROR, str(exc))


def _err(req_id, code: int, message: str) -> dict:
    return {"jsonrpc": "2.0", "id": req_id,
            "error": {"code": code, "message": message}}


class EventBus:
    """Topic -> listeners fan-out. Publishers may call from any thread; each
    listener is responsible for marshalling into its own context (the WS
    transport does this with call_soon_threadsafe)."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._listeners: Dict[str, list] = {}

    def subscribe(self, topic: str, fn: Callable[[Any], None]) -> Callable[[], None]:
        with self._lock:
            self._listeners.setdefault(topic, []).append(fn)

        def unsubscribe() -> None:
            with self._lock:
                try:
                    self._listeners.get(topic, []).remove(fn)
                except ValueError:
                    pass
        return unsubscribe

    def publish(self, topic: str, data: Any = None) -> None:
        with self._lock:
            listeners = list(self._listeners.get(topic, ()))
        for fn in listeners:
            try:
                fn(data)
            except Exception:  # noqa: BLE001 - one bad listener must not stop fan-out
                log.exception("event listener failed for topic %s", topic)
