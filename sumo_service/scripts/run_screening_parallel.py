"""
Host-side parallel screening: N × Docker SUMO runs (~3 min wall each).

Usage (from repo root):
  python sumo_service/scripts/run_screening_parallel.py --jobs 3 --sim-duration 1000
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
from scripts.screening_scenarios import Scenario, build_screen_scenarios  # noqa: E402


def _docker_run(
    scenario: Scenario,
    *,
    sim_duration: float,
    seed: int,
    out_dir: Path,
) -> dict:
    out_json = out_dir / f"{scenario.scenario_id}.json"
    inner = " ".join(
        [
            "uv pip install --python /app/.venv/bin/python httpx -q",
            "&&",
            "/app/.venv/bin/python /app/scripts/run_screening_one.py",
            f"--sim-duration {sim_duration}",
            f"--seed {seed}",
            f"--json-output /temp/screen/{scenario.scenario_id}.json",
            *scenario_to_cli_args(scenario),
            *experiment_cli_flags(),
        ]
    )
    speed = float(os.getenv("SIMULATION_SPEED", "20"))
    fast = os.getenv("EXPERIMENT_FAST", "1").strip().lower() not in ("0", "false", "no")
    timeout_s = (int(sim_duration * 3) + 600) if fast else (int(sim_duration / max(speed, 1)) + 600)
    cmd = [
        "docker",
        "compose",
        "run",
        "--rm",
        "--no-deps",
        *docker_prediction_env(),
        "-v",
        f"{SUMO_ROOT / 'scripts'}:/app/scripts",
        "-v",
        f"{SUMO_ROOT / 'app'}:/app/app",
        "-v",
        f"{out_dir}:/temp/screen",
        "sumo-service",
        "bash",
        "-c",
        inner,
    ]
    proc = subprocess.run(
        cmd,
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        timeout=timeout_s,
    )
    if proc.returncode != 0:
        return {
            "scenario_id": scenario.scenario_id,
            "status": "error",
            "reason": (proc.stderr or proc.stdout or "")[-2000:],
            "metrics": None,
        }
    if out_json.exists():
        return json.loads(out_json.read_text(encoding="utf-8"))
    return {
        "scenario_id": scenario.scenario_id,
        "status": "error",
        "reason": "missing output json",
        "metrics": None,
    }


def _pick_finalists(rows: list[dict], *, top_per_case: int) -> dict:
    by_case: dict[str, list[dict]] = {}
    for row in rows:
        if row.get("status") != "ok" or not row.get("metrics"):
            continue
        case = row.get("case", "fair")
        by_case.setdefault(case, []).append(row)
    out = {}
    for case, items in by_case.items():
        ranked = sorted(items, key=lambda r: float(r.get("score", -1e9)), reverse=True)
        out[case] = [
            {
                "scenario_id": r["scenario_id"],
                "score": r.get("score"),
                "matching_success_rate": r["metrics"].get("matching_success_rate"),
                "avg_matching_rate_error": r["metrics"].get("avg_matching_rate_error"),
                "passengers_never_offered_rate": r["metrics"].get(
                    "passengers_never_offered_rate"
                ),
                "surge_clamped_rate": r["metrics"].get("surge_clamped_rate"),
                "dispatch_acceptance_rate": r["metrics"].get("dispatch_acceptance_rate"),
            }
            for r in ranked[:top_per_case]
        ]
    return out


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--jobs",
        type=int,
        default=4,
        help="parallel Docker runs (capped at 4; set DOCKER_MAX_JOBS to override cap)",
    )
    parser.add_argument("--sim-duration", type=float, default=1000.0)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--out-dir", type=Path, default=ROOT / ".temp" / "screen")
    parser.add_argument("--include-heavy", action="store_true")
    parser.add_argument("--top-per-case", type=int, default=2)
    args = parser.parse_args()
    args.jobs = clamp_docker_jobs(args.jobs)

    scenarios = list(build_screen_scenarios())
    if not args.include_heavy:
        scenarios = [s for s in scenarios if not s.heavy]

    args.out_dir.mkdir(parents=True, exist_ok=True)
    manifest = [
        {
            "scenario_id": s.scenario_id,
            "case": s.case,
            "ratio_label": s.ratio_label,
            "passenger_lambda": s.passenger_lambda,
            "n_taxis": s.n_taxis,
            "policy_mode": s.policy_mode,
            "alpha_sensitivity": s.alpha_sensitivity,
            "elasticity": s.elasticity,
            "heavy": s.heavy,
        }
        for s in scenarios
    ]
    (args.out_dir / "manifest.json").write_text(
        json.dumps(manifest, indent=2), encoding="utf-8"
    )

    fast = os.getenv("EXPERIMENT_FAST", "1").strip().lower() not in ("0", "false", "no")
    print(
        f"Running {len(scenarios)} scenarios, jobs={args.jobs}, sim_duration={args.sim_duration}, "
        f"fast={fast}",
        flush=True,
    )
    results: list[dict] = []
    with ThreadPoolExecutor(max_workers=args.jobs) as pool:
        futures = {
            pool.submit(
                _docker_run,
                s,
                sim_duration=args.sim_duration,
                seed=args.seed,
                out_dir=args.out_dir,
            ): s
            for s in scenarios
        }
        for fut in as_completed(futures):
            s = futures[fut]
            try:
                row = fut.result()
            except Exception as exc:  # noqa: BLE001
                row = {"scenario_id": s.scenario_id, "status": "error", "reason": repr(exc)}
            results.append(row)
            status = row.get("status", "?")
            m = row.get("metrics") or {}
            print(
                f"  [{status}] {s.scenario_id} ({s.ratio_label}) "
                f"match={m.get('matching_success_rate')} "
                f"never_offered={m.get('passengers_never_offered_rate')} "
                f"error={m.get('avg_matching_rate_error')}",
                flush=True,
            )

    finalists = _pick_finalists(results, top_per_case=args.top_per_case)
    summary = {
        "sim_duration": args.sim_duration,
        "seed": args.seed,
        "jobs": args.jobs,
        "finalists": finalists,
        "all": [
            {
                "scenario_id": r.get("scenario_id"),
                "case": r.get("case"),
                "status": r.get("status"),
                "score": r.get("score"),
                "metrics": r.get("metrics"),
            }
            for r in sorted(results, key=lambda x: x.get("scenario_id", ""))
        ],
    }
    summary_path = args.out_dir / "summary.json"
    summary_path.write_text(json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8")
    print(json.dumps(finalists, indent=2, ensure_ascii=False), flush=True)
    print(f"Wrote {summary_path}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
