#!/usr/bin/env python3
"""
Arbiter — BMS Alarm Intelligence Poller
========================================
Polls the Legion of Honor BMS simulator (or live BMS) via BACnet/IP,
scores alarm conditions, and sends SMS via Twilio for critical events.

Devices polled:
  UC600    device 11001  <SIM_IP>:47808
  DriSteem device 11002  <SIM_IP>:47809

Alarm rules (from real Tracer Synchrony history):
  SpaceRH      >= 60.0 %    CRITICAL  (chronic gallery humidity alarm)
  DuctRH       >= 80.0 %    WARNING   (high limit fault)
  SafetyInterlock inactive  CRITICAL  (humidifier safety fault)
  Runmode      == 3         WARNING   (humidifier fault state)
  TEH humidity >= 55.0 %    WARNING   (return air humidity high)

Usage:
  pip3 install bacpypes3 twilio python-dotenv
  python3 arbiter_poller.py
"""

import asyncio
import os
import logging
from datetime import datetime
from dotenv import load_dotenv
from twilio.rest import Client
from bacpypes3.pdu import IPv4Address
from bacpypes3.primitivedata import ObjectIdentifier
from bacpypes3.ipv4.app import NormalApplication
from bacpypes3.local.device import DeviceObject

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
load_dotenv()

PI_IP          = os.getenv("PI_IP", "100.84.232.109") # Pi Tailscale IP
PI_PORT        = 47820 # local port for poller
SIM_IP         = os.getenv("SIM_IP", "100.101.170.34") # Mac Mini Tailscale IP (simulator)
UC600_PORT     = 47808 # UC600 port
DRISTEEM_PORT  = 47809 # DriSteem port
POLL_INTERVAL  = 60        # seconds

# Twilio
TWILIO_SID     = os.getenv("TWILIO_ACCOUNT_SID")
TWILIO_TOKEN   = os.getenv("TWILIO_AUTH_TOKEN")
TWILIO_FROM    = os.getenv("TWILIO_PHONE_NUMBER")
TWILIO_TO      = os.getenv("MY_PHONE_NUMBER")

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("arbiter")

# ---------------------------------------------------------------------------
# Alarm rules
# ---------------------------------------------------------------------------
RULES = [
    {
        "name":     "SpaceRH High",
        "port":     DRISTEEM_PORT,
        "obj":      "analog-input,1",
        "prop":     "present-value",
        "check":    lambda v: float(v) >= 60.0,
        "severity": "CRITICAL",
        "msg":      lambda v: f"Gallery SpaceRH Out of Range High: {float(v):.1f}% (limit 60%)",
    },
    {
        "name":     "DuctRH High Limit",
        "port":     DRISTEEM_PORT,
        "obj":      "analog-input,3",
        "prop":     "present-value",
        "check":    lambda v: float(v) >= 80.0,
        "severity": "WARNING",
        "msg":      lambda v: f"DuctRH High Limit: {float(v):.1f}% (limit 80%)",
    },
    {
        "name":     "SafetyInterlock Fault",
        "port":     DRISTEEM_PORT,
        "obj":      "binary-input,3",
        "prop":     "present-value",
        "check":    lambda v: str(v) == "inactive",
        "severity": "CRITICAL",
        "msg":      lambda v: "Humidifier SafetyInterlock INACTIVE — check unit immediately",
    },
    {
        "name":     "Humidifier Fault Mode",
        "port":     DRISTEEM_PORT,
        "obj":      "multi-state-value,1",
        "prop":     "present-value",
        "check":    lambda v: int(v) == 3,
        "severity": "WARNING",
        "msg":      lambda v: "DriSteem Runmode = FAULT (3)",
    },
    {
        "name":     "TEH-2 Humidity High",
        "port":     UC600_PORT,
        "obj":      "analog-input,1",
        "prop":     "present-value",
        "check":    lambda v: float(v) >= 55.0,
        "severity": "WARNING",
        "msg":      lambda v: f"TEH-2 Return Air Humidity High: {float(v):.1f}%",
    },
    {
        "name":     "TEH-1 Humidity High",
        "port":     UC600_PORT,
        "obj":      "analog-input,3",
        "prop":     "present-value",
        "check":    lambda v: float(v) >= 55.0,
        "severity": "WARNING",
        "msg":      lambda v: f"TEH-1 Return Air Humidity High: {float(v):.1f}%",
    },
]

