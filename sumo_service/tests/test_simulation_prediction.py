from datetime import datetime
from types import SimpleNamespace

import pytest
from traci import constants as tc

import app.simulation as simulation
from app.fare import TripAccumulator
from app.passenger import Passenger
from app.pricing import raw_surge_bucket
from app.simulation import ExperimentConfig, SIM_BASE_DATETIME, SimulationManager


class FakePredictionDemandProvider:
    def __init__(self, demand: dict[str, float]) -> None:
        self.demand = demand
        self.calls: list[dict] = []

    def demand_by_h3(self, sim_datetime, *, mode, actual_demand):
        if mode not in {"sync", "async"}:
            raise AssertionError(f"unsupported fake prediction mode: {mode}")
        self.calls.append({
            "sim_datetime": sim_datetime,
            "mode": mode,
            "actual_demand": actual_demand,
        })
        return dict(self.demand)

    def diagnostics(self):
        return {"prediction_request_count": len(self.calls)}

    def close(self):
        pass


def test_predicted_demand_source_uses_prediction_for_surge(monkeypatch):
    monkeypatch.setattr(simulation, "cell_center_latlng", lambda cell: (40.0, -73.0))
    provider = FakePredictionDemandProvider({"h3_a": 4.0})
    manager = SimulationManager(ExperimentConfig(demand_source="predicted"))
    manager._prediction_demand_provider = provider

    manager._build_surge_cells({"h3_a": 1}, {"h3_a": 1}, {"h3_a": 1}, 0.0)

    assert provider.calls == [
        {
            "sim_datetime": datetime(2013, 7, 8, 8, 0, 0),
            "mode": "sync",
            "actual_demand": {"h3_a": 1},
        }
    ]
    assert manager._surge_cells == [
        {
            "h3": "h3_a",
            "bucket": "raw_gte_3_5",
            "supply": 1,
            "demand": 4.0,
            "actual_demand": 1,
            "surge": 4.9,
            "raw_surge": pytest.approx(10.079368399158986),
            "target_matching_rate": 0.85,
            "center": {"lat": 40.0, "lng": -73.0},
        }
    ]
    assert manager._surge_by_h3 == {"h3_a": 4.9}
    assert manager._raw_surge_by_h3 == {"h3_a": pytest.approx(10.079368399158986)}
    assert manager._target_matching_rate_by_h3 == {"h3_a": 0.85}
    assert manager._surge_diagnostics[-1] == {
        "sim_time": 0.0,
        "h3": "h3_a",
        "bucket": "raw_gte_3_5",
        "supply": 1,
        "actual_demand": 1,
        "demand_for_surge": 4.0,
        "raw_surge": pytest.approx(10.079368399158986),
        "target_matching_rate": 0.85,
        "surge": 4.9,
    }


def test_actual_demand_source_uses_grid_demand_for_surge(monkeypatch):
    monkeypatch.setattr(simulation, "cell_center_latlng", lambda cell: (40.0, -73.0))
    manager = SimulationManager(ExperimentConfig(demand_source="actual"))

    manager._build_surge_cells({"h3_a": 1}, {"h3_a": 2}, {"h3_a": 2}, 0.0)

    assert manager._surge_cells == [
        {
            "h3": "h3_a",
            "bucket": "raw_lt_3_5",
            "supply": 1,
            "demand": 2,
            "actual_demand": 2,
            "surge": pytest.approx(3.2),
            "raw_surge": pytest.approx(3.174802103936399),
            "target_matching_rate": 0.80,
            "center": {"lat": 40.0, "lng": -73.0},
        }
    ]
    assert manager._surge_by_h3 == {"h3_a": pytest.approx(3.2)}
    # 셀 관측은 sim_time 0.0(0~5분 버킷)에 기록되었으므로 6분 시점에 조회한다.
    manager._state["sim_time"] = 360.0
    cells = {
        row["bucket"]: row
        for row in manager.get_kpi_summary()["cells"]["by_raw_bucket"]
    }
    assert cells["raw_lt_3_5"]["unique_cell_count"] == 1
    assert cells["raw_lt_3_5"]["avg_supply"] == 1.0
    assert cells["raw_lt_3_5"]["avg_demand"] == 2.0
    assert cells["raw_lt_3_5"]["avg_raw_surge"] == pytest.approx(3.174802103936399)
    assert cells["raw_lt_3_5"]["sample_h3_cells"] == ["h3_a"]


