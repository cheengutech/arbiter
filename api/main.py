#!/usr/bin/env python3
"""
Arbiter — FastAPI Backend with WebSocket Alarm Streaming
=========================================================
Runs the BMS simulator (v2) in the background and broadcasts
alarm events to all connected WebSocket clients in real time.

Endpoints:
  GET  /              Health check
  GET  /alarms        Recent alarm history (JSON)
  WS   /ws/alarms     Real-time alarm event stream

Usage:
  uvicorn api.main:app --host 0.0.0.0 --port 8000 --reload

Environment variables:
  SIM_TICK_INTERVAL   Simulator tick in seconds (default: 10)
  MAX_ALARM_HISTORY   Max alarm events kept in memory (default: 500)
"""

import asyncio
import json
import logging
import os
from collections import deque
from contextlib import asynccontextmanager

from typing import Dict, List, Optional, Set

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse

from simulator.bms_simulator_v2 import SimulatorV2, AlarmEvent

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
SIM_TICK = int(os.getenv("SIM_TICK_INTERVAL", "10"))
MAX_HISTORY = int(os.getenv("MAX_ALARM_HISTORY", "500"))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("arbiter.api")


# ---------------------------------------------------------------------------
# Connection manager — broadcasts to all connected WebSocket clients
# ---------------------------------------------------------------------------
class ConnectionManager:
    def __init__(self):
        self._connections: Set[WebSocket] = set()

    async def connect(self, ws: WebSocket):
        await ws.accept()
        self._connections.add(ws)
        log.info("WebSocket client connected (%d total)", len(self._connections))

    def disconnect(self, ws: WebSocket):
        self._connections.discard(ws)
        log.info("WebSocket client disconnected (%d total)", len(self._connections))

    async def broadcast(self, data: dict):
        payload = json.dumps(data)
        stale: List[WebSocket] = []
        for ws in self._connections:
            try:
                await ws.send_text(payload)
            except Exception:
                stale.append(ws)
        for ws in stale:
            self._connections.discard(ws)

    @property
    def count(self) -> int:
        return len(self._connections)


manager = ConnectionManager()
alarm_history: deque[dict] = deque(maxlen=MAX_HISTORY)
simulator: Optional[SimulatorV2] = None
sim_task: Optional[asyncio.Task] = None


# ---------------------------------------------------------------------------
# Alarm callback — called by the simulator for each alarm event
# ---------------------------------------------------------------------------
async def on_alarm(event: AlarmEvent):
    data = event.to_dict()
    alarm_history.appendleft(data)
    await manager.broadcast(data)


# ---------------------------------------------------------------------------
# Lifespan — start/stop the simulator with the app
# ---------------------------------------------------------------------------
@asynccontextmanager
async def lifespan(app: FastAPI):
    global simulator, sim_task
    simulator = SimulatorV2(on_alarm=on_alarm, tick_interval=SIM_TICK)
    sim_task = asyncio.create_task(simulator.run())
    log.info("Arbiter API started — simulator tick=%ds", SIM_TICK)
    yield
    simulator.stop()
    sim_task.cancel()
    try:
        await sim_task
    except asyncio.CancelledError:
        pass
    log.info("Arbiter API shut down.")


# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------
app = FastAPI(
    title="Arbiter BMS Alarm API",
    description="Real-time BACnet alarm event streaming for the Legion of Honor BMS",
    version="2.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# ---------------------------------------------------------------------------
# REST endpoints
# ---------------------------------------------------------------------------
@app.get("/")
async def health():
    return {
        "service": "arbiter",
        "status": "running",
        "simulator": simulator.running if simulator else False,
        "connected_clients": manager.count,
        "alarm_count": len(alarm_history),
    }


@app.get("/alarms")
async def get_alarms(
    limit: int = Query(default=50, ge=1, le=MAX_HISTORY),
    severity: Optional[str] = Query(default=None, description="Filter: CRITICAL, WARNING, INFO"),
    state: Optional[str] = Query(default=None, description="Filter: ACTIVE, CLEARED"),
):
    """Return recent alarm history with optional filters."""
    results = list(alarm_history)
    if severity:
        results = [a for a in results if a["severity"] == severity.upper()]
    if state:
        results = [a for a in results if a["state"] == state.upper()]
    return {"alarms": results[:limit], "total": len(results)}


@app.get("/alarms/active")
async def get_active_alarms():
    """Return currently active (uncleared) alarms by tracking latest state per point."""
    active: Dict[str, dict] = {}
    for alarm in alarm_history:
        key = f"{alarm['device']}:{alarm['point']}"
        if key not in active:
            active[key] = alarm
    return {
        "alarms": [a for a in active.values() if a["state"] == "ACTIVE"],
    }


# ---------------------------------------------------------------------------
# WebSocket endpoint
# ---------------------------------------------------------------------------
@app.websocket("/ws/alarms")
async def ws_alarms(ws: WebSocket):
    await manager.connect(ws)
    try:
        # Send recent alarm history on connect so the client has context
        await ws.send_text(json.dumps({
            "type": "history",
            "alarms": list(alarm_history)[:50],
        }))
        # Keep connection alive; listen for client pings / close
        while True:
            data = await ws.receive_text()
            if data == "ping":
                await ws.send_text("pong")
    except WebSocketDisconnect:
        pass
    finally:
        manager.disconnect(ws)


# ---------------------------------------------------------------------------
# Serve static dashboard if present
# ---------------------------------------------------------------------------
public_dir = os.path.join(os.path.dirname(os.path.dirname(__file__)), "public")
if os.path.isdir(public_dir):
    @app.get("/dashboard")
    async def dashboard():
        index = os.path.join(public_dir, "dashboard.html")
        if os.path.exists(index):
            return FileResponse(index)
        return {"error": "dashboard.html not found"}

    app.mount("/static", StaticFiles(directory=public_dir), name="static")
