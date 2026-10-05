"""Non-interactive start (probe/run): saved token only, one accountLogin, never password/2FA."""
import json, os, socket, sys, tempfile, types, unittest
from pathlib import Path
import collector as c


def fake_pyicloud(rec, *, tokens=True, token_error=None):
    class Session:
        def __init__(self):
            self.data = {"session_token": "s", "trust_token": "t"} if tokens else {"session_token": "s"}
            self.trust_env = True
        def request(self, method, url, **kw):
            return None

    class Device:
        location = {"latitude": 23.1, "longitude": 113.3, "horizontalAccuracy": 20, "timeStamp": 1}

    class Devices:
        devices = {"DEV-1": Device()}
        def refresh(self, locate=True):
            rec["refresh"] += 1

    class Service:
        def __init__(self, apple_id, password, **kw):
            rec["init"] = {"password": password, **kw}
            self.session = Session()
            self.devices = Devices()
            self.requires_2fa = self.requires_2sa = False
        def authenticate(self, *a, **k):
            rec["authenticate"] += 1
        def _authenticate_with_token(self, require_trust=True):
            rec["token"] += 1
            if token_error is not None:
                raise token_error
    m = types.ModuleType("pyicloud"); m.PyiCloudService = Service
    return m


class Startup(unittest.TestCase):
    def setUp(self):
        self.rec = {"init": None, "authenticate": 0, "token": 0, "refresh": 0}
        self.saved_mod, self.saved_gai = sys.modules.get("pyicloud"), socket.getaddrinfo
        self.tmp = tempfile.TemporaryDirectory(); c.STATE_DIR = Path(self.tmp.name)
        os.environ["ICLOUD_APPLE_ID"] = "someone@example.invalid"
        os.environ["ICLOUD_PASSWORD"] = "must-not-be-used"

    def tearDown(self):
        socket.getaddrinfo = self.saved_gai
        if self.saved_mod is None: sys.modules.pop("pyicloud", None)
        else: sys.modules["pyicloud"] = self.saved_mod
        for k in ("ICLOUD_APPLE_ID", "ICLOUD_PASSWORD"): os.environ.pop(k, None)
        self.tmp.cleanup()

    def test_service_is_token_only_and_applies_saved_edge(self):
        sys.modules["pyicloud"] = fake_pyicloud(self.rec)
        api = c._service({"icloud_region": "cn", "icloud_edge": "global"})
        self.assertEqual((self.rec["token"], self.rec["authenticate"]), (1, 0))
        self.assertIsNone(self.rec["init"]["password"])  # env password ignored
        self.assertIs(self.rec["init"]["authenticate"], False)
        self.assertIs(api.session.trust_env, False)
        self.assertTrue(getattr(socket.getaddrinfo, "_tillyou_global_edge", False))
        with self.assertRaises(c.ResumeLoginRefused):
            api.authenticate(force_refresh=True)  # Find My 450 re-auth is refused, not performed
        self.assertEqual(self.rec["authenticate"], 0)

    def test_probe_uses_the_same_path(self):
        sys.modules["pyicloud"] = fake_pyicloud(self.rec)
        self.assertEqual(c.probe({"icloud_region": "cn", "icloud_edge": "global", "device_id": "DEV-1"}), 0)
        self.assertEqual((self.rec["token"], self.rec["authenticate"], self.rec["refresh"]), (1, 0, 1))
        self.assertTrue(getattr(socket.getaddrinfo, "_tillyou_global_edge", False))

    def test_no_edge_without_config(self):
        sys.modules["pyicloud"] = fake_pyicloud(self.rec)
        c._service({"icloud_region": "cn"})
        self.assertFalse(getattr(socket.getaddrinfo, "_tillyou_global_edge", False))

    def test_missing_trust_or_failed_token_stops_run_without_fallback(self):
        from pyicloud.exceptions import PyiCloudFailedLoginException
        cfg = Path(self.tmp.name) / "config.json"
        cfg.write_text(json.dumps({"enabled": True, "generation": 2, "device_id": "DEV-1",
                                   "source_crs": "wgs84", "icloud_region": "cn", "icloud_edge": "global"}))
        for kwargs in ({"tokens": False}, {"token_error": PyiCloudFailedLoginException("Invalid authentication token.")}):
            self.rec.update(authenticate=0, token=0, refresh=0)
            real = sys.modules.get("pyicloud")
            sys.modules["pyicloud"] = fake_pyicloud(self.rec, **kwargs)
            self.assertEqual(c.run(cfg), 0)
            snap = json.loads((c.STATE_DIR / "latest.json").read_text())
            self.assertEqual((snap["status"], snap["error"]), ("needs_login", "login_failed:auth"))
            self.assertEqual((self.rec["authenticate"], self.rec["refresh"]), (0, 0))
            self.assertEqual(self.rec["token"], 0 if kwargs.get("tokens") is False else 1)


if __name__ == "__main__":
    unittest.main()
