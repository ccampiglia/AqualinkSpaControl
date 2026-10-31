import os
import httpx
from zoneinfo import ZoneInfo
from dotenv import load_dotenv

load_dotenv()

# iaqualink library hard limit for set points (°F)
IAQUALINK_MAX_SET_POINT = 104

# Aqualink / watchdog
EMAIL = os.getenv("AQUALINK_EMAIL")
PASSWORD = os.getenv("AQUALINK_PASSWORD")
SERIAL = os.getenv("AQUALINK_SERIAL")
TEMP_OVERHEAT_THRESHOLD = int(os.getenv("TEMP_OVERHEAT_THRESHOLD", "105"))
TEMP_UNDERHEAT_THRESHOLD = int(os.getenv("TEMP_UNDERHEAT_THRESHOLD", "80"))
TEMP_MONITOR_INTERVAL = int(os.getenv("TEMP_MONITOR_INTERVAL", "600"))
VALVE_COOLDOWN = int(os.getenv("VALVE_COOLDOWN", "60"))  # Protects mechanical rotation

# Alerts / owner API
PUSHOVER_USER = os.getenv("PUSHOVER_USER")
PUSHOVER_TOKEN = os.getenv("PUSHOVER_TOKEN")
AQUALINK_SPACONTROL_API_KEY = os.getenv("AQUALINK_SPACONTROL_API_KEY")
LODGIFY_API_KEY = os.getenv("LODGIFY_API_KEY")

# Guest portal / admin
SESSION_SECRET = os.getenv("SESSION_SECRET")
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD")
SPA_TEMP_MIN = int(os.getenv("SPA_TEMP_MIN", "90"))
SPA_TEMP_MAX = int(os.getenv("SPA_TEMP_MAX", "102"))
POOL_TEMP_MIN = int(os.getenv("POOL_TEMP_MIN", "82"))
POOL_TEMP_MAX = int(os.getenv("POOL_TEMP_MAX", "90"))
PROPERTY_TIMEZONE = ZoneInfo(os.getenv("PROPERTY_TIMEZONE", "America/New_York"))
EVENT_DB_PATH = os.getenv("EVENT_DB_PATH", "./data/events.db")
BOOKING_SITE_URL = os.getenv("BOOKING_SITE_URL", "https://sunshinevilladeerfield.com/")
# Set COOKIE_SECURE=false only for local http testing
COOKIE_SECURE = os.getenv("COOKIE_SECURE", "true").lower() != "false"

TEMP_LIMITS = {
    "spa": (SPA_TEMP_MIN, SPA_TEMP_MAX),
    "pool": (POOL_TEMP_MIN, POOL_TEMP_MAX),
}


def guest_portal_enabled() -> bool:
    return bool(SESSION_SECRET and LODGIFY_API_KEY)


def admin_enabled() -> bool:
    return bool(SESSION_SECRET and ADMIN_PASSWORD)


# Validates required environment variables and temperature limits at startup
def validate_env() -> None:
    missing = [k for k, v in {
        "AQUALINK_EMAIL": EMAIL,
        "AQUALINK_PASSWORD": PASSWORD,
        "AQUALINK_SPACONTROL_API_KEY": AQUALINK_SPACONTROL_API_KEY,
    }.items() if not v]
    if missing:
        raise RuntimeError(f"Missing required environment variables: {', '.join(missing)}")

    for kind, (low, high) in TEMP_LIMITS.items():
        name = kind.upper()
        if low >= high:
            raise RuntimeError(f"{name}_TEMP_MIN ({low}) must be below {name}_TEMP_MAX ({high})")
        if high > IAQUALINK_MAX_SET_POINT:
            raise RuntimeError(f"{name}_TEMP_MAX ({high}) exceeds the Aqualink limit of {IAQUALINK_MAX_SET_POINT}")
        if high >= TEMP_OVERHEAT_THRESHOLD:
            raise RuntimeError(
                f"{name}_TEMP_MAX ({high}) must be below TEMP_OVERHEAT_THRESHOLD ({TEMP_OVERHEAT_THRESHOLD}) "
                "or guests could trigger the emergency all-off"
            )

    if not LODGIFY_API_KEY:
        print("LODGIFY_API_KEY not set — /lodgify endpoints and guest portal disabled.")
    if not SESSION_SECRET:
        print("SESSION_SECRET not set — guest portal and admin page disabled.")
    elif len(SESSION_SECRET) < 32:
        print("WARNING: SESSION_SECRET is short; use at least 32 random characters.")
    if not ADMIN_PASSWORD:
        print("ADMIN_PASSWORD not set — admin page disabled.")


# Sends a Pushover notification to the owner (no-op if Pushover isn't configured)
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
