"""
Tests for POST /simulation/start, /pause, /restart REST endpoints.

SimulationManager is replaced with a lightweight stub — no SUMO or TraCI needed.
"""

from unittest.mock import AsyncMock, MagicMock, patch

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
    mgr.start = AsyncMock(side_effect=lambda *args, **kwargs: setattr(mgr, "status", SimStatus.RUNNING))
    mgr.pause = AsyncMock(side_effect=lambda: setattr(mgr, "status", SimStatus.PAUSED))
    mgr.resume = AsyncMock(side_effect=lambda: setattr(mgr, "status", SimStatus.RUNNING))
    mgr.restart = AsyncMock(side_effect=lambda *args, **kwargs: setattr(mgr, "status", SimStatus.RUNNING))
    mgr.stop = AsyncMock(side_effect=lambda: setattr(mgr, "status", SimStatus.IDLE))
    mgr.get_state = MagicMock(return_value={"vehicles": [], "passengers": [], "sim_time": 0.0})
    mgr.get_status_summary = MagicMock(side_effect=lambda: {
        "status": mgr.status,
        "vehicles": [],
        "passengers": [],
        "sim_time": 0.0,
        "frame_rate": 60.0,
        "simulation_speed": 20.0,
        "vehicle_count": 0,
        "taxi_count": 0,
        "background_vehicle_count": 0,
        "configured_background_vehicle_count": 0,
        "empty_taxi_count": 0,
        "dispatched_taxi_count": 0,
        "occupied_taxi_count": 0,
        "waiting_passenger_count": 0,
        "assigned_passenger_count": 0,
        "completed_trip_count": 0,
        "h3_resolution": 9,
        "passenger_source": "random",
    })
    mgr.get_passengers = MagicMock(return_value=[])
    mgr.get_surge = MagicMock(return_value=[])
    mgr.get_fare = MagicMock(return_value=None)
    mgr.get_kpi_summary = MagicMock(return_value={
        "sim_time": 0.0,
        "h3_resolution": 9,
        "matching": {
            "target_rate": 0.725,
            "actual_rate": 0.0,
            "matching_rate_error": -0.725,
            "request_count": 0,
            "matched_count": 0,
            "by_raw_bucket": [],
        },
        "idle_time": {},
        "driver_revenue": {},
        "passenger_waiting_incentive": {},
    })
    mgr.enqueue_manual_passenger = MagicMock(return_value="upax_1")
    mgr.enqueue_manual_taxi = MagicMock(return_value="utaxi_1")
    mgr.quote_manual_passenger = MagicMock(return_value={
        "ok": True,
        "expected_fare": 8200,
        "expected_distance_m": 3100,
        "estimated_wait_sec": 95,
        "surge_multiplier": 1.36,
        "incentive_limit": 3000,
        "total_amount": 11200,
    })
    mgr.create_manual_passenger = MagicMock(return_value={"ok": True, "passenger_id": "upax_1"})
    mgr.create_manual_taxi = MagicMock(return_value={"ok": True, "taxi_id": "utaxi_1"})
    mgr.get_taxi_standby_context = MagicMock(return_value=None)
    mgr.get_taxi_call_detail = MagicMock(return_value=None)
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


def test_start_response_contains_status_summary_fields():
    app.state.manager = make_manager(SimStatus.IDLE)
    with TestClient(app) as client:
        resp = client.post("/simulation/start")
    body = resp.json()
    assert body["vehicle_count"] == 0
    assert body["waiting_passenger_count"] == 0
    assert body["h3_resolution"] == 9
    assert body["passenger_source"] == "random"


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


def test_start_accepts_frontend_lab_body():
    mgr = make_manager(SimStatus.IDLE)
    app.state.manager = mgr
    payload = {
        "duration": 1200,
        "seed": 7,
        "passenger_source": "random",
        "target_matching_rates": {
            "raw_lt_1_5": 0.55,
            "raw_lt_2_5": 0.70,
            "raw_lt_3_5": 0.80,
            "raw_gte_3_5": 0.85,
        },
        "pricing_policy": {
            "epsilon": -0.6,
            "surge_min": 1.2,
            "surge_max": 4.9,
            "alpha_sensitivity": 1.0,
        },
        "taxi_count": 150,
        "background_vehicle_count": 600,
        "passengers_per_5min": 50,
        "simulation_speed": 30,
        "initial_passenger_count": 60,
    }
    with TestClient(app) as client:
        resp = client.post("/simulation/start", json=payload)
    assert resp.status_code == 200
    assert mgr.start.call_args.args[0].duration == 1200
    assert mgr.start.call_args.args[0].target_matching_rates["raw_gte_3_5"] == 0.85
    assert mgr.start.call_args.args[0].taxi_count == 150
    assert mgr.start.call_args.args[0].background_vehicle_count == 600
    assert mgr.start.call_args.args[0].passengers_per_5min == 50
    assert mgr.start.call_args.args[0].simulation_speed == 30


