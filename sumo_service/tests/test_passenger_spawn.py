"""
Tests for passenger spawning logic in SimulationManager.

TraCI is fully mocked — no SUMO required.
PASSENGER_SOURCE is patched to "random" unless stated otherwise.
"""

import sys
import types
from unittest.mock import MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# Minimal TraCI stub so simulation.py can be imported without SUMO installed
# ---------------------------------------------------------------------------

def _make_traci_stub():
    if "traci" in sys.modules and hasattr(sys.modules["traci"], "_is_stub"):
        return sys.modules["traci"]
    traci_mod = types.ModuleType("traci")
    traci_mod._is_stub = True
    for sub in ("simulation", "vehicle", "lane", "edge", "route", "vehicletype"):
        setattr(traci_mod, sub, MagicMock())
    traci_mod.exceptions = types.ModuleType("traci.exceptions")
    traci_mod.exceptions.TraCIException = Exception
    traci_mod.exceptions.FatalTraCIError = Exception
    traci_mod.constants = types.ModuleType("traci.constants")
    
    constants_dict = {
        "VAR_POSITION": 66,
        "VAR_ANGLE": 67,
        "VAR_SPEED": 64,
        "VAR_DISTANCE": 132,
        "VAR_ROAD_ID": 80,
        "VAR_ROUTE_INDEX": 105,
    }
    for attr, val in constants_dict.items():
        setattr(traci_mod.constants, attr, val)
        
    sys.modules["traci"] = traci_mod
    sys.modules["traci.exceptions"] = traci_mod.exceptions
    sys.modules["traci.constants"] = traci_mod.constants
    return traci_mod


_traci_stub = _make_traci_stub()

from app.simulation import SimulationManager, _poisson_sample  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

EDGES = [f"edge_{i}" for i in range(10)]

RouteResult = MagicMock
_ROUTE = MagicMock(edges=["edge_0", "edge_1"], length=2000.0)
_SHAPE = [(100.0, 200.0), (110.0, 210.0), (120.0, 220.0)]


def make_manager() -> SimulationManager:
    mgr = SimulationManager()
    mgr._routable_edges = EDGES
    mgr._routable_edges_set = set(EDGES)
    mgr._latlng = lambda x, y: (40.7 + y * 1e-5, -74.0 + x * 1e-5)
    return mgr


# ---------------------------------------------------------------------------
# random 모드 — 5분 경계 확인
# ---------------------------------------------------------------------------

def test_no_spawn_within_same_interval():
    # int(299/300)=0 — interval 0 이미 처리됐으면 299초에 재스폰 없음
    mgr = make_manager()
    mgr._last_spawn_interval = 0
    with patch("app.simulation.PASSENGER_SOURCE", "random"), \
         patch("app.simulation._poisson_sample", return_value=3):
        mgr._spawn_passengers(299.0)
    assert len(mgr._passengers) == 0


def test_spawn_at_first_interval_boundary():
    mgr = make_manager()
    _traci_stub.simulation.findRoute.return_value = _ROUTE
    _traci_stub.lane.getShape.return_value = _SHAPE
    edges_cycle = ["edge_0", "edge_1"] * 10
    with patch("app.simulation.PASSENGER_SOURCE", "random"), \
         patch("app.simulation._poisson_sample", return_value=3), \
         patch("app.simulation._random.choice", side_effect=edges_cycle):
        mgr._spawn_passengers(300.0)
    assert len(mgr._passengers) == 3


def test_no_double_spawn_same_interval():
    mgr = make_manager()
    _traci_stub.simulation.findRoute.return_value = _ROUTE
    _traci_stub.lane.getShape.return_value = _SHAPE
    edges_cycle = ["edge_0", "edge_1"] * 10
    with patch("app.simulation.PASSENGER_SOURCE", "random"), \
         patch("app.simulation._poisson_sample", return_value=2), \
         patch("app.simulation._random.choice", side_effect=edges_cycle):
        mgr._spawn_passengers(300.0)
        mgr._spawn_passengers(350.0)
    assert len(mgr._passengers) == 2


def test_zero_poisson_spawns_nothing():
    mgr = make_manager()
    with patch("app.simulation.PASSENGER_SOURCE", "random"), \
         patch("app.simulation._poisson_sample", return_value=0):
        mgr._spawn_passengers(300.0)
    assert len(mgr._passengers) == 0


def test_random_spawn_uses_passengers_per_5min_from_experiment_config():
    from app.simulation import ExperimentConfig
    mgr = make_manager()
    mgr.experiment_config = ExperimentConfig(passengers_per_5min=7)
    with patch("app.simulation.PASSENGER_SOURCE", "random"), \
         patch("app.simulation._poisson_sample", return_value=0) as poisson:
        mgr._spawn_passengers(300.0)
    poisson.assert_called_once_with(7)


