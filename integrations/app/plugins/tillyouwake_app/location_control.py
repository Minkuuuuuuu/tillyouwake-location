"""App setting: the one location switch (config + the fixed collector unit).

This side alone writes /etc/tillyouwake-location/config.json; the main gateway
owns its runtime. Only ``{"enabled": bool}`` is accepted and only the fixed unit
below is ever started/stopped. Never returns account, key or device id; the
only location it returns is ``preview``: the exact [位置] text the App text lane
would add right now (same formatter, so it is empty when switched off). Places
are only named here (id, name, radius); their centres stay in places.json.
"""
from __future__ import annotations

import contextlib
import fcntl
import grp
import importlib.util
import json
import os
import re
import secrets
import subprocess
import tempfile
import threading
import time
from pathlib import Path

from .model_settings import SettingsError

CONFIG = Path("/etc/tillyouwake-location/config.json")
CREDENTIALS = Path("/etc/tillyouwake-location/credentials.env")
LATEST = Path("/var/lib/tillyouwake-location/latest.json")
PLACES = CONFIG.with_name("places.json")
PLACE_NAME = re.compile(r"[\w·\- ]{1,12}")
PLACE_RADIUS_M = 30         # a "nearby" circle; circles of two places must not overlap
MARK_MAX_ACCURACY_M = PLACE_RADIUS_M
MARK_FRESH_SECONDS = 300
MAX_PLACES = 20
UNIT = "tillyouwake-location.service"
GROUP = "tillyouwake-location"
CRS = ("wgs84", "gcj02")
_lock = threading.Lock()


def _systemctl(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["systemctl", *args], capture_output=True, text=True, timeout=3)


def _load(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _write(config: dict) -> None:
    fd, tmp = tempfile.mkstemp(prefix=".config-", dir=CONFIG.parent)
    try:
        os.fchmod(fd, 0o640)
        with contextlib.suppress(KeyError, PermissionError):
            os.fchown(fd, -1, grp.getgrnam(GROUP).gr_gid)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(config, fh, ensure_ascii=False, indent=1)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, CONFIG)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def _configured(config: dict) -> bool:
    if not config.get("device_id") or config.get("source_crs") not in CRS:
        return False
    try:
        lines = CREDENTIALS.read_text(encoding="utf-8").splitlines()
    except OSError:
        return False
    return any(line.startswith("ICLOUD_APPLE_ID=") and line.split("=", 1)[1].strip() for line in lines)


def _active() -> str:
    try:
        return _systemctl("is-active", UNIT).stdout.strip() or "unknown"
    except (OSError, subprocess.SubprocessError):
        return "unknown"


_formatter = None


