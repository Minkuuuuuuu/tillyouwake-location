"""App re-login to Apple for the location collector: she types her own password (and the code,
only if Apple asks) in the App's location card.

One flow at a time, at most FLOW_SECONDS. The password and the code only travel the stdin pipe of
one child ``collector.py --login-pipe`` run as the collector user: never argv/env, never a file,
log or response, and the page's flow id is required to continue or cancel. Account, Key, device,
coordinate system, places and the switch stay as they are; when the switch is already on, the
collector is started again after a successful login. Every end (done, failure, cancel, timeout)
closes the pipe and kills what is left of the child's process group. Never retried.
"""
from __future__ import annotations

import json
import logging
import os
import re
import secrets
import signal
import subprocess
import threading
import time
from pathlib import Path

from .model_settings import SettingsError

COLLECTOR = ("/opt/tillyouwake-location/.venv/bin/python", "/opt/tillyouwake-location/collector.py")
USER = "tillyouwake-location"
STATE_DIR = Path("/var/lib/tillyouwake-location")
FLOW_SECONDS = 300
STAGES = {"signing_in", "need_code", "verifying", "checking_device"}
ENDS = {"done", "failed", "cancelled", "expired"}
KINDS = {"auth", "network", "other", "code_rejected", "device_missing", "busy", "cancelled"}
FLOW_ID = re.compile(r"[0-9a-f]{32}")
CODE = re.compile(r"\d{6}")
SAFE_PATH = re.compile(r"/[A-Za-z0-9/_.-]{0,80}")
SAFE_NAME = re.compile(r"[A-Za-z][A-Za-z0-9_]{0,63}")
log = logging.getLogger(__name__)
_lock = threading.Lock()
_flow: dict | None = None  # {"id", "proc", "stage", "kind", "deadline", "ended_at"}


def _mask(apple_id: str) -> str:
    """Enough to recognise her own account, nothing more: s***@q***.com / 1********34."""
    if "@" in apple_id:
        local, _, domain = apple_id.partition("@")
        name, dot, tld = domain.rpartition(".")
        return f"{local[:1]}***@{(name or tld)[:1]}***{dot}{tld if name else ''}"
    digits = re.sub(r"\D", "", apple_id)
    return f"{digits[:1]}{'*' * max(len(digits) - 3, 3)}{digits[-2:]}" if len(digits) >= 5 else "***"


def account_hint(credentials: Path) -> str | None:
    try:
        for line in credentials.read_text(encoding="utf-8").splitlines():
            if line.startswith("ICLOUD_APPLE_ID="):
                value = json.loads(line.split("=", 1)[1])
                return _mask(value.strip()) if isinstance(value, str) and value.strip() else None
    except (OSError, ValueError):
        return None
    return None


def _apple_id(credentials: Path) -> str | None:
    for line in credentials.read_text(encoding="utf-8").splitlines():
        if line.startswith("ICLOUD_APPLE_ID="):
            value = json.loads(line.split("=", 1)[1])
            return value.strip() if isinstance(value, str) and value.strip() else None
    return None


def _spawn(apple_id: str) -> subprocess.Popen:
    env = {"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "HOME": str(STATE_DIR), "LANG": "C.UTF-8",
           "PYTHONIOENCODING": "utf-8", "PYTHONDONTWRITEBYTECODE": "1", "ICLOUD_APPLE_ID": apple_id}
    return subprocess.Popen(
        ["runuser", "-u", USER, "--preserve-environment", "--", *COLLECTOR, "--login-pipe"],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, env=env,
        cwd=str(STATE_DIR), text=True, encoding="utf-8", bufsize=1, close_fds=True, start_new_session=True)


def _kill(proc: subprocess.Popen) -> None:
    """Close the pipe (a waiting child reads EOF and stops), give a finished child a moment to
    exit, then kill whatever is left of its process group."""
    try:
        proc.stdin.close()
    except (OSError, ValueError):
        pass
    try:
        proc.wait(timeout=3)
    except subprocess.TimeoutExpired:
        pass
    try:
        os.killpg(proc.pid, signal.SIGKILL)  # runuser and the collector share this new session
    except (ProcessLookupError, PermissionError):
        pass
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        pass


def _detail(event: dict) -> str:
    """Only Apple's HTTP status, the request path and exception type names; anything else dropped."""
    http = event.get("http")
    at = event.get("at")
    chain = event.get("chain") if isinstance(event.get("chain"), list) else []
    return "http=%s at=%s chain=%s" % (
        http if isinstance(http, int) and not isinstance(http, bool) and 100 <= http <= 999 else "-",
        at if isinstance(at, str) and SAFE_PATH.fullmatch(at) else "-",
        "<-".join(n for n in chain[:4] if isinstance(n, str) and SAFE_NAME.fullmatch(n)) or "-")


