from __future__ import annotations

import math
from statistics import quantiles


def score_surge_deficit_cell(
    raw_surge: float,
    supply: int,
    demand: int,
    *,
    min_raw_surge: float,
) -> float | None:
    """Higher score = stronger rebalance target (high surge and/or demand deficit)."""
    if raw_surge < min_raw_surge:
        return None
    deficit = max(0, demand - supply)
    return raw_surge * (1.0 + float(deficit))


def select_top_surge_deficit_cells(
    raw_surge_by_h3: dict[str, float],
    grid_supply: dict[str, int],
    grid_demand: dict[str, int],
    *,
    top_k: int,
    min_raw_surge: float,
) -> list[str]:
    scored: list[tuple[float, str]] = []
    cells = set(raw_surge_by_h3) | set(grid_supply) | set(grid_demand)
    for cell in cells:
        raw = float(raw_surge_by_h3.get(cell, 0.0))
        score = score_surge_deficit_cell(
            raw,
            int(grid_supply.get(cell, 0)),
            int(grid_demand.get(cell, 0)),
            min_raw_surge=min_raw_surge,
        )
        if score is not None:
            scored.append((score, cell))
    scored.sort(key=lambda item: item[0], reverse=True)
    return [cell for _, cell in scored[:top_k]]


def acceptance_bonus_from_raw_surge(
    raw_surge: float,
    *,
    coef: float,
    activation: float = 1.2,
    cap: float = 0.25,
) -> float:
    """Continuous pickup-side bonus: coef * max(0, raw_surge - 1). Saturates at cap."""
    if coef <= 0 or raw_surge < activation:
        return 0.0
    return min(cap, coef * max(0.0, raw_surge - 1.0))


def aggregate_rebalance_metrics(
    surge_diagnostics: list[dict],
    rebalance_events: list[dict],
    *,
    high_surge_threshold: float = 2.5,
    min_samples_for_percentile: int = 20,
) -> dict:
    if not surge_diagnostics:
        return {
            "avg_high_surge_deficit": 0.0,
            "p90_raw_surge": 0.0,
            "high_surge_mean_raw_surge": 0.0,
            "rebalance_redirect_count": len(rebalance_events),
        }

    raw_values = [
        float(row["raw_surge"])
        for row in surge_diagnostics
        if row.get("raw_surge") is not None
    ]
    deficits = []
    high_surge_raw: list[float] = []
    for row in surge_diagnostics:
        if row.get("raw_surge") is None:
            continue
        supply = int(row.get("supply", 0))
        demand = int(row.get("actual_demand", row.get("demand", 0)))
        raw = float(row["raw_surge"])
        deficit = max(0, demand - supply)
        if raw >= high_surge_threshold:
            deficits.append(deficit)
            high_surge_raw.append(raw)

    p90_raw = 0.0
    if len(raw_values) >= min_samples_for_percentile:
        p90_raw = quantiles(raw_values, n=10, method="inclusive")[8]
    elif raw_values:
        p90_raw = max(raw_values)

    return {
        "avg_high_surge_deficit": (
            sum(deficits) / len(deficits) if deficits else 0.0
        ),
        "p90_raw_surge": p90_raw,
        "high_surge_mean_raw_surge": (
            sum(high_surge_raw) / len(high_surge_raw) if high_surge_raw else 0.0
        ),
        "rebalance_redirect_count": len(rebalance_events),
    }


def gaussian_cell_weight(
    edge_xy: tuple[float, float],
    cell_center_xy: tuple[float, float],
    importance: float,
    *,
    sigma_m: float,
) -> float:
    if importance <= 0:
        return 0.0
    ex, ey = edge_xy
    cx, cy = cell_center_xy
    two_sigma_sq = 2.0 * sigma_m * sigma_m
    dx, dy = ex - cx, ey - cy
    return importance * math.exp(-(dx * dx + dy * dy) / two_sigma_sq)
