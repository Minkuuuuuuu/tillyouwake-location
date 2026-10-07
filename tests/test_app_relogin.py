"""App re-login flow (location_control + location_relogin) with a fake child: no Apple, no systemd."""
import importlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
APP = Path(os.environ.get("APP_PLUGIN") or HERE.parent / "integrations/app/plugins/tillyouwake_app")
PASSWORD = "pw-SECRET-123"
CODE = "246810"


class FakeSystemctl:
    def __init__(self, state="inactive"):
        self.calls, self.state = [], state

    def __call__(self, *args):
        self.calls.append(args)
        if args[0] == "start":
            self.state = "active"
        return subprocess.CompletedProcess(["systemctl", *args], 0, self.state if args[0] == "is-active" else "", "")


def alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    try:  # a zombie left for the reaper counts as gone
        return Path(f"/proc/{pid}/stat").read_text().split()[2] != "Z"
    except OSError:
        return False


class Tests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        pkg = self.tmp / "fakepkg"
        pkg.mkdir()
        (pkg / "__init__.py").write_text("")
        (pkg / "model_settings.py").write_text(
            "class SettingsError(Exception):\n    def __init__(self, status, code):\n"
            "        super().__init__(code)\n        self.status, self.code = status, code\n")
        for name in ("location_control.py", "location_relogin.py"):
            shutil.copy(APP / name, pkg / name)
        sys.path.insert(0, str(self.tmp))
        for name in ("fakepkg", "fakepkg.model_settings", "fakepkg.location_control", "fakepkg.location_relogin"):
            sys.modules.pop(name, None)
        self.lc = importlib.import_module("fakepkg.location_control")
        self.lr = importlib.import_module("fakepkg.location_relogin")
        self.SettingsError = importlib.import_module("fakepkg.model_settings").SettingsError
        d = self.tmp
        self.lc.CONFIG, self.lc.CREDENTIALS, self.lc.LATEST = d / "config.json", d / "credentials.env", d / "latest.json"
        self.lc.PLACES, self.lc.LOGIN_OK = d / "places.json", d / "login-ok.json"
        self.lc.CONFIG.write_text(json.dumps({"enabled": False, "generation": 14, "device_id": "dev1",
                                              "icloud_region": "cn", "icloud_edge": "global", "source_crs": "wgs84"}))
        self.lc.CREDENTIALS.write_text('ICLOUD_APPLE_ID="someone.private@example.com"\nICLOUD_PASSWORD=""\nAMAP_KEY="k"\n')
        self.lc.LATEST.write_text(json.dumps({"generation": 13, "status": "needs_login", "checked_at": 100.0,
                                              "error": "login_failed:auth"}))
        self.lc.PLACES.write_text(json.dumps({"places": [{"id": "p1", "name": "宿舍", "radius_m": 30}]}))
        self.before = {p.name: p.read_bytes() for p in (self.lc.CONFIG, self.lc.CREDENTIALS, self.lc.PLACES)}
        self.sys = FakeSystemctl()
        self.lc._systemctl = self.sys
        self.record = d / "record.jsonl"
        self.mode = "ok"
        real_popen = subprocess.Popen

        def spawn(apple_id):
            self.spawned_with = apple_id
            env = {"PATH": os.environ["PATH"], "FAKE_MODE": self.mode, "FAKE_RECORD": str(self.record),
                   "ICLOUD_APPLE_ID": apple_id}
            return real_popen([sys.executable, str(HERE / "fake_relogin_child.py")], stdin=subprocess.PIPE,
                              stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, env=env, text=True,
                              bufsize=1, close_fds=True, start_new_session=True)
        self.lr._spawn = spawn

    def tearDown(self):
        flow = self.lr._flow
        if flow and flow["stage"] not in self.lr.ENDS:
            with self.lr._lock:
                self.lr._end(flow, "cancelled")
        time.sleep(0.2)
        sys.path.remove(str(self.tmp))
        shutil.rmtree(self.tmp)

    def post(self, action):
        return self.lc.update({"relogin": action})

    def wait_stage(self, flow, stages, limit=8.0):
        end = time.time() + limit
        while time.time() < end:
            result = self.post({"action": "status", "flow": flow})
            if result["relogin_flow"]["stage"] in stages:
                return result
            time.sleep(0.05)
        self.fail(f"never reached {stages}")

    def wait_after(self, limit=3.0):
        end = time.time() + limit
        while time.time() < end and not any(c[0] == "start" for c in self.sys.calls):
            time.sleep(0.05)

    def child(self):
        end = time.time() + 5
        while time.time() < end and not (self.record.exists() and self.record.read_text().strip()):
            time.sleep(0.05)
        return json.loads(self.record.read_text().splitlines()[0])

    def assert_settings_untouched(self):
        for p in (self.lc.CONFIG, self.lc.CREDENTIALS, self.lc.PLACES):
            self.assertEqual(p.read_bytes(), self.before[p.name], p.name)

    def test_offered_masked_and_needed_logic(self):
        view = self.lc.snapshot()["relogin"]
        self.assertEqual(view, {"needed": True, "busy": False, "allowed": True, "account": "s***@e***.com"})
        self.lc.LOGIN_OK.write_text(json.dumps({"at": 200.0}))
        self.assertFalse(self.lc.snapshot()["relogin"]["needed"])
        self.lc.LOGIN_OK.unlink()
        self.sys.state = "active"
        self.assertEqual(self.lc.snapshot()["relogin"]["needed"], False)
        self.assertEqual(self.lc.snapshot()["relogin"]["allowed"], False)
        self.assertEqual(self.lr._mask("13812345678"), "1********78")

    def test_status_after_relogin_with_switch_on(self):
        cfg = json.loads(self.lc.CONFIG.read_text()); cfg.update(enabled=True, generation=13)
        self.lc.CONFIG.write_text(json.dumps(cfg))
        self.assertEqual(self.lc.snapshot()["status"], "needs_login")
        self.lc.LOGIN_OK.write_text(json.dumps({"at": 200.0}))
        snap = self.lc.snapshot()
        self.assertEqual((snap["status"], snap["relogin"]["needed"]), ("not_running", False))
        self.sys.state = "active"
        self.assertEqual(self.lc.snapshot()["status"], "waiting_fresh")

    def test_success_switch_off_stays_off(self):
        result = self.post({"action": "start", "password": PASSWORD})
        flow = result["relogin_flow"]["id"]
        self.assertRegex(flow, r"^[0-9a-f]{32}$")
        done = self.wait_stage(flow, self.lr.ENDS)
        self.assertEqual((done["relogin_flow"]["stage"], done["relogin_flow"].get("kind")), ("done", None))
        self.assertNotIn("id", done["relogin_flow"])
        time.sleep(0.5)
        self.assertFalse(any(c[0] == "start" for c in self.sys.calls))  # off stays off, nothing started
        self.assertFalse(self.lc.snapshot()["enabled"])
        self.assert_settings_untouched()

    def test_success_switch_on_restarts_collector_only(self):
        cfg = json.loads(self.lc.CONFIG.read_text()); cfg["enabled"] = True
        self.lc.CONFIG.write_text(json.dumps(cfg)); self.before["config.json"] = self.lc.CONFIG.read_bytes()
        flow = self.post({"action": "start", "password": PASSWORD})["relogin_flow"]["id"]
        self.wait_stage(flow, self.lr.ENDS)
        self.wait_after()
        self.assertIn(("start", "--no-block", self.lc.UNIT), self.sys.calls)
        self.assert_settings_untouched()  # same generation, same switch

    def test_code_flow_and_wrong_ids(self):
        self.mode = "code"
        flow = self.post({"action": "start", "password": PASSWORD})["relogin_flow"]["id"]
        self.wait_stage(flow, {"need_code"})
        for bad, status, code in ((("0" * 32), 409, "relogin_flow_gone"), ("x", 409, "relogin_flow_gone")):
            with self.assertRaises(self.SettingsError) as cm:
                self.post({"action": "code", "flow": bad, "code": CODE})
            self.assertEqual((cm.exception.status, cm.exception.code), (status, code))
        for bad in ("12345", "12a456", 123456, "1234567"):
            with self.assertRaises(self.SettingsError) as cm:
                self.post({"action": "code", "flow": flow, "code": bad})
            self.assertEqual(cm.exception.code, "invalid_relogin_code")
        self.post({"action": "code", "flow": flow, "code": CODE})
        done = self.wait_stage(flow, self.lr.ENDS)
        self.assertEqual(done["relogin_flow"]["stage"], "done")
        with self.assertRaises(self.SettingsError) as cm:
            self.post({"action": "code", "flow": flow, "code": CODE})
        self.assertEqual(cm.exception.code, "relogin_not_waiting_code")

    def test_code_rejected_and_auth_failure_are_kinds(self):
        self.mode = "code"
        flow = self.post({"action": "start", "password": PASSWORD})["relogin_flow"]["id"]
        self.wait_stage(flow, {"need_code"})
        self.post({"action": "code", "flow": flow, "code": "111111"})
        end = self.wait_stage(flow, self.lr.ENDS)["relogin_flow"]
        self.assertEqual((end["stage"], end["kind"]), ("failed", "code_rejected"))
        self.mode = "auth"
        flow = self.post({"action": "start", "password": PASSWORD})["relogin_flow"]["id"]
        end = self.wait_stage(flow, self.lr.ENDS)["relogin_flow"]
        self.assertEqual((end["stage"], end["kind"]), ("failed", "auth"))
        self.assertFalse(any(c[0] == "start" for c in self.sys.calls))

    def test_one_flow_at_a_time_and_no_collector_next_to_it(self):
        self.mode = "hang"
        flow = self.post({"action": "start", "password": PASSWORD})["relogin_flow"]["id"]
        with self.assertRaises(self.SettingsError) as cm:
            self.post({"action": "start", "password": PASSWORD})
        self.assertEqual((cm.exception.status, cm.exception.code), (409, "relogin_busy"))
        with self.assertRaises(self.SettingsError) as cm:
            self.lc.update({"enabled": True})
        self.assertEqual(cm.exception.code, "relogin_in_progress")
        self.assertTrue(self.lc.snapshot()["relogin"]["busy"])
        pid = self.child()["pid"]
        self.assertTrue(alive(pid))
        end = self.post({"action": "cancel", "flow": flow})["relogin_flow"]
        self.assertEqual(end["stage"], "cancelled")
        deadline = time.time() + 6
        while alive(pid) and time.time() < deadline:
            time.sleep(0.1)
        self.assertFalse(alive(pid), "cancel must end the child")
        self.mode = "ok"
        flow2 = self.post({"action": "start", "password": PASSWORD})["relogin_flow"]["id"]
        self.assertNotEqual(flow, flow2)
        with self.assertRaises(self.SettingsError) as cm:
            self.post({"action": "cancel", "flow": flow})  # the old page cannot touch the new flow
        self.assertEqual(cm.exception.code, "relogin_flow_gone")

    def test_timeout_kills_child(self):
        self.mode = "hang"
        self.lr.FLOW_SECONDS = 1
        flow = self.post({"action": "start", "password": PASSWORD})["relogin_flow"]["id"]
        pid = self.child()["pid"]
        end = self.wait_stage(flow, self.lr.ENDS, limit=5)["relogin_flow"]
        self.assertEqual(end["stage"], "expired")
        deadline = time.time() + 6
        while alive(pid) and time.time() < deadline:
            time.sleep(0.1)
        self.assertFalse(alive(pid))

    def test_refused_while_collector_runs_or_unconfigured(self):
        self.sys.state = "active"
        with self.assertRaises(self.SettingsError) as cm:
            self.post({"action": "start", "password": PASSWORD})
        self.assertEqual(cm.exception.code, "relogin_collector_running")
        self.sys.state = "inactive"
        self.lc.CREDENTIALS.write_text("ICLOUD_APPLE_ID=\n")
        with self.assertRaises(self.SettingsError) as cm:
            self.post({"action": "start", "password": PASSWORD})
        self.assertEqual(cm.exception.code, "location_not_configured")
        self.assertFalse(self.record.exists(), "nothing spawned")

    def test_bad_shapes_spawn_nothing(self):
        for action in ({"action": "start"}, {"action": "start", "password": ""},
                       {"action": "start", "password": "a\nb"}, {"action": "start", "password": "x" * 257},
                       {"action": "start", "password": 5}, {"action": "start", "password": "p", "extra": 1},
                       {"action": "nope"}, "start", {"action": "status"}):
            with self.assertRaises(self.SettingsError) as cm:
                self.post(action)
            self.assertEqual(cm.exception.status, 400 if action != {"action": "status"} else 400)
        self.assertFalse(self.record.exists())

    def test_secrets_never_leave_the_pipe(self):
        self.mode = "code"
        result = self.post({"action": "start", "password": PASSWORD})
        flow = result["relogin_flow"]["id"]
        self.wait_stage(flow, {"need_code"})
        responses = [result, self.post({"action": "code", "flow": flow, "code": CODE})]
        responses.append(self.wait_stage(flow, self.lr.ENDS))
        responses.append(self.lc.snapshot())
        blob = json.dumps(responses, ensure_ascii=False)
        for secret in (PASSWORD, CODE, "someone.private", "dev1"):
            self.assertNotIn(secret, blob)
        child = self.child()
        self.assertNotIn(PASSWORD, json.dumps(child))  # not argv, not env
        for path in self.tmp.rglob("*"):
            if path.is_file() and path.suffix != ".py":
                data = path.read_bytes()
                self.assertNotIn(PASSWORD.encode(), data, path.name)
                self.assertNotIn(CODE.encode(), data, path.name)
        self.assertEqual(self.lr.view()["stage"], "done")
        self.assertNotIn("id", self.lr.view())


if __name__ == "__main__":
    unittest.main()
