from dataclasses import dataclass
from typing import Optional


@dataclass
class Passenger:
    id: str
    x: float
    y: float
    lat: float
    lng: float
    pickup_edge: str
    dropoff_edge: str
    dropoff_x: float
    dropoff_y: float
    dropoff_lat: float
    dropoff_lng: float
    expected_distance_m: float
    expected_fare: int
    spawn_time: float
    state: str  # "waiting" | "assigned" | "picked_up" | "completed"
    taxi_id: Optional[str] = None
    h3_pickup: Optional[str] = None
    h3_dropoff: Optional[str] = None
    incentive_limit: Optional[int] = None