def test_raw_surge_bucket_boundaries():
    assert raw_surge_bucket(1.4999) == "raw_lt_1_5"
    assert raw_surge_bucket(1.5) == "raw_lt_2_5"
    assert raw_surge_bucket(2.5) == "raw_lt_3_5"
    assert raw_surge_bucket(3.5) == "raw_gte_3_5"


def test_runtime_surge_does_not_append_experiment_diagnostics(monkeypatch):
    monkeypatch.setattr(simulation, "cell_center_latlng", lambda cell: (40.0, -73.0))
    manager = SimulationManager()

    manager._build_surge_cells({"h3_a": 1}, {"h3_a": 2}, {"h3_a": 2}, 0.0)

    assert manager._surge_diagnostics == []


def test_experiment_pricing_uses_current_taxi_features(monkeypatch):
    captured = {}

    monkeypatch.setattr(
        simulation,
        "_acceptance_features",
        lambda **kwargs: SimpleNamespace(
            dV_without_fare=7.5,
            t_pu=kwargs["D_pu"] * 5.0,
        ),
    )

    def fake_required_fare_for_target_features(**kwargs):
        captured.update(kwargs)
        return 30.0

    monkeypatch.setattr(
        simulation,
        "_required_fare_for_target_features",
        fake_required_fare_for_target_features,
    )

    manager = SimulationManager(ExperimentConfig())
    manager._latlng = lambda x, y: (40.0, -73.0)
    manager._taxi_last_dropoff_cells = {"taxi_current": "h3_last"}
    manager._raw_surge_by_h3 = {"h3_pickup": 3.0}
    candidate = Passenger(
        id="p_0",
        x=0.0,
        y=0.0,
        lat=40.0,
        lng=-73.0,
        pickup_edge="pickup_edge",
        dropoff_edge="dropoff_edge",
        dropoff_x=1.0,
        dropoff_y=1.0,
        dropoff_lat=40.1,
        dropoff_lng=-73.1,
        expected_distance_m=1609.344,
        expected_fare=1000,
        spawn_time=0.0,
        state="waiting",
        h3_pickup="h3_pickup",
        h3_dropoff="h3_dropoff",
    )

    result = manager._dispatch_pricing(
        candidate=candidate,
        sim_time=0.0,
        sub_results={
            "taxi_current": {tc.VAR_POSITION: (0.0, 0.0)},
            "taxi_other": {tc.VAR_POSITION: (999.0, 999.0)},
        },
        current_veh_id="taxi_current",
        current_route=SimpleNamespace(edges=("edge_a",), length=3218.688),
    )

    assert captured["dV_without_fare"] == 7.5
    assert captured["D_pu"] == pytest.approx(2.0)
    assert captured["T_pu"] == pytest.approx(10.0)
    assert result["required_fare_usd"] == 30.0
    assert result["calculated_surge"] == pytest.approx(3.0)
    assert result["final_surge"] == pytest.approx(3.0)
    assert result["pricing_driver_count"] == 1


class FakeHistoryStore:
    def __init__(self) -> None:
        self.spawn_records: list[tuple[datetime, str | None]] = []
        self.dropoff_records: list[tuple[datetime, str | None]] = []

    def record_spawn(self, sim_datetime, h3_cell):
        self.spawn_records.append((sim_datetime, h3_cell))

    def record_dropoff(self, sim_datetime, h3_cell):
        self.dropoff_records.append((sim_datetime, h3_cell))

    def diagnostics(self):
        return {
            "history_required_count": len(self.spawn_records) + len(self.dropoff_records),
            "history_missing_count": 0,
            "history_missing_rate": 0.0,
        }


