# Arbiter

BMS alarm intelligence platform. Filters alarm noise and surfaces only critical alerts via SMS.

Built for facilities running Trane Tracer SC+ over BACnet/IP.

## Architecture

- **core/** — BACnet polling, alarm scoring, Twilio SMS
- **simulator/** — Home BMS simulator for offline development
- **api/** — FastAPI + WebSocket backend (Render)
- **dashboard/** — React frontend (Vercel)
- **scripts/** — Dev and test utilities

## Stack

- Python + BAC0 (BACnet)
- FastAPI + WebSockets
- Twilio SMS
- React (Vercel)
- Render (backend hosting)

## Setup
```bash
+
cat > README.md << 'EOF'
# Arbiter

BMS alarm intelligence platform. Filters alarm noise and surfaces only critical alerts via SMS.

Built for facilities running Trane Tracer SC+ over BACnet/IP.

## Architecture

- **core/** — BACnet polling, alarm scoring, Twilio SMS
- **simulator/** — Home BMS simulator for offline development
- **api/** — FastAPI + WebSocket backend (Render)
- **dashboard/** — React frontend (Vercel)
- **scripts/** — Dev and test utilities

## Stack

- Python + BAC0 (BACnet)
- FastAPI + WebSockets
- Twilio SMS
- React (Vercel)
- Render (backend hosting)

## Setup
```bash
cp .env.example .env
# Fill in your Twilio credentials and BACnet config
pip install bac0 fastapi uvicorn twilio
```

## Deployment

- Backend: Render
- Frontend: Vercel
- On-site node: Raspberry Pi 4 on BMS LAN
