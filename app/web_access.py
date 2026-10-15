"""Web 访问与鉴权：登录页、会话、用户管理（管理员）、使用时效。"""
import json
import os
import secrets
import threading
import time
from datetime import date, datetime, timedelta

_RUNTIME_TOKEN = None
_APP_DIR = os.path.dirname(os.path.abspath(__file__))
USERS_FILE = os.path.join(_APP_DIR, 'auth_users.json')
SECRET_FILE = os.path.join(_APP_DIR, '.auth_secret_key')
_lock = threading.RLock()
_fails = {}
_name_fails = {}
# 单企业部署：只允许这一个账号登录（无管理员/账号管理/使用时效）
ALLOWED_USER = os.environ.get('BILIGO_ONLY_USER', '13200090009')

PUBLIC_PATHS = {'/login', '/logout', '/api/auth/login', '/api/auth/me', '/favicon.ico'}
PUBLIC_EXT = ('.css', '.js', '.png', '.jpg', '.jpeg', '.gif', '.svg', '.ico', '.webp', '.woff', '.woff2', '.ttf', '.map')
# 通过静态路由一律禁止下载的敏感文件
BLOCKED_EXT = ('.py', '.pyc', '.json', '.sqlite3', '.db', '.log', '.txt', '.tgz', '.gz', '.bak', '.sh', '.md', '.ini', '.env')


def ensure_runtime_token() -> str:
    global _RUNTIME_TOKEN
    if not _RUNTIME_TOKEN:
        _RUNTIME_TOKEN = secrets.token_urlsafe(16)
    return _RUNTIME_TOKEN


def build_access_url(port: int, path: str = '') -> str:
    return f'http://127.0.0.1:{port}' + (path or '')


def open_browser_when_ready(url: str, delay: float = 1.0):
    import webbrowser

    def _open():
        time.sleep(delay)
        try:
            webbrowser.open(url)
        except Exception:
            pass

    threading.Thread(target=_open, daemon=True).start()


# ── 用户存储 ──

def _load_users() -> dict:
    with _lock:
        try:
            with open(USERS_FILE, 'r', encoding='utf-8') as f:
                data = json.load(f)
                return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}


def _save_users(users: dict) -> None:
    with _lock:
        tmp = USERS_FILE + '.tmp'
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(users, f, ensure_ascii=False, indent=2)
        os.chmod(tmp, 0o600)
        os.replace(tmp, USERS_FILE)


def _parse_expiry(value):
    """返回 'YYYY-MM-DD' 或 None（永久）。"""
    if value in (None, '', 'permanent', '永久'):
        return None
    d = datetime.strptime(str(value)[:10], '%Y-%m-%d').date()
    return d.isoformat()


def user_block_reason(user):
    if not user:
        return '账号不存在'
    if user.get('disabled'):
        return '账号已被停用，请联系管理员'
    return ''


def public_user(name: str, u: dict) -> dict:
    return {'username': name, 'display_name': u.get('display_name', ''), 'last_login': u.get('last_login', '')}


def _secret_key() -> str:
    try:
        with open(SECRET_FILE, 'r') as f:
            k = f.read().strip()
            if k:
                return k
    except OSError:
        pass
    k = secrets.token_hex(32)
    with open(SECRET_FILE, 'w') as f:
        f.write(k)
    os.chmod(SECRET_FILE, 0o600)
    return k