class ClosableProvider:
    def __init__(self) -> None:
        self.closed = False

    def close(self) -> None:
        self.closed = True


class CloseSettledDiagnosticsProvider(ClosableProvider):
    def __init__(self) -> None:
        super().__init__()
        self.request_count = 0

    def close(self) -> None:
        super().close()
        self.request_count = 3

    def diagnostics(self):
        return {
            "prediction_request_count": self.request_count,
            "prediction_success_count": self.request_count,
        }


def test_random_passenger_creation_records_spawn_history(monkeypatch):
    manager = SimulationManager(ExperimentConfig())
    history = FakeHistoryStore()
    manager._history_store = history
    manager._routable_edges = ["pickup_edge", "dropoff_edge"]
    manager._latlng = lambda x, y: (x, y)
    manager._get_edge_midpoint = lambda edge: {
        "pickup_edge": (40.70, -73.99),
        "dropoff_edge": (40.71, -73.98),
    }[edge]
    choices = iter(["pickup_edge", "dropoff_edge"])
    monkeypatch.setattr(simulation._random, "choice", lambda values: next(choices))
    monkeypatch.setattr(
        simulation.traci.simulation,
        "findRoute",
        lambda pickup, dropoff: SimpleNamespace(edges=[pickup, dropoff], length=1200.0),
    )

    manager._create_passenger_random(300.0)

    passenger = manager._passengers["p_0"]
    assert history.spawn_records == [
        (SIM_BASE_DATETIME + simulation.timedelta(seconds=300.0), passenger.h3_pickup)
    ]


def test_parquet_passenger_creation_records_spawn_history(monkeypatch):
    manager = SimulationManager(ExperimentConfig())
    history = FakeHistoryStore()
    manager._history_store = history
    manager._routable_edges_set = {"pickup_edge", "dropoff_edge"}
    manager._latlng = lambda x, y: (x, y)
    manager._get_edge_midpoint = lambda edge: {
        "pickup_edge": (40.70, -73.99),
        "dropoff_edge": (40.71, -73.98),
    }[edge]
    monkeypatch.setattr(
        simulation.traci.simulation,
        "findRoute",
        lambda pickup, dropoff: SimpleNamespace(edges=[pickup, dropoff], length=1200.0),
    )

    manager._create_passenger_from_trip(
        {
            "pickup_edge": "pickup_edge",
            "dropoff_edge": "dropoff_edge",
            "h3_pickup": "trip_h3",
        },
        600.0,
    )

    assert history.spawn_records == [
        (SIM_BASE_DATETIME + simulation.timedelta(seconds=600.0), "trip_h3")
    ]


def test_trip_completion_paths_record_dropoff_history(monkeypatch):
    monkeypatch.setattr(simulation, "calculate_fare", lambda accum: 1234)

    normal = _manager_with_active_trip(FakeHistoryStore())
    normal._random_route_from = lambda edge: []
    normal._update_taxi_states(
        100.0,
        {
            "taxi_1": {
                tc.VAR_POSITION: (0.0, 0.0),
                tc.VAR_ROAD_ID: "dropoff_edge",
            }
        },
    )
    assert normal._history_store.dropoff_records == [
        (SIM_BASE_DATETIME + simulation.timedelta(seconds=100.0), "dropoff_h3")
    ]

    timeout = _manager_with_active_trip(FakeHistoryStore())
    timeout._random_route_from = lambda edge: []
    timeout._update_taxi_states(
        2000.0,
        {
            "taxi_1": {
                tc.VAR_POSITION: (0.0, 0.0),
                tc.VAR_ROAD_ID: "not_dropoff",
            }
        },
    )
    assert timeout._history_store.dropoff_records == [
        (SIM_BASE_DATETIME + simulation.timedelta(seconds=2000.0), "dropoff_h3")
    ]

    removed = _manager_with_active_trip(FakeHistoryStore())
    removed._accumulate_fares(300.0, {})
    assert removed._history_store.dropoff_records == [
        (SIM_BASE_DATETIME + simulation.timedelta(seconds=300.0), "dropoff_h3")
    ]


