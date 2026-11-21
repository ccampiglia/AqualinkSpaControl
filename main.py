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
from iaqualink.exception import AqualinkSystemOfflineException
from lodgify import LodgifyClient, LodgifyError
from config import (
    EMAIL, PASSWORD, AQUALINK_SPACONTROL_API_KEY, LODGIFY_API_KEY, validate_env, send_pushover_alert,
)
import admin
import events
import guard
import settings
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

# Equipment tracked by the watchdog: status-line label -> Aqualink device key
WATCHED_EQUIPMENT = {
    "pool pump": "pool_pump",
    "pool heater": "pool_heater",
    "spa pump": "spa_pump",
    "spa heater": "spa_heater",
    "blower": "aux_1",
    "freeze protection": "freeze_protection",
}

_last_snapshot: dict | None = None   # equipment state from the previous watchdog cycle
_underheat_alerted = False            # True while the pool is in a cold spell we've already alerted on
_was_offline = False                  # True while the controller is reporting Offline


def _fmt_temp(value: int | None) -> str:
    return f"{value}F" if value is not None else "n/a"


# Reads temps, set points and on/off state for the status log line and change detection
def _snapshot(devices) -> dict:
    snap = {
        "temps": {k: spa._temp(devices.get(f"{k}_temp")) for k in ("air", "pool", "spa")},
        "set_points": {k: spa._temp(devices.get(f"{k}_set_point")) for k in ("pool", "spa")},
        "equipment": {label: spa._on(devices.get(key)) for label, key in WATCHED_EQUIPMENT.items()},
    }
    snap["heater_state"] = {k: spa._heater_state(devices.get(f"{k}_heater")) for k in ("pool", "spa")}
    return snap


def _status_line(snap: dict) -> str:
    t, sp, eq, hs = snap["temps"], snap["set_points"], snap["equipment"], snap["heater_state"]
    onoff = lambda v: "n/a" if v is None else ("ON" if v else "off")
    return (
        f"Status: air {_fmt_temp(t['air'])} | pool {_fmt_temp(t['pool'])} (set {_fmt_temp(sp['pool'])}) "
        f"| spa {_fmt_temp(t['spa'])} (set {_fmt_temp(sp['spa'])}) "
        f"| pool pump {onoff(eq['pool pump'])} | pool heater {hs['pool'] or 'n/a'} "
        f"| spa pump {onoff(eq['spa pump'])} | spa heater {hs['spa'] or 'n/a'} "
        f"| blower {onoff(eq['blower'])} | freeze protection {onoff(eq['freeze protection'])}"
    )


# Logs any equipment that changed state since the last cycle (e.g. the pool pump turning off on its own)
def _log_changes(snap: dict) -> None:
    global _last_snapshot
    if _last_snapshot is not None:
        changes = {
            label: {"from": _last_snapshot["equipment"].get(label), "to": state}
            for label, state in snap["equipment"].items()
            if _last_snapshot["equipment"].get(label) != state
        }
        if changes:
            events.log_event("equipment_change", "watchdog", detail=changes)
    _last_snapshot = snap


# Pool below the low threshold: alert once per cold spell and make sure the pool heater is enabled.
# Uses the pool_heater switch with a state check — never the "Pool Heat" OneTouch, which toggles on each press.
async def _handle_underheat(system, snap: dict) -> None:
    global _underheat_alerted
    threshold = settings.get("TEMP_UNDERHEAT_THRESHOLD")
    pool_temp = snap["temps"]["pool"]
    # Pool temp is only meaningful while the pool pump circulates water in pool mode
    if not snap["equipment"]["pool pump"] or snap["equipment"]["spa pump"] or pool_temp is None:
        return
    if pool_temp > threshold:
        if _underheat_alerted:
            print(f"Pool back above {threshold}F ({pool_temp}F) — low-temp alert cleared.")
        _underheat_alerted = False
        return

    heater = system.devices.get("pool_heater")
    action = "none"
    if heater is not None and not heater.is_on:
        async with spa._command_lock:
            await heater.turn_on()
        spa._invalidate_status()
        action = "pool_heater_on"
        print(f"Watchdog: pool {pool_temp}F <= {threshold}F and pool heater was off — turned pool heater on.")
        events.log_event("watchdog_pool_heater_on", "watchdog", detail={"pool_temp": pool_temp})

    if not _underheat_alerted:
        _underheat_alerted = True
        events.log_event("watchdog_underheat", "watchdog",
                         detail={"threshold": threshold, "pool_temp": pool_temp, "action": action})
        heater_note = "Pool heater was off — turned it on." if action == "pool_heater_on" else \
            f"Pool heater is {snap['heater_state']['pool'] or 'unknown'}."
        await send_pushover_alert(
            f"ALERT: Pool is {pool_temp}°F (at or below {threshold}°F). {heater_note} Check heating system."
        )


