"""주요 표시(GET) 엔드포인트의 OpenAPI 응답 스키마 모델.

라우터 핸들러는 manager가 만든 dict를 그대로 반환하므로, response_model을 붙이지
않으면 openapi.json의 응답 본문 스키마가 빈 객체({})로 나간다. 여기 모델들은
`app/simulation.py`의 실제 반환 dict와 1:1로 맞춘 것이다.

주의: 숫자 필드 중 빈 리스트 `sum()`(→ int 0)이나 cent 정수처럼 int/float가
섞일 수 있는 값은 `Number`(int | float)로 둔다. Pydantic v2 smart union이 입력
타입을 보존하므로 response_model 직렬화가 실제 응답 값(예: 250 → 250.0)을 바꾸지
않게 하기 위함이다.
"""

from typing import Any

from pydantic import BaseModel

from ..simulation import SimStatus

# 빈 리스트 sum(→ int 0) / cent 정수 등 int·float가 섞일 수 있는 값.
Number = int | float


class LatLng(BaseModel):
    lat: float
    lng: float


class VehicleSnapshot(BaseModel):
    id: str
    lat: float
    lng: float
    angle: float
    speed: float
    state: str


class PassengerSnapshot(BaseModel):
    id: str
    lat: float
    lng: float
    expected_fare: Number
    expected_distance_m: Number


# --- GET /simulation/status (+ start/pause/resume/restart/stop) -------------


class StatusResponse(BaseModel):
    status: SimStatus
    sim_time: float
    vehicles: list[VehicleSnapshot]
    passengers: list[PassengerSnapshot]
    frame_rate: float
    simulation_speed: float
    vehicle_count: int
    taxi_count: int
    background_vehicle_count: int
    configured_background_vehicle_count: int
    empty_taxi_count: int
    dispatched_taxi_count: int
    occupied_taxi_count: int
    waiting_passenger_count: int
    assigned_passenger_count: int
    completed_trip_count: int
    h3_resolution: int
    passenger_source: str
    passengers_per_5min: int
    passenger_lambda: int
    target_matching_rates: dict[str, float]
    pricing_policy: dict[str, float]
    duration: float | None
    seed: int | None
    initial_passenger_count: int


# --- GET /simulation/kpi ----------------------------------------------------


class KpiWindow(BaseModel):
    start_seconds: float
    end_seconds: float
    duration_seconds: float
    available: bool


class KpiRawBucket(BaseModel):
    bucket: str
    target_rate: float
    actual_rate: float
    matching_rate_error: float
    request_count: int
    matched_count: int
    average_wait_seconds: float
    p95_wait_seconds: float
    marginal_utility_points: list[Any]


class KpiMatching(BaseModel):
    target_rate: float
    actual_rate: float
    matching_rate_error: float
    request_count: int
    matched_count: int
    by_raw_bucket: list[KpiRawBucket]


class KpiIdleTime(BaseModel):
    baseline_seconds: float | None
    with_incentive_seconds: Number
    with_surge_seconds: Number
    average_empty_taxi_seconds: float


class KpiDriverRevenue(BaseModel):
    average_cents: int
    total_cents: int
    completed_trip_count: int
    by_alpha_bucket: list[Any]


class KpiPassengerWaiting(BaseModel):
    average_wait_seconds: float
    p95_wait_seconds: float
    marginal_utility_points: list[Any]


class KpiCellBucket(BaseModel):
    bucket: str
    unique_cell_count: int
    avg_raw_surge: float
    avg_supply: float
    avg_demand: float
    dispatch_request_count: int
    sample_h3_cells: list[str]


class KpiCells(BaseModel):
    by_raw_bucket: list[KpiCellBucket]


class KpiResponse(BaseModel):
    sim_time: float
    window: KpiWindow
    h3_resolution: int
    matching: KpiMatching
    idle_time: KpiIdleTime
    driver_revenue: KpiDriverRevenue
    passenger_waiting_incentive: KpiPassengerWaiting
    passenger_waiting: KpiPassengerWaiting
    cells: KpiCells
    parquet_replay: dict[str, Number]


# --- GET /simulation/surge --------------------------------------------------


class SurgeCell(BaseModel):
    h3: str
    bucket: str
    supply: int
    demand: Number
    actual_demand: Number
    surge: float
    raw_surge: float
    target_matching_rate: float
    center: LatLng


class SurgeResponse(BaseModel):
    h3_resolution: int
    cells: list[SurgeCell]


# --- GET /simulation/passengers ---------------------------------------------


class PassengersResponse(BaseModel):
    passengers: list[PassengerSnapshot]


# --- GET /simulation/demand-forecast ----------------------------------------


class DemandForecastCell(BaseModel):
    h3: str
    predicted_demand: Number
    center: LatLng


class DemandForecastBucket(BaseModel):
    step: int
    target_time: str
    offset_min: int
    cell_count: int
    cells: list[DemandForecastCell]


class DemandForecastResponse(BaseModel):
    enabled: bool
    generated_sim_time: float | None
    base_time: str | None
    horizon_min: int
    h3_resolution: int
    buckets: list[DemandForecastBucket]
