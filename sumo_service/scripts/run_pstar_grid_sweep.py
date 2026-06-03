"""
2×2 P* grid sweep: (fair_ratio35 | B_stress_55) × (surge_max 4.9 | 6.0) × target_p levels.

Fixed: demand_source=predicted, policy_mode=matching, fast bench, seed=42.
Outputs: .temp/pstar_grid/{run_id}.json + summary.json

Example:
  cd backend
  $env:PREDICTION_API_KEY = "..."
  $env:EXPERIMENT_FAST = "1"
  python -u sumo_service/scripts/run_pstar_grid_sweep.py --sim-duration 7200 --jobs 4
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SUMO_ROOT = ROOT / "sumo_service"
sys.path.insert(0, str(SUMO_ROOT))

from scripts.docker_run_helpers import (  # noqa: E402
    cleanup_sumo_run_containers,
    clamp_docker_jobs,
    docker_prediction_env,
    experiment_cli_flags,
)
from scripts.pstar_grid_matrix import (  # noqa: E402
    DEFAULT_PSTAR_BUCKET,
    DEFAULT_PSTAR_LEVELS,
    PstarGridCell,
    build_pstar_grid_cells,
    cell_run_id,
)

DEFAULT_OUT = ROOT / ".temp" / "pstar_grid"

# 6+6 batches: fair cells then stress cells (3 P* each).
BATCH_CELL_PREFIXES: dict[str, tuple[str, ...]] = {
    "1": ("pgrid_fair35_cap49", "pgrid_fair35_cap60"),
    "2": ("pgrid_stress55_cap49", "pgrid_stress55_cap60"),
}


def _parse_float_list(raw: str | None, default: tuple[float, ...]) -> tuple[float, ...]:
    if not raw or not str(raw).strip():
        return default
    return tuple(float(x.strip()) for x in str(raw).split(",") if x.strip())


def _run_cell_p(
    cell: PstarGridCell,
    target_p: float,
    *,
    sim_duration: float,
    seed: int,
    fast: bool,
    demand_source: str,
) -> dict:
    from scripts.run_acceptance_experiment import _run_one  # noqa: WPS433

    run_id = cell_run_id(cell, target_p)
    row = _run_one(
        elasticity=0.6,
        beta_f=None,
        seed=seed,
        sim_duration=sim_duration,
        fast=fast,
        demand_source=demand_source,
        alpha_sensitivity=1.5,
        policy_mode="matching",
        n_taxis=300,
        passenger_lambda=cell.passenger_lambda,
        dispatch_max_candidates=cell.dispatch_max_candidates,
        surge_max=cell.surge_max,
        target_p=target_p,
        target_p_bucket=DEFAULT_PSTAR_BUCKET,
    )
    row["run_id"] = run_id
    row["cell_id"] = cell.cell_id
    row["cell_label"] = cell.label
    row["ratio_label"] = cell.ratio_label
    row["surge_max"] = cell.surge_max
    row["target_p"] = target_p
    row["target_p_bucket"] = DEFAULT_PSTAR_BUCKET
    return row


def _docker_run_cell(
    cell: PstarGridCell,
    target_p: float,
    *,
    sim_duration: float,
    seed: int,
    out_dir: Path,
    demand_source: str,
) -> dict:
    run_id = cell_run_id(cell, target_p)
    out_json = out_dir / f"{run_id}.json"
    case = "stress" if cell.cell_id == "stress55" else "fair"
    inner = " ".join(
        [
            "uv pip install --python /app/.venv/bin/python httpx -q &&",
            "/app/.venv/bin/python /app/scripts/run_screening_one.py",
            f"--sim-duration {sim_duration}",
            f"--seed {seed}",
            f"--json-output /temp/pstar/{run_id}.json",
            f"--scenario-id={run_id}",
            f"--case={case}",
            f"--passenger-lambda={cell.passenger_lambda}",
            f"--n-taxis=300",
            f"--policy-mode=matching",
            f"--alpha-sensitivity=1.5",
            f"--elasticity=0.6",
            f"--surge-max={cell.surge_max}",
            f"--target-p={target_p}",
            f"--target-p-bucket={DEFAULT_PSTAR_BUCKET}",
            f"--demand-source={demand_source}",
            *experiment_cli_flags(),
        ]
    )
    fast = os.getenv("EXPERIMENT_FAST", "1").strip().lower() not in ("0", "false", "no")
    speed = float(os.getenv("SIMULATION_SPEED", "20"))
    timeout_s = (int(sim_duration * 3) + 600) if fast else (int(sim_duration / max(speed, 1)) + 600)
    cmd = [
        "docker",
        "compose",
        "run",
        "--rm",
        "--no-deps",
        *docker_prediction_env(demand_source=demand_source),
        "-v",
        f"{SUMO_ROOT / 'scripts'}:/app/scripts",
        "-v",
        f"{SUMO_ROOT / 'app'}:/app/app",
        "-v",
        f"{out_dir}:/temp/pstar",
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
            "status": "error",
            "reason": (proc.stderr or proc.stdout or "")[-2000:],
            "run_id": run_id,
            "metrics": None,
        }
    if out_json.exists():
        return json.loads(out_json.read_text(encoding="utf-8"))
    return {
        "status": "error",
        "reason": "missing output json",
        "run_id": run_id,
        "metrics": None,
    }


def _resolve_cell_prefixes(cells_arg: str, batch: str | None) -> str:
    if batch:
        b = batch.strip().lower()
        if b == "all":
            return "all"
        prefixes = BATCH_CELL_PREFIXES.get(b)
        if not prefixes:
            raise SystemExit(f"--batch must be 1, 2, or all (got {batch!r})")
        return ",".join(prefixes)
    return cells_arg


def main() -> int:
    p = argparse.ArgumentParser(description="2×2 P* grid sweep (predicted demand)")
    p.add_argument("--sim-duration", type=float, default=7200.0)
    p.add_argument("--seed", type=int, default=42)
    p.add_argument("--jobs", type=int, default=None)
    p.add_argument("--out-dir", type=Path, default=DEFAULT_OUT)
    p.add_argument(
        "--target-p-list",
        default=",".join(str(x) for x in DEFAULT_PSTAR_LEVELS),
        help="Comma-separated P* values for raw_gte_3_5 (default 0.80,0.85,0.90)",
    )
    p.add_argument(
        "--cells",
        default="all",
        help="all | pgrid_fair35_cap49,... (subset of 2×2 cells)",
    )
    p.add_argument(
        "--batch",
        choices=("1", "2", "all"),
        default=None,
        help="1=fair 6 runs, 2=stress 6 runs (overrides --cells unless --cells set explicitly)",
    )
    p.add_argument(
        "--docker",
        action="store_true",
        help="Run inside docker compose (required on hosts without SUMO)",
    )
    p.add_argument(
        "--skip-cleanup",
        action="store_true",
        help="Do not stop existing backend-sumo-service-run containers before start",
    )
    p.add_argument("--demand-source", choices=("predicted", "actual"), default="predicted")
    p.add_argument("--skip-ok", action="store_true", help="Skip runs whose JSON already has status=ok")
    p.add_argument(
        "--max-pending",
        type=int,
        default=None,
        help="Run at most N pending tasks this invocation (stable run_id order)",
    )
    p.add_argument(
        "--run-ids",
        default="",
        help="Comma-separated run_id allowlist (e.g. pgrid_stress55_cap49_p80)",
    )
    p.add_argument("--dry-run", action="store_true")
    args = p.parse_args()

    jobs = clamp_docker_jobs(args.jobs)
    target_ps = _parse_float_list(args.target_p_list, DEFAULT_PSTAR_LEVELS)
    cells_filter = _resolve_cell_prefixes(args.cells, args.batch)
    cells = list(build_pstar_grid_cells())
    if cells_filter.strip().lower() != "all":
        allowed = {x.strip() for x in cells_filter.split(",") if x.strip()}
        cells = [c for c in cells if c.scenario_id_prefix in allowed]

    tasks: list[tuple[PstarGridCell, float]] = []
    for cell in cells:
        for tp in target_ps:
            run_id = cell_run_id(cell, tp)
            out_path = args.out_dir / f"{run_id}.json"
            if args.skip_ok and out_path.exists():
                try:
                    if json.loads(out_path.read_text(encoding="utf-8")).get("status") == "ok":
                        continue
                except json.JSONDecodeError:
                    pass
            tasks.append((cell, tp))

    if args.run_ids.strip():
        allowed_runs = {x.strip() for x in args.run_ids.split(",") if x.strip()}
        tasks = [(c, tp) for c, tp in tasks if cell_run_id(c, tp) in allowed_runs]

    tasks.sort(key=lambda item: cell_run_id(item[0], item[1]))
    if args.max_pending is not None and args.max_pending > 0:
        tasks = tasks[: args.max_pending]

    use_docker = bool(args.docker)

    print(
        f"P* grid: {len(cells)} cells × {len(target_ps)} P* = {len(cells) * len(target_ps)} runs, "
        f"pending={len(tasks)}, sim={args.sim_duration}s, jobs={jobs}, "
        f"demand={args.demand_source}, docker={use_docker}, batch={args.batch or 'n/a'}",
        flush=True,
    )
    for cell in cells:
        print(
            f"  - {cell.scenario_id_prefix}: lambda={cell.passenger_lambda} "
            f"surge_max={cell.surge_max}",
            flush=True,
        )

    if args.dry_run:
        for cell, tp in tasks:
            print(f"  [dry-run] {cell_run_id(cell, tp)}", flush=True)
        return 0

    args.out_dir.mkdir(parents=True, exist_ok=True)
    if not args.skip_cleanup:
        cleanup_sumo_run_containers()
    elif use_docker:
        print("skip-cleanup: leaving existing sumo run containers running", flush=True)

    rows: list[dict] = []
    failed = 0

    def _execute(item: tuple[PstarGridCell, float]) -> dict:
        cell, tp = item
        if use_docker:
            return _docker_run_cell(
                cell,
                tp,
                sim_duration=args.sim_duration,
                seed=args.seed,
                out_dir=args.out_dir,
                demand_source=args.demand_source,
            )
        return _run_cell_p(
            cell,
            tp,
            sim_duration=args.sim_duration,
            seed=args.seed,
            fast=True,
            demand_source=args.demand_source,
        )

    with ThreadPoolExecutor(max_workers=jobs) as pool:
        futs = {pool.submit(_execute, t): t for t in tasks}
        for fut in as_completed(futs):
            cell, tp = futs[fut]
            rid = cell_run_id(cell, tp)
            try:
                row = fut.result()
            except Exception as exc:  # noqa: BLE001
                row = {
                    "status": "error",
                    "reason": repr(exc),
                    "run_id": rid,
                    "metrics": None,
                }
            out_path = args.out_dir / f"{rid}.json"
            out_path.write_text(json.dumps(row, indent=2, ensure_ascii=False), encoding="utf-8")
            m = row.get("metrics") or {}
            print(
                f"  [{row.get('status')}] {rid} match={m.get('matching_success_rate')} "
                f"rev={m.get('driver_revenue_per_hour_usd')} "
                f"band_err={m.get('band_raw_gte_3_5_p_error')}",
                flush=True,
            )
            rows.append(row)
            if row.get("status") != "ok":
                failed += 1

    # Merge with existing ok JSON on disk for summary
    merged: list[dict] = []
    for path in sorted(args.out_dir.glob("pgrid_*.json")):
        try:
            merged.append(json.loads(path.read_text(encoding="utf-8")))
        except json.JSONDecodeError:
            pass

    summary = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "sim_duration": args.sim_duration,
        "seed": args.seed,
        "demand_source": args.demand_source,
        "target_p_bucket": DEFAULT_PSTAR_BUCKET,
        "target_p_list": list(target_ps),
        "cells": [c.scenario_id_prefix for c in cells],
        "ok": sum(1 for r in merged if r.get("status") == "ok"),
        "total": len(merged),
        "runs": merged,
    }
    (args.out_dir / "summary.json").write_text(
        json.dumps(summary, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    print(f"Wrote {args.out_dir / 'summary.json'} ({summary['ok']}/{summary['total']} ok)", flush=True)
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