def test_start_rejects_invalid_passenger_source():
    mgr = make_manager(SimStatus.IDLE)
    app.state.manager = mgr
    with TestClient(app) as client:
        resp = client.post("/simulation/start", json={"passenger_source": "invalid"})
    assert resp.status_code == 400
    assert resp.json()["error"] == "invalid_request"
    mgr.start.assert_not_called()


def test_start_rejects_taxi_count_above_limit():
    mgr = make_manager(SimStatus.IDLE)
    app.state.manager = mgr
    with TestClient(app) as client:
        resp = client.post("/simulation/start", json={"taxi_count": 1001})
    assert resp.status_code == 400
    assert resp.json()["error"] == "invalid_request"
    mgr.start.assert_not_called()


def test_start_rejects_background_vehicle_count_above_limit():
    mgr = make_manager(SimStatus.IDLE)
    app.state.manager = mgr
    with TestClient(app) as client:
        resp = client.post("/simulation/start", json={"background_vehicle_count": 3001})
    assert resp.status_code == 400
    assert resp.json()["error"] == "invalid_request"
    mgr.start.assert_not_called()


def test_start_rejects_passengers_per_5min_above_limit():
    mgr = make_manager(SimStatus.IDLE)
    app.state.manager = mgr
    with TestClient(app) as client:
        resp = client.post("/simulation/start", json={"passengers_per_5min": 1001})
    assert resp.status_code == 400
    assert resp.json()["error"] == "invalid_request"
    mgr.start.assert_not_called()


def test_start_rejects_simulation_speed_above_limit():
    mgr = make_manager(SimStatus.IDLE)
    app.state.manager = mgr
    with TestClient(app) as client:
        resp = client.post("/simulation/start", json={"simulation_speed": 121})
    assert resp.status_code == 400
    assert resp.json()["error"] == "invalid_request"
    mgr.start.assert_not_called()


def test_start_rejects_initial_passenger_count_above_limit():
    mgr = make_manager(SimStatus.IDLE)
    app.state.manager = mgr
    with TestClient(app) as client:
        resp = client.post("/simulation/start", json={"initial_passenger_count": 5001})
    assert resp.status_code == 400
    assert resp.json()["error"] == "invalid_request"
    mgr.start.assert_not_called()


def test_start_rejects_zero_pricing_epsilon():
    mgr = make_manager(SimStatus.IDLE)
    app.state.manager = mgr
    with TestClient(app) as client:
        resp = client.post("/simulation/start", json={"pricing_policy": {"epsilon": 0}})
    assert resp.status_code == 400
    assert resp.json()["error"] == "invalid_request"
    mgr.start.assert_not_called()


def test_start_rejects_surge_min_above_default_max():
    mgr = make_manager(SimStatus.IDLE)
    app.state.manager = mgr
    with TestClient(app) as client:
        resp = client.post("/simulation/start", json={"pricing_policy": {"surge_min": 5.0}})
    assert resp.status_code == 400
    assert resp.json()["error"] == "invalid_request"
    mgr.start.assert_not_called()


def test_start_rejects_surge_max_below_default_min():
    mgr = make_manager(SimStatus.IDLE)
    app.state.manager = mgr
    with TestClient(app) as client:
        resp = client.post("/simulation/start", json={"pricing_policy": {"surge_max": 1.1}})
    assert resp.status_code == 400
    assert resp.json()["error"] == "invalid_request"
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


def test_get_h3_regions_returns_display_name_lookup():
    app.state.manager = make_manager(SimStatus.RUNNING)
    with TestClient(app) as client:
        resp = client.get("/simulation/h3-regions")

    assert resp.status_code == 200
    body = resp.json()
    assert body["h3_resolution"] == 9
    assert body["fallback"] == "lat_lng"
    assert isinstance(body["regions"], dict)
    assert len(body["regions"]) == 564
    first_region = next(iter(body["regions"].values()))
    assert set(first_region) == {"name", "display_name", "lat", "lng"}


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


# ---------------------------------------------------------------------------
# POST /simulation/resume
# ---------------------------------------------------------------------------

def test_resume_when_paused_returns_200():
    app.state.manager = make_manager(SimStatus.PAUSED)
    with TestClient(app) as client:
        resp = client.post("/simulation/resume")
    assert resp.status_code == 200
    assert resp.json()["status"] == SimStatus.RUNNING