# Spa or pool at/above the overheat threshold: emergency all-off.
# Turns each device off directly — never the "All OFF" OneTouch, which toggles and could turn things back on.
async def _handle_overheat(system, snap: dict) -> None:
    threshold = settings.get("TEMP_OVERHEAT_THRESHOLD")
    temps = {k: v for k, v in snap["temps"].items() if k in ("pool", "spa") and v is not None}
    guard.set_overheat_lockout({"threshold": threshold, **temps})
    try:
        turned_off = await spa.all_off(system)
        result = f"turned off: {', '.join(turned_off) or 'nothing was on'}"
        events.log_event("watchdog_overheat", "watchdog", detail={"threshold": threshold, **temps, "turned_off": turned_off})
    except Exception as e:
        result = f"FAILED — {e}"
        events.log_event("watchdog_overheat", "watchdog", detail={"threshold": threshold, **temps, "error": str(e)})
    print(f"ALARM: temp {temps} at/above {threshold}F — emergency all-off, {result}")
    await send_pushover_alert(
        f"ALARM: Pool/spa temp {temps} reached {threshold}°F — emergency all-off, {result}. "
        "Pump guard is locked out until cleared on the admin page."
    )


# Background task: logs status every cycle, records equipment changes, and handles temperature alarms
async def temp_monitor():
    global _was_offline
    while True:
        try:
            async with AqualinkClient(EMAIL, PASSWORD) as client:
                system = await get_system(client)
                try:
                    await system.update()
                except AqualinkSystemOfflineException:
                    if not _was_offline:
                        _was_offline = True
                        events.log_event("controller_offline", "watchdog")
                    print("Watchdog: controller is reporting Offline to iAqualink — skipping this cycle.")
                    continue
                if _was_offline:
                    _was_offline = False
                    events.log_event("controller_online", "watchdog")

                snap = _snapshot(system.devices)
                print(_status_line(snap))
                _log_changes(snap)

                over = settings.get("TEMP_OVERHEAT_THRESHOLD")
                if any(v is not None and v >= over for k, v in snap["temps"].items() if k in ("pool", "spa")):
                    await _handle_overheat(system, snap)
                else:
                    await _handle_underheat(system, snap)
                    result = await guard.run(system, snap)
                    if result not in ("disabled", "outside window", "ok"):
                        print(f"Pump guard: {result}")
        except Exception as e:
            print(f"Watchdog Error: {type(e).__name__}: {e}")
        finally:
            await asyncio.sleep(settings.get("TEMP_MONITOR_INTERVAL"))

# FastAPI lifespan context to manage startup/shutdown tasks, including environment validation and starting the temperature monitor
@asynccontextmanager
async def lifespan(app: FastAPI):
    validate_env()
    events.init_db()
    settings.init()
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
    return {"status": "success", "action": "spa_on", "note": "aux_1 (blower) starts about 30s after the spa pump is running"}

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

# Endpoint to turn off all heaters, the blower and both pumps (each device directly, not the toggling OneTouch)
@app.api_route("/all-off", methods=["GET", "POST"], dependencies=[Depends(get_api_key)])
async def all_off(request: Request):
    turned_off = await spa.all_off()
    guard.pause_until_window_end()  # don't let the pump guard undo a deliberate all-off
    events.log_event("api_all_off", "api", _ip(request), {"turned_off": turned_off})
    return {"status": "success", "action": "all_off", "turned_off": turned_off}

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
