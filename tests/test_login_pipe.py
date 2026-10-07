"""collector --login-pipe against a fake pyicloud: no network, no real account."""
import importlib.util
import io
import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = Path(os.environ.get("COLLECTOR") or ROOT / "collector.py")
PASSWORD = "pw-SECRET-123"
CODE = "246810"


def load():
    spec = importlib.util.spec_from_file_location("collector_under_test", SRC)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class FakeDevices:
    def __init__(self, ids):
        self.devices = {i: object() for i in ids}


class FakeApi:
    def __init__(self, two_fa=False, code_ok=True, ids=("dev1",), devices_error=None):
        self.requires_2fa, self.code_ok, self.ids, self.devices_error = two_fa, code_ok, ids, devices_error
        self.codes, self.trusted = [], False

    def validate_2fa_code(self, code):
        self.codes.append(code)
        if self.code_ok:
            self.trusted = True
            self.requires_2fa = False
        return self.code_ok

    @property
    def is_trusted_session(self):
        return self.trusted

    def trust_session(self):
        self.trusted = True

    @property
    def devices(self):
        if self.devices_error:
            raise self.devices_error
        return FakeDevices(self.ids)


class PyiCloudFailedLoginException(Exception):
    pass


class Tests(unittest.TestCase):
    def setUp(self):
        self.c = load()
        self.tmp = Path(tempfile.mkdtemp())
        self.c.STATE_DIR = self.tmp
        self.seen = []

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def run_pipe(self, api=None, lines=(PASSWORD,), error=None, config=None):
        def service(config, interactive=False, fresh=False, password=None):
            self.seen.append({"interactive": interactive, "fresh": fresh, "password": password})
            if error:
                raise error
            return api
        self.c._service = service
        stdin = io.StringIO("".join(line + "\n" for line in lines))
        stdout = io.StringIO()
        rc = self.c.login_pipe(config or {"device_id": "dev1"}, stdin, stdout)
        events = [json.loads(l) for l in stdout.getvalue().splitlines()]
        self.assertNotIn(PASSWORD, stdout.getvalue())
        self.assertNotIn(CODE, stdout.getvalue())
        return rc, [e["stage"] for e in events], events[-1]["kind"]

    def test_success_without_code(self):
        rc, stages, kind = self.run_pipe(FakeApi())
        self.assertEqual((rc, stages, kind), (0, ["signing_in", "checking_device", "done"], None))
        self.assertEqual(self.seen, [{"interactive": True, "fresh": True, "password": PASSWORD}])
        self.assertTrue((self.tmp / "login-ok.json").exists())
        self.assertNotIn(PASSWORD, (self.tmp / "login-ok.json").read_text())

    def test_success_with_code(self):
        api = FakeApi(two_fa=True)
        rc, stages, _ = self.run_pipe(api, lines=(PASSWORD, CODE))
        self.assertEqual(stages, ["signing_in", "need_code", "verifying", "checking_device", "done"])
        self.assertEqual((rc, api.codes, api.trusted), (0, [CODE], True))

    def test_code_rejected(self):
        rc, stages, kind = self.run_pipe(FakeApi(two_fa=True, code_ok=False), lines=(PASSWORD, CODE))
        self.assertEqual((rc, stages[-1], kind), (1, "failed", "code_rejected"))
        self.assertFalse((self.tmp / "login-ok.json").exists())

    def test_closed_pipe_while_waiting_for_code_is_cancel(self):
        api = FakeApi(two_fa=True)
        rc, stages, kind = self.run_pipe(api, lines=(PASSWORD,))
        self.assertEqual((rc, kind, api.codes), (130, "cancelled", []))

    def test_malformed_code_is_not_sent(self):
        api = FakeApi(two_fa=True)
        rc, _, kind = self.run_pipe(api, lines=(PASSWORD, "12ab56"))
        self.assertEqual((rc, kind, api.codes), (130, "cancelled", []))

    def test_empty_password_never_signs_in(self):
        rc, stages, kind = self.run_pipe(FakeApi(), lines=("",))
        self.assertEqual((rc, stages, kind, self.seen), (130, ["failed"], "cancelled", []))

    def test_auth_and_network_failures_are_kinds_only(self):
        rc, _, kind = self.run_pipe(error=PyiCloudFailedLoginException("Invalid email/password " + PASSWORD))
        self.assertEqual((rc, kind), (4, "auth"))
        rc, _, kind = self.run_pipe(error=TimeoutError("read timed out"))
        self.assertEqual((rc, kind), (3, "network"))

    def test_device_missing_keeps_old_needs_login(self):
        rc, _, kind = self.run_pipe(FakeApi(ids=("other",)))
        self.assertEqual((rc, kind), (8, "device_missing"))
        self.assertFalse((self.tmp / "login-ok.json").exists())

    def test_second_sign_in_is_refused_while_one_holds_the_lock(self):
        held = self.c._login_lock()
        try:
            rc, stages, kind = self.run_pipe(FakeApi())
            self.assertEqual((rc, stages, kind, self.seen), (9, ["failed"], "busy", []))
            self.assertEqual(self.c.login({"device_id": "dev1"}, fresh=True), 9)
        finally:
            held.close()
        self.assertEqual(self.run_pipe(FakeApi())[0], 0)

    def test_ssh_relogin_marks_success_too(self):
        self.c._service = lambda config, interactive=False, fresh=False, password=None: FakeApi()
        self.assertEqual(self.c.login({"device_id": "dev1"}, fresh=True), 0)
        self.assertTrue((self.tmp / "login-ok.json").exists())


if __name__ == "__main__":
    unittest.main()
