"""Write finalist list from .temp/screen/summary.json for overnight runs."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--summary", type=Path, default=ROOT / ".temp" / "screen" / "summary.json")
    p.add_argument("--out", type=Path, default=ROOT / ".temp" / "screen" / "finalists_for_overnight.json")
    p.add_argument("--top-per-case", type=int, default=2)
    p.add_argument("--exclude", default="A1_peak_12,A1_peak_15,fair_ratio30", help="comma ids")
    args = p.parse_args()

    summary = json.loads(args.summary.read_text(encoding="utf-8"))
    exclude = {x.strip() for x in args.exclude.split(",") if x.strip()}
    ids: list[str] = []
    seen: set[str] = set()
    for case, items in (summary.get("finalists") or {}).items():
        for item in items[: args.top_per_case]:
            sid = item["scenario_id"]
            if sid in exclude or sid in seen:
                continue
            seen.add(sid)
            ids.append(sid)
    # always include stress contrast
    if "B_stress_55" not in seen:
        ids.append("B_stress_55")

    payload = {"finalist_ids": ids, "source": str(args.summary)}
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    print(json.dumps(payload, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
