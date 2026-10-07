"""Stands in for `collector.py --login-pipe`: same stdin/stdout protocol, no Apple."""
import json, os, sys, time
mode = os.environ["FAKE_MODE"]
with open(os.environ["FAKE_RECORD"], "a") as fh:
    fh.write(json.dumps({"argv": sys.argv, "env": dict(os.environ), "pid": os.getpid()}) + "\n")
say = lambda stage, kind=None: (sys.stdout.write(json.dumps({"stage": stage, "kind": kind}) + "\n"), sys.stdout.flush())
print("正在连接 Apple 验证账号……", flush=True)  # the real collector's plain line: must be ignored
password = sys.stdin.readline().rstrip("\n")
if not password:
    say("failed", "cancelled"); sys.exit(130)
say("signing_in")
if mode == "hang":
    time.sleep(60)
if mode == "auth":
    say("failed", "auth"); sys.exit(4)
if mode == "code":
    say("need_code")
    code = sys.stdin.readline().strip()
    if not code:
        say("failed", "cancelled"); sys.exit(130)
    say("verifying")
    if code != "246810":
        say("failed", "code_rejected"); sys.exit(1)
say("checking_device")
say("done")
