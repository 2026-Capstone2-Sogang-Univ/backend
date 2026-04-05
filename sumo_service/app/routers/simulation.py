from fastapi import APIRouter, HTTPException, Request

from ..simulation import SimStatus

router = APIRouter()


@router.post("/start")
async def start_simulation(request: Request):
    manager = request.app.state.manager
    if manager.status == SimStatus.RUNNING:
        raise HTTPException(status_code=400, detail="Simulation is already running")
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


@router.get("/status")
async def get_status(request: Request):
    manager = request.app.state.manager
    return {"status": manager.status, **manager.get_state()}
