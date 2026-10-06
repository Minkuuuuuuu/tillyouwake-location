#!/usr/bin/env python3
"""TillyouWake location collector: iCloud Find My -> one atomic latest.json.

Reads /etc/tillyouwake-location/config.json every loop. Disabled, unconfigured or
a changed generation = exit 0 / drop the result; nothing is queued or replayed.
Logs carry kinds only: never coordinates, addresses, account names or keys.
"""
from __future__ import annotations

import argparse
import json
import logging
import math
import os
import re
import signal
import sys
import tempfile
import time
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Callable

CONFIG = Path("/etc/tillyouwake-location/config.json")
STATE_DIR = Path("/var/lib/tillyouwake-location")
POLL_SECONDS = 60
FRESH_SECONDS = 300
GOOD_ACCURACY_M = 100
ADDRESS_REUSE_M = 100
ADDRESS_MIN_GAP = 60
ADDRESS_RETRY = 300
FUTURE_SLACK = 30
CRS = ("wgs84", "gcj02")
HTTP_TIMEOUT = (10, 30)  # (connect, read) seconds for every iCloud request; pyicloud's default is None
AMAP = "https://restapi.amap.com/v3"
log = logging.getLogger("tillyouwake-location")


def distance_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    a = (math.sin((p2 - p1) / 2) ** 2
         + math.cos(p1) * math.cos(p2) * math.sin(math.radians(lon2 - lon1) / 2) ** 2)
    return 2 * 6371008.8 * math.asin(min(1.0, math.sqrt(a)))


def read_config(path: Path = CONFIG) -> dict | None:
    """Config, or None when missing/corrupt (treated as disabled)."""
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return raw if isinstance(raw, dict) else None


def runnable(config: dict | None) -> bool:
    return bool(config and config.get("enabled") is True and config.get("device_id")
                and config.get("source_crs") in CRS and isinstance(config.get("generation"), int))


def classify_error(exc: BaseException) -> str:
    """"network" when any link of the cause chain is a connection/timeout failure
    (pyicloud wraps those as login or API errors), "auth" for account/2FA problems, else "other"."""
    import http.client
    import socket
    network_names = ("ConnectionError", "Timeout", "ProtocolError", "RemoteDisconnected",
                     "NewConnectionError", "MaxRetryError", "SSLError")
    seen, link, chain = set(), exc, []
    while link is not None and id(link) not in seen:
        seen.add(id(link))
        chain.append(link)
        link = link.__cause__ or link.__context__
    for item in chain:
        if (isinstance(item, (ConnectionError, TimeoutError, socket.timeout, http.client.HTTPException))
                or any(name in type(item).__name__ for name in network_names)
                or (type(item).__name__ == "PyiCloudAPIResponseException" and "Request failed to iCloud" in str(item))):
            return "network"
    auth_names = ("Login", "2FA", "2SA", "AuthRequired", "Password", "TrustedDevice", "AcceptTerms", "Token")
    if any(any(name in type(item).__name__ for name in auth_names) for item in chain):
        return "auth"
    return "other"


def bound_timeouts(session: Any, token_only: bool = False) -> None:
    """Give this pyicloud session (only) a finite default timeout; explicit timeouts win."""
    original = session.request
    if token_only:
        session.trust_env = False

    def request(method, url, **kwargs):
        if token_only:
            kwargs["allow_redirects"] = False  # 307/308 must not resend the token POST
            kwargs["verify"] = True
            kwargs["timeout"] = HTTP_TIMEOUT
        kwargs.setdefault("timeout", HTTP_TIMEOUT)
        return original(method, url, **kwargs)

    session.request = request


_EDGE = re.compile(r"(setup|p\d{1,3}-fmipweb)\.icloud\.com\.cn")


def use_global_edge() -> None:
    """This process only: connect setup / pNN-fmipweb .icloud.com.cn through Apple's global
    setup edge; CN-only shards need not exist in global DNS. URL, Host, TLS SNI and certificate
    check all stay on the .cn name; no proxy, no hosts file, every other name untouched."""
    import socket
    original = socket.getaddrinfo
    if getattr(original, "_tillyou_global_edge", False):
        return

    def getaddrinfo(host, *args, **kwargs):
        if isinstance(host, str):
            match = _EDGE.fullmatch(host.lower().rstrip("."))
            if match:
                host = "setup.icloud.com"  # CN-only shards (e.g. p229) have no global DNS namesake
        return original(host, *args, **kwargs)

    getaddrinfo._tillyou_global_edge = True
    socket.getaddrinfo = getaddrinfo


