import pytest

from app.rebalance import (
    acceptance_bonus_from_raw_surge,
    aggregate_rebalance_metrics,
    select_top_surge_deficit_cells,
)


def test_select_top_surge_deficit_cells_prioritizes_high_surge_and_deficit():
    raw = {"a": 3.0, "b": 2.0, "c": 1.0}
    supply = {"a": 1, "b": 5, "c": 2}
    demand = {"a": 5, "b": 5, "c": 2}
    cells = select_top_surge_deficit_cells(
        raw,
        supply,
        demand,
        top_k=2,
        min_raw_surge=1.5,
    )
    assert cells[0] == "a"
    assert "b" in cells


def test_acceptance_bonus_is_zero_below_activation_and_capped():
    assert acceptance_bonus_from_raw_surge(1.1, coef=0.1, activation=1.2) == 0.0
    assert acceptance_bonus_from_raw_surge(3.0, coef=0.1, activation=1.2, cap=0.15) == pytest.approx(0.15)


def test_aggregate_rebalance_metrics_computes_high_surge_deficit():
    surge_rows = [
        {"raw_surge": 3.0, "supply": 1, "actual_demand": 4},
        {"raw_surge": 1.0, "supply": 2, "actual_demand": 1},
        {"raw_surge": 2.8, "supply": 0, "actual_demand": 2},
    ]
    metrics = aggregate_rebalance_metrics(
        surge_rows,
        [{"type": "rebalance_redirect"}, {"type": "rebalance_redirect"}],
        high_surge_threshold=2.5,
        min_samples_for_percentile=2,
    )
    assert metrics["avg_high_surge_deficit"] == pytest.approx((3 + 2) / 2)
    assert metrics["rebalance_redirect_count"] == 2
    assert metrics["high_surge_mean_raw_surge"] == pytest.approx((3.0 + 2.8) / 2)


def test_rebalance_policy_still_uses_inverse_fare_at_dispatch():
    from unittest.mock import MagicMock

    from app.simulation import ExperimentConfig, SimulationManager

    manager = SimulationManager(
        experiment_config=ExperimentConfig(policy_mode="rebalance", alpha_sensitivity=1.5)
    )
    manager._raw_surge_by_h3 = {"pickup_h3": 2.0}
    manager._surge_by_h3 = {"pickup_h3": 1.5}
    manager._pricing_policy = {"surge_min": 1.2, "surge_max": 4.9}

    candidate = MagicMock()
    candidate.expected_fare = 2500
    candidate.h3_pickup = "pickup_h3"

    manager._candidate_driver_average_features = MagicMock(
        return_value=(0.1, 0.2, 5.0, 3)
    )

    pricing = manager._dispatch_pricing(
        candidate=candidate,
        sim_time=100.0,
        sub_results={},
        current_veh_id="taxi_0",
        current_route=MagicMock(),
    )

    assert pricing["required_fare_usd"] is not None
    assert pricing["final_surge"] >= 1.0
