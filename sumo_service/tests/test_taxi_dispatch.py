"""
Tests for SimulationManager._update_taxi_states and _accumulate_fares.

TraCI is fully mocked — no SUMO required.
"""

import sys
import types
from unittest.mock import MagicMock, patch

import pytest

from tests.test_passenger_spawn import _make_traci_stub, _traci_stub  # reuse stub


from app.fare import SPEED_THRESHOLD_MPS, TripAccumulator
from app.passenger import Passenger
from app.simulation import (
    DISPATCH_TIMEOUT_S,
    STEP_LENGTH,
    TRIP_TIMEOUT_S,
    SimulationManager,
)
from traci import constants as tc


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_passenger(
    pid: str = "p_0",
    x: float = 0.0,
    y: float = 0.0,
    pickup_edge: str = "pickup_edge",
    dropoff_edge: str = "dropoff_edge",
    state: str = "waiting",
) -> Passenger:
    return Passenger(
        id=pid, x=x, y=y, lat=40.7, lng=-74.0,
        pickup_edge=pickup_edge, dropoff_edge=dropoff_edge,
        dropoff_x=500.0, dropoff_y=500.0, dropoff_lat=40.71, dropoff_lng=-73.99,
        expected_distance_m=2000.0, expected_fare=775,
        spawn_time=0.0, state=state,
    )


def make_sub_entry(x=0.0, y=0.0, speed=10.0, distance=100.0, road_id="some_edge"):
    return {
        tc.VAR_POSITION: (x, y),
        tc.VAR_ANGLE: 0.0,
        tc.VAR_SPEED: speed,
        tc.VAR_DISTANCE: distance,
        tc.VAR_ROAD_ID: road_id,
    }


def make_manager() -> SimulationManager:
    mgr = SimulationManager()
    mgr._routable_edges = [f"edge_{i}" for i in range(5)] + [
        "some_edge",
        "pickup_edge",
        "dropoff_edge",
        "other_edge",
    ]
    mgr._routable_edges_set = set(mgr._routable_edges)
    mgr._latlng = lambda x, y: (40.7 + y * 1e-5, -74.0 + x * 1e-5)
    _traci_stub.simulation.findRoute = MagicMock(
        return_value=MagicMock(edges=["some_edge", "pickup_edge"], length=1000.0)
    )
    return mgr


# ---------------------------------------------------------------------------
# _update_taxi_states — 단계 1: 배차
# ---------------------------------------------------------------------------

def test_dispatch_empty_taxi_to_waiting_passenger():
    mgr = make_manager()
    p = make_passenger()
    mgr._passengers["p_0"] = p
    sub = {"taxi_0": make_sub_entry()}
    _traci_stub.vehicle.changeTarget = MagicMock()

    mgr._update_taxi_states(0.0, sub)

    assert mgr._taxi_states["taxi_0"] == "dispatched"
    assert p.state == "assigned"
    assert mgr._taxi_targets["taxi_0"] == "p_0"


def test_dispatch_selects_nearest_passenger():
    mgr = make_manager()
    p_near = make_passenger("p_0", x=10.0, y=0.0)
    p_far  = make_passenger("p_1", x=1000.0, y=0.0)
    mgr._passengers["p_0"] = p_near
    mgr._passengers["p_1"] = p_far
    # taxi at origin → nearest is p_0
    sub = {"taxi_0": make_sub_entry(x=0.0, y=0.0)}
    _traci_stub.vehicle.changeTarget = MagicMock()

    mgr._update_taxi_states(0.0, sub)

    assert mgr._taxi_targets["taxi_0"] == "p_0"
    assert p_near.state == "assigned"
    assert p_far.state == "waiting"


def test_no_dispatch_when_no_waiting_passengers():
    mgr = make_manager()
    sub = {"taxi_0": make_sub_entry()}

    mgr._update_taxi_states(0.0, sub)

    assert mgr._taxi_states.get("taxi_0") != "dispatched"


