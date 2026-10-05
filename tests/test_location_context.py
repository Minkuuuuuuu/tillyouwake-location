import importlib.util
import json
import sys
import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
STAGE = ROOT / "integrations/app"
spec = importlib.util.spec_from_file_location("loc_plugin", STAGE / "plugins/tillyouwake_location/__init__.py")
plugin = importlib.util.module_from_spec(spec)
spec.loader.exec_module(plugin)

NOW = 1_800_000_000.0
CFG = {"enabled": True, "generation": 5, "device_id": "d", "source_crs": "wgs84"}
SNAP = {"generation": 5, "device_id": "d", "source_crs": "wgs84", "latitude": 23.123456, "longitude": 113.3,
        "accuracy_m": 18.4, "sampled_at": NOW - 40, "status": "ok",
        "address": {"text": "广州市天河区某路", "lat": 23.1235, "lon": 113.3, "crs": "wgs84"}}


class Tests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        d = Path(self.dir.name)
        plugin.CONFIG, plugin.LATEST = d / "config.json", d / "latest.json"
        plugin.CONFIG.write_text(json.dumps(CFG))
        plugin.LATEST.write_text(json.dumps(SNAP))

    def tearDown(self):
        self.dir.cleanup()

    @patch("time.time", return_value=NOW)
    def test_location_text_lane_only(self, _clock):
        got = plugin.pre_llm_call(platform="tillyouwake_app", app_lane="text", session_id="s")
        self.assertTrue(got["context"].startswith("[位置] 附近：广州市天河区某路；坐标：23.123456,113.300000（WGS-84）"))
        for lane in ("phone", None, "", "TEXT"):
            self.assertIsNone(plugin.pre_llm_call(platform="tillyouwake_app", app_lane=lane))
        for plat in ("telegram", "qqbot", ""):
            self.assertIsNone(plugin.pre_llm_call(platform=plat, app_lane="text"))

    def test_location_is_dynamic_and_fail_open(self):
        f = plugin.format_location
        cur = plugin.current
        self.assertEqual(f(cur(CFG, SNAP), NOW).count("[位置]"), 1)
        self.assertIn("误差约18米。", f(cur(CFG, SNAP), NOW))
        # stale: last known, never presented as now
        stale = f(cur(CFG, dict(SNAP, sampled_at=NOW - 301)), NOW)
        self.assertTrue(stale.startswith("[位置] 最后已知：广州市天河区某路；"))
        self.assertIn("已超过 5 分钟，不能据此判断现在仍在这里。", stale)
        # far from the cached address point: coordinates, address not updated
        moved = f(cur(CFG, dict(SNAP, latitude=23.13)), NOW)
        self.assertTrue(moved.startswith("[位置] 地址未更新；坐标：23.130000,113.300000"))
        self.assertIn("误差较大，不可靠", f(cur(CFG, dict(SNAP, accuracy_m=150)), NOW))
        # switched on, nothing of this generation yet
        for snap in (None, {}, dict(SNAP, generation=4), dict(SNAP, device_id="x"), {"generation": 5}):
            self.assertEqual(f(cur(CFG, snap), NOW), "[位置] 当前位置暂不可用。")
        # switched off / broken config: nothing at all, not even a pause note
        for cfg in (None, dict(CFG, enabled=False), "x", {}):
            self.assertEqual(f(cur(cfg, SNAP), NOW), "")
        plugin.CONFIG.write_text("{broken")
        self.assertIsNone(plugin.pre_llm_call(platform="tillyouwake_app", app_lane="text"))
        plugin.CONFIG.write_text(json.dumps(CFG))
        plugin.LATEST.write_text("{broken")
        self.assertEqual(plugin.pre_llm_call(platform="tillyouwake_app", app_lane="text")["context"],
                         "[位置] 当前位置暂不可用。")

    def test_turn_context_passes_lane(self):
        src = (ROOT / "integrations/app-voice.patch").read_text()
        self.assertEqual(src.count('app_lane=getattr(agent, "_app_lane", None),'), 1)


if __name__ == "__main__":
    unittest.main()
