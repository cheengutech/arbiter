#!/usr/bin/env python3
"""
Legion of Honor BMS Simulator — Full Building
===============================================
Three BACnet/IP devices based on real Tracer Synchrony/Summit data:

  Device 11001  H-1 UC600         port = --port      (default 47808)
  Device 11002  DriSteem VL6      port = --port + 1   (default 47809)
  Device 3001   LOH SC+ Proxy     port = --port + 2   (default 47810)
                AHU-1/SF-1, Chillers, Boiler, Cooling Tower,
                Gallery zones 2-01 through 2-10, Basement rooms

Alarm patterns from real Tracer Synchrony history Feb 26 – Mar 21 2026:
  - SpaceRH cycles 51->61->57% every 30-90 min (gallery humidity)
  - Gallery zone RHC valves maxing out (2-05, 2-06 chronic)
  - DuctRH high-limit fault when OA humidity peaks
  - SafetyInterlock occasional toggle (2-4 hr interval)
  - AHU-1 supply fan failure cluster (Mar 11-12 pattern)
  - Chiller leaving water temp drift
  - Boiler steam pressure vs 2.0 psi alarm setpoint

Usage:
  python3 bms_simulator.py --ip 100.101.170.34 --port 47808
"""

import asyncio
import argparse
import math
import random
import logging

from bacpypes3.pdu import IPv4Address
from bacpypes3.primitivedata import ObjectIdentifier
from bacpypes3.basetypes import EngineeringUnits, BinaryPV, Polarity
from bacpypes3.local.device import DeviceObject
from bacpypes3.local.analog import AnalogInputObject, AnalogValueObject
from bacpypes3.local.binary import BinaryInputObject, BinaryValueObject
from bacpypes3.local.multistate import MultiStateValueObject
from bacpypes3.ipv4.app import NormalApplication

# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("sim")

TICK        = 10   # seconds per simulation step
STATUS_EVERY = 30  # print status every N ticks (~5 min)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _sin(t, period, amp, base):
    return base + amp * math.sin(2 * math.pi * t / period)

def _cos(t, period, amp, base):
    return base + amp * math.cos(2 * math.pi * t / period)

def _jit(v, sigma=0.15):
    return v + random.gauss(0, sigma)

def _clamp(v, lo, hi):
    return max(lo, min(hi, v))


# ---------------------------------------------------------------------------
# Simulation state
# All baseline values from real Tracer Synchrony capture 2026-03-21
# ---------------------------------------------------------------------------
class S:
    t = 0.0

    # ---- UC600 (device 11001) ----
    teh2_hum    = 50.1;  teh2_tmp = 70.0
    teh1_hum    = 49.6;  teh1_tmp = 70.3
    hum1_demand = 49.7
    master_en   = True

    # ---- DriSteem VL6 (device 11002) ----
    space_rh        = 51.0
    space_dp        = 49.9
    duct_rh         = 66.5
    stm_dmnd_mass   = 0.0;  stm_dmnd_pct = 0.0
    stm_out_mass    = 0.0;  stm_out_pct  = 0.0
    tank_tmp        = 78.0
    rh_setpt        = 40.0
    duct_hl_setpt   = 80.0
    pid_band        = 10.0
    w_ads           = 60.0
    w_svc           = 19587.0
    airflow         = True
    duct_hl_sw      = False
    interlock       = True
    runmode         = 2

    # ---- AHU-1 / SF-1 (device 3001) ----
    # From Synchrony: SA 55.6°F, SA Hum 87.4%, RA 72.4°F, RA Hum 42.3%
    ahu1_sa_tmp     = 55.6   # supply air temp
    ahu1_sa_hum     = 87.4   # supply air humidity %
    ahu1_sa_flow    = 6819.5 # cfm
    ahu1_sa_static  = 0.896  # in H2O
    ahu1_ra_tmp     = 72.4   # return air temp
    ahu1_ra_hum     = 42.3   # return air humidity
    ahu1_ra_flow    = 1474.9 # cfm
    ahu1_chw_valve  = 68.8   # chilled water valve %
    ahu1_economizer = 10.0   # economizer pos %
    ahu1_fan_status = True   # supply fan running
    ahu1_rf2_status = True   # return fan running
    ahu1_filter     = True   # filter normal (True=normal)
    ahu1_smoke_det  = True   # smoke detector normal
    ahu1_oa_tmp     = 57.3   # outdoor air temp
    ahu1_oa_hum     = 77.0   # outdoor air humidity %

    # ---- Chiller Plant ----
    # CH-1: active, helical rotary, 60.1% RLA
    ch1_ewt         = 52.0   # entering water temp
    ch1_lwt         = 44.1   # leaving water temp (setpt 44.0)
    ch1_cond_ewt    = 81.8
    ch1_cond_lwt    = 88.8
    ch1_rla         = 60.1   # compressor % RLA
    ch1_status      = True   # running
    # CH-2: standby
    ch2_ewt         = 65.5
    ch2_lwt         = 59.6
    ch2_rla         = 0.0
    ch2_status      = False
    # CH-3: active, 250-ton air-cooled RTAC
    ch3_ewt         = 48.4
    ch3_lwt         = 44.4
    ch3_rla         = 49.7
    ch3_status      = True
    chw_setpt       = 44.0

    # ---- Cooling Tower ----
    ct_ent_tmp      = 87.2   # tower entering temp
    ct_lvg_tmp      = 80.5   # tower leaving temp
    ct_hl_setpt     = 90.0   # high temp alarm setpt
    ct1_vfd1_hz     = 14.1
    ct1_vfd2_hz     = 15.1
    ct_alarm        = False  # high temp alarm

    # ---- Boiler System ----
    # Boiler 2 active, Boiler 1 standby. Steam 3.71 psi, alarm setpt 2.0 psi
    boiler1_status  = False  # burner off
    boiler2_status  = True   # burner on
    steam_pressure  = 3.71   # psi
    boiler_alm_setpt= 2.0    # psi
    hw_pump_status  = True
    hw_pump_press   = 44.2   # PSIG
    hx_tmp          = 178.6  # heat exchanger temp (setpt 160)
    hx_setpt        = 160.0
    hx_tcv          = 27.2   # temp control valve %

    # ---- Gallery Zones (Garden Level Rosecrans) ----
    # From Synchrony: all zones ~67-70°F, RH 52-58%, setpt 69°F/50%RH
    # RHC valves: 2-05 and 2-06 chronically maxed
    gal_tmp = {
        "2-01": 67.9, "2-03": 68.3, "2-04": 68.2,
        "2-05": 67.6, "2-06": 69.1, "2-07": 69.1,
        "2-08": 69.9, "2-10": 58.8,
    }
    gal_rh = {
        "2-01": 52.3, "2-03": 52.0, "2-04": 56.2,
        "2-05": 57.3, "2-06": 55.3, "2-07": 58.1,
        "2-08": 54.1, "2-10": 50.6,
    }
    gal_rhc = {   # reheat valve positions %
        "2-01": 50.4, "2-03":  0.0, "2-04": 70.9,
        "2-05":100.0, "2-06": 99.8, "2-07": 24.0,
        "2-08": 55.0, "2-10": 38.8,
    }
    gal_tmp_setpt   = 69.0
    gal_rh_setpt    = 50.0

    # ---- Basement Rooms ----
    bsmt_rm101_tmp  = 69.3;  bsmt_rm101_rh = 49.5
    bsmt_rm105_tmp  = 70.3;  bsmt_rm105_rh = 48.1
    bsmt_rm106_tmp  = 69.2;  bsmt_rm106_rh = 50.8
    bsmt_mech_tmp   = 72.5

    # ---- Internal alarm/cycle state ----
    _hum_phase      = 0.0
    _hum_cycle      = 55.0
    _alarm_on       = False
    _duct_flt       = False
    _ilock_timer    = 3600.0 * 3
    _fan_fail_timer = 3600.0 * 36   # supply fan failure cluster every ~36 hrs
    _fan_fail_active= False
    _fan_fail_count = 0
    _ct_alarm       = False


