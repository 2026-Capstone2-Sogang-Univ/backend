"""Module 3 forecast validation at prediction horizon (predicted vs actual demand)."""
from __future__ import annotations

from statistics import mean


def _cell_errors(predicted: dict[str, float], actual: dict[str, float | int]) -> list[float]:
    cells = set(predicted) | set(actual)
    return [float(predicted.get(c, 0.0)) - float(actual.get(c, 0)) for c in cells]


def aggregate_module3_horizon_metrics(events: list[dict]) -> dict[str, float | int | None]:
    """Aggregate `module3_horizon_eval` events emitted when sim reaches forecast target time."""
    evals = [e for e in events if e.get("type") == "module3_horizon_eval"]
    if not evals:
        return {
            "module3_horizon_eval_count": 0,
            "module3_horizon_mae_avg": None,
            "module3_horizon_bias_avg": None,
            "module3_horizon_rmse_avg": None,
            "module3_horizon_mape_avg": None,
        }
    return {
        "module3_horizon_eval_count": len(evals),
        "module3_horizon_mae_avg": mean(e["mae"] for e in evals),
        "module3_horizon_bias_avg": mean(e["bias"] for e in evals),
        "module3_horizon_rmse_avg": mean(e["rmse"] for e in evals),
        "module3_horizon_mape_avg": _mean_optional(e.get("mape") for e in evals),
    }


def horizon_eval_snapshot(
    *,
    issued_sim_time: float,
    target_sim_time: float,
    predicted: dict[str, float],
    actual: dict[str, float | int],
) -> dict[str, float | str]:
    errors = _cell_errors(predicted, actual)
    abs_errors = [abs(v) for v in errors]
    mae = mean(abs_errors) if abs_errors else 0.0
    bias = mean(errors) if errors else 0.0
    rmse = (mean(v * v for v in errors) ** 0.5) if errors else 0.0
    actual_pos = [float(actual.get(c, 0)) for c in set(predicted) | set(actual)]
    actual_sum = sum(actual_pos)
    mape = None
    if actual_sum > 1e-9:
        mape = sum(abs_errors) / actual_sum
    return {
        "type": "module3_horizon_eval",
        "issued_sim_time": issued_sim_time,
        "target_sim_time": target_sim_time,
        "n_cells": len(set(predicted) | set(actual)),
        "mae": mae,
        "bias": bias,
        "rmse": rmse,
        "mape": mape,
    }


def _mean_optional(values) -> float | None:
    present = [float(v) for v in values if v is not None]
    return mean(present) if present else None
