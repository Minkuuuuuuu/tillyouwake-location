"""在路上 on the App side: the [位置] line while she is on the road, and the 定位 page's switch / 【到了】."""
import importlib
import importlib.util
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
STAGE = ROOT / "integrations/app"
spec = importlib.util.spec_from_file_location("loc_plugin_travel", STAGE / "plugins/tillyouwake_location/__init__.py")
plugin = importlib.util.module_from_spec(spec)
spec.loader.exec_module(plugin)

NOW = 1_800_000_000.0
CFG = {"enabled": True, "generation": 5, "device_id": "d", "source_crs": "wgs84"}
TRAVEL = {"active": True, "mode": "auto", "since": NOW - 1500, "fast_streak": 2, "speed_kmh": 281.4, "heading": "东北",
          "speed_at": NOW - 40, "entered_city": "清远市", "entered_at": NOW - 300, "left_city": "广州市",
          "start_city": "广州市", "ended_at": None, "ended_by": None, "switch_at": 0}
SNAP = {"generation": 5, "device_id": "d", "source_crs": "wgs84", "latitude": 23.70, "longitude": 113.05,
        "accuracy_m": 650.0, "sampled_at": NOW - 40, "status": "ok",
        "address": {"text": "广东省清远市清城区某镇某村", "lat": 23.66, "lon": 113.04, "crs": "wgs84"},
        "battery": {"level": 0.72, "charging": False, "at": NOW - 60}, "travel": TRAVEL}


class TravelLine(unittest.TestCase):
    def test_on_the_road_her_four_things_in_order(self):
        line = plugin.format_location(plugin.current(CFG, SNAP), NOW)
        self.assertEqual(line, "[位置] 在路上；附近：广东省清远市清城区某镇某村；约 281 km/h 往东北；"
                               "刚过广州市（%s 进清远市）；设备定位：%s；误差约650米；电量 72%%。"
                         % (plugin._when(NOW - 300, NOW), plugin._when(NOW - 40, NOW)))
        self.assertNotIn("不可靠", line)  # a wide fix is normal on a train

    def test_address_kilometres_back_still_counts_but_not_further(self):
        far = dict(SNAP, address=dict(SNAP["address"], lat=23.60))  # ~11 km back
        self.assertIn("地址未更新", plugin.format_location(plugin.current(CFG, far), NOW))

    def test_manual_start_and_stopped_wording(self):
        snap = dict(SNAP, travel=dict(TRAVEL, mode="manual", speed_kmh=0.0, left_city=None, entered_city="广州市",
                                      start_city="广州市"))
        line = plugin.format_location(plugin.current(CFG, snap), NOW)
        self.assertTrue(line.startswith("[位置] 在路上（她自己点的）；附近：广东省清远市清城区某镇某村；暂时停着；从广州市出发；"))
        stale_speed = dict(SNAP, travel=dict(TRAVEL, speed_at=NOW - 400))
        self.assertNotIn("km/h", plugin.format_location(plugin.current(CFG, stale_speed), NOW))

    def test_off_the_road_is_the_ordinary_line(self):
        snap = dict(SNAP, travel=dict(TRAVEL, active=False), accuracy_m=20.0, latitude=23.66, longitude=113.04)
        line = plugin.format_location(plugin.current(CFG, snap), NOW)
        self.assertTrue(line.startswith("[位置] 附近：广东省清远市清城区某镇某村；坐标："))
        self.assertNotIn("在路上", line)
        # and the old 5-minute rule still wins over travel
        old = dict(SNAP, sampled_at=NOW - 400)
        self.assertTrue(plugin.format_location(plugin.current(CFG, old), NOW).startswith("[位置] 最后已知："))


class FakeSystemctl:
    def __init__(self):
        self.calls = []

    def __call__(self, *args):
        self.calls.append(args)
        class R:  # noqa: D401 - tiny stand-in
            returncode = 0
            stdout = "active\n" if args[0] == "is-active" else ""
        return R()


