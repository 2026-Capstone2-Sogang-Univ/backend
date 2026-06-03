"""
Overnight pipeline: 6 finalists × 14400 (actual + predicted) → 43200 M3.

Per (scenario, arm) at most MAX_ATTEMPTS_PER_TASK tries (default 3). Stops the whole
pipeline on first exhausted task so the user can resume manually (--resume).

Monitors docker [progress] logs; on stall counts as a failed attempt for in-flight tasks.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SUMO_ROOT = ROOT / "sumo_service"
sys.path.insert(0, str(SUMO_ROOT))

from scripts.docker_run_helpers import (  # noqa: E402
    PER_RUN_MAX_WALL_S,
    abort_pipeline_on_per_run_timeout,
    cleanup_sumo_run_containers,
    clear_pipeline_abort,
    enforce_per_run_container_timeouts,
    pipeline_abort_reason,
    reset_container_wall_clock,
)

DEFAULT_FINALISTS = (
    "fair_dispatch10",
    "fair_ratio35",
    "fair_ratio40",
    "B_stress_55",
    "imb_rebalance_40",
    "imb_combo",
)

OUT_14K = ROOT / ".temp" / "triple_arm_14k"
OUT_43K = ROOT / ".temp" / "triple_arm_43k"
MONITOR_DIR = ROOT / ".temp" / "overnight"
LOG_PATH = MONITOR_DIR / "monitor_run.log"
STATE_PATH = MONITOR_DIR / "state.json"
STATUS_PATH = MONITOR_DIR / "pipeline_status.json"

SIM_14K = 14400.0
SIM_43K = 43200.0
POLL_S = int(os.getenv("OVERNIGHT_POLL_S", "600"))
STALL_WALL_S = int(os.getenv("OVERNIGHT_STALL_WALL_S", str(45 * 60)))
MAX_ATTEMPTS_PER_TASK = int(os.getenv("OVERNIGHT_MAX_ATTEMPTS_PER_TASK", "3"))

_PROGRESS_RE = re.compile(r"\[progress\].*sim_time=(\d+(?:\.\d+)?)/(\d+(?:\.\d+)?)")


@dataclass(frozen=True)
class TaskSpec:
    scenario_id: str
    arm: str  # actual | predicted
    out_dir: Path
    sim_duration: float
    jobs: int
    predicted_only: bool
    label: str


def _ts() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def log(msg: str) -> None:
    line = f"[{_ts()}] {msg}"
    print(line, flush=True)
    MONITOR_DIR.mkdir(parents=True, exist_ok=True)
    try:
        with LOG_PATH.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass


def _env() -> dict[str, str]:
    env = os.environ.copy()
    env.setdefault("EXPERIMENT_FAST", "1")
    env.setdefault("DOCKER_MAX_JOBS", "2")
    env.setdefault("N_BACKGROUND_CARS", "200")
    env.setdefault("BENCH_STEP_LENGTH", "2")
    env.setdefault("BENCH_MAX_FIND_ROUTE_PER_STEP", "600")
    env.setdefault("SURGE_RECOMPUTE_INTERVAL_S", "15")
    env.setdefault("DISPATCH_BACKLOG_WAIT_THRESHOLD", "60")
    env.setdefault("DISPATCH_MAX_EMPTY_PER_STEP_FAST", "80")
    env.setdefault("SIM_PROGRESS_LOG_INTERVAL_S", "500")
    env.setdefault("PREDICTION_TIMEOUT_S", "45")
    env.setdefault("PREDICTION_RETRY_MAX", "5")
    env.setdefault("PREDICTION_WARMUP", "1")
    if not env.get("PREDICTION_API_KEY", "").strip():
        log("WARNING: PREDICTION_API_KEY not set — predicted runs will fail")
    return env


def _task_key(scenario_id: str, arm: str) -> str:
    return f"{scenario_id}:{arm}"


def _json_path(task: TaskSpec) -> Path:
    sub = "actual" if task.arm == "actual" else "ai_policy"
    return task.out_dir / sub / f"{task.scenario_id}.json"


def _task_is_ok(task: TaskSpec) -> bool:
    path = _json_path(task)
    if not path.exists():
        return False
    try:
        return json.loads(path.read_text(encoding="utf-8")).get("status") == "ok"
    except json.JSONDecodeError:
        return False


def _load_state() -> dict:
    if STATE_PATH.exists():
        try:
            return json.loads(STATE_PATH.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            pass
    return {
        "attempts": {},
        "stopped": False,
        "stop_reason": "",
        "last_max_sim": 0.0,
        "last_change_wall": time.time(),
    }


def _save_state(state: dict) -> None:
    STATE_PATH.write_text(json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8")


def _write_stopped(state: dict, reason: str) -> None:
    state["stopped"] = True
    state["stop_reason"] = reason
    _save_state(state)
    payload = {
        "stopped": True,
        "reason": reason,
        "attempts": state.get("attempts", {}),
        "resume": "python sumo_service/scripts/run_overnight_monitored.py --resume",
        "ts": _ts(),
    }
    STATUS_PATH.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    log(f"PIPELINE STOPPED: {reason}")
    log(f"Resume with: {payload['resume']}")


def _docker_ids() -> list[str]:
    proc = subprocess.run(
        ["docker", "ps", "-q", "--filter", "name=backend-sumo-service-run"],
        capture_output=True,
        text=True,
        check=False,
    )
    return [x.strip() for x in proc.stdout.splitlines() if x.strip()]


def _container_progress() -> dict[str, dict]:
    out: dict[str, dict] = {}
    for cid in _docker_ids():
        logs = subprocess.run(
            ["docker", "logs", cid],
            capture_output=True,
            text=True,
            check=False,
        ).stdout
        sim_time = 0.0
        target = 0.0
        for line in logs.splitlines():
            m = _PROGRESS_RE.search(line)
            if m:
                sim_time = max(sim_time, float(m.group(1)))
                target = float(m.group(2))
        if sim_time == 0.0:
            for line in logs.splitlines():
                if "sim_time=" in line:
                    parts = re.findall(r"sim_time=(\d+)", line)
                    if parts:
                        sim_time = max(sim_time, float(parts[-1]))
        top = subprocess.run(
            ["docker", "top", cid],
            capture_output=True,
            text=True,
            check=False,
        ).stdout
        sid = "?"
        arm = "?"
        if m2 := re.search(r"scenario-id=(\S+)", top):
            sid = m2.group(1)
        if m3 := re.search(r"demand-source=(\S+)", top):
            arm = m3.group(1)
        pct = (100.0 * sim_time / target) if target > 0 else 0.0
        out[cid] = {
            "scenario": sid,
            "arm": arm,
            "sim_time": sim_time,
            "target": target,
            "pct": round(pct, 1),
        }
    return out


def _kill_docker_all() -> None:
    cleanup_sumo_run_containers()


def _failure_reason(task: TaskSpec) -> str:
    path = _json_path(task)
    if path.exists():
        try:
            row = json.loads(path.read_text(encoding="utf-8"))
            if row.get("reason"):
                return str(row["reason"])[:300]
        except json.JSONDecodeError:
            pass
    return _analyze_docker_tail()


def _analyze_docker_tail() -> str:
    hints: list[str] = []
    for cid in _docker_ids()[:2]:
        tail = subprocess.run(
            ["docker", "logs", "--tail", "30", cid],
            capture_output=True,
            text=True,
            check=False,
        ).stdout
        if "PredictionFallbackError" in tail:
            hints.append("Module3 API")
        if "Unknown from edge" in tail or "Quitting" in tail:
            hints.append("SUMO crash")
    return hints[0] if hints else "unknown"


def _triple_arm_cmd(task: TaskSpec, *, skip_cleanup: bool) -> list[str]:
    cmd = [
        sys.executable,
        "-u",
        str(SUMO_ROOT / "scripts" / "run_three_arm_parallel.py"),
        "--jobs",
        str(task.jobs),
        "--sim-duration",
        str(task.sim_duration),
        "--out-dir",
        str(task.out_dir),
        "--arms",
        task.arm,
        "--finalists",
        task.scenario_id,
        "--skip-ok",
    ]
    if task.predicted_only:
        cmd.append("--predicted-only")
    if skip_cleanup:
        cmd.append("--skip-cleanup")
    return cmd


def _rebuild_summary(out_dir: Path, *, sim_duration: float, predicted_only: bool, jobs: int) -> None:
    subprocess.run(
        [
            sys.executable,
            "-u",
            str(SUMO_ROOT / "scripts" / "run_three_arm_parallel.py"),
            "--summary-only",
            "--sim-duration",
            str(sim_duration),
            "--out-dir",
            str(out_dir),
            "--jobs",
            str(jobs),
        ]
        + (["--predicted-only"] if predicted_only else []),
        cwd=str(ROOT),
        env=_env(),
        check=False,
    )


def _run_subprocess(cmd: list[str], *, label: str, sim_duration: float) -> int:
    log(f"START {label} (per-run wall limit {PER_RUN_MAX_WALL_S}s)")
    reset_container_wall_clock()
    state = _load_state()
    state["last_max_sim"] = 0.0
    state["last_change_wall"] = time.time()
    _save_state(state)

    proc = subprocess.Popen(
        cmd,
        cwd=str(ROOT),
        env=_env(),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    assert proc.stdout is not None
    last_poll = 0.0

    while True:
        line = proc.stdout.readline()
        if line:
            print(line.rstrip(), flush=True)

        if proc.poll() is not None:
            rest = proc.stdout.read()
            if rest:
                print(rest, end="", flush=True)
            break

        now = time.time()
        if now - last_poll >= POLL_S:
            last_poll = now
            timed_out = enforce_per_run_container_timeouts(log_fn=log)
            if timed_out:
                abort_pipeline_on_per_run_timeout(
                    timed_out_container_ids=timed_out,
                    log_fn=log,
                )
                proc.kill()
                log(f"END {label} exit=-3 (per-run wall — pipeline aborted)")
                return -3
            prog = _container_progress()
            if prog:
                max_sim = max(p["sim_time"] for p in prog.values())
                for p in prog.values():
                    log(
                        f"  {label} {p['scenario']}/{p['arm']}: "
                        f"sim={p['sim_time']:.0f}/{p['target']:.0f} ({p['pct']}%)"
                    )
                st = _load_state()
                if max_sim > st["last_max_sim"] + 1.0:
                    st["last_max_sim"] = max_sim
                    st["last_change_wall"] = now
                    _save_state(st)
                elif now - st["last_change_wall"] > STALL_WALL_S:
                    log(f"STALL {label}: no progress {STALL_WALL_S}s")
                    proc.kill()
                    _kill_docker_all()
                    log(f"END {label} exit=-2 (stall)")
                    return -2
            else:
                log(f"  {label}: no containers (between jobs?)")

        if not line:
            time.sleep(1.0)

    code = proc.wait()
    log(f"END {label} exit={code}")
    return code


def _run_task(task: TaskSpec, state: dict, *, first_cleanup: bool) -> bool:
    """Run one (scenario, arm) until ok or attempts exhausted. False = stop pipeline."""
    key = _task_key(task.scenario_id, task.arm)
    attempts = int(state["attempts"].get(key, 0))

    if _task_is_ok(task):
        log(f"SKIP ok {key}")
        return True

    if state.get("stopped"):
        return False

    if attempts >= MAX_ATTEMPTS_PER_TASK:
        _write_stopped(state, f"{key} exhausted ({MAX_ATTEMPTS_PER_TASK} attempts)")
        return False

    while attempts < MAX_ATTEMPTS_PER_TASK:
        if state.get("stopped"):
            return False
        attempts += 1
        state["attempts"][key] = attempts
        _save_state(state)

        log(f"RUN {key} attempt {attempts}/{MAX_ATTEMPTS_PER_TASK}")
        code = _run_subprocess(
            _triple_arm_cmd(task, skip_cleanup=not first_cleanup),
            label=f"{task.label}:{key}",
            sim_duration=task.sim_duration,
        )
        first_cleanup = False

        if code == -3:
            _write_stopped(
                state,
                f"{key}: per-run wall {PER_RUN_MAX_WALL_S}s exceeded — remaining tasks cancelled",
            )
            return False

        if _task_is_ok(task):
            log(f"OK {key} on attempt {attempts}")
            return True

        reason = _failure_reason(task)
        log(f"FAIL {key} attempt {attempts}: {reason}")
        _kill_docker_all()
        time.sleep(5)

        if code == -2:
            log(f"STALL counted as attempt for {key}")

    _write_stopped(
        state,
        f"{key} failed after {MAX_ATTEMPTS_PER_TASK} attempts: {_failure_reason(task)[:120]}",
    )
    return False


def _tasks_14k() -> list[TaskSpec]:
    tasks: list[TaskSpec] = []
    for sid in DEFAULT_FINALISTS:
        tasks.append(
            TaskSpec(
                scenario_id=sid,
                arm="actual",
                out_dir=OUT_14K,
                sim_duration=SIM_14K,
                jobs=2,
                predicted_only=False,
                label="14k",
            )
        )
    for sid in DEFAULT_FINALISTS:
        tasks.append(
            TaskSpec(
                scenario_id=sid,
                arm="predicted",
                out_dir=OUT_14K,
                sim_duration=SIM_14K,
                jobs=1,
                predicted_only=False,
                label="14k",
            )
        )
    return tasks


def _tasks_43k() -> list[TaskSpec]:
    return [
        TaskSpec(
            scenario_id=sid,
            arm="predicted",
            out_dir=OUT_43K,
            sim_duration=SIM_43K,
            jobs=1,
            predicted_only=True,
            label="43k-m3",
        )
        for sid in DEFAULT_FINALISTS
    ]


def _count_ok_14k() -> tuple[int, int]:
    ok = sum(1 for t in _tasks_14k() if _task_is_ok(t))
    return ok, len(_tasks_14k())


def _count_ok_43k() -> tuple[int, int]:
    ok = sum(1 for t in _tasks_43k() if _task_is_ok(t))
    return ok, len(_tasks_43k())


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument(
        "--resume",
        action="store_true",
        help="Continue from state.json (skip ok tasks, respect stopped flag)",
    )
    p.add_argument(
        "--reset",
        action="store_true",
        help="Clear attempt counters and stopped flag before run",
    )
    args = p.parse_args()

    MONITOR_DIR.mkdir(parents=True, exist_ok=True)
    OUT_14K.mkdir(parents=True, exist_ok=True)

    state = _load_state()
    if args.reset:
        state = {
            "attempts": {},
            "stopped": False,
            "stop_reason": "",
            "last_max_sim": 0.0,
            "last_change_wall": time.time(),
        }
        _save_state(state)
        clear_pipeline_abort()
        if STATUS_PATH.exists():
            STATUS_PATH.unlink()

    aborted = pipeline_abort_reason()
    if aborted and not args.reset:
        log(f"Pipeline aborted (per-run timeout): {aborted}")
        log("Clear with: run_overnight_monitored.py --reset")
        return 1

    if state.get("stopped") and not args.reset:
        log(f"Already stopped: {state.get('stop_reason')}")
        log("Use --reset to clear or fix issues and --resume to continue.")
        return 1

    log(
        f"=== Overnight pipeline ({'resume' if args.resume else 'start'}) "
        f"max {MAX_ATTEMPTS_PER_TASK} attempts per scenario:arm ==="
    )

    first_cleanup = True
    for task in _tasks_14k():
        if not _run_task(task, state, first_cleanup=first_cleanup):
            _rebuild_summary(OUT_14K, sim_duration=SIM_14K, predicted_only=False, jobs=2)
            return 1
        first_cleanup = False

    _rebuild_summary(OUT_14K, sim_duration=SIM_14K, predicted_only=False, jobs=2)
    ok14, exp14 = _count_ok_14k()
    log(f"14k: {ok14}/{exp14} ok")
    if ok14 < exp14:
        _write_stopped(state, f"14k incomplete ({ok14}/{exp14} ok)")
        return 1

    OUT_43K.mkdir(parents=True, exist_ok=True)
    for task in _tasks_43k():
        if not _run_task(task, state, first_cleanup=False):
            _rebuild_summary(OUT_43K, sim_duration=SIM_43K, predicted_only=True, jobs=1)
            return 1

    _rebuild_summary(OUT_43K, sim_duration=SIM_43K, predicted_only=True, jobs=1)
    ok43, exp43 = _count_ok_43k()
    log(f"43k M3: {ok43}/{exp43} ok")
    if ok43 < exp43:
        _write_stopped(state, f"43k incomplete ({ok43}/{exp43} ok)")
        return 1

    state["stopped"] = False
    state["stop_reason"] = ""
    _save_state(state)
    STATUS_PATH.write_text(
        json.dumps({"stopped": False, "completed": True, "ts": _ts()}, indent=2),
        encoding="utf-8",
    )
    log("=== Pipeline completed successfully ===")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
