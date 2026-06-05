from __future__ import annotations

import argparse
import csv
import itertools
import json
import math
import sys
from pathlib import Path
from statistics import mean, median, quantiles

# 빈차 대기 분포는 long-tail이라 표본이 너무 적으면 p95가 사실상 max에 가까워 무의미.
MIN_SAMPLES_FOR_PERCENTILE = 20

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.simulation import (
    ExperimentConfig,
    N_TAXIS,
    PARQUET_REPLAY_STAT_KEYS,
    SIM_DURATION,
    STEP_LENGTH,
    SimulationManager,
)
from app.pricing import RAW_SURGE_BUCKETS, raw_surge_bucket

# CSV는 sweep을 여러 번 이어 붙일 수 있도록 JSON 결과의 params/metrics를 평탄화한 고정 컬럼을 쓴다.
CSV_COLUMNS = [
    "status",
    "reason",
    "elasticity",
    "beta_f",
    "seed",
    "demand_source",
    "prediction_mode",
    "prediction_url",
    "prediction_horizon_min",
    "passenger_elasticity",
    "alpha_sensitivity",
    "weather_source",
    "spawned_passengers",
    "unique_matched_passengers",
    "matching_success_rate",
    "accepted_dispatch_count",
    "acceptances_per_driver_hour",
    "driver_revenue_per_hour_usd",
    "avg_empty_wait_time_s",
    "p50_empty_wait_time_s",
    "p95_empty_wait_time_s",
    "avg_actual_acceptance_probability",
    "avg_target_matching_rate",
    "avg_matching_rate_error",
    "avg_abs_matching_rate_error",
    "avg_required_fare_usd",
    "avg_final_surge",
    "avg_final_fare_usd",
    "prediction_request_count",
    "prediction_success_count",
    "prediction_failure_count",
    "prediction_latency_ms_avg",
    "prediction_latency_ms_p95",
    "prediction_fallback_count",
    "prediction_stale_use_count",
    "prediction_missing_h3_rate",
    "history_required_count",
    "history_missing_count",
    "history_missing_rate",
    "avg_actual_demand_for_surge",
    "avg_predicted_demand_for_surge",
    "avg_demand_bias",
    "avg_abs_demand_error",
    "avg_surge",
    "raw_spawn_candidate_count",
    "elasticity_removed_count",
    "actual_spawned_passengers",
]
PARAM_COLUMN_COUNT = 12


def _parse_float_list(value: str | None, fallback: float | None) -> list[float]:
    if value:
        return [float(v.strip()) for v in value.split(",") if v.strip()]
    if fallback is None:
        raise ValueError("missing required value")
    return [float(fallback)]


def _parse_optional_float_list(value: str | None, fallback: float | None) -> list[float | None]:
    if value:
        return [float(v.strip()) for v in value.split(",") if v.strip()]
    if fallback is None:
        return [None]
    return [float(fallback)]


