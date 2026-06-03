"""
Long predicted-only runs for Module 3 horizon validation (parallel Docker).

Default scenarios: diverse λ / policy — predicted demand, Lab pacing (not --fast).

Usage (repo root):
  $env:PREDICTION_API_KEY = "..."
  python sumo_service/scripts/run_module3_validation_parallel.py --jobs 3 --sim-duration 43200
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SUMO_ROOT = ROOT / "sumo_service"
sys.path.insert(0, str(SUMO_ROOT))

from scripts.docker_run_helpers import (  # noqa: E402
    clamp_docker_jobs,
    docker_prediction_env,
    experiment_cli_flags,
    scenario_to_cli_args,
)
from scripts.screening_scenarios import Scenario  # noqa: E402

# Module 3 검증용: predicted만, λ·정책 다양
MODULE3_SCENARIOS: tuple[Scenario, ...] = (
    Scenario("m3_fair40", "fair", passenger_lambda=100, ratio_label="4.0:1"),
    Scenario("m3_dispatch10", "fair", passenger_lambda=100, dispatch_max_candidates=10, ratio_label="4.0:1 K10"),
    Scenario("m3_stress55", "stress", passenger_lambda=138, ratio_label="5.5:1"),
    Scenario("m3_ratio35", "fair", passenger_lambda=88, ratio_label="3.5:1"),
    Scenario("m3_rebalance", "imbalance", passenger_lambda=100, policy_mode="rebalance", ratio_label="4.0:1 reb"),
)


def _docker_run(scenario: Scenario, *, sim_duration: float, seed: int, out_dir: Path) -> dict:
    out_json = out_dir / f"{scenario.scenario_id}.json"
    inner = " ".join(
        [
            "uv pip install --python /app/.venv/bin/python httpx -q &&",
            "/app/.venv/bin/python /app/scripts/run_screening_one.py",
            f"--sim-duration {sim_duration}",
            f"--seed {seed}",
            f"--json-output /temp/m3/{scenario.scenario_id}.json",
            *scenario_to_cli_args(scenario),
            *experiment_cli_flags(),
        ]
    )
    fast = os.getenv("EXPERIMENT_FAST", "1").strip().lower() not in ("0", "false", "no")
    speed = float(os.getenv("SIMULATION_SPEED", "20"))
    timeout_s = (int(sim_duration * 3) + 3600) if fast else (int(sim_duration / max(speed, 1)) + 3600)
    cmd = [
        "docker", "compose", "run", "--rm", "--no-deps",
        *docker_prediction_env(demand_source="predicted"),
        "-v", f"{SUMO_ROOT / 'scripts'}:/app/scripts",
        "-v", f"{SUMO_ROOT / 'app'}:/app/app",
        "-v", f"{out_dir}:/temp/m3",
        "sumo-service", "bash", "-c", inner,
    ]
    proc = subprocess.run(cmd, cwd=str(ROOT), capture_output=True, text=True, timeout=timeout_s)
    if proc.returncode != 0:
        return {
            "scenario_id": scenario.scenario_id,
            "status": "error",
            "reason": (proc.stderr or proc.stdout or "")[-3000:],
        }
    if out_json.exists():
        return json.loads(out_json.read_text(encoding="utf-8"))
    return {"scenario_id": scenario.scenario_id, "status": "error", "reason": "missing json"}


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--jobs", type=int, default=4, help="parallel Docker runs (capped at 4)")
    p.add_argument("--sim-duration", type=float, default=43200.0, help="12 sim-hours @ speed 20 ~ 36min wall each")
    p.add_argument("--seed", type=int, default=42)
    p.add_argument("--out-dir", type=Path, default=ROOT / ".temp" / "m3_validation")
    args = p.parse_args()
    args.jobs = clamp_docker_jobs(args.jobs)
    args.out_dir.mkdir(parents=True, exist_ok=True)

    print(f"Module3 validation: {len(MODULE3_SCENARIOS)} runs, jobs={args.jobs}, sim={args.sim_duration}")
    rows: list[dict] = []
    with ThreadPoolExecutor(max_workers=args.jobs) as pool:
        futs = {
            pool.submit(_docker_run, s, sim_duration=args.sim_duration, seed=args.seed, out_dir=args.out_dir): s
            for s in MODULE3_SCENARIOS
        }
        for fut in as_completed(futs):
            s = futs[fut]
            try:
                row = fut.result()
            except Exception as exc:  # noqa: BLE001
                row = {"scenario_id": s.scenario_id, "status": "error", "reason": repr(exc)}
            rows.append(row)
            m = row.get("metrics") or {}
            print(
                f"  [{row.get('status')}] {s.scenario_id} "
                f"m3_evals={m.get('module3_horizon_eval_count')} "
                f"m3_mae={m.get('module3_horizon_mae_avg')} "
                f"pred_ok={m.get('prediction_success_count')}",
                flush=True,
            )

    summary = {"sim_duration": args.sim_duration, "runs": rows}
    path = args.out_dir / "summary.json"
    path.write_text(json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"Wrote {path}", flush=True)
    return 0 if all(r.get("status") == "ok" for r in rows) else 1


if __name__ == "__main__":
    raise SystemExit(main())
