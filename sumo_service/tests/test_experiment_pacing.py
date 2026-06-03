from app.experiment_pacing import bench_step_length, resolve_experiment_pacing
from app.simulation import ExperimentConfig


def test_resolve_experiment_pacing_fast_mode(monkeypatch):
    monkeypatch.delenv("BENCH_STEP_LENGTH", raising=False)
    step, sleep = resolve_experiment_pacing(ExperimentConfig(fast=True, demand_source="actual"))
    assert step == bench_step_length()
    assert sleep == 0.0

    monkeypatch.setenv("BENCH_STEP_LENGTH", "1")
    step_one, _ = resolve_experiment_pacing(ExperimentConfig(fast=True, demand_source="actual"))
    assert step_one == 1.0


def test_resolve_experiment_pacing_lab_aligned():
    step, sleep = resolve_experiment_pacing(
        ExperimentConfig(fast=False, simulation_speed=60.0, demand_source="predicted")
    )
    assert step == 1.0
    assert sleep == 1.0 / 60.0


def test_predicted_policy_interval_default_15_min():
    config = ExperimentConfig(demand_source="predicted")
    assert config.policy_update_interval_real_s == 900.0
    assert config.policy_update_interval_sim_s == 900.0


def test_experiment_fast_enabled_default(monkeypatch):
    from app.experiment_pacing import experiment_fast_enabled

    monkeypatch.delenv("EXPERIMENT_FAST", raising=False)
    assert experiment_fast_enabled() is True
    monkeypatch.setenv("EXPERIMENT_FAST", "0")
    assert experiment_fast_enabled() is False
