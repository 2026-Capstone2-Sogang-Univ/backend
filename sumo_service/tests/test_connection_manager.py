"""
Tests for ConnectionManager.

All tests use mock WebSocket objects — no SUMO or network access required.
"""

from unittest.mock import AsyncMock, MagicMock

import pytest

from app.connection_manager import ConnectionManager
from app.ws_messages_pb2 import ServerMessage


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_ws() -> MagicMock:
    ws = MagicMock()
    ws.accept     = AsyncMock()
    ws.send_bytes = AsyncMock()
    ws.close      = AsyncMock()
    return ws


def sent_messages(ws: MagicMock) -> list[ServerMessage]:
    return [
        ServerMessage.FromString(call[0][0])
        for call in ws.send_bytes.call_args_list
    ]


def payload_case(msg: ServerMessage) -> str:
    return msg.WhichOneof("payload")


# ---------------------------------------------------------------------------
# connect / disconnect
# ---------------------------------------------------------------------------

async def test_connect_accepts_websocket():
    manager = ConnectionManager()
    ws = make_ws()
    await manager.connect(ws)
    ws.accept.assert_called_once()


async def test_connect_adds_to_active_connections():
    manager = ConnectionManager()
    ws = make_ws()
    await manager.connect(ws)
    assert ws in manager._connections


async def test_connect_no_boundary_sent_before_simulation():
    manager = ConnectionManager()
    ws = make_ws()
    await manager.connect(ws)
    ws.send_bytes.assert_not_called()


async def test_connect_sends_boundary_when_available():
    manager = ConnectionManager()
    manager.set_boundary(100.0, 200.0, 300.0, 400.0, 40.70, -74.02, 40.73, -73.97)
    ws = make_ws()
    await manager.connect(ws)
    msgs = sent_messages(ws)
    assert len(msgs) == 1
    assert payload_case(msgs[0]) == "boundary"
    b = msgs[0].boundary
    assert b.sumo.min_x == 100.0
    assert b.geo.min_lat == 40.70


async def test_disconnect_removes_client():
    manager = ConnectionManager()
    ws = make_ws()
    await manager.connect(ws)
    manager.disconnect(ws)
    assert ws not in manager._connections


async def test_disconnect_nonexistent_client_is_safe():
    manager = ConnectionManager()
    ws = make_ws()
    manager.disconnect(ws)


# ---------------------------------------------------------------------------
# set_boundary / clear_boundary
# ---------------------------------------------------------------------------

async def test_set_boundary_stores_serialized_bytes():
    manager = ConnectionManager()
    manager.set_boundary(1.0, 2.0, 3.0, 4.0, 40.70, -74.02, 40.73, -73.97)
    assert manager._boundary_bytes is not None
    msg = ServerMessage.FromString(manager._boundary_bytes)
    assert payload_case(msg) == "boundary"
    assert msg.boundary.sumo.min_x == 1.0


async def test_clear_boundary_removes_boundary():
    manager = ConnectionManager()
    manager.set_boundary(1.0, 2.0, 3.0, 4.0, 40.70, -74.02, 40.73, -73.97)
    manager.clear_boundary()
    assert manager._boundary_bytes is None


async def test_clear_boundary_new_connect_sends_nothing():
    manager = ConnectionManager()
    manager.set_boundary(1.0, 2.0, 3.0, 4.0, 40.70, -74.02, 40.73, -73.97)
    manager.clear_boundary()
    ws = make_ws()
    await manager.connect(ws)
    ws.send_bytes.assert_not_called()


async def test_late_connect_after_boundary_set():
    manager = ConnectionManager()
    ws_early = make_ws()
    await manager.connect(ws_early)

    manager.set_boundary(10.0, 20.0, 30.0, 40.0, 40.70, -74.02, 40.73, -73.97)

    ws_late = make_ws()
    await manager.connect(ws_late)
    msgs = sent_messages(ws_late)
    assert payload_case(msgs[0]) == "boundary"


# ---------------------------------------------------------------------------
# broadcast_state
# ---------------------------------------------------------------------------

