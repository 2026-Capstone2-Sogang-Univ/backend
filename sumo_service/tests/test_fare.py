import pytest

from app.fare import (
    BASE_DIST_M,
    BASE_FARE,
    DIST_UNIT_FARE,
    DIST_UNIT_M,
    TIME_UNIT_FARE,
    TIME_UNIT_S,
    TripAccumulator,
    calculate_fare,
    estimate_fare,
)


# ---------------------------------------------------------------------------
# estimate_fare
# ---------------------------------------------------------------------------

def test_estimate_fare_below_base_distance():
    assert estimate_fare(0.0) == BASE_FARE


def test_estimate_fare_exactly_base_distance():
    assert estimate_fare(BASE_DIST_M) == BASE_FARE


def test_estimate_fare_one_unit_over():
    assert estimate_fare(BASE_DIST_M + DIST_UNIT_M) == BASE_FARE + DIST_UNIT_FARE


def test_estimate_fare_fractional_unit_truncated():
    # 1.5 단위 → 버림 → 1 단위분만 추가
    assert estimate_fare(BASE_DIST_M + 1.5 * DIST_UNIT_M) == BASE_FARE + DIST_UNIT_FARE


# ---------------------------------------------------------------------------
# calculate_fare
# ---------------------------------------------------------------------------

def _acc(**kwargs) -> TripAccumulator:
    return TripAccumulator(passenger_id="p_0", pickup_sim_time=0.0, **kwargs)


def test_calculate_fare_no_distance_no_lowspeed():
    assert calculate_fare(_acc()) == BASE_FARE


def test_calculate_fare_distance_only():
    acc = _acc(distance_m=BASE_DIST_M + DIST_UNIT_M)
    assert calculate_fare(acc) == BASE_FARE + DIST_UNIT_FARE


def test_calculate_fare_lowspeed_only():
    acc = _acc(low_speed_seconds=TIME_UNIT_S)
    assert calculate_fare(acc) == BASE_FARE + TIME_UNIT_FARE


def test_calculate_fare_both():
    acc = _acc(distance_m=BASE_DIST_M + DIST_UNIT_M, low_speed_seconds=TIME_UNIT_S)
    assert calculate_fare(acc) == BASE_FARE + DIST_UNIT_FARE + TIME_UNIT_FARE


def test_calculate_fare_exceeds_estimate_when_lowspeed():
    acc = _acc(distance_m=BASE_DIST_M + DIST_UNIT_M, low_speed_seconds=TIME_UNIT_S)
    assert calculate_fare(acc) > estimate_fare(acc.distance_m)