LOGIN_HTML = """<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>登录 - 客多多AI客服系统</title><style>
:root{--teal:#12a594;--teal-d:#0b7f72;--gold:#b58b34;--gold-l:#fff8e8;--line:#e2e7ed;--txt:#1f2d3d;--sub:#7a8796}
*{box-sizing:border-box}body{margin:0;min-height:100vh;display:flex;flex-direction:column;align-items:center;justify-content:center;padding:24px 16px;
background:radial-gradient(1200px 600px at 10% -10%,#dff5f1 0%,transparent 60%),radial-gradient(900px 500px at 110% 110%,#fbefd6 0%,transparent 55%),linear-gradient(135deg,#f5f7fa,#fafbfc);
font-family:-apple-system,BlinkMacSystemFont,"PingFang SC","Microsoft YaHei",sans-serif;color:var(--txt)}
.card{width:100%;max-width:400px;background:#fff;border:1px solid var(--line);border-radius:20px;padding:34px 32px 28px;box-shadow:0 18px 50px rgba(18,165,148,.12),0 2px 8px rgba(0,0,0,.04);position:relative;overflow:hidden}
.card:before{content:"";position:absolute;left:0;top:0;right:0;height:4px;background:linear-gradient(90deg,var(--teal),var(--gold))}
.brand{text-align:center}.brand h1{margin:0;font-size:24px;letter-spacing:1px;background:linear-gradient(90deg,var(--teal-d),var(--gold));-webkit-background-clip:text;background-clip:text;color:transparent}
.ver{display:inline-block;margin-top:8px;font-size:12px;color:var(--gold);background:var(--gold-l);border:1px solid #f1dfb5;border-radius:999px;padding:2px 10px}
.brand p{margin:10px 0 0;color:var(--sub);font-size:13px}
.plats{display:flex;justify-content:center;gap:12px;margin:20px 0 22px}
.plat{display:flex;align-items:center;gap:6px;padding:6px 10px;border:1px solid var(--line);border-radius:12px;background:#fafbfc;font-size:12px;color:#4a5866}
.plat img{width:20px;height:20px;border-radius:5px;object-fit:contain}.plat.first{border-color:#bfe8e2;background:#effaf8;color:var(--teal-d)}
.field{position:relative;margin-bottom:14px}.field svg{position:absolute;left:13px;top:50%;transform:translateY(-50%);width:18px;height:18px;stroke:#9aa7b4;fill:none;stroke-width:1.8}
.field input{width:100%;height:46px;padding:0 44px 0 42px;border:1px solid var(--line);border-radius:12px;font-size:15px;background:#fbfcfd;outline:none;transition:.2s}
.field input:focus{border-color:var(--teal);background:#fff;box-shadow:0 0 0 3px rgba(18,165,148,.14)}
.eye{position:absolute;right:8px;top:50%;transform:translateY(-50%);border:0;background:none;cursor:pointer;padding:6px;color:#9aa7b4;font-size:13px}
.row{display:flex;justify-content:space-between;align-items:center;font-size:13px;color:var(--sub);margin:4px 0 18px}
.row label{display:flex;align-items:center;gap:6px;cursor:pointer}.row input{accent-color:var(--teal)}
.btn{width:100%;height:46px;border:0;border-radius:12px;background:linear-gradient(135deg,var(--teal),var(--teal-d));color:#fff;font-size:16px;font-weight:600;cursor:pointer;box-shadow:0 8px 20px rgba(18,165,148,.25);display:flex;align-items:center;justify-content:center;gap:8px}
.btn:disabled{opacity:.75;cursor:wait}.spin{width:16px;height:16px;border:2px solid rgba(255,255,255,.5);border-top-color:#fff;border-radius:50%;animation:r .8s linear infinite;display:none}
.btn.loading .spin{display:inline-block}@keyframes r{to{transform:rotate(360deg)}}
.msg{display:none;margin-bottom:14px;padding:10px 12px;border-radius:10px;font-size:13px;line-height:1.5}
.msg.err{display:block;background:#fff1f0;border:1px solid #ffccc7;color:#cf1322}.msg.warn{display:block;background:var(--gold-l);border:1px solid #f1dfb5;color:#8a6420}.msg.info{display:block;background:#effaf8;border:1px solid #bfe8e2;color:var(--teal-d)}
footer{margin-top:22px;font-size:12px;color:#9aa7b4}
@media(max-width:480px){.card{padding:28px 20px 22px;border-radius:16px}.brand h1{font-size:21px}.plats{gap:6px}.plat{padding:5px 7px}}
</style></head><body>
<form class="card" id="f" novalidate>
<div class="brand"><h1>客多多AI客服系统</h1><div class="ver">V3.2 Ultra</div><p>多平台私信智能接待 · 自动回复 · 线索沉淀</p></div>
<div class="plats"><span class="plat first"><img src="/laike-icon.png" alt="">抖音来客</span><span class="plat"><img src="/tik-tok.png" alt="">抖音</span><span class="plat"><img src="/xiaohongshu-seeklogo.png" alt="">小红书</span></div>
<div class="msg" id="m"></div>
<div class="field"><svg viewBox="0 0 24 24"><circle cx="12" cy="8" r="4"/><path d="M4 21c0-4 4-6 8-6s8 2 8 6"/></svg><input id="u" name="username" placeholder="用户名" autocomplete="username" autofocus></div>
<div class="field"><svg viewBox="0 0 24 24"><rect x="5" y="11" width="14" height="10" rx="2"/><path d="M8 11V7a4 4 0 0 1 8 0v4"/></svg><input id="p" name="password" type="password" placeholder="密码" autocomplete="current-password"><button type="button" class="eye" id="eye">显示</button></div>
<div class="row"><label><input type="checkbox" id="rem" checked>记住我（7 天内免登录）</label><span>忘记密码请联系管理员</span></div>
<button class="btn" id="b" type="submit"><span class="spin"></span><span id="bt">登 录</span></button>
</form><footer>© 2026 客多多AI客服系统</footer>
<script>
const $=i=>document.getElementById(i),q=new URLSearchParams(location.search);
function show(t,k){const m=$('m');m.className='msg '+(k||'err');m.textContent=t;}
if(q.get('msg')){const t=q.get('msg');show(t,/退出/.test(t)?'info':(/到期|停用|失效/.test(t)?'warn':'info'));}
$('eye').onclick=()=>{const p=$('p');const h=p.type==='password';p.type=h?'text':'password';$('eye').textContent=h?'隐藏':'显示';};
$('f').onsubmit=async e=>{e.preventDefault();const u=$('u').value.trim(),p=$('p').value;
if(!u||!p){show('请输入用户名和密码');return;}
const b=$('b');b.disabled=true;b.classList.add('loading');$('bt').textContent='登录中…';
try{const r=await fetch('/api/auth/login',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({username:u,password:p,remember:$('rem').checked})});
let d={};try{d=await r.json();}catch(_){}
if(d.success){$('bt').textContent='登录成功，正在进入…';let n=q.get('next')||'/';if(!n.startsWith('/')||n.startsWith('//'))n='/';location.href=n;return;}
let t=d.error||'登录失败，请稍后重试';
if(r.status===429)t='登录尝试次数过多，已锁定 5 分钟，请稍后再试';
else if(r.status===401)t='用户名或密码错误，请检查后重试';
show(t,r.status===403?'warn':'err');if(r.status===401){$('p').value='';$('p').focus();}
}catch(err){show('网络异常，无法连接服务器，请稍后重试');}
b.disabled=false;b.classList.remove('loading');$('bt').textContent='登 录';};
</script></body></html>"""

