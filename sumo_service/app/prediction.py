from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
import os
from statistics import mean, quantiles
from typing import Literal

import httpx

from app.demand_history import DemandHistoryStore, floor_to_15min
from app.prediction_config import (
    prediction_retry_backoff_s,
    prediction_retry_max,
    prediction_timeout_s,
    prediction_warmup_enabled,
)
from app.weather import WeatherProvider

_logger = logging.getLogger(__name__)

class PredictionFallbackError(RuntimeError):
    pass


DemandMap = dict[str, float]
FallbackPolicy = Literal["error", "last_prediction", "actual"]
PredictionMode = Literal["sync", "async"]

_WARMUP_LOCK = threading.Lock()
_WARMUP_DONE_URLS: set[str] = set()


def prediction_base_url(prediction_url: str) -> str:
    """Service root used to wake cold Render instances (strip trailing /predict)."""
    url = prediction_url.rstrip("/")
    if url.endswith("/predict"):
        return url[: -len("/predict")] or url
    return url


def _should_retry_http(status_code: int) -> bool:
    return status_code == 429 or status_code >= 500


def _should_retry_exception(exc: BaseException) -> bool:
    if isinstance(exc, httpx.TimeoutException | httpx.ConnectError | httpx.NetworkError):
        return True
    if isinstance(exc, httpx.HTTPStatusError):
        return _should_retry_http(exc.response.status_code)
    return False


def warm_prediction_service(
    *,
    prediction_url: str,
    api_key: str | None = None,
    client: httpx.Client | None = None,
    timeout_s: float | None = None,
    retry_max: int | None = None,
) -> bool:
    """Wake a cold Module 3 host (e.g. Render) via GET on the service root with retries."""
    if not prediction_warmup_enabled():
        return True

    timeout_s = prediction_timeout_s() if timeout_s is None else timeout_s
    retry_max = prediction_retry_max() if retry_max is None else retry_max
    base = prediction_base_url(prediction_url)
    owns_client = client is None
    if client is None:
        client = httpx.Client()

    headers: dict[str, str] = {}
    if api_key:
        headers["X-API-Key"] = api_key.strip()

    attempts = retry_max + 1
    backoff = prediction_retry_backoff_s()
    try:
        for attempt in range(attempts):
            try:
                response = client.get(base, headers=headers or None, timeout=timeout_s)
                if response.status_code < 500:
                    _logger.info("prediction warmup ok: %s status=%s", base, response.status_code)
                    return True
                _logger.warning(
                    "prediction warmup retryable status: %s status=%s attempt=%s",
                    base,
                    response.status_code,
                    attempt + 1,
                )
            except Exception as exc:
                _logger.warning(
                    "prediction warmup failed: %s attempt=%s err=%s",
                    base,
                    attempt + 1,
                    exc,
                )
            if attempt < attempts - 1:
                time.sleep(backoff * (2**attempt))
    finally:
        if owns_client:
            client.close()
    return False


