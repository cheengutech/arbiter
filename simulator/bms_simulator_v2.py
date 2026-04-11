#!/usr/bin/env python3
"""
BMS Simulator v2 — Multi-Site Headless Alarm Event Generator
==============================================================
Pure-Python simulation of alarm patterns across 5 building types,
decoupled from BACnet transport. Produces structured alarm events
suitable for WebSocket broadcast.

Buildings:
  1. Legion of Honor (museum)    — humidity/HVAC focus
  2. Office Tower                — VAV/elevator/rooftop focus
  3. Hospital/Lab                — freezer/clean-room/medical-gas focus
  4. Data Center                 — CRAC/UPS/thermal focus
  5. Warehouse                   — dock heater/leak/exhaust focus

Usage:
  from simulator.bms_simulator_v2 import MultiSiteSimulator
  sim = MultiSiteSimulator(on_alarm=my_callback)
  await sim.run()
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
    building: str = ""
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
# Math helpers
# ---------------------------------------------------------------------------
def _sin(t, period, amp, base):
    return base + amp * math.sin(2 * math.pi * t / period)


def _jit(v, sigma=0.15):
    return v + random.gauss(0, sigma)


def _clamp(v, lo, hi):
    return max(lo, min(hi, v))


# ---------------------------------------------------------------------------
# Base building simulator
# ---------------------------------------------------------------------------
class BuildingSimulator:
    """Base class for per-building alarm simulation."""

    BUILDING_NAME = ""

    def __init__(self):
        self.t = 0.0

    def _evt(self, **kwargs) -> AlarmEvent:
        kwargs.setdefault("building", self.BUILDING_NAME)
        return AlarmEvent(**kwargs)

    def tick(self, dt: float) -> list[AlarmEvent]:
        self.t += dt
        return []


# ---------------------------------------------------------------------------
# 1. Legion of Honor (museum)
# ---------------------------------------------------------------------------
class LegionOfHonorSim(BuildingSimulator):
    BUILDING_NAME = "Legion of Honor"

    def __init__(self):
        super().__init__()
        # DriSteem humidity cycling
        self.space_rh = 51.0
        self._hum_phase = 0.0
        self._hum_cycle = 55.0
        self._alarm_space_rh = False

        # Duct RH
        self.duct_rh = 66.5
        self._alarm_duct_rh = False

        # Safety interlock
        self.interlock = True
        self._alarm_interlock = False
        self._ilock_timer = 3600.0 * 3

        # AHU-1 supply fan
        self.fan_status = True
        self._fan_fail_timer = 3600.0 * 36
        self._fan_fail_active = False
        self._fan_fail_count = 0
        self._alarm_fan = False

        # Cooling tower
        self.ct_ent_tmp = 87.2
        self._alarm_ct = False

        # UC600 humidity sensors
        self.teh2_hum = 50.1
        self.teh1_hum = 49.6

        # Gallery zone RH (2-05, 2-06 chronic high)
        self.gal_rh = {"2-05": 57.3, "2-06": 55.3}
        self._alarm_gal = {"2-05": False, "2-06": False}

    def tick(self, dt: float) -> list[AlarmEvent]:
        self.t += dt
        events: list[AlarmEvent] = []

        # SpaceRH humidity cycling (51 -> 61 -> 57)
        self._hum_phase += dt / 60.0
        cycle = self._hum_cycle
        phase = (self._hum_phase % cycle) / cycle
        if phase < 0.60:
            rh_base = 51.0 + (phase / 0.60) * 10.0
        else:
            rh_base = 61.0 - ((phase - 0.60) / 0.40) * 4.0
        self.space_rh = _clamp(_jit(rh_base, 0.3), 48.0, 63.0)

        if self.space_rh >= 60.0 and not self._alarm_space_rh:
            self._alarm_space_rh = True
            events.append(self._evt(
                device="DriSteem VL6 (11002)", point="SpaceRH",
                description=f"Gallery SpaceRH HIGH: {self.space_rh:.1f}%",
                severity=Severity.CRITICAL, state=AlarmState.ACTIVE,
                value=round(self.space_rh, 1), threshold=60.0, unit="%RH",
            ))
        elif self.space_rh <= 59.0 and self._alarm_space_rh:
            self._alarm_space_rh = False
            events.append(self._evt(
                device="DriSteem VL6 (11002)", point="SpaceRH",
                description=f"Gallery SpaceRH normal: {self.space_rh:.1f}%",
                severity=Severity.INFO, state=AlarmState.CLEARED,
                value=round(self.space_rh, 1), threshold=60.0, unit="%RH",
            ))

        if self._hum_phase % cycle < (dt / 60.0):
            self._hum_cycle = random.uniform(30.0, 90.0)

        # DuctRH high-limit
        self.duct_rh = _clamp(_jit(_sin(self.t, 14400, 8.0, 66.5)), 45.0, 90.0)
        if self.duct_rh >= 80.0 and not self._alarm_duct_rh:
            self._alarm_duct_rh = True
            events.append(self._evt(
                device="DriSteem VL6 (11002)", point="DuctRH",
                description=f"DuctRH High Limit: {self.duct_rh:.1f}%",
                severity=Severity.WARNING, state=AlarmState.ACTIVE,
                value=round(self.duct_rh, 1), threshold=80.0, unit="%RH",
            ))
        elif self.duct_rh < 78.0 and self._alarm_duct_rh:
            self._alarm_duct_rh = False
            events.append(self._evt(
                device="DriSteem VL6 (11002)", point="DuctRH",
                description=f"DuctRH normal: {self.duct_rh:.1f}%",
                severity=Severity.INFO, state=AlarmState.CLEARED,
                value=round(self.duct_rh, 1), threshold=80.0, unit="%RH",
            ))

        # Safety interlock
        self._ilock_timer -= dt
        if self._ilock_timer <= 0:
            self.interlock = not self.interlock
            if not self.interlock:
                self._alarm_interlock = True
                self._ilock_timer = random.uniform(120, 300)
                events.append(self._evt(
                    device="DriSteem VL6 (11002)", point="SafetyInterlock",
                    description="Humidifier SafetyInterlock INACTIVE",
                    severity=Severity.CRITICAL, state=AlarmState.ACTIVE,
                    value="inactive", threshold="active",
                ))
            else:
                self._alarm_interlock = False
                self._ilock_timer = random.uniform(7200, 14400)
                events.append(self._evt(
                    device="DriSteem VL6 (11002)", point="SafetyInterlock",
                    description="SafetyInterlock restored",
                    severity=Severity.INFO, state=AlarmState.CLEARED,
                    value="active", threshold="active",
                ))

        # AHU-1 supply fan failure cluster
        self._fan_fail_timer -= dt
        if self._fan_fail_timer <= 0 and not self._fan_fail_active:
            self._fan_fail_active = True
            self._fan_fail_count = random.randint(4, 6)
            events.append(self._evt(
                device="LOH SC+ Proxy (3001)", point="AHU-1 Supply Fan",
                description="AHU-1 Supply Fan Failure (cluster start)",
                severity=Severity.CRITICAL, state=AlarmState.ACTIVE,
                value="off", threshold="running",
            ))
            self._alarm_fan = True
            self.fan_status = False
        if self._fan_fail_active:
            self._fan_fail_count -= 1
            self.fan_status = not self.fan_status
            if self.fan_status and self._alarm_fan:
                events.append(self._evt(
                    device="LOH SC+ Proxy (3001)", point="AHU-1 Supply Fan",
                    description="AHU-1 Supply Fan returned to normal",
                    severity=Severity.INFO, state=AlarmState.CLEARED,
                    value="running", threshold="running",
                ))
            elif not self.fan_status and not self._alarm_fan:
                events.append(self._evt(
                    device="LOH SC+ Proxy (3001)", point="AHU-1 Supply Fan",
                    description="AHU-1 Supply Fan Failure",
                    severity=Severity.CRITICAL, state=AlarmState.ACTIVE,
                    value="off", threshold="running",
                ))
            self._alarm_fan = not self.fan_status
            if self._fan_fail_count <= 0:
                self._fan_fail_active = False
                self.fan_status = True
                self._alarm_fan = False
                self._fan_fail_timer = random.uniform(3600 * 24, 3600 * 48)

        # Cooling tower high temp
        self.ct_ent_tmp = _jit(_sin(self.t, 3600, 3.0, 87.2))
        if self.ct_ent_tmp >= 90.0 and not self._alarm_ct:
            self._alarm_ct = True
            events.append(self._evt(
                device="LOH SC+ Proxy (3001)", point="CT-1 Entering Water Temp",
                description=f"Cooling Tower High Temp: {self.ct_ent_tmp:.1f}°F",
                severity=Severity.WARNING, state=AlarmState.ACTIVE,
                value=round(self.ct_ent_tmp, 1), threshold=90.0, unit="°F",
            ))
        elif self.ct_ent_tmp < 89.0 and self._alarm_ct:
            self._alarm_ct = False
            events.append(self._evt(
                device="LOH SC+ Proxy (3001)", point="CT-1 Entering Water Temp",
                description=f"Cooling Tower temp normal: {self.ct_ent_tmp:.1f}°F",
                severity=Severity.INFO, state=AlarmState.CLEARED,
                value=round(self.ct_ent_tmp, 1), threshold=90.0, unit="°F",
            ))

        # UC600 humidity sensors (no alarms, just drift)
        self.teh2_hum = _jit(_sin(self.t, 7200, 2.5, 50.1))
        self.teh1_hum = _jit(_sin(self.t, 6800, 2.2, 49.6))

        # Gallery zone RH (2-05 and 2-06 chronic high)
        for zone, base_rh in [("2-05", 57.3), ("2-06", 55.3)]:
            period = 3600 * (3 + hash(zone) % 4)
            new_rh = _jit(_sin(self.t, period, 3.0, base_rh), 0.2)
            self.gal_rh[zone] = _clamp(new_rh, 45.0, 65.0)

            if self.gal_rh[zone] >= 58.0 and not self._alarm_gal[zone]:
                self._alarm_gal[zone] = True
                events.append(self._evt(
                    device="LOH SC+ Proxy (3001)", point=f"Gallery {zone} RH",
                    description=f"Gallery {zone} RH high: {self.gal_rh[zone]:.1f}%",
                    severity=Severity.WARNING, state=AlarmState.ACTIVE,
                    value=round(self.gal_rh[zone], 1), threshold=58.0, unit="%RH",
                ))
            elif self.gal_rh[zone] < 56.0 and self._alarm_gal[zone]:
                self._alarm_gal[zone] = False
                events.append(self._evt(
                    device="LOH SC+ Proxy (3001)", point=f"Gallery {zone} RH",
                    description=f"Gallery {zone} RH normal: {self.gal_rh[zone]:.1f}%",
                    severity=Severity.INFO, state=AlarmState.CLEARED,
                    value=round(self.gal_rh[zone], 1), threshold=58.0, unit="%RH",
                ))

        return events


# ---------------------------------------------------------------------------
# 2. Office Tower
# ---------------------------------------------------------------------------
class OfficeTowerSim(BuildingSimulator):
    BUILDING_NAME = "Office Tower"

    def __init__(self):
        super().__init__()
        # 2 chillers
        self.chiller_temps = [44.0, 44.5]  # leaving water temp °F
        self._alarm_chiller = [False, False]

        # 3 AHUs — discharge air temp
        self.ahu_dat = [55.0, 55.2, 54.8]
        self._alarm_ahu = [False, False, False]

        # 30 VAV boxes — 6 chronic hot-spot zones
        self._vav_hotspots = [3, 9, 14, 19, 24, 28]
        self.vav_temps = [72.0] * 30
        self._alarm_vav = [False] * 30

        # 2 elevators — intermittent fault
        self._elev_fault_timer = [random.uniform(7200, 14400), random.uniform(9000, 18000)]
        self._alarm_elev = [False, False]

        # Rooftop units — economizer stuck
        self.rtu_oa_damper = 35.0  # % open
        self._alarm_rtu_damper = False

        # Domestic hot water
        self.dhw_temp = 120.0
        self._alarm_dhw = False

        # Fire pump
        self._fire_pump_test_timer = 3600.0 * 168  # weekly test
        self._alarm_fire_pump = False

    def tick(self, dt: float) -> list[AlarmEvent]:
        self.t += dt
        events: list[AlarmEvent] = []

        # Chillers — leaving water temp drift
        for i in range(2):
            base = 44.0 + i * 0.5
            self.chiller_temps[i] = _clamp(
                _jit(_sin(self.t, 7200 + i * 600, 3.0, base), 0.2), 38.0, 52.0
            )
            if self.chiller_temps[i] >= 48.0 and not self._alarm_chiller[i]:
                self._alarm_chiller[i] = True
                events.append(self._evt(
                    device=f"Chiller-{i+1}", point="Leaving Water Temp",
                    description=f"Chiller-{i+1} LWT high: {self.chiller_temps[i]:.1f}°F",
                    severity=Severity.WARNING, state=AlarmState.ACTIVE,
                    value=round(self.chiller_temps[i], 1), threshold=48.0, unit="°F",
                ))
            elif self.chiller_temps[i] < 46.0 and self._alarm_chiller[i]:
                self._alarm_chiller[i] = False
                events.append(self._evt(
                    device=f"Chiller-{i+1}", point="Leaving Water Temp",
                    description=f"Chiller-{i+1} LWT normal: {self.chiller_temps[i]:.1f}°F",
                    severity=Severity.INFO, state=AlarmState.CLEARED,
                    value=round(self.chiller_temps[i], 1), threshold=48.0, unit="°F",
                ))

        # AHUs — discharge air temp
        for i in range(3):
            base = 55.0 + i * 0.2
            self.ahu_dat[i] = _clamp(
                _jit(_sin(self.t, 5400 + i * 300, 4.0, base), 0.3), 48.0, 65.0
            )
            if self.ahu_dat[i] >= 62.0 and not self._alarm_ahu[i]:
                self._alarm_ahu[i] = True
                events.append(self._evt(
                    device=f"AHU-{i+1}", point="Discharge Air Temp",
                    description=f"AHU-{i+1} DAT high: {self.ahu_dat[i]:.1f}°F",
                    severity=Severity.WARNING, state=AlarmState.ACTIVE,
                    value=round(self.ahu_dat[i], 1), threshold=62.0, unit="°F",
                ))
            elif self.ahu_dat[i] < 60.0 and self._alarm_ahu[i]:
                self._alarm_ahu[i] = False
                events.append(self._evt(
                    device=f"AHU-{i+1}", point="Discharge Air Temp",
                    description=f"AHU-{i+1} DAT normal: {self.ahu_dat[i]:.1f}°F",
                    severity=Severity.INFO, state=AlarmState.CLEARED,
                    value=round(self.ahu_dat[i], 1), threshold=62.0, unit="°F",
                ))

        # VAV hot-spot zones
        for idx in self._vav_hotspots:
            base = 74.0 + (idx % 5) * 0.5
            period = 3600 * (2 + idx % 3)
            self.vav_temps[idx] = _clamp(
                _jit(_sin(self.t, period, 3.5, base), 0.2), 68.0, 82.0
            )
            if self.vav_temps[idx] >= 78.0 and not self._alarm_vav[idx]:
                self._alarm_vav[idx] = True
                events.append(self._evt(
                    device=f"VAV-{idx+1:02d}", point="Zone Temp",
                    description=f"VAV-{idx+1:02d} zone temp high: {self.vav_temps[idx]:.1f}°F",
                    severity=Severity.WARNING, state=AlarmState.ACTIVE,
                    value=round(self.vav_temps[idx], 1), threshold=78.0, unit="°F",
                ))
            elif self.vav_temps[idx] < 76.0 and self._alarm_vav[idx]:
                self._alarm_vav[idx] = False
                events.append(self._evt(
                    device=f"VAV-{idx+1:02d}", point="Zone Temp",
                    description=f"VAV-{idx+1:02d} zone temp normal: {self.vav_temps[idx]:.1f}°F",
                    severity=Severity.INFO, state=AlarmState.CLEARED,
                    value=round(self.vav_temps[idx], 1), threshold=78.0, unit="°F",
                ))

        # Elevator faults
        for i in range(2):
            self._elev_fault_timer[i] -= dt
            if self._elev_fault_timer[i] <= 0:
                if not self._alarm_elev[i]:
                    self._alarm_elev[i] = True
                    self._elev_fault_timer[i] = random.uniform(300, 900)
                    events.append(self._evt(
                        device=f"Elevator-{i+1}", point="Drive Fault",
                        description=f"Elevator-{i+1} drive fault detected",
                        severity=Severity.CRITICAL, state=AlarmState.ACTIVE,
                        value="fault", threshold="normal",
                    ))
                else:
                    self._alarm_elev[i] = False
                    self._elev_fault_timer[i] = random.uniform(7200, 14400)
                    events.append(self._evt(
                        device=f"Elevator-{i+1}", point="Drive Fault",
                        description=f"Elevator-{i+1} drive fault cleared",
                        severity=Severity.INFO, state=AlarmState.CLEARED,
                        value="normal", threshold="normal",
                    ))

        # RTU economizer damper stuck
        self.rtu_oa_damper = _clamp(
            _jit(_sin(self.t, 10800, 20.0, 35.0), 1.0), 0.0, 100.0
        )
        if self.rtu_oa_damper <= 5.0 and not self._alarm_rtu_damper:
            self._alarm_rtu_damper = True
            events.append(self._evt(
                device="RTU-1", point="OA Damper Position",
                description=f"RTU-1 economizer damper stuck closed: {self.rtu_oa_damper:.0f}%",
                severity=Severity.WARNING, state=AlarmState.ACTIVE,
                value=round(self.rtu_oa_damper, 0), threshold=10.0, unit="%",
            ))
        elif self.rtu_oa_damper > 15.0 and self._alarm_rtu_damper:
            self._alarm_rtu_damper = False
            events.append(self._evt(
                device="RTU-1", point="OA Damper Position",
                description=f"RTU-1 economizer damper normal: {self.rtu_oa_damper:.0f}%",
                severity=Severity.INFO, state=AlarmState.CLEARED,
                value=round(self.rtu_oa_damper, 0), threshold=10.0, unit="%",
            ))

        # Domestic hot water temp
        self.dhw_temp = _clamp(_jit(_sin(self.t, 5400, 8.0, 120.0), 0.5), 100.0, 140.0)
        if self.dhw_temp <= 110.0 and not self._alarm_dhw:
            self._alarm_dhw = True
            events.append(self._evt(
                device="DHW Heater", point="Supply Temp",
                description=f"DHW supply temp low: {self.dhw_temp:.1f}°F",
                severity=Severity.WARNING, state=AlarmState.ACTIVE,
                value=round(self.dhw_temp, 1), threshold=110.0, unit="°F",
            ))
        elif self.dhw_temp > 115.0 and self._alarm_dhw:
            self._alarm_dhw = False
            events.append(self._evt(
                device="DHW Heater", point="Supply Temp",
                description=f"DHW supply temp normal: {self.dhw_temp:.1f}°F",
                severity=Severity.INFO, state=AlarmState.CLEARED,
                value=round(self.dhw_temp, 1), threshold=110.0, unit="°F",
            ))

        # Fire pump weekly test
        self._fire_pump_test_timer -= dt
        if self._fire_pump_test_timer <= 0:
            self._fire_pump_test_timer = 3600.0 * 168
            if random.random() < 0.15:
                events.append(self._evt(
                    device="Fire Pump", point="Weekly Test",
                    description="Fire pump weekly test FAILED — low pressure",
                    severity=Severity.CRITICAL, state=AlarmState.ACTIVE,
                    value="fail", threshold="pass",
                ))
            else:
                events.append(self._evt(
                    device="Fire Pump", point="Weekly Test",
                    description="Fire pump weekly test passed",
                    severity=Severity.INFO, state=AlarmState.CLEARED,
                    value="pass", threshold="pass",
                ))

        return events


# ---------------------------------------------------------------------------
# 3. Hospital/Lab
# ---------------------------------------------------------------------------
class HospitalLabSim(BuildingSimulator):
    BUILDING_NAME = "Hospital/Lab"

    def __init__(self):
        super().__init__()
        # 3 boilers
        self.boiler_temps = [180.0, 178.0, 182.0]
        self._alarm_boiler = [False, False, False]

        # 2 chillers
        self.chiller_temps = [42.0, 42.5]
        self._alarm_chiller = [False, False]

        # 4 AHUs
        self.ahu_filter_dp = [1.2, 1.1, 1.3, 1.0]  # in wc
        self._alarm_ahu_filter = [False, False, False, False]

        # Backup generator
        self._gen_test_timer = 3600.0 * 72  # test every 3 days
        self._alarm_gen = False

        # 6 freezers (-80°C)
        self.freezer_temps = [-80.0, -79.5, -80.2, -79.8, -80.1, -79.7]
        self._alarm_freezer = [False] * 6
        self._freezer_drift_idx = random.choice(range(6))

        # Clean room pressure sensors (3 rooms)
        self.cr_pressure = [0.05, 0.05, 0.05]  # in wc positive
        self._alarm_cr = [False, False, False]

        # Medical gas — O2 manifold pressure
        self.med_gas_psi = 55.0
        self._alarm_med_gas = False

    def tick(self, dt: float) -> list[AlarmEvent]:
        self.t += dt
        events: list[AlarmEvent] = []

        # Boilers — high stack temp
        for i in range(3):
            base = 180.0 + i * 2.0
            self.boiler_temps[i] = _clamp(
                _jit(_sin(self.t, 9000 + i * 500, 15.0, base), 0.5), 150.0, 220.0
            )
            if self.boiler_temps[i] >= 200.0 and not self._alarm_boiler[i]:
                self._alarm_boiler[i] = True
                events.append(self._evt(
                    device=f"Boiler-{i+1}", point="Supply Water Temp",
                    description=f"Boiler-{i+1} supply temp high: {self.boiler_temps[i]:.1f}°F",
                    severity=Severity.WARNING, state=AlarmState.ACTIVE,
                    value=round(self.boiler_temps[i], 1), threshold=200.0, unit="°F",
                ))
            elif self.boiler_temps[i] < 195.0 and self._alarm_boiler[i]:
                self._alarm_boiler[i] = False
                events.append(self._evt(
                    device=f"Boiler-{i+1}", point="Supply Water Temp",
                    description=f"Boiler-{i+1} supply temp normal: {self.boiler_temps[i]:.1f}°F",
                    severity=Severity.INFO, state=AlarmState.CLEARED,
                    value=round(self.boiler_temps[i], 1), threshold=200.0, unit="°F",
                ))

        # Chillers
        for i in range(2):
            base = 42.0 + i * 0.5
            self.chiller_temps[i] = _clamp(
                _jit(_sin(self.t, 6000 + i * 400, 3.0, base), 0.2), 36.0, 50.0
            )
            if self.chiller_temps[i] >= 47.0 and not self._alarm_chiller[i]:
                self._alarm_chiller[i] = True
                events.append(self._evt(
                    device=f"Chiller-{i+1}", point="Leaving Water Temp",
                    description=f"Chiller-{i+1} LWT high: {self.chiller_temps[i]:.1f}°F",
                    severity=Severity.WARNING, state=AlarmState.ACTIVE,
                    value=round(self.chiller_temps[i], 1), threshold=47.0, unit="°F",
                ))
            elif self.chiller_temps[i] < 45.0 and self._alarm_chiller[i]:
                self._alarm_chiller[i] = False
                events.append(self._evt(
                    device=f"Chiller-{i+1}", point="Leaving Water Temp",
                    description=f"Chiller-{i+1} LWT normal: {self.chiller_temps[i]:.1f}°F",
                    severity=Severity.INFO, state=AlarmState.CLEARED,
                    value=round(self.chiller_temps[i], 1), threshold=47.0, unit="°F",
                ))

        # AHU filter DP — slow clogging drift
        for i in range(4):
            self.ahu_filter_dp[i] += dt / 360000.0  # slowly rises
            self.ahu_filter_dp[i] = _clamp(_jit(self.ahu_filter_dp[i], 0.01), 0.5, 3.0)
            if self.ahu_filter_dp[i] >= 2.0 and not self._alarm_ahu_filter[i]:
                self._alarm_ahu_filter[i] = True
                events.append(self._evt(
                    device=f"AHU-{i+1}", point="Filter DP",
                    description=f"AHU-{i+1} filter DP high: {self.ahu_filter_dp[i]:.2f} in wc — replace filter",
                    severity=Severity.WARNING, state=AlarmState.ACTIVE,
                    value=round(self.ahu_filter_dp[i], 2), threshold=2.0, unit="in wc",
                ))

        # Backup generator test
        self._gen_test_timer -= dt
        if self._gen_test_timer <= 0:
            self._gen_test_timer = 3600.0 * 72
            if random.random() < 0.1:
                events.append(self._evt(
                    device="Backup Generator", point="Auto-Start Test",
                    description="Generator failed to start during auto-test",
                    severity=Severity.CRITICAL, state=AlarmState.ACTIVE,
                    value="no-start", threshold="running",
                ))
            else:
                events.append(self._evt(
                    device="Backup Generator", point="Auto-Start Test",
                    description="Generator auto-start test passed",
                    severity=Severity.INFO, state=AlarmState.CLEARED,
                    value="running", threshold="running",
                ))

        # Freezers — one drifts warm periodically
        idx = self._freezer_drift_idx
        drift = _sin(self.t, 18000, 6.0, -80.0)
        self.freezer_temps[idx] = _clamp(_jit(drift, 0.3), -85.0, -65.0)
        for i in range(6):
            if i != idx:
                self.freezer_temps[i] = _clamp(_jit(-80.0, 0.2), -82.0, -78.0)

        if self.freezer_temps[idx] >= -75.0 and not self._alarm_freezer[idx]:
            self._alarm_freezer[idx] = True
            events.append(self._evt(
                device=f"ULT Freezer-{idx+1}", point="Cabinet Temp",
                description=f"Freezer-{idx+1} temp HIGH: {self.freezer_temps[idx]:.1f}°C — specimen risk",
                severity=Severity.CRITICAL, state=AlarmState.ACTIVE,
                value=round(self.freezer_temps[idx], 1), threshold=-75.0, unit="°C",
            ))
        elif self.freezer_temps[idx] < -77.0 and self._alarm_freezer[idx]:
            self._alarm_freezer[idx] = False
            events.append(self._evt(
                device=f"ULT Freezer-{idx+1}", point="Cabinet Temp",
                description=f"Freezer-{idx+1} temp normal: {self.freezer_temps[idx]:.1f}°C",
                severity=Severity.INFO, state=AlarmState.CLEARED,
                value=round(self.freezer_temps[idx], 1), threshold=-75.0, unit="°C",
            ))

        # Clean room pressure
        for i in range(3):
            self.cr_pressure[i] = _clamp(
                _jit(_sin(self.t, 4800 + i * 200, 0.03, 0.05), 0.005), -0.02, 0.10
            )
            if self.cr_pressure[i] <= 0.01 and not self._alarm_cr[i]:
                self._alarm_cr[i] = True
                events.append(self._evt(
                    device=f"Clean Room {i+1}", point="Room Pressure",
                    description=f"Clean Room {i+1} pressure LOW: {self.cr_pressure[i]:.3f} in wc — containment risk",
                    severity=Severity.CRITICAL, state=AlarmState.ACTIVE,
                    value=round(self.cr_pressure[i], 3), threshold=0.01, unit="in wc",
                ))
            elif self.cr_pressure[i] > 0.03 and self._alarm_cr[i]:
                self._alarm_cr[i] = False
                events.append(self._evt(
                    device=f"Clean Room {i+1}", point="Room Pressure",
                    description=f"Clean Room {i+1} pressure normal: {self.cr_pressure[i]:.3f} in wc",
                    severity=Severity.INFO, state=AlarmState.CLEARED,
                    value=round(self.cr_pressure[i], 3), threshold=0.01, unit="in wc",
                ))

        # Medical gas O2 manifold
        self.med_gas_psi = _clamp(_jit(_sin(self.t, 7200, 8.0, 55.0), 0.5), 35.0, 70.0)
        if self.med_gas_psi <= 45.0 and not self._alarm_med_gas:
            self._alarm_med_gas = True
            events.append(self._evt(
                device="O2 Manifold", point="Supply Pressure",
                description=f"Medical O2 pressure LOW: {self.med_gas_psi:.0f} PSI",
                severity=Severity.CRITICAL, state=AlarmState.ACTIVE,
                value=round(self.med_gas_psi, 0), threshold=45.0, unit="PSI",
            ))
        elif self.med_gas_psi > 50.0 and self._alarm_med_gas:
            self._alarm_med_gas = False
            events.append(self._evt(
                device="O2 Manifold", point="Supply Pressure",
                description=f"Medical O2 pressure normal: {self.med_gas_psi:.0f} PSI",
                severity=Severity.INFO, state=AlarmState.CLEARED,
                value=round(self.med_gas_psi, 0), threshold=45.0, unit="PSI",
            ))

        return events


# ---------------------------------------------------------------------------
# 4. Data Center
# ---------------------------------------------------------------------------
class DataCenterSim(BuildingSimulator):
    BUILDING_NAME = "Data Center"

    def __init__(self):
        super().__init__()
        # 8 CRAC units — supply air temp
        self.crac_temps = [65.0 + i * 0.3 for i in range(8)]
        self._alarm_crac = [False] * 8

        # 2 chillers
        self.chiller_temps = [44.0, 44.5]
        self._alarm_chiller = [False, False]

        # 3 UPS systems — battery %
        self.ups_battery = [100.0, 100.0, 100.0]
        self._alarm_ups = [False, False, False]
        self._ups_drain_idx = -1
        self._ups_drain_timer = random.uniform(14400, 36000)

        # 2 generators
        self._gen_test_timer = [3600.0 * 48, 3600.0 * 48 + 3600]
        self._alarm_gen = [False, False]

        # Rack hot spots (pick 8 of 200)
        self._hot_racks = random.sample(range(200), 8)
        self.rack_temps = [72.0] * 200
        self._alarm_rack = [False] * 200

        # Hot/cold aisle containment — delta T
        self.aisle_delta_t = 20.0
        self._alarm_aisle = False

        # Fire suppression — agent pressure
        self.fire_agent_psi = 360.0
        self._alarm_fire = False

    def tick(self, dt: float) -> list[AlarmEvent]:
        self.t += dt
        events: list[AlarmEvent] = []

        # CRAC units
        for i in range(8):
            base = 65.0 + i * 0.3
            self.crac_temps[i] = _clamp(
                _jit(_sin(self.t, 3600 + i * 200, 4.0, base), 0.2), 58.0, 78.0
            )
            if self.crac_temps[i] >= 72.0 and not self._alarm_crac[i]:
                self._alarm_crac[i] = True
                events.append(self._evt(
                    device=f"CRAC-{i+1}", point="Supply Air Temp",
                    description=f"CRAC-{i+1} supply temp high: {self.crac_temps[i]:.1f}°F",
                    severity=Severity.WARNING, state=AlarmState.ACTIVE,
                    value=round(self.crac_temps[i], 1), threshold=72.0, unit="°F",
                ))
            elif self.crac_temps[i] < 70.0 and self._alarm_crac[i]:
                self._alarm_crac[i] = False
                events.append(self._evt(
                    device=f"CRAC-{i+1}", point="Supply Air Temp",
                    description=f"CRAC-{i+1} supply temp normal: {self.crac_temps[i]:.1f}°F",
                    severity=Severity.INFO, state=AlarmState.CLEARED,
                    value=round(self.crac_temps[i], 1), threshold=72.0, unit="°F",
                ))

        # Chillers
        for i in range(2):
            base = 44.0 + i * 0.5
            self.chiller_temps[i] = _clamp(
                _jit(_sin(self.t, 5400 + i * 300, 3.5, base), 0.2), 38.0, 52.0
            )
            if self.chiller_temps[i] >= 49.0 and not self._alarm_chiller[i]:
                self._alarm_chiller[i] = True
                events.append(self._evt(
                    device=f"Chiller-{i+1}", point="Leaving Water Temp",
                    description=f"Chiller-{i+1} LWT high: {self.chiller_temps[i]:.1f}°F",
                    severity=Severity.CRITICAL, state=AlarmState.ACTIVE,
                    value=round(self.chiller_temps[i], 1), threshold=49.0, unit="°F",
                ))
            elif self.chiller_temps[i] < 47.0 and self._alarm_chiller[i]:
                self._alarm_chiller[i] = False
                events.append(self._evt(
                    device=f"Chiller-{i+1}", point="Leaving Water Temp",
                    description=f"Chiller-{i+1} LWT normal: {self.chiller_temps[i]:.1f}°F",
                    severity=Severity.INFO, state=AlarmState.CLEARED,
                    value=round(self.chiller_temps[i], 1), threshold=49.0, unit="°F",
                ))

        # UPS battery drain event
        self._ups_drain_timer -= dt
        if self._ups_drain_timer <= 0 and self._ups_drain_idx < 0:
            self._ups_drain_idx = random.randint(0, 2)
            self._ups_drain_timer = random.uniform(300, 600)  # drain duration
        if self._ups_drain_idx >= 0:
            idx = self._ups_drain_idx
            self.ups_battery[idx] = max(0.0, self.ups_battery[idx] - dt * 0.5)
            if self.ups_battery[idx] <= 50.0 and not self._alarm_ups[idx]:
                self._alarm_ups[idx] = True
                events.append(self._evt(
                    device=f"UPS-{idx+1}", point="Battery Level",
                    description=f"UPS-{idx+1} battery low: {self.ups_battery[idx]:.0f}%",
                    severity=Severity.CRITICAL, state=AlarmState.ACTIVE,
                    value=round(self.ups_battery[idx], 0), threshold=50.0, unit="%",
                ))
            self._ups_drain_timer -= dt
            if self._ups_drain_timer <= 0:
                # restore
                self.ups_battery[idx] = 100.0
                if self._alarm_ups[idx]:
                    self._alarm_ups[idx] = False
                    events.append(self._evt(
                        device=f"UPS-{idx+1}", point="Battery Level",
                        description=f"UPS-{idx+1} battery restored: 100%",
                        severity=Severity.INFO, state=AlarmState.CLEARED,
                        value=100.0, threshold=50.0, unit="%",
                    ))
                self._ups_drain_idx = -1
                self._ups_drain_timer = random.uniform(14400, 36000)

        # Generator tests
        for i in range(2):
            self._gen_test_timer[i] -= dt
            if self._gen_test_timer[i] <= 0:
                self._gen_test_timer[i] = 3600.0 * 48
                if random.random() < 0.08:
                    events.append(self._evt(
                        device=f"Generator-{i+1}", point="Auto-Start Test",
                        description=f"Generator-{i+1} failed to start during auto-test",
                        severity=Severity.CRITICAL, state=AlarmState.ACTIVE,
                        value="no-start", threshold="running",
                    ))
                else:
                    events.append(self._evt(
                        device=f"Generator-{i+1}", point="Auto-Start Test",
                        description=f"Generator-{i+1} auto-start test passed",
                        severity=Severity.INFO, state=AlarmState.CLEARED,
                        value="running", threshold="running",
                    ))

        # Rack hot spots
        for idx in self._hot_racks:
            base = 78.0 + (idx % 5) * 0.5
            period = 2400 + idx % 600
            self.rack_temps[idx] = _clamp(
                _jit(_sin(self.t, period, 5.0, base), 0.3), 68.0, 95.0
            )
            if self.rack_temps[idx] >= 85.0 and not self._alarm_rack[idx]:
                self._alarm_rack[idx] = True
                row = chr(65 + idx // 20)
                pos = (idx % 20) + 1
                events.append(self._evt(
                    device=f"Rack {row}{pos:02d}", point="Inlet Temp",
                    description=f"Rack {row}{pos:02d} inlet temp HIGH: {self.rack_temps[idx]:.1f}°F",
                    severity=Severity.CRITICAL, state=AlarmState.ACTIVE,
                    value=round(self.rack_temps[idx], 1), threshold=85.0, unit="°F",
                ))
            elif self.rack_temps[idx] < 82.0 and self._alarm_rack[idx]:
                self._alarm_rack[idx] = False
                row = chr(65 + idx // 20)
                pos = (idx % 20) + 1
                events.append(self._evt(
                    device=f"Rack {row}{pos:02d}", point="Inlet Temp",
                    description=f"Rack {row}{pos:02d} inlet temp normal: {self.rack_temps[idx]:.1f}°F",
                    severity=Severity.INFO, state=AlarmState.CLEARED,
                    value=round(self.rack_temps[idx], 1), threshold=85.0, unit="°F",
                ))

        # Hot/cold aisle delta T
        self.aisle_delta_t = _clamp(
            _jit(_sin(self.t, 7200, 6.0, 20.0), 0.5), 10.0, 35.0
        )
        if self.aisle_delta_t >= 28.0 and not self._alarm_aisle:
            self._alarm_aisle = True
            events.append(self._evt(
                device="Aisle Containment", point="Hot/Cold Delta T",
                description=f"Aisle delta T high: {self.aisle_delta_t:.1f}°F — containment breach",
                severity=Severity.WARNING, state=AlarmState.ACTIVE,
                value=round(self.aisle_delta_t, 1), threshold=28.0, unit="°F",
            ))
        elif self.aisle_delta_t < 25.0 and self._alarm_aisle:
            self._alarm_aisle = False
            events.append(self._evt(
                device="Aisle Containment", point="Hot/Cold Delta T",
                description=f"Aisle delta T normal: {self.aisle_delta_t:.1f}°F",
                severity=Severity.INFO, state=AlarmState.CLEARED,
                value=round(self.aisle_delta_t, 1), threshold=28.0, unit="°F",
            ))

        # Fire suppression agent pressure
        self.fire_agent_psi = _clamp(
            _jit(_sin(self.t, 14400, 15.0, 360.0), 1.0), 300.0, 400.0
        )
        if self.fire_agent_psi <= 330.0 and not self._alarm_fire:
            self._alarm_fire = True
            events.append(self._evt(
                device="Fire Suppression", point="Agent Pressure",
                description=f"Fire suppression agent pressure LOW: {self.fire_agent_psi:.0f} PSI",
                severity=Severity.CRITICAL, state=AlarmState.ACTIVE,
                value=round(self.fire_agent_psi, 0), threshold=330.0, unit="PSI",
            ))
        elif self.fire_agent_psi > 345.0 and self._alarm_fire:
            self._alarm_fire = False
            events.append(self._evt(
                device="Fire Suppression", point="Agent Pressure",
                description=f"Fire suppression agent pressure normal: {self.fire_agent_psi:.0f} PSI",
                severity=Severity.INFO, state=AlarmState.CLEARED,
                value=round(self.fire_agent_psi, 0), threshold=330.0, unit="PSI",
            ))

        return events


# ---------------------------------------------------------------------------
# 5. Warehouse
# ---------------------------------------------------------------------------
class WarehouseSim(BuildingSimulator):
    BUILDING_NAME = "Warehouse"

    def __init__(self):
        super().__init__()
        # 2 boilers
        self.boiler_temps = [160.0, 162.0]
        self._alarm_boiler = [False, False]

        # 1 RTU — supply temp
        self.rtu_supply = 68.0
        self._alarm_rtu = False

        # Loading dock heaters (3 bays)
        self.dock_temps = [55.0, 54.0, 56.0]
        self._alarm_dock = [False, False, False]

        # Roof leak sensors (4 zones)
        self._leak_timer = [random.uniform(18000, 72000) for _ in range(4)]
        self._alarm_leak = [False, False, False, False]

        # Forklift charging stations (6)
        self._charger_fault_timer = random.uniform(7200, 28800)
        self._alarm_charger = [False] * 6
        self._charger_fault_idx = -1

        # Fire sprinkler — main pressure
        self.sprinkler_psi = 120.0
        self._alarm_sprinkler = False

        # Exhaust fans (4)
        self._exhaust_fail_timer = [random.uniform(36000, 72000) for _ in range(4)]
        self._alarm_exhaust = [False, False, False, False]

    def tick(self, dt: float) -> list[AlarmEvent]:
        self.t += dt
        events: list[AlarmEvent] = []

        # Boilers
        for i in range(2):
            base = 160.0 + i * 2.0
            self.boiler_temps[i] = _clamp(
                _jit(_sin(self.t, 7200 + i * 400, 12.0, base), 0.5), 130.0, 195.0
            )
            if self.boiler_temps[i] >= 185.0 and not self._alarm_boiler[i]:
                self._alarm_boiler[i] = True
                events.append(self._evt(
                    device=f"Boiler-{i+1}", point="Supply Water Temp",
                    description=f"Boiler-{i+1} supply temp high: {self.boiler_temps[i]:.1f}°F",
                    severity=Severity.WARNING, state=AlarmState.ACTIVE,
                    value=round(self.boiler_temps[i], 1), threshold=185.0, unit="°F",
                ))
            elif self.boiler_temps[i] < 180.0 and self._alarm_boiler[i]:
                self._alarm_boiler[i] = False
                events.append(self._evt(
                    device=f"Boiler-{i+1}", point="Supply Water Temp",
                    description=f"Boiler-{i+1} supply temp normal: {self.boiler_temps[i]:.1f}°F",
                    severity=Severity.INFO, state=AlarmState.CLEARED,
                    value=round(self.boiler_temps[i], 1), threshold=185.0, unit="°F",
                ))

        # RTU supply temp
        self.rtu_supply = _clamp(
            _jit(_sin(self.t, 5400, 6.0, 68.0), 0.3), 55.0, 82.0
        )
        if self.rtu_supply >= 78.0 and not self._alarm_rtu:
            self._alarm_rtu = True
            events.append(self._evt(
                device="RTU-1", point="Supply Air Temp",
                description=f"RTU-1 supply temp high: {self.rtu_supply:.1f}°F",
                severity=Severity.WARNING, state=AlarmState.ACTIVE,
                value=round(self.rtu_supply, 1), threshold=78.0, unit="°F",
            ))
        elif self.rtu_supply < 75.0 and self._alarm_rtu:
            self._alarm_rtu = False
            events.append(self._evt(
                device="RTU-1", point="Supply Air Temp",
                description=f"RTU-1 supply temp normal: {self.rtu_supply:.1f}°F",
                severity=Severity.INFO, state=AlarmState.CLEARED,
                value=round(self.rtu_supply, 1), threshold=78.0, unit="°F",
            ))

        # Loading dock heaters
        for i in range(3):
            base = 55.0 + i
            self.dock_temps[i] = _clamp(
                _jit(_sin(self.t, 3600 + i * 200, 8.0, base), 0.3), 35.0, 70.0
            )
            if self.dock_temps[i] <= 40.0 and not self._alarm_dock[i]:
                self._alarm_dock[i] = True
                events.append(self._evt(
                    device=f"Dock Heater Bay-{i+1}", point="Bay Temp",
                    description=f"Loading dock Bay-{i+1} temp LOW: {self.dock_temps[i]:.1f}°F — freeze risk",
                    severity=Severity.WARNING, state=AlarmState.ACTIVE,
                    value=round(self.dock_temps[i], 1), threshold=40.0, unit="°F",
                ))
            elif self.dock_temps[i] > 45.0 and self._alarm_dock[i]:
                self._alarm_dock[i] = False
                events.append(self._evt(
                    device=f"Dock Heater Bay-{i+1}", point="Bay Temp",
                    description=f"Loading dock Bay-{i+1} temp normal: {self.dock_temps[i]:.1f}°F",
                    severity=Severity.INFO, state=AlarmState.CLEARED,
                    value=round(self.dock_temps[i], 1), threshold=40.0, unit="°F",
                ))

        # Roof leak sensors
        for i in range(4):
            self._leak_timer[i] -= dt
            if self._leak_timer[i] <= 0:
                if not self._alarm_leak[i]:
                    self._alarm_leak[i] = True
                    self._leak_timer[i] = random.uniform(600, 1800)
                    events.append(self._evt(
                        device=f"Roof Leak Sensor Zone-{i+1}", point="Moisture Detected",
                        description=f"Roof leak detected — Zone {i+1}",
                        severity=Severity.CRITICAL, state=AlarmState.ACTIVE,
                        value="wet", threshold="dry",
                    ))
                else:
                    self._alarm_leak[i] = False
                    self._leak_timer[i] = random.uniform(18000, 72000)
                    events.append(self._evt(
                        device=f"Roof Leak Sensor Zone-{i+1}", point="Moisture Detected",
                        description=f"Roof leak cleared — Zone {i+1}",
                        severity=Severity.INFO, state=AlarmState.CLEARED,
                        value="dry", threshold="dry",
                    ))

        # Forklift charger fault
        self._charger_fault_timer -= dt
        if self._charger_fault_timer <= 0 and self._charger_fault_idx < 0:
            self._charger_fault_idx = random.randint(0, 5)
            self._alarm_charger[self._charger_fault_idx] = True
            self._charger_fault_timer = random.uniform(600, 1800)
            events.append(self._evt(
                device=f"Charger Station-{self._charger_fault_idx+1}", point="Charger Fault",
                description=f"Forklift charger Station-{self._charger_fault_idx+1} fault — overcurrent",
                severity=Severity.WARNING, state=AlarmState.ACTIVE,
                value="fault", threshold="normal",
            ))
        elif self._charger_fault_idx >= 0:
            self._charger_fault_timer -= dt
            if self._charger_fault_timer <= 0:
                idx = self._charger_fault_idx
                self._alarm_charger[idx] = False
                self._charger_fault_idx = -1
                self._charger_fault_timer = random.uniform(7200, 28800)
                events.append(self._evt(
                    device=f"Charger Station-{idx+1}", point="Charger Fault",
                    description=f"Forklift charger Station-{idx+1} fault cleared",
                    severity=Severity.INFO, state=AlarmState.CLEARED,
                    value="normal", threshold="normal",
                ))

        # Fire sprinkler pressure
        self.sprinkler_psi = _clamp(
            _jit(_sin(self.t, 10800, 15.0, 120.0), 1.0), 80.0, 150.0
        )
        if self.sprinkler_psi <= 95.0 and not self._alarm_sprinkler:
            self._alarm_sprinkler = True
            events.append(self._evt(
                device="Fire Sprinkler Main", point="System Pressure",
                description=f"Sprinkler system pressure LOW: {self.sprinkler_psi:.0f} PSI",
                severity=Severity.CRITICAL, state=AlarmState.ACTIVE,
                value=round(self.sprinkler_psi, 0), threshold=95.0, unit="PSI",
            ))
        elif self.sprinkler_psi > 105.0 and self._alarm_sprinkler:
            self._alarm_sprinkler = False
            events.append(self._evt(
                device="Fire Sprinkler Main", point="System Pressure",
                description=f"Sprinkler system pressure normal: {self.sprinkler_psi:.0f} PSI",
                severity=Severity.INFO, state=AlarmState.CLEARED,
                value=round(self.sprinkler_psi, 0), threshold=95.0, unit="PSI",
            ))

        # Exhaust fan failures
        for i in range(4):
            self._exhaust_fail_timer[i] -= dt
            if self._exhaust_fail_timer[i] <= 0:
                if not self._alarm_exhaust[i]:
                    self._alarm_exhaust[i] = True
                    self._exhaust_fail_timer[i] = random.uniform(300, 900)
                    events.append(self._evt(
                        device=f"Exhaust Fan-{i+1}", point="Run Status",
                        description=f"Exhaust Fan-{i+1} failure — motor fault",
                        severity=Severity.WARNING, state=AlarmState.ACTIVE,
                        value="off", threshold="running",
                    ))
                else:
                    self._alarm_exhaust[i] = False
                    self._exhaust_fail_timer[i] = random.uniform(36000, 72000)
                    events.append(self._evt(
                        device=f"Exhaust Fan-{i+1}", point="Run Status",
                        description=f"Exhaust Fan-{i+1} restored",
                        severity=Severity.INFO, state=AlarmState.CLEARED,
                        value="running", threshold="running",
                    ))

        return events


# ---------------------------------------------------------------------------
# Multi-site orchestrator
# ---------------------------------------------------------------------------
BUILDING_SIMULATORS = [
    LegionOfHonorSim,
    OfficeTowerSim,
    HospitalLabSim,
    DataCenterSim,
    WarehouseSim,
]


class MultiSiteSimulator:
    """Runs all building simulators in parallel, emitting events via callback."""

    def __init__(
        self,
        on_alarm: Optional[Callable[[AlarmEvent], Awaitable[None]]] = None,
        tick_interval: float = TICK,
        buildings: Optional[list[str]] = None,
    ):
        self.on_alarm = on_alarm
        self.tick_interval = tick_interval
        self.running = False

        if buildings:
            name_set = {b.lower() for b in buildings}
            self.sims = [
                cls() for cls in BUILDING_SIMULATORS
                if cls.BUILDING_NAME.lower() in name_set
            ]
        else:
            self.sims = [cls() for cls in BUILDING_SIMULATORS]

        log.info(
            "MultiSiteSimulator initialized: %s",
            [s.BUILDING_NAME for s in self.sims],
        )

    async def _emit(self, event: AlarmEvent):
        if self.on_alarm:
            await self.on_alarm(event)

    async def run(self):
        self.running = True
        log.info(
            "MultiSiteSimulator started (tick=%ds, buildings=%d)",
            self.tick_interval, len(self.sims),
        )
        try:
            while self.running:
                for sim in self.sims:
                    events = sim.tick(self.tick_interval)
                    for event in events:
                        log.info(
                            "%-18s %-8s %-7s %s | %s",
                            event.building, event.state, event.severity,
                            event.point, event.description,
                        )
                        await self._emit(event)
                await asyncio.sleep(self.tick_interval)
        except asyncio.CancelledError:
            pass
        finally:
            self.running = False
            log.info("MultiSiteSimulator stopped.")

    def stop(self):
        self.running = False

    def _find_sim(self, building: str) -> Optional[BuildingSimulator]:
        for s in self.sims:
            if s.BUILDING_NAME.lower() == building.lower():
                return s
        return None

    async def trigger_scenario(self, scenario: str) -> list[AlarmEvent]:
        """Inject a canned failure scenario. Returns the events produced."""
        events: list[AlarmEvent] = []
        fn = SCENARIOS.get(scenario)
        if fn:
            events = fn(self)
            for event in events:
                log.info(
                    "SCENARIO %-18s %-8s %-7s %s | %s",
                    event.building, event.state, event.severity,
                    event.point, event.description,
                )
                await self._emit(event)
        return events

    @property
    def scenario_names(self) -> list[str]:
        return list(SCENARIOS.keys())


# ---------------------------------------------------------------------------
# Canned failure scenarios
# ---------------------------------------------------------------------------
def _scenario_chiller_cascade(sim: MultiSiteSimulator) -> list[AlarmEvent]:
    """Data Center: both chillers trip, CRACs overheat."""
    dc = sim._find_sim("Data Center")
    if not dc:
        return []
    events = []
    for i in range(2):
        dc.chiller_temps[i] = 52.0
        dc._alarm_chiller[i] = True
        events.append(dc._evt(
            device=f"Chiller-{i+1}", point="Leaving Water Temp",
            description=f"Chiller-{i+1} TRIPPED — LWT {dc.chiller_temps[i]:.1f}°F",
            severity=Severity.CRITICAL, state=AlarmState.ACTIVE,
            value=dc.chiller_temps[i], threshold=49.0, unit="°F",
        ))
    for i in range(8):
        dc.crac_temps[i] = 78.0 + random.uniform(0, 4)
        dc._alarm_crac[i] = True
        events.append(dc._evt(
            device=f"CRAC-{i+1}", point="Supply Air Temp",
            description=f"CRAC-{i+1} supply temp HIGH: {dc.crac_temps[i]:.1f}°F — chiller loss",
            severity=Severity.CRITICAL, state=AlarmState.ACTIVE,
            value=round(dc.crac_temps[i], 1), threshold=72.0, unit="°F",
        ))
    return events


def _scenario_power_failure(sim: MultiSiteSimulator) -> list[AlarmEvent]:
    """Data Center: all 3 UPS draining, generators starting."""
    dc = sim._find_sim("Data Center")
    if not dc:
        return []
    events = []
    for i in range(3):
        dc.ups_battery[i] = 35.0
        dc._alarm_ups[i] = True
        events.append(dc._evt(
            device=f"UPS-{i+1}", point="Battery Level",
            description=f"UPS-{i+1} on battery: {dc.ups_battery[i]:.0f}% — mains lost",
            severity=Severity.CRITICAL, state=AlarmState.ACTIVE,
            value=dc.ups_battery[i], threshold=50.0, unit="%",
        ))
    for i in range(2):
        events.append(dc._evt(
            device=f"Generator-{i+1}", point="Run Status",
            description=f"Generator-{i+1} AUTO-START initiated",
            severity=Severity.WARNING, state=AlarmState.ACTIVE,
            value="starting", threshold="standby",
        ))
    return events


def _scenario_freezer_failure(sim: MultiSiteSimulator) -> list[AlarmEvent]:
    """Hospital/Lab: multiple freezers warming — compressor bank failure."""
    hosp = sim._find_sim("Hospital/Lab")
    if not hosp:
        return []
    events = []
    for i in range(4):
        hosp.freezer_temps[i] = -68.0 + random.uniform(0, 5)
        hosp._alarm_freezer[i] = True
        events.append(hosp._evt(
            device=f"ULT Freezer-{i+1}", point="Cabinet Temp",
            description=f"Freezer-{i+1} WARMING: {hosp.freezer_temps[i]:.1f}°C — specimen risk",
            severity=Severity.CRITICAL, state=AlarmState.ACTIVE,
            value=round(hosp.freezer_temps[i], 1), threshold=-75.0, unit="°C",
        ))
    return events


def _scenario_clean_room_breach(sim: MultiSiteSimulator) -> list[AlarmEvent]:
    """Hospital/Lab: all clean rooms lose positive pressure."""
    hosp = sim._find_sim("Hospital/Lab")
    if not hosp:
        return []
    events = []
    for i in range(3):
        hosp.cr_pressure[i] = -0.01
        hosp._alarm_cr[i] = True
        events.append(hosp._evt(
            device=f"Clean Room {i+1}", point="Room Pressure",
            description=f"Clean Room {i+1} NEGATIVE pressure: {hosp.cr_pressure[i]:.3f} in wc — containment lost",
            severity=Severity.CRITICAL, state=AlarmState.ACTIVE,
            value=hosp.cr_pressure[i], threshold=0.01, unit="in wc",
        ))
    events.append(hosp._evt(
        device="O2 Manifold", point="Supply Pressure",
        description="Medical O2 pressure dropping: 42 PSI",
        severity=Severity.CRITICAL, state=AlarmState.ACTIVE,
        value=42.0, threshold=45.0, unit="PSI",
    ))
    return events


def _scenario_humidity_spike(sim: MultiSiteSimulator) -> list[AlarmEvent]:
    """Legion of Honor: all gallery zones spike high simultaneously."""
    loh = sim._find_sim("Legion of Honor")
    if not loh:
        return []
    events = []
    loh.space_rh = 62.0
    loh._alarm_space_rh = True
    events.append(loh._evt(
        device="DriSteem VL6 (11002)", point="SpaceRH",
        description=f"Gallery SpaceRH SPIKE: {loh.space_rh:.1f}%",
        severity=Severity.CRITICAL, state=AlarmState.ACTIVE,
        value=loh.space_rh, threshold=60.0, unit="%RH",
    ))
    for zone in ["2-05", "2-06"]:
        loh.gal_rh[zone] = 61.0 + random.uniform(0, 2)
        loh._alarm_gal[zone] = True
        events.append(loh._evt(
            device="LOH SC+ Proxy (3001)", point=f"Gallery {zone} RH",
            description=f"Gallery {zone} RH SPIKE: {loh.gal_rh[zone]:.1f}%",
            severity=Severity.CRITICAL, state=AlarmState.ACTIVE,
            value=round(loh.gal_rh[zone], 1), threshold=58.0, unit="%RH",
        ))
    return events


def _scenario_warehouse_flood(sim: MultiSiteSimulator) -> list[AlarmEvent]:
    """Warehouse: all roof leak sensors trip, sprinkler pressure drops."""
    wh = sim._find_sim("Warehouse")
    if not wh:
        return []
    events = []
    for i in range(4):
        wh._alarm_leak[i] = True
        events.append(wh._evt(
            device=f"Roof Leak Sensor Zone-{i+1}", point="Moisture Detected",
            description=f"FLOOD — Roof leak Zone {i+1}",
            severity=Severity.CRITICAL, state=AlarmState.ACTIVE,
            value="wet", threshold="dry",
        ))
    wh.sprinkler_psi = 85.0
    wh._alarm_sprinkler = True
    events.append(wh._evt(
        device="Fire Sprinkler Main", point="System Pressure",
        description=f"Sprinkler pressure CRITICAL: {wh.sprinkler_psi:.0f} PSI",
        severity=Severity.CRITICAL, state=AlarmState.ACTIVE,
        value=wh.sprinkler_psi, threshold=95.0, unit="PSI",
    ))
    return events


def _scenario_elevator_entrapment(sim: MultiSiteSimulator) -> list[AlarmEvent]:
    """Office Tower: both elevators fault simultaneously."""
    ot = sim._find_sim("Office Tower")
    if not ot:
        return []
    events = []
    for i in range(2):
        ot._alarm_elev[i] = True
        events.append(ot._evt(
            device=f"Elevator-{i+1}", point="Drive Fault",
            description=f"Elevator-{i+1} ENTRAPMENT — drive fault, car stopped between floors",
            severity=Severity.CRITICAL, state=AlarmState.ACTIVE,
            value="fault", threshold="normal",
        ))
    return events


SCENARIOS: dict[str, Callable[[MultiSiteSimulator], list[AlarmEvent]]] = {
    "chiller_cascade": _scenario_chiller_cascade,
    "power_failure": _scenario_power_failure,
    "freezer_failure": _scenario_freezer_failure,
    "clean_room_breach": _scenario_clean_room_breach,
    "humidity_spike": _scenario_humidity_spike,
    "warehouse_flood": _scenario_warehouse_flood,
    "elevator_entrapment": _scenario_elevator_entrapment,
}


# ---------------------------------------------------------------------------
# Backward compat: SimulatorV2 = Legion of Honor only
# ---------------------------------------------------------------------------
class SimulatorV2(MultiSiteSimulator):
    """Legacy single-building simulator (Legion of Honor only)."""

    def __init__(
        self,
        on_alarm: Optional[Callable[[AlarmEvent], Awaitable[None]]] = None,
        tick_interval: float = TICK,
    ):
        super().__init__(
            on_alarm=on_alarm,
            tick_interval=tick_interval,
            buildings=["Legion of Honor"],
        )
