"""
ConnectionManager: manages active WebSocket connections and broadcasts simulation state.

- On connect: sends `boundary` message once (if simulation has started).
- Every ~16.7ms / 60 fps (driven by SimulationManager): broadcasts a single `snapshot` message.
- On simulation finish: broadcasts `finished` and closes all connections.
"""

import json

from fastapi import WebSocket

from .grid import H3_RESOLUTION


class ConnectionManager:
    def __init__(self) -> None:
        self._connections: set[WebSocket] = set()
        self._boundary: dict | None = None

    def set_boundary(
        self,
        min_x: float, min_y: float, max_x: float, max_y: float,
        min_lat: float, min_lng: float, max_lat: float, max_lng: float,
    ) -> None:
        self._boundary = {
            "type": "boundary",
            "sumo": {"minX": min_x, "minY": min_y, "maxX": max_x, "maxY": max_y},
            "geo":  {"minLat": min_lat, "minLng": min_lng, "maxLat": max_lat, "maxLng": max_lng},
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
        await self._broadcast({
            "type": "snapshot",
            "vehicles": state["vehicles"],
            "passengers": state["passengers"],
            "sim_time": state["sim_time"],
        })

    async def broadcast_surge(self, cells: list[dict], sim_time: float) -> None:
        await self._broadcast({
            "type": "surge",
            "h3_resolution": H3_RESOLUTION,
            "cells": cells,
            "sim_time": sim_time,
        })

    async def broadcast_fare_update(
        self,
        passenger_id: str,
        taxi_id: str,
        fare: int,
        expected_fare: int,
        distance_m: float,
        sim_time: float,
    ) -> None:
        await self._broadcast({
            "type": "fare_update",
            "passenger_id": passenger_id,
            "taxi_id": taxi_id,
            "fare": fare,
            "expected_fare": expected_fare,
            "distance_m": distance_m,
            "sim_time": sim_time,
        })

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
