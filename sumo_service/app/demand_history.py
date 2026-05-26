from __future__ import annotations

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

    def record_spawn(self, sim_datetime: datetime, h3_cell: str | None) -> None:
        if not h3_cell:
            return
        key = (floor_to_15min(sim_datetime), h3_cell)
        self._spawn_counts[key] = self._spawn_counts.get(key, 0) + 1

    def record_dropoff(self, sim_datetime: datetime, h3_cell: str | None) -> None:
        if not h3_cell:
            return
        key = (floor_to_15min(sim_datetime), h3_cell)
        self._dropoff_counts[key] = self._dropoff_counts.get(key, 0) + 1

    def records_for_prediction(self, request_time: datetime) -> list[dict[str, object]]:
        records: list[dict[str, object]] = []
        required_count = 0
        missing_count = 0

        for bucket in prediction_history_buckets(request_time):
            for h3_cell in self.model_h3_cells:
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
        missing_rate = 0.0
        if self._history_required_count:
            missing_rate = self._history_missing_count / self._history_required_count
        return {
            "history_required_count": self._history_required_count,
            "history_missing_count": self._history_missing_count,
            "history_missing_rate": missing_rate,
        }
