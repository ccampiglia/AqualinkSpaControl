import time
import asyncio
from iaqualink.client import AqualinkClient
from config import EMAIL, PASSWORD, SERIAL, IAQUALINK_MAX_SET_POINT, send_pushover_alert
import events
import settings

STATUS_CACHE_SECONDS = 15

last_run = 0.0
_command_lock = asyncio.Lock()  # Serializes equipment commands (guest page + Leviton/IFTTT + watchdog)
_status_cache: tuple[float, dict] | None = None
_background_tasks: set[asyncio.Task] = set()
_spa_generation = 0  # bumped by every spa on/off and manual blower command; cancels a pending automatic blower start

BLOWER_DELAY = 30           # seconds after spa-on before the blower starts
BLOWER_RETRY_INTERVAL = 15  # seconds between checks while waiting for the spa pump / confirming the blower
BLOWER_MAX_WAIT = 120       # give up (and alert) this long after the first attempt
BLOWER_MAX_SENDS = 3        # turn-on commands to send before giving up on a blower that won't stay on


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
    return max(0, int(settings.get("VALVE_COOLDOWN") - (time.time() - last_run)))


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


# Heater state from Aqualink: "0" off, "3" enabled (idle at set point), "1" actively heating
def _heater_state(device) -> str | None:
    if device is None:
        return None
    return {"1": "heating", "3": "on"}.get(str(device.state), "off")


# Turns on spa pump + spa heater, turns off pool heater, and enables aux_1 (blower) once the spa pump is running
async def spa_on() -> None:
    global last_run, _spa_generation
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
        _spa_generation += 1
        generation = _spa_generation
        _invalidate_status()
    task = asyncio.create_task(_enable_aux1_after_delay(generation))
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)


# Turns off spa pump + spa heater + aux_1 (blower), and turns the pool heater back on
async def spa_off() -> None:
    global last_run, _spa_generation
    async with _command_lock:
        _spa_generation += 1  # cancel any pending automatic blower start
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


# One automatic blower attempt with a fresh controller read. Returns:
#   "confirmed" — spa pump and blower both read as on
#   "sent"      — spa pump is on and the blower was off, so we sent the turn-on (confirmed on the next attempt)
#   "waiting"   — spa pump isn't reporting on yet
#   "off_again" — blower still off after we already sent the turn-on BLOWER_MAX_SENDS times (not resent)
#   "cancelled" — a spa on/off or manual blower command happened since this spa-on
async def _blower_attempt(generation: int, may_send: bool = True) -> str:
    async with _command_lock:
        if generation != _spa_generation:
            return "cancelled"
        async with AqualinkClient(EMAIL, PASSWORD) as client:
            system = await get_system(client)
            await system.update()
            devices = await system.get_devices()
            aux1 = devices.get("aux_1")
            if aux1 is None:
                raise RuntimeError("aux_1 not found")
            if not _on(devices.get("spa_pump")):
                return "waiting"
            if aux1.is_on:
                return "confirmed"
            if not may_send:
                return "off_again"
            await aux1.turn_on()
    _invalidate_status()
    return "sent"


# Background task: after spa-on, waits for the spa pump to report running, turns the blower on,
# then re-reads the controller to confirm it's actually on. Retries until BLOWER_MAX_WAIT, then alerts.
async def _enable_aux1_after_delay(generation: int) -> None:
    await asyncio.sleep(BLOWER_DELAY)
    started = time.time()
    attempts, sent, last = 0, 0, ""
    while True:
        attempts += 1
        try:
            last = await _blower_attempt(generation, may_send=sent < BLOWER_MAX_SENDS)
        except Exception as e:
            last = f"error: {type(e).__name__}: {e}"
        waited = int(time.time() - started) + BLOWER_DELAY
        if last == "cancelled":
            print("Blower: spa was turned off or the blower was changed manually — automatic blower start cancelled.")
            return
        if last == "confirmed":
            print(f"Blower: confirmed ON with spa pump running ({waited}s after spa-on).")
            events.log_event("blower_auto_on", "spa", detail={"seconds_after_spa_on": waited, "attempts": attempts})
            return
        if last == "sent":
            sent += 1
            print(f"Blower: spa pump is running — sent blower ON ({waited}s after spa-on); confirming next check.")
        else:
            print(f"Blower: not started yet — { {'waiting': 'spa pump not reporting on', 'off_again': 'blower still off after 3 tries'}.get(last, last) } ({waited}s after spa-on).")
        if last == "off_again" or time.time() - started >= BLOWER_MAX_WAIT:
            reason = {"waiting": "the spa pump never reported running"}.get(
                last, "the blower didn't stay on after being turned on" if last in ("sent", "off_again") else last)
            events.log_event("blower_auto_failed", "spa", detail={"reason": reason, "attempts": attempts, "commands_sent": sent})
            await send_pushover_alert(f"Spa blower didn't start after spa-on: {reason}.")
            return
        await asyncio.sleep(BLOWER_RETRY_INTERVAL)