def test_passenger_counter_increments():
    mgr = make_manager()
    _traci_stub.simulation.findRoute.return_value = _ROUTE
    _traci_stub.lane.getShape.return_value = _SHAPE
    # edge_0/edge_1 교대로 반환 → 항상 서로 다른 엣지
    edges_cycle = ["edge_0", "edge_1"] * 10
    with patch("app.simulation.PASSENGER_SOURCE", "random"), \
         patch("app.simulation._poisson_sample", return_value=3), \
         patch("app.simulation._random.choice", side_effect=edges_cycle):
        mgr._spawn_passengers(300.0)
    ids = list(mgr._passengers.keys())
    assert ids == ["p_0", "p_1", "p_2"]


def test_passenger_state_is_waiting():
    mgr = make_manager()
    _traci_stub.simulation.findRoute.return_value = _ROUTE
    _traci_stub.lane.getShape.return_value = _SHAPE
    with patch("app.simulation.PASSENGER_SOURCE", "random"), \
         patch("app.simulation._poisson_sample", return_value=1), \
         patch("app.simulation._random.choice", side_effect=["edge_0", "edge_1"]):
        mgr._spawn_passengers(300.0)
    p = list(mgr._passengers.values())[0]
    assert p.state == "waiting"
    assert p.h3_pickup is not None


def test_findroute_failure_skips_passenger():
    mgr = make_manager()
    _traci_stub.simulation.findRoute.return_value = MagicMock(edges=[], length=0.0)
    with patch("app.simulation.PASSENGER_SOURCE", "random"), \
         patch("app.simulation._poisson_sample", return_value=3):
        mgr._spawn_passengers(300.0)
    assert len(mgr._passengers) == 0


# ---------------------------------------------------------------------------
# parquet 모드 — _trip_queue 기반 스폰
# ---------------------------------------------------------------------------

def _make_trip(sim_time: float) -> dict:
    return {
        "sim_time": sim_time,
        "pickup_edge": "edge_0",
        "dropoff_edge": "edge_1",
        "h3_pickup": "892830828cbffff",
    }


def test_parquet_spawn_when_time_reached():
    mgr = make_manager()
    mgr._trip_queue = [_make_trip(50.0), _make_trip(120.0)]
    _traci_stub.simulation.findRoute.return_value = _ROUTE
    _traci_stub.lane.getShape.return_value = _SHAPE
    with patch("app.simulation.PASSENGER_SOURCE", "parquet"):
        mgr._spawn_passengers(100.0)
    assert len(mgr._passengers) == 1
    assert mgr._trip_queue[0]["sim_time"] == 120.0
    assert mgr._parquet_replay_stats["scheduled_due_count"] == 1
    assert mgr._parquet_replay_stats["spawned_count"] == 1


def test_parquet_no_spawn_before_time():
    mgr = make_manager()
    mgr._trip_queue = [_make_trip(200.0)]
    with patch("app.simulation.PASSENGER_SOURCE", "parquet"):
        mgr._spawn_passengers(100.0)
    assert len(mgr._passengers) == 0
    assert len(mgr._trip_queue) == 1
    assert mgr._parquet_replay_stats["scheduled_due_count"] == 0


def test_parquet_skips_pickup_outside_scc():
    mgr = make_manager()
    trip = {
        "sim_time": 50.0,
        "pickup_edge": "outside_edge",  # not in EDGES
        "dropoff_edge": "edge_1",
        "h3_pickup": "892830828cbffff",
    }
    mgr._trip_queue = [trip]
    _traci_stub.simulation.findRoute.return_value = _ROUTE
    _traci_stub.lane.getShape.return_value = _SHAPE
    with patch("app.simulation.PASSENGER_SOURCE", "parquet"):
        mgr._spawn_passengers(100.0)
    assert len(mgr._passengers) == 0
    assert mgr._parquet_replay_stats["scheduled_due_count"] == 1
    assert mgr._parquet_replay_stats["skipped_pickup"] == 1


def test_parquet_skips_dropoff_outside_scc():
    mgr = make_manager()
    trip = {
        "sim_time": 50.0,
        "pickup_edge": "edge_0",
        "dropoff_edge": "outside_edge",  # not in EDGES
        "h3_pickup": "892830828cbffff",
    }
    mgr._trip_queue = [trip]
    _traci_stub.simulation.findRoute.return_value = _ROUTE
    _traci_stub.lane.getShape.return_value = _SHAPE
    with patch("app.simulation.PASSENGER_SOURCE", "parquet"):
        mgr._spawn_passengers(100.0)
    assert len(mgr._passengers) == 0
    assert mgr._parquet_replay_stats["scheduled_due_count"] == 1
    assert mgr._parquet_replay_stats["skipped_dropoff"] == 1


