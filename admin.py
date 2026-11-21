from pathlib import Path
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
import auth
import config
import events
import guard
import settings
import spa
from lodgify import get_current_bookings, booking_key_codes

router = APIRouter(prefix="/admin")
PAGE = Path(__file__).parent / "static" / "admin.html"


class LoginBody(BaseModel):
    password: str = Field(min_length=1, max_length=200)


class ClearBody(BaseModel):
    ip: str


class OnBody(BaseModel):
    on: bool


class HeaterBody(BaseModel):
    kind: str
    on: bool


class TempBody(BaseModel):
    kind: str
    value: int


class SettingsBody(BaseModel):
    changes: dict[str, int | None]


class GuardWindow(BaseModel):
    days: list[int]
    start: str
    end: str


class GuardBody(BaseModel):
    enabled: bool
    keep_heater_on: bool
    windows: list[GuardWindow]


class PauseBody(BaseModel):
    hours: float = Field(gt=0, le=72)


@router.get("", include_in_schema=False)
async def admin_page():
    return FileResponse(PAGE)


@router.post("/api/login")
async def login(body: LoginBody, request: Request, response: Response):
    if not config.admin_enabled():
        raise HTTPException(status_code=503, detail="Admin page not configured")
    ip = auth.client_ip(request)
    wait = auth.admin_limiter.retry_after(ip)
    if wait:
        raise HTTPException(status_code=429, detail=f"Too many attempts. Try again in {wait // 60 + 1} minutes.")
    if not auth.check_admin_password(body.password):
        events.log_event("admin_login_fail", ip=ip)
        await auth.admin_limiter.record_failure(ip)
        raise HTTPException(status_code=401, detail="Incorrect password.")
    auth.admin_limiter.record_success(ip)
    auth.set_cookie(response, auth.ADMIN_COOKIE, auth.admin_token(), auth.ADMIN_COOKIE_MAX_AGE, "/admin")
    events.log_event("admin_login", "admin", ip)
    await config.send_pushover_alert(f"Spa admin page: login from IP {ip}.")
    return {"status": "success"}


@router.post("/api/logout")
async def logout(response: Response):
    response.delete_cookie(auth.ADMIN_COOKIE, path="/admin")
    return {"status": "success"}


@router.get("/api/events", dependencies=[Depends(auth.require_admin)])
async def list_events(limit: int = 200, type: str | None = None):
    return {"events": events.recent(limit, type or None), "types": events.event_types()}


@router.get("/api/overview", dependencies=[Depends(auth.require_admin)])
async def overview():
    bookings, booking_error = [], None
    try:
        for b in await get_current_bookings(config.LODGIFY_API_KEY, force=True) if config.LODGIFY_API_KEY else []:
            codes = booking_key_codes(b)
            bookings.append({
                "id": b["id"],
                "name": (b.get("guest") or {}).get("name"),
                "arrival": b["arrival"],
                "departure": b["departure"],
                "checkout": auth.checkout_at(b).isoformat(),
                "in_stay": auth.in_stay(b),
                "door_code": ["••" + c[-2:] for c in codes],
                "source": b.get("source"),
            })
    except Exception as e:
        booking_error = f"Lodgify lookup failed ({type(e).__name__})"

    status, status_error = None, None
    try:
        status = await spa.get_status()
    except Exception as e:
        status_error = f"Aqualink status failed ({type(e).__name__})"

    return {
        "bookings": bookings,
        "booking_error": booking_error,
        "status": status,
        "status_error": status_error,
        "lockouts": {"guest": auth.guest_limiter.active_lockouts(), "admin": auth.admin_limiter.active_lockouts()},
        "guest_portal_enabled": config.guest_portal_enabled(),
        "admin_limits": {k: {"min": lo, "max": hi} for k, (lo, hi) in spa.admin_temp_limits().items()},
        "guard": guard.status(),
    }


@router.post("/api/lockouts/clear", dependencies=[Depends(auth.require_admin)])
async def clear_lockout(body: ClearBody, request: Request):
    auth.guest_limiter.clear(body.ip)
    events.log_event("lockout_cleared", "admin", auth.client_ip(request), {"ip": body.ip})
    return {"status": "success"}


# --- Equipment controls ---

