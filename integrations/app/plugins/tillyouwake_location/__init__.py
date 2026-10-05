"""Her latest location as current-turn context for the App text lane (never phone)."""
from __future__ import annotations

import datetime as dt
import json
import math
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

CONFIG = Path("/etc/tillyouwake-location/config.json")
LATEST = Path("/var/lib/tillyouwake-location/latest.json")
FRESH_SECONDS = 300
GOOD_ACCURACY_M = 100
ADDRESS_REUSE_M = 100
SH = ZoneInfo("Asia/Shanghai")
CRS_LABEL = {"wgs84": "WGS-84", "gcj02": "GCJ-02"}


def _load(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _distance_m(lat1, lon1, lat2, lon2) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    a = (math.sin((p2 - p1) / 2) ** 2
         + math.cos(p1) * math.cos(p2) * math.sin(math.radians(lon2 - lon1) / 2) ** 2)
    return 2 * 6371008.8 * math.asin(min(1.0, math.sqrt(a)))


def current(config: Any, snapshot: Any) -> dict | None:
    """The snapshot when location is on and it belongs to this generation/device, else None.

    Returns ``{}`` when switched on but no usable sample of this generation exists yet.
    """
    if not isinstance(config, dict) or config.get("enabled") is not True:
        return None
    if (not isinstance(snapshot, dict) or snapshot.get("generation") != config.get("generation")
            or snapshot.get("device_id") != config.get("device_id")
            or snapshot.get("source_crs") != config.get("source_crs")
            or not isinstance(snapshot.get("sampled_at"), (int, float))):
        return {}
    return snapshot


def _when(ts: float, now: float) -> str:
    moment = dt.datetime.fromtimestamp(ts, SH)
    today = dt.datetime.fromtimestamp(now, SH).date()
    return moment.strftime("%H:%M" if moment.date() == today else "%m月%d日 %H:%M")


def format_location(snapshot: dict | None, now: float) -> str:
    if snapshot is None:
        return ""
    if not snapshot:
        return "[位置] 当前位置暂不可用。"
    lat, lon = float(snapshot["latitude"]), float(snapshot["longitude"])
    crs = CRS_LABEL.get(snapshot.get("source_crs"), str(snapshot.get("source_crs")))
    coords = f"{lat:.6f},{lon:.6f}（{crs}）"
    addr = snapshot.get("address") or {}
    near = None
    if addr.get("text") and addr.get("crs") == snapshot.get("source_crs"):
        if _distance_m(addr["lat"], addr["lon"], lat, lon) <= ADDRESS_REUSE_M:
            near = str(addr["text"])[:200]
    when = _when(float(snapshot["sampled_at"]), now)
    if now - float(snapshot["sampled_at"]) > FRESH_SECONDS:
        return f"[位置] 最后已知：{near or coords}；设备定位：{when}，已超过 5 分钟，不能据此判断现在仍在这里。"
    accuracy = float(snapshot.get("accuracy_m") or 0)
    head = f"附近：{near}" if near else "地址未更新"
    text = f"[位置] {head}；坐标：{coords}；设备定位：{when}；误差约{round(accuracy)}米。"
    if accuracy > GOOD_ACCURACY_M:
        text = text[:-1] + "，误差较大，不可靠。"
    return text


def pre_llm_call(*_args: Any, platform: Any = None, app_lane: Any = None, **_kwargs: Any) -> dict | None:
    if str(getattr(platform, "value", platform) or "").lower() != "tillyouwake_app" or app_lane != "text":
        return None
    import time
    text = format_location(current(_load(CONFIG), _load(LATEST)), time.time())
    return {"context": text} if text else None


def register(ctx: Any) -> None:
    ctx.register_hook("pre_llm_call", pre_llm_call)


__all__ = ["register", "pre_llm_call", "format_location", "current"]
