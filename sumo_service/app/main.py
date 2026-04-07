import asyncio
import sys
import threading
from contextlib import asynccontextmanager

from fastapi import FastAPI

from .connection_manager import ConnectionManager
from .simulation import SimulationManager
from .routers import simulation as simulation_router
from .routers import ws as ws_router


def _cli_loop(loop: asyncio.AbstractEventLoop, sim: SimulationManager) -> None:
    """Read commands from stdin in a background thread and dispatch to SimulationManager."""
    print("Console ready. Commands: start | pause | resume | restart", flush=True)
    for line in sys.stdin:
        cmd = line.strip().lower()
        if cmd == "start":
            asyncio.run_coroutine_threadsafe(sim.start(), loop)
            print(">> start", flush=True)
        elif cmd == "pause":
            asyncio.run_coroutine_threadsafe(sim.pause(), loop)
            print(">> pause", flush=True)
        elif cmd == "resume":
            asyncio.run_coroutine_threadsafe(sim.resume(), loop)
            print(">> resume", flush=True)
        elif cmd == "restart":
            asyncio.run_coroutine_threadsafe(sim.restart(), loop)
            print(">> restart", flush=True)
        else:
            print(f"Unknown command: {cmd!r}. Try: start | pause | resume | restart", flush=True)


@asynccontextmanager
async def lifespan(app: FastAPI):
    loop = asyncio.get_event_loop()
    cli_thread = threading.Thread(
        target=_cli_loop,
        args=(loop, app.state.manager),
        daemon=True,
        name="cli-stdin",
    )
    cli_thread.start()
    yield
    await app.state.manager.stop()


app = FastAPI(title="SUMO Service", version="0.1.0", lifespan=lifespan)

connection_manager = ConnectionManager()
manager = SimulationManager()
manager.connection_manager = connection_manager

app.state.manager = manager
app.state.connection_manager = connection_manager

app.include_router(simulation_router.router, prefix="/simulation", tags=["simulation"])
app.include_router(ws_router.router, tags=["websocket"])
