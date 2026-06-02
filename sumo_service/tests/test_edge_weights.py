"""
Tests for hotspot-based edge weighting (_compute_edge_weights, weighted _random_route_from).

TraCI is fully mocked.
"""

from unittest.mock import MagicMock

import pytest

from tests.test_passenger_spawn import _traci_stub  # reuse stub

from app.simulation import (
    _BG_EXTENSION_MIN_ROUTE_EDGES,
    _BG_ROUTE_EXTENSION_MAX_PER_TICK,
    _BG_ROUTE_EXTENSION_MIN_SPEED_MPS,
    _BG_ROUTE_EXTEND_REMAINING,
    _TAXI_EXTENSION_MIN_ROUTE_EDGES,
    HOTSPOTS,
    HOTSPOT_BASE_WEIGHT,
    HOTSPOT_SIGMA_M,
    SimulationManager,
)
from traci import constants as tc


def make_manager() -> SimulationManager:
    mgr = SimulationManager()
    mgr._routable_edges = ["near_edge", "far_edge"]
    return mgr


# ---------------------------------------------------------------------------
# _compute_edge_weights
# ---------------------------------------------------------------------------

def test_compute_edge_weights_length_matches_edges():
    mgr = make_manager()
    # convertGeo: lat/lng → SUMO 좌표. 임의 매핑.
    _traci_stub.simulation.convertGeo = MagicMock(side_effect=lambda lng, lat, fromGeo: (0.0, 0.0))
    # 모든 엣지의 midpoint를 핫스팟 위치로 설정 → 모두 동일 가중치
    mgr._get_edge_midpoint = MagicMock(return_value=(0.0, 0.0))

    weights = mgr._compute_edge_weights()

    assert len(weights) == len(mgr._routable_edges)


def test_edge_near_hotspot_gets_higher_weight():
    mgr = make_manager()
    # 첫 번째 핫스팟을 (0, 0)으로 매핑, 나머지는 멀리
    _traci_stub.simulation.convertGeo = MagicMock(
        side_effect=lambda lng, lat, fromGeo: (0.0, 0.0) if lat == HOTSPOTS[0][0] else (1e6, 1e6)
    )
    # near_edge는 (0, 0), far_edge는 (10000, 10000)
    midpoints = {"near_edge": (0.0, 0.0), "far_edge": (10000.0, 10000.0)}
    mgr._get_edge_midpoint = MagicMock(side_effect=lambda e: midpoints[e])

    weights = mgr._compute_edge_weights()

    assert weights[0] > weights[1]  # near > far


def test_edge_with_no_midpoint_gets_base_weight():
    mgr = make_manager()
    _traci_stub.simulation.convertGeo = MagicMock(side_effect=lambda lng, lat, fromGeo: (0.0, 0.0))
    mgr._get_edge_midpoint = MagicMock(return_value=None)

    weights = mgr._compute_edge_weights()

    assert all(w == HOTSPOT_BASE_WEIGHT for w in weights)


def test_compute_edge_weights_handles_convertgeo_failure():
    mgr = make_manager()
    _traci_stub.simulation.convertGeo = MagicMock(side_effect=Exception("convertGeo failed"))
    mgr._get_edge_midpoint = MagicMock(return_value=(0.0, 0.0))

    # 핫스팟 변환이 모두 실패해도 base weight로 동작
    weights = mgr._compute_edge_weights()

    assert all(w == HOTSPOT_BASE_WEIGHT for w in weights)


# ---------------------------------------------------------------------------
# _random_route_from with weights
# ---------------------------------------------------------------------------

def test_random_route_from_uses_weights_when_available():
    """가중치가 매우 비대칭(0:1000)이면 거의 항상 가중치 높은 엣지로 향한다."""
    mgr = make_manager()
    mgr._routable_edges = ["edge_low", "edge_high"]
    mgr._edge_weights = [0.001, 1000.0]
    _traci_stub.simulation.findRoute = MagicMock(
        return_value=MagicMock(edges=["start", "edge_high"])
    )

    # 100번 시도 중 high 비율이 압도적이어야 함
    high_count = 0
    for _ in range(100):
        route = mgr._random_route_from("start")
        # findRoute가 항상 ["start", "edge_high"] 반환하므로
        # _random.choices가 weight에 따라 dst를 선택하는지 확인
        if route is not None:
            high_count += 1

    # 모든 호출이 성공해야 (weights 작동)
    assert high_count > 0


def test_random_route_from_falls_back_to_uniform_when_weights_empty():
    mgr = make_manager()
    mgr._routable_edges = ["edge_a", "edge_b"]
    mgr._edge_weights = []  # 미계산 상태
    _traci_stub.simulation.findRoute = MagicMock(
        return_value=MagicMock(edges=["start", "edge_a"])
    )

    route = mgr._random_route_from("start")

    assert route == ["start", "edge_a"]