# ---------------------------------------------------------------------------
# Simulation tick
# ---------------------------------------------------------------------------
def tick():
    s = S
    s.t += TICK

    # ===== UC600 =====
    s.teh2_hum = _jit(_sin(s.t, 7200, 2.5, 50.1))
    s.teh2_tmp = _jit(_sin(s.t, 5400, 0.8, 70.0))
    s.teh1_hum = _jit(_sin(s.t, 6800, 2.2, 49.6))
    s.teh1_tmp = _jit(_sin(s.t, 5000, 0.9, 70.3))
    avg_rh = (s.teh1_hum + s.teh2_hum) / 2.0
    s.hum1_demand = _jit(_clamp((43.0 - avg_rh) * 5.0 + 50.0, 0, 100), 0.5)

    # ===== DriSteem: humidity cycling =====
    s._hum_phase += TICK / 60.0
    cycle = s._hum_cycle
    phase = (s._hum_phase % cycle) / cycle
    if phase < 0.60:
        rh_base = 51.0 + (phase / 0.60) * 10.0
    else:
        rh_base = 61.0 - ((phase - 0.60) / 0.40) * 4.0
    s.space_rh = _clamp(_jit(rh_base, 0.3), 48.0, 63.0)

    if s.space_rh >= 60.0 and not s._alarm_on:
        s._alarm_on = True
        log.warning(f"ALARM  SpaceRH High  {s.space_rh:.1f}%")
    if s.space_rh <= 59.0 and s._alarm_on:
        s._alarm_on = False
        log.info(f"CLEAR  SpaceRH normal  {s.space_rh:.1f}%")
    if s._hum_phase % cycle < (TICK / 60.0):
        s._hum_cycle = random.uniform(30.0, 90.0)

    s.duct_rh = _clamp(_jit(_sin(s.t, 14400, 8.0, 66.5)), 45.0, 90.0)
    if s.duct_rh >= 80.0 and not s._duct_flt:
        s._duct_flt = True
        log.warning(f"ALARM  DuctRH High Limit  {s.duct_rh:.1f}%")
    if s.duct_rh < 78.0 and s._duct_flt:
        s._duct_flt = False
        log.info(f"CLEAR  DuctRH normal  {s.duct_rh:.1f}%")
    s.duct_hl_sw = s._duct_flt

    rh_err = s.rh_setpt - s.space_rh
    stm = _clamp(rh_err * 8.0, 0, 100) if (rh_err > 0 and s.airflow and s.interlock) else 0.0
    s.stm_dmnd_pct  = _clamp(_jit(stm, 0.3), 0, 100)
    s.stm_dmnd_mass = s.stm_dmnd_pct * 0.12
    s.stm_out_pct   = s.stm_dmnd_pct
    s.stm_out_mass  = s.stm_dmnd_mass
    s.tank_tmp = _clamp(s.tank_tmp + (0.05 if s.stm_out_pct > 5 else -0.02), 70, 212)
    s.tank_tmp = _jit(s.tank_tmp, 0.1)
    tc = (69.0 - 32) * 5 / 9
    s.space_dp = (tc - (1 - s.space_rh / 100) * 17.0) * 9 / 5 + 32
    drain = s.stm_out_mass * TICK / 3600.0
    s.w_ads = _clamp(s.w_ads - drain, 0, 200)
    s.w_svc = _clamp(s.w_svc - drain, 0, 50000)
    if s.w_ads < 1.0:
        s.w_ads = random.uniform(55, 65)
        log.info("       ADS drain cycle complete")

    s._ilock_timer -= TICK
    if s._ilock_timer <= 0:
        s.interlock = not s.interlock
        if not s.interlock:
            log.warning("ALARM  SafetyInterlock INACTIVE")
            s._ilock_timer = random.uniform(120, 300)
        else:
            log.info("CLEAR  SafetyInterlock ACTIVE")
            s._ilock_timer = random.uniform(7200, 14400)
    s.runmode = 3 if not s.interlock else 2

    # ===== AHU-1 / SF-1 =====
    # OA conditions drift slowly (SF Bay Area marine layer pattern)
    s.ahu1_oa_tmp = _jit(_sin(s.t, 43200, 8.0, 57.0))   # 12-hr cycle 49-65°F
    s.ahu1_oa_hum = _jit(_sin(s.t, 43200, 10.0, 77.0))  # 12-hr cycle 67-87%

    # Supply air temp tracks setpoint (55°F) with small deviation
    s.ahu1_sa_tmp = _jit(_sin(s.t, 3600, 0.4, 55.6))
    # Supply air humidity is high because OA is humid and economizer open
    s.ahu1_sa_hum = _jit(_clamp(s.ahu1_oa_hum * 0.95 + 5.0, 70.0, 95.0))
    # Return air tracks gallery zones
    s.ahu1_ra_tmp = _jit(_sin(s.t, 7200, 1.0, 72.4))
    s.ahu1_ra_hum = _jit(_sin(s.t, 7200, 2.0, 42.3))
    # Chilled water valve responds to load
    s.ahu1_chw_valve = _jit(_sin(s.t, 5400, 8.0, 68.8))
    s.ahu1_chw_valve = _clamp(s.ahu1_chw_valve, 20.0, 95.0)
    # Supply flow and static pressure
    s.ahu1_sa_flow   = _jit(_sin(s.t, 7200, 150.0, 6819.5), 10.0)
    s.ahu1_sa_static = _jit(0.896, 0.01)
    s.ahu1_ra_flow   = _jit(1474.9, 15.0)
    s.ahu1_economizer= _clamp(_jit(10.0, 0.5), 5.0, 40.0)

    # Supply fan failure cluster — from real Mar 11-12 pattern
    # Fires every ~36 hours, 4-6 rapid in/out cycles over ~20 minutes
    s._fan_fail_timer -= TICK
    if s._fan_fail_timer <= 0 and not s._fan_fail_active:
        s._fan_fail_active = True
        s._fan_fail_count  = random.randint(4, 6)
        log.warning("ALARM  AHU-1 Supply Fan Failure (cluster start)")
    if s._fan_fail_active:
        s._fan_fail_count -= 1
        s.ahu1_fan_status = not s.ahu1_fan_status
        if s.ahu1_fan_status:
            log.info("CLEAR  AHU-1 Supply Fan Returned to Normal")
        else:
            log.warning("ALARM  AHU-1 Supply Fan Failure")
        if s._fan_fail_count <= 0:
            s._fan_fail_active = False
            s.ahu1_fan_status  = True
            s._fan_fail_timer  = random.uniform(3600 * 24, 3600 * 48)
            log.info("       Supply fan failure cluster ended")

    # ===== Chiller Plant =====
    # CH-1 active: LWT tracks setpoint with ±0.5°F variance
    s.ch1_lwt     = _jit(_sin(s.t, 3600, 0.4, 44.1))
    s.ch1_ewt     = _jit(_sin(s.t, 3600, 1.5, 52.0))
    s.ch1_rla     = _jit(_sin(s.t, 7200, 8.0, 60.1))
    s.ch1_cond_ewt= _jit(_sin(s.t, 3600, 2.0, 81.8))
    s.ch1_cond_lwt= _jit(_sin(s.t, 3600, 2.0, 88.8))

    # CH-3 active: air-cooled 250-ton
    s.ch3_lwt = _jit(_sin(s.t, 3600, 0.3, 44.4))
    s.ch3_ewt = _jit(_sin(s.t, 3600, 1.2, 48.4))
    s.ch3_rla = _jit(_sin(s.t, 7200, 6.0, 49.7))

    # CH-2 standby: occasional warm-up
    s.ch2_lwt = _jit(_sin(s.t, 14400, 3.0, 59.6))
    s.ch2_ewt = _jit(_sin(s.t, 14400, 3.0, 65.5))

    # ===== Cooling Tower =====
    s.ct_ent_tmp = _jit(_sin(s.t, 3600, 1.5, 87.2))
    s.ct_lvg_tmp = _jit(_sin(s.t, 3600, 1.5, 80.5))
    s.ct1_vfd1_hz= _jit(_sin(s.t, 5400, 1.0, 14.1))
    s.ct1_vfd2_hz= _jit(_sin(s.t, 5400, 1.0, 15.1))
    # High temp alarm if entering > 90°F
    if s.ct_ent_tmp >= 90.0 and not s._ct_alarm:
        s._ct_alarm = True
        log.warning(f"ALARM  Cooling Tower High Temp  {s.ct_ent_tmp:.1f}°F")
    if s.ct_ent_tmp < 89.0 and s._ct_alarm:
        s._ct_alarm = False
        log.info(f"CLEAR  Cooling Tower temp normal  {s.ct_ent_tmp:.1f}°F")
    s.ct_alarm = s._ct_alarm

    # ===== Boiler =====
    # Steam pressure drifts around 3.71 psi (alarm setpt 2.0 psi — we're safe)
    s.steam_pressure = _jit(_sin(s.t, 3600, 0.3, 3.71))
    s.steam_pressure = _clamp(s.steam_pressure, 1.5, 5.0)
    s.hx_tmp  = _jit(_sin(s.t, 3600, 2.0, 178.6))
    s.hx_tcv  = _jit(_sin(s.t, 7200, 5.0, 27.2))
    s.hw_pump_press = _jit(44.2, 0.3)

    # ===== Gallery Zones =====
    # Each zone drifts independently, 2-05 and 2-06 RH chronically high
    # RHC valves respond to zone RH vs setpoint
    for zone in s.gal_rh:
        # Different drift periods per zone for variety
        period = 3600 * (3 + hash(zone) % 4)
        amp_rh = 3.0 if zone in ("2-05", "2-06", "2-07") else 2.0
        base_rh = s.gal_rh[zone]
        new_rh = _jit(_sin(s.t, period, amp_rh, base_rh), 0.2)
        s.gal_rh[zone] = _clamp(new_rh, 45.0, 65.0)

        base_tmp = s.gal_tmp[zone]
        new_tmp = _jit(_sin(s.t, period * 1.3, 0.8, base_tmp), 0.1)
        s.gal_tmp[zone] = _clamp(new_tmp, 62.0, 74.0)

        # RHC valve: higher when RH is above setpoint (dehumidifying)
        rh_over = s.gal_rh[zone] - s.gal_rh_setpt
        rhc = _clamp(50.0 + rh_over * 8.0, 0.0, 100.0)
        s.gal_rhc[zone] = _jit(rhc, 1.0)

    # ===== Basement Rooms =====
    s.bsmt_rm101_tmp = _jit(_sin(s.t, 5400, 0.5, 69.3))
    s.bsmt_rm101_rh  = _jit(_sin(s.t, 7200, 1.5, 49.5))
    s.bsmt_rm105_tmp = _jit(_sin(s.t, 6000, 0.4, 70.3))
    s.bsmt_rm105_rh  = _jit(_sin(s.t, 7200, 1.5, 48.1))
    s.bsmt_rm106_tmp = _jit(_sin(s.t, 5000, 0.5, 69.2))
    s.bsmt_rm106_rh  = _jit(_sin(s.t, 7200, 1.5, 50.8))
    s.bsmt_mech_tmp  = _jit(_sin(s.t, 9000, 1.0, 72.5))


