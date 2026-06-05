import asyncio
import os
import signal
import math

from fastapi import APIRouter, Body, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from ..db.engine import get_pool
from ..grid import H3_RESOLUTION
from ..h3_regions import load_h3_region_map
from ..simulation import DEFAULT_PRICING_POLICY, SimStatus, SimulationStartOptions

router = APIRouter()

MAX_DURATION_S = 604800.0
MAX_TAXI_COUNT = 1000
MAX_BACKGROUND_VEHICLE_COUNT = 3000
MAX_INITIAL_PASSENGER_COUNT = 5000
MAX_PASSENGERS_PER_5MIN = 1000
MAX_SIMULATION_SPEED = 120.0
PASSENGER_SOURCES = {"random", "parquet"}


class TargetMatchingRatesBody(BaseModel):
    raw_lt_1_5: float | None = None
    raw_lt_2_5: float | None = None
    raw_lt_3_5: float | None = None
    raw_gte_3_5: float | None = None


class PricingPolicyBody(BaseModel):
    epsilon: float | None = None
    surge_min: float | None = None
    surge_max: float | None = None
    alpha_sensitivity: float | None = None


class StartSimulationBody(BaseModel):
    duration: float | None = Field(default=None, gt=0)
    seed: int | None = None
    passenger_source: str | None = None
    target_matching_rates: TargetMatchingRatesBody | None = None
    pricing_policy: PricingPolicyBody | None = None
    taxi_count: int | None = Field(default=None, gt=0)
    background_vehicle_count: int | None = Field(default=None, ge=0)
    passengers_per_5min: int | None = Field(default=None, ge=0)
    simulation_speed: float | None = Field(default=None, gt=0)
    initial_passenger_count: int | None = Field(default=None, ge=0)


class LatLngBody(BaseModel):
    lat: float
    lng: float


class CreatePassengerBody(BaseModel):
    pickup: LatLngBody
    dropoff: LatLngBody
    incentive_limit: int = Field(default=0, ge=0)


class CreateTaxiBody(BaseModel):
    lat: float
    lng: float


def _status_payload(manager):
    return manager.get_status_summary()


def _start_options(body: StartSimulationBody | None) -> SimulationStartOptions | None:
    if body is None:
        return None
    return SimulationStartOptions(
        duration=body.duration,
        seed=body.seed,
        passenger_source=body.passenger_source,
        target_matching_rates=(
            body.target_matching_rates.model_dump(exclude_none=True)
            if body.target_matching_rates else None
        ),
        pricing_policy=(
            body.pricing_policy.model_dump(exclude_none=True)
            if body.pricing_policy else None
        ),
        taxi_count=body.taxi_count,
        background_vehicle_count=body.background_vehicle_count,
        passengers_per_5min=body.passengers_per_5min,
        simulation_speed=body.simulation_speed,
        initial_passenger_count=body.initial_passenger_count,
    )


def _validate_lat_lng(lat: float, lng: float) -> bool:
    return (
        math.isfinite(lat)
        and math.isfinite(lng)
        and -90.0 <= lat <= 90.0
        and -180.0 <= lng <= 180.0
    )


def _error(status_code: int, code: str, message: str) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content={"error": code, "message": message},
    )


def _manual_result_response(result: dict, success_keys: set[str] | None = None):
    if result.get("ok"):
        if success_keys is None:
            return {k: v for k, v in result.items() if k != "ok"}
        return {k: result[k] for k in success_keys if k in result}
    return _error(
        400,
        str(result.get("error", "invalid_request")),
        str(result.get("message", "Request failed")),
    )