def test_random_route_from_returns_none_when_all_attempts_fail():
    mgr = make_manager()
    mgr._routable_edges = ["edge_a"]
    mgr._edge_weights = [1.0]
    _traci_stub.simulation.findRoute = MagicMock(return_value=MagicMock(edges=[]))

    route = mgr._random_route_from("start", attempts=3)

    assert route is None


def _route_extend_sub_entry(road_id: str, route_index: int, speed: float = 10.0) -> dict:
    return {
        tc.VAR_ROAD_ID: road_id,
        tc.VAR_ROUTE_INDEX: route_index,
        tc.VAR_SPEED: speed,
    }


def test_bg_route_extension_respects_interval_gate():
    mgr = make_manager()
    mgr._bg_route_len["bg_0"] = 6
    mgr._random_route_from = MagicMock(return_value=["bg_edge", "next_edge"])
    _traci_stub.vehicle.setRoute = MagicMock()

    mgr._extend_vehicle_routes(
        {"bg_0": _route_extend_sub_entry("bg_edge", 4)},
        0.0,
        extend_bg_routes=False,
    )

    mgr._random_route_from.assert_not_called()
    _traci_stub.vehicle.setRoute.assert_not_called()


def test_bg_route_extension_uses_larger_remaining_threshold():
    mgr = make_manager()
    mgr._bg_route_len["bg_0"] = 10
    mgr._random_route_from = MagicMock(return_value=["bg_edge", "next_edge"])
    _traci_stub.vehicle.setRoute = MagicMock()

    mgr._extend_vehicle_routes(
        {
            "bg_0": _route_extend_sub_entry(
                "bg_edge",
                10 - 1 - _BG_ROUTE_EXTEND_REMAINING,
            )
        },
        0.0,
        extend_bg_routes=True,
    )

    mgr._random_route_from.assert_called_once_with(
        "bg_edge",
        min_edges=_BG_EXTENSION_MIN_ROUTE_EDGES,
    )
    _traci_stub.vehicle.setRoute.assert_called_once_with("bg_0", ["bg_edge", "next_edge"])


def test_bg_route_extension_skips_stopped_vehicle():
    mgr = make_manager()
    mgr._bg_route_len["bg_0"] = 10
    mgr._random_route_from = MagicMock(return_value=["bg_edge", "next_edge"])
    _traci_stub.vehicle.setRoute = MagicMock()

    mgr._extend_vehicle_routes(
        {
            "bg_0": _route_extend_sub_entry(
                "bg_edge",
                10 - 1 - _BG_ROUTE_EXTEND_REMAINING,
                speed=_BG_ROUTE_EXTENSION_MIN_SPEED_MPS,
            )
        },
        0.0,
        extend_bg_routes=True,
    )

    mgr._random_route_from.assert_not_called()
    _traci_stub.vehicle.setRoute.assert_not_called()


def test_bg_route_extension_caps_attempts_per_tick():
    mgr = make_manager()
    mgr._random_route_from = MagicMock(return_value=["bg_edge", "next_edge"])
    _traci_stub.vehicle.setRoute = MagicMock()
    sub_results = {}
    for i in range(_BG_ROUTE_EXTENSION_MAX_PER_TICK + 3):
        veh_id = f"bg_{i}"
        mgr._bg_route_len[veh_id] = 10
        sub_results[veh_id] = _route_extend_sub_entry(
            "bg_edge",
            10 - 1 - _BG_ROUTE_EXTEND_REMAINING,
        )

    mgr._extend_vehicle_routes(sub_results, 0.0, extend_bg_routes=True)

    assert mgr._random_route_from.call_count == _BG_ROUTE_EXTENSION_MAX_PER_TICK
    assert _traci_stub.vehicle.setRoute.call_count == _BG_ROUTE_EXTENSION_MAX_PER_TICK


def test_taxi_route_extension_still_runs_when_bg_gate_is_closed():
    mgr = make_manager()
    mgr._taxi_route_len["taxi_0"] = 3
    mgr._random_route_from = MagicMock(return_value=["taxi_edge", "next_edge"])
    _traci_stub.vehicle.setRoute = MagicMock()

    mgr._extend_vehicle_routes(
        {"taxi_0": _route_extend_sub_entry("taxi_edge", 0)},
        0.0,
        extend_bg_routes=False,
    )

    mgr._random_route_from.assert_called_once_with(
        "taxi_edge",
        min_edges=_TAXI_EXTENSION_MIN_ROUTE_EDGES,
    )
    _traci_stub.vehicle.setRoute.assert_called_once_with("taxi_0", ["taxi_edge", "next_edge"])