def test_prediction_provider_is_closed_on_reinitialize_and_run_reset(monkeypatch):
    monkeypatch.setattr(simulation, "PASSENGER_SOURCE", "random")
    manager = SimulationManager(ExperimentConfig())
    manager._latlng = lambda x, y: (x, y)
    manager._routable_edges = []
    provider = ClosableProvider()
    manager._prediction_demand_provider = provider

    manager._initialize_prediction_components()

    assert provider.closed
    assert manager._prediction_demand_provider is None

    reset_provider = ClosableProvider()
    manager._prediction_demand_provider = reset_provider
    manager._history_store = FakeHistoryStore()
    manager._surge_diagnostics = [{"stale": True}]
    monkeypatch.setattr(manager, "_run_loop", lambda: None)

    assert manager.run_experiment() == []
    assert reset_provider.closed
    assert manager._history_store is None
    assert manager._surge_diagnostics == []


def test_run_experiment_emits_prediction_history_and_surge_diagnostics(monkeypatch):
    monkeypatch.setattr(simulation, "PASSENGER_SOURCE", "random")
    manager = SimulationManager(ExperimentConfig())
    provider = ClosableProvider()
    provider.diagnostics = lambda: {"prediction_request_count": 2}
    history = FakeHistoryStore()
    history.record_spawn(SIM_BASE_DATETIME, "h3_a")

    def fake_run_loop():
        manager._prediction_demand_provider = provider
        manager._history_store = history
        manager._surge_diagnostics = [
            {
                "sim_time": 60.0,
                "h3": "h3_a",
                "supply": 1,
                "actual_demand": 2.0,
                "demand_for_surge": 3.0,
                "surge": 1.2,
            }
        ]

    monkeypatch.setattr(manager, "_run_loop", fake_run_loop)

    assert manager.run_experiment() == [
        {
            "type": "diagnostics",
            "prediction_request_count": 2,
            "history_required_count": 1,
            "history_missing_count": 0,
            "history_missing_rate": 0.0,
        },
        {
            "type": "surge_diagnostic",
            "sim_time": 60.0,
            "h3": "h3_a",
            "supply": 1,
            "actual_demand": 2.0,
            "demand_for_surge": 3.0,
            "surge": 1.2,
        },
    ]
    assert provider.closed


def test_run_experiment_closes_prediction_provider_before_diagnostics(monkeypatch):
    monkeypatch.setattr(simulation, "PASSENGER_SOURCE", "random")
    manager = SimulationManager(ExperimentConfig())
    provider = CloseSettledDiagnosticsProvider()

    def fake_run_loop():
        provider.request_count = 1
        manager._prediction_demand_provider = provider

    monkeypatch.setattr(manager, "_run_loop", fake_run_loop)

    assert manager.run_experiment() == [
        {
            "type": "diagnostics",
            "prediction_request_count": 3,
            "prediction_success_count": 3,
        }
    ]
    assert provider.closed
    assert manager._prediction_demand_provider is None


