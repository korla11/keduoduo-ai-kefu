#!/usr/bin/env python3
"""客多多AI客服系统 - 跨平台安装/启动器（Windows / macOS / Linux）

用法:
  python install.py install [--dest 目录] [--no-autostart] [--port 4999]
  python install.py start | stop | status   [--dest 目录]
默认安装目录: ~/KeDuoDuo  (Windows: %USERPROFILE%\\KeDuoDuo)
"""
import argparse, os, shutil, subprocess, sys, time, urllib.request, json

SRC = os.path.dirname(os.path.abspath(__file__))
IS_WIN, IS_MAC = sys.platform == 'win32', sys.platform == 'darwin'
LABEL = 'com.keduoduo.gateway'
TASK = 'KeDuoDuoGateway'
NOWIN = getattr(subprocess, 'CREATE_NO_WINDOW', 0)

RUNNER = r'''# 由 install.py 生成：设置环境后运行网关
import os, runpy, sys
here = os.path.dirname(os.path.abspath(__file__))
cfg = os.path.join(here, 'kdd_settings.json')
try:
    import json; s = json.load(open(cfg, encoding='utf-8'))
except Exception:
    s = {}
os.environ.setdefault('PORT', str(s.get('port', 4999)))
os.environ.setdefault('BILIGO_LAUNCH', 'process')
os.environ.setdefault('BILIGO_GATEWAY_HOST', s.get('host', '127.0.0.1'))
os.environ.setdefault('PYTHONUNBUFFERED', '1')
os.environ.setdefault('PYTHONUTF8', '1')
app = os.path.join(here, 'app')
os.makedirs(os.path.join(here, 'logs'), exist_ok=True)
if sys.stdout is None or 'pythonw' in os.path.basename(sys.executable).lower():
    f = open(os.path.join(here, 'logs', 'gateway.log'), 'a', encoding='utf-8', buffering=1)
    sys.stdout = sys.stderr = f
with open(os.path.join(here, 'gateway.pid'), 'w') as f:
    f.write(str(os.getpid()))
os.chdir(app); sys.path.insert(0, app)
runpy.run_path(os.path.join(app, 'gateway.py'), run_name='__main__')
'''


def log(m): print('==> ' + m, flush=True)


def venv_py(dest, windowless=False):
    if IS_WIN:
        return os.path.join(dest, 'venv', 'Scripts', 'pythonw.exe' if windowless else 'python.exe')
    return os.path.join(dest, 'venv', 'bin', 'python')


def run(cmd, **kw):
    print('   $ ' + ' '.join(cmd), flush=True)
    subprocess.check_call(cmd, **kw)


def copy_app(dest):
    src_app, dst_app = os.path.join(SRC, 'app'), os.path.join(dest, 'app')
    keep = os.path.isdir(os.path.join(dst_app, 'users'))
    if keep:
        log('检测到已有数据：保留 users/ 与 auth_users.json，仅更新程序文件')
    for root, dirs, files in os.walk(src_app):
        rel = os.path.relpath(root, src_app)
        if keep and (rel == 'users' or rel.startswith('users' + os.sep)):
            continue
        dirs[:] = [d for d in dirs if d != '__pycache__']
        os.makedirs(os.path.join(dst_app, rel), exist_ok=True)
        for fn in files:
            if keep and rel == '.' and fn in ('auth_users.json', '.auth_secret_key'):
                continue
            shutil.copy2(os.path.join(root, fn), os.path.join(dst_app, rel, fn))
    for fn in os.listdir(SRC):
        if fn.endswith(('.sh', '.bat', '.ps1', '.plist', '.md', '.txt')) or fn == 'install.py':
            shutil.copy2(os.path.join(SRC, fn), os.path.join(dest, fn))


