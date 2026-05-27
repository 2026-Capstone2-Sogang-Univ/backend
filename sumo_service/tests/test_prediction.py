import threading
import time
from contextlib import contextmanager
from datetime import datetime
from typing import Iterator

import httpx
import pytest

from app.demand_history import DemandHistoryStore
from app.prediction import PredictionDemandProvider, PredictionFallbackError
from app.weather import StaticWeatherProvider


def test_sync_request_payload_includes_history_and_weather():
    requests: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request.read())
        return httpx.Response(
            200,
            json={
                "predictions": [
                    {
                        "target_time": "2013-07-08T08:30:00",
                        "h3": "h3_a",
                        "predicted_demand": 3,
                    }
                ]
            },
        )

    store = DemandHistoryStore(model_h3_cells=["h3_a"])
    store.record_spawn(datetime(2013, 7, 8, 8, 16), "h3_a")
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        provider = PredictionDemandProvider(
            prediction_url="https://prediction.test/predict",
            model_h3_cells=["h3_a"],
            history_store=store,
            weather_provider=StaticWeatherProvider(temp=28.0, cat="rain"),
            client=client,
        )
        demand = provider.demand_by_h3(datetime(2013, 7, 8, 8, 17))
        provider.close()

    assert demand == {"h3_a": 3.0}
    assert len(requests) == 1
    payload = httpx.Response(200, content=requests[0]).json()
    assert payload["request_time"] == "2013-07-08T08:17:00"
    assert payload["target_time"] == "2013-07-08T08:30:00"
    assert payload["horizon_min"] == 15
    assert payload["history"][0] == {
        "h3": "h3_a",
        "time_bucket": "2013-07-08T08:15:00",
        "demand_count": 1,
        "dropoff_trip_count": 0,
    }
    assert payload["weather"]["feat_weather_temp"] == 28.0
    assert payload["weather"]["feat_weather_cat"] == "rain"


def test_cache_avoids_duplicate_prediction_request_for_target_time():
    request_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal request_count
        request_count += 1
        return httpx.Response(
            200,
            json={
                "predictions": [
                    {
                        "target_time": "2013-07-08T08:30:00",
                        "h3": "h3_a",
                        "predicted_demand": 4,
                    }
                ]
            },
        )

    with _provider(["h3_a"], handler) as provider:
        assert provider.demand_by_h3(datetime(2013, 7, 8, 8, 17)) == {"h3_a": 4.0}
        assert provider.demand_by_h3(datetime(2013, 7, 8, 8, 22)) == {"h3_a": 4.0}
        assert request_count == 1
        assert provider.diagnostics()["prediction_request_count"] == 1


def test_diagnostics_do_not_wait_for_slow_prediction_http_request():
    request_started = threading.Event()
    release_response = threading.Event()
    result: list[dict[str, float]] = []
    diagnostics_result: list[dict[str, float | int]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        request_started.set()
        assert release_response.wait(timeout=1.0)
        return httpx.Response(
            200,
            json={
                "predictions": [
                    {
                        "target_time": "2013-07-08T08:30:00",
                        "h3": "h3_a",
                        "predicted_demand": 9,
                    }
                ]
            },
        )

    with _provider(["h3_a"], handler) as provider:
        request_thread = threading.Thread(
            target=lambda: result.append(provider.demand_by_h3(datetime(2013, 7, 8, 8, 17)))
        )
        request_thread.start()
        assert request_started.wait(timeout=1.0)

        diagnostics_done = threading.Event()
        diagnostics_thread = threading.Thread(
            target=lambda: (
                diagnostics_result.append(provider.diagnostics()),
                diagnostics_done.set(),
            )
        )
        diagnostics_thread.start()

        try:
            assert diagnostics_done.wait(timeout=0.2)
            assert diagnostics_result[0]["prediction_request_count"] == 1
            assert diagnostics_result[0]["prediction_success_count"] == 0
        finally:
            release_response.set()
            request_thread.join(timeout=1.0)
            diagnostics_thread.join(timeout=1.0)

        assert result == [{"h3_a": 9.0}]


def test_error_fallback_policy_raises_prediction_fallback_error():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"error": "unavailable"})

    with _provider(["h3_a"], handler) as provider:
        with pytest.raises(PredictionFallbackError):
            provider.demand_by_h3(datetime(2013, 7, 8, 8, 17))

        diagnostics = provider.diagnostics()
        assert diagnostics["prediction_failure_count"] == 1
        assert diagnostics["prediction_fallback_count"] == 0


