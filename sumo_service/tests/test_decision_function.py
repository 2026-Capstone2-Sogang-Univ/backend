from datetime import datetime

import h3
import pytest

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
        pickup_cell=cell,
        dropoff_cell=cell,
        call_datetime=datetime(2013, 7, 8, 8, 0),
        target_p=target_p,
        D_pu=0.3,
        trip_distance=1.8,
        beta_f=beta_f,
    )
    p = acceptance_probability(
        last_dropoff_cell=cell,
        pickup_cell=cell,
        dropoff_cell=cell,
        call_datetime=datetime(2013, 7, 8, 8, 0),
        fare_amount=fare,
        D_pu=0.3,
        trip_distance=1.8,
        beta_f=beta_f,
    )

    assert p == pytest.approx(target_p)


def test_required_fare_rejects_pu_impossible_target():
    cell = h3.latlng_to_cell(40.7580, -73.9855, 9)
    impossible_target = 1.0 / pu_correction_constant()

    with pytest.raises(ValueError, match="target_p"):
        required_fare_for_target_p(
            last_dropoff_cell=cell,
            pickup_cell=cell,
            dropoff_cell=cell,
            call_datetime=datetime(2013, 7, 8, 8, 0),
            target_p=impossible_target,
            D_pu=0.3,
            trip_distance=1.8,
            beta_f=0.006,
        )


def test_required_fare_rejects_zero_beta():
    cell = h3.latlng_to_cell(40.7580, -73.9855, 9)

    with pytest.raises(ValueError, match="beta_f"):
        required_fare_for_target_p(
            last_dropoff_cell=cell,
            pickup_cell=cell,
            dropoff_cell=cell,
            call_datetime=datetime(2013, 7, 8, 8, 0),
            target_p=0.8,
            D_pu=0.3,
            trip_distance=1.8,
            beta_f=0.0,
        )