def test_resume_when_not_paused_returns_400():
    app.state.manager = make_manager(SimStatus.IDLE)
    with TestClient(app) as client:
        resp = client.post("/simulation/resume")
    assert resp.status_code == 400


def test_resume_when_running_returns_400():
    app.state.manager = make_manager(SimStatus.RUNNING)
    with TestClient(app) as client:
        resp = client.post("/simulation/resume")
    assert resp.status_code == 400


def test_resume_calls_resume_on_manager():
    mgr = make_manager(SimStatus.PAUSED)
    app.state.manager = mgr
    with TestClient(app) as client:
        client.post("/simulation/resume")
    mgr.resume.assert_called_once()


# ---------------------------------------------------------------------------
# GET /simulation/status
# ---------------------------------------------------------------------------

def test_get_status_returns_200():
    app.state.manager = make_manager(SimStatus.RUNNING)
    with TestClient(app) as client:
        resp = client.get("/simulation/status")
    assert resp.status_code == 200


def test_get_status_contains_status_field():
    app.state.manager = make_manager(SimStatus.RUNNING)
    with TestClient(app) as client:
        resp = client.get("/simulation/status")
    assert "status" in resp.json()


def test_get_status_contains_snapshot_fields():
    app.state.manager = make_manager(SimStatus.RUNNING)
    with TestClient(app) as client:
        resp = client.get("/simulation/status")
    body = resp.json()
    assert "vehicles" in body
    assert "passengers" in body
    assert "sim_time" in body


def test_get_status_uses_manager_status_summary():
    mgr = make_manager(SimStatus.RUNNING)
    app.state.manager = mgr
    with TestClient(app) as client:
        client.get("/simulation/status")
    mgr.get_status_summary.assert_called_once()


def test_get_status_contains_summary_fields():
    app.state.manager = make_manager(SimStatus.RUNNING)
    with TestClient(app) as client:
        resp = client.get("/simulation/status")
    body = resp.json()
    assert "vehicle_count" in body
    assert "taxi_count" in body
    assert "empty_taxi_count" in body
    assert "dispatched_taxi_count" in body
    assert "occupied_taxi_count" in body
    assert "waiting_passenger_count" in body
    assert "assigned_passenger_count" in body
    assert "completed_trip_count" in body


def test_get_status_reflects_paused_state():
    app.state.manager = make_manager(SimStatus.PAUSED)
    with TestClient(app) as client:
        resp = client.get("/simulation/status")
    assert resp.json()["status"] == SimStatus.PAUSED


# ---------------------------------------------------------------------------
# GET /simulation/kpi
# ---------------------------------------------------------------------------

def test_get_kpi_returns_200():
    app.state.manager = make_manager(SimStatus.RUNNING)
    with TestClient(app) as client:
        resp = client.get("/simulation/kpi")
    assert resp.status_code == 200
    assert "matching" in resp.json()


# ---------------------------------------------------------------------------
# POST /simulation/passengers and /taxis
# ---------------------------------------------------------------------------

def test_quote_passenger_returns_frontend_contract_fields():
    app.state.manager = make_manager(SimStatus.RUNNING)
    with TestClient(app) as client:
        resp = client.post("/simulation/passengers/quote", json={
            "pickup": {"lat": 40.75, "lng": -73.98},
            "dropoff": {"lat": 40.76, "lng": -73.97},
            "incentive_limit": 3000,
        })
    assert resp.status_code == 200
    assert resp.json() == {
        "expected_fare": 8200,
        "expected_distance_m": 3100,
        "estimated_wait_sec": 95,
        "surge_multiplier": 1.36,
        "incentive_limit": 3000,
        "total_amount": 11200,
    }


def test_quote_passenger_maps_manager_error_to_400():
    mgr = make_manager(SimStatus.RUNNING)
    mgr.quote_manual_passenger = MagicMock(return_value={
        "ok": False,
        "error": "no_route_found",
        "message": "No route found between pickup and dropoff.",
    })
    app.state.manager = mgr
    with TestClient(app) as client:
        resp = client.post("/simulation/passengers/quote", json={
            "pickup": {"lat": 40.75, "lng": -73.98},
            "dropoff": {"lat": 40.76, "lng": -73.97},
            "incentive_limit": 3000,
        })
    assert resp.status_code == 400
    assert resp.json()["error"] == "no_route_found"


def test_create_passenger_returns_reserved_id():
    app.state.manager = make_manager(SimStatus.RUNNING)
    with TestClient(app) as client:
        resp = client.post("/simulation/passengers", json={
            "pickup": {"lat": 40.75, "lng": -73.98},
            "dropoff": {"lat": 40.76, "lng": -73.97},
            "incentive_limit": 3000,
        })
    assert resp.status_code == 200
    assert resp.json() == {"passenger_id": "upax_1"}
    app.state.manager.create_manual_passenger.assert_called_once()


