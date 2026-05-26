import csv

import pytest

from scripts.run_acceptance_experiment import CSV_COLUMNS, _aggregate, _append_csv, _run_one


def test_aggregate_includes_prediction_history_and_demand_diagnostics():
    events = [
        {"type": "passenger_spawned", "passenger_id": "p1"},
        {
            "type": "dispatch_decision",
            "passenger_id": "p1",
            "accepted": True,
            "p_actual": 0.8,
            "incentive_usd": 1.0,
        },
        {
            "type": "diagnostics",
            "prediction_request_count": 1,
            "history_required_count": 14,
        },
        {
            "type": "surge_diagnostic",
            "actual_demand": 2.0,
            "demand_for_surge": 3.0,
            "surge": 1.2,
        },
    ]

    metrics = _aggregate(events, sim_duration=3600.0)

    assert metrics["prediction_request_count"] == 1
    assert metrics["history_required_count"] == 14
    assert metrics["avg_actual_demand_for_surge"] == 2.0
    assert metrics["avg_predicted_demand_for_surge"] == 3.0
    assert metrics["avg_demand_bias"] == 1.0
    assert metrics["avg_abs_demand_error"] == 1.0
    assert metrics["avg_surge"] == 1.2


def test_append_csv_writes_new_columns_without_keyerror_for_sparse_rows(tmp_path):
    csv_path = tmp_path / "results.csv"
    row = {
        "status": "invalid",
        "reason": "bad params",
        "params": {
            "target_p": 1.0,
            "elasticity": 0.5,
            "beta_f": 0.006,
            "seed": 42,
        },
        "metrics": None,
    }

    _append_csv(csv_path, [row])

    with csv_path.open(newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))

    assert rows[0]["status"] == "invalid"
    assert "prediction_request_count" in rows[0]
    assert "demand_source" in rows[0]
    assert rows[0]["prediction_request_count"] == ""
    assert list(rows[0]) == CSV_COLUMNS


def test_append_csv_rejects_existing_file_with_old_header(tmp_path):
    csv_path = tmp_path / "results.csv"
    csv_path.write_text("status,reason,target_p\nok,,0.8\n", encoding="utf-8")

    with pytest.raises(ValueError, match="CSV header mismatch"):
        _append_csv(csv_path, [])


def test_run_one_passes_prediction_mode_params_to_experiment_config(monkeypatch):
    captured = {}

    class FakeSimulationManager:
        @classmethod
        def fresh_experiment(cls, config):
            captured["config"] = config
            return cls()

        def run_experiment(self):
            return []

    monkeypatch.setattr(
        "scripts.run_acceptance_experiment.SimulationManager",
        FakeSimulationManager,
    )

    row = _run_one(
        0.8,
        0.5,
        0.006,
        7,
        60.0,
        1.0,
        demand_source="predicted",
        prediction_mode="async",
        prediction_url="https://example.test/predict",
        prediction_horizon_min=30,
        passenger_elasticity=0.25,
        alpha_sensitivity=1.5,
        weather_source="static",
    )

    config = captured["config"]
    assert config.demand_source == "predicted"
    assert config.prediction_mode == "async"
    assert config.prediction_url == "https://example.test/predict"
    assert config.prediction_horizon_min == 30
    assert config.passenger_elasticity == 0.25
    assert config.alpha_sensitivity == 1.5
    assert config.weather_source == "static"
    assert row["params"]["demand_source"] == "predicted"
    assert row["params"]["prediction_mode"] == "async"
    assert row["params"]["prediction_url"] == "https://example.test/predict"
    assert row["params"]["prediction_horizon_min"] == 30
    assert row["params"]["passenger_elasticity"] == 0.25
    assert row["params"]["alpha_sensitivity"] == 1.5
    assert row["params"]["weather_source"] == "static"


def test_run_one_uses_sync_when_predicted_demand_has_no_prediction_mode(monkeypatch):
    captured = {}

    class FakeSimulationManager:
        @classmethod
        def fresh_experiment(cls, config):
            captured["config"] = config
            return cls()

        def run_experiment(self):
            return []

    monkeypatch.setattr(
        "scripts.run_acceptance_experiment.SimulationManager",
        FakeSimulationManager,
    )

    row = _run_one(
        0.8,
        0.5,
        0.006,
        7,
        60.0,
        1.0,
        demand_source="predicted",
        prediction_mode="none",
    )

    assert captured["config"].prediction_mode == "sync"
    assert row["params"]["prediction_mode"] == "sync"
