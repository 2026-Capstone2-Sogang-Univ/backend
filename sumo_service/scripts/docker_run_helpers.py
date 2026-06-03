"""Shared Docker experiment helpers — job cap, CLI args, cleanup."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path

from app.experiment_pacing import experiment_fast_enabled
from scripts.screening_scenarios import Scenario

ROOT = Path(__file__).resolve().parents[2]
SUMO_ROOT = ROOT / "sumo_service"

# Host RAM + Module 3 API: cap concurrent `docker compose run` (override via DOCKER_MAX_JOBS).
MAX_DOCKER_JOBS = 6


def clamp_docker_jobs(requested: int | None = None) -> int:
    """Clamp parallel Docker workers to [1, MAX_DOCKER_JOBS] (env DOCKER_MAX_JOBS)."""
    if requested is None:
        requested = int(os.getenv("DOCKER_MAX_JOBS", str(MAX_DOCKER_JOBS)))
    return max(1, min(int(requested), MAX_DOCKER_JOBS))


def experiment_cli_flags() -> list[str]:
    """Extra flags for batch Docker runs (bench speed by default)."""
    if experiment_fast_enabled():
        return ["--fast"]
    return []


def scenario_to_cli_args(s: Scenario) -> list[str]:
    """Space-separated argv tokens for run_screening_one (use = form to avoid bash splits)."""
    band = ""
    if s.band_incentive_usd:
        band = ",".join(str(x) for x in s.band_incentive_usd)
    args = [
        f"--scenario-id={s.scenario_id}",
        f"--case={s.case}",
        f"--passenger-lambda={s.passenger_lambda}",
        f"--n-taxis={s.n_taxis}",
        f"--policy-mode={s.policy_mode}",
        f"--alpha-sensitivity={s.alpha_sensitivity}",
        f"--elasticity={s.elasticity}",
        f"--band-incentive-usd={band}",
    ]
    if s.dispatch_max_candidates is not None:
        args.append(f"--dispatch-max-candidates={s.dispatch_max_candidates}")
    if s.surge_max is not None:
        args.append(f"--surge-max={s.surge_max}")
    return args


def scenario_to_policy_ab_args(s: Scenario) -> list[str]:
    """CLI args for run_policy_ab_test (no scenario-id / case)."""
    return [a for a in scenario_to_cli_args(s) if not a.startswith(("--scenario-id=", "--case="))]


def docker_experiment_env(*, demand_source: str) -> list[str]:
    """Docker -e flags for a single experiment run."""
    speed = os.getenv("SIMULATION_SPEED", "").strip() or "20"
    policy_iv = os.getenv("POLICY_UPDATE_INTERVAL_REAL_S", "").strip() or "900"
    sim_policy = os.getenv("POLICY_UPDATE_INTERVAL_SIM_S", "").strip() or "900"
    out = [
        "-e",
        f"DEMAND_SOURCE={demand_source}",
        "-e",
        f"SIMULATION_SPEED={speed}",
        "-e",
        f"POLICY_UPDATE_INTERVAL_REAL_S={policy_iv}",
        "-e",
        f"POLICY_UPDATE_INTERVAL_SIM_S={sim_policy}",
    ]
    if demand_source == "predicted":
        key = os.getenv("PREDICTION_API_KEY", "").strip()
        if not key:
            raise SystemExit("PREDICTION_API_KEY must be set for predicted demand runs")
        url = os.getenv("PREDICTION_URL", "").strip() or "https://module3-ml.onrender.com/predict"
        out.extend(["-e", f"PREDICTION_API_KEY={key}", "-e", f"PREDICTION_URL={url}"])
    bg_cars = os.getenv("N_BACKGROUND_CARS", "").strip()
    if not bg_cars and experiment_fast_enabled():
        bg_cars = "200"
    if bg_cars:
        out.extend(["-e", f"N_BACKGROUND_CARS={bg_cars}"])
    if experiment_fast_enabled():
        out.extend(
            [
                "-e",
                f"BENCH_STEP_LENGTH={os.getenv('BENCH_STEP_LENGTH', '2')}",
                "-e",
                f"BENCH_MAX_FIND_ROUTE_PER_STEP={os.getenv('BENCH_MAX_FIND_ROUTE_PER_STEP', '600')}",
                "-e",
                f"SURGE_RECOMPUTE_INTERVAL_S={os.getenv('SURGE_RECOMPUTE_INTERVAL_S', '15')}",
                "-e",
                f"DISPATCH_BACKLOG_WAIT_THRESHOLD={os.getenv('DISPATCH_BACKLOG_WAIT_THRESHOLD', '60')}",
                "-e",
                f"DISPATCH_MAX_EMPTY_PER_STEP_FAST={os.getenv('DISPATCH_MAX_EMPTY_PER_STEP_FAST', '80')}",
                "-e",
                f"TAXI_DISPATCH_COOLDOWN_S={os.getenv('TAXI_DISPATCH_COOLDOWN_S', '5')}",
                "-e",
                f"PAIR_DISPATCH_COOLDOWN_S={os.getenv('PAIR_DISPATCH_COOLDOWN_S', '60')}",
                "-e",
                f"DISPATCH_H3_K_RING={os.getenv('DISPATCH_H3_K_RING', '1')}",
                "-e",
                f"PICKUP_MAX_EUCLIDEAN_MILES={os.getenv('PICKUP_MAX_EUCLIDEAN_MILES', '2.14')}",
                "-e",
                f"PASSENGER_WAIT_ABANDON_S={os.getenv('PASSENGER_WAIT_ABANDON_S', '900')}",
            ]
        )
    progress_iv = os.getenv("SIM_PROGRESS_LOG_INTERVAL_S", "").strip() or "500"
    out.extend(["-e", f"SIM_PROGRESS_LOG_INTERVAL_S={progress_iv}"])
    out.extend(
        [
            "-e",
            f"PREDICTION_TIMEOUT_S={os.getenv('PREDICTION_TIMEOUT_S', '30')}",
            "-e",
            f"PREDICTION_RETRY_MAX={os.getenv('PREDICTION_RETRY_MAX', '4')}",
            "-e",
            f"PREDICTION_RETRY_BACKOFF_S={os.getenv('PREDICTION_RETRY_BACKOFF_S', '2')}",
            "-e",
            f"PREDICTION_WARMUP={os.getenv('PREDICTION_WARMUP', '1')}",
        ]
    )
    return out


def docker_prediction_env(*, demand_source: str | None = None) -> list[str]:
    demand = demand_source or os.getenv("DEMAND_SOURCE", "").strip() or "predicted"
    return docker_experiment_env(demand_source=demand)


# Wall clock per `docker compose run` container (not whole pipeline).
PER_RUN_MAX_WALL_S = int(os.getenv("EXPERIMENT_PER_RUN_MAX_WALL_S", str(2 * 60 * 60)))
_container_start_wall: dict[str, float] = {}


def sumo_run_container_ids() -> list[str]:
    proc = subprocess.run(
        ["docker", "ps", "-q", "--filter", "name=backend-sumo-service-run"],
        capture_output=True,
        text=True,
        check=False,
    )
    return [x.strip() for x in proc.stdout.splitlines() if x.strip()]


def enforce_per_run_container_timeouts(
    *,
    max_wall_s: int | None = None,
    log_fn: Callable[[str], None] | None = None,
) -> list[str]:
    """Stop sumo run containers running longer than max_wall_s since first seen."""
    limit = PER_RUN_MAX_WALL_S if max_wall_s is None else max_wall_s
    emit = log_fn or (lambda msg: print(msg, flush=True))
    now = time.time()
    active = set(sumo_run_container_ids())
    stopped: list[str] = []
    for cid in list(_container_start_wall):
        if cid not in active:
            del _container_start_wall[cid]
    for cid in active:
        if cid not in _container_start_wall:
            _container_start_wall[cid] = now
            continue
        elapsed = now - _container_start_wall[cid]
        if elapsed > limit:
            emit(f"PER-RUN TIMEOUT {cid[:12]}: {elapsed:.0f}s > {limit}s — docker stop")
            subprocess.run(["docker", "stop", "-t", "15", cid], check=False)
            stopped.append(cid)
            del _container_start_wall[cid]
    return stopped


def reset_container_wall_clock() -> None:
    """Clear per-container timers (e.g. between overnight tasks)."""
    _container_start_wall.clear()


PIPELINE_ABORT_PATH = ROOT / ".temp" / "overnight" / "pipeline_aborted.json"


def pipeline_abort_reason() -> str | None:
    if not PIPELINE_ABORT_PATH.exists():
        return None
    try:
        row = json.loads(PIPELINE_ABORT_PATH.read_text(encoding="utf-8"))
        return str(row.get("reason") or "aborted")
    except (json.JSONDecodeError, OSError):
        return "aborted"


def mark_pipeline_aborted(reason: str) -> None:
    PIPELINE_ABORT_PATH.parent.mkdir(parents=True, exist_ok=True)
    PIPELINE_ABORT_PATH.write_text(
        json.dumps(
            {
                "reason": reason,
                "ts": datetime.now(timezone.utc).isoformat(),
            },
            indent=2,
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )


def clear_pipeline_abort() -> None:
    if PIPELINE_ABORT_PATH.exists():
        PIPELINE_ABORT_PATH.unlink()


def stop_experiment_processes() -> None:
    """Stop all sumo run containers and experiment orchestrator Python processes."""
    cleanup_sumo_run_containers()
    if sys.platform == "win32":
        subprocess.run(
            [
                "powershell",
                "-NoProfile",
                "-Command",
                "Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" | "
                "Where-Object { $_.CommandLine -match "
                "'run_three_arm_parallel|run_overnight_monitored' } | "
                "ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }",
            ],
            capture_output=True,
            check=False,
        )
    else:
        subprocess.run(["pkill", "-f", "run_three_arm_parallel"], check=False)
        subprocess.run(["pkill", "-f", "run_overnight_monitored"], check=False)


def abort_pipeline_on_per_run_timeout(
    *,
    timed_out_container_ids: list[str],
    log_fn: Callable[[str], None] | None = None,
) -> None:
    """One run exceeded wall limit → cancel all remaining work."""
    emit = log_fn or (lambda msg: print(msg, flush=True))
    reason = (
        f"per-run wall {PER_RUN_MAX_WALL_S}s exceeded "
        f"(container(s): {', '.join(c[:12] for c in timed_out_container_ids)})"
    )
    mark_pipeline_aborted(reason)
    emit(f"PIPELINE ABORT: {reason} — no further runs")
    stop_experiment_processes()


def cleanup_sumo_run_containers(*, dry_run: bool = False) -> int:
    """Stop/remove orphaned `docker compose run` sumo-service containers."""
    list_proc = subprocess.run(
        ["docker", "ps", "-aq", "--filter", "name=backend-sumo-service-run"],
        capture_output=True,
        text=True,
        check=False,
    )
    ids = [x.strip() for x in list_proc.stdout.splitlines() if x.strip()]
    if not ids:
        print("No backend-sumo-service-run containers to remove.", flush=True)
        return 0
    print(f"{'Would remove' if dry_run else 'Removing'} {len(ids)} sumo run container(s)...", flush=True)
    if dry_run:
        return len(ids)
    rm = subprocess.run(["docker", "rm", "-f", *ids], capture_output=True, text=True, check=False)
    if rm.returncode != 0:
        print((rm.stderr or rm.stdout or "")[-2000:], flush=True)
        return 1
    print(f"Removed {len(ids)} container(s).", flush=True)
    return 0


def estimate_prediction_calls_per_run(sim_duration_s: float, *, speed: float | None = None) -> int:
    """Rough HTTP /predict count (policy wall interval 900s, Lab pacing ~speed sim-s per wall-s)."""
    speed = speed or float(os.getenv("SIMULATION_SPEED", "20"))
    policy_iv = float(os.getenv("POLICY_UPDATE_INTERVAL_REAL_S", "900"))
    wall_s = sim_duration_s / max(speed, 1.0)
    return max(1, int(wall_s // policy_iv) + 1)
