"""
Paired policy A/B: actual-demand vs predicted-demand (Module 3) on identical sim settings.

Module 3 validation KPIs come from horizon_eval events (predicted run only).
Policy lift = predicted − actual on Module 4 KPIs.

Usage:
  set PREDICTION_API_KEY=...
  uv run scripts/run_policy_ab_test.py --sim-duration 3600 --passenger-lambda 100
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.policy_comparison import POLICY_KPI_KEYS, compare_policy_ab
from app.prediction_config import require_prediction_api_key, resolve_prediction_url
from scripts.run_acceptance_experiment import _run_one


def main() -> int:
    p = argparse.ArgumentParser(description="Actual vs predicted demand policy A/B")
    p.add_argument("--seed", type=int, default=42)
    p.add_argument("--sim-duration", type=float, default=3600.0)
    p.add_argument("--passenger-lambda", type=int, default=100)
    p.add_argument("--n-taxis", type=int, default=300)
    p.add_argument("--alpha-sensitivity", type=float, default=1.5)
    p.add_argument("--elasticity", type=float, default=0.6)
    p.add_argument("--policy-mode", default="matching")
    p.add_argument("--dispatch-max-candidates", type=int, default=None)
    p.add_argument("--surge-max", type=float, default=None)
    p.add_argument("--band-incentive-usd", default="")
    p.add_argument("--fast", action="store_true")
    p.add_argument("--simulation-speed", type=float, default=None)
    p.add_argument("--json-output", type=Path, default=None)
    args = p.parse_args()

    require_prediction_api_key()
    band = None
    if args.band_incentive_usd.strip():
        parts = [float(x) for x in args.band_incentive_usd.split(",")]
        if len(parts) == 4:
            band = tuple(parts)
    common = dict(
        elasticity=args.elasticity,
        beta_f=None,
        seed=args.seed,
        sim_duration=args.sim_duration,
        step_length=None,
        alpha_sensitivity=args.alpha_sensitivity,
        policy_mode=args.policy_mode,
        n_taxis=args.n_taxis,
        passenger_lambda=args.passenger_lambda,
        fast=args.fast,
        simulation_speed=args.simulation_speed,
        prediction_url=resolve_prediction_url(),
        dispatch_max_candidates=args.dispatch_max_candidates,
        surge_max=args.surge_max,
        band_incentive_usd=band,
    )

    actual_row = _run_one(demand_source="actual", **common)
    predicted_row = _run_one(demand_source="predicted", prediction_mode="sync", **common)

    report: dict[str, object] = {
        "seed": args.seed,
        "sim_duration": args.sim_duration,
        "passenger_lambda": args.passenger_lambda,
        "n_taxis": args.n_taxis,
        "actual": actual_row,
        "predicted": predicted_row,
    }

    if actual_row.get("status") == "ok" and predicted_row.get("status") == "ok":
        am = actual_row["metrics"]
        pm = predicted_row["metrics"]
        report["policy_comparison"] = compare_policy_ab(am, pm)
        report["module3_validation"] = {
            k: pm.get(k)
            for k in (
                "module3_horizon_eval_count",
                "module3_horizon_mae_avg",
                "module3_horizon_bias_avg",
                "module3_horizon_rmse_avg",
                "module3_horizon_mape_avg",
                "prediction_request_count",
                "prediction_success_count",
                "avg_demand_bias",
                "avg_abs_demand_error",
            )
        }
        report["policy_kpi_table"] = {
            key: {"actual": am.get(key), "predicted": pm.get(key)}
            for key in POLICY_KPI_KEYS
        }

    text = json.dumps(report, ensure_ascii=False, indent=2)
    if args.json_output:
        args.json_output.parent.mkdir(parents=True, exist_ok=True)
        args.json_output.write_text(text, encoding="utf-8")
        print(f"Wrote {args.json_output}", flush=True)
    print(text, flush=True)
    ok = actual_row.get("status") == "ok" and predicted_row.get("status") == "ok"
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
