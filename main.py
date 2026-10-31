import os
import secrets
import time
import asyncio
import httpx
from contextlib import asynccontextmanager
from fastapi import FastAPI, BackgroundTasks, HTTPException, Security, Depends
from fastapi.security.api_key import APIKeyHeader
from starlette.status import HTTP_401_UNAUTHORIZED
from iaqualink.client import AqualinkClient
from dotenv import load_dotenv
from lodgify import LodgifyClient, LodgifyError

load_dotenv()

#Define the header name for API key authentication
API_KEY_NAME = "X-API-KEY"
api_key_header = APIKeyHeader(name=API_KEY_NAME, auto_error=False)

# Load environment variables
EMAIL = os.getenv("AQUALINK_EMAIL")
PASSWORD = os.getenv("AQUALINK_PASSWORD")
SERIAL = os.getenv("AQUALINK_SERIAL")
TEMP_OVERHEAT_THRESHOLD = int(os.getenv("TEMP_OVERHEAT_THRESHOLD", "105"))
TEMP_UNDERHEAT_THRESHOLD = int(os.getenv("TEMP_UNDERHEAT_THRESHOLD", "80"))
TEMP_MONITOR_INTERVAL = int(os.getenv("TEMP_MONITOR_INTERVAL", "600"))
VALVE_COOLDOWN = int(os.getenv("VALVE_COOLDOWN", "60"))  # Protects mechanical rotation
PUSHOVER_USER = os.getenv("PUSHOVER_USER")
PUSHOVER_TOKEN = os.getenv("PUSHOVER_TOKEN")
AQUALINK_SPACONTROL_API_KEY = os.getenv("AQUALINK_SPACONTROL_API_KEY")
LODGIFY_API_KEY = os.getenv("LODGIFY_API_KEY")
last_run = 0

# Dependency function to validate API key for protected endpoints
async def get_api_key(api_key: str = Security(api_key_header)):
    if api_key and AQUALINK_SPACONTROL_API_KEY and secrets.compare_digest(api_key.encode(), AQUALINK_SPACONTROL_API_KEY.encode()):
        return api_key
    raise HTTPException(status_code=HTTP_401_UNAUTHORIZED, detail="Invalid API Key")


# Helper function to validate required environment variables at startup
def _validate_env() -> None:
    missing = [k for k, v in {
        "AQUALINK_EMAIL": EMAIL,
        "AQUALINK_PASSWORD": PASSWORD,
        "AQUALINK_SPACONTROL_API_KEY": AQUALINK_SPACONTROL_API_KEY,
    }.items() if not v]
    if missing:
        raise RuntimeError(f"Missing required environment variables: {', '.join(missing)}")
    if not LODGIFY_API_KEY:
        print("LODGIFY_API_KEY not set — /lodgify endpoints disabled.")

# Helper function to retrieve the Aqualink system, either by specified serial or defaulting to the first available
async def get_system(client):
    systems = await client.get_systems()
    if not systems:
        raise RuntimeError("No iAquaLink systems found for this account.")
    print(f"Found systems: {list(systems.keys())}")
    return systems.get(SERIAL) or list(systems.values())[0]

# Helper function to check if we're in a cooldown period after a recent spa on/off command
def is_locked():
    """Check if the system is in a mechanical cooldown period."""
    global last_run
    print(f"Checking cooldown: last run at {last_run}, current time {time.time()}")
    return (time.time() - last_run) < VALVE_COOLDOWN

# Function to send Pushover alerts for overheat/underheat conditions
async def send_pushover_alert(message: str) -> None:
    if not PUSHOVER_USER or not PUSHOVER_TOKEN:
        return
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            r = await client.post(
                "https://api.pushover.net/1/messages.json",
                data={"token": PUSHOVER_TOKEN, "user": PUSHOVER_USER, "message": message},
            )
            r.raise_for_status()
    except httpx.HTTPError as e:
        print(f"Pushover alert failed: {type(e).__name__}")

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
                    await send_pushover_alert(f"ALARM: Pool/spa temp exceeded {TEMP_OVERHEAT_THRESHOLD}\u00b0F \u2014 emergency all-off triggered.")
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
                    await send_pushover_alert(f"ALERT: Pool/spa temp dropped below {TEMP_UNDERHEAT_THRESHOLD}\u00b0F \u2014 Check heating system.")
                    print(f"ALERT: Pool/spa temp dropped below {TEMP_UNDERHEAT_THRESHOLD}\u00b0F \u2014 Check heating system.")
                    r = await system._send_session_request("set_onetouch_4") #Turn on pool heater only
                    if r.status_code == 200:
                        print({"status": "success", "action": "pool_heater_on", "response": r.json()})
                        print(f"ALERT: Pool heater turned on (temp underheat threshold).")
                    else:
                        print(f"ALERT: Failed to turn on pool heater (temp underheat threshold). Response: {r.text}")

        except Exception as e:
            print(f"Watchdog Error: {e}")
        await asyncio.sleep(TEMP_MONITOR_INTERVAL)

