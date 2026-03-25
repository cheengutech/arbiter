#!/usr/bin/env python3
"""
Arbiter — BMS Alarm Intelligence Poller
========================================
Polls the Legion of Honor BMS (or simulator) via BACnet/IP using BAC0,
scores alarm conditions, and sends SMS via Twilio for critical events.

Usage:
  pip install BAC0 twilio python-dotenv
  python3 core/bacnet_client.py
"""

import asyncio
import os
import sys
import logging
from datetime import datetime
from dotenv import load_dotenv
from twilio.rest import Client
import BAC0

sys.stdout.reconfigure(reconfigure=True)
load_dotenv()

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
BMS_IP         = os.getenv("BMS_IP", "10.10.60.50/24")   # live BMS LAN IP/mask
POLL_INTERVAL  = int(os.getenv("POLL_INTERVAL", "300"))   # seconds (default 5 min)

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
# Each rule: BAC0 address string, threshold check, severity, message builder
# BAC0 address format: '<network>:<device_instance> <objectType> <instance> <property>'
# ---------------------------------------------------------------------------
RULES = [
    {
        "name":     "TEH-2 Humidity High",
        "address":  "11:1 analogInput 1 presentValue",
        "check":    lambda v: float(v) > 55.0,
        "severity": "CRITICAL",
        "msg":      lambda v: f"TEH-2 Return Air Humidity HIGH: {float(v):.1f}% (limit 55%)",
    },
    {
        "name":     "TEH-2 Humidity Low",
        "address":  "11:1 analogInput 1 presentValue",
        "check":    lambda v: float(v) < 40.0,
        "severity": "WARNING",
        "msg":      lambda v: f"TEH-2 Return Air Humidity LOW: {float(v):.1f}% (limit 40%)",
    },
    {
        "name":     "TEH-1 Humidity High",
        "address":  "11:1 analogInput 3 presentValue",
        "check":    lambda v: float(v) > 55.0,
        "severity": "CRITICAL",
        "msg":      lambda v: f"TEH-1 Return Air Humidity HIGH: {float(v):.1f}% (limit 55%)",
    },
    {
        "name":     "TEH-1 Humidity Low",
        "address":  "11:1 analogInput 3 presentValue",
        "check":    lambda v: float(v) < 40.0,
        "severity": "WARNING",
        "msg":      lambda v: f"TEH-1 Return Air Humidity LOW: {float(v):.1f}% (limit 40%)",
    },
    {
        "name":     "SpaceRH High",
        "address":  "11:2 analogInput 1 presentValue",
        "check":    lambda v: float(v) >= 60.0,
        "severity": "CRITICAL",
        "msg":      lambda v: f"Gallery SpaceRH HIGH: {float(v):.1f}% (limit 60%)",
    },
    {
        "name":     "DuctRH High Limit",
        "address":  "11:2 analogInput 3 presentValue",
        "check":    lambda v: float(v) >= 80.0,
        "severity": "WARNING",
        "msg":      lambda v: f"DuctRH High Limit: {float(v):.1f}% (limit 80%)",
    },
    {
        "name":     "SafetyInterlock Fault",
        "address":  "11:2 binaryInput 3 presentValue",
        "check":    lambda v: str(v) == "inactive",
        "severity": "CRITICAL",
        "msg":      lambda v: "Humidifier SafetyInterlock INACTIVE — check unit immediately",
    },
    {
        "name":     "Humidifier Fault Mode",
        "address":  "11:2 multiStateValue 1 presentValue",
        "check":    lambda v: int(v) == 3,
        "severity": "WARNING",
        "msg":      lambda v: "DriSteem Runmode = FAULT (3)",
    },
]

# Tracks active alarm state per rule to avoid repeat SMS
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
# Single poll cycle
# ---------------------------------------------------------------------------
async def poll_cycle(bacnet):
    now = datetime.now().strftime("%H:%M:%S")
    log.info(f"--- Poll {now} ---")

    new_alarms     = []
    cleared_alarms = []

    for rule in RULES:
        try:
            val = await bacnet.read(rule["address"])
        except Exception as e:
            log.warning(f"  {rule['name']:35s} READ FAILED: {e}")
            continue

        in_alarm     = rule["check"](val)
        was_in_alarm = alarm_state[rule["name"]]

        try:
            display = f"{float(val):.2f}"
        except (TypeError, ValueError):
            display = str(val)

        status = "⚠️  ALARM" if in_alarm else "✓"
        log.info(f"  {rule['name']:35s} {display:10s} {status}")

        if in_alarm and not was_in_alarm:
            alarm_state[rule["name"]] = True
            new_alarms.append((rule["severity"], rule["msg"](val)))

        elif not in_alarm and was_in_alarm:
            alarm_state[rule["name"]] = False
            cleared_alarms.append(rule["name"])
            log.info(f"  CLEARED: {rule['name']}")
            send_sms(f"CLEARED: {rule['name']}")

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
    log.info(f"  BMS IP    : {BMS_IP}")
    log.info(f"  Interval  : {POLL_INTERVAL}s")
    log.info(f"  SMS to    : {TWILIO_TO}")
    log.info("=" * 55)

    async with BAC0.start(ip=BMS_IP) as bacnet:
        await asyncio.sleep(5)
        await bacnet._discover()
        await asyncio.sleep(10)
        log.info("BACnet ready. Starting poll loop.\n")

        while True:
            try:
                new, cleared = await poll_cycle(bacnet)
                if new:
                    log.info(f"  → {new} new alarm(s), SMS sent")
                if cleared:
                    log.info(f"  → {cleared} alarm(s) cleared")
            except Exception as e:
                log.error(f"Poll error: {e}")

            await asyncio.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log.info("Arbiter stopped.")