# Runs an equipment command, logs it as an admin action, and maps failures to friendly errors
async def _run(request: Request, action: str, coro, detail: dict | None = None):
    ip = auth.client_ip(request)
    try:
        result = await coro
    except spa.CooldownError:
        raise HTTPException(status_code=409, detail=f"Valves are still moving. Try again in {spa.cooldown_remaining()} seconds.")
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        events.log_event(f"admin_{action}_error", "admin", ip, {"error": f"{type(e).__name__}: {e}"})
        raise HTTPException(status_code=502, detail=f"The controller didn't respond ({type(e).__name__}). Try again in a minute.")
    if isinstance(result, list):
        detail = {**(detail or {}), "turned_off": result}
    events.log_event(f"admin_{action}", "admin", ip, detail)
    return {"status": "success", "action": action}


@router.post("/api/spa", dependencies=[Depends(auth.require_admin)])
async def admin_spa(body: OnBody, request: Request):
    return await _run(request, "spa_on" if body.on else "spa_off", spa.spa_on() if body.on else spa.spa_off())


@router.post("/api/blower", dependencies=[Depends(auth.require_admin)])
async def admin_blower(body: OnBody, request: Request):
    return await _run(request, "blower_on" if body.on else "blower_off", spa.set_aux1(body.on))


@router.post("/api/heater", dependencies=[Depends(auth.require_admin)])
async def admin_heater(body: HeaterBody, request: Request):
    return await _run(request, f"{body.kind}_heater_{'on' if body.on else 'off'}", spa.set_heater(body.kind, body.on))


@router.post("/api/temp", dependencies=[Depends(auth.require_admin)])
async def admin_temp(body: TempBody, request: Request):
    return await _run(request, "set_temp", spa.set_temp(body.kind, body.value, admin=True), {"kind": body.kind, "value": body.value})


@router.post("/api/pool-pump", dependencies=[Depends(auth.require_admin)])
async def admin_pool_pump(body: OnBody, request: Request):
    result = await _run(request, "pool_pump_on" if body.on else "pool_pump_off", spa.set_pool_pump(body.on))
    # A deliberate pump-off shouldn't be undone by the pump guard a few minutes later
    if not body.on and guard.get_config()["enabled"] and guard.pause_until_window_end():
        events.log_event("guard_paused", "admin", auth.client_ip(request), {"reason": "pool pump turned off", "until": "end of window"})
    return result


@router.post("/api/all-off", dependencies=[Depends(auth.require_admin)])
async def admin_all_off(request: Request):
    result = await _run(request, "all_off", spa.all_off())
    if guard.get_config()["enabled"] and guard.pause_until_window_end():
        events.log_event("guard_paused", "admin", auth.client_ip(request), {"reason": "all off", "until": "end of window"})
    return result


# --- Settings ---

@router.get("/api/settings", dependencies=[Depends(auth.require_admin)])
async def get_settings():
    return {"settings": settings.describe(), "guard": guard.status(), "day_names": guard.DAY_NAMES}


@router.post("/api/settings", dependencies=[Depends(auth.require_admin)])
async def save_settings(body: SettingsBody, request: Request):
    before = settings.all_numeric()
    try:
        after = settings.update_numeric(body.changes)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    changed = {k: {"from": before[k], "to": after[k]} for k in after if before[k] != after[k]}
    if changed:
        events.log_event("settings_changed", "admin", auth.client_ip(request), changed)
    return {"status": "success", "settings": settings.describe()}


@router.post("/api/guard", dependencies=[Depends(auth.require_admin)])
async def save_guard(body: GuardBody, request: Request):
    try:
        cfg = guard.save_config(body.model_dump())
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    events.log_event("guard_config_changed", "admin", auth.client_ip(request), cfg)
    return {"status": "success", "guard": guard.status()}


@router.post("/api/guard/pause", dependencies=[Depends(auth.require_admin)])
async def pause_guard(body: PauseBody, request: Request):
    guard.pause(int(body.hours * 3600))
    events.log_event("guard_paused", "admin", auth.client_ip(request), {"hours": body.hours})
    return {"status": "success", "guard": guard.status()}


@router.post("/api/guard/resume", dependencies=[Depends(auth.require_admin)])
async def resume_guard(request: Request):
    guard.resume()
    events.log_event("guard_resumed", "admin", auth.client_ip(request))
    return {"status": "success", "guard": guard.status()}


@router.post("/api/guard/clear-lockout", dependencies=[Depends(auth.require_admin)])
async def clear_guard_lockout(request: Request):
    guard.clear_overheat_lockout()
    events.log_event("guard_lockout_cleared", "admin", auth.client_ip(request))
    return {"status": "success", "guard": guard.status()}
