#!/usr/bin/env python3
"""
BMS Simulator v2 — Headless Alarm Event Generator
===================================================
Pure-Python simulation of Legion of Honor BMS alarm patterns,
decoupled from BACnet transport. Produces structured alarm events
suitable for WebSocket broadcast.

Based on real Tracer Synchrony history Feb 26 - Mar 21 2026:
  - SpaceRH cycles 51->61->57% every 30-90 min
  - DuctRH high-limit fault when OA humidity peaks
  - SafetyInterlock toggle every 2-4 hours
  - AHU-1 supply fan failure cluster every ~36 hours
  - Cooling Tower high-temp alarm

Usage:
  from simulator.bms_simulator_v2 import SimulatorV2
  sim = SimulatorV2(on_alarm=my_callback)
  await sim.run()       # runs forever, calling on_alarm for each event
"""

import asyncio
import math
import random
import logging
import uuid
from datetime import datetime, timezone
from dataclasses import dataclass, field, asdict
from enum import Enum
from typing import Callable, Awaitable, Optional, Union

log = logging.getLogger("sim_v2")

TICK = 10  # seconds per simulation step


# ---------------------------------------------------------------------------
# Alarm event model
# ---------------------------------------------------------------------------
class Severity(str, Enum):
    CRITICAL = "CRITICAL"
    WARNING = "WARNING"
    INFO = "INFO"


class AlarmState(str, Enum):
    ACTIVE = "ACTIVE"
    CLEARED = "CLEARED"


@dataclass
class AlarmEvent:
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    timestamp: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    device: str = ""
    point: str = ""
    description: str = ""
    severity: str = "WARNING"
    state: str = "ACTIVE"
    value: Optional[Union[float, str]] = None
    threshold: Optional[Union[float, str]] = None
    unit: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


# ---------------------------------------------------------------------------
# Math helpers (from v1)
# ---------------------------------------------------------------------------
def _sin(t, period, amp, base):
    return base + amp * math.sin(2 * math.pi * t / period)


def _jit(v, sigma=0.15):
    return v + random.gauss(0, sigma)


def _clamp(v, lo, hi):
    return max(lo, min(hi, v))


