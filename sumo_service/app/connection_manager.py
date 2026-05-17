"""
ConnectionManager: manages active WebSocket connections and broadcasts simulation state.

- On connect: sends `boundary` message once (if simulation has started).
- Every ~16.7ms / 60 fps (driven by SimulationManager): broadcasts a single `snapshot` message.
- On simulation finish: broadcasts `finished` and closes all connections.
"""

from fastapi import WebSocket

from .grid import H3_RESOLUTION
from .ws_messages_pb2 import (
    Boundary,
    FareUpdate,
    Finished,
    GeoRect,
    PassengerMsg,
    ServerMessage,
    Snapshot,
    Surge,
    SurgeCell,
    SumoRect,
    Vehicle,
    VehicleState,
)

# 백엔드 _capture_state는 bg_* 차량을 필터링하므로 실제로는 "car"가 들어오지 않음.
# CAR enum은 mock 서버(Front/mock/server.js) 호환용으로만 유지.
_STATE_MAP = {
    "empty":      VehicleState.EMPTY,
    "dispatched": VehicleState.DISPATCHED,
    "occupied":   VehicleState.OCCUPIED,
    "car":        VehicleState.CAR,
}


class ConnectionManager:
    def __init__(self) -> None:
        self._connections: set[WebSocket] = set()
        self._boundary_bytes: bytes | None = None

    def set_boundary(
        self,
        min_x: float, min_y: float, max_x: float, max_y: float,
        min_lat: float, min_lng: float, max_lat: float, max_lng: float,
    ) -> None:
        msg = ServerMessage(boundary=Boundary(
            sumo=SumoRect(min_x=min_x, min_y=min_y, max_x=max_x, max_y=max_y),
            geo=GeoRect(min_lat=min_lat, min_lng=min_lng, max_lat=max_lat, max_lng=max_lng),
        ))
        self._boundary_bytes = msg.SerializeToString()

    def clear_boundary(self) -> None:
        self._boundary_bytes = None

    async def connect(self, ws: WebSocket) -> None:
        await ws.accept()
        self._connections.add(ws)
        if self._boundary_bytes is not None:
            await self._send_raw(ws, self._boundary_bytes)

    def disconnect(self, ws: WebSocket) -> None:
        self._connections.discard(ws)

    async def broadcast_state(self, state: dict) -> None:
        def _state_enum(s: str) -> int:
            pb = _STATE_MAP.get(s)
            if pb is None:
                print(f"[ws] unknown vehicle state: {s!r} → UNKNOWN", flush=True)
                return VehicleState.VEHICLE_STATE_UNKNOWN
            return pb

        vehicles = [
            Vehicle(
                id=v["id"], lat=v["lat"], lng=v["lng"],
                angle=v["angle"], speed=v["speed"],
                state=_state_enum(v["state"]),
            )
            for v in state["vehicles"]
        ]
        passengers = [
            PassengerMsg(
                id=p["id"], lat=p["lat"], lng=p["lng"],
                expected_fare=p["expected_fare"],
                expected_distance_m=p["expected_distance_m"],
            )
            for p in state["passengers"]
        ]
        msg = ServerMessage(snapshot=Snapshot(
            vehicles=vehicles,
            passengers=passengers,
            sim_time=state["sim_time"],
        ))
        await self._broadcast(msg)

    async def broadcast_surge(self, cells: list[dict], sim_time: float) -> None:
        if not cells:
            return
        pb_cells = [
            SurgeCell(
                h3_index=c["h3"],
                supply=c["supply"],
                demand=c["demand"],
                surge_coeff=c["surge"],
                center_lat=c["center"]["lat"],
                center_lng=c["center"]["lng"],
            )
            for c in cells
        ]
        msg = ServerMessage(surge=Surge(
            h3_resolution=H3_RESOLUTION,
            cells=pb_cells,
            sim_time=sim_time,
        ))
        await self._broadcast(msg)

    async def broadcast_fare_update(
        self,
        passenger_id: str,
        taxi_id: str,
        fare: int,
        expected_fare: int,
        distance_m: float,
        sim_time: float,
    ) -> None:
        msg = ServerMessage(fare_update=FareUpdate(
            passenger_id=passenger_id,
            taxi_id=taxi_id,
            fare=fare,
            expected_fare=expected_fare,
            distance_m=distance_m,
            sim_time=sim_time,
        ))
        await self._broadcast(msg)

    async def notify_finished(self) -> None:
        msg = ServerMessage(finished=Finished())
        await self._broadcast(msg)
        for ws in list(self._connections):
            try:
                await ws.close()
            except Exception:
                pass
        self._connections.clear()

    async def _broadcast(self, message: ServerMessage) -> None:
        data = message.SerializeToString()
        dead: list[WebSocket] = []
        for ws in list(self._connections):
            try:
                await ws.send_bytes(data)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self._connections.discard(ws)

    async def _send_raw(self, ws: WebSocket, data: bytes) -> None:
        try:
            await ws.send_bytes(data)
        except Exception:
            self._connections.discard(ws)
