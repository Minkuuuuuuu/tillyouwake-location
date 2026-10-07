import importlib
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
STAGE = ROOT / "integrations/app"


def load_control(tmp):
    pkg = Path(tmp) / "fakepkg"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("")
    (pkg / "model_settings.py").write_text(
        "class SettingsError(Exception):\n    def __init__(self, status, code):\n"
        "        super().__init__(code)\n        self.status, self.code = status, code\n")
    shutil.copy(STAGE / "plugins/tillyouwake_app/location_control.py", pkg / "location_control.py")
    shutil.copy(STAGE / "plugins/tillyouwake_app/location_relogin.py", pkg / "location_relogin.py")
    sys.path.insert(0, tmp)
    for name in ("fakepkg", "fakepkg.model_settings", "fakepkg.location_control", "fakepkg.location_relogin"):
        sys.modules.pop(name, None)
    return importlib.import_module("fakepkg.location_control")


class FakeSystemctl:
    def __init__(self, fail=()):
        self.calls, self.fail, self.state = [], set(fail), "inactive"

    def __call__(self, *args):
        self.calls.append(args)
        verb = args[0]
        rc = 1 if verb in self.fail else 0
        if verb == "start" and not rc:
            self.state = "active"
        if verb == "stop" and not rc:
            self.state = "inactive"
        out = self.state if verb == "is-active" else ""
        return subprocess.CompletedProcess(["systemctl", *args], rc, out, "")


class Tests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.lc = load_control(self.tmp)
        d = Path(self.tmp)
        self.lc.CONFIG, self.lc.CREDENTIALS, self.lc.LATEST = d / "config.json", d / "credentials.env", d / "latest.json"
        self.lc.CONFIG.write_text(json.dumps({"enabled": False, "generation": 0, "enabled_since": None,
                                              "device_id": "dev1", "icloud_region": "cn", "source_crs": "wgs84"}))
        self.lc.CREDENTIALS.write_text("ICLOUD_APPLE_ID=someone\nAMAP_KEY=\n")
        self.sys = FakeSystemctl()
        self.lc._systemctl = self.sys

    def tearDown(self):
        sys.path.remove(self.tmp)
        shutil.rmtree(self.tmp)

    def cfg(self):
        return json.loads(self.lc.CONFIG.read_text())

    def test_location_toggle_lifecycle(self):
        lc = self.lc
        for bad in (None, [], {}, {"enabled": "true"}, {"enabled": 1}, {"enabled": True, "unit": "x"},
                    {"enabled": True, "cmd": "rm"}):
            with self.assertRaises(lc.SettingsError) as e:
                lc.update(bad)
            self.assertEqual(e.exception.code, "invalid_location_setting")
        self.assertEqual(self.sys.calls, [])
        got = lc.update({"enabled": True})
        self.assertEqual(self.cfg()["generation"], 1)
        self.assertTrue(self.cfg()["enabled"] and self.cfg()["enabled_since"])
        self.assertIn(("start", "--no-block", "tillyouwake-location.service"), self.sys.calls)
        self.assertEqual((got["enabled"], got["running"], got["status"]), (True, True, "waiting_fresh"))
        lc.update({"enabled": True})  # same value: no new generation
        self.assertEqual(self.cfg()["generation"], 1)
        lc.LATEST.write_text(json.dumps({"generation": 1, "status": "ok", "error": None, "latitude": 1}))
        snap = lc.snapshot()
        self.assertEqual(snap["status"], "ok")
        self.assertNotIn("latitude", json.dumps(snap))
        # off: config first, then the unit
        order = []
        real_write = lc._write
        lc._write = lambda c: (order.append(("write", c["enabled"])), real_write(c))
        self.sys_calls_before = len(self.sys.calls)
        lc._systemctl = lambda *a: (order.append(a[0]), self.sys(*a))[1]
        got = lc.update({"enabled": False})
        self.assertEqual(order[:2], [("write", False), "stop"])
        self.assertEqual((self.cfg()["enabled"], self.cfg()["generation"]), (False, 2))
        self.assertEqual((got["enabled"], got["running"], got["status"]), (False, False, "off"))
        lc.update({"enabled": False})
        self.assertEqual(self.cfg()["generation"], 2)

    def test_failures_report_real_state(self):
        lc = self.lc
        lc._systemctl = FakeSystemctl(fail={"start"})
        with self.assertRaises(lc.SettingsError) as e:
            lc.update({"enabled": True})
        self.assertEqual(e.exception.code, "location_start_failed")
        self.assertFalse(self.cfg()["enabled"])
        lc._systemctl = FakeSystemctl()
        lc.update({"enabled": True})
        lc._systemctl.fail = {"stop"}
        got = lc.update({"enabled": False})
        self.assertEqual((got["enabled"], got["status"]), (False, "stop_failed"))
        self.assertFalse(self.cfg()["enabled"])  # consumers already refuse location
        # unconfigured cannot be switched on
        self.lc.CREDENTIALS.write_text("ICLOUD_APPLE_ID=\n")
        with self.assertRaises(lc.SettingsError) as e:
            lc.update({"enabled": True})
        self.assertEqual(e.exception.code, "location_not_configured")
        self.assertEqual(lc.snapshot()["status"], "stopping")  # the failed stop is still visible
        lc._systemctl = FakeSystemctl()
        self.assertEqual(lc.snapshot()["status"], "needs_setup")
        lc.CONFIG.write_text("{broken")
        self.assertEqual(lc.snapshot()["status"], "unavailable")


if __name__ == "__main__":
    unittest.main()
