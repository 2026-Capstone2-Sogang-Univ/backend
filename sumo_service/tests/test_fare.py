from app.fare import (
    BASE_FARE,
    DIST_UNIT_FARE,
    DIST_UNIT_M,
    FIXED_SURCHARGES,
    TIME_UNIT_FARE,
    TIME_UNIT_S,
    TripAccumulator,
    calculate_fare,
    calculate_meter_fare,
    estimate_fare,
)


# ---------------------------------------------------------------------------
# estimate_fare
# ---------------------------------------------------------------------------

def test_estimate_fare_zero_distance():
    # base + no distance units + surcharges
    assert estimate_fare(0.0) == BASE_FARE + FIXED_SURCHARGES


def test_estimate_fare_one_unit():
    assert estimate_fare(DIST_UNIT_M) == BASE_FARE + DIST_UNIT_FARE + FIXED_SURCHARGES


def test_estimate_fare_fractional_unit_truncated():
    # 1.5 units → floor to 1
    assert estimate_fare(1.5 * DIST_UNIT_M) == BASE_FARE + DIST_UNIT_FARE + FIXED_SURCHARGES


def test_estimate_fare_two_units():
    assert estimate_fare(2 * DIST_UNIT_M) == BASE_FARE + 2 * DIST_UNIT_FARE + FIXED_SURCHARGES


# ---------------------------------------------------------------------------
# calculate_fare
# ---------------------------------------------------------------------------

def _acc(**kwargs) -> TripAccumulator:
    return TripAccumulator(passenger_id="p_0", pickup_sim_time=0.0, **kwargs)


def test_calculate_fare_no_distance_no_lowspeed():
    assert calculate_fare(_acc()) == BASE_FARE + FIXED_SURCHARGES


def test_calculate_fare_distance_only():
    acc = _acc(distance_m=DIST_UNIT_M)
    assert calculate_fare(acc) == BASE_FARE + DIST_UNIT_FARE + FIXED_SURCHARGES


def test_calculate_fare_lowspeed_only():
    acc = _acc(low_speed_seconds=TIME_UNIT_S)
    assert calculate_fare(acc) == BASE_FARE + TIME_UNIT_FARE + FIXED_SURCHARGES


def test_calculate_fare_both():
    acc = _acc(distance_m=DIST_UNIT_M, low_speed_seconds=TIME_UNIT_S)
    assert calculate_fare(acc) == BASE_FARE + DIST_UNIT_FARE + TIME_UNIT_FARE + FIXED_SURCHARGES


def test_calculate_fare_applies_dispatch_surge():
    acc = _acc(distance_m=DIST_UNIT_M, surge=2.0)
    meter_fare = BASE_FARE + DIST_UNIT_FARE + FIXED_SURCHARGES
    assert calculate_meter_fare(acc) == meter_fare
    assert calculate_fare(acc) == round(meter_fare * 2.0)


def test_calculate_fare_exceeds_estimate_when_lowspeed():
    acc = _acc(distance_m=DIST_UNIT_M, low_speed_seconds=TIME_UNIT_S)
    assert calculate_fare(acc) > estimate_fare(acc.distance_m)