# ---------------------------------------------------------------------------
# Object factory helpers
# ---------------------------------------------------------------------------
def _ai(inst, name, val, units):
    return AnalogInputObject(
        objectIdentifier=ObjectIdentifier(f"analog-input,{inst}"),
        objectName=name, presentValue=float(val),
        units=EngineeringUnits(units),
        statusFlags=[False,False,False,False], covIncrement=0.1,
    )

def _av(inst, name, val, units):
    return AnalogValueObject(
        objectIdentifier=ObjectIdentifier(f"analog-value,{inst}"),
        objectName=name, presentValue=float(val),
        units=EngineeringUnits(units),
        statusFlags=[False,False,False,False], covIncrement=0.1,
    )

def _bi(inst, name, active):
    return BinaryInputObject(
        objectIdentifier=ObjectIdentifier(f"binary-input,{inst}"),
        objectName=name,
        presentValue=BinaryPV("active") if active else BinaryPV("inactive"),
        statusFlags=[False,False,False,False], polarity=Polarity("normal"),
    )

def _bv(inst, name, active):
    return BinaryValueObject(
        objectIdentifier=ObjectIdentifier(f"binary-value,{inst}"),
        objectName=name,
        presentValue=BinaryPV("active") if active else BinaryPV("inactive"),
        statusFlags=[False,False,False,False],
    )