def test_run_experiment_resets_stale_run_state_before_loop(monkeypatch):
    monkeypatch.setattr(simulation, "PASSENGER_SOURCE", "random")
    manager = SimulationManager(ExperimentConfig())
    stale_provider = ClosableProvider()
    manager._prediction_demand_provider = stale_provider
    manager._history_store = FakeHistoryStore()
    manager._last_spawn_interval = 7
    manager._last_surge_interval = 9
    manager._passenger_counter = 3
    manager._passengers = {"p_stale": object()}
    manager._active_trips = {"taxi_1": object()}
    manager._taxi_targets = {"taxi_1": "p_stale"}
    manager._taxi_states = {"taxi_1": "occupied"}
    manager._taxi_dispatch_times = {"taxi_1": 12.0}
    manager._taxi_dispatch_ids = {"taxi_1": "dispatch_stale"}
    manager._taxi_dispatch_surge = {"taxi_1": 2.0}
    manager._taxi_last_dropoff_cells = {"taxi_1": "h3_old"}
    manager._taxi_previous_dropoff_times = {"taxi_1": 30.0}
    manager._taxi_appeared = {"taxi_1"}
    manager._taxi_missing_since = {"taxi_1": 50.0}
    manager._bg_appeared = {"bg_1"}
    manager._bg_missing_since = {"bg_1": 60.0}
    manager._routable_edges = ["edge_old"]
    manager._routable_edges_set = {"edge_old"}
    manager._edge_weights = [1.0]
    manager._taxi_route_len = {"taxi_1": 2}
    manager._taxi_last_extend_route_index = {"taxi_1": 1}
    manager._taxi_pickup_route_index = {"taxi_1": 1}
    manager._taxi_dropoff_route_index = {"taxi_1": 2}
    manager._bg_route_len = {"bg_1": 2}
    manager._trip_queue = [{"sim_time": 1.0}]
    manager._completed_passengers = [{"passenger_id": "p_stale"}]
    manager._completed_trip_count = 3
    manager._surge_cells = [{"h3": "old"}]
    manager._surge_by_h3 = {"old": 2.0}
    manager._raw_surge_by_h3 = {"old": 2.0}
    manager._target_matching_rate_by_h3 = {"old": 0.7}
    manager._surge_diagnostics = [{"stale": True}]
    manager._event_log = [{"type": "stale"}]

    def inspect_reset_state():
        assert manager._history_store is None
        assert manager._last_spawn_interval == -1
        assert manager._last_surge_interval == -1
        assert manager._passenger_counter == 0
        assert manager._passengers == {}
        assert manager._active_trips == {}
        assert manager._taxi_targets == {}
        assert manager._taxi_states == {}
        assert manager._taxi_dispatch_times == {}
        assert manager._taxi_dispatch_ids == {}
        assert manager._taxi_dispatch_surge == {}
        assert manager._taxi_last_dropoff_cells == {}
        assert manager._taxi_previous_dropoff_times == {}
        assert manager._taxi_appeared == set()
        assert manager._taxi_missing_since == {}
        assert manager._bg_appeared == set()
        assert manager._bg_missing_since == {}
        assert manager._routable_edges == []
        assert manager._routable_edges_set == set()
        assert manager._edge_weights == []
        assert manager._taxi_route_len == {}
        assert manager._taxi_last_extend_route_index == {}
        assert manager._taxi_pickup_route_index == {}
        assert manager._taxi_dropoff_route_index == {}
        assert manager._bg_route_len == {}
        assert manager._trip_queue == []
        assert manager._completed_passengers == []
        assert manager._completed_trip_count == 0
        assert manager._surge_cells == []
        assert manager._surge_by_h3 == {}
        assert manager._raw_surge_by_h3 == {}
        assert manager._target_matching_rate_by_h3 == {}
        assert manager._surge_diagnostics == []
        assert manager._event_log == []
        assert manager.status == simulation.SimStatus.RUNNING

    monkeypatch.setattr(manager, "_run_loop", inspect_reset_state)

    assert manager.run_experiment() == []
    assert stale_provider.closed


