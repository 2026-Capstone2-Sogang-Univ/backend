"""
Quick (~3 min wall) scenario screen → pick finalists → optional 1h sim run.

Targets per raw_surge band: <1.5→55%, <2.5→70%, <3.5→80%, >=3.5→85% (inverse fare).
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.run_acceptance_experiment import _append_csv, _run_one  # noqa: E402


@dataclass(frozen=True)
class Scenario:
    scenario_id: str
    case: str  # "imbalance" | "fair"
    passenger_lambda: int = 100
    policy_mode: str = "matching"
    alpha_sensitivity: float = 1.5
    elasticity: float = 0.6
    dispatch_max_candidates: int | None = None
    surge_max: float | None = None
    band_incentive_usd: tuple[float, float, float, float] | None = None
    n_taxis: int = 300


SCREEN_SCENARIOS: tuple[Scenario, ...] = (
    Scenario("baseline_55", "fair", passenger_lambda=138, policy_mode="matching"),
    Scenario("ratio40_fair", "fair", passenger_lambda=100),
    Scenario("ratio30_fair", "fair", passenger_lambda=75),
    Scenario("rebalance_40", "imbalance", passenger_lambda=100, policy_mode="rebalance"),
    Scenario("dispatch10_40", "fair", passenger_lambda=100, dispatch_max_candidates=10),
    Scenario("surge6_40", "fair", passenger_lambda=100, surge_max=6.0),
    Scenario(
        "band_inc_40",
        "imbalance",
        passenger_lambda=100,
        band_incentive_usd=(0.0, 0.0, 1.5, 4.0),
    ),
    Scenario(
        "combo_reb_disp_band",
        "imbalance",
        passenger_lambda=100,
        policy_mode="rebalance",
        dispatch_max_candidates=10,
        band_incentive_usd=(0.0, 0.0, 2.0, 5.0),
    ),
    Scenario("ratio40_reb", "imbalance", passenger_lambda=100, policy_mode="rebalance"),
    Scenario("surge6_reb40", "imbalance", passenger_lambda=100, policy_mode="rebalance", surge_max=6.0),
)


def _score(case: str, metrics: dict) -> float:
    m = metrics
    match = float(m.get("matching_success_rate") or 0.0)
    err = abs(float(m.get("avg_matching_rate_error") or 0.0))
    deficit = float(m.get("avg_high_surge_deficit") or 0.0)
    never = float(m.get("passengers_never_offered_rate") or 0.0)
    fare = float(m.get("avg_final_fare_usd") or 0.0)
    band_penalty = 0.0
    for key, target in (
        ("band_raw_lt_1_5_p_error", 0.55),
        ("band_raw_lt_2_5_p_error", 0.70),
        ("band_raw_lt_3_5_p_error", 0.80),
        ("band_raw_gte_3_5_p_error", 0.85),
    ):
        if m.get(key) is not None:
            band_penalty += abs(float(m[key]))

    if case == "imbalance":
        return match + 0.5 * (1.0 - never) - 0.15 * deficit - 0.05 * band_penalty
    return match - 0.4 * err - 0.002 * fare - 0.1 * never - 0.05 * band_penalty


def _run_scenario(scenario: Scenario, *, seed: int, sim_duration: float, step_length: float) -> dict:
    row = _run_one(
        scenario.elasticity,
        None,
        seed,
        sim_duration,
        step_length,
        alpha_sensitivity=scenario.alpha_sensitivity,
        policy_mode=scenario.policy_mode,
        n_taxis=scenario.n_taxis,
        passenger_lambda=scenario.passenger_lambda,
        dispatch_max_candidates=scenario.dispatch_max_candidates,
        surge_max=scenario.surge_max,
        band_incentive_usd=scenario.band_incentive_usd,
    )
    row["scenario_id"] = scenario.scenario_id
    row["case"] = scenario.case
    if row.get("metrics"):
        row["score"] = _score(scenario.case, row["metrics"])
    return row


def main() -> int:
    parser = argparse.ArgumentParser(description="Screen scenarios then run finalists.")
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--screen-duration", type=float, default=1200.0, help="~3–4 min wall")
    parser.add_argument("--final-duration", type=float, default=3600.0, help="sim 1h")
    parser.add_argument("--step-length", type=float, default=1.0)
    parser.add_argument("--screen-csv", type=Path, default=ROOT.parent / ".temp" / "screen_results.csv")
    parser.add_argument("--final-csv", type=Path, default=ROOT.parent / ".temp" / "final_results.csv")
    parser.add_argument("--top-k", type=int, default=3)
    parser.add_argument("--skip-finals", action="store_true")
    parser.add_argument("--scenario-ids", help="comma-separated subset")
    args = parser.parse_args()

    scenarios = SCREEN_SCENARIOS
    if args.scenario_ids:
        wanted = {s.strip() for s in args.scenario_ids.split(",")}
        scenarios = tuple(s for s in SCREEN_SCENARIOS if s.scenario_id in wanted)

    screen_rows: list[dict] = []
    for scenario in scenarios:
        print(f"[screen] {scenario.scenario_id} ({scenario.case})", flush=True)
        row = _run_scenario(scenario, seed=args.seed, sim_duration=args.screen_duration, step_length=args.step_length)
        screen_rows.append(row)
        if args.screen_csv:
            _append_csv(args.screen_csv, [row])

    ranked = sorted(
        [r for r in screen_rows if r.get("status") == "ok" and r.get("metrics")],
        key=lambda r: float(r.get("score", -1e9)),
        reverse=True,
    )
    summary_path = args.screen_csv.with_suffix(".summary.json") if args.screen_csv else None
    summary = {
        "screen_duration": args.screen_duration,
        "ranked": [
            {
                "scenario_id": r["scenario_id"],
                "case": r["case"],
                "score": r.get("score"),
                "matching_success_rate": r["metrics"].get("matching_success_rate"),
                "avg_matching_rate_error": r["metrics"].get("avg_matching_rate_error"),
                "passengers_never_offered_rate": r["metrics"].get("passengers_never_offered_rate"),
                "avg_high_surge_deficit": r["metrics"].get("avg_high_surge_deficit"),
            }
            for r in ranked
        ],
    }
    if summary_path:
        summary_path.parent.mkdir(parents=True, exist_ok=True)
        summary_path.write_text(json.dumps(summary, indent=2), encoding="utf-8")
    print(json.dumps(summary, indent=2), flush=True)

    if args.skip_finals or not ranked:
        return 0

    finalists = ranked[: args.top_k]
    by_id = {s.scenario_id: s for s in SCREEN_SCENARIOS}
    final_rows: list[dict] = []
    for pick in finalists:
        scenario = by_id[pick["scenario_id"]]
        print(f"[final] {scenario.scenario_id} sim_duration={args.final_duration}", flush=True)
        row = _run_scenario(
            scenario,
            seed=args.seed,
            sim_duration=args.final_duration,
            step_length=args.step_length,
        )
        row["phase"] = "final"
        final_rows.append(row)
        if args.final_csv:
            _append_csv(args.final_csv, [row])

    print(json.dumps(final_rows, indent=2, ensure_ascii=False), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
