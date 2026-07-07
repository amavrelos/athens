"""WebSocket transport for the BridgeService (optional `websockets` dep).

Wire format, one JSON object per message:
  request:      {"id": 1, "method": "get_state", "params": {...}}
  response:     {"jsonrpc": "2.0", "id": 1, "result": ...} | {"error": {...}}
  subscribe:    {"id": 2, "method": "subscribe", "params": {"topics": ["value"]}}
  event push:   {"event": "value", "data": {...}}

Bus events are marshalled into the asyncio loop via call_soon_threadsafe (they
originate on MIDI/OSC/bridge threads) and fanned out per-connection.
"""
from __future__ import annotations

import asyncio
import json
import logging

from .service import BridgeService

log = logging.getLogger(__name__)


async def _client_session(ws, service: BridgeService, loop) -> None:
    queue: asyncio.Queue = asyncio.Queue()
    unsubscribers: list = []

    def listener_for(topic: str):
        def listener(data) -> None:
            loop.call_soon_threadsafe(queue.put_nowait,
                                      {"event": topic, "data": data})
        return listener

    async def sender() -> None:
        while True:
            await ws.send(json.dumps(await queue.get()))

    send_task = asyncio.ensure_future(sender())
    try:
        async for raw in ws:
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                await ws.send(json.dumps({"error": {"message": "bad json"}}))
                continue
            method = msg.get("method")
            if method == "subscribe":
                for topic in (msg.get("params") or {}).get("topics", []):
                    unsubscribers.append(
                        service.bus.subscribe(topic, listener_for(topic)))
                await ws.send(json.dumps({"jsonrpc": "2.0", "id": msg.get("id"),
                                          "result": {"subscribed": True}}))
            else:
                await ws.send(json.dumps(service.rpc.handle(msg)))
    finally:
        send_task.cancel()
        for unsub in unsubscribers:
            unsub()


async def serve_async(service: BridgeService, host: str = "127.0.0.1",
                      port: int = 8765) -> None:
    import websockets  # lazy: only the serve path needs it

    loop = asyncio.get_running_loop()

    async def handler(ws) -> None:
        await _client_session(ws, service, loop)

    async with websockets.serve(handler, host, port):
        log.info("API listening on ws://%s:%d", host, port)
        await asyncio.Future()   # run until cancelled


def serve(service: BridgeService, host: str = "127.0.0.1",
          port: int = 8765) -> None:
    asyncio.run(serve_async(service, host, port))
