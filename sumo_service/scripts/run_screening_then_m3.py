"""
Sequential experiment driver: Docker cleanup → screening sweep → Module 3 long validation.

Never runs both phases in parallel. Each phase uses at most DOCKER_MAX_JOBS (default 4).

Usage (repo root):
  $env:PREDICTION_API_KEY = "..."
  python sumo_service/scripts/run_screening_then_m3.py --jobs 4
"""
from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SUMO_ROOT = ROOT / "sumo_service"
sys.path.insert(0, str(SUMO_ROOT))

from scripts.docker_run_helpers import (  # noqa: E402
    clamp_docker_jobs,
    cleanup_sumo_run_containers,
    estimate_prediction_calls_per_run,
)


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--jobs", type=int, default=4, help=f"max parallel Docker per phase (cap {4})")
    p.add_argument("--skip-cleanup", action="store_true")
    p.add_argument("--screen-only", action="store_true")
    p.add_argument("--m3-only", action="store_true")
    p.add_argument("--screen-duration", type=float, default=1000.0)
    p.add_argument("--m3-duration", type=float, default=43200.0)
    args = p.parse_args()
    jobs = clamp_docker_jobs(args.jobs)
    pred_screen = estimate_prediction_calls_per_run(args.screen_duration) * jobs
    pred_m3 = estimate_prediction_calls_per_run(args.m3_duration) * jobs
    print(
        f"Plan: jobs={jobs} (max {4}). "
        f"Est. peak /predict: ~{pred_screen} during screening, ~{pred_m3} during M3 "
        f"(15min wall interval; Render usually fine at ≤4 concurrent).",
        flush=True,
    )

    if not args.skip_cleanup:
        cleanup_sumo_run_containers()

    py = sys.executable
    if not args.m3_only:
        print("=== Phase 1: screening sweep ===", flush=True)
        rc = subprocess.call(
            [
                py,
                str(SUMO_ROOT / "scripts" / "run_screening_parallel.py"),
                "--jobs",
                str(jobs),
                "--sim-duration",
                str(args.screen_duration),
            ],
            cwd=str(ROOT),
        )
        if rc != 0:
            return rc

    if args.screen_only:
        return 0

    print("=== Phase 2: Module 3 long validation ===", flush=True)
    return subprocess.call(
        [
            py,
            str(SUMO_ROOT / "scripts" / "run_module3_validation_parallel.py"),
            "--jobs",
            str(jobs),
            "--sim-duration",
            str(args.m3_duration),
        ],
        cwd=str(ROOT),
    )


if __name__ == "__main__":
    raise SystemExit(main())