def test_dispatch_skipped_when_findroute_raises():
    mgr = make_manager()
    p = make_passenger()
    mgr._passengers["p_0"] = p
    sub = {"taxi_0": make_sub_entry()}
    _traci_stub.simulation.findRoute = MagicMock(side_effect=Exception("TraCIError"))

    mgr._update_taxi_states(0.0, sub)

    assert mgr._taxi_states.get("taxi_0") != "dispatched"
    assert p.state == "waiting"


@pytest.mark.parametrize("road_id", ["", ":junction_edge", "outside_edge"])
def test_dispatch_skips_invalid_current_road_without_findroute(road_id):
    mgr = make_manager()
    p = make_passenger()
    mgr._passengers["p_0"] = p
    sub = {"taxi_0": make_sub_entry(road_id=road_id)}
    _traci_stub.simulation.findRoute = MagicMock()

    mgr._update_taxi_states(0.0, sub)

    _traci_stub.simulation.findRoute.assert_not_called()
    assert mgr._taxi_states.get("taxi_0") != "dispatched"
    assert p.state == "waiting"


# ---------------------------------------------------------------------------
# _update_taxi_states — 단계 2: 배차 타임아웃 / 픽업
# ---------------------------------------------------------------------------

def test_capture_grid_counts_matches_capture_state_counts():
    mgr = make_manager()
    waiting = make_passenger("p_waiting", state="waiting")
    assigned = make_passenger("p_assigned", state="assigned")
    picked_up = make_passenger("p_picked_up", state="picked_up")
    waiting.h3_pickup = "pickup_cell_a"
    assigned.h3_pickup = "pickup_cell_a"
    picked_up.h3_pickup = "pickup_cell_b"
    mgr._passengers = {
        waiting.id: waiting,
        assigned.id: assigned,
        picked_up.id: picked_up,
    }
    mgr._taxi_states = {
        "taxi_empty": "empty",
        "taxi_dispatched": "dispatched",
        "bg_0": "empty",
    }
    sub = {
        "taxi_empty": make_sub_entry(x=0.0, y=0.0),
        "taxi_dispatched": make_sub_entry(x=100.0, y=100.0),
        "bg_0": make_sub_entry(x=200.0, y=200.0),
    }

    _, state_supply, state_demand = mgr._capture_state(10.0, sub)
    grid_supply, grid_demand = mgr._capture_grid_counts(sub)

    assert grid_supply == state_supply
    assert grid_demand == state_demand


def test_dispatch_timeout_reverts_passenger_to_waiting():
    mgr = make_manager()
    p = make_passenger(state="assigned")
    mgr._passengers["p_0"] = p
    mgr._taxi_states["taxi_0"] = "dispatched"
    mgr._taxi_targets["taxi_0"] = "p_0"
    mgr._taxi_dispatch_times["taxi_0"] = 0.0
    mgr._taxi_dispatch_surge["taxi_0"] = 2.0
    sub = {"taxi_0": make_sub_entry(road_id="other_edge")}

    mgr._update_taxi_states(DISPATCH_TIMEOUT_S + 1, sub)

    assert mgr._taxi_states["taxi_0"] == "empty"
    assert p.state == "waiting"
    assert "taxi_0" not in mgr._taxi_targets
    assert "taxi_0" not in mgr._taxi_dispatch_surge


def test_pickup_when_taxi_on_pickup_edge():
    mgr = make_manager()
    p = make_passenger(state="assigned", pickup_edge="pickup_edge")
    mgr._passengers["p_0"] = p
    mgr._taxi_states["taxi_0"] = "dispatched"
    mgr._taxi_targets["taxi_0"] = "p_0"
    mgr._taxi_dispatch_times["taxi_0"] = 0.0
    _traci_stub.simulation.findRoute = MagicMock(
        return_value=MagicMock(edges=["pickup_edge", "dropoff_edge"])
    )
    _traci_stub.vehicle.setRoute = MagicMock()
    _traci_stub.vehicle.getDistance = MagicMock(return_value=50.0)
    sub = {"taxi_0": make_sub_entry(road_id="pickup_edge")}

    mgr._update_taxi_states(100.0, sub)

    assert mgr._taxi_states["taxi_0"] == "occupied"
    assert p.state == "picked_up"
    assert "taxi_0" in mgr._active_trips


