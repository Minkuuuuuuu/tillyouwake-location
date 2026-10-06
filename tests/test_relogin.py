"""Re-login keeping settings: fresh password sign-in, device check, no config/credential writes."""
import contextlib, importlib.util, io, json, os, subprocess, sys, tempfile, types, unittest
from pathlib import Path
from unittest import mock
import collector as c
REAL_SERVICE = c._service  # test_location swaps it out without restoring

spec = importlib.util.spec_from_file_location("setup_login", Path(__file__).resolve().parents[1] / "setup-login.py")
sl = importlib.util.module_from_spec(spec)
spec.loader.exec_module(sl)


def fake_pyicloud(rec, devices):
    class Session:
        data = {}
        def request(self, *a, **k):
            return None

    class Service:
        def __init__(self, apple_id, password, **kw):
            rec["password"] = password
            self.session, self.requires_2fa = Session(), False
            self.devices = types.SimpleNamespace(devices={d: types.SimpleNamespace(name="n", model_name="m")
                                                          for d in devices})
        def authenticate(self, force_refresh=False, **k):
            rec["force_refresh"] = force_refresh
    m = types.ModuleType("pyicloud"); m.PyiCloudService = Service
    return m


class CollectorFreshLogin(unittest.TestCase):
    def setUp(self):
        self.saved = sys.modules.get("pyicloud")
        c._service = REAL_SERVICE
        self.tmp = tempfile.TemporaryDirectory(); c.STATE_DIR = Path(self.tmp.name)
        os.environ.update(ICLOUD_APPLE_ID="someone@example.invalid", ICLOUD_PASSWORD="typed-by-her")

    def tearDown(self):
        if self.saved is None: sys.modules.pop("pyicloud", None)
        else: sys.modules["pyicloud"] = self.saved
        for k in ("ICLOUD_APPLE_ID", "ICLOUD_PASSWORD"): os.environ.pop(k, None)
        self.tmp.cleanup()

    def login(self, devices):
        rec = {}
        sys.modules["pyicloud"] = fake_pyicloud(rec, devices)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = c.login({"icloud_region": "cn", "device_id": "DEV-1"}, fresh=True)
        return code, rec, out.getvalue()

    def test_fresh_signin_confirms_configured_device_without_printing_ids(self):
        code, rec, out = self.login(["DEV-1", "DEV-2"])
        self.assertEqual((code, rec["force_refresh"], rec["password"]), (0, True, "typed-by-her"))
        self.assertNotIn("DEV-", out)

    def test_missing_device_is_reported(self):
        code, rec, out = self.login(["DEV-2"])
        self.assertEqual(code, 8)
        self.assertNotIn("DEV-", out)

    def test_initial_login_is_unchanged(self):
        rec = {}
        sys.modules["pyicloud"] = fake_pyicloud(rec, ["DEV-1"])
        with contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(c.login({"icloud_region": "cn"}), 0)
        self.assertIs(rec["force_refresh"], False)
        self.assertIn("DEV-1", out.getvalue())


class SetupRelogin(unittest.TestCase):
    def run_relogin(self, active=False, child=0, timeout=False):
        d = Path(tempfile.mkdtemp())
        cfg = {"enabled": True, "generation": 7, "device_id": "DEV-1", "icloud_region": "cn",
               "source_crs": "wgs84", "icloud_edge": "global"}
        (d / "config.json").write_text(json.dumps(cfg))
        (d / "credentials.env").write_text(sl.credential_text("someone@example.invalid", "0123456789abcdef" * 2))
        before = {p.name: p.read_bytes() for p in d.iterdir()}
        calls = []

        def call(argv, env=None, timeout=None):
            calls.append((argv, env, timeout))
            if argv[0] == "systemctl":
                return 0 if active else 3
            if timeout_flag:
                raise subprocess.TimeoutExpired(argv, timeout)
            return child
        timeout_flag = timeout
        with mock.patch.object(sl, "DIRECTORY", d), mock.patch.object(sl, "CONFIG", d / "config.json"), \
                mock.patch.object(sl.os, "geteuid", return_value=0), \
                mock.patch.object(sl.sys.stdin, "isatty", return_value=True), \
                mock.patch.object(sl.subprocess, "call", call), \
                mock.patch("builtins.input", side_effect=AssertionError("no questions")), \
                contextlib.redirect_stdout(io.StringIO()):
            try:
                sl.relogin(); exited = None
            except SystemExit as e:
                exited = str(e)
        after = {p.name: p.read_bytes() for p in d.iterdir()}
        self.assertEqual(before, after)  # device, Key, crs, route, switch untouched in every outcome
        return calls, exited

    def test_success_runs_fresh_login_with_saved_account_only(self):
        calls, exited = self.run_relogin()
        self.assertIsNone(exited)
        argv, env, timeout = calls[1]
        self.assertEqual(argv[-2:], ["--login", "--fresh"])
        self.assertEqual(env["ICLOUD_APPLE_ID"], "someone@example.invalid")
        self.assertNotIn("ICLOUD_PASSWORD", env)
        self.assertEqual(timeout, 600)

    def test_running_collector_is_left_alone(self):
        calls, exited = self.run_relogin(active=True)
        self.assertIn("不需要重新登录", exited)
        self.assertEqual(len(calls), 1)

    def test_failure_and_timeout_stop_once(self):
        for kw in ({"child": 4}, {"timeout": True}):
            calls, exited = self.run_relogin(**kw)
            self.assertIn("设置都没改", exited)
            self.assertEqual(len(calls), 2)


if __name__ == "__main__":
    unittest.main()
