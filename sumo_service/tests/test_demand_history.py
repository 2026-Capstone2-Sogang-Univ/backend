import threading
from datetime import datetime

from app.demand_history import (
    DemandHistoryStore,
    floor_to_15min,
    prediction_history_buckets,
)


def test_floor_to_15min_zeroes_seconds_and_microseconds():
    assert floor_to_15min(datetime(2013, 7, 8, 8, 0, 59, 123)) == datetime(
        2013, 7, 8, 8, 0
    )
    assert floor_to_15min(datetime(2013, 7, 8, 8, 14, 59)) == datetime(
        2013, 7, 8, 8, 0
    )
    assert floor_to_15min(datetime(2013, 7, 8, 8, 15, 1)) == datetime(
        2013, 7, 8, 8, 15
    )
    assert floor_to_15min(datetime(2013, 7, 8, 8, 59, 59)) == datetime(
        2013, 7, 8, 8, 45
    )


def test_prediction_history_buckets_are_in_required_order():
    assert prediction_history_buckets(datetime(2013, 7, 8, 8, 17, 45)) == [
        datetime(2013, 7, 8, 8, 15),
        datetime(2013, 7, 8, 8, 0),
        datetime(2013, 7, 8, 7, 45),
        datetime(2013, 7, 8, 7, 30),
        datetime(2013, 7, 8, 7, 15),
        datetime(2013, 7, 7, 8, 15),
        datetime(2013, 7, 1, 8, 15),
    ]


def test_records_for_prediction_zero_fills_and_accumulates_diagnostics():
    store = DemandHistoryStore(model_h3_cells=["h3_a", "h3_b"])

    records = store.records_for_prediction(datetime(2013, 7, 8, 8, 17))

    assert len(records) == 14
    assert records[0] == {
        "h3": "h3_a",
        "time_bucket": "2013-07-08T08:15:00",
        "demand_count": 0,
        "dropoff_trip_count": 0,
    }
    assert records[-1] == {
        "h3": "h3_b",
        "time_bucket": "2013-07-01T08:15:00",
        "demand_count": 0,
        "dropoff_trip_count": 0,
    }
    assert store.diagnostics() == {
        "history_required_count": 14,
        "history_missing_count": 14,
        "history_missing_rate": 1.0,
    }

    store.records_for_prediction(datetime(2013, 7, 8, 8, 17))

    assert store.diagnostics() == {
        "history_required_count": 28,
        "history_missing_count": 28,
        "history_missing_rate": 1.0,
    }


def test_records_for_prediction_includes_recorded_spawn_and_dropoff_counts():
    store = DemandHistoryStore(model_h3_cells=["h3_a", "h3_b"])
    store.record_spawn(datetime(2013, 7, 8, 8, 16), "h3_a")
    store.record_spawn(datetime(2013, 7, 8, 8, 29), "h3_a")
    store.record_dropoff(datetime(2013, 7, 8, 8, 3), "h3_b")
    store.record_dropoff(datetime(2013, 7, 8, 8, 14), "h3_b")

    records = store.records_for_prediction(datetime(2013, 7, 8, 8, 17))

    assert records[0] == {
        "h3": "h3_a",
        "time_bucket": "2013-07-08T08:15:00",
        "demand_count": 2,
        "dropoff_trip_count": 0,
    }
    assert records[3] == {
        "h3": "h3_b",
        "time_bucket": "2013-07-08T08:00:00",
        "demand_count": 0,
        "dropoff_trip_count": 2,
    }
    assert store.diagnostics() == {
        "history_required_count": 14,
        "history_missing_count": 12,
        "history_missing_rate": 12 / 14,
    }


def test_records_for_prediction_uses_predicted_overrides_for_future_buckets():
    store = DemandHistoryStore(model_h3_cells=["h3_a"])
    store.record_spawn(datetime(2013, 7, 8, 8, 1), "h3_a")  # actual bucket 08:00

    # request_time 08:30 (roll-forward step), 08:30 bucket is a future bucket
    # filled from a prior prediction.
    overrides = {datetime(2013, 7, 8, 8, 30): {"h3_a": 7.0}}
    records = store.records_for_prediction(
        datetime(2013, 7, 8, 8, 30), predicted_overrides=overrides
    )

    by_bucket = {r["time_bucket"]: r for r in records}
    # overridden future bucket uses the predicted demand and zero dropoff
    assert by_bucket["2013-07-08T08:30:00"]["demand_count"] == 7.0
    assert by_bucket["2013-07-08T08:30:00"]["dropoff_trip_count"] == 0
    # actual past bucket still comes from recorded spawns
    assert by_bucket["2013-07-08T08:00:00"]["demand_count"] == 1

    # overridden (predicted) buckets are excluded from actual-history missing stats:
    # 7 history buckets total, 1 overridden → 6 counted, all missing except 08:00.
    diagnostics = store.diagnostics()
    assert diagnostics["history_required_count"] == 6
    assert diagnostics["history_missing_count"] == 5


def test_recording_history_is_thread_safe():
    store = DemandHistoryStore(model_h3_cells=["h3_a"])
    event_time = datetime(2013, 7, 8, 8, 16)

    def record_events() -> None:
        for _ in range(1000):
            store.record_spawn(event_time, "h3_a")
            store.record_dropoff(event_time, "h3_a")

    threads = [threading.Thread(target=record_events) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=1.0)

    records = store.records_for_prediction(datetime(2013, 7, 8, 8, 17))

    assert records[0] == {
        "h3": "h3_a",
        "time_bucket": "2013-07-08T08:15:00",
        "demand_count": 4000,
        "dropoff_trip_count": 4000,
    }


def test_record_spawn_and_dropoff_ignore_missing_h3_cells():
    store = DemandHistoryStore(model_h3_cells=[""])
    event_time = datetime(2013, 7, 8, 8, 16)
    store.record_spawn(event_time, None)
    store.record_spawn(event_time, "")
    store.record_dropoff(event_time, None)
    store.record_dropoff(event_time, "")

    records = store.records_for_prediction(datetime(2013, 7, 8, 8, 17))

    assert records[0] == {
        "h3": "",
        "time_bucket": "2013-07-08T08:15:00",
        "demand_count": 0,
        "dropoff_trip_count": 0,
    }
    assert store.diagnostics() == {
        "history_required_count": 7,
        "history_missing_count": 7,
        "history_missing_rate": 1.0,
    }
