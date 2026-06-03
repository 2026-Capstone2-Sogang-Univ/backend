"""
Wait for screening summary to finish, then run Module 3 long validation (sequential-friendly).

Usage (repo root):
  python sumo_service/scripts/run_m3_after_screen.py --jobs 1 --sim-duration 43200
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SUMMARY = ROOT / ".temp" / "screen" / "summary.json"
SCRIPT = ROOT / "sumo_service" / "scripts" / "run_module3_validation_parallel.py"


def _screening_complete(path: Path, *, min_ok: int) -> bool:
    if not path.exists():
        return False
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return False
    ok = sum(1 for row in data.get("all", []) if row.get("status") == "ok")
    return ok >= min_ok


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--min-ok", type=int, default=14, help="expected screening scenarios")
    p.add_argument("--poll-s", type=float, default=30.0)
    p.add_argument("--timeout-s", type=float, default=7200.0)
    p.add_argument("--jobs", type=int, default=4, help="parallel Docker (capped at 4; run after sweep only)")
    p.add_argument("--sim-duration", type=float, default=43200.0)
    args, extra = p.parse_known_args()

    deadline = time.monotonic() + args.timeout_s
    last_mtime = 0.0
    stable_ticks = 0
    print(f"Waiting for screening at {SUMMARY} (need>={args.min_ok} ok)...", flush=True)
    while time.monotonic() < deadline:
        if SUMMARY.exists():
            mtime = SUMMARY.stat().st_mtime
            if mtime == last_mtime and _screening_complete(SUMMARY, min_ok=args.min_ok):
                stable_ticks += 1
                if stable_ticks >= 2:
                    break
            else:
                stable_ticks = 0
                last_mtime = mtime
        time.sleep(args.poll_s)
    else:
        print("Timeout waiting for screening", flush=True)
        return 1

    print("Screening done. Starting Module 3 validation...", flush=True)
    cmd = [
        sys.executable,
        str(SCRIPT),
        "--jobs",
        str(args.jobs),
        "--sim-duration",
        str(args.sim_duration),
        *extra,
    ]
    return subprocess.call(cmd, cwd=str(ROOT))


if __name__ == "__main__":
    raise SystemExit(main())
