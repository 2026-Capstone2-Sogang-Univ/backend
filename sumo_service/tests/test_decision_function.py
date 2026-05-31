from datetime import datetime
import math

import h3
import pytest

from app.driver import decision_function
from app.driver.decision_function import (
    acceptance_probability,
    pu_correction_constant,
    required_fare_for_target_p,
)


def test_required_fare_round_trips_to_target_probability():
    cell = h3.latlng_to_cell(40.7580, -73.9855, 9)
    target_p = 0.8
    beta_f = 0.006

    fare = required_fare_for_target_p(
        last_dropoff_cell=cell,
        dropoff_cell=cell,
        call_datetime=datetime(2013, 7, 8, 8, 0),
        target_p=target_p,
        D_pu=0.3,
        trip_distance=1.8,
        beta_f=beta_f,
    )
    p = acceptance_probability(
        last_dropoff_cell=cell,
        dropoff_cell=cell,
        call_datetime=datetime(2013, 7, 8, 8, 0),
        fare_amount=fare,
        D_pu=0.3,
        trip_distance=1.8,
        beta_f=beta_f,
    )

    assert p == pytest.approx(target_p)


def test_required_fare_round_trips_to_target_probability_with_alpha_sensitivity():
    cell = h3.latlng_to_cell(40.7580, -73.9855, 9)
    target_p = 0.8
    beta_f = 0.006
    alpha_sensitivity = 1.25

    fare = required_fare_for_target_p(
        last_dropoff_cell=cell,
        dropoff_cell=cell,
        call_datetime=datetime(2013, 7, 8, 8, 0),
        target_p=target_p,
        D_pu=0.3,
        trip_distance=1.8,
        beta_f=beta_f,
        alpha_sensitivity=alpha_sensitivity,
    )
    p = acceptance_probability(
        last_dropoff_cell=cell,
        dropoff_cell=cell,
        call_datetime=datetime(2013, 7, 8, 8, 0),
        fare_amount=fare,
        D_pu=0.3,
        trip_distance=1.8,
        beta_f=beta_f,
        alpha_sensitivity=alpha_sensitivity,
    )

    assert p == pytest.approx(target_p)


def test_larger_alpha_sensitivity_increases_probability_for_positive_utility(monkeypatch):
    cell = h3.latlng_to_cell(40.7580, -73.9855, 9)

    monkeypatch.setattr(decision_function, "_BETA_0", -2.0)
    monkeypatch.setattr(
        decision_function,
        "acceptance_features",
        lambda **kwargs: decision_function.AcceptanceFeatures(
            dV_without_fare=0.0,
            t_pu=0.0,
            t_c=0.0,
            z_without_fare=-2.0,
        ),
    )
    common = dict(
        last_dropoff_cell=cell,
        dropoff_cell=cell,
        call_datetime=datetime(2013, 7, 8, 8, 0),
        fare_amount=12.5,
        D_pu=0.0,
        trip_distance=0.0,
        beta_f=0.006,
    )

    p_default = acceptance_probability(**common, alpha_sensitivity=1.0)
    p_larger_alpha = acceptance_probability(**common, alpha_sensitivity=1.25)

    assert p_larger_alpha > p_default


def test_required_fare_rejects_pu_impossible_target():
    cell = h3.latlng_to_cell(40.7580, -73.9855, 9)
    impossible_target = 1.0 / pu_correction_constant()

    with pytest.raises(ValueError, match="target_p"):
        required_fare_for_target_p(
            last_dropoff_cell=cell,
            dropoff_cell=cell,
            call_datetime=datetime(2013, 7, 8, 8, 0),
            target_p=impossible_target,
            D_pu=0.3,
            trip_distance=1.8,
            beta_f=0.006,
        )


def test_required_fare_rejects_target_probability_above_one():
    cell = h3.latlng_to_cell(40.7580, -73.9855, 9)

    with pytest.raises(ValueError, match="target_p"):
        required_fare_for_target_p(
            last_dropoff_cell=cell,
            dropoff_cell=cell,
            call_datetime=datetime(2013, 7, 8, 8, 0),
            target_p=1.1,
            D_pu=0.3,
            trip_distance=1.8,
            beta_f=0.006,
        )


def test_required_fare_can_return_negative_for_low_target_p():
    # target_p가 작으면 비가격 효용만으로도 목표 확률이 충족돼 fare가 음수로 나올 수 있다.
    # 호출부가 max(raw_incentive, 0)으로 처리하므로 함수는 음수를 그대로 돌려준다.
    cell = h3.latlng_to_cell(40.7580, -73.9855, 9)
    fare = required_fare_for_target_p(
        last_dropoff_cell=cell,
        dropoff_cell=cell,
        call_datetime=datetime(2013, 7, 8, 8, 0),
        target_p=0.01,
        D_pu=0.3,
        trip_distance=1.8,
        beta_f=0.006,
    )

    p = acceptance_probability(
        last_dropoff_cell=cell,
        dropoff_cell=cell,
        call_datetime=datetime(2013, 7, 8, 8, 0),
        fare_amount=fare,
        D_pu=0.3,
        trip_distance=1.8,
        beta_f=0.006,
    )
    assert p == pytest.approx(0.01)


def test_required_fare_changes_with_direct_fare_beta():
    # 작은 beta_f는 인센티브당 효용 기여가 작아 같은 target_p를 위해 더 큰 fare가 필요하다.
    cell = h3.latlng_to_cell(40.7580, -73.9855, 9)
    common = dict(
        last_dropoff_cell=cell,
        dropoff_cell=cell,
        call_datetime=datetime(2013, 7, 8, 8, 0),
        target_p=0.8,
        D_pu=0.3,
        trip_distance=1.8,
    )

    fare_large_beta = required_fare_for_target_p(**common, beta_f=0.01)
    fare_small_beta = required_fare_for_target_p(**common, beta_f=1e-4)

    assert math.isfinite(fare_large_beta)
    assert math.isfinite(fare_small_beta)
    assert fare_small_beta != pytest.approx(fare_large_beta)


def test_required_fare_allows_zero_direct_fare_beta_when_total_fare_denominator_is_valid():
    cell = h3.latlng_to_cell(40.7580, -73.9855, 9)

    fare = required_fare_for_target_p(
        last_dropoff_cell=cell,
        dropoff_cell=cell,
        call_datetime=datetime(2013, 7, 8, 8, 0),
        target_p=0.8,
        D_pu=0.3,
        trip_distance=1.8,
        beta_f=0.0,
    )

    assert math.isfinite(fare)


@pytest.mark.parametrize("beta_f", [math.inf, -math.inf, math.nan])
def test_required_fare_rejects_non_finite_beta(beta_f):
    cell = h3.latlng_to_cell(40.7580, -73.9855, 9)

    with pytest.raises(ValueError, match="beta_f"):
        required_fare_for_target_p(
            last_dropoff_cell=cell,
            dropoff_cell=cell,
            call_datetime=datetime(2013, 7, 8, 8, 0),
            target_p=0.8,
            D_pu=0.3,
            trip_distance=1.8,
            beta_f=beta_f,
        )


def test_required_fare_rejects_zero_alpha_sensitivity():
    cell = h3.latlng_to_cell(40.7580, -73.9855, 9)

    with pytest.raises(ValueError, match="alpha_sensitivity"):
        required_fare_for_target_p(
            last_dropoff_cell=cell,
            dropoff_cell=cell,
            call_datetime=datetime(2013, 7, 8, 8, 0),
            target_p=0.8,
            D_pu=0.3,
            trip_distance=1.8,
            beta_f=0.006,
            alpha_sensitivity=0.0,
        )
