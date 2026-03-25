#!/usr/bin/env python3
"""
Legion of Honor BMS Simulator
==============================
Serves two BACnet/IP devices based on real snapshot + alarm history:

  Device 11001  H-1 UC600       port = --port      (default 47808)
  Device 11002  DriSteem VL6    port = --port + 1   (default 47809)

Alarm patterns from Tracer Synchrony history Feb 26 – Mar 21 2026:
  - SpaceRH cycles 51 -> ~61 -> 57 %RH, firing every 30-90 min
  - DuctRH occasional high-limit fault
  - SafetyInterlock occasional toggle (every 2-4 hours)

Usage:
  pip3 install BAC0          # pulls in bacpypes3 as dependency
  python3 bms_simulator.py --ip 192.168.x.x --port 47808

Poll from Pi:
  import BAC0
  bacnet = BAC0.lite(ip='<pi-ip>/24')
  val = bacnet.read('<mac-ip>:47808 analogInput 1 presentValue device:11001')
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
from bacpypes3.local.binary import BinaryInputObject
from bacpypes3.local.multistate import MultiStateValueObject
from bacpypes3.ipv4.app import NormalApplication

# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("sim")

TICK = 10           # seconds per simulation step
STATUS_EVERY = 30   # print status every N ticks (~5 minutes)

# ---------------------------------------------------------------------------
# Simulation state  (all values from real snapshot + Synchrony data)
# ---------------------------------------------------------------------------
class S:
    t = 0.0

    # UC600
    teh2_hum   = 50.1;  teh2_tmp = 70.0
    teh1_hum   = 49.6;  teh1_tmp = 70.3
    hum1_demand = 49.7
    master_en  = True

    # DriSteem
    space_rh        = 51.0    # THE chronic alarm point
    space_dp        = 49.9
    duct_rh         = 66.5
    stm_dmnd_mass   = 0.0;  stm_dmnd_pct  = 0.0
    stm_out_mass    = 0.0;  stm_out_pct   = 0.0
    tank_tmp        = 78.0
    rh_setpt        = 40.0   # SpaceRHSetPoint (real snapshot value)
    duct_hl_setpt   = 80.0
    pid_band        = 10.0
    w_ads           = 60.0
    w_svc           = 19587.0
    airflow         = True
    duct_hl_sw      = False
    interlock       = True
    runmode         = 2       # 2=run  3=fault

    # internal
    _hum_phase   = 0.0
    _hum_cycle   = 55.0   # minutes
    _alarm_on    = False
    _duct_flt    = False
    _ilock_timer = 3600.0 * 3


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _sin(t, period, amp, base):
    return base + amp * math.sin(2 * math.pi * t / period)

def _jit(v, sigma=0.15):
    return v + random.gauss(0, sigma)

def _clamp(v, lo, hi):
    return max(lo, min(hi, v))


# ---------------------------------------------------------------------------
# Simulation tick
# ---------------------------------------------------------------------------
def tick():
    s = S
    s.t += TICK

    # --- UC600 ---
    s.teh2_hum = _jit(_sin(s.t, 7200, 2.5, 50.1))
    s.teh2_tmp = _jit(_sin(s.t, 5400, 0.8, 70.0))
    s.teh1_hum = _jit(_sin(s.t, 6800, 2.2, 49.6))
    s.teh1_tmp = _jit(_sin(s.t, 5000, 0.9, 70.3))
    avg_rh = (s.teh1_hum + s.teh2_hum) / 2.0
    s.hum1_demand = _jit(_clamp((43.0 - avg_rh) * 5.0 + 50.0, 0, 100), 0.5)

    # --- DriSteem: humidity cycling ---
    # From real alarm logs: 51% rises to 60-61%, clears at 57-59%, 30-90 min cycle
    s._hum_phase += TICK / 60.0
    cycle = s._hum_cycle
    phase = (s._hum_phase % cycle) / cycle   # 0..1

    if phase < 0.60:
        rh_base = 51.0 + (phase / 0.60) * 10.0       # rising  51->61
    else:
        rh_base = 61.0 - ((phase - 0.60) / 0.40) * 4.0  # falling 61->57

    s.space_rh = _clamp(_jit(rh_base, 0.3), 48.0, 63.0)

    if s.space_rh >= 60.0 and not s._alarm_on:
        s._alarm_on = True
        log.warning(f"ALARM  SpaceRH Out of Range High  {s.space_rh:.1f}%  "
                    f"(setpt {s.rh_setpt}%)")
    if s.space_rh <= 59.0 and s._alarm_on:
        s._alarm_on = False
        log.info(   f"CLEAR  SpaceRH Returned to Normal  {s.space_rh:.1f}%")

    # randomise cycle length when phase wraps
    if s._hum_phase % cycle < (TICK / 60.0):
        s._hum_cycle = random.uniform(30.0, 90.0)

    # --- DuctRH ---
    s.duct_rh = _clamp(_jit(_sin(s.t, 14400, 8.0, 66.5)), 45.0, 90.0)
    if s.duct_rh >= 80.0 and not s._duct_flt:
        s._duct_flt = True
        log.warning(f"ALARM  DuctRH High Limit  {s.duct_rh:.1f}%")
    if s.duct_rh < 78.0 and s._duct_flt:
        s._duct_flt = False
        log.info(   f"CLEAR  DuctRH normal  {s.duct_rh:.1f}%")
    s.duct_hl_sw = s._duct_flt

    # --- Steam demand ---
    rh_err = s.rh_setpt - s.space_rh
    stm = _clamp(rh_err * 8.0, 0, 100) if (rh_err > 0 and s.airflow and s.interlock) else 0.0
    s.stm_dmnd_pct  = _clamp(_jit(stm, 0.3), 0, 100)
    s.stm_dmnd_mass = s.stm_dmnd_pct * 0.12
    s.stm_out_pct   = s.stm_dmnd_pct
    s.stm_out_mass  = s.stm_dmnd_mass

    # --- Tank temp ---
    s.tank_tmp = _clamp(s.tank_tmp + (0.05 if s.stm_out_pct > 5 else -0.02), 70, 212)
    s.tank_tmp = _jit(s.tank_tmp, 0.1)

    # --- Dewpoint (Magnus approx) ---
    tc = (69.0 - 32) * 5 / 9
    s.space_dp = (tc - (1 - s.space_rh / 100) * 17.0) * 9 / 5 + 32

    # --- Water counters ---
    drain = s.stm_out_mass * TICK / 3600.0
    s.w_ads = _clamp(s.w_ads - drain, 0, 200)
    s.w_svc = _clamp(s.w_svc - drain, 0, 50000)
    if s.w_ads < 1.0:
        s.w_ads = random.uniform(55, 65)
        log.info("       ADS drain cycle complete, refilled")

    # --- Safety interlock (occasional fault, 2-4 hr interval) ---
    s._ilock_timer -= TICK
    if s._ilock_timer <= 0:
        s.interlock = not s.interlock
        if not s.interlock:
            log.warning("ALARM  SafetyInterlock INACTIVE")
            s._ilock_timer = random.uniform(120, 300)
        else:
            log.info("CLEAR  SafetyInterlock restored ACTIVE")
            s._ilock_timer = random.uniform(7200, 14400)

    s.runmode = 3 if not s.interlock else 2


# ---------------------------------------------------------------------------
# Object factory helpers
# ---------------------------------------------------------------------------
def _ai(inst, name, val, units):
    return AnalogInputObject(
        objectIdentifier=ObjectIdentifier(f"analog-input,{inst}"),
        objectName=name,
        presentValue=float(val),
        units=EngineeringUnits(units),
        statusFlags=[False, False, False, False],
        covIncrement=0.1,
    )

def _av(inst, name, val, units):
    return AnalogValueObject(
        objectIdentifier=ObjectIdentifier(f"analog-value,{inst}"),
        objectName=name,
        presentValue=float(val),
        units=EngineeringUnits(units),
        statusFlags=[False, False, False, False],
        covIncrement=0.1,
    )

def _bi(inst, name, active):
    return BinaryInputObject(
        objectIdentifier=ObjectIdentifier(f"binary-input,{inst}"),
        objectName=name,
        presentValue=BinaryPV("active") if active else BinaryPV("inactive"),
        statusFlags=[False, False, False, False],
        polarity=Polarity("normal"),
    )

def _msv(inst, name, val, n=10):
    return MultiStateValueObject(
        objectIdentifier=ObjectIdentifier(f"multi-state-value,{inst}"),
        objectName=name,
        presentValue=int(val),
        numberOfStates=n,
        statusFlags=[False, False, False, False],
    )


# ---------------------------------------------------------------------------
# Device definitions
# ---------------------------------------------------------------------------
def build_uc600():
    s = S
    dev = DeviceObject(
        objectIdentifier=ObjectIdentifier("device,11001"),
        objectName="H-1 UC600",
        description="Humidity/Temp Controller - Gallery System",
        vendorName="Trane",
        vendorIdentifier=14,
        modelName="UC600",
        firmwareRevision="1.0",
        applicationSoftwareVersion="1.0",
        maxApduLengthAccepted=1024,
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


def build_dristeem():
    s = S
    dev = DeviceObject(
        objectIdentifier=ObjectIdentifier("device,11002"),
        objectName="DriSteem VL6",
        description="Steam Humidifier Controller - Gallery H-1",
        vendorName="DriSteem",
        vendorIdentifier=0,
        modelName="VL6",
        firmwareRevision="1.0",
        applicationSoftwareVersion="1.0",
        maxApduLengthAccepted=1024,
    )
    objs = [
        _ai(1,  "SpaceRH",              s.space_rh,       "percent-relative-humidity"),
        _ai(2,  "SpaceDewPoint",        s.space_dp,       "degrees-fahrenheit"),
        _ai(3,  "DuctRH",               s.duct_rh,        "percent-relative-humidity"),
        _ai(4,  "SteamDemandMass",      s.stm_dmnd_mass,  "pounds-mass-per-hour"),
        _ai(5,  "SteamDemandPercent",   s.stm_dmnd_pct,   "percent"),
        _ai(6,  "AuxTemp",              0.0,              "degrees-fahrenheit"),
        _ai(7,  "TankTemperature",      s.tank_tmp,       "degrees-fahrenheit"),
        _ai(8,  "MTSteamDemandMass",    0.0,              "no-units"),
        _ai(9,  "MTSteamDemandPercent", 0.0,              "percent"),
        _av(1,  "SteamOutputMass",      s.stm_out_mass,   "pounds-mass-per-hour"),
        _av(2,  "SteamOutputPercent",   s.stm_out_pct,    "percent"),
        _av(3,  "WaterUntilADS",        s.w_ads,          "pounds-mass"),
        _av(4,  "WaterUntilService",    s.w_svc,          "pounds-mass"),
        _av(5,  "SpaceRHSetPoint",      s.rh_setpt,       "percent-relative-humidity"),
        _av(6,  "SpaceDewPointSetPoint",50.0,             "degrees-fahrenheit"),
        _av(7,  "DuctHighLimitSetPoint",s.duct_hl_setpt,  "percent-relative-humidity"),
        _av(8,  "FieldbusDemandMass",   0.0,              "pounds-mass-per-hour"),
        _av(9,  "FieldbusDemandPcnt",   0.0,              "percent"),
        _av(10, "PIDBand",              s.pid_band,       "percent-relative-humidity"),
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
# Update live objects from sim state
# ---------------------------------------------------------------------------
def _set_ai(omap, inst, val):
    o = omap.get(("analog-input", inst))
    if o:
        o.presentValue = float(val)

def _set_av(omap, inst, val):
    o = omap.get(("analog-value", inst))
    if o:
        o.presentValue = float(val)

def _set_bi(omap, inst, active):
    o = omap.get(("binary-input", inst))
    if o:
        o.presentValue = BinaryPV("active") if active else BinaryPV("inactive")

def _set_msv(omap, inst, val):
    o = omap.get(("multi-state-value", inst))
    if o:
        o.presentValue = int(val)


def update_objects(om1, om2):
    s = S
    # UC600
    _set_ai(om1, 1, s.teh2_hum);   _set_ai(om1, 2, s.teh2_tmp)
    _set_ai(om1, 3, s.teh1_hum);   _set_ai(om1, 4, s.teh1_tmp)
    _set_ai(om1, 5, s.hum1_demand)
    o = om1.get(("binary-input", 1))  # R-1 HUM-1 Master Enable
    if o: o.presentValue = BinaryPV("active") if s.master_en else BinaryPV("inactive")

    # DriSteem analog inputs
    for inst, val in [
        (1, s.space_rh), (2, s.space_dp), (3, s.duct_rh),
        (4, s.stm_dmnd_mass), (5, s.stm_dmnd_pct), (7, s.tank_tmp),
    ]:
        _set_ai(om2, inst, val)

    # DriSteem analog values
    for inst, val in [
        (1, s.stm_out_mass), (2, s.stm_out_pct),
        (3, s.w_ads),        (4, s.w_svc),
    ]:
        _set_av(om2, inst, val)

    # DriSteem binary inputs
    _set_bi(om2, 1, s.airflow)
    _set_bi(om2, 2, s.duct_hl_sw)
    _set_bi(om2, 3, s.interlock)

    # DriSteem runmode
    _set_msv(om2, 1, s.runmode)


# ---------------------------------------------------------------------------
# Status print
# ---------------------------------------------------------------------------
def print_status():
    s = S
    m = int(s.t / 60)
    al = "HIGH ⚠️ " if s._alarm_on  else "ok    "
    du = "FAULT⚠️ " if s._duct_flt  else "ok    "
    ik = "FAULT⚠️ " if not s.interlock else "safe  "
    log.info(
        f"[T+{m:4d}m] SpaceRH={s.space_rh:5.1f}%({al}) "
        f"DuctRH={s.duct_rh:5.1f}%({du}) "
        f"Tank={s.tank_tmp:5.1f}°F  Steam={s.stm_out_pct:5.1f}%  "
        f"Interlock={ik}"
    )
    log.info(
        f"           TEH1={s.teh1_hum:.1f}%/{s.teh1_tmp:.1f}°F  "
        f"TEH2={s.teh2_hum:.1f}%/{s.teh2_tmp:.1f}°F  "
        f"HUM1={s.hum1_demand:.1f}%"
    )


# ---------------------------------------------------------------------------
# Build an object map from a list of BACnet objects
# ---------------------------------------------------------------------------
def obj_map(objs):
    m = {}
    for o in objs:
        oid = o.objectIdentifier
        m[(str(oid[0]), oid[1])] = o
    return m


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
async def main(ip: str, port: int):
    log.info("Legion of Honor BMS Simulator starting")
    log.info(f"  UC600    device 11001  {ip}:{port}")
    log.info(f"  DriSteem device 11002  {ip}:{port+1}")

    addr1 = IPv4Address(f"{ip}:{port}")
    addr2 = IPv4Address(f"{ip}:{port+1}")

    dev1, objs1 = build_uc600()
    app1 = NormalApplication(dev1, addr1)
    om1 = obj_map(objs1)
    for o in objs1:
        app1.add_object(o)
    log.info(f"  UC600    {len(objs1)} objects registered")

    dev2, objs2 = build_dristeem()
    app2 = NormalApplication(dev2, addr2)
    om2 = obj_map(objs2)
    for o in objs2:
        app2.add_object(o)
    log.info(f"  DriSteem {len(objs2)} objects registered")

    log.info("Running. Ctrl+C to stop.\n")

    n = 0
    try:
        while True:
            await asyncio.sleep(TICK)
            tick()
            update_objects(om1, om2)
            n += 1
            if n % STATUS_EVERY == 0:
                print_status()
    except asyncio.CancelledError:
        pass
    finally:
        app1.close()
        app2.close()
        log.info("Stopped.")


if __name__ == "__main__":
    p = argparse.ArgumentParser(description="LOH BMS Simulator")
    p.add_argument("--ip",   required=True,
                   help="Mac Mini IP (no mask), e.g. 192.168.1.50")
    p.add_argument("--port", type=int, default=47808,
                   help="Base UDP port. UC600=port, DriSteem=port+1 (default 47808)")
    args = p.parse_args()
    try:
        asyncio.run(main(args.ip, args.port))
    except KeyboardInterrupt:
        pass
