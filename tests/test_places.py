"""Named places: mark only from a fresh precise fix of this generation; names checked; overlap = no guess.
Offline: temp config/latest/places, staged plugin + control module."""
import json, os, shutil, sys, tempfile, time, unittest
from pathlib import Path

APP = Path(__file__).resolve().parents[1] / "integrations/app/plugins"
root = Path(tempfile.mkdtemp())
for d in ("plugins/tillyouwake_app", "plugins/tillyouwake_location"):
    (root / d).mkdir(parents=True)
(root / "plugins/__init__.py").write_text("")
(root / "plugins/tillyouwake_app/__init__.py").write_text("")
(root / "plugins/tillyouwake_app/model_settings.py").write_text(
    "class SettingsError(Exception):\n    def __init__(self, status, code):\n        super().__init__(code)\n"
    "        self.status, self.code = status, code\n")
shutil.copy(APP / "tillyouwake_app/location_control.py", root / "plugins/tillyouwake_app/location_control.py")
shutil.copy(APP / "tillyouwake_app/location_relogin.py", root / "plugins/tillyouwake_app/location_relogin.py")
shutil.copy(APP / "tillyouwake_location/__init__.py", root / "plugins/tillyouwake_location/__init__.py")
sys.path.insert(0, str(root))
from plugins.tillyouwake_app import location_control as lc  # noqa: E402
from plugins.tillyouwake_app.model_settings import SettingsError  # noqa: E402

LAT, LON = 23.1, 113.3
DLAT_1M = 1 / 111_195