BADGE_JS = """<script>(function(){
const of=window.fetch;window.fetch=async function(){const r=await of.apply(this,arguments);
if(r.status===401){try{const d=await r.clone().json();if(d&&d.auth_required){location.href='/login?msg='+encodeURIComponent(d.error||'请重新登录')+'&next='+encodeURIComponent(location.pathname+location.search);}}catch(e){}}return r;};
of('/api/auth/me').then(r=>r.json()).then(d=>{if(!d.username)return;const b=document.createElement('div');
b.style.cssText='position:fixed;right:12px;bottom:12px;z-index:99999;background:rgba(255,255,255,.95);border:1px solid #e3e8f0;border-radius:18px;padding:6px 12px;font-size:12px;color:#444;box-shadow:0 2px 10px rgba(0,0,0,.08)';
b.innerHTML='👤 '+d.username+' · <a href="#" id="kdd-cpw">修改密码</a> · <a href="/logout">退出</a>';document.body.appendChild(b);
document.getElementById('kdd-cpw').onclick=async e=>{e.preventDefault();const o=prompt('请输入当前密码');if(!o)return;const n=prompt('请输入新密码（至少 8 位）');if(!n)return;if(prompt('请再次输入新密码')!==n){alert('两次输入的新密码不一致');return;}
const r=await of('/api/auth/change-password',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({old_password:o,new_password:n})});const x=await r.json();alert(x.success?'密码已修改，其他设备上的登录将失效':(x.error||'修改失败'));};}).catch(()=>{});
})();</script>"""


