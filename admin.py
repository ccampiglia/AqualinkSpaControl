from pathlib import Path
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
import auth
import config
import events
import spa
from lodgify import get_current_bookings, booking_key_codes

router = APIRouter(prefix="/admin")
PAGE = Path(__file__).parent / "static" / "admin.html"


class LoginBody(BaseModel):
    password: str = Field(min_length=1, max_length=200)


class ClearBody(BaseModel):
    ip: str


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
    }


@router.post("/api/lockouts/clear", dependencies=[Depends(auth.require_admin)])
async def clear_lockout(body: ClearBody, request: Request):
    auth.guest_limiter.clear(body.ip)
    events.log_event("lockout_cleared", "admin", auth.client_ip(request), {"ip": body.ip})
    return {"status": "success"}
