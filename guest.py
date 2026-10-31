from pathlib import Path
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
import auth
import config
import events
import spa
from lodgify import find_current_booking

router = APIRouter(prefix="/guest")
PAGE = Path(__file__).parent / "static" / "guest.html"


class LoginBody(BaseModel):
    pin: str = Field(min_length=1, max_length=10)


class BlowerBody(BaseModel):
    on: bool


class HeaterBody(BaseModel):
    kind: str
    on: bool


class TempBody(BaseModel):
    kind: str
    value: int


def _actor(booking: dict) -> str:
    return f"{auth.guest_first_name(booking)} (#{booking['id']})"


# Runs an equipment command, logs it, and maps failures to friendly errors
async def _run(request: Request, booking: dict, action: str, coro, detail: dict | None = None):
    try:
        await coro
    except spa.CooldownError:
        raise HTTPException(status_code=409, detail=f"The valves are still moving. Try again in {spa.cooldown_remaining()} seconds.")
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        events.log_event(f"guest_{action}_error", _actor(booking), auth.client_ip(request), {"error": type(e).__name__})
        raise HTTPException(status_code=502, detail="The spa controller didn't respond. Please try again in a minute.")
    events.log_event(f"guest_{action}", _actor(booking), auth.client_ip(request), detail)
    return {"status": "success", "action": action}


@router.get("", include_in_schema=False)
async def guest_page():
    return FileResponse(PAGE)


@router.post("/api/login")
async def login(body: LoginBody, request: Request, response: Response):
    if not config.guest_portal_enabled():
        raise HTTPException(status_code=503, detail="Guest portal not configured")
    ip = auth.client_ip(request)
    wait = auth.guest_limiter.retry_after(ip)
    if wait:
        raise HTTPException(status_code=429, detail=f"Too many attempts. Please try again in {wait // 60 + 1} minutes.")

    pin = body.pin.strip()
    booking = None
    if len(pin) == 4 and pin.isdigit():
        try:
            booking = await find_current_booking(config.LODGIFY_API_KEY, pin)
            if booking is None:  # code may have been assigned in the last minute — bypass the cache once
                booking = await find_current_booking(config.LODGIFY_API_KEY, pin, force=True)
        except Exception as e:
            print(f"Lodgify lookup failed: {type(e).__name__}")
            raise HTTPException(status_code=503, detail="Unable to verify your reservation right now. Please try again shortly.")

    if booking is None or not auth.in_stay(booking):
        events.log_event("guest_login_fail", ip=ip, detail={"reason": "no_match" if booking is None else "outside_stay"})
        await auth.guest_limiter.record_failure(ip)
        raise HTTPException(status_code=401, detail="That code doesn't match a current reservation.")

    auth.guest_limiter.record_success(ip)
    auth.set_cookie(response, auth.GUEST_COOKIE, auth.guest_token(booking, pin), auth.GUEST_COOKIE_MAX_AGE, "/guest")
    events.log_event("guest_login", _actor(booking), ip)
    await config.send_pushover_alert(f"Spa portal: {auth.guest_first_name(booking)} logged in (booking #{booking['id']}, IP {ip}).")
    return {"status": "success"}


@router.post("/api/logout")
async def logout(response: Response):
    response.delete_cookie(auth.GUEST_COOKIE, path="/guest")
    return {"status": "success"}


@router.get("/api/me")
async def me(booking: dict = Depends(auth.require_guest)):
    rooms = booking.get("rooms") or []
    guests = sum(
        (r.get("guest_breakdown") or {}).get("adults", 0) + (r.get("guest_breakdown") or {}).get("children", 0)
        for r in rooms
    )
    return {
        "first_name": auth.guest_first_name(booking),
        "arrival": booking["arrival"],
        "departure": booking["departure"],
        "checkout": auth.checkout_at(booking).isoformat(),
        "timezone": config.PROPERTY_TIMEZONE.key,
        "guests": guests or None,
        "booking_site_url": config.BOOKING_SITE_URL,
    }


@router.get("/api/status")
async def status(booking: dict = Depends(auth.require_guest)):
    try:
        return await spa.get_status()
    except Exception as e:
        print(f"Status error: {type(e).__name__}: {e}")
        raise HTTPException(status_code=502, detail="The spa controller didn't respond. Please try again in a minute.")


@router.post("/api/spa-on")
async def guest_spa_on(request: Request, booking: dict = Depends(auth.require_guest)):
    return await _run(request, booking, "spa_on", spa.spa_on())


@router.post("/api/spa-off")
async def guest_spa_off(request: Request, booking: dict = Depends(auth.require_guest)):
    return await _run(request, booking, "spa_off", spa.spa_off())


@router.post("/api/blower")
async def guest_blower(body: BlowerBody, request: Request, booking: dict = Depends(auth.require_guest)):
    return await _run(request, booking, "blower_on" if body.on else "blower_off", spa.set_aux1(body.on))


@router.post("/api/heater")
async def guest_heater(body: HeaterBody, request: Request, booking: dict = Depends(auth.require_guest)):
    action = f"{body.kind}_heater_{'on' if body.on else 'off'}"
    return await _run(request, booking, action, spa.set_heater(body.kind, body.on))


@router.post("/api/temp")
async def guest_temp(body: TempBody, request: Request, booking: dict = Depends(auth.require_guest)):
    return await _run(request, booking, "set_temp", spa.set_temp(body.kind, body.value), {"kind": body.kind, "value": body.value})
