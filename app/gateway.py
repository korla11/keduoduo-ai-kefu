"""客多多AI客服系统 - 多用户网关。

负责登录/账号管理，并把每个登录用户的请求转发到该用户独立的后端进程
（/opt/biligo/users/<uid>/，systemd 单元 biligo-user@<uid>）。每个后端拥有独立的
配置、AI 接口、知识库、平台登录（浏览器 profile）、日志、仪表盘数据与监控线程。
"""
import json
import os
import secrets
import socket
import subprocess
import threading
import time

import shutil
import sys

import requests
from flask import Flask, Response, jsonify, redirect, request, send_from_directory, session

import web_access

BASE = os.path.dirname(os.path.abspath(__file__))
USERS_DIR = os.path.join(BASE, 'users')
CODE_EXT = ('.py', '.html', '.js', '.css', '.png', '.ico', '.svg', '.jpg', '.jpeg', '.gif', '.webp', '.woff', '.woff2', '.ttf')
DATA_NAMES = {'laike_shot.png', 'laike_qr.png', 'laike_now.png', 'gateway.py', 'vps_pw.py', 'qr_login.py'}
STATIC_EXT = ('.css', '.js', '.png', '.ico', '.svg', '.jpg', '.jpeg', '.gif', '.webp', '.woff', '.woff2', '.ttf', '.map')
HOP = {'connection', 'keep-alive', 'proxy-authenticate', 'proxy-authorization', 'te', 'trailers',
       'transfer-encoding', 'upgrade', 'content-encoding', 'content-length', 'set-cookie'}
RUN_PY = '''import os, runpy, sys
here = os.path.dirname(os.path.abspath(__file__))
os.chdir(here)
sys.path.insert(0, here)
runpy.run_path(os.path.join(here, 'app.py'), run_name='__main__')
'''
_locks = {}
_glock = threading.Lock()

app = Flask(__name__)
web_access.register_web_auth(app)


def _is_code(name: str) -> bool:
    return (name.endswith(CODE_EXT) and name not in DATA_NAMES and not name.startswith('.')
            and os.path.isfile(os.path.join(BASE, name)))


def user_dir(uid: str) -> str:
    return os.path.join(USERS_DIR, uid)


def sync_code_links(uid: str) -> None:
    d = user_dir(uid)
    os.makedirs(d, exist_ok=True)
    for name in os.listdir(BASE):
        if not _is_code(name):
            continue
        link = os.path.join(d, name)
        target = os.path.join(BASE, name)
        if os.path.islink(link):
            if os.readlink(link) == target:
                continue
            os.unlink(link)
        elif os.path.exists(link):
            if IS_WIN:  # Windows 用拷贝代替软链接：代码有更新时刷新拷贝
                st_t, st_l = os.stat(target), os.stat(link)
                if st_t.st_size != st_l.st_size or st_t.st_mtime > st_l.st_mtime + 1:
                    shutil.copy2(target, link)
            continue  # 用户目录里的真实文件（数据）不覆盖
        if IS_WIN:
            shutil.copy2(target, link)
            continue
        try:
            os.symlink(target, link)
        except OSError:
            shutil.copy2(target, link)
    for name in os.listdir(d):  # 清理失效链接
        p = os.path.join(d, name)
        if os.path.islink(p) and not os.path.exists(p):
            os.unlink(p)
    run = os.path.join(d, 'run.py')
    if not os.path.exists(run) or os.path.islink(run):
        if os.path.islink(run):
            os.unlink(run)
        with open(run, 'w', encoding='utf-8') as f:
            f.write(RUN_PY)


def backend_env(uid: str) -> dict:
    path = os.path.join(user_dir(uid), 'backend.env')
    env = {}
    if os.path.exists(path):
        for line in open(path, encoding='utf-8'):
            if '=' in line:
                k, v = line.strip().split('=', 1)
                env[k] = v
    if not env.get('PORT') or not env.get('BILIGO_BACKEND_SECRET'):
        used = set()
        if os.path.isdir(USERS_DIR):
            for u in os.listdir(USERS_DIR):
                p = os.path.join(USERS_DIR, u, 'backend.env')
                if u != uid and os.path.exists(p):
                    for line in open(p, encoding='utf-8'):
                        if line.startswith('PORT='):
                            used.add(int(line.strip().split('=', 1)[1]))
        port = 5101
        while port in used:
            port += 1
        env = {'PORT': str(port), 'BILIGO_BACKEND_SECRET': secrets.token_hex(24), 'BILIGO_HOST': '127.0.0.1',
               'BILIGO_OPEN_BROWSER': '0', 'PYTHONUNBUFFERED': '1'}
        os.makedirs(user_dir(uid), exist_ok=True)
        with open(path, 'w', encoding='utf-8') as f:
            f.write(''.join(f'{k}={v}\n' for k, v in env.items()))
        os.chmod(path, 0o600)
    return env