def _aggregate(events: list[dict], sim_duration: float) -> dict:
    # SimulationManager는 실험 중 원시 이벤트만 남기고, KPI 정의는 runner 한 곳에서 집계한다.
    # 이렇게 두면 SUMO loop는 가볍게 유지하고 지표 정의 변경도 CLI 쪽에서 좁게 처리할 수 있다.
    spawned = [e for e in events if e["type"] == "passenger_spawned"]
    decisions = [e for e in events if e["type"] == "dispatch_decision"]
    accepted = [e for e in decisions if e["accepted"]]
    completed_trips = [
        e for e in events
        if e["type"] == "trip_completed" and e.get("completion") != "forced_at_end"
    ]
    diagnostics = _latest_diagnostics(events)
    surge_diagnostics = [e for e in events if e["type"] == "surge_diagnostic"]
    actual_values = [e["actual_demand"] for e in surge_diagnostics]
    predicted_values = [e["demand_for_surge"] for e in surge_diagnostics]
    demand_errors = [p - a for a, p in zip(actual_values, predicted_values)]
    surge_values = [e["surge"] for e in surge_diagnostics]
    target_matching_rates = [
        e["target_matching_rate"] for e in decisions
        if e.get("target_matching_rate") is not None
    ]
    matching_rate_errors = [
        e["matching_rate_error"] for e in decisions
        if e.get("matching_rate_error") is not None
    ]
    final_surge_values = [
        e["final_surge"] for e in decisions if e.get("final_surge") is not None
    ]
    final_fare_values = [
        e["final_fare_usd"] for e in decisions if e.get("final_fare_usd") is not None
    ]
    elasticity_events = [e for e in events if e["type"] == "passenger_elasticity"]
    parquet_replay = _latest_parquet_replay(events)
    matching_by_raw_bucket = _aggregate_by_raw_bucket(decisions)
    cells_by_raw_bucket = _aggregate_cells_by_raw_bucket(surge_diagnostics, decisions)

    spawned_count = len(spawned)
    matched_passengers = {e["passenger_id"] for e in accepted}
    matching_success_rate = (
        len(matched_passengers) / spawned_count if spawned_count else 0.0
    )
    experiment_hours = sim_duration / 3600.0
    acceptances_per_driver_hour = (
        len(accepted) / (N_TAXIS * experiment_hours)
        if N_TAXIS > 0 and experiment_hours > 0
        else 0.0
    )
    completed_fare_total_usd = sum(e.get("fare_usd", 0.0) for e in completed_trips)
    driver_revenue_per_driver_hour_usd = (
        completed_fare_total_usd / (N_TAXIS * experiment_hours)
        if N_TAXIS > 0 and experiment_hours > 0
        else 0.0
    )

    empty_waits = [
        e["empty_wait_time_s"] for e in accepted if e.get("empty_wait_time_s") is not None
    ]
    p95_empty_wait = None
    if len(empty_waits) >= MIN_SAMPLES_FOR_PERCENTILE:
        p95_empty_wait = quantiles(empty_waits, n=20, method="inclusive")[18]

    return {
        "spawned_passengers": spawned_count,
        "unique_matched_passengers": len(matched_passengers),
        "matching_success_rate": matching_success_rate,
        "accepted_dispatch_count": len(accepted),
        "acceptances_per_driver_hour": acceptances_per_driver_hour,
        "driver_revenue_per_hour_usd": driver_revenue_per_driver_hour_usd,
        "avg_empty_wait_time_s": mean(empty_waits) if empty_waits else None,
        "p50_empty_wait_time_s": median(empty_waits) if empty_waits else None,
        "p95_empty_wait_time_s": p95_empty_wait,
        "avg_actual_acceptance_probability": (
            mean(e.get("p_actual", 0.0) for e in decisions) if decisions else 0.0
        ),
        "avg_target_matching_rate": (
            mean(target_matching_rates) if target_matching_rates else 0.0
        ),
        "avg_matching_rate_error": (
            mean(matching_rate_errors) if matching_rate_errors else 0.0
        ),
        "avg_abs_matching_rate_error": (
            mean(abs(v) for v in matching_rate_errors) if matching_rate_errors else 0.0
        ),
        "avg_required_fare_usd": _mean_present(e.get("required_fare_usd") for e in decisions),
        "avg_final_surge": mean(final_surge_values) if final_surge_values else 0.0,
        "avg_final_fare_usd": mean(final_fare_values) if final_fare_values else 0.0,
        **diagnostics,
        "avg_actual_demand_for_surge": mean(actual_values) if actual_values else 0.0,
        "avg_predicted_demand_for_surge": mean(predicted_values) if predicted_values else 0.0,
        "avg_demand_bias": mean(demand_errors) if demand_errors else 0.0,
        "avg_abs_demand_error": mean(abs(v) for v in demand_errors) if demand_errors else 0.0,
        "avg_surge": mean(surge_values) if surge_values else 0.0,
        "raw_spawn_candidate_count": sum(e["raw_spawn_candidate_count"] for e in elasticity_events),
        "elasticity_removed_count": sum(e["elasticity_removed_count"] for e in elasticity_events),
        "actual_spawned_passengers": sum(e["actual_spawned_passengers"] for e in elasticity_events) if elasticity_events else spawned_count,
        "parquet_replay": parquet_replay,
        "matching": {
            "target_rate": mean(target_matching_rates) if target_matching_rates else 0.0,
            "actual_rate": len(accepted) / len(decisions) if decisions else 0.0,
            "matching_rate_error": (
                (len(accepted) / len(decisions) if decisions else 0.0) - mean(target_matching_rates)
                if target_matching_rates else (len(accepted) / len(decisions) if decisions else 0.0)
            ),
            "request_count": len(decisions),
            "matched_count": len(accepted),
            "by_raw_bucket": matching_by_raw_bucket,
        },
        "cells": {
            "by_raw_bucket": cells_by_raw_bucket,
        },
    }


