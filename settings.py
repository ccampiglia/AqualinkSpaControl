"""Admin-editable settings, stored in the event database.

Environment variables remain the defaults; a value saved from the admin page overrides them.
Secrets (passwords, API keys) are never stored here.
"""
import json
import threading
import config
import events

_lock = threading.Lock()
_overrides: dict[str, int] = {}     # numeric settings saved from the admin page
_state: dict[str, object] = {}      # other stored values (pump guard schedule, pause, lockout)

# key -> (label, default from env, allowed min, allowed max, unit, help)
NUMERIC = {
    "TEMP_OVERHEAT_THRESHOLD": ("Overheat shutoff", config.TEMP_OVERHEAT_THRESHOLD, 90, 115, "°F",
                                "Everything turns off if the pool or spa reaches this temperature."),
    "TEMP_UNDERHEAT_THRESHOLD": ("Low pool temp alert", config.TEMP_UNDERHEAT_THRESHOLD, 40, 95, "°F",
                                 "Alert and turn on the pool heater when the pool is at or below this."),
    "TEMP_MONITOR_INTERVAL": ("Watchdog check interval", config.TEMP_MONITOR_INTERVAL, 60, 3600, "seconds",
                              "How often the watchdog reads the controller. Applies after the current wait."),
    "VALVE_COOLDOWN": ("Valve cooldown", config.VALVE_COOLDOWN, 0, 600, "seconds",
                       "Wait between spa on/off commands so the valves can finish moving."),
    "SPA_TEMP_MIN": ("Guest spa min", config.SPA_TEMP_MIN, 40, config.IAQUALINK_MAX_SET_POINT, "°F", "Lowest spa set point guests can choose."),
    "SPA_TEMP_MAX": ("Guest spa max", config.SPA_TEMP_MAX, 40, config.IAQUALINK_MAX_SET_POINT, "°F", "Highest spa set point guests can choose."),
    "POOL_TEMP_MIN": ("Guest pool min", config.POOL_TEMP_MIN, 40, config.IAQUALINK_MAX_SET_POINT, "°F", "Lowest pool set point guests can choose."),
    "POOL_TEMP_MAX": ("Guest pool max", config.POOL_TEMP_MAX, 40, config.IAQUALINK_MAX_SET_POINT, "°F", "Highest pool set point guests can choose."),
}

DEFAULT_GUARD = {"enabled": False, "keep_heater_on": False, "windows": []}


def init() -> None:
    """Creates the settings table and loads saved values. Call after events.init_db()."""
    conn = events._conn
    with events._lock:
        conn.execute("CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        conn.commit()
        rows = conn.execute("SELECT key, value FROM settings").fetchall()
    with _lock:
        for key, raw in rows:
            value = json.loads(raw)
            if key in NUMERIC:
                _overrides[key] = int(value)
            else:
                _state[key] = value
    try:
        validate(all_numeric())
    except ValueError as e:
        print(f"WARNING: saved settings are invalid ({e}) — ignoring saved overrides and using environment defaults.")
        with _lock:
            _overrides.clear()


def _write(key: str, value) -> None:
    with events._lock:
        if value is None:
            events._conn.execute("DELETE FROM settings WHERE key = ?", (key,))
        else:
            events._conn.execute(
                "INSERT INTO settings (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, json.dumps(value)),
            )
        events._conn.commit()


def get(key: str) -> int:
    with _lock:
        return _overrides.get(key, NUMERIC[key][1])


def all_numeric() -> dict[str, int]:
    return {k: get(k) for k in NUMERIC}


def temp_limits() -> dict[str, tuple[int, int]]:
    return {"spa": (get("SPA_TEMP_MIN"), get("SPA_TEMP_MAX")), "pool": (get("POOL_TEMP_MIN"), get("POOL_TEMP_MAX"))}


def validate(values: dict[str, int]) -> None:
    for key, (label, _, lo, hi, unit, _) in NUMERIC.items():
        if not lo <= values[key] <= hi:
            raise ValueError(f"{label} must be between {lo} and {hi} {unit}")
    over, under = values["TEMP_OVERHEAT_THRESHOLD"], values["TEMP_UNDERHEAT_THRESHOLD"]
    if under >= over:
        raise ValueError("Low pool temp alert must be below the overheat shutoff")
    for kind in ("SPA", "POOL"):
        low, high = values[f"{kind}_TEMP_MIN"], values[f"{kind}_TEMP_MAX"]
        if low >= high:
            raise ValueError(f"Guest {kind.lower()} min must be below guest {kind.lower()} max")
        if high >= over:
            raise ValueError(f"Guest {kind.lower()} max must be below the overheat shutoff, or guests could trigger the emergency all-off")


def update_numeric(changes: dict[str, int | None]) -> dict[str, int]:
    """Saves changes (None resets a setting to its environment default). Raises ValueError if invalid."""
    unknown = set(changes) - set(NUMERIC)
    if unknown:
        raise ValueError(f"Unknown setting: {', '.join(sorted(unknown))}")
    merged = all_numeric()
    for key, value in changes.items():
        merged[key] = NUMERIC[key][1] if value is None else int(value)
    validate(merged)
    with _lock:
        for key, value in changes.items():
            if value is None or int(value) == NUMERIC[key][1]:
                _overrides.pop(key, None)
                _write(key, None)
            else:
                _overrides[key] = int(value)
                _write(key, int(value))
    return all_numeric()


def describe() -> list[dict]:
    with _lock:
        overrides = dict(_overrides)
    return [
        {"key": k, "label": label, "value": overrides.get(k, default), "default": default,
         "min": lo, "max": hi, "unit": unit, "help": help, "overridden": k in overrides}
        for k, (label, default, lo, hi, unit, help) in NUMERIC.items()
    ]


def get_state(key: str, default=None):
    with _lock:
        value = _state.get(key, default)
    return json.loads(json.dumps(value))  # copy, so callers can't mutate the cache


def set_state(key: str, value) -> None:
    with _lock:
        if value is None:
            _state.pop(key, None)
        else:
            _state[key] = value
        _write(key, value)