def install(a):
    dest = os.path.abspath(os.path.expanduser(a.dest))
    if sys.version_info < (3, 10):
        sys.exit('需要 Python 3.10 或更高版本')
    log(f'安装目录: {dest}   Python: {sys.version.split()[0]}')
    os.makedirs(dest, exist_ok=True)
    if status_ok(a.port):
        log('检测到服务正在运行，先停止'); stop(a)
    copy_app(dest)
    with open(os.path.join(dest, 'kdd_run.py'), 'w', encoding='utf-8') as f:
        f.write(RUNNER)
    with open(os.path.join(dest, 'kdd_settings.json'), 'w', encoding='utf-8') as f:
        json.dump({'port': a.port, 'host': '127.0.0.1'}, f)
    if not os.path.exists(venv_py(dest)):
        log('创建虚拟环境'); run([sys.executable, '-m', 'venv', os.path.join(dest, 'venv')])
    py = venv_py(dest)
    log('安装依赖'); run([py, '-m', 'pip', 'install', '-q', '--upgrade', 'pip'])
    run([py, '-m', 'pip', 'install', '-q', '-r', os.path.join(SRC, 'requirements.txt')])
    log('安装 Playwright Chromium（约 150MB）')
    pw = [py, '-m', 'playwright', 'install']
    if sys.platform.startswith('linux') and hasattr(os, 'geteuid') and os.geteuid() == 0:
        pw.append('--with-deps')
    run(pw + ['chromium'])
    if not a.no_autostart:
        register_autostart(dest)
    start(a)
    for _ in range(30):
        if status_ok(a.port):
            log(f'✅ 安装完成：浏览器打开 http://localhost:{a.port}'); break
        time.sleep(1)
    else:
        log(f'⚠️ 服务暂未响应，请稍候打开 http://localhost:{a.port} ，日志: {os.path.join(dest, "logs", "gateway.log")}')
    if IS_WIN and not a.no_browser:
        os.startfile(f'http://localhost:{a.port}')


def register_autostart(dest):
    if IS_WIN:
        log('注册开机自启（任务计划程序：登录时启动、隐藏窗口、失败自动重启）')
        ps = os.path.join(dest, 'install_windows.ps1')
        run(['powershell', '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', ps, '-RegisterOnly', '-Dest', dest])
    elif IS_MAC:
        log('注册开机自启（launchd）')
        plist = os.path.expanduser(f'~/Library/LaunchAgents/{LABEL}.plist')
        os.makedirs(os.path.dirname(plist), exist_ok=True); os.makedirs(os.path.join(dest, 'logs'), exist_ok=True)
        with open(os.path.join(SRC, 'com.keduoduo.gateway.plist'), encoding='utf-8') as f:
            s = f.read().replace('__DEST__', dest)
        s = s.replace(f'<string>{dest}/app/gateway.py</string>', f'<string>{dest}/kdd_run.py</string>')
        with open(plist, 'w', encoding='utf-8') as f:
            f.write(s)
        uid = os.getuid()
        subprocess.run(['launchctl', 'bootout', f'gui/{uid}/{LABEL}'], capture_output=True)
        subprocess.run(['launchctl', 'enable', f'gui/{uid}/{LABEL}'], capture_output=True)
    elif os.geteuid() == 0 and shutil.which('systemctl'):
        log('注册 systemd 服务 keduoduo')
        unit = f'''[Unit]
Description=客多多AI客服系统
After=network-online.target
[Service]
WorkingDirectory={dest}
ExecStart={venv_py(dest)} {dest}/kdd_run.py
KillMode=control-group
Restart=always
RestartSec=5
[Install]
WantedBy=multi-user.target
'''
        with open('/etc/systemd/system/keduoduo.service', 'w', encoding='utf-8') as f:
            f.write(unit)
        subprocess.run(['systemctl', 'daemon-reload']); subprocess.run(['systemctl', 'enable', 'keduoduo'])
    else:
        log('非 root Linux：未注册自启。可用 crontab -e 加一行：@reboot ' + f'{venv_py(dest)} {dest}/install.py start --dest {dest}')


def status_ok(port=4999):
    try:
        return urllib.request.urlopen(f'http://127.0.0.1:{port}/login', timeout=3).status == 200
    except Exception:
        return False