def _plugin():
    global _formatter
    if _formatter is None:
        path = Path(__file__).resolve().parents[1] / "tillyouwake_location" / "__init__.py"
        spec = importlib.util.spec_from_file_location("tillyouwake_location_preview", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        _formatter = module
    return _formatter


def _preview(config: dict) -> str | None:
    """The plugin's own formatter over the local snapshot; never a new Apple/AMap request."""
    try:
        plugin = _plugin()
        return plugin.format_location(plugin.current(config, _load(LATEST)), time.time(),
                                      plugin.load_places(PLACES)) or None
    except Exception:  # noqa: BLE001 - a preview never breaks the settings page
        return None


def _places() -> list:
    data = _load(PLACES)
    items = data.get("places") if isinstance(data, dict) else None
    return [p for p in items if isinstance(p, dict)] if isinstance(items, list) else []


def _markable(config: dict):
    """(fix, None) when the cached fix of this generation is fresh and precise, else (None, reason)."""
    if config.get("enabled") is not True:
        return None, "location_off"
    try:
        fix = _plugin().current(config, _load(LATEST))
    except Exception:  # noqa: BLE001
        fix = None
    if not fix:
        return None, "no_fix"
    if time.time() - float(fix["sampled_at"]) > MARK_FRESH_SECONDS:
        return None, "fix_stale"
    if float(fix.get("accuracy_m") or 1e9) > MARK_MAX_ACCURACY_M:
        return None, "fix_imprecise"
    return fix, None


def _place_name(value) -> str:
    name = " ".join(value.split()) if isinstance(value, str) else ""
    if not name or not PLACE_NAME.fullmatch(name):
        raise SettingsError(400, "invalid_place_name")
    return name


def _place(action) -> None:
    """mark / rename / delete one named place; caller holds the config lock."""
    if not isinstance(action, dict) or action.get("action") not in {"mark", "rename", "delete"}:
        raise SettingsError(400, "invalid_place_action")
    kind = action["action"]
    expected = {"mark": {"action", "name"}, "rename": {"action", "id", "name"}, "delete": {"action", "id"}}[kind]
    if set(action) != expected:
        raise SettingsError(400, "invalid_place_action")
    places = _places()
    if kind == "mark":
        name = _place_name(action["name"])
        config = _load(CONFIG)
        fix, reason = _markable(config if isinstance(config, dict) else {})
        if fix is None:
            raise SettingsError(409, "place_" + reason)
        if any(p.get("name") == name for p in places):
            raise SettingsError(409, "place_name_taken")
        if len(places) >= MAX_PLACES:
            raise SettingsError(409, "place_limit")
        plugin = _plugin()
        if any(p.get("crs") == fix["source_crs"] and isinstance(p.get("lat"), (int, float))
               and plugin._distance_m(p["lat"], p["lon"], fix["latitude"], fix["longitude"])
               < PLACE_RADIUS_M + float(p.get("radius_m") or PLACE_RADIUS_M)
               for p in places):
            raise SettingsError(409, "place_too_close")
        places.append({"id": secrets.token_hex(6), "name": name, "lat": float(fix["latitude"]),
                       "lon": float(fix["longitude"]), "crs": fix["source_crs"], "radius_m": PLACE_RADIUS_M,
                       "accuracy_m": float(fix["accuracy_m"]), "sampled_at": float(fix["sampled_at"]),
                       "saved_at": time.time()})
    else:
        target = next((p for p in places if isinstance(action["id"], str) and p.get("id") == action["id"]), None)
        if target is None:
            raise SettingsError(409, "place_not_found")
        if kind == "rename":
            name = _place_name(action["name"])
            if any(p.get("name") == name and p is not target for p in places):
                raise SettingsError(409, "place_name_taken")
            target["name"] = name
        else:
            places.remove(target)
    fd, tmp = tempfile.mkstemp(prefix=".places-", dir=PLACES.parent)
    try:
        os.fchmod(fd, 0o600)  # root only: the App reads it, the collector never needs it
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump({"places": places}, fh, ensure_ascii=False, indent=1)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, PLACES)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def snapshot() -> dict:
    config = _load(CONFIG)
    if not isinstance(config, dict):
        return {"enabled": False, "running": False, "status": "unavailable", "last_error": None}
    places = [{"id": p.get("id"), "name": p.get("name"), "radius_m": p.get("radius_m")} for p in _places()
              if isinstance(p.get("id"), str) and isinstance(p.get("name"), str)]
    _, blocker = _markable(config)
    active = _active()
    running = active in {"active", "activating", "reloading"}
    enabled = config.get("enabled") is True
    status, error = "off", None
    if not _configured(config):
        status = "needs_setup"
    if enabled:
        latest = _load(LATEST)
        mine = isinstance(latest, dict) and latest.get("generation") == config.get("generation")
        status = (latest.get("status") if mine else "waiting_fresh") or "waiting_fresh"
        error = latest.get("error") if mine else None
        if not running and status not in {"needs_login"}:
            status = "not_running"
    elif running:
        status = "stopping"
    return {"enabled": enabled, "running": running, "status": status,
            "last_error": error if isinstance(error, str) else None,
            "preview": _preview(config) if enabled else None,
            "places": places, "mark_blocker": blocker}


def update(payload) -> dict:
    if isinstance(payload, dict) and set(payload) == {"place"}:
        with _lock, open(CONFIG.with_name(".config.lock"), "a") as lock_file:
            fcntl.flock(lock_file, fcntl.LOCK_EX)
            _place(payload["place"])
        return snapshot()
    if not isinstance(payload, dict) or set(payload) != {"enabled"} or not isinstance(payload["enabled"], bool):
        raise SettingsError(400, "invalid_location_setting")
    want = payload["enabled"]
    with _lock, open(CONFIG.with_name(".config.lock"), "a") as lock_file:
        fcntl.flock(lock_file, fcntl.LOCK_EX)
        config = _load(CONFIG)
        if not isinstance(config, dict):
            raise SettingsError(503, "location_config_unavailable")
        changed = (config.get("enabled") is True) != want
        if want:
            if not _configured(config):
                raise SettingsError(409, "location_not_configured")
            if changed:
                config.update(enabled=True, generation=int(config.get("generation") or 0) + 1,
                              enabled_since=time.time())
                _write(config)
            try:
                started = _systemctl("start", "--no-block", UNIT).returncode == 0
            except (OSError, subprocess.SubprocessError):
                started = False
            if not started:
                config.update(enabled=False, generation=int(config["generation"]) + 1)
                _write(config)
                raise SettingsError(503, "location_start_failed")
        else:
            if changed:
                config.update(enabled=False, generation=int(config.get("generation") or 0) + 1)
                _write(config)  # consumers stop using location before the process is stopped
            try:
                stopped = _systemctl("stop", "--no-block", UNIT).returncode == 0
            except (OSError, subprocess.SubprocessError):
                stopped = False
            if not stopped:
                return {**snapshot(), "status": "stop_failed"}
    return snapshot()
