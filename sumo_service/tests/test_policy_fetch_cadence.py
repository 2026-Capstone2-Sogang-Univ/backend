from app.simulation import ExperimentConfig, SimulationManager


def test_fast_mode_fetches_prediction_on_sim_interval():
    mgr = SimulationManager()
    mgr.experiment_config = ExperimentConfig(
        fast=True,
        demand_source="predicted",
        policy_update_interval_sim_s=100.0,
        sim_duration=1000.0,
    )
    assert mgr._should_fetch_prediction(0.0) is True
    assert mgr._should_fetch_prediction(50.0) is False
    assert mgr._should_fetch_prediction(100.0) is True