def test_create_passenger_requires_running_simulation():
    app.state.manager = make_manager(SimStatus.IDLE)
    with TestClient(app) as client:
        resp = client.post("/simulation/passengers", json={
            "pickup": {"lat": 40.75, "lng": -73.98},
            "dropoff": {"lat": 40.76, "lng": -73.97},
        })
    assert resp.status_code == 400
    assert resp.json()["error"] == "simulation_not_running"


def test_create_passenger_validation_error_uses_frontend_error_shape():
    app.state.manager = make_manager(SimStatus.RUNNING)
    with TestClient(app) as client:
        resp = client.post("/simulation/passengers", json={
            "pickup": {"lat": "not-a-number", "lng": -73.98},
            "dropoff": {"lat": 40.76, "lng": -73.97},
        })
    assert resp.status_code == 422
    assert resp.json() == {
        "error": "invalid_request",
        "message": "Request validation failed",
    }


def test_create_taxi_returns_reserved_id():
    app.state.manager = make_manager(SimStatus.RUNNING)
    with TestClient(app) as client:
        resp = client.post("/simulation/taxis", json={"lat": 40.748, "lng": -73.985})
    assert resp.status_code == 200
    assert resp.json() == {"taxi_id": "utaxi_1"}


def test_get_taxi_standby_returns_context():
    mgr = make_manager(SimStatus.RUNNING)
    mgr.get_taxi_standby_context = MagicMock(return_value={
        "taxi_id": "utaxi_1",
        "location": {"lat": 40.748, "lng": -73.985},
        "current_incentive": 1200,
        "current_surge": 1.5,
        "recommended_cells": [],
    })
    app.state.manager = mgr
    with TestClient(app) as client:
        resp = client.get("/simulation/taxis/utaxi_1/standby")
    assert resp.status_code == 200
    assert resp.json()["taxi_id"] == "utaxi_1"


def test_get_taxi_call_returns_context():
    mgr = make_manager(SimStatus.RUNNING)
    mgr.get_taxi_call_detail = MagicMock(return_value={
        "taxi_id": "utaxi_1",
        "passenger_id": "upax_1",
        "pickup": {"lat": 40.748, "lng": -73.985},
        "dropoff": {"lat": 40.758, "lng": -73.975},
        "incentive": 8500,
        "destination_surge": 1.6,
        "incentive_breakdown": {
            "base_fare": 6200,
            "passenger_incentive": 2300,
            "surge_bonus": 0,
        },
    })
    app.state.manager = mgr
    with TestClient(app) as client:
        resp = client.get("/simulation/taxis/utaxi_1/call")
    assert resp.status_code == 200
    assert resp.json()["incentive_breakdown"]["passenger_incentive"] == 2300


# ---------------------------------------------------------------------------
# POST /simulation/stop
# ---------------------------------------------------------------------------

def test_stop_returns_200():
    app.state.manager = make_manager(SimStatus.RUNNING)
    with TestClient(app) as client:
        resp = client.post("/simulation/stop")
    assert resp.status_code == 200


def test_stop_calls_stop_on_manager():
    mgr = make_manager(SimStatus.RUNNING)
    app.state.manager = mgr
    with TestClient(app) as client:
        client.post("/simulation/stop")
    mgr.stop.assert_called()


def test_stop_returns_idle_status():
    app.state.manager = make_manager(SimStatus.RUNNING)
    with TestClient(app) as client:
        resp = client.post("/simulation/stop")
    assert resp.json()["status"] == SimStatus.IDLE


# ---------------------------------------------------------------------------
# POST /simulation/shutdown
# ---------------------------------------------------------------------------

def test_shutdown_returns_200():
    mgr = make_manager(SimStatus.RUNNING)
    app.state.manager = mgr
    with patch("os.kill") as mock_kill:
        with TestClient(app) as client:
            resp = client.post("/simulation/shutdown")
    assert resp.status_code == 200


def test_shutdown_response_status_is_shutting_down():
    mgr = make_manager(SimStatus.RUNNING)
    app.state.manager = mgr
    with patch("os.kill"):
        with TestClient(app) as client:
            resp = client.post("/simulation/shutdown")
    assert resp.json()["status"] == "shutting down"


def test_shutdown_calls_stop_on_manager():
    mgr = make_manager(SimStatus.RUNNING)
    app.state.manager = mgr
    with patch("os.kill"):
        with TestClient(app) as client:
            client.post("/simulation/shutdown")
    mgr.stop.assert_called()
