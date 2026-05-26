from __future__ import annotations

import argparse
import csv
import itertools
import json
import sys
from pathlib import Path
from statistics import mean, median, quantiles

# 빈차 대기 분포는 long-tail이라 표본이 너무 적으면 p95가 사실상 max에 가까워 무의미.
MIN_SAMPLES_FOR_PERCENTILE = 20

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.driver.decision_function import pu_correction_constant
from app.simulation import (
    ExperimentConfig,
    N_TAXIS,
    SIM_DURATION,
    STEP_LENGTH,
    SimulationManager,
)

# CSV는 sweep을 여러 번 이어 붙일 수 있도록 JSON 결과의 params/metrics를 평탄화한 고정 컬럼을 쓴다.
CSV_COLUMNS = [
    "status",
    "reason",
    "target_p",
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
    "incentive_cost_total_usd",
    "capped_dispatch_attempt_rate",
    "avg_target_gap_when_capped",
    "avg_actual_acceptance_probability",
    "avg_required_fare_usd",
    "avg_incentive_usd",
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
]


def _parse_float_list(value: str | None, fallback: float | None) -> list[float]:
    if value:
        return [float(v.strip()) for v in value.split(",") if v.strip()]
    if fallback is None:
        raise ValueError("missing required value")
    return [float(fallback)]


def _aggregate(events: list[dict], sim_duration: float) -> dict:
    # SimulationManager는 실험 중 원시 이벤트만 남기고, KPI 정의는 runner 한 곳에서 집계한다.
    # 이렇게 두면 SUMO loop는 가볍게 유지하고 지표 정의 변경도 CLI 쪽에서 좁게 처리할 수 있다.
    spawned = [e for e in events if e["type"] == "passenger_spawned"]
    decisions = [e for e in events if e["type"] == "dispatch_decision"]
    accepted = [e for e in decisions if e["accepted"]]
    capped = [e for e in decisions if e.get("capped")]
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
        "incentive_cost_total_usd": sum(e.get("incentive_usd", 0.0) for e in accepted),
        "capped_dispatch_attempt_rate": len(capped) / len(decisions) if decisions else 0.0,
        "avg_target_gap_when_capped": (
            mean(e.get("target_gap", 0.0) for e in capped) if capped else 0.0
        ),
        "avg_actual_acceptance_probability": (
            mean(e.get("p_actual", 0.0) for e in decisions) if decisions else 0.0
        ),
        "avg_required_fare_usd": _mean_present(e.get("required_fare_usd") for e in decisions),
        "avg_incentive_usd": (
            mean(e.get("incentive_usd", 0.0) for e in decisions) if decisions else 0.0
        ),
        **diagnostics,
        "avg_actual_demand_for_surge": mean(actual_values) if actual_values else 0.0,
        "avg_predicted_demand_for_surge": mean(predicted_values) if predicted_values else 0.0,
        "avg_demand_bias": mean(demand_errors) if demand_errors else 0.0,
        "avg_abs_demand_error": mean(abs(v) for v in demand_errors) if demand_errors else 0.0,
        "avg_surge": mean(surge_values) if surge_values else 0.0,
    }


def _latest_diagnostics(events: list[dict]) -> dict:
    diagnostics = {}
    for event in events:
        if event["type"] == "diagnostics":
            diagnostics.update({k: v for k, v in event.items() if k != "type"})
    return diagnostics


def _mean_present(values) -> float | None:
    present = [v for v in values if v is not None]
    return mean(present) if present else None


def _invalid_reason(target_p: float, beta_f: float) -> str | None:
    # 수학적으로 역산이 불가능한 조합은 SUMO를 띄우기 전에 invalid row로 남긴다.
    c = pu_correction_constant()
    if target_p * c >= 1:
        return "target_p * c must be < 1"
    if abs(beta_f) < 1e-9:
        return "beta_f too close to zero for inverse fare calculation"
    return None


def _run_one(
    target_p: float,
    elasticity: float,
    beta_f: float,
    seed: int,
    sim_duration: float,
    step_length: float,
) -> dict:
    # 각 조합은 독립 SimulationManager와 독립 SUMO/TraCI 연결을 사용해 상태 누수를 막는다.
    params = {
        "target_p": target_p,
        "elasticity": elasticity,
        "beta_f": beta_f,
        "seed": seed,
        "demand_source": ExperimentConfig.demand_source,
        "prediction_mode": ExperimentConfig.prediction_mode,
        "prediction_url": ExperimentConfig.prediction_url,
        "prediction_horizon_min": ExperimentConfig.prediction_horizon_min,
        "passenger_elasticity": None,
        "alpha_sensitivity": None,
        "weather_source": ExperimentConfig.weather_source,
    }
    reason = _invalid_reason(target_p, beta_f)
    if reason:
        return {"status": "invalid", "reason": reason, "params": params, "metrics": None}

    manager = SimulationManager.fresh_experiment(
        ExperimentConfig(
            target_p=target_p,
            elasticity=elasticity,
            beta_f=beta_f,
            seed=seed,
            sim_duration=sim_duration,
            step_length=step_length,
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
                "target_p": params.get("target_p"),
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
                **{col: metrics.get(col) for col in CSV_COLUMNS[13:]},
            })


def main() -> int:
    parser = argparse.ArgumentParser(description="Run SUMO acceptance experiment sweeps.")
    parser.add_argument("--target-p", type=float)
    parser.add_argument("--target-p-list")
    parser.add_argument("--elasticity", type=float)
    parser.add_argument("--elasticity-list")
    parser.add_argument("--beta-f", type=float)
    parser.add_argument("--beta-f-list")
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--sim-duration", type=float, default=SIM_DURATION)
    parser.add_argument("--step-length", type=float, default=STEP_LENGTH)
    parser.add_argument("--csv-output", type=Path)
    parser.add_argument(
        "--json-output",
        type=Path,
        help="JSON 결과를 파일로 저장. 미지정 시 stdout으로 출력 (SUMO 로그와 섞일 수 있음).",
    )
    args = parser.parse_args()

    target_ps = _parse_float_list(args.target_p_list, args.target_p)
    elasticities = _parse_float_list(args.elasticity_list, args.elasticity)
    beta_fs = _parse_float_list(args.beta_f_list, args.beta_f)

    # 조합별로 즉시 CSV append 한다. sweep 도중 SUMO/TraCI가 죽어도 완료된 row는 보존된다.
    rows: list[dict] = []
    for target_p, elasticity, beta_f in itertools.product(target_ps, elasticities, beta_fs):
        row = _run_one(target_p, elasticity, beta_f, args.seed, args.sim_duration, args.step_length)
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
