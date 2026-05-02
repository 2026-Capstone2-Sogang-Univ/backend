"""
Tests for POST /simulation/start, /pause, /restart REST endpoints.

SimulationManager is replaced with a lightweight stub — no SUMO or TraCI needed.
"""

from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.simulation import SimStatus


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_manager(status: SimStatus) -> MagicMock:
    """Return a stub SimulationManager with async control methods."""
    mgr = MagicMock()
    mgr.status = status
    mgr.start = AsyncMock(side_effect=lambda: setattr(mgr, "status", SimStatus.RUNNING))
    mgr.pause = AsyncMock(side_effect=lambda: setattr(mgr, "status", SimStatus.PAUSED))
    mgr.resume = AsyncMock(side_effect=lambda: setattr(mgr, "status", SimStatus.RUNNING))
    mgr.restart = AsyncMock(side_effect=lambda: setattr(mgr, "status", SimStatus.RUNNING))
    mgr.stop = AsyncMock()
    mgr.get_state = MagicMock(return_value={"vehicles": [], "passengers": [], "sim_time": 0.0})
    mgr.get_passengers = MagicMock(return_value=[])
    mgr.get_surge = MagicMock(return_value=[])
    mgr.get_fare = MagicMock(return_value=None)
    return mgr


# ---------------------------------------------------------------------------
# POST /simulation/start
# ---------------------------------------------------------------------------

def test_start_from_idle_returns_200():
    app.state.manager = make_manager(SimStatus.IDLE)
    with TestClient(app) as client:
        resp = client.post("/simulation/start")
    assert resp.status_code == 200
    assert resp.json()["status"] == SimStatus.RUNNING


def test_start_when_already_running_returns_409():
    app.state.manager = make_manager(SimStatus.RUNNING)
    with TestClient(app) as client:
        resp = client.post("/simulation/start")
    assert resp.status_code == 409


def test_start_when_already_running_does_not_call_start():
    mgr = make_manager(SimStatus.RUNNING)
    app.state.manager = mgr
    with TestClient(app) as client:
        client.post("/simulation/start")
    mgr.start.assert_not_called()


# ---------------------------------------------------------------------------
# POST /simulation/pause
# ---------------------------------------------------------------------------

def test_pause_when_running_returns_200():
    app.state.manager = make_manager(SimStatus.RUNNING)
    with TestClient(app) as client:
        resp = client.post("/simulation/pause")
    assert resp.status_code == 200
    assert resp.json()["status"] == SimStatus.PAUSED


def test_pause_when_not_running_returns_400():
    app.state.manager = make_manager(SimStatus.IDLE)
    with TestClient(app) as client:
        resp = client.post("/simulation/pause")
    assert resp.status_code == 400


# ---------------------------------------------------------------------------
# POST /simulation/restart
# ---------------------------------------------------------------------------

def test_restart_returns_200():
    app.state.manager = make_manager(SimStatus.RUNNING)
    with TestClient(app) as client:
        resp = client.post("/simulation/restart")
    assert resp.status_code == 200
    assert resp.json()["status"] == SimStatus.RUNNING


def test_restart_calls_restart_on_manager():
    mgr = make_manager(SimStatus.IDLE)
    app.state.manager = mgr
    with TestClient(app) as client:
        client.post("/simulation/restart")
    mgr.restart.assert_called_once()


# ---------------------------------------------------------------------------
# GET /simulation/surge
# ---------------------------------------------------------------------------

def test_get_surge_returns_200():
    app.state.manager = make_manager(SimStatus.RUNNING)
    with TestClient(app) as client:
        resp = client.get("/simulation/surge")
    assert resp.status_code == 200


def test_get_surge_response_structure():
    app.state.manager = make_manager(SimStatus.RUNNING)
    with TestClient(app) as client:
        resp = client.get("/simulation/surge")
    body = resp.json()
    assert "h3_resolution" in body
    assert "cells" in body
    assert isinstance(body["cells"], list)


def test_get_surge_with_cells():
    mgr = make_manager(SimStatus.RUNNING)
    mgr.get_surge = MagicMock(return_value=[{"h3": "892830828cbffff", "supply": 3, "demand": 1,
                                              "surge": 0.5, "center": {"lat": 40.71, "lng": -74.0},
                                              "boundary": []}])
    app.state.manager = mgr
    with TestClient(app) as client:
        resp = client.get("/simulation/surge")
    assert len(resp.json()["cells"]) == 1


# ---------------------------------------------------------------------------
# GET /simulation/passengers
# ---------------------------------------------------------------------------

def test_get_passengers_returns_200():
    app.state.manager = make_manager(SimStatus.RUNNING)
    with TestClient(app) as client:
        resp = client.get("/simulation/passengers")
    assert resp.status_code == 200


def test_get_passengers_response_structure():
    app.state.manager = make_manager(SimStatus.RUNNING)
    with TestClient(app) as client:
        resp = client.get("/simulation/passengers")
    body = resp.json()
    assert "passengers" in body
    assert isinstance(body["passengers"], list)


def test_get_passengers_with_data():
    mgr = make_manager(SimStatus.RUNNING)
    mgr.get_passengers = MagicMock(return_value=[
        {"id": "p_0", "x": 100.0, "y": 200.0, "lat": 40.71, "lng": -74.0,
         "expected_fare": 5900, "expected_distance_m": 2100.5}
    ])
    app.state.manager = mgr
    with TestClient(app) as client:
        resp = client.get("/simulation/passengers")
    assert len(resp.json()["passengers"]) == 1


# ---------------------------------------------------------------------------
# GET /simulation/fare/{passenger_id}
# ---------------------------------------------------------------------------

def test_get_fare_returns_404_when_not_found():
    app.state.manager = make_manager(SimStatus.RUNNING)
    with TestClient(app) as client:
        resp = client.get("/simulation/fare/p_999")
    assert resp.status_code == 404


def test_get_fare_returns_200_when_found():
    mgr = make_manager(SimStatus.RUNNING)
    fare_record = {"passenger_id": "p_0", "taxi_id": "taxi_1",
                   "fare": 6200, "expected_fare": 5900,
                   "distance_m": 2100.5, "sim_time": 480.0}
    mgr.get_fare = MagicMock(return_value=fare_record)
    app.state.manager = mgr
    with TestClient(app) as client:
        resp = client.get("/simulation/fare/p_0")
    assert resp.status_code == 200
    assert resp.json()["fare"] == 6200


def test_get_fare_calls_manager_with_correct_id():
    mgr = make_manager(SimStatus.RUNNING)
    app.state.manager = mgr
    with TestClient(app) as client:
        client.get("/simulation/fare/p_42")
    mgr.get_fare.assert_called_once_with("p_42")