def _end(flow: dict, stage: str, kind: str | None = None, detail: str = "") -> None:
    """Caller holds _lock. Idempotent: the first end wins."""
    if flow["stage"] in ENDS:
        return
    log.warning("location relogin end stage=%s kind=%s from=%s seconds=%d %s", stage, kind or "-",
                flow["stage"], int(time.time() - flow["deadline"] + FLOW_SECONDS), detail)
    flow.update(stage=stage, kind=kind, ended_at=time.time())
    threading.Thread(target=_kill, args=(flow["proc"],), daemon=True).start()


def _read(flow: dict, on_done) -> None:
    """Follow the child's JSON stage lines; anything else on stdout is ignored."""
    proc = flow["proc"]
    try:
        for line in proc.stdout:
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if not isinstance(event, dict):
                continue
            stage, kind = event.get("stage"), event.get("kind")
            with _lock:
                if flow["stage"] in ENDS:
                    return
                if stage in STAGES:
                    flow["stage"] = stage
                elif stage == "done":
                    _end(flow, "done")
                    break
                elif stage == "failed":
                    _end(flow, "failed", kind if kind in KINDS else "other", _detail(event))
                    return
    except (OSError, ValueError):
        pass
    finally:
        try:
            proc.stdout.close()
        except (OSError, ValueError):
            pass
    with _lock:
        succeeded = flow["stage"] == "done"
        _end(flow, "failed", "other")  # no-op after done; the child vanished otherwise
    if succeeded:
        on_done()


def _expire(flow: dict) -> None:
    with _lock:
        _end(flow, "expired")


def active() -> bool:
    with _lock:
        return _flow is not None and _flow["stage"] not in ENDS


def view() -> dict | None:
    """The current/last flow for the page that holds its id; no id, nothing typed."""
    with _lock:
        if _flow is None:
            return None
        ended = _flow["stage"] in ENDS
        return {"stage": _flow["stage"], "kind": _flow["kind"],
                "seconds_left": 0 if ended else max(0, int(_flow["deadline"] - time.time()))}


def start(password, credentials: Path, ready, on_done) -> str:
    """ready() -> None or raises SettingsError (configured, collector stopped); caller holds the
    config lock. Returns the flow id that code/cancel must carry."""
    global _flow
    if not isinstance(password, str) or not 0 < len(password) <= 256 or any(c in password for c in "\r\n\0"):
        raise SettingsError(400, "invalid_relogin")
    with _lock:
        if _flow is not None and _flow["stage"] not in ENDS:
            raise SettingsError(409, "relogin_busy")
        ready()
        apple_id = _apple_id(credentials)
        if not apple_id:
            raise SettingsError(409, "location_not_configured")
        try:
            proc = _spawn(apple_id)
        except OSError as exc:
            raise SettingsError(503, "relogin_unavailable") from exc
        flow = {"id": secrets.token_hex(16), "proc": proc, "stage": "signing_in", "kind": None,
                "deadline": time.time() + FLOW_SECONDS, "ended_at": None}
        _flow = flow
        try:
            proc.stdin.write(password + "\n")
            proc.stdin.flush()
        except (OSError, ValueError):
            _end(flow, "failed", "other")
            return flow["id"]
        finally:
            password = None
        timer = threading.Timer(FLOW_SECONDS, _expire, args=(flow,))
        timer.daemon = True
        timer.start()
        threading.Thread(target=_read, args=(flow, on_done), daemon=True).start()
        return flow["id"]


def _mine(flow_id) -> dict:
    if not isinstance(flow_id, str) or not FLOW_ID.fullmatch(flow_id) or _flow is None \
            or not secrets.compare_digest(flow_id, _flow["id"]):
        raise SettingsError(409, "relogin_flow_gone")
    return _flow


def code(flow_id, value) -> None:
    if not isinstance(value, str) or not CODE.fullmatch(value):
        raise SettingsError(400, "invalid_relogin_code")
    with _lock:
        flow = _mine(flow_id)
        if flow["stage"] != "need_code":
            raise SettingsError(409, "relogin_not_waiting_code")
        flow["stage"] = "verifying"
        try:
            flow["proc"].stdin.write(value + "\n")
            flow["proc"].stdin.flush()
        except (OSError, ValueError):
            _end(flow, "failed", "other")


def cancel(flow_id) -> None:
    with _lock:
        _end(_mine(flow_id), "cancelled")


def status(flow_id) -> None:
    with _lock:
        flow = _mine(flow_id)
        if flow["stage"] not in ENDS and time.time() > flow["deadline"]:
            _end(flow, "expired")
