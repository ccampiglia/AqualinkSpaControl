import base64
import hashlib
import hmac
import json
import time
from collections import deque
from datetime import date, datetime, time as dtime
from fastapi import HTTPException, Request
import config
import events
from lodgify import get_current_bookings, booking_key_codes

GUEST_COOKIE = "spa_guest"
ADMIN_COOKIE = "spa_admin"
GUEST_COOKIE_MAX_AGE = 24 * 3600
ADMIN_COOKIE_MAX_AGE = 12 * 3600
DEFAULT_CHECKOUT = dtime(11, 0)


# --- Signed cookies (HMAC-SHA256 over a small JSON payload) ---

def _sign(data: bytes) -> str:
    return hmac.new(config.SESSION_SECRET.encode(), data, hashlib.sha256).hexdigest()


def make_token(payload: dict) -> str:
    data = base64.urlsafe_b64encode(json.dumps(payload, separators=(",", ":")).encode())
    return f"{data.decode()}.{_sign(data)}"


def read_token(token: str | None) -> dict | None:
    if not token or not config.SESSION_SECRET or "." not in token:
        return None
    data, sig = token.rsplit(".", 1)
    if not hmac.compare_digest(sig, _sign(data.encode())):
        return None
    try:
        payload = json.loads(base64.urlsafe_b64decode(data))
    except ValueError:
        return None
    if payload.get("exp", 0) < time.time():
        return None
    return payload


def set_cookie(response, name: str, token: str, max_age: int, path: str) -> None:
    response.set_cookie(
        name, token, max_age=max_age, path=path,
        httponly=True, secure=config.COOKIE_SECURE, samesite="strict",
    )


def client_ip(request: Request) -> str:
    return request.client.host if request.client else "unknown"


def _code_hash(code: str) -> str:
    return hashlib.sha256(f"{config.SESSION_SECRET}:{code}".encode()).hexdigest()[:32]


# --- Brute-force protection ---

