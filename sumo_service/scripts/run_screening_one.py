"""Run a single screening scenario (Docker or local). Writes JSON result path."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.prediction_config import (  # noqa: E402
    require_prediction_api_key,
    resolve_demand_source,
    resolve_prediction_mode,
    resolve_prediction_url,
)
from scripts.run_acceptance_experiment import _run_one  # noqa: E402
from scripts.screening_scenarios import score_scenario  # noqa: E402


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--scenario-id", required=True)
    p.add_argument("--case", default="fair")
    p.add_argument("--seed", type=int, default=42)
    p.add_argument("--sim-duration", type=float, default=1000.0)
    p.add_argument("--step-length", type=float, default=None)
    p.add_argument("--fast", action="store_true", help="Bench mode (no Lab pacing)")
    p.add_argument("--simulation-speed", type=float, default=None)
    p.add_argument("--policy-update-interval-real-s", type=float, default=None)
    p.add_argument("--passenger-lambda", type=int, required=True)
    p.add_argument("--n-taxis", type=int, default=300)
    p.add_argument("--policy-mode", default="matching")
    p.add_argument("--alpha-sensitivity", type=float, default=1.5)
    p.add_argument("--elasticity", type=float, default=0.6)
    p.add_argument("--dispatch-max-candidates", type=int, default=None)
    p.add_argument("--surge-max", type=float, default=None)
    p.add_argument("--band-incentive-usd", default="")
    p.add_argument("--demand-source", choices=("actual", "predicted"), default=None)
    p.add_argument("--prediction-mode", choices=("none", "sync", "async"), default=None)
    p.add_argument("--prediction-url", default=None)
    p.add_argument(
        "--target-p",
        type=float,
        default=None,
        help="Override target acceptance P* for one bucket (see --target-p-bucket)",
    )
    p.add_argument(
        "--target-p-bucket",
        default="raw_gte_3_5",
        help="Bucket key for --target-p (default high surge)",
    )
    p.add_argument("--json-output", type=Path, required=True)
    args = p.parse_args()

    demand_source = resolve_demand_source(args.demand_source)
    prediction_mode = resolve_prediction_mode(demand_source, args.prediction_mode)
    prediction_url = args.prediction_url or resolve_prediction_url()
    if demand_source == "predicted":
        require_prediction_api_key()

    band = None
    if args.band_incentive_usd.strip():
        parts = [float(x) for x in args.band_incentive_usd.split(",")]
        if len(parts) == 4:
            band = tuple(parts)

    row = _run_one(
        args.elasticity,
        None,
        args.seed,
        args.sim_duration,
        args.step_length,
        fast=args.fast,
        simulation_speed=args.simulation_speed,
        policy_update_interval_real_s=args.policy_update_interval_real_s,
        alpha_sensitivity=args.alpha_sensitivity,
        policy_mode=args.policy_mode,
        n_taxis=args.n_taxis,
        passenger_lambda=args.passenger_lambda,
        dispatch_max_candidates=args.dispatch_max_candidates,
        surge_max=args.surge_max,
        band_incentive_usd=band,
        demand_source=demand_source,
        prediction_mode=prediction_mode,
        prediction_url=prediction_url,
        target_p=args.target_p,
        target_p_bucket=args.target_p_bucket,
    )
    row["scenario_id"] = args.scenario_id
    row["demand_source"] = demand_source
    row["prediction_mode"] = prediction_mode
    row["case"] = args.case
    if row.get("metrics"):
        row["score"] = score_scenario(args.case, row["metrics"])

    args.json_output.parent.mkdir(parents=True, exist_ok=True)
    args.json_output.write_text(json.dumps(row, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(row, ensure_ascii=False), flush=True)
    return 0 if row.get("status") == "ok" else 1


if __name__ == "__main__":
    raise SystemExit(main())
