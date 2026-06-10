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
    DISPATCH_DELAY_S,
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
    spawn_time: float = 0.0,
) -> Passenger:
    return Passenger(
        id=pid, x=x, y=y, lat=40.7, lng=-74.0,
        pickup_edge=pickup_edge, dropoff_edge=dropoff_edge,
        dropoff_x=500.0, dropoff_y=500.0, dropoff_lat=40.71, dropoff_lng=-73.99,
        expected_distance_m=2000.0, expected_fare=775,
        spawn_time=spawn_time, state=state,
    )


def make_sub_entry(
    x=0.0,
    y=0.0,
    speed=10.0,
    distance=100.0,
    road_id="some_edge",
    route_index=0,
):
    return {
        tc.VAR_POSITION: (x, y),
        tc.VAR_ANGLE: 0.0,
        tc.VAR_SPEED: speed,
        tc.VAR_DISTANCE: distance,
        tc.VAR_ROAD_ID: road_id,
        tc.VAR_ROUTE_INDEX: route_index,
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
    p = make_passenger(spawn_time=-100.0)  # 유예 지난 승객
    mgr._passengers["p_0"] = p
    sub = {"taxi_0": make_sub_entry()}
    _traci_stub.vehicle.changeTarget = MagicMock()

    mgr._update_taxi_states(0.0, sub)

    assert mgr._taxi_states["taxi_0"] == "dispatched"
    assert p.state == "assigned"
    assert mgr._taxi_targets["taxi_0"] == "p_0"
    assert mgr._taxi_pickup_route_index["taxi_0"] == 1


def test_dispatch_selects_nearest_passenger():
    mgr = make_manager()
    p_near = make_passenger("p_0", x=10.0, y=0.0, spawn_time=-100.0)
    p_far  = make_passenger("p_1", x=1000.0, y=0.0, spawn_time=-100.0)
    mgr._passengers["p_0"] = p_near
    mgr._passengers["p_1"] = p_far
    # taxi at origin → nearest is p_0
    sub = {"taxi_0": make_sub_entry(x=0.0, y=0.0)}
    _traci_stub.vehicle.changeTarget = MagicMock()

    mgr._update_taxi_states(0.0, sub)

    assert mgr._taxi_targets["taxi_0"] == "p_0"
    assert p_near.state == "assigned"
    assert p_far.state == "waiting"


def test_dispatch_skips_passenger_beyond_pickup_cutoff():
    # 2.14마일(~3444m) 컷오프 밖 승객은 유일한 후보여도 배차되지 않는다.
    mgr = make_manager()
    p_far = make_passenger("p_0", x=5000.0, y=0.0, spawn_time=-100.0)  # > 3444m
    mgr._passengers["p_0"] = p_far
    sub = {"taxi_0": make_sub_entry(x=0.0, y=0.0)}
    _traci_stub.vehicle.changeTarget = MagicMock()

    mgr._update_taxi_states(0.0, sub)

    assert mgr._taxi_states.get("taxi_0") != "dispatched"
    assert p_far.state == "waiting"


def test_dispatch_cutoff_disabled_when_zero(monkeypatch):
    # 컷오프 비활성(0)이면 먼 승객도 배차 후보가 된다.
    monkeypatch.setattr("app.simulation.DISPATCH_MAX_PICKUP_M2", 0)
    mgr = make_manager()
    p_far = make_passenger("p_0", x=5000.0, y=0.0, spawn_time=-100.0)
    mgr._passengers["p_0"] = p_far
    sub = {"taxi_0": make_sub_entry(x=0.0, y=0.0)}
    _traci_stub.vehicle.changeTarget = MagicMock()

    mgr._update_taxi_states(0.0, sub)

    assert mgr._taxi_states["taxi_0"] == "dispatched"
    assert p_far.state == "assigned"


def test_manual_taxi_claims_passenger_before_fleet():
    # 수동 택시(utaxi_)가 fleet보다 먼저 순회되어 승객을 claim한다(기본 우선순위 on).
    mgr = make_manager()
    p = make_passenger("p_0", x=0.0, y=0.0, spawn_time=-100.0)
    mgr._passengers["p_0"] = p
    # dict 삽입 순서상 fleet가 먼저 → 우선순위가 없으면 fleet가 claim했을 상황.
    sub = {
        "taxi_0": make_sub_entry(x=10.0, y=0.0),
        "utaxi_0": make_sub_entry(x=20.0, y=0.0),
    }
    _traci_stub.vehicle.changeTarget = MagicMock()

    mgr._update_taxi_states(0.0, sub)

    assert mgr._taxi_targets.get("utaxi_0") == "p_0"
    assert mgr._taxi_states["utaxi_0"] == "dispatched"
    assert mgr._taxi_states.get("taxi_0") != "dispatched"


def test_manual_taxi_priority_disabled(monkeypatch):
    # 우선순위 off면 기존 구독 순서대로 → 먼저 등장한 fleet가 claim한다.
    monkeypatch.setattr("app.simulation.MANUAL_TAXI_DISPATCH_PRIORITY", False)
    mgr = make_manager()
    p = make_passenger("p_0", x=0.0, y=0.0, spawn_time=-100.0)
    mgr._passengers["p_0"] = p
    sub = {
        "taxi_0": make_sub_entry(x=10.0, y=0.0),
        "utaxi_0": make_sub_entry(x=20.0, y=0.0),
    }
    _traci_stub.vehicle.changeTarget = MagicMock()

    mgr._update_taxi_states(0.0, sub)

    assert mgr._taxi_targets.get("taxi_0") == "p_0"
    assert mgr._taxi_states["taxi_0"] == "dispatched"
    assert mgr._taxi_states.get("utaxi_0") != "dispatched"


def test_dispatch_delayed_until_grace_elapsed():
    mgr = make_manager()
    spawn = 10.0
    p = make_passenger(spawn_time=spawn)
    mgr._passengers["p_0"] = p
    sub = {"taxi_0": make_sub_entry()}
    _traci_stub.vehicle.changeTarget = MagicMock()

    # 유예 중(생성 후 DISPATCH_DELAY_S 미만) → 배차 안 됨
    mgr._update_taxi_states(spawn + DISPATCH_DELAY_S - 1.0, sub)
    assert mgr._taxi_states.get("taxi_0") != "dispatched"
    assert p.state == "waiting"

    # 유예 경과 후 → 배차됨
    mgr._update_taxi_states(spawn + DISPATCH_DELAY_S + 1.0, sub)
    assert mgr._taxi_states["taxi_0"] == "dispatched"
    assert p.state == "assigned"


def test_manual_passenger_dispatch_delay_can_be_disabled(monkeypatch):
    # DISPATCH_DELAY_MANUAL=False면 수동 승객은 유예 없이 즉시 배차 후보가 된다.
    monkeypatch.setattr("app.simulation.DISPATCH_DELAY_MANUAL", False)
    mgr = make_manager()
    p = make_passenger(spawn_time=10.0)
    p.manual = True
    mgr._passengers["p_0"] = p
    sub = {"taxi_0": make_sub_entry()}
    _traci_stub.vehicle.changeTarget = MagicMock()

    # 생성 후 1초(유예 미만)인데도 수동+토글off라 즉시 배차
    mgr._update_taxi_states(11.0, sub)
    assert mgr._taxi_states["taxi_0"] == "dispatched"
    assert p.state == "assigned"


def test_no_dispatch_when_no_waiting_passengers():
    mgr = make_manager()
    sub = {"taxi_0": make_sub_entry()}

    mgr._update_taxi_states(0.0, sub)

    assert mgr._taxi_states.get("taxi_0") != "dispatched"


def test_dispatch_pricing_applies_passenger_incentive_limit(monkeypatch):
    # surge_by_h3 기반 캡 적용 검증 — 목표매칭률 추종 인센티브(LIVE_TARGET_PRICING)는 끈 상태.
    monkeypatch.setattr("app.simulation.LIVE_TARGET_PRICING", False)
    mgr = make_manager()
    p = make_passenger()
    p.h3_pickup = "pickup_cell"
    p.h3_dropoff = "dropoff_cell"
    p.expected_fare = 1000
    p.incentive_limit = 300
    mgr._surge_by_h3 = {"pickup_cell": 2.0}

    pricing = mgr._dispatch_pricing(
        candidate=p,
        sim_time=0.0,
        sub_results={"taxi_0": make_sub_entry()},
        current_veh_id="taxi_0",
        current_route=MagicMock(edges=["some_edge", "pickup_edge"], length=1000.0),
    )

    assert pricing["system_surge"] == 2.0
    assert pricing["final_fare_cents"] == 1300
    assert pricing["final_surge"] == pytest.approx(1.3)
    assert pricing["incentive_cap_applied"] is True


def test_dispatch_skipped_when_findroute_raises():
    mgr = make_manager()
    p = make_passenger()
    mgr._passengers["p_0"] = p
    sub = {"taxi_0": make_sub_entry()}
    _traci_stub.simulation.findRoute = MagicMock(side_effect=Exception("TraCIError"))

    mgr._update_taxi_states(0.0, sub)

    assert mgr._taxi_states.get("taxi_0") != "dispatched"
    assert p.state == "waiting"


def test_rejected_dispatch_sets_cooldown_and_skips_immediate_retry():
    mgr = make_manager()
    p = make_passenger(spawn_time=-100.0)  # 유예 지난 승객
    p.h3_pickup = "pickup_cell"
    p.h3_dropoff = "dropoff_cell"
    mgr._passengers["p_0"] = p
    mgr._taxi_last_dropoff_cells["taxi_0"] = "last_cell"
    mgr._dispatch_pricing = MagicMock(return_value={
        "base_fare_usd": 7.75,
        "raw_surge": 1.0,
        "bucket": "low",
        "target_matching_rate": 0.7,
        "required_fare_usd": None,
        "calculated_surge": 1.0,
        "system_surge": 1.0,
        "final_surge": 1.0,
        "final_fare_usd": 7.75,
        "final_fare_cents": 775,
        "uncapped_fare_cents": 775,
        "quote_cap_fare_cents": None,
        "incentive_cap_applied": False,
        "surge_clamped": False,
        "pricing_driver_count": 1,
    })
    sub = {"taxi_0": make_sub_entry()}

    with patch("app.simulation._acceptance_probability", return_value=0.0), \
         patch("app.simulation._random.random", return_value=1.0):
        mgr._update_taxi_states(0.0, sub)
        assert p.state == "waiting"
        assert ("taxi_0", "p_0") in mgr._rejected_dispatch_pairs
        assert mgr._taxi_dispatch_cooldown_until["taxi_0"] > 0.0

        _traci_stub.simulation.findRoute.reset_mock()
        mgr._update_taxi_states(1.0, sub)

    _traci_stub.simulation.findRoute.assert_not_called()


def test_rejected_dispatch_pair_is_retryable_after_cooldown_expires():
    mgr = make_manager()
    mgr._set_dispatch_cooldowns("taxi_0", "p_0", 0.0)
    mgr._record_dispatch_rejection("taxi_0", "p_0")

    mgr._prune_dispatch_cooldowns(61.0)

    assert ("taxi_0", "p_0") not in mgr._rejected_dispatch_pairs
    assert not mgr._pair_on_dispatch_cooldown("taxi_0", "p_0", 61.0)


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
    grid_supply, grid_demand, grid_demand_weighted = mgr._capture_grid_counts(sub)

    assert grid_supply == state_supply
    # 실제 카운트(grid_demand)는 _capture_state와 일치해야 한다.
    assert grid_demand == state_demand


def test_capture_grid_counts_weights_assigned_demand(monkeypatch):
    monkeypatch.setattr("app.simulation.ASSIGNED_DEMAND_WEIGHT", 0.5)
    mgr = make_manager()
    w = make_passenger("p_w", state="waiting"); w.h3_pickup = "cell"
    a1 = make_passenger("p_a1", state="assigned"); a1.h3_pickup = "cell"
    a2 = make_passenger("p_a2", state="assigned"); a2.h3_pickup = "cell"
    mgr._passengers = {w.id: w, a1.id: a1, a2.id: a2}

    _, grid_demand, grid_demand_weighted = mgr._capture_grid_counts({})

    assert grid_demand["cell"] == 3                  # 실제 카운트: waiting1 + assigned2
    assert grid_demand_weighted["cell"] == 2.0       # 가중: 1 + 2*0.5


def test_capture_grid_counts_weight_one_reproduces_count(monkeypatch):
    # ASSIGNED_DEMAND_WEIGHT=1.0이면 가중 demand가 실제 카운트와 동일(현재 동작 재현).
    monkeypatch.setattr("app.simulation.ASSIGNED_DEMAND_WEIGHT", 1.0)
    mgr = make_manager()
    a = make_passenger("p_a", state="assigned"); a.h3_pickup = "cell"
    mgr._passengers = {a.id: a}

    _, grid_demand, grid_demand_weighted = mgr._capture_grid_counts({})

    assert grid_demand_weighted["cell"] == grid_demand["cell"] == 1.0


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


def test_pickup_when_taxi_passed_pickup_route_index(monkeypatch):
    monkeypatch.setattr("app.simulation.SIM_PROFILE", True)
    mgr = make_manager()
    p = make_passenger(state="assigned", pickup_edge="pickup_edge", dropoff_edge="dropoff_edge")
    mgr._passengers["p_0"] = p
    mgr._taxi_states["taxi_0"] = "dispatched"
    mgr._taxi_targets["taxi_0"] = "p_0"
    mgr._taxi_dispatch_times["taxi_0"] = 0.0
    mgr._taxi_pickup_route_index["taxi_0"] = 1
    _traci_stub.simulation.findRoute = MagicMock(
        return_value=MagicMock(edges=["buffer_edge", "dropoff_edge"])
    )
    _traci_stub.vehicle.setRoute = MagicMock()
    _traci_stub.vehicle.getDistance = MagicMock(return_value=80.0)
    sub = {
        "taxi_0": make_sub_entry(
            road_id="buffer_edge",
            route_index=2,
        )
    }
    mgr._routable_edges.append("buffer_edge")
    mgr._routable_edges_set.add("buffer_edge")

    mgr._update_taxi_states(100.0, sub)

    assert mgr._taxi_states["taxi_0"] == "occupied"
    assert p.state == "picked_up"
    assert "taxi_0" not in mgr._taxi_pickup_route_index
    # dropoff index는 prefix offset(현재 route_index=2)만큼 보정됨: 2 + (트립경로 상대 index 1) = 3
    assert mgr._taxi_dropoff_route_index["taxi_0"] == 3
    assert mgr._prof_counters["pickup_index_reached"] == 1


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


def test_dropoff_fare_update_respects_passenger_cap():
    mgr = make_manager()
    p = make_passenger(state="picked_up", dropoff_edge="dropoff_edge")
    p.expected_fare = 300
    p.incentive_limit = 100
    mgr._passengers["p_0"] = p
    mgr._taxi_states["taxi_0"] = "occupied"
    mgr._taxi_targets["taxi_0"] = "p_0"
    mgr._active_trips["taxi_0"] = TripAccumulator(
        passenger_id="p_0",
        pickup_sim_time=0.0,
        distance_m=2000.0,
        surge=2.0,
        fare_cap=400,
    )
    sub = {"taxi_0": make_sub_entry(road_id="dropoff_edge")}

    fare_updates = mgr._update_taxi_states(200.0, sub)

    assert fare_updates[0]["fare"] == 400
    assert fare_updates[0]["uncapped_fare"] > fare_updates[0]["fare"]
    assert fare_updates[0]["incentive_cap_applied"] is True


def test_dropoff_when_taxi_passed_dropoff_route_index(monkeypatch):
    monkeypatch.setattr("app.simulation.SIM_PROFILE", True)
    mgr = make_manager()
    p = make_passenger(state="picked_up", dropoff_edge="dropoff_edge")
    mgr._passengers["p_0"] = p
    mgr._taxi_states["taxi_0"] = "occupied"
    mgr._taxi_targets["taxi_0"] = "p_0"
    mgr._taxi_dropoff_route_index["taxi_0"] = 1
    mgr._active_trips["taxi_0"] = TripAccumulator(
        passenger_id="p_0",
        pickup_sim_time=50.0,
        last_distance_snapshot=0.0,
        surge=1.0,
    )
    _traci_stub.vehicle.setRoute = MagicMock()
    sub = {
        "taxi_0": make_sub_entry(
            road_id="buffer_edge",
            route_index=2,
            distance=500.0,
        )
    }
    mgr._routable_edges.append("buffer_edge")
    mgr._routable_edges_set.add("buffer_edge")

    fare_updates = mgr._update_taxi_states(100.0, sub)

    assert len(fare_updates) == 1
    assert "taxi_0" not in mgr._active_trips
    assert "p_0" not in mgr._passengers
    assert "taxi_0" not in mgr._taxi_dropoff_route_index
    assert mgr._prof_counters["dropoff_index_reached"] == 1


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