def _validate_start_body(body: StartSimulationBody | None) -> JSONResponse | None:
    if body is None:
        return None
    if body.duration is not None and body.duration > MAX_DURATION_S:
        return _error(
            400,
            "invalid_request",
            f"duration must be <= {int(MAX_DURATION_S)} seconds",
        )
    if body.passenger_source is not None and body.passenger_source not in PASSENGER_SOURCES:
        return _error(
            400,
            "invalid_request",
            "passenger_source must be one of: random, parquet",
        )
    if body.taxi_count is not None and body.taxi_count > MAX_TAXI_COUNT:
        return _error(
            400,
            "invalid_request",
            f"taxi_count must be <= {MAX_TAXI_COUNT}",
        )
    if (
        body.background_vehicle_count is not None
        and body.background_vehicle_count > MAX_BACKGROUND_VEHICLE_COUNT
    ):
        return _error(
            400,
            "invalid_request",
            f"background_vehicle_count must be <= {MAX_BACKGROUND_VEHICLE_COUNT}",
        )
    if (
        body.passengers_per_5min is not None
        and body.passengers_per_5min > MAX_PASSENGERS_PER_5MIN
    ):
        return _error(
            400,
            "invalid_request",
            f"passengers_per_5min must be <= {MAX_PASSENGERS_PER_5MIN}",
        )
    if body.simulation_speed is not None:
        if not math.isfinite(body.simulation_speed):
            return _error(400, "invalid_request", "simulation_speed must be finite")
        if body.simulation_speed > MAX_SIMULATION_SPEED:
            return _error(
                400,
                "invalid_request",
                f"simulation_speed must be <= {MAX_SIMULATION_SPEED:g}",
            )
    if (
        body.initial_passenger_count is not None
        and body.initial_passenger_count > MAX_INITIAL_PASSENGER_COUNT
    ):
        return _error(
            400,
            "invalid_request",
            f"initial_passenger_count must be <= {MAX_INITIAL_PASSENGER_COUNT}",
        )
    if body.target_matching_rates is not None:
        for key, value in body.target_matching_rates.model_dump(exclude_none=True).items():
            if not math.isfinite(value) or not 0.0 <= value <= 1.0:
                return _error(
                    400,
                    "invalid_request",
                    f"target_matching_rates.{key} must be between 0 and 1",
                )
    if body.pricing_policy is not None:
        policy = body.pricing_policy.model_dump(exclude_none=True)
        merged_policy = dict(DEFAULT_PRICING_POLICY)
        merged_policy.update(policy)
        for key, value in policy.items():
            if not math.isfinite(value):
                return _error(400, "invalid_request", f"pricing_policy.{key} must be finite")
        if abs(merged_policy["epsilon"]) < 1e-12:
            return _error(400, "invalid_request", "pricing_policy.epsilon must be non-zero")
        if "surge_min" in policy and policy["surge_min"] < 1.0:
            return _error(400, "invalid_request", "pricing_policy.surge_min must be >= 1")
        if "surge_max" in policy and policy["surge_max"] < 1.0:
            return _error(400, "invalid_request", "pricing_policy.surge_max must be >= 1")
        if merged_policy["surge_min"] > merged_policy["surge_max"]:
            return _error(
                400,
                "invalid_request",
                "pricing_policy.surge_min must be <= surge_max",
            )
        if "alpha_sensitivity" in policy and policy["alpha_sensitivity"] <= 0:
            return _error(
                400,
                "invalid_request",
                "pricing_policy.alpha_sensitivity must be > 0",
            )
    return None


@router.post("/start")
async def start_simulation(
    request: Request,
    body: StartSimulationBody | None = Body(default=None),
):
    manager = request.app.state.manager
    if manager.status == SimStatus.RUNNING:
        raise HTTPException(status_code=409, detail="Simulation is already running")
    validation_error = _validate_start_body(body)
    if validation_error is not None:
        return validation_error
    await manager.start(_start_options(body))
    return _status_payload(manager)


@router.post("/pause")
async def pause_simulation(request: Request):
    manager = request.app.state.manager
    if manager.status != SimStatus.RUNNING:
        raise HTTPException(status_code=400, detail="Simulation is not running")
    await manager.pause()
    return _status_payload(manager)


@router.post("/resume")
async def resume_simulation(request: Request):
    manager = request.app.state.manager
    if manager.status != SimStatus.PAUSED:
        raise HTTPException(status_code=400, detail="Simulation is not paused")
    await manager.resume()
    return _status_payload(manager)


@router.post("/restart")
async def restart_simulation(
    request: Request,
    body: StartSimulationBody | None = Body(default=None),
):
    manager = request.app.state.manager
    validation_error = _validate_start_body(body)
    if validation_error is not None:
        return validation_error
    await manager.restart(_start_options(body))
    return _status_payload(manager)


@router.post("/stop")
async def stop_simulation(request: Request):
    manager = request.app.state.manager
    await manager.stop()
    return _status_payload(manager)


@router.post("/shutdown")
async def shutdown_server(request: Request):
    await request.app.state.manager.stop()
    asyncio.get_event_loop().call_later(0.5, os.kill, os.getpid(), signal.SIGTERM)
    return {"status": "shutting down"}


@router.get("/status")
async def get_status(request: Request):
    manager = request.app.state.manager
    return _status_payload(manager)


@router.get("/kpi")
async def get_kpi(request: Request):
    manager = request.app.state.manager
    return manager.get_kpi_summary()


@router.get("/surge")
async def get_surge(request: Request):
    manager = request.app.state.manager
    return {"h3_resolution": H3_RESOLUTION, "cells": manager.get_surge()}