class LoginLimiter:
    """Per-IP lockout after repeated failures, plus a global cap across all IPs."""

    def __init__(self, scope: str, max_failures=5, window=900, lockout=900, global_max=30, global_window=3600):
        self.scope = scope
        self.max_failures, self.window, self.lockout = max_failures, window, lockout
        self.global_max, self.global_window = global_max, global_window
        self.failures: dict[str, deque] = {}
        self.locked_until: dict[str, float] = {}
        self.global_failures: deque = deque()
        self.global_locked_until = 0.0

    def _prune(self, q: deque, window: int, now: float) -> None:
        while q and q[0] < now - window:
            q.popleft()

    # Returns seconds until this IP may try again (0 = allowed)
    def retry_after(self, ip: str) -> int:
        now = time.time()
        until = max(self.locked_until.get(ip, 0), self.global_locked_until)
        return max(0, int(until - now))

    async def record_failure(self, ip: str) -> None:
        now = time.time()
        q = self.failures.setdefault(ip, deque())
        q.append(now)
        self._prune(q, self.window, now)
        self.global_failures.append(now)
        self._prune(self.global_failures, self.global_window, now)

        if len(q) >= self.max_failures:
            self.locked_until[ip] = now + self.lockout
            q.clear()
            events.log_event(f"{self.scope}_lockout", ip=ip, detail={"minutes": self.lockout // 60})
            await config.send_pushover_alert(
                f"Spa {self.scope} login: IP {ip} locked out for {self.lockout // 60} min after {self.max_failures} wrong codes."
            )
        if len(self.global_failures) >= self.global_max and self.global_locked_until < now:
            self.global_locked_until = now + self.global_window
            self.global_failures.clear()
            events.log_event(f"{self.scope}_global_lockout", ip=ip, detail={"minutes": self.global_window // 60})
            await config.send_pushover_alert(
                f"Spa {self.scope} login DISABLED for {self.global_window // 60} min: {self.global_max}+ failed attempts in the last hour. "
                "Possible brute-force attempt — clear it from the admin page if this is a guest."
            )

    def record_success(self, ip: str) -> None:
        self.failures.pop(ip, None)

    def active_lockouts(self) -> list[dict]:
        now = time.time()
        out = [{"ip": ip, "seconds": int(t - now)} for ip, t in self.locked_until.items() if t > now]
        if self.global_locked_until > now:
            out.append({"ip": "ALL (global)", "seconds": int(self.global_locked_until - now)})
        return out

    def clear(self, ip: str) -> None:
        if ip == "ALL (global)":
            self.global_locked_until = 0.0
            self.global_failures.clear()
        else:
            self.locked_until.pop(ip, None)
            self.failures.pop(ip, None)


guest_limiter = LoginLimiter("guest")
admin_limiter = LoginLimiter("admin")


# --- Guest access window ---

def checkout_at(booking: dict) -> datetime:
    """Departure date + check_out.time (updated in Lodgify for late checkouts), in the property's timezone."""
    t = DEFAULT_CHECKOUT
    raw = (booking.get("check_out") or {}).get("time")
    if raw:
        try:
            t = dtime.fromisoformat(raw)
        except ValueError:
            pass
    return datetime.combine(date.fromisoformat(booking["departure"]), t, tzinfo=config.PROPERTY_TIMEZONE)


def in_stay(booking: dict, now: datetime | None = None) -> bool:
    now = now or datetime.now(config.PROPERTY_TIMEZONE)
    start = datetime.combine(date.fromisoformat(booking["arrival"]), dtime(0, 0), tzinfo=config.PROPERTY_TIMEZONE)
    return start <= now < checkout_at(booking)


def guest_first_name(booking: dict) -> str:
    name = ((booking.get("guest") or {}).get("name") or "").strip()
    return name.split()[0] if name else "Guest"


def guest_token(booking: dict, code: str) -> str:
    return make_token({
        "r": "guest",
        "b": booking["id"],
        "k": _code_hash(code),
        "exp": int(time.time()) + GUEST_COOKIE_MAX_AGE,
    })


# FastAPI dependency: returns the guest's current booking, or 401 if the session is invalid or the stay has ended
async def require_guest(request: Request) -> dict:
    if not config.guest_portal_enabled():
        raise HTTPException(status_code=503, detail="Guest portal not configured")
    payload = read_token(request.cookies.get(GUEST_COOKIE))
    if not payload or payload.get("r") != "guest":
        raise HTTPException(status_code=401, detail="not_logged_in")
    try:
        bookings = await get_current_bookings(config.LODGIFY_API_KEY)
    except Exception as e:
        print(f"Lodgify check failed: {type(e).__name__}")
        raise HTTPException(status_code=503, detail="Unable to verify your reservation right now. Please try again shortly.")
    booking = next((b for b in bookings if b.get("id") == payload.get("b")), None)
    if (
        booking is None
        or not any(hmac.compare_digest(_code_hash(c), payload.get("k", "")) for c in booking_key_codes(booking))
        or not in_stay(booking)
    ):
        raise HTTPException(status_code=401, detail="stay_ended")
    return booking


# --- Admin ---

def check_admin_password(password: str) -> bool:
    return bool(config.ADMIN_PASSWORD) and hmac.compare_digest(password.encode(), config.ADMIN_PASSWORD.encode())


def admin_token() -> str:
    return make_token({"r": "admin", "exp": int(time.time()) + ADMIN_COOKIE_MAX_AGE})


# FastAPI dependency: requires a valid admin session cookie
async def require_admin(request: Request) -> None:
    if not config.admin_enabled():
        raise HTTPException(status_code=503, detail="Admin page not configured")
    payload = read_token(request.cookies.get(ADMIN_COOKIE))
    if not payload or payload.get("r") != "admin":
        raise HTTPException(status_code=401, detail="not_logged_in")