def test_status_summary_contains_runtime_counts():
    manager = SimulationManager()
    manager.status = simulation.SimStatus.RUNNING
    manager._state = {
        "vehicles": [
            {"id": "taxi_0", "state": "empty"},
            {"id": "taxi_1", "state": "occupied"},
            {"id": "car_0", "state": "car"},
        ],
        "passengers": [],
        "sim_time": 42.5,
    }
    manager._taxi_states = {
        "taxi_0": "empty",
        "taxi_1": "occupied",
        "taxi_2": "dispatched",
    }
    manager._passengers = {
        "p_waiting": Passenger(
            id="p_waiting",
            x=0.0,
            y=0.0,
            lat=40.0,
            lng=-73.0,
            pickup_edge="pickup_edge",
            dropoff_edge="dropoff_edge",
            dropoff_x=1.0,
            dropoff_y=1.0,
            dropoff_lat=40.1,
            dropoff_lng=-73.1,
            expected_distance_m=1000.0,
            expected_fare=1000,
            spawn_time=0.0,
            state="waiting",
        ),
        "p_assigned": Passenger(
            id="p_assigned",
            x=0.0,
            y=0.0,
            lat=40.0,
            lng=-73.0,
            pickup_edge="pickup_edge",
            dropoff_edge="dropoff_edge",
            dropoff_x=1.0,
            dropoff_y=1.0,
            dropoff_lat=40.1,
            dropoff_lng=-73.1,
            expected_distance_m=1000.0,
            expected_fare=1000,
            spawn_time=0.0,
            state="assigned",
            taxi_id="taxi_0",
        ),
    }
    manager._completed_trip_count = 5

    summary = manager.get_status_summary()

    assert summary["status"] == simulation.SimStatus.RUNNING
    assert summary["sim_time"] == 42.5
    assert summary["vehicle_count"] == 3
    assert summary["taxi_count"] == 2
    assert summary["empty_taxi_count"] == 1
    assert summary["dispatched_taxi_count"] == 0
    assert summary["occupied_taxi_count"] == 1
    assert summary["waiting_passenger_count"] == 1
    assert summary["assigned_passenger_count"] == 1
    assert summary["completed_trip_count"] == 5
    assert summary["h3_resolution"] == simulation.H3_RESOLUTION
    assert summary["passenger_source"] == simulation.PASSENGER_SOURCE


def _make_passenger(passenger_id: str, spawn_time: float) -> Passenger:
    return Passenger(
        id=passenger_id, x=0.0, y=0.0, lat=40.0, lng=-73.0,
        pickup_edge="pickup_edge", dropoff_edge="dropoff_edge",
        dropoff_x=1.0, dropoff_y=1.0, dropoff_lat=40.1, dropoff_lng=-73.1,
        expected_distance_m=1000.0, expected_fare=1000,
        spawn_time=spawn_time, state="assigned",
    )


def test_kpi_summary_groups_matching_by_raw_surge_bucket():
    manager = SimulationManager()
    # 두 이벤트 모두 0~5분(0~300s) 버킷에 들어간다.
    manager._record_dispatch_kpi({"raw_surge": 1.2, "passenger_id": "p_low"}, accepted=True, sim_time=10.0)
    manager._record_dispatch_kpi({"raw_surge": 2.8, "passenger_id": "p_high"}, accepted=False, sim_time=20.0)

    # 6분 시점에 조회 → 직전 완료 버킷(0~5분)을 응답.
    manager._state["sim_time"] = 360.0
    summary = manager.get_kpi_summary()
    buckets = {row["bucket"]: row for row in summary["matching"]["by_raw_bucket"]}

    assert summary["window"]["start_seconds"] == 0.0
    assert summary["window"]["end_seconds"] == 300.0
    assert summary["window"]["available"] is True
    assert buckets["raw_lt_1_5"]["request_count"] == 1
    assert buckets["raw_lt_1_5"]["matched_count"] == 1
    assert buckets["raw_lt_1_5"]["actual_rate"] == 1.0
    assert buckets["raw_lt_3_5"]["request_count"] == 1
    assert buckets["raw_lt_3_5"]["matched_count"] == 0
    assert summary["matching"]["request_count"] == 2
    assert summary["matching"]["matched_count"] == 1


