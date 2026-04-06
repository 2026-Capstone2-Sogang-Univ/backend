import asyncio
import json
import sys
import threading
from contextlib import asynccontextmanager

from fastapi import FastAPI, WebSocket, WebSocketDisconnect

from .simulation import SimulationManager, SimStatus
from .routers import simulation as simulation_router


class ConnectionManager:
    """Tracks active WebSocket connections and broadcasts messages to all of them."""

    def __init__(self) -> None:
        self._clients: list[WebSocket] = []

    async def connect(self, ws: WebSocket) -> None:
        await ws.accept()
        self._clients.append(ws)

    def disconnect(self, ws: WebSocket) -> None:
        if ws in self._clients:
            self._clients.remove(ws)

    async def broadcast(self, message: dict) -> None:
        data = json.dumps(message)
        dead: list[WebSocket] = []
        for ws in list(self._clients):
            try:
                await ws.send_text(data)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.disconnect(ws)

    def has_clients(self) -> bool:
        return bool(self._clients)


async def _broadcast_loop(sim: SimulationManager, conn: ConnectionManager) -> None:
    """Background task: broadcast simulation state to all WebSocket clients every 100ms."""
    prev_status: SimStatus | None = None
    while True:
        await asyncio.sleep(0.1)
        if not conn.has_clients():
            prev_status = sim.status
            continue

        state = sim.get_state()
        await conn.broadcast({"type": "vehicles", "vehicles": state["vehicles"]})
        await conn.broadcast({"type": "passengers", "passengers": state["passengers"]})

        if sim.status == SimStatus.FINISHED and prev_status != SimStatus.FINISHED:
            await conn.broadcast({"type": "finished"})

        prev_status = sim.status


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
    broadcast_task = asyncio.create_task(
        _broadcast_loop(app.state.manager, app.state.conn_manager)
    )
    cli_thread = threading.Thread(
        target=_cli_loop,
        args=(loop, app.state.manager),
        daemon=True,
        name="cli-stdin",
    )
    cli_thread.start()
    yield
    broadcast_task.cancel()
    try:
        await broadcast_task
    except asyncio.CancelledError:
        pass
    await app.state.manager.stop()


app = FastAPI(title="SUMO Service", version="0.1.0", lifespan=lifespan)

manager = SimulationManager()
conn_manager = ConnectionManager()
app.state.manager = manager
app.state.conn_manager = conn_manager

app.include_router(simulation_router.router, prefix="/simulation", tags=["simulation"])


@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket) -> None:
    conn: ConnectionManager = websocket.app.state.conn_manager
    sim: SimulationManager = websocket.app.state.manager
    await conn.connect(websocket)
    try:
        boundary = sim.get_boundary()
        await websocket.send_text(json.dumps({"type": "boundary", **boundary}))
        # Keep the connection open; broadcast_loop handles outgoing messages.
        # We discard any incoming messages from the client.
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        pass
    except Exception:
        pass
    finally:
        conn.disconnect(websocket)
