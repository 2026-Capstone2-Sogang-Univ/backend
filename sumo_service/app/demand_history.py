from __future__ import annotations

import threading
from dataclasses import dataclass, field
from datetime import datetime, timedelta


def floor_to_15min(value: datetime) -> datetime:
    minute = (value.minute // 15) * 15
    return value.replace(minute=minute, second=0, microsecond=0)


def prediction_history_buckets(request_time: datetime) -> list[datetime]:
    bucket = floor_to_15min(request_time)
    return [
        bucket,
        bucket - timedelta(minutes=15),
        bucket - timedelta(minutes=30),
        bucket - timedelta(minutes=45),
        bucket - timedelta(minutes=60),
        bucket - timedelta(days=1),
        bucket - timedelta(days=7),
    ]


@dataclass
class DemandHistoryStore:
    model_h3_cells: list[str]
    _spawn_counts: dict[tuple[datetime, str], int] = field(default_factory=dict, init=False)
    _dropoff_counts: dict[tuple[datetime, str], int] = field(default_factory=dict, init=False)
    _history_required_count: int = field(default=0, init=False)
    _history_missing_count: int = field(default=0, init=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, init=False)

    def record_spawn(self, sim_datetime: datetime, h3_cell: str | None) -> None:
        if not h3_cell:
            return
        key = (floor_to_15min(sim_datetime), h3_cell)
        with self._lock:
            self._spawn_counts[key] = self._spawn_counts.get(key, 0) + 1

    def record_dropoff(self, sim_datetime: datetime, h3_cell: str | None) -> None:
        if not h3_cell:
            return
        key = (floor_to_15min(sim_datetime), h3_cell)
        with self._lock:
            self._dropoff_counts[key] = self._dropoff_counts.get(key, 0) + 1

    def records_for_prediction(
        self,
        request_time: datetime,
        *,
        predicted_overrides: dict[datetime, dict[str, float]] | None = None,
    ) -> list[dict[str, object]]:
        """Build the Module3 history payload for ``request_time``.

        ``predicted_overrides`` maps a 15-minute bucket to predicted demand by
        H3 cell. When a history bucket is present in the overrides it is treated
        as a future bucket filled from a prior prediction (roll-forward), so its
        demand is taken from the prediction and it is excluded from the
        actual-history missing-rate diagnostics.
        """
        records: list[dict[str, object]] = []
        required_count = 0
        missing_count = 0

        with self._lock:
            for bucket in prediction_history_buckets(request_time):
                override = predicted_overrides.get(bucket) if predicted_overrides else None
                for h3_cell in self.model_h3_cells:
                    if override is not None:
                        demand_count: float | int = override.get(h3_cell, 0)
                        dropoff_trip_count: float | int = 0
                    else:
                        key = (bucket, h3_cell)
                        demand_count = self._spawn_counts.get(key, 0)
                        dropoff_trip_count = self._dropoff_counts.get(key, 0)
                        required_count += 1
                        if demand_count == 0 and dropoff_trip_count == 0:
                            missing_count += 1
                    records.append(
                        {
                            "h3": h3_cell,
                            "time_bucket": bucket.isoformat(),
                            "demand_count": demand_count,
                            "dropoff_trip_count": dropoff_trip_count,
                        }
                    )

            self._history_required_count += required_count
            self._history_missing_count += missing_count
        return records

    def diagnostics(self) -> dict[str, float | int]:
        with self._lock:
            missing_rate = 0.0
            if self._history_required_count:
                missing_rate = self._history_missing_count / self._history_required_count
            return {
                "history_required_count": self._history_required_count,
                "history_missing_count": self._history_missing_count,
                "history_missing_rate": missing_rate,
            }