def _msv(inst, name, val, n=10):
    return MultiStateValueObject(
        objectIdentifier=ObjectIdentifier(f"multi-state-value,{inst}"),
        objectName=name, presentValue=int(val), numberOfStates=n,
        statusFlags=[False,False,False,False],
    )


# ---------------------------------------------------------------------------
# Device 11001 — H-1 UC600
# ---------------------------------------------------------------------------
def build_uc600():
    s = S
    dev = DeviceObject(
        objectIdentifier=ObjectIdentifier("device,11001"),
        objectName="H-1 UC600",
        description="Humidity/Temp Controller - Gallery System",
        vendorName="Trane", vendorIdentifier=14,
        modelName="UC600", firmwareRevision="1.0",
        applicationSoftwareVersion="1.0", maxApduLengthAccepted=1024,
    )
    objs = [
        _ai(1, "TEH-2 Local Humidity",           s.teh2_hum,    "percent"),
        _ai(2, "TEH-2 Local Temperature",         s.teh2_tmp,    "degrees-fahrenheit"),
        _ai(3, "TEH-1 Local Humidity",            s.teh1_hum,    "percent"),
        _ai(4, "TEH-1 Local Temperature",         s.teh1_tmp,    "degrees-fahrenheit"),
        _ai(5, "HUM-1 Demand Signal",             s.hum1_demand, "percent"),
        _bi(1, "R-1 HUM-1 Master Enable Command", s.master_en),
    ]
    return dev, objs


