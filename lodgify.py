import hmac
import time
from typing import Any
import httpx

BASE_URL = "https://api.lodgify.com"
CURRENT_CACHE_SECONDS = 60

_current_cache: tuple[float, list[dict]] | None = None


# Raised when Lodgify returns a non-2xx response. Never includes the API key.
class LodgifyError(Exception):
    def __init__(self, status_code: int, method: str, path: str, body: str = ""):
        self.status_code = status_code
        self.body = body[:500]
        super().__init__(f"Lodgify {method} {path} failed with HTTP {status_code}")


# Async client for the Lodgify public API. Usage:
#   async with LodgifyClient(api_key) as lg:
#       bookings = await lg.get_bookings()
class LodgifyClient:
    def __init__(self, api_key: str, timeout: float = 15.0):
        if not api_key:
            raise ValueError("Lodgify API key is required")
        self._client = httpx.AsyncClient(
            base_url=BASE_URL,
            headers={"X-ApiKey": api_key, "Accept": "application/json"},
            timeout=timeout,
        )

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        await self._client.aclose()

    # Shared request helper: drops None params, lowercases booleans, raises LodgifyError on failure
    async def _request(self, method: str, path: str, *, params: dict | None = None, json: Any = None) -> Any:
        if params:
            params = {
                k: (str(v).lower() if isinstance(v, bool) else v)
                for k, v in params.items() if v is not None
            }
        r = await self._client.request(method, path, params=params, json=json)
        if r.is_error:
            raise LodgifyError(r.status_code, method, path, r.text)
        return r.json() if r.content else None

    # --- Endpoints (add new ones below) ---

    # GET /v2/reservations/bookings (GetAllAsync) — list bookings
    async def get_bookings(
        self,
        page: int = 1,
        size: int = 50,
        include_count: bool = False,
        stay_filter: str = "Current",
        include_transactions: bool = False,
        include_external: bool = False,
        include_quote_details: bool = False,
        **extra: Any,
    ) -> dict:
        params = {
            "page": page,
            "size": size,
            "includeCount": include_count,
            "stayFilter": stay_filter,
            "includeTransactions": include_transactions,
            "includeExternal": include_external,
            "includeQuoteDetails": include_quote_details,
            **extra,  # other Lodgify filters, passed through using Lodgify's camelCase names
        }
        return await self._request("GET", "/v2/reservations/bookings", params=params)


# Current (in-stay) bookings, cached briefly so guest page polling doesn't hammer Lodgify
async def get_current_bookings(api_key: str, force: bool = False) -> list[dict]:
    global _current_cache
    if not force and _current_cache and time.time() - _current_cache[0] < CURRENT_CACHE_SECONDS:
        return _current_cache[1]
    async with LodgifyClient(api_key) as lg:
        data = await lg.get_bookings(stay_filter="Current")
    items = [b for b in (data or {}).get("items") or [] if b.get("status") == "Booked" and not b.get("is_deleted")]
    _current_cache = (time.time(), items)
    return items


# Door codes (rooms[].key_code) assigned to a booking
def booking_key_codes(booking: dict) -> list[str]:
    return [str(r["key_code"]) for r in booking.get("rooms") or [] if r.get("key_code")]


# Finds the current booking whose door code matches key_code (constant-time compare)
async def find_current_booking(api_key: str, key_code: str, force: bool = False) -> dict | None:
    match = None
    for booking in await get_current_bookings(api_key, force=force):
        for code in booking_key_codes(booking):
            if hmac.compare_digest(code.encode(), key_code.encode()):
                match = booking
    return match
