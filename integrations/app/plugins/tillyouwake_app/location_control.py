"""App setting: the one location switch (config + the fixed collector unit).

This side alone writes /etc/tillyouwake-location/config.json; the main gateway
owns its runtime. Only ``{"enabled": bool}`` is accepted and only the fixed unit
below is ever started/stopped. Never returns coordinates, account or key.
"""
from __future__ import annotations

import contextlib
import fcntl
import grp
import json
import os
import subprocess
import tempfile
import threading
import time
from pathlib import Path

from .model_settings import SettingsError

CONFIG = Path("/etc/tillyouwake-location/config.json")
CREDENTIALS = Path("/etc/tillyouwake-location/credentials.env")
LATEST = Path("/var/lib/tillyouwake-location/latest.json")
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


def snapshot() -> dict:
    config = _load(CONFIG)
    if not isinstance(config, dict):
        return {"enabled": False, "running": False, "status": "unavailable", "last_error": None}
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
            "last_error": error if isinstance(error, str) else None}


def update(payload) -> dict:
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