def test_kpi_summary_records_bucket_wait_and_revenue():
    manager = SimulationManager()
    passenger = _make_passenger("p_wait", spawn_time=10.0)
    manager._record_dispatch_kpi({"raw_surge": 1.8, "passenger_id": passenger.id}, accepted=True, sim_time=10.0)
    manager._record_passenger_boarded_kpi(passenger, 40.0)
    manager._record_trip_kpi(fare=2500, meter_fare=2000, sim_time=40.0)

    manager._state["sim_time"] = 360.0
    summary = manager.get_kpi_summary()
    bucket = {
        row["bucket"]: row for row in summary["matching"]["by_raw_bucket"]
    }["raw_lt_2_5"]

    assert bucket["average_wait_seconds"] == 30.0
    assert summary["passenger_waiting_incentive"]["average_wait_seconds"] == 30.0
    assert summary["driver_revenue"]["completed_trip_count"] == 1
    assert summary["driver_revenue"]["total_cents"] == 2500


def test_kpi_summary_returns_last_completed_five_minute_bucket():
    manager = SimulationManager()
    # 75~80분(4500~4800s) 구간 이벤트
    manager._record_dispatch_kpi({"raw_surge": 1.2, "passenger_id": "a"}, accepted=True, sim_time=4500.0)
    manager._record_trip_kpi(fare=2500, sim_time=4700.0)
    # 진행 중인 80~85분 버킷 이벤트 — 응답에서 제외되어야 한다.
    manager._record_dispatch_kpi({"raw_surge": 1.2, "passenger_id": "b"}, accepted=True, sim_time=4850.0)
    manager._record_trip_kpi(fare=9999, sim_time=4850.0)

    # 83분30초(5010s)에 조회 → 75~80분 통계만 응답.
    manager._state["sim_time"] = 5010.0
    summary = manager.get_kpi_summary()

    assert summary["window"]["start_seconds"] == 4500.0
    assert summary["window"]["end_seconds"] == 4800.0
    assert summary["window"]["available"] is True
    assert summary["matching"]["request_count"] == 1
    assert summary["matching"]["matched_count"] == 1
    assert summary["driver_revenue"]["completed_trip_count"] == 1
    assert summary["driver_revenue"]["total_cents"] == 2500


def test_kpi_summary_empty_when_no_completed_bucket():
    manager = SimulationManager()
    manager._record_dispatch_kpi({"raw_surge": 1.2, "passenger_id": "a"}, accepted=True, sim_time=100.0)

    # 아직 첫 5분 버킷이 끝나기 전(4분) 시점.
    manager._state["sim_time"] = 240.0
    summary = manager.get_kpi_summary()

    assert summary["window"]["available"] is False
    assert summary["matching"]["request_count"] == 0
    assert summary["driver_revenue"]["completed_trip_count"] == 0


def test_kpi_summary_wait_seconds_scoped_to_bucket():
    manager = SimulationManager()
    # 75~80분 버킷: 대기 20초
    p1 = _make_passenger("p1", spawn_time=4500.0)
    manager._record_dispatch_kpi({"raw_surge": 1.8, "passenger_id": p1.id}, accepted=True, sim_time=4500.0)
    manager._record_passenger_boarded_kpi(p1, 4520.0)
    # 진행 중 버킷: 큰 대기시간(응답에 섞이면 안 됨)
    p2 = _make_passenger("p2", spawn_time=4800.0)
    manager._record_dispatch_kpi({"raw_surge": 1.8, "passenger_id": p2.id}, accepted=True, sim_time=4850.0)
    manager._record_passenger_boarded_kpi(p2, 4850.0)

    manager._state["sim_time"] = 5010.0
    summary = manager.get_kpi_summary()
    assert summary["passenger_waiting"]["average_wait_seconds"] == 20.0


def test_kpi_time_buckets_preserve_cumulative_average_for_pricing():
    manager = SimulationManager()
    # 서로 다른 5분 버킷에 트립을 기록해도 가격 산정용 누적 평균은 전체를 반영한다.
    manager._record_trip_kpi(fare=2000, sim_time=100.0)
    manager._record_trip_kpi(fare=4000, sim_time=4000.0)
    assert manager._average_trip_fare_cents() == 3000


def test_estimate_pickup_eta_uses_route_travel_time_when_available():
    route = SimpleNamespace(length=800.0, travelTime=123.4)

    assert SimulationManager._estimate_pickup_eta_seconds(route) == 123