def test_parquet_outside_scc_trip_consumed_from_queue():
    """SCC 외부 trip이라도 큐에서 제거되어야 다음 trip이 처리됨."""
    mgr = make_manager()
    trip_outside = {
        "sim_time": 50.0,
        "pickup_edge": "outside_edge",
        "dropoff_edge": "edge_1",
        "h3_pickup": "892830828cbffff",
    }
    trip_inside = _make_trip(60.0)
    mgr._trip_queue = [trip_outside, trip_inside]
    _traci_stub.simulation.findRoute.return_value = _ROUTE
    _traci_stub.lane.getShape.return_value = _SHAPE
    with patch("app.simulation.PASSENGER_SOURCE", "parquet"):
        mgr._spawn_passengers(100.0)
    # 외부 trip은 스킵되지만 큐에서 빠지고, 내부 trip 1개만 스폰
    assert len(mgr._passengers) == 1
    assert len(mgr._trip_queue) == 0
    assert mgr._parquet_replay_stats["scheduled_due_count"] == 2
    assert mgr._parquet_replay_stats["skipped_pickup"] == 1
    assert mgr._parquet_replay_stats["spawned_count"] == 1


def test_parquet_counts_route_failure():
    mgr = make_manager()
    mgr._trip_queue = [_make_trip(50.0)]
    _traci_stub.simulation.findRoute.side_effect = _traci_stub.exceptions.TraCIException("route failed")
    try:
        with patch("app.simulation.PASSENGER_SOURCE", "parquet"):
            mgr._spawn_passengers(100.0)
    finally:
        _traci_stub.simulation.findRoute.side_effect = None

    assert len(mgr._passengers) == 0
    assert mgr._parquet_replay_stats["scheduled_due_count"] == 1
    assert mgr._parquet_replay_stats["route_failed"] == 1


def test_parquet_trip_queue_samples_each_5min_bucket_and_records_stats():
    from app.simulation import ExperimentConfig
    mgr = make_manager()
    mgr.experiment_config = ExperimentConfig(passengers_per_5min=1, seed=123)
    mgr._runtime_duration = 600.0
    trips = [
        _make_trip(10.0),
        _make_trip(20.0),
        _make_trip(310.0),
        _make_trip(320.0),
    ]

    mgr._prepare_parquet_trip_queue(trips)

    assert mgr._parquet_replay_stats["original_count"] == 4
    assert mgr._parquet_replay_stats["scheduled_count_per_loop"] == 2
    assert mgr._parquet_replay_stats["downsampled_count"] == 2
    assert len(mgr._trip_template) == 2
    assert len(mgr._trip_queue) == 2


def test_parquet_trip_queue_loops_when_runtime_exceeds_source_duration():
    from app.simulation import ExperimentConfig
    mgr = make_manager()
    mgr.experiment_config = ExperimentConfig(passengers_per_5min=1, seed=123)
    mgr._runtime_duration = 900.0
    trips = [_make_trip(10.0)]

    mgr._prepare_parquet_trip_queue(trips)

    assert mgr._parquet_replay_stats["source_duration_s"] == 300.0
    assert mgr._parquet_replay_stats["loop_count"] == 3
    assert [trip["sim_time"] for trip in mgr._trip_queue] == [10.0, 310.0, 610.0]


def test_passenger_elasticity_zero_keeps_raw_spawn_count():
    from app.simulation import ExperimentConfig
    mgr = make_manager()
    mgr.experiment_config = ExperimentConfig(
        target_p=0.8,
        elasticity=0.6,
        beta_f=0.006,
        passenger_elasticity=0.0,
    )
    mgr._surge_by_h3 = {"892830828cbffff": 4.0}

    assert mgr._adjust_spawn_count_for_elasticity(5, "892830828cbffff", 300.0) == 5


def test_negative_passenger_elasticity_removes_spawn_candidates():
    from app.simulation import ExperimentConfig
    mgr = make_manager()
    mgr.experiment_config = ExperimentConfig(
        target_p=0.8,
        elasticity=0.6,
        beta_f=0.006,
        passenger_elasticity=-0.6,
    )
    mgr._surge_by_h3 = {"892830828cbffff": 4.0}

    adjusted = mgr._adjust_spawn_count_for_elasticity(10, "892830828cbffff", 300.0)

    assert adjusted == 4
    assert mgr._event_log[-1]["type"] == "passenger_elasticity"
    assert mgr._event_log[-1]["raw_spawn_candidate_count"] == 10
    assert mgr._event_log[-1]["actual_spawned_passengers"] == 4