async def test_broadcast_state_sends_snapshot():
    manager = ConnectionManager()
    ws = make_ws()
    await manager.connect(ws)
    ws.send_bytes.reset_mock()

    await manager.broadcast_state({
        "vehicles": [{"id": "taxi_0", "lat": 40.716, "lng": -74.001,
                      "angle": 90.0, "speed": 5.0, "state": "empty"}],
        "passengers": [],
        "sim_time": 300.0,
    })

    msgs = sent_messages(ws)
    assert len(msgs) == 1
    assert payload_case(msgs[0]) == "snapshot"


async def test_broadcast_state_vehicles_payload():
    manager = ConnectionManager()
    ws = make_ws()
    await manager.connect(ws)
    ws.send_bytes.reset_mock()

    await manager.broadcast_state({
        "vehicles": [{"id": "taxi_1", "lat": 40.716, "lng": -74.001,
                      "angle": 45.0, "speed": 5.2, "state": "dispatched"}],
        "passengers": [],
        "sim_time": 0.0,
    })

    snap = sent_messages(ws)[0].snapshot
    assert len(snap.vehicles) == 1
    v = snap.vehicles[0]
    assert v.id == "taxi_1"
    assert v.lat == pytest.approx(40.716)
    assert v.state == 2  # VehicleState.DISPATCHED


async def test_broadcast_state_passengers_payload():
    manager = ConnectionManager()
    ws = make_ws()
    await manager.connect(ws)
    ws.send_bytes.reset_mock()

    await manager.broadcast_state({
        "vehicles": [],
        "passengers": [{"id": "p_1", "lat": 40.715, "lng": -74.002,
                        "expected_fare": 5900, "expected_distance_m": 2100.5}],
        "sim_time": 0.0,
    })

    snap = sent_messages(ws)[0].snapshot
    assert len(snap.passengers) == 1
    p = snap.passengers[0]
    assert p.id == "p_1"
    assert p.expected_fare == 5900
    assert p.expected_distance_m == pytest.approx(2100.5)


async def test_broadcast_state_reaches_all_clients():
    manager = ConnectionManager()
    ws1, ws2 = make_ws(), make_ws()
    await manager.connect(ws1)
    await manager.connect(ws2)
    ws1.send_bytes.reset_mock()
    ws2.send_bytes.reset_mock()

    await manager.broadcast_state({"vehicles": [], "passengers": [], "sim_time": 0.0})

    assert ws1.send_bytes.call_count == 1
    assert ws2.send_bytes.call_count == 1


async def test_broadcast_state_empty_lists():
    manager = ConnectionManager()
    ws = make_ws()
    await manager.connect(ws)
    ws.send_bytes.reset_mock()

    await manager.broadcast_state({"vehicles": [], "passengers": [], "sim_time": 0.0})

    snap = sent_messages(ws)[0].snapshot
    assert list(snap.vehicles) == []
    assert list(snap.passengers) == []


async def test_broadcast_state_unknown_vehicle_state(capsys):
    manager = ConnectionManager()
    ws = make_ws()
    await manager.connect(ws)
    ws.send_bytes.reset_mock()

    await manager.broadcast_state({
        "vehicles": [{"id": "taxi_0", "lat": 40.7, "lng": -74.0,
                      "angle": 0.0, "speed": 0.0, "state": "unknown_state"}],
        "passengers": [],
        "sim_time": 0.0,
    })

    captured = capsys.readouterr()
    assert "[ws] unknown vehicle state" in captured.out
    snap = sent_messages(ws)[0].snapshot
    assert snap.vehicles[0].state == 0  # VEHICLE_STATE_UNKNOWN


# ---------------------------------------------------------------------------
# notify_finished
# ---------------------------------------------------------------------------

async def test_notify_finished_sends_finished_message():
    manager = ConnectionManager()
    ws = make_ws()
    await manager.connect(ws)
    ws.send_bytes.reset_mock()

    await manager.notify_finished()

    cases = {payload_case(m) for m in sent_messages(ws)}
    assert "finished" in cases


async def test_notify_finished_closes_all_connections():
    manager = ConnectionManager()
    ws1, ws2 = make_ws(), make_ws()
    await manager.connect(ws1)
    await manager.connect(ws2)

    await manager.notify_finished()

    ws1.close.assert_called_once()
    ws2.close.assert_called_once()


async def test_notify_finished_clears_connections():
    manager = ConnectionManager()
    ws = make_ws()
    await manager.connect(ws)
    await manager.notify_finished()
    assert len(manager._connections) == 0