def test_missing_h3_rate_zero_fills_absent_model_cells():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "predictions": [
                    {
                        "target_time": "2013-07-08T08:30:00",
                        "h3": "h3_a",
                        "predicted_demand": 2.5,
                    },
                    {
                        "target_time": "2013-07-08T08:30:00",
                        "h3": "unknown_h3",
                        "predicted_demand": 99,
                    },
                ]
            },
        )

    with _provider(["h3_a", "h3_b"], handler) as provider:
        assert provider.demand_by_h3(datetime(2013, 7, 8, 8, 17)) == {
            "h3_a": 2.5,
            "h3_b": 0.0,
        }
        assert provider.diagnostics()["prediction_missing_h3_rate"] == 0.5


def test_async_fallback_returns_actual_demand_and_counts_fallback_and_stale_use():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "predictions": [
                    {
                        "target_time": "2013-07-08T08:30:00",
                        "h3": "h3_a",
                        "predicted_demand": 8,
                    }
                ]
            },
        )

    with _provider(["h3_a", "h3_b"], handler, fallback_policy="actual") as provider:
        demand = provider.demand_by_h3(
            datetime(2013, 7, 8, 8, 17),
            mode="async",
            actual_demand={"h3_a": 5, "h3_b": 1},
        )

        assert demand == {"h3_a": 5.0, "h3_b": 1.0}
        diagnostics = provider.diagnostics()
        assert diagnostics["prediction_fallback_count"] == 1
        assert diagnostics["prediction_stale_use_count"] == 1


def test_async_mode_fails_before_starting_background_request_without_fallback():
    request_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal request_count
        request_count += 1
        return httpx.Response(200, json={"predictions": []})

    with _provider(["h3_a"], handler) as provider:
        with pytest.raises(PredictionFallbackError):
            provider.demand_by_h3(datetime(2013, 7, 8, 8, 17), mode="async")

        time.sleep(0.01)
        assert request_count == 0
        assert provider.diagnostics()["prediction_request_count"] == 0


def test_async_background_request_eventually_caches_prediction_and_success():
    completed = threading.Event()

    def handler(request: httpx.Request) -> httpx.Response:
        completed.set()
        return httpx.Response(
            200,
            json={
                "predictions": [
                    {
                        "target_time": "2013-07-08T08:30:00",
                        "h3": "h3_a",
                        "predicted_demand": 8,
                    }
                ]
            },
        )

    with _provider(["h3_a"], handler, fallback_policy="actual") as provider:
        assert provider.demand_by_h3(
            datetime(2013, 7, 8, 8, 17),
            mode="async",
            actual_demand={"h3_a": 1},
        ) == {"h3_a": 1.0}

        assert completed.wait(timeout=1.0)
        _wait_for_success(provider)
        assert provider.demand_by_h3(datetime(2013, 7, 8, 8, 17)) == {"h3_a": 8.0}
        diagnostics = provider.diagnostics()
        assert diagnostics["prediction_success_count"] == 1
        assert diagnostics["prediction_request_count"] == 1


def test_prediction_latency_p95_uses_inclusive_quantile():
    assert PredictionDemandProvider._p95([25.0]) == 25.0
    assert PredictionDemandProvider._p95([10.0, 30.0]) == pytest.approx(29.0)


@contextmanager
def _provider(
    model_h3_cells: list[str],
    handler,
    *,
    fallback_policy: str = "error",
) -> Iterator[PredictionDemandProvider]:
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        provider = PredictionDemandProvider(
            prediction_url="https://prediction.test/predict",
            model_h3_cells=model_h3_cells,
            history_store=DemandHistoryStore(model_h3_cells=model_h3_cells),
            weather_provider=StaticWeatherProvider(),
            fallback_policy=fallback_policy,
            client=client,
        )
        try:
            yield provider
        finally:
            provider.close()


def _wait_for_success(provider: PredictionDemandProvider) -> None:
    deadline = time.monotonic() + 1.0
    while time.monotonic() < deadline:
        if provider.diagnostics()["prediction_success_count"] == 1:
            return
        time.sleep(0.01)
    raise AssertionError("prediction request did not complete")


def test_async_mode_uses_actual_fallback_while_background_request_runs():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"predictions": []})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    provider = PredictionDemandProvider(
        prediction_url="https://module3-ml.onrender.com/predict",
        model_h3_cells=["h3_a", "h3_b"],
        history_store=DemandHistoryStore(model_h3_cells=["h3_a", "h3_b"]),
        weather_provider=StaticWeatherProvider(),
        client=client,
        fallback_policy="last_prediction",
    )

    demand = provider.demand_by_h3(
        datetime(2013, 7, 8, 8, 0),
        mode="async",
        actual_demand={"h3_a": 1},
    )

    assert demand == {"h3_a": 1.0, "h3_b": 0.0}
    assert provider.diagnostics()["prediction_fallback_count"] == 1
    assert provider.diagnostics()["prediction_stale_use_count"] == 1
    provider.close()

