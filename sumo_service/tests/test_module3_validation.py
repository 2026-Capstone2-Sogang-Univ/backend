from app.module3_validation import aggregate_module3_horizon_metrics, horizon_eval_snapshot


def test_horizon_eval_snapshot_mae_and_bias():
    event = horizon_eval_snapshot(
        issued_sim_time=0.0,
        target_sim_time=900.0,
        predicted={"a": 4.0, "b": 2.0},
        actual={"a": 3.0, "b": 2.0},
    )
    assert event["mae"] == 0.5
    assert event["bias"] == 0.5
    assert event["n_cells"] == 2


def test_aggregate_module3_horizon_metrics():
    events = [
        {"type": "module3_horizon_eval", "mae": 1.0, "bias": 0.5, "rmse": 1.2, "mape": 0.1},
        {"type": "module3_horizon_eval", "mae": 3.0, "bias": -1.0, "rmse": 3.5, "mape": 0.2},
    ]
    m = aggregate_module3_horizon_metrics(events)
    assert m["module3_horizon_eval_count"] == 2
    assert m["module3_horizon_mae_avg"] == 2.0
    assert m["module3_horizon_bias_avg"] == -0.25
