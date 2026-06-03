"""
Sleep watchdog: wait for in-flight gate runs; if both finish ok, continue 14k/43k.
Per Docker run: kill after PER_RUN_MAX_WALL_S (default 2h). If any run hits that
limit, abort the entire pipeline (no further scenarios). No whole-pipeline wall limit.

Usage (background):
  cd backend
  $env:PREDICTION_API_KEY = "..."
  $env:EXPERIMENT_FAST = "1"
  python -u sumo_service/scripts/run_sleep_watchdog.py
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SUMO_ROOT = ROOT / "sumo_service"
sys.path.insert(0, str(SUMO_ROOT))

from scripts.docker_run_helpers import (  # noqa: E402
    PER_RUN_MAX_WALL_S,
    abort_pipeline_on_per_run_timeout,
    enforce_per_run_container_timeouts,
    pipeline_abort_reason,
    sumo_run_container_ids,
)
from scripts.run_overnight_monitored import (  # noqa: E402
    OUT_14K,
    _count_ok_14k,
    _env,
    _ts,
)

POLL_S = int(os.getenv("SLEEP_WATCHDOG_POLL_S", "60"))
GATE_SCENARIOS = ("fair_dispatch10", "fair_ratio35")
LOG_PATH = ROOT / ".temp" / "overnight" / "sleep_watchdog.log"
STATUS_PATH = ROOT / ".temp" / "overnight" / "sleep_watchdog_status.json"
OVERNIGHT_SCRIPT = SUMO_ROOT / "scripts" / "run_overnight_monitored.py"


def log(msg: str) -> None:
    line = f"[{_ts()}] {msg}"
    print(line, flush=True)
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with LOG_PATH.open("a", encoding="utf-8") as f:
        f.write(line + "\n")


def _write_status(payload: dict) -> None:
    payload["ts"] = datetime.now(timezone.utc).isoformat()
    STATUS_PATH.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")


def _gate_scenarios_ok() -> tuple[bool, str]:
    reasons: list[str] = []
    for sid in GATE_SCENARIOS:
        path = OUT_14K / "actual" / f"{sid}.json"
        if not path.exists():
            reasons.append(f"{sid}: missing")
            continue
        try:
            row = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as e:
            reasons.append(f"{sid}: bad json ({e})")
            continue
        st = row.get("status")
        if st != "ok":
            reasons.append(f"{sid}: status={st!r}")
    if reasons:
        return False, "; ".join(reasons)
    return True, "both gate scenarios ok"


def _latest_progress() -> str:
    if not sumo_run_container_ids():
        return "no containers"
    lines: list[str] = []
    for cid in sumo_run_container_ids():
        tail = subprocess.run(
            ["docker", "logs", "--tail", "5", cid],
            capture_output=True,
            text=True,
            check=False,
        ).stdout
        for line in reversed(tail.splitlines()):
            if "[progress]" in line:
                lines.append(line.strip()[:120])
                break
    return " | ".join(lines) if lines else "containers up, no progress line yet"


def _handle_timeout(timed_out: list[str], pipeline_proc: subprocess.Popen[bytes] | None) -> int:
    abort_pipeline_on_per_run_timeout(timed_out_container_ids=timed_out, log_fn=log)
    if pipeline_proc is not None and pipeline_proc.poll() is None:
        pipeline_proc.kill()
    ok14, exp14 = _count_ok_14k()
    _write_status(
        {
            "phase": "aborted",
            "ok_14k": ok14,
            "expected_14k": exp14,
            "reason": pipeline_abort_reason(),
        }
    )
    return 3


def main() -> int:
    log(
        f"=== Sleep watchdog: per-run wall={PER_RUN_MAX_WALL_S}s; "
        "gate ok -> continue; any timeout -> stop all remaining ==="
    )
    log(f"Gate scenarios: {', '.join(GATE_SCENARIOS)} (both actual 14k status=ok)")

    prior = pipeline_abort_reason()
    if prior:
        log(f"Already aborted: {prior}")
        _write_status({"phase": "aborted", "reason": prior})
        return 3

    pipeline_proc: subprocess.Popen[bytes] | None = None
    pipeline_started = False

    while True:
        timed_out = enforce_per_run_container_timeouts(log_fn=log)
        if timed_out:
            return _handle_timeout(timed_out, pipeline_proc)

        if pipeline_abort_reason():
            return _handle_timeout([], pipeline_proc)

        if pipeline_proc is not None and pipeline_proc.poll() is not None:
            code = pipeline_proc.returncode
            ok14, exp14 = _count_ok_14k()
            log(f"Overnight pipeline exited code={code}; 14k {ok14}/{exp14} ok")
            _write_status(
                {
                    "phase": "done",
                    "pipeline_exit": code,
                    "ok_14k": ok14,
                    "expected_14k": exp14,
                    "reason": "pipeline_finished",
                }
            )
            return 0 if code == 0 else 1

        if pipeline_proc is None:
            if sumo_run_container_ids():
                log(f"WAIT in-flight docker: {_latest_progress()}")
            else:
                ok, detail = _gate_scenarios_ok()
                if ok and not pipeline_started:
                    log(f"GATE passed ({detail}) — starting overnight --resume")
                    pipeline_started = True
                    overnight_log = LOG_PATH.parent / "sleep_overnight.log"
                    log_fh = overnight_log.open("a", encoding="utf-8")
                    pipeline_proc = subprocess.Popen(
                        [sys.executable, "-u", str(OVERNIGHT_SCRIPT), "--resume"],
                        cwd=str(ROOT),
                        env=_env(),
                        stdout=log_fh,
                        stderr=subprocess.STDOUT,
                    )
                    log(f"Overnight logs: {overnight_log}")
                elif not ok:
                    log(f"WAIT gate: {detail}")
        else:
            ok14, exp14 = _count_ok_14k()
            log(f"PIPELINE running; 14k {ok14}/{exp14} ok")

        time.sleep(POLL_S)


if __name__ == "__main__":
    raise SystemExit(main())