@router.get("/h3-regions")
async def get_h3_regions():
    return {
        "h3_resolution": H3_RESOLUTION,
        "regions": load_h3_region_map(),
        "fallback": "lat_lng",
    }


@router.get("/passengers")
async def get_passengers(request: Request):
    manager = request.app.state.manager
    return {"passengers": manager.get_passengers()}


@router.post("/passengers/quote")
async def quote_passenger(body: CreatePassengerBody, request: Request):
    manager = request.app.state.manager
    if manager.status != SimStatus.RUNNING:
        return _error(400, "simulation_not_running", "Simulation is not running")
    if not _validate_lat_lng(body.pickup.lat, body.pickup.lng):
        return _error(400, "invalid_request", "pickup lat/lng is invalid")
    if not _validate_lat_lng(body.dropoff.lat, body.dropoff.lng):
        return _error(400, "invalid_request", "dropoff lat/lng is invalid")
    result = await asyncio.to_thread(
        manager.quote_manual_passenger,
        pickup_lat=body.pickup.lat,
        pickup_lng=body.pickup.lng,
        dropoff_lat=body.dropoff.lat,
        dropoff_lng=body.dropoff.lng,
        incentive_limit=body.incentive_limit,
    )
    return _manual_result_response(
        result,
        {
            "expected_fare",
            "expected_distance_m",
            "estimated_wait_sec",
            "surge_multiplier",
            "incentive_limit",
            "total_amount",
        },
    )


@router.post("/passengers")
async def create_passenger(body: CreatePassengerBody, request: Request):
    manager = request.app.state.manager
    if manager.status != SimStatus.RUNNING:
        return _error(400, "simulation_not_running", "Simulation is not running")
    if not _validate_lat_lng(body.pickup.lat, body.pickup.lng):
        return _error(400, "invalid_request", "pickup lat/lng is invalid")
    if not _validate_lat_lng(body.dropoff.lat, body.dropoff.lng):
        return _error(400, "invalid_request", "dropoff lat/lng is invalid")
    result = await asyncio.to_thread(
        manager.create_manual_passenger,
        pickup_lat=body.pickup.lat,
        pickup_lng=body.pickup.lng,
        dropoff_lat=body.dropoff.lat,
        dropoff_lng=body.dropoff.lng,
        incentive_limit=body.incentive_limit,
    )
    return _manual_result_response(result, {"passenger_id"})


@router.post("/taxis")
async def create_taxi(body: CreateTaxiBody, request: Request):
    manager = request.app.state.manager
    if manager.status != SimStatus.RUNNING:
        return _error(400, "simulation_not_running", "Simulation is not running")
    if not _validate_lat_lng(body.lat, body.lng):
        return _error(400, "invalid_request", "lat/lng is invalid")
    result = await asyncio.to_thread(manager.create_manual_taxi, lat=body.lat, lng=body.lng)
    return _manual_result_response(result, {"taxi_id"})


@router.get("/taxis/{taxi_id}/standby")
async def get_taxi_standby(taxi_id: str, request: Request):
    manager = request.app.state.manager
    detail = manager.get_taxi_standby_context(taxi_id)
    if detail is None:
        return _error(404, "not_found", "Taxi standby context was not found")
    return detail


@router.get("/taxis/{taxi_id}/call")
async def get_taxi_call(taxi_id: str, request: Request):
    manager = request.app.state.manager
    detail = manager.get_taxi_call_detail(taxi_id)
    if detail is None:
        return _error(404, "not_found", "Current call was not found")
    return detail


@router.get("/fare/{passenger_id}")
async def get_fare(passenger_id: str, request: Request):
    manager = request.app.state.manager
    pool = get_pool()
    if pool is not None:
        run_id = manager._run_id
        if run_id is None:
            raise HTTPException(status_code=404, detail="Fare not found")
        row = await pool.fetchrow(
            "SELECT taxi_id, meter_fare, fare, surge, expected_fare, distance_m, dropoff_sim_time "
            "FROM trip WHERE passenger_id = $1 AND run_id = $2 LIMIT 1",
            passenger_id, run_id,
        )
        if row is None:
            raise HTTPException(status_code=404, detail="Fare not found")
        return {
            "passenger_id": passenger_id,
            "taxi_id": row["taxi_id"],
            "meter_fare": row["meter_fare"],
            "fare": row["fare"],
            "surge": row["surge"],
            "expected_fare": row["expected_fare"],
            "distance_m": row["distance_m"],
            "sim_time": row["dropoff_sim_time"],
        }
    result = manager.get_fare(passenger_id)
    if result is None:
        raise HTTPException(status_code=404, detail="Fare not found")
    return result
