"""Compare actual-demand vs predicted-demand policy KPIs (Module 4 simulation)."""
from __future__ import annotations

POLICY_KPI_KEYS: tuple[str, ...] = (
    "matching_success_rate",
    "passengers_never_offered_rate",
    "dispatch_acceptance_rate",
    "avg_empty_wait_time_s",
    "p95_empty_wait_time_s",
    "acceptances_per_driver_hour",
    "driver_revenue_per_hour_usd",
    "avg_matching_rate_error",
    "avg_abs_matching_rate_error",
    "avg_final_surge",
    "avg_final_fare_usd",
    "surge_clamped_rate",
)

# Higher is better for these keys when judging "AI helped"
_HIGHER_IS_BETTER = {
    "matching_success_rate",
    "dispatch_acceptance_rate",
    "acceptances_per_driver_hour",
    "driver_revenue_per_hour_usd",
}

# Lower is better
_LOWER_IS_BETTER = {
    "passengers_never_offered_rate",
    "avg_empty_wait_time_s",
    "p95_empty_wait_time_s",
    "avg_abs_matching_rate_error",
    "surge_clamped_rate",
}


def compare_policy_ab(
    actual_metrics: dict,
    predicted_metrics: dict,
) -> dict[str, object]:
    """Return deltas (predicted − actual) and a coarse improvement summary."""
    deltas: dict[str, float | None] = {}
    improved: list[str] = []
    regressed: list[str] = []
    unchanged: list[str] = []

    for key in POLICY_KPI_KEYS:
        a = actual_metrics.get(key)
        p = predicted_metrics.get(key)
        if a is None or p is None:
            deltas[key] = None
            continue
        delta = float(p) - float(a)
        deltas[key] = delta
        if abs(delta) < 1e-6:
            unchanged.append(key)
            continue
        if key in _HIGHER_IS_BETTER:
            (improved if delta > 0 else regressed).append(key)
        elif key in _LOWER_IS_BETTER:
            (improved if delta < 0 else regressed).append(key)

    return {
        "deltas_predicted_minus_actual": deltas,
        "policy_improved_keys": improved,
        "policy_regressed_keys": regressed,
        "policy_unchanged_keys": unchanged,
        "policy_net_improved": len(improved) > len(regressed),
    }
