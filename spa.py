import time
import asyncio
from iaqualink.client import AqualinkClient
from config import EMAIL, PASSWORD, SERIAL, VALVE_COOLDOWN, TEMP_LIMITS

STATUS_CACHE_SECONDS = 15

last_run = 0.0
_command_lock = asyncio.Lock()  # Serializes equipment commands (guest page + Leviton/IFTTT + watchdog)
_status_cache: tuple[float, dict] | None = None
_background_tasks: set[asyncio.Task] = set()


class CooldownError(Exception):
    pass


# Retrieves the Aqualink system, either by specified serial or defaulting to the first available
async def get_system(client):
    systems = await client.get_systems()
    if not systems:
        raise RuntimeError("No iAquaLink systems found for this account.")
    return systems.get(SERIAL) or list(systems.values())[0]


# Seconds left in the mechanical cooldown after a spa on/off command (0 if not locked)
def cooldown_remaining() -> int:
    return max(0, int(VALVE_COOLDOWN - (time.time() - last_run)))


def is_locked() -> bool:
    """Check if the system is in a mechanical cooldown period."""
    return cooldown_remaining() > 0


def _invalidate_status() -> None:
    global _status_cache
    _status_cache = None


def _temp(device) -> int | None:
    try:
        return int(float(device.state)) if device and device.state not in (None, "") else None
    except ValueError:
        return None


def _on(device) -> bool | None:
    return bool(device.is_on) if device is not None else None


# Turns on spa pump + spa heater, turns off pool heater, and enables aux_1 (blower) after 30s
async def spa_on() -> None:
    global last_run
    async with _command_lock:
        if is_locked():
            raise CooldownError()
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
        _invalidate_status()
    task = asyncio.create_task(_enable_aux1_after_delay())
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)


# Turns off spa pump + spa heater + aux_1 (blower), and turns the pool heater back on
async def spa_off() -> None:
    global last_run
    async with _command_lock:
        if is_locked():
            raise CooldownError()
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
        _invalidate_status()


# Background task: turns on aux_1 30 seconds after spa-on
async def _enable_aux1_after_delay() -> None:
    await asyncio.sleep(30)
    try:
        await set_aux1(True)
        print("aux_1 enabled (30s post spa-on delay).")
    except Exception as e:
        print(f"aux_1 enable error: {e}")


# Turns aux_1 (air blower) on or off
async def set_aux1(on: bool) -> None:
    async with _command_lock:
        async with AqualinkClient(EMAIL, PASSWORD) as client:
            system = await get_system(client)
            await system.update()
            devices = await system.get_devices()
            aux1 = devices.get("aux_1")
            if aux1 is None:
                raise RuntimeError("aux_1 not found")
            if on and not aux1.is_on:
                await aux1.turn_on()
            elif not on and aux1.is_on:
                await aux1.turn_off()
        _invalidate_status()


# Sets the spa or pool set point, enforcing the configured min/max range
async def set_temp(kind: str, value: int) -> None:
    if kind not in TEMP_LIMITS:
        raise ValueError(f"Unknown set point: {kind}")
    low, high = TEMP_LIMITS[kind]
    if not low <= value <= high:
        raise ValueError(f"{kind.capitalize()} temperature must be between {low} and {high}°F")
    async with _command_lock:
        async with AqualinkClient(EMAIL, PASSWORD) as client:
            system = await get_system(client)
            await system.update()
            thermostat = system.devices.get(f"{kind}_set_point")
            if thermostat is None:
                raise RuntimeError(f"{kind}_set_point not found")
            await thermostat.set_temperature(value)
        _invalidate_status()


# Current spa/pool state for the guest and admin pages (cached briefly to limit Aqualink logins)
async def get_status(force: bool = False) -> dict:
    global _status_cache
    if not force and _status_cache and time.time() - _status_cache[0] < STATUS_CACHE_SECONDS:
        status = dict(_status_cache[1])
    else:
        async with AqualinkClient(EMAIL, PASSWORD) as client:
            system = await get_system(client)
            await system.update()
            d = system.devices
            status = {
                "spa_pump": _on(d.get("spa_pump")),
                "spa_heater": _on(d.get("spa_heater")),
                "blower": _on(d.get("aux_1")),
                "pool_pump": _on(d.get("pool_pump")),
                "pool_heater": _on(d.get("pool_heater")),
                "temps": {
                    "spa": _temp(d.get("spa_temp")),
                    "pool": _temp(d.get("pool_temp")),
                    "air": _temp(d.get("air_temp")),
                },
                "set_points": {
                    "spa": _temp(d.get("spa_set_point")),
                    "pool": _temp(d.get("pool_set_point")),
                },
            }
        _status_cache = (time.time(), status)
        status = dict(status)
    status["limits"] = {k: {"min": lo, "max": hi} for k, (lo, hi) in TEMP_LIMITS.items()}
    status["cooldown_remaining"] = cooldown_remaining()
    return status