class Places(unittest.TestCase):
    def setUp(self):
        self.d = Path(tempfile.mkdtemp())
        lc.CONFIG, lc.LATEST, lc.CREDENTIALS = self.d / "config.json", self.d / "latest.json", self.d / "credentials.env"
        lc.PLACES = self.d / "places.json"
        lc.CREDENTIALS.write_text('ICLOUD_APPLE_ID="a@example.invalid"\n')
        lc._active = lambda: "active"
        self.cfg = {"enabled": True, "generation": 9, "device_id": "DEV", "source_crs": "wgs84"}
        self.set_fix()

    def set_fix(self, cfg=None, **over):
        now = time.time()
        snap = {"generation": 9, "device_id": "DEV", "source_crs": "wgs84", "status": "ok", "error": None,
                "checked_at": now, "latitude": LAT, "longitude": LON, "accuracy_m": 8.0, "sampled_at": now - 30,
                "address": {"text": "测试路1号", "lat": LAT, "lon": LON, "crs": "wgs84"}}
        snap.update(over)
        lc.CONFIG.write_text(json.dumps(cfg or self.cfg)); lc.LATEST.write_text(json.dumps(snap))

    def act(self, **place):
        return lc.update({"place": place})

    def code(self, **place):
        with self.assertRaises(SettingsError) as cm:
            self.act(**place)
        return cm.exception.code

    def test_mark_saves_centre_privately_and_names_it_in_the_model_text(self):
        out = self.act(action="mark", name="宿舍")
        saved = json.loads(lc.PLACES.read_text())["places"][0]
        self.assertEqual((saved["name"], saved["crs"], saved["radius_m"]), ("宿舍", "wgs84", 30))
        self.assertEqual(oct(lc.PLACES.stat().st_mode & 0o777), "0o600")
        self.assertEqual(set(out["places"][0]), {"id", "name", "radius_m"})  # no centre to the page
        self.assertIn("常用地点：「宿舍」附近", out["preview"])
        self.assertIsNone(out["mark_blocker"])

    def test_mark_refuses_off_old_generation_stale_and_coarse(self):
        cases = [(dict(self.cfg, enabled=False, generation=10), {}, "place_location_off"),
                 (dict(self.cfg, generation=11), {}, "place_no_fix"),
                 (None, {"sampled_at": time.time() - 400}, "place_fix_stale"),
                 (None, {"accuracy_m": 35.0}, "place_fix_imprecise")]  # centre must be within 30 m
        for cfg, over, want in cases:
            self.set_fix(cfg, **over)
            self.assertEqual(self.code(action="mark", name="宿舍"), want)
            self.assertEqual(lc.snapshot()["mark_blocker"], want.removeprefix("place_"))
        self.assertFalse(lc.PLACES.exists())

    def test_names_are_plain_short_text(self):
        for bad in ("", "   ", "x" * 13, "<b>宿舍</b>", "「宿舍」", "a;b", "宿舍\x00"):
            self.assertEqual(self.code(action="mark", name=bad), "invalid_place_name", bad)
        self.act(action="mark", name=" 宿舍\n 3 栋 ")
        self.assertEqual(json.loads(lc.PLACES.read_text())["places"][0]["name"], "宿舍 3 栋")

    def test_duplicate_name_too_close_rename_delete(self):
        self.act(action="mark", name="宿舍")
        self.assertEqual(self.code(action="mark", name="宿舍"), "place_name_taken")
        self.assertEqual(self.code(action="mark", name="实验室"), "place_too_close")
        self.set_fix(latitude=LAT + 55 * DLAT_1M, address=None)  # circles of 30 m would overlap
        self.assertEqual(self.code(action="mark", name="实验室"), "place_too_close")
        self.set_fix(latitude=LAT + 65 * DLAT_1M, address=None)  # dorm-lab can be <150 m: 65 m must work
        out = self.act(action="mark", name="实验室")
        self.assertIn("「实验室」附近", out["preview"]); self.assertNotIn("宿舍", out["preview"])
        pid = out["places"][1]["id"]
        out = self.act(action="rename", id=pid, name="实验楼")
        self.assertEqual([p["name"] for p in out["places"]], ["宿舍", "实验楼"])
        self.assertEqual(self.code(action="rename", id=pid, name="宿舍"), "place_name_taken")
        out = self.act(action="delete", id=pid)
        self.assertEqual([p["name"] for p in out["places"]], ["宿舍"])
        self.assertEqual(self.code(action="delete", id=pid), "place_not_found")
        for bad in ({"action": "mark"}, {"action": "mark", "name": "a", "lat": 1}, {"action": "move", "id": pid}):
            self.assertEqual(self.code(**bad), "invalid_place_action")
        with self.assertRaises(SettingsError):
            lc.update({"place": {"action": "mark", "name": "a"}, "enabled": True})

    def test_overlap_and_coarse_fix_do_not_guess(self):
        lc.PLACES.write_text(json.dumps({"places": [
            {"id": "a" * 12, "name": "宿舍", "lat": LAT, "lon": LON, "crs": "wgs84", "radius_m": 30},
            {"id": "b" * 12, "name": "实验室", "lat": LAT + 20 * DLAT_1M, "lon": LON, "crs": "wgs84", "radius_m": 30}]}))
        self.assertIn("「宿舍」「实验室」都在附近，分不清是哪个", lc.snapshot()["preview"])
        self.set_fix(accuracy_m=31.0)  # error larger than the 30 m circle: never named
        self.assertNotIn("常用地点", lc.snapshot()["preview"])
        self.set_fix(latitude=LAT - 31 * DLAT_1M)  # just outside the dorm circle
        self.assertNotIn("常用地点", lc.snapshot()["preview"])
        self.set_fix(source_crs="gcj02", cfg=dict(self.cfg, source_crs="gcj02"))
        self.assertNotIn("常用地点", lc.snapshot()["preview"])  # other coordinate system never matches

    def test_saved_100m_place_still_blocks_its_own_circle(self):
        lc.PLACES.write_text(json.dumps({"places": [
            {"id": "a" * 12, "name": "旧点", "lat": LAT, "lon": LON, "crs": "wgs84", "radius_m": 100}]}))
        self.set_fix(latitude=LAT + 120 * DLAT_1M)  # 100 + 30 m circles overlap
        self.assertEqual(self.code(action="mark", name="实验室"), "place_too_close")

    def test_stale_fix_gets_no_place_and_off_shows_names_only(self):
        self.act(action="mark", name="宿舍")
        self.set_fix(sampled_at=time.time() - 900)
        self.assertNotIn("宿舍", lc.snapshot()["preview"])
        self.set_fix(dict(self.cfg, enabled=False, generation=10))
        out = lc.snapshot()
        self.assertIsNone(out["preview"]); self.assertEqual(out["places"][0]["name"], "宿舍")


if __name__ == "__main__":
    unittest.main()
