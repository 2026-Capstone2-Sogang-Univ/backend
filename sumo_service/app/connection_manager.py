"""
ConnectionManager: manages active WebSocket connections and broadcasts simulation state.

- On connect: sends `boundary` message once (if simulation has started).
- Every 100ms (driven by SimulationManager): broadcasts `vehicles` and `passengers` messages.
- On simulation finish: broadcasts `finished` and closes all connections.
"""

import json

from fastapi import WebSocket


class ConnectionManager:
    def __init__(self) -> None:
        self._connections: set[WebSocket] = set()
        self._boundary: dict | None = None

    def set_boundary(self, min_x: float, min_y: float, max_x: float, max_y: float) -> None:
        self._boundary = {
            "type": "boundary",
            "minX": min_x,
            "minY": min_y,
            "maxX": max_x,
            "maxY": max_y,
        }

    def clear_boundary(self) -> None:
        self._boundary = None

    async def connect(self, ws: WebSocket) -> None:
        await ws.accept()
        self._connections.add(ws)
        if self._boundary is not None:
            await self._send(ws, self._boundary)

    def disconnect(self, ws: WebSocket) -> None:
        self._connections.discard(ws)

    async def broadcast_state(self, state: dict) -> None:
        await self._broadcast({"type": "vehicles", "vehicles": state["vehicles"]})
        await self._broadcast({"type": "passengers", "passengers": state["passengers"]})

    async def notify_finished(self) -> None:
        await self._broadcast({"type": "finished"})
        for ws in list(self._connections):
            try:
                await ws.close()
            except Exception:
                pass
        self._connections.clear()

    async def _broadcast(self, message: dict) -> None:
        text = json.dumps(message)
        dead: list[WebSocket] = []
        for ws in list(self._connections):
            try:
                await ws.send_text(text)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self._connections.discard(ws)

    async def _send(self, ws: WebSocket, message: dict) -> None:
        try:
            await ws.send_text(json.dumps(message))
        except Exception:
            self._connections.discard(ws)
