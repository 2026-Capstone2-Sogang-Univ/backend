"""
Tests for ConnectionManager.

All tests use mock WebSocket objects — no SUMO or network access required.
"""

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.connection_manager import ConnectionManager


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_ws() -> MagicMock:
    """Return a mock WebSocket with async accept/send_text/close."""
    ws = MagicMock()
    ws.accept = AsyncMock()
    ws.send_text = AsyncMock()
    ws.close = AsyncMock()
    return ws


def sent_messages(ws: MagicMock) -> list[dict]:
    """Return all messages sent to a mock WebSocket as parsed dicts."""
    return [json.loads(call[0][0]) for call in ws.send_text.call_args_list]


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
    """boundary is only sent once the simulation has set it."""
    manager = ConnectionManager()
    ws = make_ws()
    await manager.connect(ws)
    ws.send_text.assert_not_called()


async def test_connect_sends_boundary_when_available():
    manager = ConnectionManager()
    manager.set_boundary(100.0, 200.0, 300.0, 400.0)
    ws = make_ws()
    await manager.connect(ws)
    msgs = sent_messages(ws)
    assert len(msgs) == 1
    assert msgs[0] == {
        "type": "boundary",
        "minX": 100.0,
        "minY": 200.0,
        "maxX": 300.0,
        "maxY": 400.0,
    }


async def test_disconnect_removes_client():
    manager = ConnectionManager()
    ws = make_ws()
    await manager.connect(ws)
    manager.disconnect(ws)
    assert ws not in manager._connections


async def test_disconnect_nonexistent_client_is_safe():
    manager = ConnectionManager()
    ws = make_ws()
    manager.disconnect(ws)  # should not raise


# ---------------------------------------------------------------------------
# set_boundary / clear_boundary
# ---------------------------------------------------------------------------

async def test_set_boundary_updates_boundary():
    manager = ConnectionManager()
    manager.set_boundary(1.0, 2.0, 3.0, 4.0)
    assert manager._boundary == {
        "type": "boundary",
        "minX": 1.0,
        "minY": 2.0,
        "maxX": 3.0,
        "maxY": 4.0,
    }


async def test_clear_boundary_removes_boundary():
    manager = ConnectionManager()
    manager.set_boundary(1.0, 2.0, 3.0, 4.0)
    manager.clear_boundary()
    assert manager._boundary is None


async def test_late_connect_after_boundary_set():
    """Client that connects after simulation start must still receive boundary."""
    manager = ConnectionManager()
    ws_early = make_ws()
    await manager.connect(ws_early)

    manager.set_boundary(10.0, 20.0, 30.0, 40.0)

    ws_late = make_ws()
    await manager.connect(ws_late)
    msgs = sent_messages(ws_late)
    assert msgs[0]["type"] == "boundary"


# ---------------------------------------------------------------------------
# broadcast_state
# ---------------------------------------------------------------------------

async def test_broadcast_state_sends_vehicles_and_passengers():
    manager = ConnectionManager()
    ws = make_ws()
    await manager.connect(ws)
    ws.send_text.reset_mock()

    state = {
        "vehicles": [{"id": "taxi_0", "x": 1.0, "y": 2.0, "angle": 90.0, "state": "empty"}],
        "passengers": [{"id": "p_0", "x": 3.0, "y": 4.0}],
    }
    await manager.broadcast_state(state)

    msgs = sent_messages(ws)
    types = {m["type"] for m in msgs}
    assert types == {"vehicles", "passengers"}


async def test_broadcast_state_vehicles_payload():
    manager = ConnectionManager()
    ws = make_ws()
    await manager.connect(ws)
    ws.send_text.reset_mock()

    vehicle = {"id": "taxi_1", "x": 5.0, "y": 6.0, "angle": 45.0, "state": "dispatched"}
    await manager.broadcast_state({"vehicles": [vehicle], "passengers": []})

    vehicles_msg = next(m for m in sent_messages(ws) if m["type"] == "vehicles")
    assert vehicles_msg["vehicles"] == [vehicle]


async def test_broadcast_state_passengers_payload():
    manager = ConnectionManager()
    ws = make_ws()
    await manager.connect(ws)
    ws.send_text.reset_mock()

    passenger = {"id": "p_1", "x": 7.0, "y": 8.0}
    await manager.broadcast_state({"vehicles": [], "passengers": [passenger]})

    passengers_msg = next(m for m in sent_messages(ws) if m["type"] == "passengers")
    assert passengers_msg["passengers"] == [passenger]


async def test_broadcast_state_reaches_all_clients():
    manager = ConnectionManager()
    ws1, ws2 = make_ws(), make_ws()
    await manager.connect(ws1)
    await manager.connect(ws2)
    ws1.send_text.reset_mock()
    ws2.send_text.reset_mock()

    await manager.broadcast_state({"vehicles": [], "passengers": []})

    assert ws1.send_text.call_count == 2  # vehicles + passengers
    assert ws2.send_text.call_count == 2


async def test_broadcast_state_empty_lists():
    """Empty vehicles/passengers must still produce well-formed messages."""
    manager = ConnectionManager()
    ws = make_ws()
    await manager.connect(ws)
    ws.send_text.reset_mock()

    await manager.broadcast_state({"vehicles": [], "passengers": []})

    msgs = sent_messages(ws)
    vehicles_msg = next(m for m in msgs if m["type"] == "vehicles")
    passengers_msg = next(m for m in msgs if m["type"] == "passengers")
    assert vehicles_msg["vehicles"] == []
    assert passengers_msg["passengers"] == []


# ---------------------------------------------------------------------------
# notify_finished
# ---------------------------------------------------------------------------

async def test_notify_finished_sends_finished_message():
    manager = ConnectionManager()
    ws = make_ws()
    await manager.connect(ws)
    ws.send_text.reset_mock()

    await manager.notify_finished()

    types = {m["type"] for m in sent_messages(ws)}
    assert "finished" in types


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
    """If ws.close() raises, notify_finished must still complete."""
    manager = ConnectionManager()
    ws = make_ws()
    ws.close = AsyncMock(side_effect=RuntimeError("already closed"))
    await manager.connect(ws)

    await manager.notify_finished()  # must not raise

    assert len(manager._connections) == 0


# ---------------------------------------------------------------------------
# Dead connection cleanup
# ---------------------------------------------------------------------------

async def test_dead_connection_removed_on_broadcast():
    """A client whose send_text raises is silently removed from the active set."""
    manager = ConnectionManager()
    ws_alive = make_ws()
    ws_dead = make_ws()
    ws_dead.send_text = AsyncMock(side_effect=RuntimeError("connection lost"))

    await manager.connect(ws_alive)
    await manager.connect(ws_dead)
    ws_alive.send_text.reset_mock()

    await manager.broadcast_state({"vehicles": [], "passengers": []})

    assert ws_dead not in manager._connections
    assert ws_alive in manager._connections
