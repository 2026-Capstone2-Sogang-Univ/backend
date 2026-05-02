from dataclasses import dataclass, field

BASE_FARE = 4800
BASE_DIST_M = 1600.0
DIST_UNIT_M = 131.0
DIST_UNIT_FARE = 100
SPEED_THRESHOLD_MPS = 15 * 1000 / 3600  # ≈4.167 m/s
TIME_UNIT_S = 30.0
TIME_UNIT_FARE = 100


@dataclass
class TripAccumulator:
    passenger_id: str
    pickup_sim_time: float
    distance_m: float = 0.0
    low_speed_seconds: float = 0.0
    last_distance_snapshot: float = 0.0


def calculate_fare(a: TripAccumulator) -> int:
    fare = BASE_FARE
    if a.distance_m > BASE_DIST_M:
        fare += int((a.distance_m - BASE_DIST_M) / DIST_UNIT_M) * DIST_UNIT_FARE
    fare += int(a.low_speed_seconds / TIME_UNIT_S) * TIME_UNIT_FARE
    return fare


def estimate_fare(distance_m: float) -> int:
    fare = BASE_FARE
    if distance_m > BASE_DIST_M:
        fare += int((distance_m - BASE_DIST_M) / DIST_UNIT_M) * DIST_UNIT_FARE
    return fare
