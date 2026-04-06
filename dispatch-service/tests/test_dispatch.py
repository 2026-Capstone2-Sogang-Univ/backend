"""
Tests for the Dispatch Service core algorithms.

All tests are free of external dependencies (no SUMO, gRPC, or ML model).
"""

import random

import pytest

from app.dispatch import (
    MAX_IMBALANCE,
    Assignment,
    Passenger,
    Vehicle,
    assign_nearest_taxi,
    compute_imbalance,
    compute_incentive_level,
    should_reroute,
)


# ---------------------------------------------------------------------------
# compute_imbalance
# ---------------------------------------------------------------------------


class TestComputeImbalance:
    def test_positive_imbalance(self):
        assert compute_imbalance(10, 4) == 6

    def test_zero_imbalance(self):
        assert compute_imbalance(5, 5) == 0

    def test_negative_imbalance(self):
        assert compute_imbalance(3, 10) == -7

    def test_no_demand(self):
        assert compute_imbalance(0, 5) == -5

    def test_no_taxis(self):
        assert compute_imbalance(8, 0) == 8


# ---------------------------------------------------------------------------
# compute_incentive_level
# ---------------------------------------------------------------------------


class TestComputeIncentiveLevel:
    def test_zero_when_no_imbalance(self):
        assert compute_incentive_level(0) == 0.0

    def test_zero_when_negative_imbalance(self):
        assert compute_incentive_level(-5) == 0.0

    def test_scales_linearly(self):
        half = MAX_IMBALANCE // 2
        level = compute_incentive_level(half)
        assert 0.0 < level < 1.0
        assert abs(level - 0.5) < 1e-9

    def test_clamps_at_max_imbalance(self):
        assert compute_incentive_level(MAX_IMBALANCE) == 1.0

    def test_clamps_beyond_max_imbalance(self):
        assert compute_incentive_level(MAX_IMBALANCE + 100) == 1.0

    def test_range_is_valid(self):
        for imbalance in range(-10, MAX_IMBALANCE + 10):
            level = compute_incentive_level(imbalance)
            assert 0.0 <= level <= 1.0


# ---------------------------------------------------------------------------
# should_reroute  (probabilistic)
# ---------------------------------------------------------------------------


class TestShouldReroute:
    _TRIALS = 10_000

    def test_never_reroutes_at_zero(self):
        rng = random.Random(42)
        results = [should_reroute(0.0, rng) for _ in range(self._TRIALS)]
        assert not any(results)

    def test_always_reroutes_at_one(self):
        rng = random.Random(42)
        results = [should_reroute(1.0, rng) for _ in range(self._TRIALS)]
        assert all(results)

    def test_approximately_correct_probability(self):
        incentive = 0.3
        rng = random.Random(0)
        hits = sum(should_reroute(incentive, rng) for _ in range(self._TRIALS))
        observed = hits / self._TRIALS
        # Allow ±3 % tolerance (well within 6-sigma for n=10 000)
        assert abs(observed - incentive) < 0.03

    def test_returns_bool(self):
        result = should_reroute(0.5, random.Random(1))
        assert isinstance(result, bool)


# ---------------------------------------------------------------------------
# assign_nearest_taxi
# ---------------------------------------------------------------------------


class TestAssignNearestTaxi:
    def test_nearest_taxi_selected(self):
        passenger = Passenger("p_0", 0.0, 0.0)
        taxis = [
            Vehicle("taxi_far", 100.0, 100.0),
            Vehicle("taxi_near", 1.0, 1.0),
        ]
        assignments = assign_nearest_taxi([passenger], taxis)
        assert len(assignments) == 1
        assert assignments[0].taxi_id == "taxi_near"
        assert assignments[0].passenger_id == "p_0"

    def test_each_taxi_assigned_at_most_once(self):
        passengers = [
            Passenger("p_0", 0.0, 0.0),
            Passenger("p_1", 0.1, 0.0),  # very close to p_0
        ]
        taxis = [
            Vehicle("taxi_0", 0.0, 0.0),
            Vehicle("taxi_1", 50.0, 50.0),
        ]
        assignments = assign_nearest_taxi(passengers, taxis)
        assigned_taxi_ids = [a.taxi_id for a in assignments]
        assert len(assigned_taxi_ids) == len(set(assigned_taxi_ids)), "taxi double-assigned"
        assert len(assignments) == 2

    def test_no_assignment_when_no_taxis(self):
        passengers = [Passenger("p_0", 0.0, 0.0)]
        assert assign_nearest_taxi(passengers, []) == []

    def test_no_assignment_when_no_passengers(self):
        taxis = [Vehicle("taxi_0", 0.0, 0.0)]
        assert assign_nearest_taxi([], taxis) == []

    def test_fewer_taxis_than_passengers(self):
        passengers = [Passenger(f"p_{i}", float(i), 0.0) for i in range(5)]
        taxis = [Vehicle("taxi_0", 0.0, 0.0)]
        assignments = assign_nearest_taxi(passengers, taxis)
        assert len(assignments) == 1

    def test_returns_assignment_dataclass(self):
        assignments = assign_nearest_taxi(
            [Passenger("p_0", 0.0, 0.0)],
            [Vehicle("taxi_0", 1.0, 0.0)],
        )
        assert isinstance(assignments[0], Assignment)

    def test_multiple_passengers_correct_matching(self):
        # p_0 is closest to taxi_A, p_1 is closest to taxi_B
        passengers = [
            Passenger("p_0", 0.0, 0.0),
            Passenger("p_1", 100.0, 0.0),
        ]
        taxis = [
            Vehicle("taxi_A", 1.0, 0.0),
            Vehicle("taxi_B", 99.0, 0.0),
        ]
        assignments = assign_nearest_taxi(passengers, taxis)
        id_map = {a.passenger_id: a.taxi_id for a in assignments}
        assert id_map["p_0"] == "taxi_A"
        assert id_map["p_1"] == "taxi_B"
