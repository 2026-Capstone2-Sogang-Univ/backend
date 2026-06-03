"""
Three-arm parallel experiment (shared worker pool):

  1. actual      — no AI (demand_source=actual, surge from waiting passengers)
  2. ai_forecast — predicted run → Module 3 horizon KPIs (same sim as policy)
  3. ai_policy   — predicted run → Module 4 policy KPIs

Predicted arm runs once per scenario; results are indexed under both ai_forecast and ai_policy.

Usage (repo root):
  $env:PREDICTION_API_KEY = "..."
  $env:EXPERIMENT_FAST = "1"
  $env:DOCKER_MAX_JOBS = "6"
  python sumo_service/scripts/run_three_arm_parallel.py --jobs 6 --sim-duration 1000
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SUMO_ROOT = ROOT / "sumo_service"
sys.path.insert(0, str(SUMO_ROOT))

from app.policy_comparison import compare_policy_ab  # noqa: E402
from scripts.docker_run_helpers import (  # noqa: E402
    clamp_docker_jobs,
    cleanup_sumo_run_containers,
    docker_experiment_env,
    experiment_cli_flags,
    scenario_to_cli_args,
)
from scripts.screening_scenarios import Scenario, build_screen_scenarios  # noqa: E402

DEFAULT_FINALISTS = (
    "fair_dispatch10",
    "fair_ratio35",
    "fair_ratio40",
    "B_stress_55",
    "imb_rebalance_40",
    "imb_combo",
)


@dataclass(frozen=True)
class Task:
    arm: str  # actual | predicted
    scenario: Scenario
    out_subdir: str  # actual | ai_forecast | ai_policy
    demand_source: str


def _scenario_by_id(scenario_id: str) -> Scenario:
    for s in build_screen_scenarios():
        if s.scenario_id == scenario_id:
            return s
    raise KeyError(scenario_id)


def _build_tasks(
    finalist_ids: tuple[str, ...],
    *,
    predicted_only: bool = False,
    arms: str = "all",
) -> list[Task]:
    tasks: list[Task] = []
    include_actual = arms in ("all", "actual") and not predicted_only
    include_predicted = arms in ("all", "predicted")
    for sid in finalist_ids:
        s = _scenario_by_id(sid)
        if include_actual:
            tasks.append(Task("actual", s, "actual", "actual"))
        if include_predicted:
            tasks.append(Task("predicted", s, "ai_policy", "predicted"))
    return tasks


def _task_output_json(task: Task, out_root: Path) -> Path:
    return out_root / task.out_subdir / f"{task.scenario.scenario_id}.json"


def _task_already_ok(task: Task, out_root: Path) -> bool:
    path = _task_output_json(task, out_root)
    if not path.exists():
        return False
    try:
        row = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return False
    return row.get("status") == "ok"


def _docker_run(task: Task, *, sim_duration: float, seed: int, out_root: Path) -> dict:
    out_dir = out_root / task.out_subdir
    out_dir.mkdir(parents=True, exist_ok=True)
    out_json = out_dir / f"{task.scenario.scenario_id}.json"
    inner = " ".join(
        [
            "uv pip install --python /app/.venv/bin/python httpx -q &&",
            "/app/.venv/bin/python /app/scripts/run_screening_one.py",
            f"--sim-duration {sim_duration}",
            f"--seed {seed}",
            f"--json-output /temp/out/{task.scenario.scenario_id}.json",
            f"--demand-source={task.demand_source}",
            *scenario_to_cli_args(task.scenario),
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
        *docker_experiment_env(demand_source=task.demand_source),
        "-v",
        f"{SUMO_ROOT / 'scripts'}:/app/scripts",
        "-v",
        f"{SUMO_ROOT / 'app'}:/app/app",
        "-v",
        f"{out_dir}:/temp/out",
        "sumo-service",
        "bash",
        "-c",
        inner,
    ]
    proc = subprocess.run(cmd, cwd=str(ROOT), capture_output=True, text=True, timeout=timeout_s)
    if proc.returncode != 0:
        return {
            "arm": task.arm,
            "out_subdir": task.out_subdir,
            "scenario_id": task.scenario.scenario_id,
            "status": "error",
            "reason": (proc.stderr or proc.stdout or "")[-2500:],
        }
    if not out_json.exists():
        return {
            "arm": task.arm,
            "out_subdir": task.out_subdir,
            "scenario_id": task.scenario.scenario_id,
            "status": "error",
            "reason": "missing output json",
        }
    row = json.loads(out_json.read_text(encoding="utf-8"))
    row["arm"] = task.arm
    row["out_subdir"] = task.out_subdir
    return row


def _m3_slice(row: dict) -> dict:
    m = row.get("metrics") or {}
    return {
        "scenario_id": row.get("scenario_id"),
        "status": row.get("status"),
        "module3_horizon_eval_count": m.get("module3_horizon_eval_count"),
        "module3_horizon_mae_avg": m.get("module3_horizon_mae_avg"),
        "module3_horizon_bias_avg": m.get("module3_horizon_bias_avg"),
        "module3_horizon_rmse_avg": m.get("module3_horizon_rmse_avg"),
        "prediction_success_count": m.get("prediction_success_count"),
        "prediction_request_count": m.get("prediction_request_count"),
    }


def _policy_slice(row: dict) -> dict:
    m = row.get("metrics") or {}
    return {
        "scenario_id": row.get("scenario_id"),
        "status": row.get("status"),
        "matching_success_rate": m.get("matching_success_rate"),
        "passengers_never_offered_rate": m.get("passengers_never_offered_rate"),
        "avg_matching_rate_error": m.get("avg_matching_rate_error"),
        "avg_abs_matching_rate_error": m.get("avg_abs_matching_rate_error"),
        "dispatch_acceptance_rate": m.get("dispatch_acceptance_rate"),
        "surge_clamped_rate": m.get("surge_clamped_rate"),
    }


def write_summary_from_disk(
    *,
    out_root: Path,
    finalist_ids: tuple[str, ...],
    sim_duration: float,
    seed: int,
    jobs: int,
    predicted_only: bool = False,
) -> dict:
    """Build summary.json from per-scenario JSON already on disk."""
    rows: list[dict] = []
    for sid in finalist_ids:
        for arm, subdir in (("actual", "actual"), ("predicted", "ai_policy")):
            if predicted_only and arm == "actual":
                continue
            path = out_root / subdir / f"{sid}.json"
            if not path.exists():
                continue
            row = json.loads(path.read_text(encoding="utf-8"))
            row["arm"] = arm
            row["out_subdir"] = subdir
            row.setdefault("scenario_id", sid)
            rows.append(row)

    ai_forecast_dir = out_root / "ai_forecast"
    ai_forecast_dir.mkdir(parents=True, exist_ok=True)
    for row in rows:
        if row.get("arm") != "predicted" or row.get("status") != "ok":
            continue
        sid = row["scenario_id"]
        src = out_root / "ai_policy" / f"{sid}.json"
        if src.exists():
            (ai_forecast_dir / f"{sid}.json").write_text(
                src.read_text(encoding="utf-8"), encoding="utf-8"
            )

    actual_by_id = {
        r["scenario_id"]: r for r in rows if r.get("arm") == "actual" and r.get("status") == "ok"
    }
    predicted_by_id = {
        r["scenario_id"]: r for r in rows if r.get("arm") == "predicted" and r.get("status") == "ok"
    }
    policy_ab: list[dict] = []
    if not predicted_only:
        for sid in finalist_ids:
            a_row = actual_by_id.get(sid)
            p_row = predicted_by_id.get(sid)
            if not a_row or not p_row or not a_row.get("metrics") or not p_row.get("metrics"):
                policy_ab.append({"scenario_id": sid, "status": "incomplete"})
                continue
            cmp = compare_policy_ab(a_row["metrics"], p_row["metrics"])
            policy_ab.append({"scenario_id": sid, "status": "ok", **cmp})

    by_arm: dict[str, list] = {
        "ai_policy": [_policy_slice(r) for r in rows if r.get("arm") == "predicted"],
        "ai_forecast": [_m3_slice(r) for r in rows if r.get("arm") == "predicted"],
    }
    if not predicted_only:
        by_arm["actual"] = [_policy_slice(r) for r in rows if r.get("arm") == "actual"]
        by_arm["policy_ab"] = policy_ab

    summary = {
        "sim_duration": sim_duration,
        "seed": seed,
        "jobs": jobs,
        "finalists": list(finalist_ids),
        "predicted_only": predicted_only,
        "note": "rebuilt from disk",
        "arms": by_arm,
        "runs": rows,
    }
    (out_root / "summary.json").write_text(
        json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    return summary


def main() -> int:
    p = argparse.ArgumentParser(description="Three-arm finalists experiment (6 parallel default)")
    p.add_argument("--jobs", type=int, default=6)
    p.add_argument("--sim-duration", type=float, default=1000.0)
    p.add_argument("--seed", type=int, default=42)
    p.add_argument("--out-dir", type=Path, default=ROOT / ".temp" / "triple_arm")
    p.add_argument("--finalists", default=",".join(DEFAULT_FINALISTS))
    p.add_argument("--skip-cleanup", action="store_true")
    p.add_argument(
        "--predicted-only",
        action="store_true",
        help="Module 3 long run: same 6 finalists, predicted demand only",
    )
    p.add_argument(
        "--arms",
        choices=("all", "actual", "predicted"),
        default="all",
        help="Run only actual or predicted tasks (default: all)",
    )
    p.add_argument(
        "--skip-ok",
        action="store_true",
        help="Skip scenarios whose output JSON already has status=ok",
    )
    p.add_argument(
        "--summary-only",
        action="store_true",
        help="Only rebuild summary.json from existing outputs (no Docker)",
    )
    args = p.parse_args()
    jobs = clamp_docker_jobs(args.jobs)
    finalist_ids = tuple(x.strip() for x in args.finalists.split(",") if x.strip())

    if args.summary_only:
        write_summary_from_disk(
            out_root=args.out_dir,
            finalist_ids=finalist_ids,
            sim_duration=args.sim_duration,
            seed=args.seed,
            jobs=jobs,
            predicted_only=args.predicted_only,
        )
        print(f"Wrote {args.out_dir / 'summary.json'}", flush=True)
        return 0

    tasks = _build_tasks(
        finalist_ids,
        predicted_only=args.predicted_only,
        arms=args.arms,
    )
    if args.skip_ok:
        tasks = [t for t in tasks if not _task_already_ok(t, args.out_dir)]

    if not args.skip_cleanup:
        cleanup_sumo_run_containers()

    if not tasks:
        print("No tasks to run (all ok or empty filter).", flush=True)
        write_summary_from_disk(
            out_root=args.out_dir,
            finalist_ids=finalist_ids,
            sim_duration=args.sim_duration,
            seed=args.seed,
            jobs=jobs,
            predicted_only=args.predicted_only,
        )
        return 0

    n_actual = sum(1 for t in tasks if t.arm == "actual")
    n_pred = sum(1 for t in tasks if t.arm == "predicted")
    print(
        f"Three-arm run: {len(finalist_ids)} scenarios, "
        f"{n_actual} actual + {n_pred} predicted sims, jobs={jobs}, sim={args.sim_duration}",
        flush=True,
    )

    if n_pred > 0:
        from app.prediction import warm_prediction_service  # noqa: WPS433
        from app.prediction_config import require_prediction_api_key, resolve_prediction_url

        warmed = warm_prediction_service(
            prediction_url=resolve_prediction_url(),
            api_key=require_prediction_api_key(),
        )
        print(
            f"Module3 warmup ({resolve_prediction_url()}): "
            f"{'ok' if warmed else 'failed — per-request retries will still run'}",
            flush=True,
        )

    rows: list[dict] = []
    with ThreadPoolExecutor(max_workers=jobs) as pool:
        futs = {
            pool.submit(_docker_run, t, sim_duration=args.sim_duration, seed=args.seed, out_root=args.out_dir): t
            for t in tasks
        }
        for fut in as_completed(futs):
            t = futs[fut]
            try:
                row = fut.result()
            except Exception as exc:  # noqa: BLE001
                row = {
                    "arm": t.arm,
                    "scenario_id": t.scenario.scenario_id,
                    "status": "error",
                    "reason": repr(exc),
                }
            rows.append(row)
            m = row.get("metrics") or {}
            if t.arm == "actual":
                print(
                    f"  [actual/{row.get('status')}] {t.scenario.scenario_id} "
                    f"match={m.get('matching_success_rate')}",
                    flush=True,
                )
            else:
                print(
                    f"  [predicted/{row.get('status')}] {t.scenario.scenario_id} "
                    f"match={m.get('matching_success_rate')} "
                    f"m3_mae={m.get('module3_horizon_mae_avg')}",
                    flush=True,
                )

    # Merge this batch into summary (includes prior ok JSON on disk).
    existing_rows: list[dict] = []
    for t in _build_tasks(finalist_ids, predicted_only=args.predicted_only, arms=args.arms):
        path = _task_output_json(t, args.out_dir)
        if path.exists():
            try:
                row = json.loads(path.read_text(encoding="utf-8"))
                row["arm"] = t.arm
                row["out_subdir"] = t.out_subdir
                row.setdefault("scenario_id", t.scenario.scenario_id)
                existing_rows.append(row)
            except json.JSONDecodeError:
                pass
    # Prefer fresh rows from this invocation when same scenario+arm.
    merged: dict[tuple[str, str], dict] = {}
    for row in existing_rows:
        merged[(row.get("scenario_id", ""), row.get("arm", ""))] = row
    for row in rows:
        merged[(row.get("scenario_id", ""), row.get("arm", ""))] = row
    all_rows = list(merged.values())

    write_summary_from_disk(
        out_root=args.out_dir,
        finalist_ids=finalist_ids,
        sim_duration=args.sim_duration,
        seed=args.seed,
        jobs=jobs,
        predicted_only=args.predicted_only,
    )
    # write_summary_from_disk reads disk; ensure new rows are on disk (already written by _docker_run)
    path = args.out_dir / "summary.json"
    print(f"Wrote {path}", flush=True)
    ok = sum(1 for r in all_rows if r.get("status") == "ok")
    expected = len(
        _build_tasks(finalist_ids, predicted_only=args.predicted_only, arms="all")
    )
    return 0 if ok >= expected else 1


if __name__ == "__main__":
    raise SystemExit(main())
