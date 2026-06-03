"""Shared screening scenarios — inverse pricing + PU model unchanged."""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Scenario:
    scenario_id: str
    case: str  # "fair" | "imbalance" | "stress"
    passenger_lambda: int
    n_taxis: int = 300
    policy_mode: str = "matching"
    alpha_sensitivity: float = 1.5
    elasticity: float = 0.6
    dispatch_max_candidates: int | None = None
    surge_max: float | None = None
    band_incentive_usd: tuple[float, float, float, float] | None = None
    ratio_label: str = ""

    @property
    def heavy(self) -> bool:
        return self.n_taxis > 400


def score_scenario(case: str, metrics: dict) -> float:
    m = metrics
    match = float(m.get("matching_success_rate") or 0.0)
    err = abs(float(m.get("avg_matching_rate_error") or 0.0))
    deficit = float(m.get("avg_high_surge_deficit") or 0.0)
    never = float(m.get("passengers_never_offered_rate") or 0.0)
    fare = float(m.get("avg_final_fare_usd") or 0.0)
    band_penalty = sum(
        abs(float(m[key]))
        for key in (
            "band_raw_lt_1_5_p_error",
            "band_raw_lt_2_5_p_error",
            "band_raw_lt_3_5_p_error",
            "band_raw_gte_3_5_p_error",
        )
        if m.get(key) is not None
    )
    if case == "imbalance":
        return match + 0.5 * (1.0 - never) - 0.15 * deficit - 0.05 * band_penalty
    return match - 0.4 * err - 0.002 * fare - 0.1 * never - 0.05 * band_penalty


def lambda_for_ratio(n_taxis: int, ratio: float) -> int:
    """PASSENGER_LAMBDA from 승객:택시 ratio (spawn every 5 sim-min → 12 intervals/h)."""
    return max(1, int(round(n_taxis * ratio / 12.0)))


def build_screen_scenarios() -> tuple[Scenario, ...]:
    n = 300
    return (
        Scenario(
            "B_stress_55",
            "stress",
            passenger_lambda=lambda_for_ratio(n, 5.5),
            n_taxis=n,
            ratio_label="5.5:1",
        ),
        Scenario(
            "A1_peak_15",
            "fair",
            passenger_lambda=lambda_for_ratio(n, 1.5),
            n_taxis=n,
            ratio_label="1.5:1",
        ),
        Scenario(
            "A1_peak_12",
            "fair",
            passenger_lambda=lambda_for_ratio(n, 1.2),
            n_taxis=n,
            ratio_label="1.2:1",
        ),
        Scenario(
            "fair_ratio40",
            "fair",
            passenger_lambda=lambda_for_ratio(n, 4.0),
            ratio_label="4.0:1",
        ),
        Scenario(
            "fair_ratio35",
            "fair",
            passenger_lambda=lambda_for_ratio(n, 3.5),
            ratio_label="3.5:1",
        ),
        Scenario(
            "fair_ratio30",
            "fair",
            passenger_lambda=lambda_for_ratio(n, 3.0),
            ratio_label="3.0:1",
        ),
        Scenario(
            "fair_surge6",
            "fair",
            passenger_lambda=lambda_for_ratio(n, 4.0),
            surge_max=6.0,
            ratio_label="4.0:1 cap6",
        ),
        Scenario(
            "fair_dispatch10",
            "fair",
            passenger_lambda=lambda_for_ratio(n, 4.0),
            dispatch_max_candidates=10,
            ratio_label="4.0:1 K10",
        ),
        Scenario(
            "fair_alpha20",
            "fair",
            passenger_lambda=lambda_for_ratio(n, 4.0),
            alpha_sensitivity=2.0,
            ratio_label="4.0:1 a2",
        ),
        Scenario(
            "fair_alpha125",
            "fair",
            passenger_lambda=lambda_for_ratio(n, 4.0),
            alpha_sensitivity=1.25,
            ratio_label="4.0:1 a1.25",
        ),
        Scenario(
            "fair_elast08",
            "fair",
            passenger_lambda=lambda_for_ratio(n, 4.0),
            elasticity=0.8,
            ratio_label="4.0:1 e0.8",
        ),
        Scenario(
            "imb_rebalance_40",
            "imbalance",
            passenger_lambda=lambda_for_ratio(n, 4.0),
            policy_mode="rebalance",
            ratio_label="4.0:1 reb",
        ),
        Scenario(
            "imb_band_inc",
            "imbalance",
            passenger_lambda=lambda_for_ratio(n, 4.0),
            band_incentive_usd=(0.0, 0.0, 1.5, 4.0),
            ratio_label="4.0:1 band",
        ),
        Scenario(
            "imb_combo",
            "imbalance",
            passenger_lambda=lambda_for_ratio(n, 4.0),
            policy_mode="rebalance",
            dispatch_max_candidates=10,
            surge_max=6.0,
            band_incentive_usd=(0.0, 0.0, 2.0, 5.0),
            ratio_label="4.0:1 combo",
        ),
        Scenario(
            "A2_supply_peak15",
            "fair",
            passenger_lambda=lambda_for_ratio(1100, 5.5),
            n_taxis=1100,
            ratio_label="1.5:1 N1100",
        ),
    )