def normalize_fix(location: Any, now: float) -> dict | None:
    """Find My location dict -> {latitude, longitude, accuracy_m, sampled_at} or None."""
    if not isinstance(location, dict):
        return None
    try:
        lat, lon = float(location["latitude"]), float(location["longitude"])
        acc = float(location["horizontalAccuracy"])
        ts = float(location["timeStamp"])
    except (KeyError, TypeError, ValueError):
        return None
    if ts > 1e11:  # Find My reports epoch milliseconds
        ts /= 1000.0
    if (not all(math.isfinite(v) for v in (lat, lon, acc, ts)) or not -90 <= lat <= 90
            or not -180 <= lon <= 180 or acc < 0 or ts <= 0 or ts > now + FUTURE_SLACK):
        return None
    return {"latitude": lat, "longitude": lon, "accuracy_m": acc, "sampled_at": ts}


def _address_needed(snap: dict, now: float) -> bool:
    if now - snap["sampled_at"] > FRESH_SECONDS or snap["accuracy_m"] > GOOD_ACCURACY_M:
        return False
    addr = snap.get("address") or {}
    if now < float(addr.get("retry_at") or 0) or now - float(addr.get("queried_at") or 0) < ADDRESS_MIN_GAP:
        return False
    if not addr.get("text") or addr.get("crs") != snap["source_crs"]:
        return True
    return distance_m(addr["lat"], addr["lon"], snap["latitude"], snap["longitude"]) > ADDRESS_REUSE_M


def poll_once(config: dict, previous: dict | None, now: float,
              fetch: Callable[[], Any], geocode: Callable[[float, float, str], str] | None) -> dict:
    """One poll. ``fetch`` returns the raw Find My location dict; ``geocode`` -> address text."""
    gen, device, crs = config["generation"], config["device_id"], config["source_crs"]
    prev = previous if (isinstance(previous, dict) and previous.get("generation") == gen
                        and previous.get("device_id") == device and previous.get("source_crs") == crs) else None
    since = float(config.get("enabled_since") or 0)
    snap: dict[str, Any] = {"generation": gen, "device_id": device, "source_crs": crs,
                            "checked_at": now, "status": "waiting_fresh", "error": None}
    for key in ("latitude", "longitude", "accuracy_m", "sampled_at", "fetched_at", "address"):
        if prev and key in prev:
            snap[key] = prev[key]
    try:
        fix = normalize_fix(fetch(), now)
        if fix is None:
            snap["error"] = "no_valid_fix"
        elif fix["sampled_at"] < since - FUTURE_SLACK:
            snap["error"] = "older_than_enable"
        elif "sampled_at" not in snap or fix["sampled_at"] > snap["sampled_at"]:
            snap.update(fix, fetched_at=now)
    except Exception as exc:  # noqa: BLE001 - one failed poll must not kill the loop
        snap["error"] = f"fetch_failed:{classify_error(exc)}:{type(exc).__name__}"
    if "sampled_at" in snap:
        snap["status"] = "ok" if now - snap["sampled_at"] <= FRESH_SECONDS else "stale"
        if geocode is not None and _address_needed(snap, now):
            addr = dict(snap.get("address") or {})
            try:
                text = str(geocode(snap["latitude"], snap["longitude"], crs) or "").strip()[:200]
                if not text:
                    raise ValueError("empty address")
                addr = {"text": text, "lat": snap["latitude"], "lon": snap["longitude"], "crs": crs,
                        "queried_at": now, "retry_at": 0, "error": None}
            except Exception as exc:  # noqa: BLE001 - coordinates stay valid without an address
                addr.update(retry_at=now + ADDRESS_RETRY, error="geocode_failed:" + type(exc).__name__)
            snap["address"] = addr
    return snap


def write_snapshot(path: Path, snapshot: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".latest-", dir=path.parent)
    try:
        os.fchmod(fd, 0o640)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(snapshot, fh, ensure_ascii=False)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def _amap_get(endpoint: str, params: dict, key: str) -> dict:
    url = f"{AMAP}/{endpoint}?" + urllib.parse.urlencode({**params, "key": key})
    with urllib.request.urlopen(url, timeout=8) as resp:
        data = json.loads(resp.read(65536))
    if str(data.get("status")) != "1":
        raise RuntimeError("amap_" + str(data.get("infocode") or "error")[:12])
    return data