# Background task to enable aux_1 after a delay when spa is turned on
async def _enable_aux1_after_delay():
    """Turns on aux_1 after a 30-second delay — runs as a background task after spa-on."""
    await asyncio.sleep(30)
    try:
        async with AqualinkClient(EMAIL, PASSWORD) as client:
            system = await get_system(client)
            await system.update()
            devices = await system.get_devices()
            aux1 = devices.get("aux_1")
            if aux1 and not aux1.is_on:
                await aux1.turn_on()
                print("aux_1 enabled (30s post spa-on delay).")
    except Exception as e:
        print(f"aux_1 enable error: {e}")

# FastAPI lifespan context to manage startup/shutdown tasks, including environment validation and starting the temperature monitor
@asynccontextmanager
async def lifespan(app: FastAPI):
    _validate_env()
    task = asyncio.create_task(temp_monitor())
    yield
    task.cancel()

# Initialize FastAPI app with lifespan context for startup/shutdown management
app = FastAPI(lifespan=lifespan)

# Define API endpoints with authentication and device control logic
@app.api_route("/spa-on", methods=["GET", "POST"], dependencies=[Depends(get_api_key)])
async def spa_on(background_tasks: BackgroundTasks):
    global last_run
    if is_locked():
        return {"status": "ignored", "reason": "Mechanical cooldown in progress"}

    async with AqualinkClient(EMAIL, PASSWORD) as client:
        system = await get_system(client)
        await system.update()
        devices = await system.get_devices()

        spa = devices.get("spa_pump")
        if spa and not spa.is_on:
            await spa.turn_on()

        heater = devices.get("spa_heater")
        if heater and not heater.is_on:
            await heater.turn_on()

        pool_heater = devices.get("pool_heater")
        if pool_heater and pool_heater.is_on:
            await pool_heater.turn_off()

    last_run = time.time()
    background_tasks.add_task(_enable_aux1_after_delay)
    return {"status": "success", "action": "spa_on", "note": "aux_1 will enable in 30s"}

# Endpoint to turn off spa and related devices, with cooldown protection and pool heater fallback
@app.api_route("/spa-off", methods=["GET", "POST"], dependencies=[Depends(get_api_key)])
async def spa_off():
    global last_run
    if is_locked():
        return {"status": "ignored", "reason": "Mechanical cooldown in progress"}

    async with AqualinkClient(EMAIL, PASSWORD) as client:
        system = await get_system(client)
        await system.update()
        devices = await system.get_devices()

        spa = devices.get("spa_pump")
        if spa and spa.is_on:
            await spa.turn_off()

        spa_heater = devices.get("spa_heater")
        if spa_heater and spa_heater.is_on:
            await spa_heater.turn_off()

        aux1 = devices.get("aux_1")
        if aux1 and aux1.is_on:
            await aux1.turn_off()

        pool_heater = devices.get("pool_heater")
        if pool_heater and not pool_heater.is_on:
            await pool_heater.turn_on()

    last_run = time.time()
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
async def all_off():
    async with AqualinkClient(EMAIL, PASSWORD) as client:
        system = await get_system(client)
        r = await system._send_session_request("set_onetouch_1")
        return {"status": "success", "action": "all_off", "response": r.json()}

# Endpoint to trigger the "spa mode" one-touch command, which typically turns on the spa pump and heater and turns off the pool heater
@app.api_route("/spa-mode", methods=["GET", "POST"], dependencies=[Depends(get_api_key)])
async def spa_mode():
    async with AqualinkClient(EMAIL, PASSWORD) as client:
        system = await get_system(client)
        r = await system._send_session_request("set_onetouch_2")
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
