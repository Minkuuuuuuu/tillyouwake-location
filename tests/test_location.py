import json
import math
import os
import sys
import tempfile
import unittest
from pathlib import Path

import collector as c

NOW = 1_800_000_000.0
CFG = {"enabled": True, "generation": 3, "enabled_since": NOW - 600, "device_id": "dev1", "source_crs": "wgs84"}


def raw(lat=23.1, lon=113.3, acc=20.0, ts=NOW - 30, ms=True):
    return {"latitude": lat, "longitude": lon, "horizontalAccuracy": acc, "timeStamp": ts * 1000 if ms else ts}


class Geo:
    def __init__(self, fail=False):
        self.calls, self.fail = 0, fail

    def __call__(self, lat, lon, crs):
        self.calls += 1
        if self.fail:
            raise RuntimeError("quota")
        return "广州市天河区某路"


class Tests(unittest.TestCase):
    def test_snapshot_quality_and_address_cache(self):
        geo = Geo()
        s = c.poll_once(CFG, None, NOW, lambda: raw(), geo)
        self.assertEqual((s["status"], s["sampled_at"], s["generation"]), ("ok", NOW - 30, 3))
        self.assertEqual(s["address"]["text"], "广州市天河区某路")
        self.assertEqual(geo.calls, 1)
        # invalid fixes never refresh the valid sample
        for bad in (raw(lat=float("nan")), raw(lat=91), raw(acc=-1), {"latitude": 1},
                    raw(ts=NOW + 41, ms=False), None):
            t = c.poll_once(CFG, s, NOW + 10, lambda b=bad: b, geo)
            self.assertEqual(t["sampled_at"], NOW - 30)
            self.assertEqual(t["error"], "no_valid_fix")
        # an older sample does not look newer
        t = c.poll_once(CFG, s, NOW + 10, lambda: raw(ts=NOW - 100), geo)
        self.assertEqual(t["sampled_at"], NOW - 30)
        # 300 s fresh / 301 s stale
        self.assertEqual(c.poll_once(CFG, s, NOW - 30 + 300, lambda: None, geo)["status"], "ok")
        self.assertEqual(c.poll_once(CFG, s, NOW - 30 + 301, lambda: None, geo)["status"], "stale")
        # within 100 m: address reused, no AMap call even after the 60 s gap
        near = c.poll_once(CFG, s, NOW + 120, lambda: raw(lat=23.1005, ts=NOW + 100), geo)
        self.assertEqual(geo.calls, 1)
        # >100 m: new lookup; 101 m accuracy: no lookup
        far = c.poll_once(CFG, near, NOW + 240, lambda: raw(lat=23.102, ts=NOW + 230), geo)
        self.assertEqual(geo.calls, 2)
        c.poll_once(CFG, far, NOW + 400, lambda: raw(lat=23.11, acc=101, ts=NOW + 390), geo)
        self.assertEqual(geo.calls, 2)
        c.poll_once(CFG, far, NOW + 400, lambda: raw(lat=23.11, acc=100, ts=NOW + 390), geo)
        self.assertEqual(geo.calls, 3)
        # another device never inherits the old address or fix
        other = c.poll_once(dict(CFG, device_id="dev2"), far, NOW + 500, lambda: None, geo)
        self.assertNotIn("address", other)
        self.assertNotIn("sampled_at", other)
        self.assertEqual(other["status"], "waiting_fresh")

    def test_failures_keep_coordinates_and_generation_gate(self):
        geo = Geo(fail=True)
        s = c.poll_once(CFG, None, NOW, lambda: raw(), geo)
        self.assertEqual(s["status"], "ok")
        self.assertTrue(s["address"]["error"].startswith("geocode_failed"))
        self.assertEqual(s["address"]["retry_at"], NOW + 300)
        c.poll_once(CFG, s, NOW + 100, lambda: raw(lat=23.2, ts=NOW + 90), geo)
        self.assertEqual(geo.calls, 1)  # waits out the 300 s retry
        def boom():
            raise ConnectionError("offline")
        t = c.poll_once(CFG, s, NOW + 50, boom, None)
        self.assertEqual((t["sampled_at"], t["error"]), (NOW - 30, "fetch_failed:network:ConnectionError"))
        # a new generation never exposes the previous fix, nor accepts pre-enable samples
        cfg2 = dict(CFG, generation=4, enabled_since=NOW + 1000)
        u = c.poll_once(cfg2, s, NOW + 1010, lambda: raw(ts=NOW + 900), None)
        self.assertNotIn("sampled_at", u)
        self.assertEqual((u["status"], u["error"]), ("waiting_fresh", "older_than_enable"))
        v = c.poll_once(cfg2, s, NOW + 1010, lambda: raw(ts=NOW + 980), None)  # 30 s clock slack
        self.assertEqual(v["sampled_at"], NOW + 980)

    def test_disabled_exits_without_requests_and_atomic_write(self):
        with tempfile.TemporaryDirectory() as d:
            cfg = Path(d) / "config.json"
            for content in (None, "{broken", json.dumps(dict(CFG, enabled=False)),
                            json.dumps(dict(CFG, source_crs=None))):
                if content is None:
                    cfg.unlink(missing_ok=True)
                else:
                    cfg.write_text(content)
                c.STATE_DIR = Path(d) / "state"
                called = []
                c._service = lambda *a, **k: called.append(1)
                self.assertEqual(c.run(cfg), 0)
                self.assertEqual(called, [])
            out = Path(d) / "x" / "latest.json"
            c.write_snapshot(out, {"a": 1})
            self.assertEqual(json.loads(out.read_text()), {"a": 1})
            self.assertEqual(oct(out.stat().st_mode & 0o777), "0o640")
            self.assertEqual([p.name for p in out.parent.iterdir()], ["latest.json"])

    def test_late_result_never_crosses_a_toggle(self):
        with tempfile.TemporaryDirectory() as d:
            cfg = Path(d) / "config.json"
            cfg.write_text(json.dumps(CFG))
            c.STATE_DIR = Path(d) / "state"

            class Dev:
                @property
                def location(self):
                    cfg.write_text(json.dumps(dict(CFG, generation=4)))  # toggled mid-request
                    return raw()

            class Devices:
                devices = {"dev1": Dev()}
                def refresh(self, locate=True):
                    pass

            class Api:
                requires_2fa = requires_2sa = False
                devices = Devices()

            c._service = lambda *a, **k: Api()
            self.assertEqual(c.run(cfg), 0)
            self.assertFalse((c.STATE_DIR / "latest.json").exists())

    def test_auth_failure_stops_instead_of_retrying(self):
        from pyicloud.exceptions import PyiCloudFailedLoginException
        def fail():
            raise PyiCloudFailedLoginException("Invalid email/password combination.")
        s = c.poll_once(CFG, None, NOW, fail, None)
        self.assertTrue(c.auth_lost(s))
        self.assertFalse(c.auth_lost(c.poll_once(CFG, None, NOW, lambda: (_ for _ in ()).throw(TimeoutError()), None)))

    def _chain(self):
        """RemoteDisconnected -> requests ConnectionError -> pyicloud API error -> FailedLogin,
        the exact shape of the 2026-10-03 trust_session failure."""
        import http.client
        import requests
        from pyicloud.exceptions import PyiCloudAPIResponseException, PyiCloudFailedLoginException
        try:
            try:
                try:
                    raise http.client.RemoteDisconnected("Remote end closed connection without response")
                except Exception as e0:
                    raise requests.exceptions.ConnectionError(e0) from e0
            except Exception as e1:
                raise PyiCloudAPIResponseException("Request failed to iCloud") from e1
        except Exception as e2:
            try:
                raise PyiCloudFailedLoginException("Invalid authentication token.", e2) from e2
            except Exception as e3:
                return e3

    def test_errors_are_classified(self):
        from pyicloud.exceptions import PyiCloudAPIResponseException, PyiCloudFailedLoginException
        self.assertEqual(c.classify_error(self._chain()), "network")
        try:
            try:
                raise PyiCloudAPIResponseException("", 401)
            except Exception as e:
                raise PyiCloudFailedLoginException("Invalid email/password combination.") from e
        except Exception as e:
            self.assertEqual(c.classify_error(e), "auth")
        self.assertEqual(c.classify_error(TimeoutError()), "network")
        self.assertEqual(c.classify_error(ValueError()), "other")
        # a network-caused "login" failure during polling is not a lost login
        chain = self._chain()
        s = c.poll_once(CFG, None, NOW, lambda: (_ for _ in ()).throw(chain), None)
        self.assertEqual(s["error"], "fetch_failed:network:PyiCloudFailedLoginException")
        self.assertFalse(c.auth_lost(s))

    def test_requests_get_a_bounded_default_timeout(self):
        seen = []

        class Session:
            def request(self, method, url, **kw):
                seen.append(kw.get("timeout"))
                return "ok"

        s = Session()
        c.bound_timeouts(s)
        self.assertEqual(s.request("GET", "https://x"), "ok")
        s.request("POST", "https://x", timeout=5)
        self.assertEqual(seen, [c.HTTP_TIMEOUT, 5])
        self.assertTrue(all(v and v <= 60 for v in c.HTTP_TIMEOUT))

    def test_login_names_network_failure_and_stays_quiet_about_secrets(self):
        import contextlib
        import io
        chain = self._chain()
        def boom(*a, **k):
            raise chain
        c._service = boom
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = c.login({})
        text = out.getvalue()
        self.assertEqual(code, 3)
        self.assertIn("网络", text)
        self.assertIn("不是密码或验证码错误", text)
        self.assertNotIn("Traceback", text)

    def test_global_edge_rewrites_only_cn_setup_and_findmy(self):
        import socket
        calls = []
        real = socket.getaddrinfo
        socket.getaddrinfo = lambda host, *a, **k: calls.append(host) or []
        try:
            c.use_global_edge()
            c.use_global_edge()  # idempotent: never stacks
            for host in ("setup.icloud.com.cn", "SETUP.icloud.com.cn.", "p66-fmipweb.icloud.com.cn",
                         "idmsa.apple.com", "www.icloud.com.cn", "evilsetup.icloud.com.cn",
                         "setup.icloud.com.cn.evil.example", "p66-fmipweb.icloud.com", "restapi.amap.com"):
                socket.getaddrinfo(host, 443)
        finally:
            socket.getaddrinfo = real
        self.assertEqual(calls, ["setup.icloud.com", "setup.icloud.com", "setup.icloud.com",
                                 "idmsa.apple.com", "www.icloud.com.cn", "evilsetup.icloud.com.cn",
                                 "setup.icloud.com.cn.evil.example", "p66-fmipweb.icloud.com",
                                 "restapi.amap.com"])

    def test_cn_only_findmy_shard_does_not_require_global_namesake(self):
        import socket
        original = socket.getaddrinfo
        calls = []
        def resolve(host, *args, **kwargs):
            calls.append(host)
            if host == "p229-fmipweb.icloud.com":
                raise socket.gaierror(-2, "Name or service not known")
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("192.0.2.1", 443))]
        socket.getaddrinfo = resolve
        try:
            c.use_global_edge()
            result = socket.getaddrinfo("p229-fmipweb.icloud.com.cn", 443)
        finally:
            socket.getaddrinfo = original
        self.assertEqual(calls, ["setup.icloud.com"])
        self.assertEqual(result[0][4], ("192.0.2.1", 443))

    def _fake_pyicloud(self, *, tokens=True, token_error=None, devices_reauth=False, findme=None,
                       devices_error=None):
        import types
        rec = {"init": None, "token_calls": 0, "authenticate_calls": 0}

        class Session:
            def __init__(self):
                self.data = {"session_token": "s", "trust_token": "t"} if tokens else {"session_token": "s"}

            def request(self, method, url, **kw):
                return None

        class Devices:
            def __init__(self, api):
                if devices_reauth:
                    api.authenticate(force_refresh=True)  # what FindMy does on a 450
                self.devices = {"DEV-1": types.SimpleNamespace(name="iPhone", model_name="iPhone 15")}

        class Service:
            def __init__(self, apple_id, password, **kw):
                rec["init"] = {"password": password, **kw}
                self.session = Session()

            def authenticate(self, *a, **k):
                rec["authenticate_calls"] += 1

            def _authenticate_with_token(self, require_trust=True):
                rec["token_calls"] += 1
                if token_error is not None:
                    raise token_error
                if findme:
                    self.data = {"webservices": {"findme": {"url": findme}}}

            data = {}

            @property
            def devices(self):
                rec["devices_calls"] = rec.get("devices_calls", 0) + 1
                if devices_error is not None:
                    raise devices_error
                return Devices(self)

        module = types.ModuleType("pyicloud")
        module.PyiCloudService = Service
        return module, rec

    def _resume(self, module, **cfg):
        import contextlib, io
        saved = sys.modules.get("pyicloud")
        sys.modules["pyicloud"] = module
        os.environ["ICLOUD_APPLE_ID"] = "someone@example.invalid"
        out = io.StringIO()
        try:
            with tempfile.TemporaryDirectory() as d, contextlib.redirect_stdout(out):
                c.STATE_DIR = Path(d)
                code = c.resume(dict({"icloud_region": "cn"}, **cfg))
        finally:
            os.environ.pop("ICLOUD_APPLE_ID", None)
            if saved is None:
                sys.modules.pop("pyicloud", None)
            else:
                sys.modules["pyicloud"] = saved
        return code, out.getvalue()

    def test_resume_is_one_token_request_without_password_or_2fa(self):
        module, rec = self._fake_pyicloud()
        code, text = self._resume(module)
        self.assertEqual(code, 0)
        self.assertEqual((rec["token_calls"], rec["authenticate_calls"]), (1, 0))
        self.assertIsNone(rec["init"]["password"])
        self.assertIs(rec["init"]["authenticate"], False)
        self.assertIs(rec["init"]["china_mainland"], True)
        self.assertIn("DEV-1 | iPhone | iPhone 15", text)
        self.assertNotIn("latitude", text)

    def test_resume_refuses_fallbacks(self):
        from pyicloud.exceptions import PyiCloudFailedLoginException
        module, rec = self._fake_pyicloud(tokens=False)
        code, text = self._resume(module)
        self.assertEqual((code, rec["token_calls"]), (6, 0))
        self.assertIn("重新完整登录", text)
        module, rec = self._fake_pyicloud(token_error=self._chain())
        code, text = self._resume(module)
        self.assertEqual((code, rec["token_calls"], rec["authenticate_calls"]), (3, 1, 0))
        self.assertIn("不是密码或验证码错误", text)
        module, rec = self._fake_pyicloud(token_error=PyiCloudFailedLoginException("Invalid authentication token."))
        code, text = self._resume(module)
        self.assertEqual((code, rec["token_calls"], rec["authenticate_calls"]), (4, 1, 0))
        module, rec = self._fake_pyicloud(devices_reauth=True)  # a hidden re-auth is refused, not performed
        code, text = self._resume(module)
        self.assertEqual((code, rec["token_calls"], rec["authenticate_calls"]), (4, 1, 0))

    def _diag(self):
        return json.loads((c.STATE_DIR / "last-resume-diagnostic.json").read_text())

    def test_resume_stops_before_an_unrouted_cn_findmy_host(self):
        import contextlib, io
        module, rec = self._fake_pyicloud(findme="https://fmipweb.icloud.com.cn:443")
        saved = sys.modules.get("pyicloud"); sys.modules["pyicloud"] = module
        os.environ["ICLOUD_APPLE_ID"] = "someone@example.invalid"
        out = io.StringIO()
        try:
            with tempfile.TemporaryDirectory() as d, contextlib.redirect_stdout(out):
                c.STATE_DIR = Path(d)
                code = c.resume({"icloud_region": "cn", "icloud_edge": "global"})
                diag = self._diag()
        finally:
            os.environ.pop("ICLOUD_APPLE_ID", None)
            sys.modules["pyicloud"] = saved
        self.assertEqual((code, rec["token_calls"], rec.get("devices_calls", 0)), (7, 1, 0))
        self.assertIn("Find My 服务器：fmipweb.icloud.com.cn（直连）", out.getvalue())
        self.assertEqual((diag["stage"], diag["host"], diag["via_global_edge"]), ("读取设备列表", "fmipweb.icloud.com.cn", False))
        self.assertNotIn("example.invalid", json.dumps(diag))

    def test_resume_device_failure_reports_stage_host_and_cause_types(self):
        import contextlib, io
        module, rec = self._fake_pyicloud(findme="https://p66-fmipweb.icloud.com.cn:443", devices_error=self._chain())
        saved = sys.modules.get("pyicloud"); sys.modules["pyicloud"] = module
        os.environ["ICLOUD_APPLE_ID"] = "someone@example.invalid"
        out = io.StringIO()
        try:
            with tempfile.TemporaryDirectory() as d, contextlib.redirect_stdout(out):
                c.STATE_DIR = Path(d)
                code = c.resume({"icloud_region": "cn", "icloud_edge": "global"})
                diag = self._diag()
        finally:
            os.environ.pop("ICLOUD_APPLE_ID", None)
            sys.modules["pyicloud"] = saved
        text = out.getvalue()
        self.assertEqual((code, rec["token_calls"], rec["authenticate_calls"]), (3, 1, 0))
        self.assertIn("出错阶段：读取设备列表；服务器：p66-fmipweb.icloud.com.cn（经全球边缘）", text)
        self.assertIn("RemoteDisconnected", text)
        self.assertEqual(diag["error_chain"][-1], "RemoteDisconnected")
        self.assertNotIn("https://", json.dumps(diag))

    def test_distance(self):
        self.assertAlmostEqual(c.distance_m(23.1, 113.3, 23.1018, 113.3), 200.2, delta=1.0)


if __name__ == "__main__":
    unittest.main()