def amap_geocoder(key: str) -> Callable[[float, float, str], str]:
    def geocode(lat: float, lon: float, crs: str) -> str:
        loc = f"{lon:.6f},{lat:.6f}"  # AMap wants lng,lat
        if crs == "wgs84":
            loc = _amap_get("assistant/coordinate/convert", {"locations": loc, "coordsys": "gps"}, key)["locations"]
            loc = loc.split(";")[0]
        regeo = _amap_get("geocode/regeo", {"location": loc, "extensions": "base"}, key)
        text = regeo.get("regeocode", {}).get("formatted_address")
        return text if isinstance(text, str) else ""
    return geocode


class ResumeLoginRefused(Exception):
    """pyicloud tried to re-authenticate (e.g. Find My 450); token-only paths never do that."""


class SavedLoginMissing(ResumeLoginRefused):
    """No trusted saved session (session_token + trust_token) to continue."""


def _token_session(config: dict):
    """A pyicloud instance that can only continue the saved trusted session: no password,
    locked transport (no redirects, TLS verify, timeout, no env proxies) and every
    re-authentication refused. The caller does the single accountLogin."""
    from pyicloud import PyiCloudService
    apple_id = os.environ.get("ICLOUD_APPLE_ID", "").strip()
    if not apple_id:
        raise RuntimeError("apple_id_missing")
    api = PyiCloudService(apple_id, None, cookie_directory=str(STATE_DIR / "cookies"), with_family=False,
                          china_mainland=config.get("icloud_region") == "cn", authenticate=False)
    bound_timeouts(api.session, token_only=True)

    def refuse(*_args, **_kwargs):
        raise ResumeLoginRefused()

    api.authenticate = refuse  # no SRP / 2FA fallback, also not via Find My's 450 re-auth
    if not (api.session.data.get("session_token") and api.session.data.get("trust_token")):
        raise SavedLoginMissing()
    return api


def _service(config: dict, interactive: bool = False, fresh: bool = False):
    if config.get("icloud_edge") == "global":
        use_global_edge()
    if not interactive:
        # probe / run: saved token only, one accountLogin, Find My then on this same instance.
        api = _token_session(config)
        api._authenticate_with_token()  # ponytail: private pyicloud==2.7.0 step; re-check on upgrade
        return api
    from pyicloud import PyiCloudService
    apple_id = os.environ.get("ICLOUD_APPLE_ID", "").strip()
    if not apple_id:
        raise RuntimeError("apple_id_missing")
    password = os.environ.get("ICLOUD_PASSWORD") or None
    if interactive and password is None:
        import getpass
        password = getpass.getpass("Apple ID 密码（不会保存）：")
    cookies = STATE_DIR / "cookies"
    cookies.mkdir(mode=0o700, parents=True, exist_ok=True)
    api = PyiCloudService(apple_id, password, cookie_directory=str(cookies), with_family=False,
                          china_mainland=config.get("icloud_region") == "cn", authenticate=False)
    bound_timeouts(api.session)
    if interactive:
        print(f"正在连接 Apple 验证账号（每一步最多等约 {sum(HTTP_TIMEOUT)} 秒）……", flush=True)
    api.authenticate(force_refresh=fresh)  # fresh: skip the saved web cookie, sign in with the password
    return api


_LOGIN_FAILURE = {
    "network": (3, "连不上 Apple 的服务器，网络中断或超时（这是网络问题，不是密码或验证码错误）。"
                   "不要马上重试，把这句话告诉 Codex。"),
    "auth": (4, "Apple 没有接受这次登录（账号、密码或验证需要重新确认）。先别重试，把这句话告诉 Codex。"),
    "other": (5, "登录中途出错了。先别重试，把这句话告诉 Codex。"),
}