# ---------------------------------------------------------------------------
# Device 11002 — DriSteem VL6
# ---------------------------------------------------------------------------
def build_dristeem():
    s = S
    dev = DeviceObject(
        objectIdentifier=ObjectIdentifier("device,11002"),
        objectName="DriSteem VL6",
        description="Steam Humidifier Controller - Gallery H-1",
        vendorName="DriSteem", vendorIdentifier=0,
        modelName="VL6", firmwareRevision="1.0",
        applicationSoftwareVersion="1.0", maxApduLengthAccepted=1024,
    )
    objs = [
        _ai(1,  "SpaceRH",              s.space_rh,      "percent-relative-humidity"),
        _ai(2,  "SpaceDewPoint",        s.space_dp,      "degrees-fahrenheit"),
        _ai(3,  "DuctRH",               s.duct_rh,       "percent-relative-humidity"),
        _ai(4,  "SteamDemandMass",      s.stm_dmnd_mass, "pounds-mass-per-hour"),
        _ai(5,  "SteamDemandPercent",   s.stm_dmnd_pct,  "percent"),
        _ai(6,  "AuxTemp",              0.0,             "degrees-fahrenheit"),
        _ai(7,  "TankTemperature",      s.tank_tmp,      "degrees-fahrenheit"),
        _ai(8,  "MTSteamDemandMass",    0.0,             "no-units"),
        _ai(9,  "MTSteamDemandPercent", 0.0,             "percent"),
        _av(1,  "SteamOutputMass",      s.stm_out_mass,  "pounds-mass-per-hour"),
        _av(2,  "SteamOutputPercent",   s.stm_out_pct,   "percent"),
        _av(3,  "WaterUntilADS",        s.w_ads,         "pounds-mass"),
        _av(4,  "WaterUntilService",    s.w_svc,         "pounds-mass"),
        _av(5,  "SpaceRHSetPoint",      s.rh_setpt,      "percent-relative-humidity"),
        _av(6,  "SpaceDewPointSetPoint",50.0,            "degrees-fahrenheit"),
        _av(7,  "DuctHighLimitSetPoint",s.duct_hl_setpt, "percent-relative-humidity"),
        _av(8,  "FieldbusDemandMass",   0.0,             "pounds-mass-per-hour"),
        _av(9,  "FieldbusDemandPcnt",   0.0,             "percent"),
        _av(10, "PIDBand",              s.pid_band,      "percent-relative-humidity"),
        _bi(1,  "AirflowProvingSwitch", s.airflow),
        _bi(2,  "DuctHLSwitch",         s.duct_hl_sw),
        _bi(3,  "SafetyInterlock",      s.interlock),
        _bi(8,  "MTActiveFaultInSystem",    False),
        _bi(9,  "MTActiveMessageInSystem",  False),
        _msv(1, "Runmode",    s.runmode),
        _msv(2, "MT_Runmode", 2),
    ]
    return dev, objs


