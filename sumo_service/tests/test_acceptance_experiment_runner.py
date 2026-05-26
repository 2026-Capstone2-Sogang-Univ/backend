import csv

import pytest

from scripts.run_acceptance_experiment import CSV_COLUMNS, _aggregate, _append_csv


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