def _pid(dest):
    try:
        return int(open(os.path.join(dest, 'gateway.pid')).read().strip())
    except Exception:
        return None


def start(a):
    dest = os.path.abspath(os.path.expanduser(a.dest))
    if status_ok(a.port):
        log('已在运行'); return
    if IS_WIN:
        r = subprocess.run(['schtasks', '/Run', '/TN', TASK], capture_output=True, creationflags=NOWIN)
        if r.returncode == 0:
            log('已通过任务计划启动'); return
        subprocess.Popen([venv_py(dest, True), os.path.join(dest, 'kdd_run.py')], cwd=dest,
                         creationflags=subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS | NOWIN,
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    elif IS_MAC and os.path.exists(os.path.expanduser(f'~/Library/LaunchAgents/{LABEL}.plist')):
        uid = os.getuid()
        r = subprocess.run(['launchctl', 'bootstrap', f'gui/{uid}', os.path.expanduser(f'~/Library/LaunchAgents/{LABEL}.plist')], capture_output=True)
        if r.returncode != 0:
            subprocess.run(['launchctl', 'kickstart', '-k', f'gui/{uid}/{LABEL}'])
    elif os.path.exists('/etc/systemd/system/keduoduo.service') and os.geteuid() == 0:
        subprocess.run(['systemctl', 'start', 'keduoduo'])
    else:
        os.makedirs(os.path.join(dest, 'logs'), exist_ok=True)
        lf = open(os.path.join(dest, 'logs', 'gateway.log'), 'ab')
        subprocess.Popen([venv_py(dest), os.path.join(dest, 'kdd_run.py')], cwd=dest, stdin=subprocess.DEVNULL,
                         stdout=lf, stderr=subprocess.STDOUT, start_new_session=True)
    log(f'已启动：http://localhost:{a.port}')


def stop(a):
    dest = os.path.abspath(os.path.expanduser(a.dest))
    if IS_WIN:
        subprocess.run(['schtasks', '/End', '/TN', TASK], capture_output=True, creationflags=NOWIN)
        pid = _pid(dest)
        if pid:
            subprocess.run(['taskkill', '/T', '/F', '/PID', str(pid)], capture_output=True, creationflags=NOWIN)
    elif IS_MAC and os.path.exists(os.path.expanduser(f'~/Library/LaunchAgents/{LABEL}.plist')):
        subprocess.run(['launchctl', 'bootout', f'gui/{os.getuid()}/{LABEL}'], capture_output=True)
    elif os.path.exists('/etc/systemd/system/keduoduo.service') and os.geteuid() == 0:
        subprocess.run(['systemctl', 'stop', 'keduoduo'])
    else:
        pid = _pid(dest)
        if pid:
            import signal
            try:
                os.kill(pid, signal.SIGTERM)
            except OSError:
                pass
    for _ in range(20):
        if not status_ok(a.port):
            break
        time.sleep(1)
    try:
        os.remove(os.path.join(dest, 'gateway.pid'))
    except OSError:
        pass
    log('已停止')


def main():
    ap = argparse.ArgumentParser(description='客多多AI客服系统 安装/启动器')
    ap.add_argument('cmd', choices=['install', 'start', 'stop', 'status'])
    default = os.path.dirname(os.path.abspath(__file__)) if os.path.exists(os.path.join(SRC, 'kdd_run.py')) else '~/KeDuoDuo'
    ap.add_argument('--dest', default=default)
    ap.add_argument('--port', type=int, default=4999)
    ap.add_argument('--no-autostart', action='store_true')
    ap.add_argument('--no-browser', action='store_true')
    a = ap.parse_args()
    if a.cmd == 'install':
        install(a)
    elif a.cmd == 'start':
        start(a)
    elif a.cmd == 'stop':
        stop(a)
    else:
        ok = status_ok(a.port); print('运行中' if ok else '未运行'); sys.exit(0 if ok else 1)


if __name__ == '__main__':
    main()