# ---------------------------------------------------------------------------
# Device 3001 — LOH SC+ Proxy (AHU, Chillers, Boiler, Galleries)
# ---------------------------------------------------------------------------
def build_sc_proxy():
    s = S
    dev = DeviceObject(
        objectIdentifier=ObjectIdentifier("device,3001"),
        objectName="LOH SC-01 Proxy",
        description="Legion of Honor Trane SC+ BMS Proxy",
        vendorName="Trane", vendorIdentifier=14,
        modelName="Tracer SC+", firmwareRevision="v6.00.2017",
        applicationSoftwareVersion="1.0", maxApduLengthAccepted=1024,
    )
    objs = []

    # --- AHU-1 / SF-1 (AI 1-20) ---
    objs += [
        _ai(1,  "AHU-1 Supply Air Temperature",     s.ahu1_sa_tmp,    "degrees-fahrenheit"),
        _ai(2,  "AHU-1 Supply Air Humidity",         s.ahu1_sa_hum,    "percent"),
        _ai(3,  "AHU-1 Supply Air Flow",             s.ahu1_sa_flow,   "cubic-feet-per-minute"),
        _ai(4,  "AHU-1 Supply Duct Static Pressure", s.ahu1_sa_static, "inches-of-water"),
        _ai(5,  "AHU-1 Return Air Temperature",      s.ahu1_ra_tmp,    "degrees-fahrenheit"),
        _ai(6,  "AHU-1 Return Air Humidity",         s.ahu1_ra_hum,    "percent"),
        _ai(7,  "AHU-1 Return Air Flow",             s.ahu1_ra_flow,   "cubic-feet-per-minute"),
        _ai(8,  "AHU-1 Chilled Water Valve",         s.ahu1_chw_valve, "percent"),
        _ai(9,  "AHU-1 Economizer Position",         s.ahu1_economizer,"percent"),
        _ai(10, "AHU-1 Outdoor Air Temperature",     s.ahu1_oa_tmp,    "degrees-fahrenheit"),
        _ai(11, "AHU-1 Outdoor Air Humidity",        s.ahu1_oa_hum,    "percent"),
    ]
    objs += [
        _bi(1,  "AHU-1 Supply Fan Status",    s.ahu1_fan_status),
        _bi(2,  "AHU-1 RF-2 Fan Status",      s.ahu1_rf2_status),
        _bi(3,  "AHU-1 Filter Status Normal", s.ahu1_filter),
        _bi(4,  "AHU-1 Duct Smoke Detector",  s.ahu1_smoke_det),
    ]

    # --- Chillers (AI 20-40) ---
    objs += [
        _ai(20, "CH-1 Leaving Water Temperature",    s.ch1_lwt,     "degrees-fahrenheit"),
        _ai(21, "CH-1 Entering Water Temperature",   s.ch1_ewt,     "degrees-fahrenheit"),
        _ai(22, "CH-1 Compressor RLA",               s.ch1_rla,     "percent"),
        _ai(23, "CH-1 Condenser Entering Temp",      s.ch1_cond_ewt,"degrees-fahrenheit"),
        _ai(24, "CH-1 Condenser Leaving Temp",       s.ch1_cond_lwt,"degrees-fahrenheit"),
        _ai(25, "CH-2 Leaving Water Temperature",    s.ch2_lwt,     "degrees-fahrenheit"),
        _ai(26, "CH-2 Entering Water Temperature",   s.ch2_ewt,     "degrees-fahrenheit"),
        _ai(27, "CH-2 Compressor RLA",               s.ch2_rla,     "percent"),
        _ai(28, "CH-3 Leaving Water Temperature",    s.ch3_lwt,     "degrees-fahrenheit"),
        _ai(29, "CH-3 Entering Water Temperature",   s.ch3_ewt,     "degrees-fahrenheit"),
        _ai(30, "CH-3 Compressor RLA",               s.ch3_rla,     "percent"),
    ]
    objs += [
        _av(20, "Chilled Water Setpoint",     s.chw_setpt,   "degrees-fahrenheit"),
        _bi(10, "CH-1 Run Status",            s.ch1_status),
        _bi(11, "CH-2 Run Status",            s.ch2_status),
        _bi(12, "CH-3 Run Status",            s.ch3_status),
    ]

    # --- Cooling Tower (AI 40-50) ---
    objs += [
        _ai(40, "CT-1 Entering Water Temperature",  s.ct_ent_tmp,   "degrees-fahrenheit"),
        _ai(41, "CT-1 Leaving Water Temperature",   s.ct_lvg_tmp,   "degrees-fahrenheit"),
        _ai(42, "CT-1 VFD-1 Speed",                 s.ct1_vfd1_hz,  "hertz"),
        _ai(43, "CT-1 VFD-2 Speed",                 s.ct1_vfd2_hz,  "hertz"),
        _av(40, "CT High Temp Alarm Setpoint",       s.ct_hl_setpt,  "degrees-fahrenheit"),
        _bi(20, "CT-1 High Temp Alarm",              s.ct_alarm),
    ]

    # --- Boiler / HX (AI 50-60) ---
    objs += [
        _ai(50, "Steam Pressure",                   s.steam_pressure, "pounds-force-per-square-inch"),
        _ai(51, "Heat Exchanger Temperature",        s.hx_tmp,         "degrees-fahrenheit"),
        _ai(52, "HX Temperature Control Valve",     s.hx_tcv,         "percent"),
        _ai(53, "HW Pump Discharge Pressure",       s.hw_pump_press,  "pounds-force-per-square-inch"),
        _av(50, "Boiler Alarm Pressure Setpoint",   s.boiler_alm_setpt,"pounds-force-per-square-inch"),
        _av(51, "HX Temperature Setpoint",          s.hx_setpt,       "degrees-fahrenheit"),
        _bi(30, "Boiler-1 Burner Status",           s.boiler1_status),
        _bi(31, "Boiler-2 Burner Status",           s.boiler2_status),
        _bi(32, "HW Pump Status",                   s.hw_pump_status),
    ]

    # --- Gallery Zones (AI 100+, one slot per zone per parameter) ---
    zone_list = ["2-01","2-03","2-04","2-05","2-06","2-07","2-08","2-10"]
    for i, z in enumerate(zone_list):
        base = 100 + i * 3
        objs += [
            _ai(base+0, f"Gallery {z} Space Temperature", s.gal_tmp[z], "degrees-fahrenheit"),
            _ai(base+1, f"Gallery {z} Space Humidity",    s.gal_rh[z],  "percent"),
            _ai(base+2, f"Gallery {z} RHC Valve",         s.gal_rhc[z], "percent"),
        ]
    objs += [
        _av(100, "Gallery Temperature Setpoint", s.gal_tmp_setpt, "degrees-fahrenheit"),
        _av(101, "Gallery RH Setpoint",          s.gal_rh_setpt,  "percent"),
    ]

    # --- Basement Rooms (AI 130+) ---
    objs += [
        _ai(130, "Room 1-01 Space Temperature",  s.bsmt_rm101_tmp, "degrees-fahrenheit"),
        _ai(131, "Room 1-01 Space Humidity",     s.bsmt_rm101_rh,  "percent"),
        _ai(132, "Room 1-05 Space Temperature",  s.bsmt_rm105_tmp, "degrees-fahrenheit"),
        _ai(133, "Room 1-05 Space Humidity",     s.bsmt_rm105_rh,  "percent"),
        _ai(134, "Room 1-06 Space Temperature",  s.bsmt_rm106_tmp, "degrees-fahrenheit"),
        _ai(135, "Room 1-06 Space Humidity",     s.bsmt_rm106_rh,  "percent"),
        _ai(136, "Mech Room Temperature",        s.bsmt_mech_tmp,  "degrees-fahrenheit"),
    ]

    return dev, objs


