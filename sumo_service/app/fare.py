from dataclasses import dataclass

# ---------------------------------------------------------------------------
# NYC Taxi Meter — all monetary amounts in USD cents
# ---------------------------------------------------------------------------

# Base meter rate
BASE_FARE = 300            # $3.00 initial charge
DIST_UNIT_M = 321.869      # 1/5 mile in meters
DIST_UNIT_FARE = 70        # $0.70 per 1/5 mile
SPEED_THRESHOLD_MPS = 12 * 1609.344 / 3600  # 12 mph ≈ 5.364 m/s
TIME_UNIT_S = 60.0         # 60 seconds at low speed
TIME_UNIT_FARE = 70        # $0.70 per 60 s

# Mandatory surcharges (unconditional)
SURCHARGE_IMPROVEMENT = 100   # $1.00 — improvement surcharge (all trips)
SURCHARGE_MTA = 50            # $0.50 — MTA surcharge (NYC trips, always in Manhattan)
FIXED_SURCHARGES = SURCHARGE_IMPROVEMENT + SURCHARGE_MTA  # $1.50 = 150¢

# Conditional surcharges — not applied in simulation (geographic check omitted)
#   NYS Congestion Surcharge : $2.50  (south of 96th St, Manhattan)
#   CBD Congestion Toll       : $0.75  (south of 60th St, Manhattan)

# Time-based surcharges — not applied in simulation
#   Night surcharge      : $1.00  (8 pm – 6 am)
#   Rush hour surcharge  : $2.50  (weekdays 4 pm – 8 pm)


@dataclass
class TripAccumulator:
    passenger_id: str
    pickup_sim_time: float
    distance_m: float = 0.0
    low_speed_seconds: float = 0.0
    last_distance_snapshot: float = 0.0


def calculate_fare(a: TripAccumulator) -> int:
    fare = BASE_FARE
    fare += int(a.distance_m / DIST_UNIT_M) * DIST_UNIT_FARE
    fare += int(a.low_speed_seconds / TIME_UNIT_S) * TIME_UNIT_FARE
    fare += FIXED_SURCHARGES
    return fare


def estimate_fare(distance_m: float) -> int:
    fare = BASE_FARE
    fare += int(distance_m / DIST_UNIT_M) * DIST_UNIT_FARE
    fare += FIXED_SURCHARGES
    return fare
