"""
Long runs for screening finalists: actual vs predicted policy A/B per scenario.

Reads finalist IDs from `.temp/screen/finalists_for_overnight.json` or CLI.

Usage:
  python sumo_service/scripts/run_finalists_overnight.py --jobs 2 --sim-duration 3600
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

from scripts.docker_run_helpers import (
    clamp_docker_jobs,
    docker_prediction_env,
    experiment_cli_flags,
    scenario_to_policy_ab_args,
)
from scripts.screening_scenarios import build_screen_scenarios

DEFAULT_FINALISTS = (
    "fair_dispatch10",
    "fair_ratio35",
    "fair_ratio40",
    "B_stress_55",
    "imb_rebalance_40",
)


def _scenario_by_id(scenario_id: str):
    for s in build_screen_scenarios():
        if s.scenario_id == scenario_id:
            return s
    raise KeyError(scenario_id)


def _run_ab(scenario_id: str, *, sim_duration: float, seed: int, out_dir: Path) -> dict:
    s = _scenario_by_id(scenario_id)
    out_json = out_dir / f"{scenario_id}_ab.json"
    inner = " ".join(
        [
            "uv pip install --python /app/.venv/bin/python httpx -q &&",
            "/app/.venv/bin/python /app/scripts/run_policy_ab_test.py",
            f"--seed {seed}",
            f"--sim-duration {sim_duration}",
            f"--json-output /temp/finalists/{scenario_id}_ab.json",
            *scenario_to_policy_ab_args(s),
            *experiment_cli_flags(),
        ]
    )
    cmd = [
        "docker", "compose", "run", "--rm", "--no-deps", *docker_prediction_env(),
        "-v", f"{SUMO_ROOT / 'scripts'}:/app/scripts",
        "-v", f"{SUMO_ROOT / 'app'}:/app/app",
        "-v", f"{out_dir}:/temp/finalists",
        "sumo-service", "bash", "-c", inner,
    ]
    fast = os.getenv("EXPERIMENT_FAST", "1").strip().lower() not in ("0", "false", "no")
    speed = float(os.getenv("SIMULATION_SPEED", "20"))
    per_arm = (int(sim_duration * 3) + 600) if fast else (int(sim_duration / max(speed, 1)) + 600)
    timeout = 2 * per_arm + 600
    proc = subprocess.run(cmd, cwd=str(ROOT), capture_output=True, text=True, timeout=timeout)
    if proc.returncode != 0:
        return {"scenario_id": scenario_id, "status": "error", "reason": (proc.stderr or "")[-3000:]}
    if out_json.exists():
        return json.loads(out_json.read_text(encoding="utf-8"))
    return {"scenario_id": scenario_id, "status": "error", "reason": "missing json"}


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--jobs", type=int, default=4, help="parallel A/B pairs (capped at 4; each runs 2 sims)")
    p.add_argument("--sim-duration", type=float, default=3600.0)
    p.add_argument("--seed", type=int, default=42)
    p.add_argument("--finalists", default=",".join(DEFAULT_FINALISTS))
    p.add_argument("--finalists-json", type=Path, default=ROOT / ".temp" / "screen" / "finalists_for_overnight.json")
    p.add_argument("--out-dir", type=Path, default=ROOT / ".temp" / "finalists")
    args = p.parse_args()
    args.jobs = clamp_docker_jobs(args.jobs)

    if args.finalists_json.exists():
        ids = json.loads(args.finalists_json.read_text(encoding="utf-8")).get("finalist_ids", [])
    else:
        ids = [x.strip() for x in args.finalists.split(",") if x.strip()]

    args.out_dir.mkdir(parents=True, exist_ok=True)
    print(f"Finalists A/B: {ids}, jobs={args.jobs}, sim={args.sim_duration}")

    rows: list[dict] = []
    with ThreadPoolExecutor(max_workers=args.jobs) as pool:
        futs = {
            pool.submit(_run_ab, sid, sim_duration=args.sim_duration, seed=args.seed, out_dir=args.out_dir): sid
            for sid in ids
        }
        for fut in as_completed(futs):
            sid = futs[fut]
            try:
                row = fut.result()
            except Exception as exc:  # noqa: BLE001
                row = {"scenario_id": sid, "status": "error", "reason": repr(exc)}
            rows.append(row)
            pc = row.get("policy_comparison") or {}
            print(
                f"  [{row.get('actual', {}).get('status')}/{row.get('predicted', {}).get('status')}] {sid} "
                f"net_improved={pc.get('policy_net_improved')}",
                flush=True,
            )

    summary_path = args.out_dir / "summary.json"
    summary_path.write_text(json.dumps({"finalists": ids, "runs": rows}, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"Wrote {summary_path}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