async def test_notify_finished_tolerates_close_error():
    manager = ConnectionManager()
    ws = make_ws()
    ws.close = AsyncMock(side_effect=RuntimeError("already closed"))
    await manager.connect(ws)
    await manager.notify_finished()
    assert len(manager._connections) == 0


# ---------------------------------------------------------------------------
# Dead connection cleanup
# ---------------------------------------------------------------------------

async def test_dead_connection_removed_on_broadcast():
    manager = ConnectionManager()
    ws_alive = make_ws()
    ws_dead  = make_ws()
    ws_dead.send_bytes = AsyncMock(side_effect=RuntimeError("connection lost"))

    await manager.connect(ws_alive)
    await manager.connect(ws_dead)
    ws_alive.send_bytes.reset_mock()

    await manager.broadcast_state({"vehicles": [], "passengers": [], "sim_time": 0.0})

    assert ws_dead  not in manager._connections
    assert ws_alive in     manager._connections


async def test_dead_connection_subsequent_broadcast_succeeds():
    manager = ConnectionManager()
    ws_alive = make_ws()
    ws_dead  = make_ws()
    ws_dead.send_bytes = AsyncMock(side_effect=RuntimeError("connection lost"))

    await manager.connect(ws_alive)
    await manager.connect(ws_dead)

    await manager.broadcast_state({"vehicles": [], "passengers": [], "sim_time": 0.0})
    ws_alive.send_bytes.reset_mock()

    await manager.broadcast_state({"vehicles": [], "passengers": [], "sim_time": 1.0})

    assert ws_alive.send_bytes.call_count == 1


# ---------------------------------------------------------------------------
# broadcast_surge
# ---------------------------------------------------------------------------

async def test_broadcast_surge_sends_correct_type():
    manager = ConnectionManager()
    ws = make_ws()
    await manager.connect(ws)
    ws.send_bytes.reset_mock()

    cells = [{"h3": "abc123", "supply": 1, "demand": 1, "surge": 1.0,
              "center": {"lat": 40.7, "lng": -74.0}}]
    await manager.broadcast_surge(cells, sim_time=100.0)

    assert payload_case(sent_messages(ws)[0]) == "surge"


async def test_broadcast_surge_payload():
    manager = ConnectionManager()
    ws = make_ws()
    await manager.connect(ws)
    ws.send_bytes.reset_mock()

    cells = [{"h3": "abc123", "supply": 3, "demand": 1, "surge": 0.5,
              "center": {"lat": 40.7, "lng": -74.0}}]
    await manager.broadcast_surge(cells, sim_time=300.0)

    surge = sent_messages(ws)[0].surge
    assert surge.sim_time == 300.0
    assert len(surge.cells) == 1
    assert surge.cells[0].h3_index == "abc123"
    assert surge.cells[0].supply == 3
    assert surge.cells[0].center_lat == pytest.approx(40.7)


async def test_broadcast_surge_empty_cells_sends_nothing():
    manager = ConnectionManager()
    ws = make_ws()
    await manager.connect(ws)
    ws.send_bytes.reset_mock()

    await manager.broadcast_surge([], sim_time=0.0)
    ws.send_bytes.assert_not_called()


# ---------------------------------------------------------------------------
# broadcast_fare_update
# ---------------------------------------------------------------------------

async def test_broadcast_fare_update_sends_correct_type():
    manager = ConnectionManager()
    ws = make_ws()
    await manager.connect(ws)
    ws.send_bytes.reset_mock()

    await manager.broadcast_fare_update("p_0", "taxi_0", 845, 775, 1500.0, 500.0)

    assert payload_case(sent_messages(ws)[0]) == "fare_update"


async def test_broadcast_fare_update_payload():
    manager = ConnectionManager()
    ws = make_ws()
    await manager.connect(ws)
    ws.send_bytes.reset_mock()

    await manager.broadcast_fare_update(
        passenger_id="p_42", taxi_id="taxi_7",
        fare=845, expected_fare=775,
        distance_m=1500.0, sim_time=500.0,
    )

    fu = sent_messages(ws)[0].fare_update
    assert fu.passenger_id == "p_42"
    assert fu.taxi_id == "taxi_7"
    assert fu.fare == 845
    assert fu.expected_fare == 775
    assert fu.distance_m == pytest.approx(1500.0)
    assert fu.sim_time == pytest.approx(500.0)
