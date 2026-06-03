"""Wait for triple_arm_14k to finish, append results doc, run 43200s M3 if all ok."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
OUT_14K = ROOT / ".temp" / "triple_arm_14k"
SUMMARY = OUT_14K / "summary.json"
RUN_LOG = OUT_14K / "run.log"
DOC = ROOT / "docs" / "2026-06-01-experiment-results.md"
POLL_S = 120
MAX_WAIT_H = 8
# Ignore summary.json written before this watcher (stale failed batch).
_WATCHER_STARTED_AT = time.time()


def _summary_is_fresh() -> bool:
    if not SUMMARY.exists():
        return False
    return SUMMARY.stat().st_mtime >= _WATCHER_STARTED_AT - 5


def _run_still_active() -> bool:
    proc = subprocess.run(
        ["docker", "ps", "-q", "--filter", "name=backend-sumo-service-run"],
        capture_output=True,
        text=True,
        check=False,
    )
    return bool(proc.stdout.strip())


def _load_summary() -> dict | None:
    if not SUMMARY.exists():
        return None
    try:
        return json.loads(SUMMARY.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None


def _counts(summary: dict) -> tuple[int, int, int]:
    runs = summary.get("runs") or []
    ok = sum(1 for r in runs if r.get("status") == "ok")
    err = sum(1 for r in runs if r.get("status") == "error")
    return ok, err, len(runs)


def _append_doc(summary: dict) -> None:
    ok, err, total = _counts(summary)
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    lines = [
        "",
        f"### 8.6 Triple-arm 14400s (auto log {ts})",
        "",
        f"- **runs:** {ok}/{total} ok, {err} error",
        "",
    ]
    for arm in ("actual", "ai_policy"):
        rows = (summary.get("arms") or {}).get(arm) or []
        if not rows:
            continue
        lines.append(f"**{arm}**")
        lines.append("")
        lines.append("| scenario | status | match | m3_mae |")
        lines.append("|----------|--------|-------|--------|")
        for r in rows:
            sid = r.get("scenario_id", "")
            st = r.get("status", "")
            match = r.get("matching_success_rate")
            mae = r.get("module3_horizon_mae_avg")
            lines.append(f"| {sid} | {st} | {match} | {mae} |")
        lines.append("")
    policy_ab = (summary.get("arms") or {}).get("policy_ab") or []
    if policy_ab:
        lines.append("**policy_ab**")
        lines.append("")
        lines.append("| scenario | status | Δ match |")
        lines.append("|----------|--------|---------|")
        for r in policy_ab:
            d = r.get("delta_matching_success_rate")
            lines.append(f"| {r.get('scenario_id')} | {r.get('status')} | {d} |")
        lines.append("")
    marker = "### 8.6 Triple-arm 14400s"
    text = DOC.read_text(encoding="utf-8") if DOC.exists() else ""
    if marker in text:
        head, _, tail = text.partition(marker)
        tail = tail.split("\n### ", 1)
        tail = tail[1] if len(tail) > 1 else ""
        text = head.rstrip() + "\n" + "\n".join(lines)
        if tail:
            text += "\n### " + tail
    else:
        insert_at = "## 9. 변경 이력"
        if insert_at in text:
            text = text.replace(insert_at, "\n".join(lines) + "\n\n" + insert_at)
        else:
            text = text.rstrip() + "\n" + "\n".join(lines)
    DOC.write_text(text, encoding="utf-8")
    print(f"Updated {DOC}", flush=True)


def _run_m3() -> int:
    cmd = [
        sys.executable,
        str(ROOT / "sumo_service" / "scripts" / "run_three_arm_parallel.py"),
        "--jobs",
        "6",
        "--sim-duration",
        "43200",
        "--predicted-only",
        "--out-dir",
        str(ROOT / ".temp" / "triple_arm_43k"),
    ]
    env = os.environ.copy()
    env.setdefault("EXPERIMENT_FAST", "1")
    env.setdefault("DOCKER_MAX_JOBS", "6")
    print("Starting M3 long run (43200s, predicted-only)...", flush=True)
    return subprocess.call(cmd, cwd=str(ROOT), env=env)


def main() -> int:
    deadline = time.time() + MAX_WAIT_H * 3600
    print(f"Waiting for {SUMMARY} (poll {POLL_S}s, max {MAX_WAIT_H}h)...", flush=True)
    while time.time() < deadline:
        if not _summary_is_fresh():
            if _run_still_active():
                print("Run in progress (docker active), waiting...", flush=True)
            time.sleep(POLL_S)
            continue
        summary = _load_summary()
        if summary:
            ok, err, total = _counts(summary)
            if total >= 12 and ok + err == total and not _run_still_active():
                print(f"Done: {ok}/{total} ok", flush=True)
                _append_doc(summary)
                if err == 0 and ok == total:
                    return _run_m3()
                print("Skipping M3: not all runs ok", flush=True)
                return 1
        time.sleep(POLL_S)
    print("Timeout waiting for triple_arm_14k", flush=True)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
