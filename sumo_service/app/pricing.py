from __future__ import annotations

import math

DEFAULT_EPSILON = -0.6
DEFAULT_SURGE_MIN = 1.2
DEFAULT_SURGE_MAX = 4.9
DEFAULT_SURGE_STEP = 0.1
RAW_SURGE_BUCKETS: tuple[tuple[str, float, float | None, float], ...] = (
    ("raw_lt_1_5", float("-inf"), 1.5, 0.55),
    ("raw_lt_2_5", 1.5, 2.5, 0.70),
    ("raw_lt_3_5", 2.5, 3.5, 0.80),
    ("raw_gte_3_5", 3.5, None, 0.85),
)


def compute_raw_surge(
    supply: int | float,
    demand: int | float,
    *,
    elasticity: float = abs(DEFAULT_EPSILON),
    max_surge: float = DEFAULT_SURGE_MAX,
) -> float:
    """Return the unclamped v2 market pressure signal, (D/S)^(1/|epsilon|)."""
    if supply <= 0:
        return max_surge if demand > 0 else 1.0
    if demand <= 0:
        return 0.0
    return (demand / supply) ** (1.0 / abs(elasticity))


def apply_surge_limits(
    surge: float,
    *,
    min_active_surge: float = DEFAULT_SURGE_MIN,
    max_surge: float = DEFAULT_SURGE_MAX,
    increment: float = DEFAULT_SURGE_STEP,
) -> float:
    """Apply v2 surge activation, 0.1 ceil discretization, and upper cap."""
    if not math.isfinite(surge):
        return max_surge
    if surge < min_active_surge:
        return 1.0
    stepped_surge = math.ceil(surge / increment) * increment
    return min(round(stepped_surge, 10), max_surge)


def compute_limited_surge(
    supply: int | float,
    demand: int | float,
    *,
    elasticity: float = abs(DEFAULT_EPSILON),
    min_active_surge: float = DEFAULT_SURGE_MIN,
    max_surge: float = DEFAULT_SURGE_MAX,
    increment: float = DEFAULT_SURGE_STEP,
) -> float:
    raw_surge = compute_raw_surge(
        supply,
        demand,
        elasticity=elasticity,
        max_surge=max_surge,
    )
    return apply_surge_limits(
        raw_surge,
        min_active_surge=min_active_surge,
        max_surge=max_surge,
        increment=increment,
    )


def raw_surge_bucket(raw_surge: float) -> str:
    for bucket, lower, upper, _ in RAW_SURGE_BUCKETS:
        if raw_surge >= lower and (upper is None or raw_surge < upper):
            return bucket
    return RAW_SURGE_BUCKETS[-1][0]


def get_target_matching_rate(raw_surge: float) -> float:
    """Return the v2 target matching rate P* from raw surge bands."""
    bucket = raw_surge_bucket(raw_surge)
    return next(target for key, _, _, target in RAW_SURGE_BUCKETS if key == bucket)