# ---------------------------------------------------------------------------
# Simulator V2
# ---------------------------------------------------------------------------
class SimulatorV2:
    """Headless BMS simulator that produces AlarmEvent objects via callback."""

    def __init__(
        self,
        on_alarm: Optional[Callable[[AlarmEvent], Awaitable[None]]] = None,
        tick_interval: float = TICK,
    ):
        self.on_alarm = on_alarm
        self.tick_interval = tick_interval
        self.t = 0.0
        self.running = False

        # --- DriSteem humidity cycling ---
        self.space_rh = 51.0
        self._hum_phase = 0.0
        self._hum_cycle = 55.0
        self._alarm_space_rh = False

        # --- Duct RH ---
        self.duct_rh = 66.5
        self._alarm_duct_rh = False

        # --- Safety interlock ---
        self.interlock = True
        self._alarm_interlock = False
        self._ilock_timer = 3600.0 * 3

        # --- AHU-1 supply fan ---
        self.fan_status = True
        self._fan_fail_timer = 3600.0 * 36
        self._fan_fail_active = False
        self._fan_fail_count = 0
        self._alarm_fan = False

        # --- Cooling tower ---
        self.ct_ent_tmp = 87.2
        self._alarm_ct = False

        # --- UC600 humidity sensors ---
        self.teh2_hum = 50.1
        self.teh1_hum = 49.6

        # --- Gallery zone RH (2-05, 2-06 chronic high) ---
        self.gal_rh = {
            "2-05": 57.3, "2-06": 55.3,
        }
        self._alarm_gal = {"2-05": False, "2-06": False}

    async def _emit(self, event: AlarmEvent):
        if self.on_alarm:
            await self.on_alarm(event)

    def _tick(self) -> list[AlarmEvent]:
        """Advance simulation by one step. Returns list of new alarm events."""
        self.t += self.tick_interval
        events: list[AlarmEvent] = []

        # ===== SpaceRH humidity cycling (51 -> 61 -> 57) =====
        self._hum_phase += self.tick_interval / 60.0
        cycle = self._hum_cycle
        phase = (self._hum_phase % cycle) / cycle
        if phase < 0.60:
            rh_base = 51.0 + (phase / 0.60) * 10.0
        else:
            rh_base = 61.0 - ((phase - 0.60) / 0.40) * 4.0
        self.space_rh = _clamp(_jit(rh_base, 0.3), 48.0, 63.0)

        if self.space_rh >= 60.0 and not self._alarm_space_rh:
            self._alarm_space_rh = True
            events.append(AlarmEvent(
                device="DriSteem VL6 (11002)",
                point="SpaceRH",
                description=f"Gallery SpaceRH HIGH: {self.space_rh:.1f}%",
                severity=Severity.CRITICAL,
                state=AlarmState.ACTIVE,
                value=round(self.space_rh, 1),
                threshold=60.0,
                unit="%RH",
            ))
        elif self.space_rh <= 59.0 and self._alarm_space_rh:
            self._alarm_space_rh = False
            events.append(AlarmEvent(
                device="DriSteem VL6 (11002)",
                point="SpaceRH",
                description=f"Gallery SpaceRH normal: {self.space_rh:.1f}%",
                severity=Severity.INFO,
                state=AlarmState.CLEARED,
                value=round(self.space_rh, 1),
                threshold=60.0,
                unit="%RH",
            ))

        if self._hum_phase % cycle < (self.tick_interval / 60.0):
            self._hum_cycle = random.uniform(30.0, 90.0)

        # ===== DuctRH high-limit =====
        self.duct_rh = _clamp(_jit(_sin(self.t, 14400, 8.0, 66.5)), 45.0, 90.0)
        if self.duct_rh >= 80.0 and not self._alarm_duct_rh:
            self._alarm_duct_rh = True
            events.append(AlarmEvent(
                device="DriSteem VL6 (11002)",
                point="DuctRH",
                description=f"DuctRH High Limit: {self.duct_rh:.1f}%",
                severity=Severity.WARNING,
                state=AlarmState.ACTIVE,
                value=round(self.duct_rh, 1),
                threshold=80.0,
                unit="%RH",
            ))
        elif self.duct_rh < 78.0 and self._alarm_duct_rh:
            self._alarm_duct_rh = False
            events.append(AlarmEvent(
                device="DriSteem VL6 (11002)",
                point="DuctRH",
                description=f"DuctRH normal: {self.duct_rh:.1f}%",
                severity=Severity.INFO,
                state=AlarmState.CLEARED,
                value=round(self.duct_rh, 1),
                threshold=80.0,
                unit="%RH",
            ))

        # ===== Safety interlock =====
        self._ilock_timer -= self.tick_interval
        if self._ilock_timer <= 0:
            self.interlock = not self.interlock
            if not self.interlock:
                self._alarm_interlock = True
                self._ilock_timer = random.uniform(120, 300)
                events.append(AlarmEvent(
                    device="DriSteem VL6 (11002)",
                    point="SafetyInterlock",
                    description="Humidifier SafetyInterlock INACTIVE",
                    severity=Severity.CRITICAL,
                    state=AlarmState.ACTIVE,
                    value="inactive",
                    threshold="active",
                ))
            else:
                self._alarm_interlock = False
                self._ilock_timer = random.uniform(7200, 14400)
                events.append(AlarmEvent(
                    device="DriSteem VL6 (11002)",
                    point="SafetyInterlock",
                    description="SafetyInterlock restored",
                    severity=Severity.INFO,
                    state=AlarmState.CLEARED,
                    value="active",
                    threshold="active",
                ))

        # ===== AHU-1 supply fan failure cluster =====
        self._fan_fail_timer -= self.tick_interval
        if self._fan_fail_timer <= 0 and not self._fan_fail_active:
            self._fan_fail_active = True
            self._fan_fail_count = random.randint(4, 6)
            events.append(AlarmEvent(
                device="LOH SC+ Proxy (3001)",
                point="AHU-1 Supply Fan",
                description="AHU-1 Supply Fan Failure (cluster start)",
                severity=Severity.CRITICAL,
                state=AlarmState.ACTIVE,
                value="off",
                threshold="running",
            ))
            self._alarm_fan = True
            self.fan_status = False
        if self._fan_fail_active:
            self._fan_fail_count -= 1
            self.fan_status = not self.fan_status
            if self.fan_status and self._alarm_fan:
                events.append(AlarmEvent(
                    device="LOH SC+ Proxy (3001)",
                    point="AHU-1 Supply Fan",
                    description="AHU-1 Supply Fan returned to normal",
                    severity=Severity.INFO,
                    state=AlarmState.CLEARED,
                    value="running",
                    threshold="running",
                ))
            elif not self.fan_status and not self._alarm_fan:
                events.append(AlarmEvent(
                    device="LOH SC+ Proxy (3001)",
                    point="AHU-1 Supply Fan",
                    description="AHU-1 Supply Fan Failure",
                    severity=Severity.CRITICAL,
                    state=AlarmState.ACTIVE,
                    value="off",
                    threshold="running",
                ))
            self._alarm_fan = not self.fan_status
            if self._fan_fail_count <= 0:
                self._fan_fail_active = False
                self.fan_status = True
                self._alarm_fan = False
                self._fan_fail_timer = random.uniform(3600 * 24, 3600 * 48)

        # ===== Cooling tower high temp =====
        self.ct_ent_tmp = _jit(_sin(self.t, 3600, 3.0, 87.2))
        if self.ct_ent_tmp >= 90.0 and not self._alarm_ct:
            self._alarm_ct = True
            events.append(AlarmEvent(
                device="LOH SC+ Proxy (3001)",
                point="CT-1 Entering Water Temp",
                description=f"Cooling Tower High Temp: {self.ct_ent_tmp:.1f}\u00b0F",
                severity=Severity.WARNING,
                state=AlarmState.ACTIVE,
                value=round(self.ct_ent_tmp, 1),
                threshold=90.0,
                unit="\u00b0F",
            ))
        elif self.ct_ent_tmp < 89.0 and self._alarm_ct:
            self._alarm_ct = False
            events.append(AlarmEvent(
                device="LOH SC+ Proxy (3001)",
                point="CT-1 Entering Water Temp",
                description=f"Cooling Tower temp normal: {self.ct_ent_tmp:.1f}\u00b0F",
                severity=Severity.INFO,
                state=AlarmState.CLEARED,
                value=round(self.ct_ent_tmp, 1),
                threshold=90.0,
                unit="\u00b0F",
            ))

        # ===== UC600 humidity sensors =====
        self.teh2_hum = _jit(_sin(self.t, 7200, 2.5, 50.1))
        self.teh1_hum = _jit(_sin(self.t, 6800, 2.2, 49.6))

        # ===== Gallery zone RH (2-05 and 2-06 chronic high) =====
        for zone, base_rh in [("2-05", 57.3), ("2-06", 55.3)]:
            period = 3600 * (3 + hash(zone) % 4)
            new_rh = _jit(_sin(self.t, period, 3.0, base_rh), 0.2)
            self.gal_rh[zone] = _clamp(new_rh, 45.0, 65.0)

            if self.gal_rh[zone] >= 58.0 and not self._alarm_gal[zone]:
                self._alarm_gal[zone] = True
                events.append(AlarmEvent(
                    device="LOH SC+ Proxy (3001)",
                    point=f"Gallery {zone} RH",
                    description=f"Gallery {zone} RH high: {self.gal_rh[zone]:.1f}%",
                    severity=Severity.WARNING,
                    state=AlarmState.ACTIVE,
                    value=round(self.gal_rh[zone], 1),
                    threshold=58.0,
                    unit="%RH",
                ))
            elif self.gal_rh[zone] < 56.0 and self._alarm_gal[zone]:
                self._alarm_gal[zone] = False
                events.append(AlarmEvent(
                    device="LOH SC+ Proxy (3001)",
                    point=f"Gallery {zone} RH",
                    description=f"Gallery {zone} RH normal: {self.gal_rh[zone]:.1f}%",
                    severity=Severity.INFO,
                    state=AlarmState.CLEARED,
                    value=round(self.gal_rh[zone], 1),
                    threshold=58.0,
                    unit="%RH",
                ))

        return events

    async def run(self):
        """Run the simulation loop indefinitely."""
        self.running = True
        log.info("SimulatorV2 started (tick=%ds)", self.tick_interval)
        try:
            while self.running:
                events = self._tick()
                for event in events:
                    log.info(
                        "%-8s %-7s %s | %s",
                        event.state, event.severity,
                        event.point, event.description,
                    )
                    await self._emit(event)
                await asyncio.sleep(self.tick_interval)
        except asyncio.CancelledError:
            pass
        finally:
            self.running = False
            log.info("SimulatorV2 stopped.")

    def stop(self):
        self.running = False