def test_pickup_creates_trip_accumulator():
    mgr = make_manager()
    p = make_passenger(state="assigned", pickup_edge="pickup_edge")
    mgr._passengers["p_0"] = p
    mgr._taxi_states["taxi_0"] = "dispatched"
    mgr._taxi_targets["taxi_0"] = "p_0"
    mgr._taxi_dispatch_times["taxi_0"] = 0.0
    _traci_stub.simulation.findRoute = MagicMock(
        return_value=MagicMock(edges=["pickup_edge", "dropoff_edge"])
    )
    _traci_stub.vehicle.setRoute = MagicMock()
    _traci_stub.vehicle.getDistance = MagicMock(return_value=50.0)
    sub = {"taxi_0": make_sub_entry(road_id="pickup_edge")}

    mgr._update_taxi_states(100.0, sub)

    accum = mgr._active_trips["taxi_0"]
    assert accum.passenger_id == "p_0"
    assert accum.pickup_sim_time == 100.0
    assert accum.last_distance_snapshot == 50.0


def test_pickup_carries_dispatch_surge_into_trip_accumulator():
    mgr = make_manager()
    p = make_passenger(state="assigned", pickup_edge="pickup_edge")
    mgr._passengers["p_0"] = p
    mgr._taxi_states["taxi_0"] = "dispatched"
    mgr._taxi_targets["taxi_0"] = "p_0"
    mgr._taxi_dispatch_times["taxi_0"] = 0.0
    mgr._taxi_dispatch_surge["taxi_0"] = 2.4
    _traci_stub.simulation.findRoute = MagicMock(
        return_value=MagicMock(edges=["pickup_edge", "dropoff_edge"])
    )
    _traci_stub.vehicle.setRoute = MagicMock()
    _traci_stub.vehicle.getDistance = MagicMock(return_value=50.0)
    sub = {"taxi_0": make_sub_entry(road_id="pickup_edge")}

    mgr._update_taxi_states(100.0, sub)

    assert mgr._active_trips["taxi_0"].surge == 2.4
    assert "taxi_0" not in mgr._taxi_dispatch_surge


# ---------------------------------------------------------------------------
# _update_taxi_states — 단계 3: 하차 / 트립 타임아웃
# ---------------------------------------------------------------------------

def test_dropoff_when_taxi_on_dropoff_edge():
    mgr = make_manager()
    p = make_passenger(state="picked_up", dropoff_edge="dropoff_edge")
    mgr._passengers["p_0"] = p
    mgr._taxi_states["taxi_0"] = "occupied"
    mgr._taxi_targets["taxi_0"] = "p_0"
    mgr._active_trips["taxi_0"] = TripAccumulator(
        passenger_id="p_0", pickup_sim_time=0.0, distance_m=1000.0
    )
    _traci_stub.vehicle.changeTarget = MagicMock()
    sub = {"taxi_0": make_sub_entry(road_id="dropoff_edge")}

    fare_updates = mgr._update_taxi_states(200.0, sub)

    assert len(fare_updates) == 1
    assert fare_updates[0]["passenger_id"] == "p_0"
    assert fare_updates[0]["taxi_id"] == "taxi_0"
    assert fare_updates[0]["fare"] > 0
    assert mgr._taxi_states["taxi_0"] == "empty"
    assert "p_0" not in mgr._passengers
    assert "taxi_0" not in mgr._active_trips