def login(config: dict, fresh: bool = False) -> int:
    """Interactive, run by her: password + 2FA, then list devices (ids/names only).
    ``fresh`` (re-login keeping the settings): always a new sign-in, then only confirm that the
    configured device is still on the account; prints no ids.

    Prints each stage and one classified line on failure; never a traceback, never secrets.
    """
    try:
        api = _service(config, interactive=True, fresh=fresh)
        if api.requires_2fa:
            code = input("请输入 Apple 发到你设备上的 6 位验证码，然后按回车：").strip()
            print("正在提交验证码并建立会话……", flush=True)
            if not api.validate_2fa_code(code):
                print("验证码没有通过（Apple 明确拒绝了这个验证码）。")
                return 1
            if not api.is_trusted_session:
                api.trust_session()
        print("登录成功，正在读取设备列表……", flush=True)
        if fresh:
            if config.get("device_id") in api.devices.devices:
                print("找到了原来设置的那台设备。", flush=True)
                return 0
            print("账号里没有找到原来设置的那台设备。登录本身成功了，但定位还用不了，把这句话告诉 Codex。")
            return 8
        for device_id, device in api.devices.devices.items():
            print(device_id, "|", getattr(device, "name", "?"), "|", getattr(device, "model_name", "?"))
        return 0
    except KeyboardInterrupt:
        print("\n已取消。")
        return 130
    except Exception as exc:  # noqa: BLE001 - one readable line instead of a traceback
        kind = classify_error(exc)
        code, message = _LOGIN_FAILURE[kind]
        print(f"{message}（类型：{kind}/{type(exc).__name__}）")
        return code


def _error_chain(exc: BaseException) -> list[str]:
    """Exception type names along the cause chain (no messages: they may carry URLs/bodies)."""
    names, seen, link = [], set(), exc
    while link is not None and id(link) not in seen:
        seen.add(id(link))
        names.append(type(link).__name__)
        link = link.__cause__ or link.__context__
    return names


def _http_code(exc: BaseException) -> int | None:
    """First Apple/HTTP status number on the cause chain (never a body): 421 expired vs 5xx outage."""
    seen, link = set(), exc
    while link is not None and id(link) not in seen:
        seen.add(id(link))
        code = getattr(link, "code", None)
        if str(code).isdigit():
            return int(code)
        link = link.__cause__ or link.__context__
    return None


def _save_resume_diagnostic(stage: str, host: str, routed: bool | None, exc: BaseException | None) -> None:
    """Privacy-free metadata for the next fix: stage, Apple host, route, error types. No account,
    no URL path/query, no headers, no body, no token."""
    try:
        write_snapshot(STATE_DIR / "last-resume-diagnostic.json", {
            "at": time.time(), "stage": stage, "host": host, "via_global_edge": routed,
            "error_kind": classify_error(exc) if exc else None,
            "error_chain": _error_chain(exc) if exc else []})
    except OSError:
        pass


def resume(config: dict) -> int:
    """Interactive, run by her: finish the accountLogin that broke after 2FA, using the saved
    trusted session. No password, no 2FA, exactly one token request; then list devices."""
    print("续接已保存的可信会话：不需要密码和验证码，只向 Apple 发一次会话登录请求。", flush=True)
    if config.get("icloud_edge") == "global":
        use_global_edge()
        print("中国区原域名和证书不变，改经 Apple 全球边缘节点连接；这条路是否适用于你的中国区账号，以这次结果为准。",
              flush=True)
    stage, host, routed = "会话登录", "setup.icloud.com.cn", config.get("icloud_edge") == "global"
    try:
        try:
            api = _token_session(config)
        except SavedLoginMissing:
            print("没有找到可续接的可信会话，需要本人重新完整登录。")
            return 6
        print(f"正在完成会话登录（最多约 {sum(HTTP_TIMEOUT)} 秒）……", flush=True)
        # ponytail: private pyicloud==2.7.0 step (POST {setup}/accountLogin); re-check on upgrade
        api._authenticate_with_token()
        findme = ((getattr(api, "data", None) or {}).get("webservices") or {}).get("findme") or {}
        stage = "读取设备列表"
        host = (urllib.parse.urlsplit(str(findme.get("url") or "")).hostname or "").lower()
        routed = config.get("icloud_edge") == "global" and bool(_EDGE.fullmatch(host))
        print(f"登录成功。Find My 服务器：{host or '未知'}（{'经全球边缘' if routed else '直连'}）", flush=True)
        if config.get("icloud_edge") == "global" and host.endswith(".icloud.com.cn") and not routed:
            _save_resume_diagnostic(stage, host, routed, None)
            print("这个 Find My 服务器不在已验证的全球边缘范围内，为免直连超时，先停下，没有发请求。把上一行告诉 Codex。")
            return 7
        print("正在读取设备列表……", flush=True)
        for device_id, device in api.devices.devices.items():
            print(device_id, "|", getattr(device, "name", "?"), "|", getattr(device, "model_name", "?"))
        return 0
    except KeyboardInterrupt:
        print("\n已取消。")
        return 130
    except Exception as exc:  # noqa: BLE001 - one readable line, then stop; never fall back
        kind = classify_error(exc)
        code, message = _LOGIN_FAILURE[kind]
        print(f"{message}（类型：{kind}/{type(exc).__name__}）")
        print(f"出错阶段：{stage}；服务器：{host or '未知'}（{'经全球边缘' if routed else '直连'}）；"
              f"原因链：{' <- '.join(_error_chain(exc))}")
        _save_resume_diagnostic(stage, host, routed, exc)
        if kind == "auth":
            print("已保存的会话没能续接；不会改用密码或验证码重试。")
        return code