# ---------------------------------------------------------------------------
# Object map builder
# ---------------------------------------------------------------------------
def obj_map(objs):
    m = {}
    for o in objs:
        oid = o.objectIdentifier
        m[(str(oid[0]), oid[1])] = o
    return m


# ---------------------------------------------------------------------------
# Update live BACnet objects from sim state
# ---------------------------------------------------------------------------
def _set_ai(om, inst, val):
    o = om.get(("analog-input", inst))
    if o: o.presentValue = float(val)

def _set_av(om, inst, val):
    o = om.get(("analog-value", inst))
    if o: o.presentValue = float(val)

def _set_bi(om, inst, active):
    o = om.get(("binary-input", inst))
    if o: o.presentValue = BinaryPV("active") if active else BinaryPV("inactive")

def _set_bv(om, inst, active):
    o = om.get(("binary-value", inst))
    if o: o.presentValue = BinaryPV("active") if active else BinaryPV("inactive")

def _set_msv(om, inst, val):
    o = om.get(("multi-state-value", inst))
    if o: o.presentValue = int(val)


def update_all(om1, om2, om3):
    s = S

    # UC600
    _set_ai(om1, 1, s.teh2_hum); _set_ai(om1, 2, s.teh2_tmp)
    _set_ai(om1, 3, s.teh1_hum); _set_ai(om1, 4, s.teh1_tmp)
    _set_ai(om1, 5, s.hum1_demand)
    o = om1.get(("binary-input", 1))
    if o: o.presentValue = BinaryPV("active") if s.master_en else BinaryPV("inactive")

    # DriSteem
    for inst, val in [(1,s.space_rh),(2,s.space_dp),(3,s.duct_rh),
                      (4,s.stm_dmnd_mass),(5,s.stm_dmnd_pct),(7,s.tank_tmp)]:
        _set_ai(om2, inst, val)
    for inst, val in [(1,s.stm_out_mass),(2,s.stm_out_pct),(3,s.w_ads),(4,s.w_svc)]:
        _set_av(om2, inst, val)
    _set_bi(om2, 1, s.airflow); _set_bi(om2, 2, s.duct_hl_sw); _set_bi(om2, 3, s.interlock)
    _set_msv(om2, 1, s.runmode)

    # SC+ Proxy — AHU-1
    for inst, val in [
        (1,s.ahu1_sa_tmp),(2,s.ahu1_sa_hum),(3,s.ahu1_sa_flow),
        (4,s.ahu1_sa_static),(5,s.ahu1_ra_tmp),(6,s.ahu1_ra_hum),
        (7,s.ahu1_ra_flow),(8,s.ahu1_chw_valve),(9,s.ahu1_economizer),
        (10,s.ahu1_oa_tmp),(11,s.ahu1_oa_hum),
    ]:
        _set_ai(om3, inst, val)
    _set_bi(om3, 1, s.ahu1_fan_status); _set_bi(om3, 2, s.ahu1_rf2_status)
    _set_bi(om3, 3, s.ahu1_filter);     _set_bi(om3, 4, s.ahu1_smoke_det)

    # SC+ Proxy — Chillers
    for inst, val in [
        (20,s.ch1_lwt),(21,s.ch1_ewt),(22,s.ch1_rla),(23,s.ch1_cond_ewt),(24,s.ch1_cond_lwt),
        (25,s.ch2_lwt),(26,s.ch2_ewt),(27,s.ch2_rla),
        (28,s.ch3_lwt),(29,s.ch3_ewt),(30,s.ch3_rla),
    ]:
        _set_ai(om3, inst, val)
    _set_bi(om3, 10, s.ch1_status); _set_bi(om3, 11, s.ch2_status); _set_bi(om3, 12, s.ch3_status)

    # SC+ Proxy — Cooling Tower
    for inst, val in [(40,s.ct_ent_tmp),(41,s.ct_lvg_tmp),(42,s.ct1_vfd1_hz),(43,s.ct1_vfd2_hz)]:
        _set_ai(om3, inst, val)
    _set_bi(om3, 20, s.ct_alarm)

    # SC+ Proxy — Boiler/HX
    for inst, val in [(50,s.steam_pressure),(51,s.hx_tmp),(52,s.hx_tcv),(53,s.hw_pump_press)]:
        _set_ai(om3, inst, val)
    _set_bi(om3, 30, s.boiler1_status); _set_bi(om3, 31, s.boiler2_status)
    _set_bi(om3, 32, s.hw_pump_status)

    # SC+ Proxy — Gallery Zones
    zone_list = ["2-01","2-03","2-04","2-05","2-06","2-07","2-08","2-10"]
    for i, z in enumerate(zone_list):
        base = 100 + i * 3
        _set_ai(om3, base+0, s.gal_tmp[z])
        _set_ai(om3, base+1, s.gal_rh[z])
        _set_ai(om3, base+2, s.gal_rhc[z])

    # SC+ Proxy — Basement
    for inst, val in [
        (130,s.bsmt_rm101_tmp),(131,s.bsmt_rm101_rh),
        (132,s.bsmt_rm105_tmp),(133,s.bsmt_rm105_rh),
        (134,s.bsmt_rm106_tmp),(135,s.bsmt_rm106_rh),
        (136,s.bsmt_mech_tmp),
    ]:
        _set_ai(om3, inst, val)