# ── 后端启动方式：systemd（VPS）或直接子进程（Mac/本地，BILIGO_LAUNCH=process）──
_procs = {}
IS_WIN = sys.platform == 'win32'


def _launch_mode() -> str:
    m = os.environ.get('BILIGO_LAUNCH', '').strip().lower()
    if m in ('systemd', 'process'):
        return m
    if IS_WIN:
        return 'process'
    return 'systemd' if (shutil.which('systemctl') and os.path.exists('/etc/systemd/system/biligo-user@.service')) else 'process'


def _start_unit(uid: str) -> None:
    if _launch_mode() == 'systemd':
        subprocess.run(['systemctl', 'start', f'biligo-user@{uid}.service'], check=False, timeout=30)
        return
    p = _procs.get(uid)
    if p and p.poll() is None:
        return
    d = user_dir(uid)
    env = dict(os.environ)
    env.pop('PORT', None)
    env.update(backend_env(uid))
    env.setdefault('PYTHONUTF8', '1')
    env.setdefault('PYTHONIOENCODING', 'utf-8')
    logf = open(os.path.join(d, 'backend.out.log'), 'ab')
    kw = {}
    if IS_WIN:
        kw['creationflags'] = subprocess.CREATE_NEW_PROCESS_GROUP | getattr(subprocess, 'CREATE_NO_WINDOW', 0)
    else:
        kw['start_new_session'] = True
    _procs[uid] = subprocess.Popen([sys.executable, os.path.join(d, 'run.py')], cwd=d, env=env,
                                   stdin=subprocess.DEVNULL, stdout=logf, stderr=subprocess.STDOUT, **kw)


