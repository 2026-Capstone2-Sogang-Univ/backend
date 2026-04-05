from contextlib import asynccontextmanager

from fastapi import FastAPI

from .simulation import SimulationManager
from .routers import simulation as simulation_router


@asynccontextmanager
async def lifespan(app: FastAPI):
    yield
    # Gracefully shut down the simulation loop on app exit
    await app.state.manager.stop()


app = FastAPI(title="SUMO Service", version="0.1.0", lifespan=lifespan)

manager = SimulationManager()
app.state.manager = manager

app.include_router(simulation_router.router, prefix="/simulation", tags=["simulation"])