# ---------------------------------------------------------------------------
# Status print
# ---------------------------------------------------------------------------
def print_status():
    s = S
    m = int(s.t / 60)
    log.info(f"[T+{m:4d}m] ── AHU-1 ──  SA={s.ahu1_sa_tmp:.1f}°F  "
             f"SA_Hum={s.ahu1_sa_hum:.1f}%  RA={s.ahu1_ra_tmp:.1f}°F  "
             f"CHW={s.ahu1_chw_valve:.1f}%  Fan={'ON' if s.ahu1_fan_status else 'OFF'}")
    log.info(f"          ── Chillers ──  CH1 LWT={s.ch1_lwt:.1f}°F {s.ch1_rla:.0f}%RLA  "
             f"CH3 LWT={s.ch3_lwt:.1f}°F {s.ch3_rla:.0f}%RLA  "
             f"CWSetpt={s.chw_setpt:.1f}°F")
    log.info(f"          ── Boiler ──  Steam={s.steam_pressure:.2f}psi  "
             f"HX={s.hx_tmp:.1f}°F  Boiler2={'ON' if s.boiler2_status else 'OFF'}")
    gal_str = "  ".join(f"{z}:{s.gal_rh[z]:.0f}%" for z in ["2-05","2-06","2-07","2-08"])
    log.info(f"          ── Galleries ──  {gal_str}")
    al = "⚠️ HIGH" if s._alarm_on else "ok"
    log.info(f"          ── Humid ──  SpaceRH={s.space_rh:.1f}%({al})  "
             f"DuctRH={s.duct_rh:.1f}%  Tank={s.tank_tmp:.1f}°F  "
             f"Interlock={'safe' if s.interlock else '⚠️ FAULT'}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
async def main(ip: str, port: int):
    log.info("=" * 60)
    log.info("Legion of Honor BMS Simulator — Full Building")
    log.info(f"  UC600    device 11001  {ip}:{port}")
    log.info(f"  DriSteem device 11002  {ip}:{port+1}")
    log.info(f"  SC+ Proxy device 3001  {ip}:{port+2}")
    log.info("=" * 60)

    dev1, objs1 = build_uc600()
    app1 = NormalApplication(dev1, IPv4Address(f"{ip}:{port}"))
    om1 = obj_map(objs1)
    for o in objs1: app1.add_object(o)

    dev2, objs2 = build_dristeem()
    app2 = NormalApplication(dev2, IPv4Address(f"{ip}:{port+1}"))
    om2 = obj_map(objs2)
    for o in objs2: app2.add_object(o)

    dev3, objs3 = build_sc_proxy()
    app3 = NormalApplication(dev3, IPv4Address(f"{ip}:{port+2}"))
    om3 = obj_map(objs3)
    for o in objs3: app3.add_object(o)

    log.info(f"  UC600    {len(objs1)} objects")
    log.info(f"  DriSteem {len(objs2)} objects")
    log.info(f"  SC+ Proxy {len(objs3)} objects")
    log.info("Running. Ctrl+C to stop.\n")

    n = 0
    try:
        while True:
            await asyncio.sleep(TICK)
            tick()
            update_all(om1, om2, om3)
            n += 1
            if n % STATUS_EVERY == 0:
                print_status()
    except asyncio.CancelledError:
        pass
    finally:
        app1.close(); app2.close(); app3.close()
        log.info("Stopped.")


if __name__ == "__main__":
    p = argparse.ArgumentParser(description="LOH BMS Simulator — Full Building")
    p.add_argument("--ip",   required=True, help="Mac Mini IP, e.g. 100.101.170.34")
    p.add_argument("--port", type=int, default=47808,
                   help="Base port. UC600=port, DriSteem=port+1, SC+=port+2")
    args = p.parse_args()
    try:
        asyncio.run(main(args.ip, args.port))
    except KeyboardInterrupt:
        pass