def test_trip_timeout_forces_fare():
    mgr = make_manager()
    p = make_passenger(state="picked_up")
    mgr._passengers["p_0"] = p
    mgr._taxi_states["taxi_0"] = "occupied"
    mgr._taxi_targets["taxi_0"] = "p_0"
    mgr._active_trips["taxi_0"] = TripAccumulator(
        passenger_id="p_0", pickup_sim_time=0.0, distance_m=500.0
    )
    _traci_stub.vehicle.changeTarget = MagicMock()
    sub = {"taxi_0": make_sub_entry(road_id="other_edge")}

    fare_updates = mgr._update_taxi_states(TRIP_TIMEOUT_S + 1, sub)

    assert len(fare_updates) == 1
    assert mgr._taxi_states["taxi_0"] == "empty"
    assert "taxi_0" not in mgr._active_trips


# ---------------------------------------------------------------------------
# _accumulate_fares
# ---------------------------------------------------------------------------

def test_accumulate_distance_delta():
    mgr = make_manager()
    accum = TripAccumulator(
        passenger_id="p_0", pickup_sim_time=0.0,
        distance_m=0.0, last_distance_snapshot=100.0,
    )
    mgr._active_trips["taxi_0"] = accum
    mgr._taxi_states["taxi_0"] = "occupied"
    sub = {"taxi_0": make_sub_entry(distance=250.0, speed=10.0)}

    mgr._accumulate_fares(10.0, sub)

    assert accum.distance_m == pytest.approx(150.0)
    assert accum.last_distance_snapshot == 250.0


def test_accumulate_low_speed_time():
    mgr = make_manager()
    accum = TripAccumulator(
        passenger_id="p_0", pickup_sim_time=0.0, last_distance_snapshot=100.0,
    )
    mgr._active_trips["taxi_0"] = accum
    mgr._taxi_states["taxi_0"] = "occupied"
    low_speed = SPEED_THRESHOLD_MPS - 0.1
    sub = {"taxi_0": make_sub_entry(distance=100.0, speed=low_speed)}

    mgr._accumulate_fares(10.0, sub)

    assert accum.low_speed_seconds == pytest.approx(STEP_LENGTH)


def test_no_low_speed_accumulation_above_threshold():
    mgr = make_manager()
    accum = TripAccumulator(
        passenger_id="p_0", pickup_sim_time=0.0, last_distance_snapshot=100.0,
    )
    mgr._active_trips["taxi_0"] = accum
    mgr._taxi_states["taxi_0"] = "occupied"
    high_speed = SPEED_THRESHOLD_MPS + 1.0
    sub = {"taxi_0": make_sub_entry(distance=100.0, speed=high_speed)}

    mgr._accumulate_fares(10.0, sub)

    assert accum.low_speed_seconds == 0.0


def test_negative_distance_delta_not_accumulated():
    mgr = make_manager()
    accum = TripAccumulator(
        passenger_id="p_0", pickup_sim_time=0.0,
        distance_m=500.0, last_distance_snapshot=300.0,
    )
    mgr._active_trips["taxi_0"] = accum
    mgr._taxi_states["taxi_0"] = "occupied"
    # distance decreased (shouldn't happen in practice, but guard against it)
    sub = {"taxi_0": make_sub_entry(distance=200.0, speed=10.0)}

    mgr._accumulate_fares(10.0, sub)

    assert accum.distance_m == 500.0


def test_taxi_disappeared_generates_fare_update():
    mgr = make_manager()
    p = make_passenger(state="picked_up")
    mgr._passengers["p_0"] = p
    mgr._taxi_targets["taxi_0"] = "p_0"
    mgr._taxi_states["taxi_0"] = "occupied"
    mgr._active_trips["taxi_0"] = TripAccumulator(
        passenger_id="p_0", pickup_sim_time=0.0, distance_m=800.0,
    )
    # taxi_0 absent from sub_results → disappeared
    sub = {}

    fare_updates = mgr._accumulate_fares(100.0, sub)

    assert len(fare_updates) == 1
    assert fare_updates[0]["passenger_id"] == "p_0"
    assert "taxi_0" not in mgr._active_trips
    assert "p_0" not in mgr._passengers
