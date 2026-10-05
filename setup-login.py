"""One-time, user-operated login. Never starts the location collector."""
import fcntl
import getpass
import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import sys
import tempfile

DIRECTORY = Path('/etc/tillyouwake-location')
CONFIG = DIRECTORY / 'config.json'


def credential_text(apple_id, key):
    return ('# systemd EnvironmentFile; do not source in a shell.\n'
            + 'ICLOUD_APPLE_ID=' + json.dumps(apple_id, ensure_ascii=False) + '\n'
            + 'ICLOUD_PASSWORD=""\nAMAP_KEY=' + json.dumps(key) + '\n')


def atomic_write(path, content, mode, uid, gid):
    fd, name = tempfile.mkstemp(prefix='.' + path.name + '-', dir=path.parent)
    try:
        os.fchmod(fd, mode)
        os.fchown(fd, uid, gid)
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def ask_key():
    key = getpass.getpass('高德 Web 服务 Key（粘贴后按回车，输入不显示）：').strip()
    if not re.fullmatch(r'[a-fA-F0-9]{32}', key):
        raise SystemExit('Key 应是 32 位十六进制字符；请确认复制的是 Web 服务 Key。没有保存配置。')
    return key


def main(resume=False):
    if os.geteuid() != 0 or not sys.stdin.isatty():
        raise SystemExit('请用 ssh -t 在交互终端运行此入口。')
    os.umask(0o077)
    config = json.loads(CONFIG.read_text())
    if config.get('enabled') is not False or config.get('device_id'):
        raise SystemExit('定位已启用或已有设备配置，请回聊天确认当前进度。')
    if config.get('icloud_region') != 'cn':
        raise SystemExit('当前不是已确认的中国大陆区，请回聊天检查。')
    if resume:
        print('续接上次已经通过验证码的登录（不会打开持续定位）。不需要密码和验证码。')
        print('沿用中国区原域名和证书，经 Apple 全球边缘节点只尝试一次；失败就停下，不会改用密码重试。')
        print('Apple 账号和高德 Key 会存入 VPS 的受限配置。\n')
    else:
        print('第一步：Apple 登录和设备选择（不会打开持续定位）。')
        print('登录会访问 Apple 查找中的设备及最后已知位置。密码只用于本次登录，不保存。')
        print('Apple 账号和高德 Key 会存入 VPS 的受限配置；会话过期时需本人重新登录。\n')
    apple_id = input('上次登录成功走到验证码那一步用的 Apple 账号（邮箱）：' if resume
                     else 'Apple 账号（邮箱或手机号）：').strip()
    if not apple_id or len(apple_id) > 254 or any(c.isspace() or ord(c) < 32 for c in apple_id):
        raise SystemExit('账号格式不正确；没有保存配置。')
    key = None if resume else ask_key()  # resume: only after Apple has answered
    env = dict(os.environ, ICLOUD_APPLE_ID=apple_id, HOME='/var/lib/tillyouwake-location', PYTHONIOENCODING='utf-8')
    env.pop('ICLOUD_PASSWORD', None)
    if not resume:
        print('\n接下来输入 Apple 密码，并按提示处理手机上的登录验证。')
    code = subprocess.call(['runuser', '-u', 'tillyouwake-location', '--preserve-environment', '--',
                            '/opt/tillyouwake-location/.venv/bin/python',
                            '/opt/tillyouwake-location/collector.py',
                            *(['--resume', '--global-edge'] if resume else ['--login'])], env=env)
    if code:
        raise SystemExit('登录未完成；没有保存账号或 Key。把错误提示告诉 Codex 即可，不要发密码或验证码。')
    print('\n上面每行依次是：设备 ID | 名称 | 型号。')
    device_id = input('复制你的 iPhone 那一行最左侧的设备 ID，粘贴到这里：').strip()
    if not re.fullmatch(r'[A-Za-z0-9+/=_:.-]{8,512}', device_id):
        raise SystemExit('设备 ID 格式不正确；没有保存配置。请重新运行。')
    if key is None:
        key = ask_key()
    with (DIRECTORY / '.config.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        config = json.loads(CONFIG.read_text())
        if config.get('enabled') is not False or config.get('device_id'):
            raise SystemExit('配置刚被别处改变，没有覆盖。请回聊天检查。')
        atomic_write(DIRECTORY / 'credentials.env', credential_text(apple_id, key), 0o600, 0, 0)
        config.update(icloud_region='cn', device_id=device_id, source_crs='')
        if resume:
            config['icloud_edge'] = 'global'  # the collector keeps the route that just worked
        st = CONFIG.stat()
        atomic_write(CONFIG, json.dumps(config, ensure_ascii=False, indent=2) + '\n', st.st_mode & 0o7777, st.st_uid, st.st_gid)
    print('\n账号接入完成，持续定位仍关闭。回聊天告诉 Codex「登录好了」，继续核对坐标。')


if __name__ == '__main__':
    if '--self-check' in sys.argv:
        sample = 'demo+test@example.com'
        key = '0123456789abcdef' * 2
        parsed = dict(line.split('=', 1) for line in credential_text(sample, key).splitlines() if not line.startswith('#'))
        assert shlex.split(parsed['ICLOUD_APPLE_ID']) == [sample]
        assert shlex.split(parsed['ICLOUD_PASSWORD']) == ['']
        assert shlex.split(parsed['AMAP_KEY']) == [key]
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / 'config.json'
            atomic_write(p, '{"enabled": false}\n', 0o640, os.getuid(), os.getgid())
            assert json.loads(p.read_text())['enabled'] is False
            assert p.stat().st_mode & 0o777 == 0o640
            assert len(list(Path(tmp).iterdir())) == 1
        print('PASS: offline credential encoding and atomic configuration; no Apple/AMap calls.')
    else:
        try:
            main(resume='--resume' in sys.argv)
        except (KeyboardInterrupt, EOFError):
            raise SystemExit('\n已取消，持续定位没有开启。')
