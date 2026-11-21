"""Pump guard: keeps the pool pump (and optionally the pool heater) running during scheduled windows.

The schedule is kept in the app because the iAqualink API doesn't expose the controller's own schedules.
"""
import time
from datetime import datetime, timedelta
import config
import events
import settings
import spa

MAX_RESTARTS_PER_WINDOW = 3
DAY_NAMES = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]

_restarts = 0            # times the guard turned the pump back on in the current window
_gave_up = False         # True once the guard stopped retrying in the current window
_window_key: str | None = None


def get_config() -> dict:
    return settings.get_state("pump_guard", settings.DEFAULT_GUARD)


def _minutes(hhmm: str) -> int:
    h, m = hhmm.split(":")
    h, m = int(h), int(m)
    if not (0 <= h <= 23 and 0 <= m <= 59):
        raise ValueError
    return h * 60 + m


def validate_config(cfg: dict) -> dict:
    windows = []
    for w in cfg.get("windows") or []:
        days = sorted({int(d) for d in w.get("days") or []})
        if not days or any(d < 0 or d > 6 for d in days):
            raise ValueError("Each run window needs at least one day")
        try:
            start, end = _minutes(w["start"]), _minutes(w["end"])
        except (KeyError, ValueError, AttributeError):
            raise ValueError("Run window times must be HH:MM")
        if start == end:
            raise ValueError("A run window's start and end time can't be the same")
        windows.append({"days": days, "start": w["start"], "end": w["end"]})
    if len(windows) > 20:
        raise ValueError("Too many run windows")
    if cfg.get("enabled") and not windows:
        raise ValueError("Add at least one run window before turning the guard on")
    return {"enabled": bool(cfg.get("enabled")), "keep_heater_on": bool(cfg.get("keep_heater_on")), "windows": windows}


def save_config(cfg: dict) -> dict:
    cfg = validate_config(cfg)
    settings.set_state("pump_guard", cfg)
    return cfg


def _now() -> datetime:
    return datetime.now(config.PROPERTY_TIMEZONE)


# Returns (window index, window start datetime, window end datetime) if now is inside a window.
# Windows whose end is earlier than their start run overnight; the days list is the day they start.
def current_window(now: datetime | None = None, cfg: dict | None = None):
    now = now or _now()
    cfg = cfg or get_config()
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    for i, w in enumerate(cfg["windows"]):
        start, end = _minutes(w["start"]), _minutes(w["end"])
        overnight = end < start
        for day_offset in ((0, -1) if overnight else (0,)):
            day = midnight + timedelta(days=day_offset)
            if day.weekday() not in w["days"]:
                continue
            ws = day + timedelta(minutes=start)
            we = day + timedelta(days=1 if overnight else 0, minutes=end)
            if ws <= now < we:
                return i, ws, we
    return None


def paused_until() -> float:
    return float(settings.get_state("guard_paused_until", 0) or 0)


def pause(seconds: int) -> None:
    settings.set_state("guard_paused_until", time.time() + seconds)


def pause_until_window_end() -> bool:
    """Pauses the guard until the current window ends. Returns False if not inside a window."""
    win = current_window()
    if not win:
        return False
    settings.set_state("guard_paused_until", win[2].timestamp())
    return True


def resume() -> None:
    settings.set_state("guard_paused_until", None)


def overheat_lockout() -> dict | None:
    return settings.get_state("guard_overheat_lockout")


def set_overheat_lockout(detail: dict) -> None:
    settings.set_state("guard_overheat_lockout", {"ts": time.time(), **detail})


def clear_overheat_lockout() -> None:
    settings.set_state("guard_overheat_lockout", None)


def status() -> dict:
    cfg = get_config()
    win = current_window(cfg=cfg) if cfg["windows"] else None
    pu = paused_until()
    return {
        "config": cfg,
        "in_window": bool(win),
        "window_end": win[2].isoformat() if win else None,
        "paused_until": pu if pu > time.time() else None,
        "overheat_lockout": overheat_lockout(),
        "restarts_this_window": _restarts if win else 0,
        "gave_up": _gave_up if win else False,
        "max_restarts": MAX_RESTARTS_PER_WINDOW,
        "timezone": config.PROPERTY_TIMEZONE.key,
    }


# Called by the watchdog each cycle with an already-updated system. Returns a short description of what it did.
async def run(system, snap: dict) -> str:
    global _restarts, _gave_up, _window_key
    cfg = get_config()
    if not cfg["enabled"]:
        return "disabled"
    if overheat_lockout():
        return "overheat lockout"
    if paused_until() > time.time():
        return "paused"
    win = current_window(cfg=cfg)
    if not win:
        _window_key, _restarts, _gave_up = None, 0, False
        return "outside window"
    key = f"{win[0]}@{win[1].isoformat()}"
    if key != _window_key:
        _window_key, _restarts, _gave_up = key, 0, False

    eq = snap["equipment"]
    if eq["spa pump"]:
        return "spa in use"
    if spa.is_locked():
        return "valve cooldown"

    if not eq["pool pump"]:
        if _restarts >= MAX_RESTARTS_PER_WINDOW:
            if not _gave_up:
                _gave_up = True
                events.log_event("guard_gave_up", "guard", detail={"restarts": _restarts})
                await config.send_pushover_alert(
                    f"Pump guard: the pool pump keeps turning off — turned it back on {_restarts} times in this run "
                    "window. Not retrying until the next window. Check the controller schedule or for a fault."
                )
            return "gave up"
        pump = system.devices.get("pool_pump")
        if pump is None:
            return "no pool pump"
        async with spa._command_lock:
            await pump.turn_on()
        spa._invalidate_status()
        _restarts += 1
        events.log_event("guard_pump_on", "guard", detail={"restart": _restarts, "window_end": win[2].strftime("%H:%M")})
        await config.send_pushover_alert(
            f"Pump guard: pool pump was off during a scheduled run window — turned it back on (#{_restarts})."
        )
        return "pump turned on"

    if cfg["keep_heater_on"] and not eq["pool heater"]:
        heater = system.devices.get("pool_heater")
        if heater is None:
            return "no pool heater"
        async with spa._command_lock:
            await heater.turn_on()
        spa._invalidate_status()
        events.log_event("guard_heater_on", "guard")
        return "heater turned on"
    return "ok"
