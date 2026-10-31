import secrets
import asyncio
import httpx
from pathlib import Path
from contextlib import asynccontextmanager
from fastapi import FastAPI, HTTPException, Request, Security, Depends
from fastapi.responses import RedirectResponse
from fastapi.security.api_key import APIKeyHeader
from fastapi.staticfiles import StaticFiles
from starlette.status import HTTP_401_UNAUTHORIZED
from iaqualink.client import AqualinkClient
from lodgify import LodgifyClient, LodgifyError
from config import (
    EMAIL, PASSWORD, TEMP_OVERHEAT_THRESHOLD, TEMP_UNDERHEAT_THRESHOLD, TEMP_MONITOR_INTERVAL,
    AQUALINK_SPACONTROL_API_KEY, LODGIFY_API_KEY, validate_env, send_pushover_alert,
)
import admin
import events
import guest
import spa
from spa import get_system

#Define the header name for API key authentication
API_KEY_NAME = "X-API-KEY"
api_key_header = APIKeyHeader(name=API_KEY_NAME, auto_error=False)

# Dependency function to validate API key for protected endpoints
async def get_api_key(api_key: str = Security(api_key_header)):
    if api_key and AQUALINK_SPACONTROL_API_KEY and secrets.compare_digest(api_key.encode(), AQUALINK_SPACONTROL_API_KEY.encode()):
        return api_key
    raise HTTPException(status_code=HTTP_401_UNAUTHORIZED, detail="Invalid API Key")

def _ip(request: Request) -> str:
    return request.client.host if request.client else "unknown"

# Background task to monitor temperatures and trigger alerts/shutdowns based on thresholds
async def temp_monitor():
    while True:
        try:
            async with AqualinkClient(EMAIL, PASSWORD) as client:
                system = await get_system(client)
                await system.update()
                temp_sensor_spa = system.devices.get("spa_temp")
                temp_sensor_pool = system.devices.get("pool_temp")
                temp_sensor_air = system.devices.get("air_temp")

                if temp_sensor_air:
                    print(f"Current air temp: {temp_sensor_air.state}F")
                for sensor in [temp_sensor_spa, temp_sensor_pool]:
                    if sensor and sensor.state:
                        print(f"Current {sensor.name}: {sensor.state}F")

                alarm_overheat = any(
                    s and s.state and float(s.state) >= TEMP_OVERHEAT_THRESHOLD
                    for s in [temp_sensor_spa, temp_sensor_pool]
                )
                alarm_underheat = any(
                    s and s.state and float(s.state) <= TEMP_UNDERHEAT_THRESHOLD
                    for s in [temp_sensor_spa, temp_sensor_pool]
                )
                if alarm_overheat:
                    events.log_event("watchdog_overheat", "watchdog", detail={"threshold": TEMP_OVERHEAT_THRESHOLD})
                    await send_pushover_alert(f"ALARM: Pool/spa temp exceeded {TEMP_OVERHEAT_THRESHOLD}°F — emergency all-off triggered.")
                    r = await system._send_session_request("set_onetouch_1")
                    if r.status_code == 200:
                        print({"status": "success", "action": "all_off", "response": r.json()})
                        print(f"ALARM: Emergency shutdown — ALL-OFF (temp threshold exceeded).")
                    else:
                        print(f"ALARM: Failed to trigger all-off (temp threshold exceeded). Response: {r.text}")
                        print("Attempting manual pump shutdown...")
                        for pump_key in ("spa_pump", "pool_pump"):
                            pump = system.devices.get(pump_key)
                            if pump and pump.is_on:
                                await pump.turn_off()
                                print(f"ALARM: Emergency shutdown — {pump_key} off (temp threshold exceeded).")
                elif alarm_underheat:
                    events.log_event("watchdog_underheat", "watchdog", detail={"threshold": TEMP_UNDERHEAT_THRESHOLD})
                    await send_pushover_alert(f"ALERT: Pool/spa temp dropped below {TEMP_UNDERHEAT_THRESHOLD}°F — Check heating system.")
                    print(f"ALERT: Pool/spa temp dropped below {TEMP_UNDERHEAT_THRESHOLD}°F — Check heating system.")
                    r = await system._send_session_request("set_onetouch_4") #Turn on pool heater only
                    if r.status_code == 200:
                        print({"status": "success", "action": "pool_heater_on", "response": r.json()})
                        print(f"ALERT: Pool heater turned on (temp underheat threshold).")
                    else:
                        print(f"ALERT: Failed to turn on pool heater (temp underheat threshold). Response: {r.text}")

        except Exception as e:
            print(f"Watchdog Error: {e}")
        await asyncio.sleep(TEMP_MONITOR_INTERVAL)

