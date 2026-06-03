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

from app.experiment_metrics import (
    aggregate_surge_band_from_buckets,
    aggregate_surge_band_metrics,
)
from app.module3_validation import aggregate_module3_horizon_metrics
from app.prediction_config import (
    require_prediction_api_key,
    resolve_demand_source,
    resolve_prediction_mode,
    resolve_prediction_url,
)
from app.rebalance import aggregate_rebalance_metrics
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
    "policy_mode",
    "rebalance_interval_s",
    "rebalance_top_k",
    "spawned_passengers",
    "unique_matched_passengers",
    "matching_success_rate",
    "dispatch_acceptance_rate",
    "passengers_never_offered_rate",
    "surge_clamped_rate",
    "avg_dispatch_decisions_per_passenger",
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
    "avg_high_surge_deficit",
    "p90_raw_surge",
    "high_surge_mean_raw_surge",
    "rebalance_redirect_count",
    "module3_horizon_eval_count",
    "module3_horizon_mae_avg",
    "module3_horizon_bias_avg",
    "module3_horizon_rmse_avg",
    "module3_horizon_mape_avg",
]
PARAM_COLUMN_COUNT = 15


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
    fd = next(
        (e for e in reversed(events) if e["type"] == "dispatch_kpi_fast_summary"),
        None,
    )
    decisions = [e for e in events if e["type"] == "dispatch_decision"]
    dispatch_attempts = [e for e in events if e["type"] == "dispatch_attempted"]
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
    rebalance_events = [e for e in events if e["type"] == "rebalance_redirect"]

    spawned_count = len(spawned)
    experiment_hours = sim_duration / 3600.0

    if fd is not None:
        decision_count = int(fd["decision_count"])
        accept_count = int(fd["accept_count"])
        unique_matched = int(fd["matched_passenger_count"])
        offered_count = int(fd["offered_passenger_count"])
        dpp = fd.get("decisions_per_passenger") or {}
        matching_success_rate = unique_matched / spawned_count if spawned_count else 0.0
        dispatch_acceptance_rate = (
            accept_count / decision_count if decision_count else 0.0
        )
        passengers_never_offered_rate = (
            (spawned_count - offered_count) / spawned_count if spawned_count else 0.0
        )
        surge_clamped_rate = (
            int(fd["surge_clamped_count"]) / decision_count if decision_count else 0.0
        )
        avg_dispatch_decisions_per_passenger = (
            sum(dpp.values()) / len(dpp) if dpp else 0.0
        )
        acceptances_per_driver_hour = (
            accept_count / (N_TAXIS * experiment_hours)
            if N_TAXIS > 0 and experiment_hours > 0
            else 0.0
        )
        mre_count = int(fd.get("matching_rate_error_count") or 0)
        avg_matching_rate_error = (
            float(fd["matching_rate_error_sum"]) / mre_count if mre_count else 0.0
        )
        mre_abs_count = mre_count
        avg_abs_matching_rate_error = (
            float(fd["matching_rate_error_abs_sum"]) / mre_abs_count
            if mre_abs_count
            else 0.0
        )
        p_actual_count = int(fd.get("p_actual_count") or 0)
        avg_actual_acceptance_probability = (
            float(fd["p_actual_sum"]) / p_actual_count if p_actual_count else 0.0
        )
        fs_count = int(fd.get("final_surge_count") or 0)
        avg_final_surge = float(fd["final_surge_sum"]) / fs_count if fs_count else 0.0
        ff_count = int(fd.get("final_fare_count") or 0)
        avg_final_fare_usd = float(fd["final_fare_sum"]) / ff_count if ff_count else 0.0
        rf_count = int(fd.get("required_fare_count") or 0)
        avg_required_fare_usd = (
            float(fd["required_fare_sum"]) / rf_count if rf_count else None
        )
        empty_waits = list(fd.get("empty_wait_seconds") or [])
        band_metrics = aggregate_surge_band_from_buckets(fd.get("buckets") or {})
    else:
        accepted = [e for e in decisions if e["accepted"]]
        attempted_passenger_ids = {e["passenger_id"] for e in dispatch_attempts}
        if not attempted_passenger_ids:
            attempted_passenger_ids = {
                e["passenger_id"] for e in decisions if e.get("passenger_id")
            }
        unique_matched = len({e["passenger_id"] for e in accepted})
        accept_count = len(accepted)
        decision_count = len(decisions)
        matching_success_rate = unique_matched / spawned_count if spawned_count else 0.0
        dispatch_acceptance_rate = (
            accept_count / decision_count if decision_count else 0.0
        )
        passengers_never_offered_rate = (
            (spawned_count - len(attempted_passenger_ids)) / spawned_count
            if spawned_count
            else 0.0
        )
        clamped_flags = [
            bool(e.get("surge_clamped"))
            for e in decisions
            if e.get("surge_clamped") is not None
        ]
        surge_clamped_rate = mean(clamped_flags) if clamped_flags else 0.0
        decisions_per_passenger: dict[str, int] = {}
        for decision in decisions:
            pid = decision.get("passenger_id")
            if pid:
                decisions_per_passenger[pid] = decisions_per_passenger.get(pid, 0) + 1
        avg_dispatch_decisions_per_passenger = (
            sum(decisions_per_passenger.values()) / len(decisions_per_passenger)
            if decisions_per_passenger
            else 0.0
        )
        acceptances_per_driver_hour = (
            accept_count / (N_TAXIS * experiment_hours)
            if N_TAXIS > 0 and experiment_hours > 0
            else 0.0
        )
        avg_matching_rate_error = (
            mean(matching_rate_errors) if matching_rate_errors else 0.0
        )
        avg_abs_matching_rate_error = (
            mean(abs(v) for v in matching_rate_errors) if matching_rate_errors else 0.0
        )
        avg_actual_acceptance_probability = (
            mean(e.get("p_actual", 0.0) for e in decisions) if decisions else 0.0
        )
        avg_final_surge = mean(final_surge_values) if final_surge_values else 0.0
        avg_final_fare_usd = mean(final_fare_values) if final_fare_values else 0.0
        avg_required_fare_usd = _mean_present(
            e.get("required_fare_usd") for e in decisions
        )
        empty_waits = [
            e["empty_wait_time_s"] for e in accepted if e.get("empty_wait_time_s") is not None
        ]
        band_metrics = aggregate_surge_band_metrics(decisions)
    completed_fare_total_usd = sum(e.get("fare_usd", 0.0) for e in completed_trips)
    driver_revenue_per_driver_hour_usd = (
        completed_fare_total_usd / (N_TAXIS * experiment_hours)
        if N_TAXIS > 0 and experiment_hours > 0
        else 0.0
    )

    p95_empty_wait = None
    if len(empty_waits) >= MIN_SAMPLES_FOR_PERCENTILE:
        p95_empty_wait = quantiles(empty_waits, n=20, method="inclusive")[18]

    if fd is None:
        avg_target_matching_rate = (
            mean(target_matching_rates) if target_matching_rates else 0.0
        )
    else:
        avg_target_matching_rate = 0.0  # not stored in fast summary; band targets cover P*

    return {
        "spawned_passengers": spawned_count,
        "unique_matched_passengers": unique_matched,
        "matching_success_rate": matching_success_rate,
        "dispatch_acceptance_rate": dispatch_acceptance_rate,
        "passengers_never_offered_rate": passengers_never_offered_rate,
        "surge_clamped_rate": surge_clamped_rate,
        "avg_dispatch_decisions_per_passenger": avg_dispatch_decisions_per_passenger,
        "accepted_dispatch_count": accept_count,
        "acceptances_per_driver_hour": acceptances_per_driver_hour,
        "driver_revenue_per_hour_usd": driver_revenue_per_driver_hour_usd,
        "avg_empty_wait_time_s": mean(empty_waits) if empty_waits else None,
        "p50_empty_wait_time_s": median(empty_waits) if empty_waits else None,
        "p95_empty_wait_time_s": p95_empty_wait,
        "avg_actual_acceptance_probability": avg_actual_acceptance_probability,
        "avg_target_matching_rate": avg_target_matching_rate,
        "avg_matching_rate_error": avg_matching_rate_error,
        "avg_abs_matching_rate_error": avg_abs_matching_rate_error,
        "avg_required_fare_usd": avg_required_fare_usd,
        "avg_final_surge": avg_final_surge,
        "avg_final_fare_usd": avg_final_fare_usd,
        **diagnostics,
        "avg_actual_demand_for_surge": mean(actual_values) if actual_values else 0.0,
        "avg_predicted_demand_for_surge": mean(predicted_values) if predicted_values else 0.0,
        "avg_demand_bias": mean(demand_errors) if demand_errors else 0.0,
        "avg_abs_demand_error": mean(abs(v) for v in demand_errors) if demand_errors else 0.0,
        "avg_surge": mean(surge_values) if surge_values else 0.0,
        "raw_spawn_candidate_count": sum(e["raw_spawn_candidate_count"] for e in elasticity_events),
        "elasticity_removed_count": sum(e["elasticity_removed_count"] for e in elasticity_events),
        "actual_spawned_passengers": sum(e["actual_spawned_passengers"] for e in elasticity_events) if elasticity_events else spawned_count,
        **aggregate_rebalance_metrics(surge_diagnostics, rebalance_events),
        **band_metrics,
        **aggregate_module3_horizon_metrics(events),
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


def _parse_target_matching_rate_overrides(raw: str | None) -> tuple[tuple[str, float], ...] | None:
    if not raw or not str(raw).strip():
        return None
    data = json.loads(raw)
    if not isinstance(data, dict):
        raise ValueError("target-matching-rates must be a JSON object")
    return tuple((str(k), float(v)) for k, v in data.items())


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
    step_length: float | None = None,
    *,
    fast: bool = False,
    simulation_speed: float | None = None,
    policy_update_interval_real_s: float | None = None,
    demand_source: str = ExperimentConfig.demand_source,
    prediction_mode: str = ExperimentConfig.prediction_mode,
    prediction_url: str = ExperimentConfig.prediction_url,
    prediction_horizon_min: int = ExperimentConfig.prediction_horizon_min,
    passenger_elasticity: float = ExperimentConfig.passenger_elasticity,
    alpha_sensitivity: float = ExperimentConfig.alpha_sensitivity,
    weather_source: str = ExperimentConfig.weather_source,
    policy_mode: str = ExperimentConfig.policy_mode,
    rebalance_interval_s: float = ExperimentConfig.rebalance_interval_s,
    rebalance_top_k: int = ExperimentConfig.rebalance_top_k,
    rebalance_min_raw_surge: float = ExperimentConfig.rebalance_min_raw_surge,
    rebalance_acceptance_coef: float = ExperimentConfig.rebalance_acceptance_coef,
    n_taxis: int | None = None,
    passenger_lambda: int | None = None,
    dispatch_max_candidates: int | None = None,
    surge_max: float | None = None,
    band_incentive_usd: tuple[float, float, float, float] | None = None,
    target_p: float | None = None,
    target_p_bucket: str = "raw_gte_3_5",
    target_matching_rate_overrides: tuple[tuple[str, float], ...] | None = None,
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
        "alpha_sensitivity": alpha_sensitivity,
        "weather_source": weather_source,
        "policy_mode": policy_mode,
        "rebalance_interval_s": rebalance_interval_s,
        "rebalance_top_k": rebalance_top_k,
        "n_taxis": n_taxis,
        "passenger_lambda": passenger_lambda,
        "dispatch_max_candidates": dispatch_max_candidates,
        "surge_max": surge_max,
        "band_incentive_usd": list(band_incentive_usd) if band_incentive_usd else None,
        "fast": fast,
        "simulation_speed": simulation_speed,
        "policy_update_interval_real_s": policy_update_interval_real_s,
        "target_p": target_p,
        "target_p_bucket": target_p_bucket,
        "target_matching_rate_overrides": (
            dict(target_matching_rate_overrides) if target_matching_rate_overrides else None
        ),
    }
    if policy_mode not in ("matching", "rebalance"):
        return {
            "status": "invalid",
            "reason": "policy_mode must be matching or rebalance",
            "params": params,
            "metrics": None,
        }
    reason = _invalid_reason(beta_f, alpha_sensitivity)
    if reason:
        return {"status": "invalid", "reason": reason, "params": params, "metrics": None}

    config_kwargs: dict = {
        "elasticity": elasticity,
        "beta_f": beta_f,
        "seed": seed,
        "sim_duration": sim_duration,
        "demand_source": demand_source,
        "prediction_mode": effective_prediction_mode,
        "prediction_url": prediction_url,
        "prediction_horizon_min": prediction_horizon_min,
        "passenger_elasticity": passenger_elasticity,
        "alpha_sensitivity": alpha_sensitivity,
        "weather_source": weather_source,
        "policy_mode": policy_mode,
        "rebalance_interval_s": rebalance_interval_s,
        "rebalance_top_k": rebalance_top_k,
        "rebalance_min_raw_surge": rebalance_min_raw_surge,
        "rebalance_acceptance_coef": rebalance_acceptance_coef,
        "n_taxis": n_taxis,
        "passenger_lambda": passenger_lambda,
        "dispatch_max_candidates": dispatch_max_candidates,
        "surge_max": surge_max,
        "band_incentive_usd": band_incentive_usd,
        "fast": fast,
        "target_p": target_p,
        "target_p_bucket": target_p_bucket,
        "target_matching_rate_overrides": target_matching_rate_overrides,
    }
    if step_length is not None:
        config_kwargs["step_length"] = step_length
    if simulation_speed is not None:
        config_kwargs["simulation_speed"] = simulation_speed
    if policy_update_interval_real_s is not None:
        config_kwargs["policy_update_interval_real_s"] = policy_update_interval_real_s

    manager = SimulationManager.fresh_experiment(ExperimentConfig(**config_kwargs))
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
    parser.add_argument(
        "--step-length",
        type=float,
        default=None,
        help="TraCI step-length (sim sec). Default: 1.0 if --fast else SIMULATION_SPEED/60",
    )
    parser.add_argument(
        "--fast",
        action="store_true",
        help="Bench mode: no wall-clock sleep, step-length 1.0 (no Lab pacing)",
    )
    parser.add_argument(
        "--simulation-speed",
        type=float,
        default=None,
        help="Sim seconds per real second when paced (default: SIMULATION_SPEED env)",
    )
    parser.add_argument(
        "--policy-update-interval-real-s",
        type=float,
        default=None,
        help="Wall-clock seconds between surge+prediction refresh (default: 900)",
    )
    parser.add_argument(
        "--demand-source",
        choices=("actual", "predicted"),
        default=None,
        help="default: predicted (use --demand-source actual only for baseline comparison)",
    )
    parser.add_argument(
        "--prediction-mode",
        choices=("none", "sync", "async"),
        default=None,
        help="default: sync when demand-source=predicted, else none",
    )
    parser.add_argument(
        "--prediction-url",
        default=None,
        help="default: PREDICTION_URL env or https://module3-ml.onrender.com/predict",
    )
    parser.add_argument("--prediction-horizon-min", type=int, default=15)
    parser.add_argument("--passenger-elasticity", type=float, default=0.0)
    parser.add_argument("--alpha-sensitivity", type=float, default=1.0)
    parser.add_argument("--alpha-sensitivity-list")
    parser.add_argument(
        "--target-p",
        type=float,
        default=None,
        help="Override target acceptance P* for one raw_surge bucket (see --target-p-bucket)",
    )
    parser.add_argument(
        "--target-p-bucket",
        default="raw_gte_3_5",
        choices=("raw_lt_1_5", "raw_lt_2_5", "raw_lt_3_5", "raw_gte_3_5"),
        help="Bucket key for --target-p (default: raw_gte_3_5 high surge)",
    )
    parser.add_argument(
        "--target-matching-rates",
        default=None,
        help='JSON object of bucket→P*, e.g. \'{"raw_gte_3_5":0.90,"raw_lt_3_5":0.82}\'',
    )
    parser.add_argument("--weather-source", choices=("static",), default="static")
    parser.add_argument(
        "--policy-mode",
        choices=("matching", "rebalance"),
        default=ExperimentConfig.policy_mode,
        help="matching: inverse fare only. rebalance: inverse fare + empty-taxi redirect to hot cells.",
    )
    parser.add_argument(
        "--rebalance-interval-s",
        type=float,
        default=ExperimentConfig.rebalance_interval_s,
    )
    parser.add_argument("--rebalance-top-k", type=int, default=ExperimentConfig.rebalance_top_k)
    parser.add_argument(
        "--rebalance-min-raw-surge",
        type=float,
        default=ExperimentConfig.rebalance_min_raw_surge,
    )
    parser.add_argument(
        "--rebalance-acceptance-coef",
        type=float,
        default=ExperimentConfig.rebalance_acceptance_coef,
    )
    parser.add_argument("--csv-output", type=Path)
    parser.add_argument(
        "--json-output",
        type=Path,
        help="JSON 결과를 파일로 저장. 미지정 시 stdout으로 출력 (SUMO 로그와 섞일 수 있음).",
    )
    args = parser.parse_args()

    demand_source = resolve_demand_source(args.demand_source)
    prediction_mode = resolve_prediction_mode(demand_source, args.prediction_mode)
    prediction_url = args.prediction_url or resolve_prediction_url()
    if demand_source == "predicted":
        require_prediction_api_key()

    elasticities = _parse_float_list(args.elasticity_list, args.elasticity)
    beta_fs = _parse_optional_float_list(args.beta_f_list, args.beta_f)
    alpha_sensitivities = _parse_float_list(
        args.alpha_sensitivity_list,
        args.alpha_sensitivity,
    )
    target_overrides = _parse_target_matching_rate_overrides(args.target_matching_rates)

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
            fast=args.fast,
            simulation_speed=args.simulation_speed,
            policy_update_interval_real_s=args.policy_update_interval_real_s,
            demand_source=demand_source,
            prediction_mode=prediction_mode,
            prediction_url=prediction_url,
            prediction_horizon_min=args.prediction_horizon_min,
            passenger_elasticity=args.passenger_elasticity,
            alpha_sensitivity=alpha_sensitivity,
            weather_source=args.weather_source,
            policy_mode=args.policy_mode,
            rebalance_interval_s=args.rebalance_interval_s,
            rebalance_top_k=args.rebalance_top_k,
            rebalance_min_raw_surge=args.rebalance_min_raw_surge,
            rebalance_acceptance_coef=args.rebalance_acceptance_coef,
            target_p=args.target_p,
            target_p_bucket=args.target_p_bucket,
            target_matching_rate_overrides=target_overrides,
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
