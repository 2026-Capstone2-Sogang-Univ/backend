import asyncio
import os
import signal

from fastapi import APIRouter, HTTPException, Request

from ..db.engine import get_pool
from ..grid import H3_RESOLUTION
from ..simulation import SimStatus

router = APIRouter()


@router.post("/start")
async def start_simulation(request: Request):
    manager = request.app.state.manager
    if manager.status == SimStatus.RUNNING:
        raise HTTPException(status_code=409, detail="Simulation is already running")
    await manager.start()
    return {"status": manager.status}


@router.post("/pause")
async def pause_simulation(request: Request):
    manager = request.app.state.manager
    if manager.status != SimStatus.RUNNING:
        raise HTTPException(status_code=400, detail="Simulation is not running")
    await manager.pause()
    return {"status": manager.status}


@router.post("/resume")
async def resume_simulation(request: Request):
    manager = request.app.state.manager
    if manager.status != SimStatus.PAUSED:
        raise HTTPException(status_code=400, detail="Simulation is not paused")
    await manager.resume()
    return {"status": manager.status}


@router.post("/restart")
async def restart_simulation(request: Request):
    manager = request.app.state.manager
    await manager.restart()
    return {"status": manager.status}


@router.post("/stop")
async def stop_simulation(request: Request):
    manager = request.app.state.manager
    await manager.stop()
    return {"status": manager.status}


@router.post("/shutdown")
async def shutdown_server(request: Request):
    await request.app.state.manager.stop()
    asyncio.get_event_loop().call_later(0.5, os.kill, os.getpid(), signal.SIGTERM)
    return {"status": "shutting down"}


@router.get("/status")
async def get_status(request: Request):
    manager = request.app.state.manager
    return {"status": manager.status, **manager.get_state()}


@router.get("/surge")
async def get_surge(request: Request):
    manager = request.app.state.manager
    return {"h3_resolution": H3_RESOLUTION, "cells": manager.get_surge()}


@router.get("/passengers")
async def get_passengers(request: Request):
    manager = request.app.state.manager
    return {"passengers": manager.get_passengers()}


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