def _stop_unit(uid: str) -> None:
    if _launch_mode() == 'systemd':
        subprocess.run(['systemctl', 'stop', f'biligo-user@{uid}.service'], check=False, timeout=60)
        return
    p = _procs.pop(uid, None)
    if p and p.poll() is None:
        if IS_WIN:  # 连同 Playwright 驱动 / Chromium 子进程一起结束
            subprocess.run(['taskkill', '/T', '/F', '/PID', str(p.pid)], check=False,
                           capture_output=True, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        else:
            p.terminate()
        try:
            p.wait(timeout=30)
        except subprocess.TimeoutExpired:
            p.kill()


def _stop_all() -> None:
    for uid in list(_procs):
        _stop_unit(uid)


def _port_open(port: int) -> bool:
    try:
        with socket.create_connection(('127.0.0.1', port), timeout=0.5):
            return True
    except OSError:
        return False


def ensure_backend(uid: str, wait: float = 25.0):
    with _glock:
        lock = _locks.setdefault(uid, threading.Lock())
    with lock:
        env = backend_env(uid)
        port = int(env['PORT'])
        if _port_open(port):
            return port, env['BILIGO_BACKEND_SECRET']
        sync_code_links(uid)
        _start_unit(uid)
        deadline = time.time() + wait
        while time.time() < deadline:
            if _port_open(port):
                return port, env['BILIGO_BACKEND_SECRET']
            time.sleep(0.5)
    return None, None


def stop_backend(uid: str) -> None:
    if uid:
        _stop_unit(uid)


def _on_deleted(uid):
    if not uid:
        return
    stop_backend(uid)
    d = user_dir(uid)
    if os.path.isdir(d):
        os.rename(d, os.path.join(USERS_DIR, f'_deleted_{uid}_{int(time.time())}'))


def _on_changed(uid, blocked):
    if uid and blocked:
        stop_backend(uid)


web_access.USER_HOOKS['deleted'] = _on_deleted
web_access.USER_HOOKS['changed'] = _on_changed


def current_uid():
    name = session.get('user')
    if name != web_access.ALLOWED_USER:
        return None
    users = web_access._load_users()
    u = users.get(name)
    if not u:
        return None
    if not u.get('uid'):
        u['uid'] = web_access.new_uid()
        web_access._save_users(users)
    return u['uid']


@app.route('/', defaults={'path': ''}, methods=['GET', 'POST', 'PUT', 'DELETE', 'PATCH'])
@app.route('/<path:path>', methods=['GET', 'POST', 'PUT', 'DELETE', 'PATCH'])
def proxy(path):
    if request.method == 'GET' and '/' not in path and path.lower().endswith(STATIC_EXT) and _is_code(path):
        return send_from_directory(BASE, path, max_age=600)
    uid = current_uid()
    if not uid:
        return jsonify({'success': False, 'auth_required': True, 'error': '请先登录'}), 401
    port, secret = ensure_backend(uid)
    if not port:
        if path.startswith('api/'):
            return jsonify({'success': False, 'error': '您的工作空间正在启动，请稍后重试'}), 503
        return Response('<meta http-equiv="refresh" content="3"><p style="font-family:sans-serif;padding:40px">'
                        '正在启动您的工作空间，请稍候…</p>', 503, mimetype='text/html')
    url = f'http://127.0.0.1:{port}/{path}'
    if request.query_string:
        url += '?' + request.query_string.decode('latin-1')
    headers = {k: v for k, v in request.headers if k.lower() not in ('host', 'cookie', 'content-length', 'x-biligo-gateway')}
    headers['X-Biligo-Gateway'] = secret
    headers['X-Forwarded-For'] = request.remote_addr or ''
    try:
        r = requests.request(request.method, url, data=request.get_data(), headers=headers,
                             stream=True, allow_redirects=False, timeout=(5, 600))
    except requests.RequestException as exc:
        return jsonify({'success': False, 'error': f'后端暂不可用：{exc}'}), 502
    out_headers = []
    for k, v in r.raw.headers.items():
        if k.lower() in HOP:
            continue
        if k.lower() == 'location':
            v = v.replace(f'http://127.0.0.1:{port}', '')
        out_headers.append((k, v))
    ctype = r.headers.get('Content-Type', '')
    if 'text/html' in ctype or 'json' in ctype:
        return Response(r.content, r.status_code, out_headers)
    return Response(r.iter_content(65536), r.status_code, out_headers, direct_passthrough=True)


def _autostart():
    """网关启动时：为重启前开启了监控的有效用户启动其后端（后端会自动恢复监控）。"""
    time.sleep(2)
    _autostart_once()


def _autostart_once():
    users = web_access._load_users()
    for name, u in users.items():
        uid = u.get('uid')
        if not uid or web_access.user_block_reason(u):
            continue
        st = os.path.join(user_dir(uid), 'douyin_laike_state.json')
        try:
            with open(st, encoding='utf-8') as f:
                wanted = json.load(f).get('monitoring_enabled')
        except (OSError, ValueError):
            wanted = False
        if wanted:
            ensure_backend(uid, wait=60)


def _watchdog():
    """本地进程模式：后端意外退出时自动拉起（相当于 systemd Restart=always）。"""
    while True:
        time.sleep(30)
        if _launch_mode() != 'process':
            return
        try:
            _autostart_once()
        except Exception as exc:  # noqa: BLE001
            print('watchdog error:', exc)


def _reaper():
    """定期停止已停用/已到期用户的后端，避免其监控继续运行。"""
    while True:
        time.sleep(600)
        try:
            for u in web_access._load_users().values():
                if u.get('uid') and web_access.user_block_reason(u) \
                        and os.path.exists(os.path.join(user_dir(u['uid']), 'backend.env')):
                    env = backend_env(u['uid'])
                    if _port_open(int(env['PORT'])):
                        stop_backend(u['uid'])
        except Exception as exc:  # noqa: BLE001
            print('reaper error:', exc)


if __name__ == '__main__':
    import atexit, signal
    atexit.register(_stop_all)
    for _sig in ('SIGTERM', 'SIGBREAK'):
        if hasattr(signal, _sig):
            try:
                signal.signal(getattr(signal, _sig), lambda *a: (_stop_all(), os._exit(0)))
            except (ValueError, OSError):
                pass
    os.makedirs(USERS_DIR, exist_ok=True)
    threading.Thread(target=_autostart, daemon=True).start()
    threading.Thread(target=_reaper, daemon=True).start()
    threading.Thread(target=_watchdog, daemon=True).start()
    app.run(host=os.environ.get('BILIGO_GATEWAY_HOST', '0.0.0.0'), port=int(os.environ.get('PORT', 4999)), debug=False, threaded=True)