def test_estimate_pickup_eta_falls_back_to_route_length():
    route = SimpleNamespace(length=800.0)

    assert SimulationManager._estimate_pickup_eta_seconds(route) == 100


def test_manual_entity_detection_only_matches_manual_ids():
    assert SimulationManager._is_manual_entity("upax_1")
    assert SimulationManager._is_manual_entity("utaxi_1")
    assert SimulationManager._is_manual_entity("p_1", "utaxi_1")
    assert not SimulationManager._is_manual_entity("p_1", "taxi_1")


@pytest.mark.asyncio
async def test_start_resets_stale_run_state_before_runtime_launch(monkeypatch):
    manager = SimulationManager()
    manager.status = simulation.SimStatus.FINISHED
    manager._last_spawn_interval = 7
    manager._passengers = {"p_stale": object()}
    manager._active_trips = {"taxi_1": object()}
    manager._taxi_states = {"taxi_1": "occupied"}
    manager._surge_cells = [{"h3": "old"}]
    manager._surge_by_h3 = {"old": 2.0}
    manager._raw_surge_by_h3 = {"old": 2.0}
    manager._target_matching_rate_by_h3 = {"old": 0.7}
    manager._surge_diagnostics = [{"stale": True}]
    manager._event_log = [{"type": "stale"}]
    stale_provider = ClosableProvider()
    manager._prediction_demand_provider = stale_provider

    monkeypatch.setattr(simulation, "get_pool", lambda: None)

    class FakeLoop:
        def __init__(self) -> None:
            self.submitted = None

        def run_in_executor(self, executor, func):
            assert manager._last_spawn_interval == -1
            assert manager._passengers == {}
            assert manager._active_trips == {}
            assert manager._taxi_states == {}
            assert manager._surge_cells == []
            assert manager._surge_by_h3 == {}
            assert manager._raw_surge_by_h3 == {}
            assert manager._target_matching_rate_by_h3 == {}
            assert manager._surge_diagnostics == []
            assert manager._event_log == []
            assert stale_provider.closed
            self.submitted = (executor, func)
            return "executor-task"

    fake_loop = FakeLoop()
    monkeypatch.setattr(simulation.asyncio, "get_event_loop", lambda: fake_loop)

    def fake_create_task(coro):
        coro.close()
        return "broadcast-task"

    monkeypatch.setattr(simulation.asyncio, "create_task", fake_create_task)

    await manager.start()

    assert manager.status == simulation.SimStatus.RUNNING
    assert manager._loop is fake_loop
    assert manager._executor_task == "executor-task"
    assert manager._broadcast_task == "broadcast-task"
    assert fake_loop.submitted == (None, manager._run_loop)


def _manager_with_active_trip(history: FakeHistoryStore) -> SimulationManager:
    manager = SimulationManager(ExperimentConfig())
    manager._history_store = history
    passenger = Passenger(
        id="p_0",
        x=0.0,
        y=0.0,
        lat=40.0,
        lng=-73.0,
        pickup_edge="pickup_edge",
        dropoff_edge="dropoff_edge",
        dropoff_x=1.0,
        dropoff_y=1.0,
        dropoff_lat=40.1,
        dropoff_lng=-73.1,
        expected_distance_m=1000.0,
        expected_fare=1000,
        spawn_time=0.0,
        state="picked_up",
        h3_pickup="pickup_h3",
        h3_dropoff="dropoff_h3",
    )
    manager._passengers = {"p_0": passenger}
    manager._taxi_targets = {"taxi_1": "p_0"}
    manager._taxi_states = {"taxi_1": "occupied"}
    manager._active_trips = {
        "taxi_1": TripAccumulator(
            passenger_id="p_0",
            pickup_sim_time=0.0,
            dispatch_id="dispatch_1",
            dispatch_sim_time=0.0,
            last_distance_snapshot=0.0,
        )
    }
    return manager