# Turns aux_1 (air blower) on or off; turning it on requires the spa pump to be running
async def set_aux1(on: bool) -> None:
    global _spa_generation
    async with _command_lock:
        _spa_generation += 1  # a manual blower command overrides any pending automatic start
        async with AqualinkClient(EMAIL, PASSWORD) as client:
            system = await get_system(client)
            await system.update()
            devices = await system.get_devices()
            aux1 = devices.get("aux_1")
            if aux1 is None:
                raise RuntimeError("aux_1 not found")
            if on and not _on(devices.get("spa_pump")):
                raise ValueError("The blower can only run while the spa is on.")
            if on and not aux1.is_on:
                await aux1.turn_on()
            elif not on and aux1.is_on:
                await aux1.turn_off()
        _invalidate_status()


# Turns the spa or pool heater on or off
async def set_heater(kind: str, on: bool) -> None:
    if kind not in ("spa", "pool"):
        raise ValueError(f"Unknown heater: {kind}")
    async with _command_lock:
        async with AqualinkClient(EMAIL, PASSWORD) as client:
            system = await get_system(client)
            await system.update()
            heater = system.devices.get(f"{kind}_heater")
            if heater is None:
                raise RuntimeError(f"{kind}_heater not found")
            if on and not heater.is_on:
                await heater.turn_on()
            elif not on and heater.is_on:
                await heater.turn_off()
        _invalidate_status()


# Set point range for the admin page: anything the controller accepts, kept below the overheat shutoff
def admin_temp_limits() -> dict[str, tuple[int, int]]:
    high = min(IAQUALINK_MAX_SET_POINT, settings.get("TEMP_OVERHEAT_THRESHOLD") - 1)
    return {"spa": (40, high), "pool": (40, high)}


# Sets the spa or pool set point, enforcing the guest min/max range (or the admin range when admin=True)
async def set_temp(kind: str, value: int, admin: bool = False) -> None:
    limits = admin_temp_limits() if admin else settings.temp_limits()
    if kind not in limits:
        raise ValueError(f"Unknown set point: {kind}")
    low, high = limits[kind]
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
                "freeze_protection": _on(d.get("freeze_protection")),
                "heater_state": {
                    "spa": _heater_state(d.get("spa_heater")),
                    "pool": _heater_state(d.get("pool_heater")),
                },
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
    status["limits"] = {k: {"min": lo, "max": hi} for k, (lo, hi) in settings.temp_limits().items()}
    status["cooldown_remaining"] = cooldown_remaining()
    return status


# Turns the pool pump on or off (admin only)
async def set_pool_pump(on: bool) -> None:
    async with _command_lock:
        async with AqualinkClient(EMAIL, PASSWORD) as client:
            system = await get_system(client)
            await system.update()
            pump = system.devices.get("pool_pump")
            if pump is None:
                raise RuntimeError("pool_pump not found")
            if on and not pump.is_on:
                await pump.turn_on()
            elif not on and pump.is_on:
                await pump.turn_off()
        _invalidate_status()


# Order matters: heaters off before the pumps that feed them, blower before the spa pump
ALL_OFF_ORDER = ("spa_heater", "pool_heater", "aux_1", "spa_pump", "pool_pump")


# Turns off heaters, blower and pumps one by one, only touching what is on (never a toggle that could turn
# something back on). Uses an already-updated system when given (watchdog), otherwise opens its own session.
# Returns the devices that were turned off; raises if any of them failed.
async def all_off(system=None) -> list[str]:
    async def run(sys_) -> list[str]:
        turned_off, failed = [], []
        for key in ALL_OFF_ORDER:
            device = sys_.devices.get(key)
            if device is None or not device.is_on:
                continue
            try:
                await device.turn_off()
                turned_off.append(key)
            except Exception as e:
                failed.append(f"{key} ({type(e).__name__})")
        if failed:
            raise RuntimeError(f"Could not turn off: {', '.join(failed)}; turned off: {', '.join(turned_off) or 'nothing'}")
        return turned_off

    async with _command_lock:
        if system is not None:
            result = await run(system)
        else:
            async with AqualinkClient(EMAIL, PASSWORD) as client:
                sys_ = await get_system(client)
                await sys_.update()
                result = await run(sys_)
    _invalidate_status()
    return result