def auth_lost(snapshot: dict) -> bool:
    """A poll that failed on authentication: stop instead of re-logging in every minute
    (each attempt may push a verification prompt to her phone)."""
    return str(snapshot.get("error") or "").startswith("fetch_failed:auth:")


def probe(config: dict) -> int:
    """Interactive, with her present: print one raw fix of the chosen device to the
    terminal (never to a log) so the coordinate system can be checked against a known place."""
    api = _service(config)
    api.devices.refresh(locate=True)
    fix = normalize_fix(api.devices.devices[config["device_id"]].location, time.time())
    print(json.dumps(fix, ensure_ascii=False) if fix else "这台设备暂时没有有效定位。")
    return 0


def run(config_path: Path = CONFIG) -> int:
    stop = {"flag": False}
    signal.signal(signal.SIGTERM, lambda *_: stop.update(flag=True))
    latest = STATE_DIR / "latest.json"
    config = read_config(config_path)
    if not runnable(config):
        log.info("location disabled_or_unconfigured; exiting")
        return 0
    try:
        api = _service(config)
        if api.requires_2fa or api.requires_2sa:
            raise RuntimeError("login_required")
    except Exception as exc:  # noqa: BLE001
        kind = classify_error(exc)
        log.warning("location login_failed kind=%s type=%s code=%s", kind, type(exc).__name__, _http_code(exc))
        write_snapshot(latest, {"generation": config["generation"], "device_id": config["device_id"],
                                "source_crs": config["source_crs"], "checked_at": time.time(),
                                "status": "network_error" if kind == "network" else "needs_login",
                                "error": f"login_failed:{kind}"})
        return 0  # ponytail: no automatic re-login against her main account; she switches on again
    key = os.environ.get("AMAP_KEY", "").strip()
    geocode = amap_geocoder(key) if key else None
    while not stop["flag"]:
        try:
            previous = json.loads(latest.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            previous = None

        def fetch():
            api.devices.refresh(locate=True)
            return api.devices.devices[config["device_id"]].location

        snap = poll_once(config, previous, time.time(), fetch, geocode)
        current = read_config(config_path)  # a late result never crosses a toggle
        if not runnable(current) or current["generation"] != config["generation"]:
            log.info("location config_changed; exiting without publishing")
            return 0
        if auth_lost(snap):
            snap.update(status="needs_login")
            write_snapshot(latest, snap)
            log.warning("location login_lost; exiting")
            return 0
        write_snapshot(latest, snap)
        log.info("location poll status=%s error=%s", snap["status"], snap.get("error") or "none")
        for _ in range(POLL_SECONDS):
            if stop["flag"]:
                break
            time.sleep(1)
            if not runnable(read_config(config_path)):
                return 0
    return 0


def main(argv: list[str] | None = None) -> int:
    global STATE_DIR
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path, default=CONFIG)
    parser.add_argument("--state-dir", type=Path, default=STATE_DIR)
    parser.add_argument("--login", action="store_true")
    parser.add_argument("--fresh", action="store_true")
    parser.add_argument("--probe", action="store_true")
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--global-edge", action="store_true")
    args = parser.parse_args(argv)
    STATE_DIR = args.state_dir
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    if args.login:
        return login(read_config(args.config) or {}, fresh=args.fresh)
    if args.probe:
        return probe(read_config(args.config) or {})
    if args.resume:
        config = read_config(args.config) or {}
        if args.global_edge:
            config["icloud_edge"] = "global"
        return resume(config)
    return run(args.config)


if __name__ == "__main__":
    sys.exit(main())
