"""Settings preview == the plugin's model text; off/old-generation never shows a point. Offline, temp files only."""
import json, shutil, sys, tempfile, time, unittest
from pathlib import Path

APP = Path(__file__).resolve().parents[1] / "integrations/app/plugins"
root = Path(tempfile.mkdtemp())
(root / "plugins/tillyouwake_app").mkdir(parents=True)
(root / "plugins/tillyouwake_location").mkdir(parents=True)
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
import importlib.util  # noqa: E402
spec = importlib.util.spec_from_file_location("plug", root / "plugins/tillyouwake_location/__init__.py")
plug = importlib.util.module_from_spec(spec); spec.loader.exec_module(plug)


class Preview(unittest.TestCase):
    def setUp(self):
        self.d = Path(tempfile.mkdtemp())
        lc.CONFIG, lc.LATEST, lc.CREDENTIALS = self.d / "config.json", self.d / "latest.json", self.d / "credentials.env"
        lc.PLACES = self.d / "places.json"
        lc.CREDENTIALS.write_text('ICLOUD_APPLE_ID="secret-account@example.invalid"\nAMAP_KEY="SECRETKEY"\n')
        lc._active = lambda: "active"
        self.cfg = {"enabled": True, "generation": 9, "device_id": "SECRET-DEVICE", "source_crs": "wgs84"}
        now = time.time()
        self.snap = {"generation": 9, "device_id": "SECRET-DEVICE", "source_crs": "wgs84", "status": "ok",
                     "error": None, "checked_at": now, "latitude": 23.1, "longitude": 113.3, "accuracy_m": 7.0,
                     "sampled_at": now - 60, "address": {"text": "测试路1号", "lat": 23.1, "lon": 113.3, "crs": "wgs84"}}

    def write(self, cfg, snap):
        lc.CONFIG.write_text(json.dumps(cfg)); lc.LATEST.write_text(json.dumps(snap))
        return lc.snapshot()

    def test_on_shows_exactly_the_model_text_and_no_secrets(self):
        out = self.write(self.cfg, self.snap)
        self.assertEqual(out["preview"], plug.format_location(plug.current(self.cfg, self.snap), time.time()))
        self.assertIn("附近：测试路1号", out["preview"]); self.assertIn("WGS-84", out["preview"]); self.assertIn("误差约7米", out["preview"])
        blob = json.dumps(out, ensure_ascii=False)
        for secret in ("SECRET-DEVICE", "secret-account", "SECRETKEY"):
            self.assertNotIn(secret, blob)

    def test_off_never_shows_the_last_point(self):
        out = self.write(dict(self.cfg, enabled=False, generation=10), self.snap)
        self.assertIsNone(out["preview"])

    def test_old_generation_is_not_presented_as_current(self):
        out = self.write(dict(self.cfg, generation=11), self.snap)
        self.assertEqual(out["preview"], "[位置] 当前位置暂不可用。")

    def test_stale_and_low_accuracy_are_labelled(self):
        out = self.write(self.cfg, dict(self.snap, sampled_at=time.time() - 900))
        self.assertIn("已超过 5 分钟", out["preview"])
        out = self.write(self.cfg, dict(self.snap, accuracy_m=250.0))
        self.assertIn("不可靠", out["preview"])

    def test_formatter_failure_keeps_the_page(self):
        lc._formatter = type("Broken", (), {"current": staticmethod(lambda *a: 1 / 0)})
        try:
            out = self.write(self.cfg, self.snap)
        finally:
            lc._formatter = None
        self.assertIsNone(out["preview"]); self.assertEqual(out["status"], "ok")


if __name__ == "__main__":
    unittest.main()