# FastAPI lifespan context to manage startup/shutdown tasks, including environment validation and starting the temperature monitor
@asynccontextmanager
async def lifespan(app: FastAPI):
    validate_env()
    events.init_db()
    task = asyncio.create_task(temp_monitor())
    yield
    task.cancel()

# Initialize FastAPI app with lifespan context for startup/shutdown management
app = FastAPI(lifespan=lifespan)
app.mount("/static", StaticFiles(directory=Path(__file__).parent / "static"), name="static")
app.include_router(guest.router)
app.include_router(admin.router)

# Root URL sends guests to the spa portal (handy for QR codes)
@app.get("/", include_in_schema=False)
async def root():
    return RedirectResponse("/guest")

# Define API endpoints with authentication and device control logic
@app.api_route("/spa-on", methods=["GET", "POST"], dependencies=[Depends(get_api_key)])
async def spa_on(request: Request):
    try:
        await spa.spa_on()
    except spa.CooldownError:
        return {"status": "ignored", "reason": "Mechanical cooldown in progress"}
    events.log_event("api_spa_on", "api", _ip(request))
    return {"status": "success", "action": "spa_on", "note": "aux_1 will enable in 30s"}

# Endpoint to turn off spa and related devices, with cooldown protection and pool heater fallback
@app.api_route("/spa-off", methods=["GET", "POST"], dependencies=[Depends(get_api_key)])
async def spa_off(request: Request):
    try:
        await spa.spa_off()
    except spa.CooldownError:
        return {"status": "ignored", "reason": "Mechanical cooldown in progress"}
    events.log_event("api_spa_off", "api", _ip(request))
    return {"status": "success", "action": "spa_off"}

# Endpoint to list all devices with their current state and type information
@app.get("/devices", dependencies=[Depends(get_api_key)])
async def list_devices():
    async with AqualinkClient(EMAIL, PASSWORD) as client:
        system = await get_system(client)
        await system.update()
        devices = await system.get_devices()

    return {
        key: {
            "type": type(device).__name__,
            "state": device.state,
            "is_on": device.is_on if hasattr(device, "is_on") else None,
            "label": device.label,
        }
        for key, device in devices.items()
    }

# Endpoint to retrieve the current one-touch status from the Aqualink system
@app.get("/onetouch", dependencies=[Depends(get_api_key)])
async def get_onetouch():
    async with AqualinkClient(EMAIL, PASSWORD) as client:
        system = await get_system(client)
        r = await system._send_session_request("get_onetouch")
        return r.json()

# Endpoint to trigger the "all off" one-touch command, which turns off all pumps and heaters immediately
@app.api_route("/all-off", methods=["GET", "POST"], dependencies=[Depends(get_api_key)])
async def all_off(request: Request):
    async with AqualinkClient(EMAIL, PASSWORD) as client:
        system = await get_system(client)
        r = await system._send_session_request("set_onetouch_1")
        events.log_event("api_all_off", "api", _ip(request))
        return {"status": "success", "action": "all_off", "response": r.json()}

# Endpoint to trigger the "spa mode" one-touch command, which typically turns on the spa pump and heater and turns off the pool heater
@app.api_route("/spa-mode", methods=["GET", "POST"], dependencies=[Depends(get_api_key)])
async def spa_mode(request: Request):
    async with AqualinkClient(EMAIL, PASSWORD) as client:
        system = await get_system(client)
        r = await system._send_session_request("set_onetouch_2")
        events.log_event("api_spa_mode", "api", _ip(request))
        return {"status": "success", "action": "spa_mode", "response": r.json()}

# Endpoint to list Lodgify bookings (GET /v2/reservations/bookings)
@app.get("/lodgify/bookings", dependencies=[Depends(get_api_key)])
async def lodgify_bookings(page: int = 1, size: int = 50, stay_filter: str = "Current"):
    if not LODGIFY_API_KEY:
        raise HTTPException(status_code=503, detail="Lodgify not configured")
    try:
        async with LodgifyClient(LODGIFY_API_KEY) as lg:
            return await lg.get_bookings(page=page, size=size, stay_filter=stay_filter)
    except LodgifyError as e:
        print(f"Lodgify error: {e}")
        raise HTTPException(status_code=502, detail=f"Lodgify returned HTTP {e.status_code}")
    except httpx.HTTPError as e:
        print(f"Lodgify request failed: {type(e).__name__}")
        raise HTTPException(status_code=502, detail="Lodgify request failed")
