import os
import time
import asyncio
from contextlib import asynccontextmanager
from fastapi import FastAPI, BackgroundTasks
from iaqualink.client import AqualinkClient
from dotenv import load_dotenv

load_dotenv()

EMAIL = os.getenv("AQUALINK_EMAIL")
PASSWORD = os.getenv("AQUALINK_PASSWORD")
SERIAL = os.getenv("AQUALINK_SERIAL")
TEMP_MONITOR_INTERVAL = int(os.getenv("TEMP_MONITOR_INTERVAL", "600"))
VALVE_COOLDOWN = int(os.getenv("VALVE_COOLDOWN", "60"))  # Protects mechanical rotation
last_run = 0


def _validate_env() -> None:
    missing = [k for k, v in {"AQUALINK_EMAIL": EMAIL, "AQUALINK_PASSWORD": PASSWORD}.items() if not v]
    if missing:
        raise RuntimeError(f"Missing required environment variables: {', '.join(missing)}")


async def get_system(client):
    systems = await client.get_systems()
    if not systems:
        raise RuntimeError("No iAquaLink systems found for this account.")
    print(f"Found systems: {list(systems.keys())}")
    return systems.get(SERIAL) or list(systems.values())[0]


def is_locked():
    """Check if the system is in a mechanical cooldown period."""
    global last_run
    print(f"Checking cooldown: last run at {last_run}, current time {time.time()}")
    return (time.time() - last_run) < VALVE_COOLDOWN


async def temp_monitor():
    while True:
        try:
            async with AqualinkClient(EMAIL, PASSWORD) as client:
                system = await get_system(client)
                await system.update()
                temp_sensor_spa = system.devices.get("spa_temp")
                temp_sensor_pool = system.devices.get("pool_temp")

                for sensor in [temp_sensor_spa, temp_sensor_pool]:
                    if sensor and sensor.state:
                        print(f"Current {sensor.name}: {sensor.state}F")

                alarm = any(
                    s and s.state and float(s.state) >= 110
                    for s in [temp_sensor_spa, temp_sensor_pool]
                )
                if alarm:
                    for pump_key in ("spa_pump", "pool_pump"):
                        pump = system.devices.get(pump_key)
                        if pump and pump.is_on:
                            await pump.turn_off()
                            print(f"ALARM: Emergency shutdown — {pump_key} off (temp threshold exceeded).")
        except Exception as e:
            print(f"Watchdog Error: {e}")
        await asyncio.sleep(TEMP_MONITOR_INTERVAL)


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


@asynccontextmanager
async def lifespan(app: FastAPI):
    _validate_env()
    task = asyncio.create_task(temp_monitor())
    yield
    task.cancel()


app = FastAPI(lifespan=lifespan)


@app.api_route("/spa-on", methods=["GET", "POST"])
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


@app.api_route("/spa-off", methods=["GET", "POST"])
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


@app.get("/devices")
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


@app.get("/onetouch")
async def get_onetouch():
    async with AqualinkClient(EMAIL, PASSWORD) as client:
        system = await get_system(client)
        r = await system._send_session_request("get_onetouch")
        return r.json()


@app.api_route("/all-off", methods=["GET", "POST"])
async def all_off():
    async with AqualinkClient(EMAIL, PASSWORD) as client:
        system = await get_system(client)
        r = await system._send_session_request("set_onetouch_1")
        return {"status": "success", "action": "all_off", "response": r.json()}


@app.api_route("/spa-mode", methods=["GET", "POST"])
async def spa_mode():
    async with AqualinkClient(EMAIL, PASSWORD) as client:
        system = await get_system(client)
        r = await system._send_session_request("set_onetouch_2")
        return {"status": "success", "action": "spa_mode", "response": r.json()}
