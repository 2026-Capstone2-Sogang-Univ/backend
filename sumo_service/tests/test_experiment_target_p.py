"""ExperimentConfig P* overrides reach dispatch pricing targets."""

from __future__ import annotations

import pytest

from app.simulation import (
    DEFAULT_TARGET_MATCHING_RATES,
    ExperimentConfig,
    SimulationManager,
)


def test_target_p_overrides_high_surge_bucket() -> None:
    mgr = SimulationManager.fresh_experiment(
        ExperimentConfig(
            target_p=0.90,
            target_p_bucket="raw_gte_3_5",
            fast=True,
            demand_source="actual",
        )
    )
    assert mgr._target_matching_rates["raw_gte_3_5"] == pytest.approx(0.90)
    assert mgr._target_matching_rates["raw_lt_1_5"] == pytest.approx(
        DEFAULT_TARGET_MATCHING_RATES["raw_lt_1_5"]
    )
    assert mgr._runtime_kpi.buckets["raw_gte_3_5"].target_rate == pytest.approx(0.90)


def test_target_matching_rate_overrides_multi_bucket() -> None:
    mgr = SimulationManager.fresh_experiment(
        ExperimentConfig(
            target_matching_rate_overrides=(
                ("raw_lt_3_5", 0.75),
                ("raw_gte_3_5", 0.88),
            ),
            fast=True,
            demand_source="actual",
        )
    )
    assert mgr._target_matching_rates["raw_lt_3_5"] == pytest.approx(0.75)
    assert mgr._target_matching_rates["raw_gte_3_5"] == pytest.approx(0.88)


def test_run_experiment_preserves_target_p_after_reset() -> None:
    mgr = SimulationManager.fresh_experiment(
        ExperimentConfig(
            target_p=0.92,
            target_p_bucket="raw_gte_3_5",
            sim_duration=1.0,
            fast=True,
            demand_source="actual",
            passenger_lambda=1,
            n_taxis=1,
        )
    )
    # Do not run full SUMO — only verify reset path keeps overridden P*.
    mgr._apply_experiment_overrides()
    mgr._reset_run_state()
    assert mgr._target_matching_rates["raw_gte_3_5"] == pytest.approx(0.92)
    assert mgr._runtime_kpi.buckets["raw_gte_3_5"].target_rate == pytest.approx(0.92)


def test_invalid_target_p_rejected() -> None:
    with pytest.raises(ValueError, match="target_p must be"):
        SimulationManager.fresh_experiment(
            ExperimentConfig(target_p=1.5, fast=True, demand_source="actual")
        )
