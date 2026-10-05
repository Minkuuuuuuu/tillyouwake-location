import importlib.util, json, os, sys, tempfile, unittest
from pathlib import Path
from unittest import mock

spec = importlib.util.spec_from_file_location("setup_login", Path(__file__).resolve().parents[1] / "setup-login.py")
sl = importlib.util.module_from_spec(spec)
spec.loader.exec_module(sl)


class SetupResume(unittest.TestCase):
    def run_flow(self, collector_exit):
        d = Path(tempfile.mkdtemp())
        (d / "config.json").write_text(json.dumps({"enabled": False, "generation": 0, "device_id": None,
                                                   "icloud_region": "cn", "source_crs": None}))
        calls, keys = [], []
        answers = iter(["someone@example.invalid", "DEVICE-ID-123456"])
        with mock.patch.object(sl, "DIRECTORY", d), mock.patch.object(sl, "CONFIG", d / "config.json"), \
                mock.patch.object(sl.os, "geteuid", return_value=0), \
                mock.patch.object(sl.sys.stdin, "isatty", return_value=True), \
                mock.patch("builtins.input", lambda *_: next(answers)), \
                mock.patch.object(sl.getpass, "getpass", lambda *_: keys.append(1) or "0123456789abcdef" * 2), \
                mock.patch.object(sl.subprocess, "call", lambda argv, env: calls.append((argv, env)) or collector_exit), \
                mock.patch.object(sl, "atomic_write", lambda p, c, m, u, g: Path(p).write_text(c)):
            try:
                sl.main(resume=True)
                exited = None
            except SystemExit as e:
                exited = str(e)
        self.keys_asked = len(keys)
        return d, calls, exited

    def test_failure_saves_nothing(self):
        d, calls, exited = self.run_flow(4)
        self.assertIn("没有保存", exited)
        self.assertEqual(calls[0][0][-2:], ["--resume", "--global-edge"])
        self.assertNotIn("ICLOUD_PASSWORD", calls[0][1])
        self.assertFalse((d / "credentials.env").exists())
        self.assertEqual(self.keys_asked, 0)  # resume never asks for the Key before Apple answers
        cfg = json.loads((d / "config.json").read_text())
        self.assertEqual((cfg["enabled"], cfg["device_id"], cfg.get("icloud_edge")), (False, None, None))

    def test_success_saves_device_and_route_but_stays_off(self):
        d, calls, exited = self.run_flow(0)
        self.assertIsNone(exited)
        cfg = json.loads((d / "config.json").read_text())
        self.assertEqual((cfg["enabled"], cfg["device_id"], cfg["icloud_edge"]), (False, "DEVICE-ID-123456", "global"))
        self.assertTrue((d / "credentials.env").exists())
        self.assertEqual(self.keys_asked, 1)


if __name__ == "__main__":
    unittest.main()