USER_HOOKS = {'deleted': None, 'changed': None}  # 由网关设置：用户删除/变更时停止其后端


def _hook(kind, *args):
    fn = USER_HOOKS.get(kind)
    if fn:
        try:
            fn(*args)
        except Exception as exc:  # noqa: BLE001
            print('user hook failed:', kind, exc)


def new_uid() -> str:
    return 'u' + secrets.token_hex(5)


def register_backend_guard(app) -> None:
    """用户后端模式：只接受来自网关（带共享密钥）的请求，不做登录。"""
    from flask import request, Response
    secret = os.environ.get('BILIGO_BACKEND_SECRET', '')
    app.secret_key = secrets.token_hex(32)

    @app.before_request
    def _gateway_only():
        if not secret or not secrets.compare_digest(request.headers.get('X-Biligo-Gateway', ''), secret):
            return Response('Forbidden', 403)
        low = (request.path or '/').lower()
        base = os.path.basename(low)
        if not low.startswith('/api/') and (base.startswith('.') or low.endswith(BLOCKED_EXT)):
            return Response('Not Found', 404)
        return None


def register_web_auth(app) -> None:
    if os.environ.get('BILIGO_BACKEND_SECRET'):
        return register_backend_guard(app)
    from flask import jsonify, redirect, request, session, Response
    from werkzeug.security import check_password_hash, generate_password_hash

    app.secret_key = _secret_key()
    app.config.update(SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE='Lax',
                      SESSION_COOKIE_NAME='kdd_session', PERMANENT_SESSION_LIFETIME=timedelta(days=7))

    def _deny(msg, status=401):
        session.clear()
        if request.path.startswith('/api/'):
            return jsonify({'success': False, 'auth_required': True, 'error': msg}), status
        from urllib.parse import quote
        return redirect(f'/login?msg={quote(msg)}&next={quote(request.full_path.rstrip("?"))}')

    @app.before_request
    def _auth_guard():
        path = request.path or '/'
        low = path.lower()
        base = os.path.basename(low)
        if low == '/admin' or low.startswith('/admin/') or low.startswith('/api/admin'):
            return Response('Not Found', 404)  # 单企业部署：无管理后台
        if base.startswith('.') or low.endswith(BLOCKED_EXT) or base.startswith('auth_users'):
            if not low.startswith('/api/'):
                return Response('Not Found', 404)
        if path in PUBLIC_PATHS or low.endswith(PUBLIC_EXT):
            return None
        name = session.get('user')
        if not name:
            if path.startswith('/api/'):
                return jsonify({'success': False, 'auth_required': True, 'error': '请先登录'}), 401
            return _deny('请先登录') if path not in ('/', '') else redirect('/login')
        users = _load_users()
        u = users.get(name) if name == ALLOWED_USER else None
        reason = user_block_reason(u)
        if reason:
            return _deny(reason)
        if int(session.get('ver', 0)) != int(u.get('session_version', 1)):
            return _deny('登录已失效（密码或权限已变更），请重新登录')
        return None

    @app.after_request
    def _inject_badge(resp):
        try:
            if (resp.mimetype == 'text/html' and resp.status_code == 200 and session.get('user')
                    and request.path not in ('/login',)):
                resp.direct_passthrough = False
                body = resp.get_data(as_text=True)
                if '</body>' in body:
                    resp.set_data(body.replace('</body>', BADGE_JS + '</body>', 1))
        except Exception:
            pass
        return resp

    @app.route('/login')
    def auth_login_page():
        return Response(LOGIN_HTML, mimetype='text/html')

    @app.route('/logout')
    def auth_logout():
        session.clear()
        return redirect('/login?msg=' + '已退出登录')

    @app.route('/api/auth/login', methods=['POST'])
    def auth_login():
        # 只认 TCP 对端地址（网关直连公网，不信任 X-Forwarded-For / X-Real-IP 等可伪造头）
        ip = request.environ.get('REMOTE_ADDR') or ''
        now = time.time()
        data = request.get_json(silent=True) or {}
        name = str(data.get('username', '')).strip()
        pw = str(data.get('password', ''))
        with _lock:
            rec = [t for t in _fails.get(ip, []) if now - t < 300]
            nrec = [t for t in _name_fails.get(ALLOWED_USER, []) if now - t < 900]
            glob = [t for v in _fails.values() for t in v if now - t < 60]
        if len(rec) >= 8:
            return jsonify({'success': False, 'error': '尝试次数过多，请 5 分钟后再试'}), 429
        if len(nrec) >= 20 or len(glob) >= 30:
            # 账号级/全局限速：换 IP 也无法无限尝试
            return jsonify({'success': False, 'error': '尝试次数过多，请稍后再试'}), 429
        users = _load_users()
        u = users.get(name) if name == ALLOWED_USER else None
        if not u or not check_password_hash(u.get('password_hash', ''), pw):
            with _lock:
                rec.append(now)
                _fails[ip] = rec
                nrec.append(now)
                _name_fails[ALLOWED_USER] = nrec
                if len(_fails) > 5000:
                    for k in [k for k, v in _fails.items() if not v or now - v[-1] > 300]:
                        _fails.pop(k, None)
            time.sleep(0.5)
            return jsonify({'success': False, 'error': '用户名或密码错误'}), 401
        reason = user_block_reason(u)
        if reason:
            return jsonify({'success': False, 'error': reason}), 403
        _fails.pop(ip, None)
        _name_fails.pop(ALLOWED_USER, None)
        session.clear()
        session.permanent = data.get('remember', True) is not False
        session['user'] = name
        session['ver'] = int(u.get('session_version', 1))
        u['last_login'] = datetime.now().isoformat(timespec='seconds')
        _save_users(users)
        return jsonify({'success': True, 'user': public_user(name, u)})

    @app.route('/api/auth/me')
    def auth_me():
        name = session.get('user')
        u = _load_users().get(name) if name == ALLOWED_USER else None
        if not u:
            return jsonify({})
        return jsonify(public_user(name, u))

    @app.route('/api/auth/change-password', methods=['POST'])
    def auth_change_password():
        name = session.get('user')
        data = request.get_json(silent=True) or {}
        users = _load_users()
        u = users.get(name) if name == ALLOWED_USER else None
        if not u or int(session.get('ver', 0)) != int(u.get('session_version', 1)):
            return jsonify({'success': False, 'auth_required': True, 'error': '请先登录'}), 401
        if not check_password_hash(u.get('password_hash', ''), str(data.get('old_password', ''))):
            return jsonify({'success': False, 'error': '当前密码不正确'}), 400
        new = str(data.get('new_password', ''))
        if len(new) < 8:
            return jsonify({'success': False, 'error': '新密码至少 8 位'}), 400
        u['password_hash'] = generate_password_hash(new)
        u['session_version'] = int(u.get('session_version', 1)) + 1
        _save_users(users)
        session['ver'] = u['session_version']
        return jsonify({'success': True})