@dataclass
class PredictionDemandProvider:
    prediction_url: str
    model_h3_cells: list[str]
    history_store: DemandHistoryStore
    weather_provider: WeatherProvider
    prediction_horizon_min: int = 15
    fallback_policy: FallbackPolicy = "error"
    api_key: str | None = None
    client: httpx.Client | None = None
    timeout_s: float = field(default_factory=prediction_timeout_s)
    retry_max: int = field(default_factory=prediction_retry_max)
    _cache: dict[datetime, DemandMap] = field(default_factory=dict, init=False)
    _last_prediction: DemandMap | None = field(default=None, init=False)
    _inflight_targets: set[datetime] = field(default_factory=set, init=False)
    _background_threads: list[threading.Thread] = field(default_factory=list, init=False)
    _lock: threading.RLock = field(default_factory=threading.RLock, init=False)
    _owns_client: bool = field(default=False, init=False)
    _closed: bool = field(default=False, init=False)
    _prediction_request_count: int = field(default=0, init=False)
    _prediction_success_count: int = field(default=0, init=False)
    _prediction_failure_count: int = field(default=0, init=False)
    _prediction_retry_count: int = field(default=0, init=False)
    _prediction_fallback_count: int = field(default=0, init=False)
    _prediction_stale_use_count: int = field(default=0, init=False)
    _prediction_missing_h3_count: int = field(default=0, init=False)
    _prediction_expected_h3_count: int = field(default=0, init=False)
    _latencies_ms: list[float] = field(default_factory=list, init=False)

    def __post_init__(self) -> None:
        if self.api_key is None:
            self.api_key = os.getenv("PREDICTION_API_KEY")
        if self.api_key is not None:
            self.api_key = self.api_key.strip()
        if not self.api_key:
            raise ValueError("PREDICTION_API_KEY is required for Module3 prediction requests")
        self._owns_client = self.client is None
        self._ensure_warmup()

    def __enter__(self) -> PredictionDemandProvider:
        return self

    def __exit__(self, exc_type: object, exc_value: object, traceback: object) -> None:
        self.close()

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            client = self.client
            background_threads = list(self._background_threads)
            self._closed = True
        for thread in background_threads:
            if thread is not threading.current_thread():
                thread.join(timeout=self.timeout_s)
        if self._owns_client and client is not None:
            client.close()

    def demand_by_h3(
        self,
        sim_datetime: datetime,
        *,
        mode: PredictionMode = "sync",
        actual_demand: dict[str, float | int] | None = None,
    ) -> DemandMap:
        target_time = self._target_time(sim_datetime)
        cached = self._cached(target_time)
        if cached is not None:
            return cached

        if mode == "sync":
            try:
                return self._fetch_and_cache(sim_datetime, target_time)
            except PredictionFallbackError:
                return self._fallback(actual_demand)

        if mode != "async":
            raise ValueError(f"unsupported prediction mode: {mode}")

        fallback = self._fallback(actual_demand, stale_use=True)
        self._start_background_request(sim_datetime, target_time)
        return fallback

    def diagnostics(self) -> dict[str, float | int]:
        with self._lock:
            latencies = list(self._latencies_ms)
            expected_h3_count = self._prediction_expected_h3_count
            missing_h3_count = self._prediction_missing_h3_count
            request_count = self._prediction_request_count
            success_count = self._prediction_success_count
            failure_count = self._prediction_failure_count
            retry_count = self._prediction_retry_count
            fallback_count = self._prediction_fallback_count
            stale_use_count = self._prediction_stale_use_count

        missing_rate = 0.0
        if expected_h3_count:
            missing_rate = missing_h3_count / expected_h3_count

        return {
            "prediction_request_count": request_count,
            "prediction_success_count": success_count,
            "prediction_failure_count": failure_count,
            "prediction_retry_count": retry_count,
            "prediction_latency_ms_avg": mean(latencies) if latencies else 0.0,
            "prediction_latency_ms_p95": self._p95(latencies),
            "prediction_fallback_count": fallback_count,
            "prediction_stale_use_count": stale_use_count,
            "prediction_missing_h3_rate": missing_rate,
        }

    def _ensure_warmup(self) -> None:
        if not prediction_warmup_enabled():
            return
        with _WARMUP_LOCK:
            if self.prediction_url in _WARMUP_DONE_URLS:
                return
            warm_prediction_service(
                prediction_url=self.prediction_url,
                api_key=self.api_key,
                client=self._http_client() if self.client is not None else None,
                timeout_s=self.timeout_s,
                retry_max=self.retry_max,
            )
            _WARMUP_DONE_URLS.add(self.prediction_url)

    def _target_time(self, sim_datetime: datetime) -> datetime:
        return floor_to_15min(sim_datetime + timedelta(minutes=self.prediction_horizon_min))

    def _cached(self, target_time: datetime) -> DemandMap | None:
        with self._lock:
            cached = self._cache.get(target_time)
            if cached is None:
                return None
            return dict(cached)

    def _fetch_and_cache(self, sim_datetime: datetime, target_time: datetime) -> DemandMap:
        started_at = time.perf_counter()
        with self._lock:
            if self._closed:
                raise PredictionFallbackError("prediction provider is closed")
        self._record_request()
        payload = self._request_payload(sim_datetime, target_time)
        headers = {"X-API-Key": self.api_key}
        attempts = self.retry_max + 1
        backoff = prediction_retry_backoff_s()
        last_exc: BaseException | None = None

        for attempt in range(attempts):
            try:
                response = self._http_client().post(
                    self.prediction_url,
                    json=payload,
                    headers=headers,
                    timeout=self.timeout_s,
                )
                response.raise_for_status()
                demand = self._parse_predictions(response.json(), target_time)
                latency_ms = (time.perf_counter() - started_at) * 1000
                self._record_success(target_time, demand, latency_ms)
                return dict(demand)
            except Exception as exc:
                last_exc = exc
                if attempt < attempts - 1 and _should_retry_exception(exc):
                    with self._lock:
                        self._prediction_retry_count += 1
                    sleep_s = backoff * (2**attempt)
                    _logger.warning(
                        "prediction request retry in %.1fs (attempt %s/%s): %s",
                        sleep_s,
                        attempt + 2,
                        attempts,
                        exc,
                    )
                    time.sleep(sleep_s)
                    continue
                latency_ms = (time.perf_counter() - started_at) * 1000
                self._record_failure(latency_ms)
                raise PredictionFallbackError("prediction request failed") from exc

        latency_ms = (time.perf_counter() - started_at) * 1000
        self._record_failure(latency_ms)
        raise PredictionFallbackError("prediction request failed") from last_exc

    def _request_payload(self, sim_datetime: datetime, target_time: datetime) -> dict[str, object]:
        del target_time
        request_time = floor_to_15min(sim_datetime)
        return {
            "timestamp": request_time.isoformat(),
            "weather": self.weather_provider.features_at(request_time),
            "records": self.history_store.records_for_prediction(request_time),
        }

    def _parse_predictions(self, payload: object, target_time: datetime) -> DemandMap:
        if not isinstance(payload, dict) or not isinstance(payload.get("predictions"), list):
            raise ValueError("prediction response must include a predictions list")
        response_target_time = datetime.fromisoformat(str(payload["target_time"]))
        if response_target_time != target_time:
            raise ValueError(
                f"prediction response target_time {response_target_time.isoformat()} "
                f"does not match expected {target_time.isoformat()}"
            )

        model_cells = set(self.model_h3_cells)
        demand: DemandMap = {h3_cell: 0.0 for h3_cell in self.model_h3_cells}
        seen: set[str] = set()

        for item in payload["predictions"]:
            if not isinstance(item, dict):
                raise ValueError("prediction item must be an object")
            h3_cell = str(item["h3"])
            predicted_demand = float(item["predicted_demand_count"])
            if h3_cell in model_cells:
                demand[h3_cell] = predicted_demand
                seen.add(h3_cell)

        with self._lock:
            self._prediction_expected_h3_count += len(self.model_h3_cells)
            self._prediction_missing_h3_count += len(self.model_h3_cells) - len(seen)

        return demand

    def _fallback(
        self,
        actual_demand: dict[str, float | int] | None,
        *,
        stale_use: bool = False,
    ) -> DemandMap:
        # Long-running experiments may choose "last_prediction" to keep a batch
        # alive during transient Module3 outages. The default remains fail-fast
        # so validation runs do not silently mix predicted and fallback demand.
        with self._lock:
            last_prediction = None if self._last_prediction is None else dict(self._last_prediction)

        if self.fallback_policy == "last_prediction" and last_prediction is not None:
            self._record_fallback(stale_use)
            return last_prediction

        if self.fallback_policy in {"last_prediction", "actual"} and actual_demand is not None:
            self._record_fallback(stale_use)
            return {h3_cell: float(actual_demand.get(h3_cell, 0.0)) for h3_cell in self.model_h3_cells}

        raise PredictionFallbackError("prediction unavailable and no fallback is configured")

    def _start_background_request(self, sim_datetime: datetime, target_time: datetime) -> None:
        with self._lock:
            if target_time in self._inflight_targets:
                return
            self._inflight_targets.add(target_time)

        thread = threading.Thread(
            target=self._background_fetch,
            args=(sim_datetime, target_time),
            daemon=True,
        )
        with self._lock:
            if self._closed:
                self._inflight_targets.discard(target_time)
                raise RuntimeError("prediction provider is closed")
            self._background_threads.append(thread)
            thread.start()

    def _background_fetch(self, sim_datetime: datetime, target_time: datetime) -> None:
        try:
            self._fetch_and_cache(sim_datetime, target_time)
        except PredictionFallbackError:
            pass
        finally:
            current_thread = threading.current_thread()
            with self._lock:
                self._inflight_targets.discard(target_time)
                self._background_threads = [
                    thread for thread in self._background_threads if thread is not current_thread
                ]

    def _http_client(self) -> httpx.Client:
        with self._lock:
            if self._closed:
                raise RuntimeError("prediction provider is closed")
            if self.client is None:
                self.client = httpx.Client()
            return self.client

    def _record_request(self) -> None:
        with self._lock:
            self._prediction_request_count += 1

    def _record_success(self, target_time: datetime, demand: DemandMap, latency_ms: float) -> None:
        with self._lock:
            self._prediction_success_count += 1
            self._latencies_ms.append(latency_ms)
            self._cache[target_time] = dict(demand)
            self._last_prediction = dict(demand)

    def _record_failure(self, latency_ms: float) -> None:
        with self._lock:
            self._prediction_failure_count += 1
            self._latencies_ms.append(latency_ms)

    def _record_fallback(self, stale_use: bool) -> None:
        with self._lock:
            self._prediction_fallback_count += 1
            if stale_use:
                self._prediction_stale_use_count += 1

    @staticmethod
    def _p95(values: list[float]) -> float:
        if not values:
            return 0.0
        if len(values) == 1:
            return values[0]
        return quantiles(values, n=20, method="inclusive")[18]
