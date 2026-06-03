from app.experiment_metrics import aggregate_surge_band_metrics, raw_surge_bucket


def test_raw_surge_bucket_thresholds():
    assert raw_surge_bucket(1.4) == "raw_lt_1_5"
    assert raw_surge_bucket(2.0) == "raw_lt_2_5"
    assert raw_surge_bucket(3.0) == "raw_lt_3_5"
    assert raw_surge_bucket(4.0) == "raw_gte_3_5"


def test_aggregate_surge_band_metrics():
    decisions = [
        {"raw_surge": 1.2, "p_actual": 0.5, "accepted": True},
        {"raw_surge": 2.0, "p_actual": 0.65, "accepted": False},
        {"raw_surge": 3.0, "p_actual": 0.75, "accepted": True},
    ]
    m = aggregate_surge_band_metrics(decisions)
    assert m["band_raw_lt_1_5_target_p"] == 0.55
    assert m["band_raw_lt_2_5_p_error"] == 0.65 - 0.70