class TravelSwitch(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        d = Path(self.dir.name)
        pkg = d / "fakepkg_travel"
        pkg.mkdir()
        (pkg / "__init__.py").write_text("")
        (pkg / "model_settings.py").write_text(
            "class SettingsError(Exception):\n    def __init__(self, status, code):\n        super().__init__(code)\n"
            "        self.status, self.code = status, code\n")
        shutil.copy(STAGE / "plugins/tillyouwake_app/location_control.py", pkg / "location_control.py")
        shutil.copy(STAGE / "plugins/tillyouwake_app/location_relogin.py", pkg / "location_relogin.py")
        (d / "plugins" / "tillyouwake_location").mkdir(parents=True)
        shutil.copy(STAGE / "plugins/tillyouwake_location/__init__.py", d / "plugins/tillyouwake_location/__init__.py")
        sys.path.insert(0, str(d))
        importlib.invalidate_caches()
        for name in [n for n in sys.modules if n.startswith("fakepkg_travel")]:
            sys.modules.pop(name, None)
        self.lc = importlib.import_module("fakepkg_travel.location_control")
        lc = self.lc
        lc.CONFIG, lc.LATEST, lc.PLACES = d / "config.json", d / "latest.json", d / "places.json"
        lc.CREDENTIALS = d / "credentials.env"
        lc.LOGIN_OK = d / "login-ok.json"
        lc.CREDENTIALS.write_text("ICLOUD_APPLE_ID=a@b\n")
        lc.CONFIG.write_text(json.dumps({"enabled": True, "generation": 5, "device_id": "d", "source_crs": "wgs84",
                                         "enabled_since": 1.0}))
        lc.LATEST.write_text(json.dumps(SNAP))
        lc._systemctl = FakeSystemctl()
        lc._formatter = None
        lc._plugin = lambda: plugin

    def tearDown(self):
        sys.path.remove(str(Path(self.dir.name)))
        self.dir.cleanup()

    def cfg(self):
        return json.loads(self.lc.CONFIG.read_text())

    def test_switch_and_button_write_the_note_and_the_card_shows_it(self):
        lc = self.lc
        for bad in ({"travel": None}, {"travel": {}}, {"travel": {"action": "fly"}}, {"travel": {"action": "start", "x": 1}}):
            with self.assertRaises(lc.SettingsError) as e:
                lc.update(bad)
            self.assertEqual(e.exception.code, "invalid_travel_action")
        view = lc.snapshot()["travel"]
        self.assertEqual((view["active"], view["pending"], view["entered_city"], view["left_city"]),
                         (True, None, "清远市", "广州市"))
        self.assertNotIn("latitude", json.dumps(view))
        got = lc.update({"travel": {"action": "arrive"}})
        note = self.cfg()["travel_switch"]
        self.assertEqual(note["state"], "arrived")
        self.assertEqual(got["travel"]["pending"], "arrived")  # the collector has not consumed it yet
        self.assertEqual(self.cfg()["generation"], 5)  # never a new generation: the base must not reset
        # the collector consumed it: snapshot says so, the card stops pending
        snap = dict(SNAP, travel=dict(TRAVEL, active=False, ended_by="button", ended_at=NOW, switch_at=note["at"]))
        lc.LATEST.write_text(json.dumps(snap))
        view = lc.snapshot()["travel"]
        self.assertEqual((view["active"], view["pending"], view["ended_by"]), (False, None, "button"))
        got = lc.update({"travel": {"action": "start"}})
        self.assertEqual(self.cfg()["travel_switch"]["state"], "on")
        self.assertEqual(got["travel"]["pending"], "on")
        self.assertEqual(lc._systemctl.calls, [c for c in lc._systemctl.calls if c[0] == "is-active"])  # never touches the unit

    def test_needs_location_on(self):
        lc = self.lc
        lc.CONFIG.write_text(json.dumps({"enabled": False, "generation": 6, "device_id": "d", "source_crs": "wgs84"}))
        with self.assertRaises(lc.SettingsError) as e:
            lc.update({"travel": {"action": "start"}})
        self.assertEqual(e.exception.code, "travel_location_off")
        self.assertIsNone(lc.snapshot()["travel"])


if __name__ == "__main__":
    unittest.main()
