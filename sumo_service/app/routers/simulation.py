import asyncio
import os
import signal

from fastapi import APIRouter, HTTPException, Request

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
    result = manager.get_fare(passenger_id)
    if result is None:
        raise HTTPException(status_code=404, detail="Fare not found")
    return result
