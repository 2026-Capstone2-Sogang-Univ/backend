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
