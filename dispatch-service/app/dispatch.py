"""
Dispatch Service core algorithms.

Responsibilities:
- Supply-demand imbalance score calculation
- Incentive level determination (0.0 – 1.0)
- Probabilistic rerouting decision
- Nearest-taxi assignment (Euclidean distance)
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass

# Maximum imbalance value used for normalising the incentive level.
# Any imbalance >= MAX_IMBALANCE maps to incentive 1.0.
MAX_IMBALANCE = 20


@dataclass(frozen=True)
class Vehicle:
    id: str
    x: float
    y: float


@dataclass(frozen=True)
class Passenger:
    id: str
    x: float
    y: float


@dataclass(frozen=True)
class Assignment:
    passenger_id: str
    taxi_id: str


def compute_imbalance(predicted_demand: int, available_taxis: int) -> int:
    """Return supply-demand imbalance score.

    imbalance > 0  → demand exceeds supply (incentive needed)
    imbalance <= 0 → supply meets or exceeds demand
    """
    return predicted_demand - available_taxis


def compute_incentive_level(imbalance: int) -> float:
    """Return a normalised incentive level in [0.0, 1.0].

    The level scales linearly from 0 to MAX_IMBALANCE and is clamped to 1.0.
    When imbalance <= 0 the level is 0.0 (no incentive required).
    """
    if imbalance <= 0:
        return 0.0
    return min(imbalance / MAX_IMBALANCE, 1.0)


def should_reroute(incentive_level: float, rng: random.Random | None = None) -> bool:
    """Decide probabilistically whether an empty taxi should be rerouted.

    The probability equals the incentive level:
    - incentive_level 0.0 → never rerouted
    - incentive_level 1.0 → always rerouted
    """
    if rng is None:
        rng = random.Random()
    return rng.random() < incentive_level


def assign_nearest_taxi(
    passengers: list[Passenger],
    empty_taxis: list[Vehicle],
) -> list[Assignment]:
    """Match each passenger to the nearest available empty taxi.

    Taxis are consumed greedily: once assigned they are no longer available.
    Passengers without a reachable taxi are skipped.

    Returns a list of (passenger_id, taxi_id) assignments.
    """
    if not passengers or not empty_taxis:
        return []

    available = list(empty_taxis)
    assignments: list[Assignment] = []

    for passenger in passengers:
        if not available:
            break
        nearest = min(
            available,
            key=lambda t: math.hypot(t.x - passenger.x, t.y - passenger.y),
        )
        assignments.append(Assignment(passenger_id=passenger.id, taxi_id=nearest.id))
        available.remove(nearest)

    return assignments