def _latest_diagnostics(events: list[dict]) -> dict:
    diagnostics = {}
    for event in events:
        if event["type"] == "diagnostics":
            diagnostics.update({k: v for k, v in event.items() if k != "type"})
    return diagnostics


def _aggregate_by_raw_bucket(decisions: list[dict]) -> list[dict]:
    states = {
        bucket: {
            "request_count": 0,
            "matched_count": 0,
            "target_sum": 0.0,
            "target_count": 0,
            "wait_seconds": [],
        }
        for bucket, _, _, _ in RAW_SURGE_BUCKETS
    }
    defaults = {bucket: target for bucket, _, _, target in RAW_SURGE_BUCKETS}

    for decision in decisions:
        bucket = decision.get("bucket")
        if bucket not in states:
            bucket = raw_surge_bucket(float(decision.get("raw_surge", 1.0) or 1.0))
        state = states[bucket]
        state["request_count"] += 1
        if decision.get("accepted"):
            state["matched_count"] += 1
        if decision.get("target_matching_rate") is not None:
            state["target_sum"] += float(decision["target_matching_rate"])
            state["target_count"] += 1
        if decision.get("empty_wait_time_s") is not None:
            state["wait_seconds"].append(float(decision["empty_wait_time_s"]))

    rows = []
    for bucket, _, _, _ in RAW_SURGE_BUCKETS:
        state = states[bucket]
        request_count = int(state["request_count"])
        matched_count = int(state["matched_count"])
        target_rate = (
            state["target_sum"] / state["target_count"]
            if state["target_count"] else defaults[bucket]
        )
        actual_rate = matched_count / request_count if request_count else 0.0
        waits = state["wait_seconds"]
        rows.append({
            "bucket": bucket,
            "target_rate": target_rate,
            "actual_rate": actual_rate,
            "matching_rate_error": actual_rate - target_rate,
            "request_count": request_count,
            "matched_count": matched_count,
            "average_wait_seconds": mean(waits) if waits else 0.0,
            "p95_wait_seconds": _percentile(waits, 95.0) if waits else 0.0,
            "marginal_utility_points": [],
        })
    return rows


def _aggregate_cells_by_raw_bucket(surge_diagnostics: list[dict], decisions: list[dict]) -> list[dict]:
    states = {
        bucket: {
            "cells": set(),
            "supply_sum": 0.0,
            "demand_sum": 0.0,
            "raw_surge_sum": 0.0,
            "observation_count": 0,
            "dispatch_request_count": 0,
        }
        for bucket, _, _, _ in RAW_SURGE_BUCKETS
    }

    for decision in decisions:
        bucket = decision.get("bucket")
        if bucket not in states:
            bucket = raw_surge_bucket(float(decision.get("raw_surge", 1.0) or 1.0))
        states[bucket]["dispatch_request_count"] += 1

    for row in surge_diagnostics:
        raw_surge = float(row.get("raw_surge", row.get("surge", 1.0)) or 1.0)
        bucket = row.get("bucket")
        if bucket not in states:
            bucket = raw_surge_bucket(raw_surge)
        state = states[bucket]
        h3_cell = row.get("h3")
        if h3_cell:
            state["cells"].add(str(h3_cell))
        state["supply_sum"] += float(row.get("supply", 0.0) or 0.0)
        state["demand_sum"] += float(row.get("demand_for_surge", row.get("demand", 0.0)) or 0.0)
        state["raw_surge_sum"] += raw_surge
        state["observation_count"] += 1

    rows = []
    for bucket, _, _, _ in RAW_SURGE_BUCKETS:
        state = states[bucket]
        observations = int(state["observation_count"])
        cells = sorted(state["cells"])
        rows.append({
            "bucket": bucket,
            "unique_cell_count": len(cells),
            "avg_raw_surge": state["raw_surge_sum"] / observations if observations else 0.0,
            "avg_supply": state["supply_sum"] / observations if observations else 0.0,
            "avg_demand": state["demand_sum"] / observations if observations else 0.0,
            "dispatch_request_count": int(state["dispatch_request_count"]),
            "sample_h3_cells": cells[:10],
        })
    return rows


