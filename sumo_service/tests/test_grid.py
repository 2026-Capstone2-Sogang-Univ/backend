import pytest

from app.grid import compute_surge


def test_surge_zero_zero():
    assert compute_surge(0, 0) == 1.0


def test_surge_no_supply_with_demand():
    assert compute_surge(0, 5) == 5.0


def test_surge_supply_no_demand():
    assert compute_surge(5, 0) == 0.0


def test_surge_equal_supply_demand():
    # (4/4)^(1/0.6) = 1.0^1.667 = 1.0
    assert compute_surge(4, 4) == pytest.approx(1.0)


def test_surge_more_demand_than_supply():
    # (2/1)^1.667 ≈ 3.175 > 1.0
    assert compute_surge(1, 2) > 1.0


def test_surge_less_demand_than_supply():
    # (1/2)^1.667 ≈ 0.315 < 1.0
    assert compute_surge(2, 1) < 1.0


def test_surge_capped_at_max():
    # demand=100, supply=1 → well above max_surge
    assert compute_surge(1, 100) == 5.0


def test_surge_custom_max_surge():
    # supply=0, demand>0 → max_surge
    assert compute_surge(0, 3, max_surge=3.0) == 3.0
    # large ratio capped at custom max
    assert compute_surge(1, 100, max_surge=2.0) == 2.0