# Tracks which alarms are currently active to avoid repeat SMS
alarm_state = {rule["name"]: False for rule in RULES}

# ---------------------------------------------------------------------------
# SMS
# ---------------------------------------------------------------------------
def send_sms(message: str):
    try:
        client = Client(TWILIO_SID, TWILIO_TOKEN)
        client.messages.create(
            body=f"[ARBITER] {message}",
            from_=TWILIO_FROM,
            to=TWILIO_TO,
        )
        log.info(f"SMS sent: {message}")
    except Exception as e:
        log.error(f"SMS failed: {e}")


# ---------------------------------------------------------------------------
# BACnet read helper
# ---------------------------------------------------------------------------
async def read_point(app, port: int, obj_id: str, prop: str):
    try:
        val = await app.read_property(
            IPv4Address(f"{SIM_IP}:{port}"),
            ObjectIdentifier(obj_id),
            prop,
        )
        return val
    except Exception as e:
        log.warning(f"  Read failed {obj_id}@{port}: {e}")
        return None


# ---------------------------------------------------------------------------
# Single poll cycle
# ---------------------------------------------------------------------------
async def poll_cycle(app):
    now = datetime.now().strftime("%H:%M:%S")
    log.info(f"--- Poll {now} ---")

    new_alarms     = []
    cleared_alarms = []

    for rule in RULES:
        val = await read_point(app, rule["port"], rule["obj"], rule["prop"])

        if val is None:
            log.warning(f"  {rule['name']:30s} NO READ")
            continue

        in_alarm     = rule["check"](val)
        was_in_alarm = alarm_state[rule["name"]]

        # Display value
        try:
            display = f"{float(val):.2f}"
        except (TypeError, ValueError):
            display = str(val)

        status = "⚠️  ALARM" if in_alarm else "✓"
        log.info(f"  {rule['name']:30s} {display:10s} {status}")

        if in_alarm and not was_in_alarm:
            alarm_state[rule["name"]] = True
            new_alarms.append((rule["severity"], rule["msg"](val)))

        elif not in_alarm and was_in_alarm:
            alarm_state[rule["name"]] = False
            cleared_alarms.append(rule["name"])
            log.info(f"  CLEARED: {rule['name']}")

    # Send SMS — CRITICAL first, then WARNING
    for severity, msg in sorted(new_alarms, key=lambda x: x[0] != "CRITICAL"):
        send_sms(f"{severity}: {msg}")

    return len(new_alarms), len(cleared_alarms)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
async def main():
    log.info("=" * 55)
    log.info("Arbiter BMS Alarm Poller")
    log.info(f"  Simulator : {SIM_IP}")
    log.info(f"  Interval  : {POLL_INTERVAL}s")
    log.info(f"  SMS to    : {TWILIO_TO}")
    log.info("=" * 55)

    dev = DeviceObject(
        objectIdentifier=ObjectIdentifier("device,9001"),
        objectName="Arbiter-Poller",
        maxApduLengthAccepted=1024,
    )
    app = NormalApplication(dev, IPv4Address(f"{PI_IP}:{PI_PORT}"))
    await asyncio.sleep(1)
    log.info("BACnet ready. Starting poll loop.\n")

    try:
        while True:
            try:
                new, cleared = await poll_cycle(app)
                if new:
                    log.info(f"  → {new} new alarm(s), SMS sent")
                if cleared:
                    log.info(f"  → {cleared} alarm(s) cleared")
            except Exception as e:
                log.error(f"Poll error: {e}")
            await asyncio.sleep(POLL_INTERVAL)

    except asyncio.CancelledError:
        pass
    finally:
        app.close()
        log.info("Arbiter stopped.")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