def _percentile(values: list[float], percentile: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    rank = (len(ordered) - 1) * percentile / 100.0
    lower = math.floor(rank)
    upper = math.ceil(rank)
    if lower == upper:
        return ordered[int(rank)]
    weight = rank - lower
    return ordered[lower] * (1.0 - weight) + ordered[upper] * weight


def _latest_parquet_replay(events: list[dict]) -> dict:
    replay = {key: 0 for key in PARQUET_REPLAY_STAT_KEYS}
    for event in events:
        if event["type"] == "parquet_replay":
            replay.update({
                key: int(event.get(key, 0))
                for key in PARQUET_REPLAY_STAT_KEYS
            })
    return replay


def _mean_present(values) -> float | None:
    present = [v for v in values if v is not None]
    return mean(present) if present else None


def _invalid_reason(beta_f: float | None, alpha_sensitivity: float) -> str | None:
    # 수학적으로 역산이 불가능한 조합은 SUMO를 띄우기 전에 invalid row로 남긴다.
    if beta_f is not None and not math.isfinite(beta_f):
        return "beta_f must be finite when provided"
    if not math.isfinite(alpha_sensitivity) or alpha_sensitivity <= 1e-9:
        return "alpha_sensitivity must be positive and finite"
    return None


def _run_one(
    elasticity: float,
    beta_f: float | None,
    seed: int,
    sim_duration: float,
    step_length: float,
    *,
    demand_source: str = ExperimentConfig.demand_source,
    prediction_mode: str = ExperimentConfig.prediction_mode,
    prediction_url: str = ExperimentConfig.prediction_url,
    prediction_horizon_min: int = ExperimentConfig.prediction_horizon_min,
    passenger_elasticity: float = ExperimentConfig.passenger_elasticity,
    passengers_per_5min: int | None = ExperimentConfig.passengers_per_5min,
    alpha_sensitivity: float = ExperimentConfig.alpha_sensitivity,
    weather_source: str = ExperimentConfig.weather_source,
) -> dict:
    # 각 조합은 독립 SimulationManager와 독립 SUMO/TraCI 연결을 사용해 상태 누수를 막는다.
    effective_prediction_mode = (
        "sync" if demand_source == "predicted" and prediction_mode == "none" else prediction_mode
    )
    params = {
        "elasticity": elasticity,
        "beta_f": beta_f,
        "seed": seed,
        "demand_source": demand_source,
        "prediction_mode": effective_prediction_mode,
        "prediction_url": prediction_url,
        "prediction_horizon_min": prediction_horizon_min,
        "passenger_elasticity": passenger_elasticity,
        "passengers_per_5min": passengers_per_5min,
        "alpha_sensitivity": alpha_sensitivity,
        "weather_source": weather_source,
    }
    reason = _invalid_reason(beta_f, alpha_sensitivity)
    if reason:
        return {"status": "invalid", "reason": reason, "params": params, "metrics": None}

    manager = SimulationManager.fresh_experiment(
        ExperimentConfig(
            elasticity=elasticity,
            beta_f=beta_f,
            seed=seed,
            sim_duration=sim_duration,
            step_length=step_length,
            demand_source=demand_source,
            prediction_mode=effective_prediction_mode,
            prediction_url=prediction_url,
            prediction_horizon_min=prediction_horizon_min,
            passenger_elasticity=passenger_elasticity,
            passengers_per_5min=passengers_per_5min,
            alpha_sensitivity=alpha_sensitivity,
            weather_source=weather_source,
        )
    )
    try:
        events = manager.run_experiment()
    except Exception as e:  # noqa: BLE001 — SUMO/TraCI 측 예외 종류가 다양해 광범위 캐치 필요
        # 한 조합이 죽어도 sweep 전체가 망가지지 않도록 error row로 남긴다.
        return {"status": "error", "reason": repr(e), "params": params, "metrics": None}
    return {
        "status": "ok",
        "reason": None,
        "params": params,
        "metrics": _aggregate(events, sim_duration),
    }


def _append_csv(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    exists = path.exists()
    if exists and path.stat().st_size > 0:
        with path.open("r", newline="", encoding="utf-8") as f:
            existing_header = next(csv.reader(f), None)
        if existing_header != CSV_COLUMNS:
            raise ValueError(
                f"CSV header mismatch for {path}; refusing to append rows with a different schema"
            )
    with path.open("a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_COLUMNS)
        if not exists or path.stat().st_size == 0:
            writer.writeheader()
        for row in rows:
            params = row["params"]
            metrics = row["metrics"] or {}
            writer.writerow({
                "status": row["status"],
                "reason": row["reason"],
                "elasticity": params.get("elasticity"),
                "beta_f": params.get("beta_f"),
                "seed": params.get("seed"),
                "demand_source": params.get("demand_source"),
                "prediction_mode": params.get("prediction_mode"),
                "prediction_url": params.get("prediction_url"),
                "prediction_horizon_min": params.get("prediction_horizon_min"),
                "passenger_elasticity": params.get("passenger_elasticity"),
                "alpha_sensitivity": params.get("alpha_sensitivity"),
                "weather_source": params.get("weather_source"),
                **{col: metrics.get(col) for col in CSV_COLUMNS[PARAM_COLUMN_COUNT:]},
            })


def main() -> int:
    parser = argparse.ArgumentParser(description="Run SUMO acceptance experiment sweeps.")
    parser.add_argument("--elasticity", type=float, default=ExperimentConfig.elasticity)
    parser.add_argument("--elasticity-list")
    parser.add_argument("--beta-f", type=float)
    parser.add_argument("--beta-f-list")
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--sim-duration", type=float, default=SIM_DURATION)
    parser.add_argument("--step-length", type=float, default=STEP_LENGTH)
    parser.add_argument("--demand-source", choices=("actual", "predicted"), default="actual")
    parser.add_argument("--prediction-mode", choices=("none", "sync", "async"), default="none")
    parser.add_argument(
        "--prediction-url",
        default="https://module3-ml.onrender.com/predict",
    )
    parser.add_argument("--prediction-horizon-min", type=int, default=15)
    parser.add_argument("--passenger-elasticity", type=float, default=0.0)
    parser.add_argument("--passengers-per-5min", type=int)
    parser.add_argument("--alpha-sensitivity", type=float, default=1.0)
    parser.add_argument("--alpha-sensitivity-list")
    parser.add_argument("--weather-source", choices=("static",), default="static")
    parser.add_argument("--csv-output", type=Path)
    parser.add_argument(
        "--json-output",
        type=Path,
        help="JSON 결과를 파일로 저장. 미지정 시 stdout으로 출력 (SUMO 로그와 섞일 수 있음).",
    )
    args = parser.parse_args()

    elasticities = _parse_float_list(args.elasticity_list, args.elasticity)
    beta_fs = _parse_optional_float_list(args.beta_f_list, args.beta_f)
    alpha_sensitivities = _parse_float_list(
        args.alpha_sensitivity_list,
        args.alpha_sensitivity,
    )

    # 조합별로 즉시 CSV append 한다. sweep 도중 SUMO/TraCI가 죽어도 완료된 row는 보존된다.
    rows: list[dict] = []
    for elasticity, beta_f, alpha_sensitivity in itertools.product(
        elasticities,
        beta_fs,
        alpha_sensitivities,
    ):
        row = _run_one(
            elasticity,
            beta_f,
            args.seed,
            args.sim_duration,
            args.step_length,
            demand_source=args.demand_source,
            prediction_mode=args.prediction_mode,
            prediction_url=args.prediction_url,
            prediction_horizon_min=args.prediction_horizon_min,
            passenger_elasticity=args.passenger_elasticity,
            passengers_per_5min=args.passengers_per_5min,
            alpha_sensitivity=alpha_sensitivity,
            weather_source=args.weather_source,
        )
        rows.append(row)
        if args.csv_output:
            _append_csv(args.csv_output, [row])

    payload = json.dumps(rows, ensure_ascii=False, indent=2)
    if args.json_output:
        args.json_output.parent.mkdir(parents=True, exist_ok=True)
        args.json_output.write_text(payload, encoding="utf-8")
    else:
        print(payload)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
