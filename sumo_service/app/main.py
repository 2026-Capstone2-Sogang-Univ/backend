from contextlib import asynccontextmanager

from fastapi import FastAPI

from .connection_manager import ConnectionManager
from .simulation import SimulationManager
from .routers import simulation as simulation_router
from .routers import ws as ws_router


@asynccontextmanager
async def lifespan(app: FastAPI):
    yield
    # Gracefully shut down the simulation loop on app exit
    await app.state.manager.stop()


app = FastAPI(title="SUMO Service", version="0.1.0", lifespan=lifespan)

connection_manager = ConnectionManager()
manager = SimulationManager()
manager.connection_manager = connection_manager

app.state.manager = manager
app.state.connection_manager = connection_manager

app.include_router(simulation_router.router, prefix="/simulation", tags=["simulation"])
app.include_router(ws_router.router, tags=["websocket"])
