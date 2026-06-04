from dataclasses import dataclass

# ---------------------------------------------------------------------------
# NYC Taxi Meter — 2013 TLC rate card — all monetary amounts in USD cents
# ---------------------------------------------------------------------------

BASE_FARE = 250            # $2.50 initial charge
DIST_UNIT_M = 321.869      # 1/5 mile in meters
DIST_UNIT_FARE = 50        # $0.50 per 1/5 mile
SPEED_THRESHOLD_MPS = 12 * 1609.344 / 3600  # 12 mph ≈ 5.364 m/s
TIME_UNIT_S = 60.0         # 60 seconds at low speed
TIME_UNIT_FARE = 50        # $0.50 per 60 s

# Fixed surcharge (unconditional)
SURCHARGE_MTA = 50                # $0.50 — NY State Tax (all trips)
FIXED_SURCHARGES = SURCHARGE_MTA  # $0.50 total fixed

# Time-based surcharges (2013) — not applied;
#   Peak hour : $1.00  (weekday 4 pm – 8 pm)
#   Night     : $0.50  (8 pm – 6 am)

# Tip — not applied;
# 15% ~ 20%

@dataclass
class TripAccumulator:
    passenger_id: str
    pickup_sim_time: float
    distance_m: float = 0.0
    low_speed_seconds: float = 0.0
    last_distance_snapshot: float = 0.0
    dispatch_id: str | None = None
    dispatch_sim_time: float = 0.0
    surge: float = 1.0
    fare_cap: int | None = None


def calculate_meter_fare(a: TripAccumulator) -> int:
    fare = BASE_FARE
    fare += int(a.distance_m / DIST_UNIT_M) * DIST_UNIT_FARE
    fare += int(a.low_speed_seconds / TIME_UNIT_S) * TIME_UNIT_FARE
    fare += FIXED_SURCHARGES
    return fare


def calculate_fare(a: TripAccumulator) -> int:
    return round(calculate_meter_fare(a) * a.surge)


def estimate_fare(distance_m: float) -> int:
    fare = BASE_FARE
    fare += int(distance_m / DIST_UNIT_M) * DIST_UNIT_FARE
    fare += FIXED_SURCHARGES
    return fare
