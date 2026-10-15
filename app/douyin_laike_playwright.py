"""抖音来客商家后台私信 — Playwright 浏览器自动化。
独立于 douyin_playwright.py（个人版 douyin.com），专门处理 life.douyin.com 商家后台的会话。

状态：选择器已按 2026-10-12 云端浏览器实测 DOM 校准。
"""
from __future__ import annotations

import logging
import os
import sys
import queue
import re
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger(__name__)

# 抖音来客商家后台
LAIKE_HOME = 'https://life.douyin.com'
# 私信会话页（groupid 等参数按实际账号填充）
LAIKE_CHAT_URL_TEMPLATE = (
    'https://life.douyin.com/p/liteapp/leads_cs/chat/session'
    '?groupid={groupid}&life_biz_view_id={biz_view_id}&life_account_biz_ids='
)
LOGIN_TIMEOUT = 300
LAIKE_CHAT_BASE = 'https://life.douyin.com/p/liteapp/leads_cs/chat/session'
LAIKE_LOGIN_URL = 'https://life.douyin.com/p/login'
# 抖音系登录态 Cookie（任意一个非空即视为已登录）
# 来客后台登录后 Cookie 带 _ls 后缀（如 sessionid_ls、sid_tt_ls，域 .life.douyin.com）
SESSION_COOKIE_NAMES = ('sessionid', 'sessionid_ss', 'sid_tt', 'sid_guard',
                        'sessionid_ls', 'sessionid_ss_ls', 'sid_tt_ls', 'sid_guard_ls')
# 选择器兜底：CSS Modules 哈希类名会随前端发版变化，用前缀匹配兜底
SEL_CONV_ITEM = '.conversationItem-RaXg9G, [class*="conversationItem-"]'
SEL_CONV_NAME = '.conversationName-DE7XW9, [class*="conversationName-"]'
# 会话项定位：精确 → 归一化（NFKC、去空白/零宽/变体符）→ 前缀（列表昵称截断 30 字）
FIND_CONV_JS = r"""([itemSel, nameSel, nick]) => {
    const norm = s => (s || '').normalize('NFKC').replace(/[\s\u200b-\u200f\u2060\ufe0e\ufe0f]/g, '');
    const items = Array.from(document.querySelectorAll(itemSel));
    const names = items.map(el => { const n = el.querySelector(nameSel); return ((n ? n.innerText : '') || '').trim(); });
    let i = names.findIndex(t => t === nick);
    if (i < 0) { const k = norm(nick); i = names.findIndex(t => t && norm(t) === k); }
    if (i < 0) { const k = norm(nick); i = names.findIndex(t => { const v = norm(t); return v && (v.startsWith(k) || k.startsWith(v)); }); }
    return i;
}"""
# 记录会话对方的抖音账号信息（getDouyinUserInfoByUid 接口：IndustryCode>0 为企业号/蓝V）
PEER_HOOK_JS = r"""(() => { if (window.__peerHook) return; window.__peerHook = 1; window.__peerInfo = window.__peerInfo || {};
  const keep = (u, t) => { try { if (!/getDouyinUserInfoByUid/.test(String(u))) return; const d = JSON.parse(t); const ui = (d.data || {}).UserInfo || {};
    Object.values(ui).forEach(x => { if (x && x.NickName) window.__peerInfo[x.NickName] = {industry: Number(x.IndustryCode || 0), uid: String(x.DouyinUid || '')}; }); } catch (e) {} };
  const of = window.fetch; if (of) window.fetch = function (...a) { return of.apply(this, a).then(r => { try { const u = (a[0] && a[0].url) || a[0]; if (/getDouyinUserInfoByUid/.test(String(u))) r.clone().text().then(t => keep(u, t)); } catch (e) {} return r; }); };
  const oo = XMLHttpRequest.prototype.open, os = XMLHttpRequest.prototype.send;
  XMLHttpRequest.prototype.open = function (m, u) { this.__pu = u; return oo.apply(this, arguments); };
  XMLHttpRequest.prototype.send = function () { this.addEventListener('load', () => { try { if (/getDouyinUserInfoByUid/.test(String(this.__pu))) keep(this.__pu, (!this.responseType || this.responseType === 'text') ? this.responseText : JSON.stringify(this.response)); } catch (e) {} }); return os.apply(this, arguments); };
})()"""
SEL_MSG_EDITOR = '.msgEditor-hzp4al, [class*="msgEditor-"]'


@dataclass
class LaikeConversation:
    """抖音来客会话。"""
    conv_id: str = ''
    nickname: str = ''
    preview: str = ''
    unread: int = 0
    has_lead: bool = False  # 是否已留资（右侧关联线索）
    last_time: str = ''
    extra: Dict[str, Any] = field(default_factory=dict)


@dataclass
class LaikeMessage:
    """单条消息。"""
    text: str = ''
    is_self: bool = False
    sender: str = ''
    msg_id: str = ''
    auto: bool = False  # 平台自动发送（此消息由「自动回复配置/自动挽留/欢迎语」发送）


class DouyinLaikeWorker:
    """在单一线程内运行 Playwright，操作抖音来客后台。"""

    PROFILE_DIR_NAME = 'douyin_laike_browser_profile'

    def __init__(self, storage_path: str, headless: bool = True, profile_dir: Optional[str] = None):
        self.storage_path = storage_path
        self.profile_dir = os.path.abspath(profile_dir) if profile_dir else ''
        self.headless = headless
        self._abort = False
        self._queue: queue.Queue = queue.Queue()
        self._thread: Optional[threading.Thread] = None
        self._lifecycle_lock = threading.RLock()
        self._ready = threading.Event()
        self._owner_thread_id: Optional[int] = None
        self._running = False
        self._playwright = None
        self._context = None
        self._page = None
        # 来客后台参数（groupid 等，登录后从 URL 解析或手动配置）
        self._groupid: str = ''
        self._biz_view_id: str = ''
        # WebSocket / IM 接口触发器（参照 douyin_playwright.setup_im_websocket_trigger）
        self._im_callback: Optional[Callable[[str], None]] = None
        self._ws_pages: set = set()
        self.ws_urls: List[str] = []
        self.ws_open = 0
        self.ws_frames = 0

    # ── 线程生命周期（与 DouyinBrowserWorker 同模式） ──

    def start_worker(self) -> None:
        with self._lifecycle_lock:
            if self._thread and self._thread.is_alive():
                return
            self._queue = queue.Queue()
            self._running = True
            self._ready.clear()
            owner = threading.Thread(target=self._worker_main, daemon=True)
            self._thread = owner
            owner.start()
            self._ready.wait(timeout=10)
            if self._ready.is_set() and self._running and owner.is_alive():
                time.sleep(0.1)

    def stop_worker(self) -> None:
        if self._thread is threading.current_thread():
            self._running = False
            return
        with self._lifecycle_lock:
            self._running = False
            try:
                self._queue.put({'op': 'shutdown'})
            except Exception:
                pass

    def _call(self, op: str, timeout: float = 30, **kwargs):
        if not self._thread or not self._thread.is_alive():
            self.start_worker()
        resp_q: queue.Queue = queue.Queue()
        cancelled = threading.Event()
        self._queue.put({
            'op': op,
            'kwargs': kwargs,
            'resp_q': resp_q,
            'cancelled': cancelled,
        })
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                cancelled.set()
                _nk = kwargs.get('nickname')
                raise TimeoutError(f'来客浏览器操作超时: {op}' + (f' [{_nk}]' if _nk else ''))
            try:
                result = resp_q.get(timeout=min(0.5, remaining))
                break
            except queue.Empty:
                if not self._thread or not self._thread.is_alive():
                    cancelled.set()
                    raise RuntimeError(f'来客浏览器工作线程已退出: {op}')
        if isinstance(result, dict) and result.get('error'):
            raise RuntimeError(result['error'])
        return result

    def _worker_main(self) -> None:
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            logger.error('未安装 playwright')
            self._running = False
            self._ready.set()
            return
        try:
            self._owner_thread_id = threading.get_ident()
            with sync_playwright() as playwright:
                self._playwright = playwright
                self._ready.set()
                while self._running:
                    try:
                        job = self._queue.get(timeout=0.5)
                    except queue.Empty:
                        continue
                    op = job.get('op')
                    resp_q = job.get('resp_q')
                    kwargs = job.get('kwargs') or {}
                    cancelled = job.get('cancelled')
                    try:
                        if cancelled is not None and cancelled.is_set():
                            if resp_q:
                                resp_q.put({'error': f'操作已过期: {op}'})
                            continue
                        if op == 'shutdown':
                            self._cleanup_browser()
                            if resp_q:
                                resp_q.put({'ok': True})
                            break
                        result = self._dispatch(op, **kwargs)
                        if resp_q:
                            resp_q.put(result)
                    except Exception as exc:
                        logger.exception('Laike worker op=%s failed', op)
                        if resp_q:
                            resp_q.put({'error': str(exc)})
        except BaseException:
            logger.exception('Laike browser worker terminated unexpectedly')
        finally:
            self._ready.set()
            self._running = False
            self._cleanup_browser()
            self._playwright = None
            self._owner_thread_id = None

    def _dispatch(self, op: str, **kwargs):
        if self._owner_thread_id != threading.get_ident():
            raise RuntimeError('Playwright 操作必须在所属工作线程中执行')
        handlers = {
            'navigate_chat': lambda: self._op_navigate_chat(),
            'clear_cache_reload': lambda: self._op_clear_cache_reload(),
            'list_conversations': lambda: self._op_list_conversations(),
            'open_conversation': lambda: self._op_open_conversation(
                kwargs.get('nickname', ''),
            ),
            'read_messages': lambda: self._op_read_messages(
                kwargs.get('limit', 20),
            ),
            'send_text': lambda: self._op_send_text(
                kwargs.get('nickname', ''),
                kwargs.get('text', ''),
            ),
            'ensure_takeover': lambda: self._op_ensure_takeover(),
            'toolbar_probe': lambda: self._op_toolbar_probe(),
            'send_lead_card': lambda: self._op_send_lead_card(kwargs.get('nickname', '')),
            'takeover_probe': lambda: self._takeover_state(detail=True),
            'page_exec': lambda: self._op_page_exec(**kwargs),
            'peer_info': lambda: self._op_peer_info(),
            'get_lead_info': lambda: self._op_get_lead_info(),
            'get_account_info': lambda: self._op_get_account_info(),
            'list_merchant_accounts': lambda: self._op_list_merchant_accounts(),
            'get_page_debug': lambda: self._op_get_page_debug(), 
            'check_session': lambda: self._op_check_session(),
            'open_home': lambda: self._op_open_home(),
            'take_screenshot': lambda: self._op_take_screenshot(kwargs.get('path', '')),
            # ── 登录（无头，短信验证码 / 密码 / 远程点击验证码）──
            'login_open': lambda: self._op_login_open(),
            'login_send_code': lambda: self._op_login_send_code(kwargs.get('phone', '')),
            'login_submit_code': lambda: self._op_login_submit_code(kwargs.get('code', '')),
            'login_password': lambda: self._op_login_password(
                kwargs.get('account', ''), kwargs.get('password', ''),
            ),
            'login_snapshot': lambda: self._op_login_snapshot(),
            'login_mouse': lambda: self._op_login_mouse(**kwargs),
            'login_status': lambda: self._op_login_status(),
            'import_cookies': lambda: self._op_import_cookies(kwargs.get('cookies') or []),
            'detect_chat_params': lambda: self._op_detect_chat_params(),
            'debug_info': lambda: self._op_debug_info(),
            'detect_account': lambda: self._op_detect_account(kwargs.get('navigate', True), kwargs.get('with_stores', True)),
            'select_store': lambda: self._op_select_store(kwargs.get('name', '')),
            'logout_clear': lambda: self._op_logout_clear(),
            'probe_editor': lambda: self._op_probe_editor(),
        }
        handler = handlers.get(op)
        if handler is None:
            raise RuntimeError(f'未知操作: {op}')
        return handler()

    # ── 对外接口 ──

    def set_chat_params(self, groupid: str, biz_view_id: str) -> None:
        self._groupid = groupid
        self._biz_view_id = biz_view_id

    def clear_cache_reload(self) -> None:
        """清浏览器 HTTP 缓存 + 回收 JS 内存后重开会话页（保留 cookie/登录）。"""
        self._call('clear_cache_reload', timeout=90)

    def navigate_chat(self) -> None:
        self._call('navigate_chat', timeout=60)

    def list_conversations(self) -> List[LaikeConversation]:
        rows = self._call('list_conversations', timeout=45) or []
        return [LaikeConversation(**r) for r in rows]

    def open_conversation(self, nickname: str) -> bool:
        r = self._call('open_conversation', timeout=30, nickname=nickname)
        return bool(r.get('ok'))

    def read_messages(self, limit: int = 20) -> List[LaikeMessage]:
        rows = self._call('read_messages', timeout=30, limit=limit) or []
        return [LaikeMessage(**r) for r in rows]

    def send_text(self, nickname: str, text: str) -> bool:
        r = self._call('send_text', timeout=60, nickname=nickname, text=text)
        return bool(r.get('ok'))

    def _op_list_merchant_accounts(self) -> Dict[str, Any]:
        """探测账号切换器，列出所有可用的商家账号。"""
        self._ensure_browser()
        # 先确保在会话页
        try:
            url = self._page.url or ''
            if 'leads_cs/chat' not in url:
                self._op_navigate_chat()
                time.sleep(3)
        except Exception:
            pass
        # 尝试多种方式获取账号名
        js_detect = """
        () => {
            const result = {account_name: '', candidates: [], url: location.href};
            // 方式1：找页面顶部账号选择器
            const topBar = document.body.innerText.slice(0, 2000);
            result.body_start = topBar.slice(0, 300);
            // 方式2：找所有可能的下拉触发器
            const triggers = [];
            document.querySelectorAll('*').forEach(el => {
                const text = (el.innerText || '').trim();
                if (text.length < 30 && text.length > 2) {
                    const cls = el.className || '';
                    if (typeof cls === 'string' && (cls.includes('account') || cls.includes('Account') || cls.includes('select') || cls.includes('Select'))) {
                        triggers.push({text: text.slice(0, 40), tag: el.tagName, cls: cls.slice(0, 60)});
                    }
                }
            });
            result.candidates = triggers.slice(0, 10);
            return result;
        }
        """
        try:
            detect = self._page.evaluate(js_detect) or {}
            logger.info('账号探测: %s', detect)
        except Exception as exc:
            return {'accounts': [], 'error': f'页面读取失败: {exc}'}

        # 如果页面显示未登录，返回明确错误
        body_start = detect.get('body_start', '')
        if '登录' in body_start and '账号' not in body_start:
            return {'accounts': [], 'error': '页面显示未登录，请先完成登录'}

        accounts = []
        # 尝试从 URL 直接获取当前账号的 groupid（至少返回当前这一个）
        try:
            import re as _re
            url = self._page.url or ''
            m = _re.search(r'groupid=(\d+)', url)
            groupid = m.group(1) if m else ''
            m2 = _re.search(r'life_biz_view_id=(\d+)', url)
            biz_view = m2.group(1) if m2 else ''
            if groupid:
                # 尝试获取账号名
                name_js = """
                () => {
                    // 从页面顶部找账号名
                    const els = document.querySelectorAll('*');
                    for (const el of els) {
                        const text = (el.innerText || '').trim();
                        if (text.includes('盛世百花') || text.includes('礼宴')) {
                            return text.split('\n')[0].slice(0, 40);
                        }
                    }
                    return '';
                }
                """
                try:
                    acc_name = self._page.evaluate(name_js) or ''
                except Exception:
                    acc_name = ''
                accounts.append({
                    'name': acc_name or f'账号_{groupid[-6:]}',
                    'groupid': groupid,
                    'biz_view_id': biz_view,
                })
        except Exception as exc:
            logger.warning('获取当前账号失败: %s', exc)

        if not accounts:
            return {'accounts': [], 'error': f'未找到账号切换器。页面内容: {body_start[:100]}'}
        return {'accounts': accounts, 'note': '仅检测到当前账号，多账号切换需手动操作'}

    def _op_get_page_debug(self) -> Dict[str, Any]:
        """返回当前页面调试信息。"""
        self._ensure_browser()
        try:
            js = """
            () => ({
                url: location.href,
                title: document.title,
                div_count: document.querySelectorAll('div').length,
                body_len: document.body.innerText.length,
                body_start: document.body.innerText.slice(0, 200),
                has_conv_hint: document.body.innerText.includes('会话') || document.body.innerText.includes('私信'),
            })
            """
            return self._page.evaluate(js) or {}
        except Exception as exc:
            return {'error': str(exc)}

    def _op_take_screenshot(self, path: str = '') -> Dict[str, Any]:
        self._ensure_browser()
        try:
            import os
            if not path:
                path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'laike_qr.png')
            self._page.screenshot(path=path)
            return {'ok': True, 'path': path}
        except Exception as exc:
            return {'ok': False, 'error': str(exc)}

    def take_screenshot(self, path: str = '', timeout: int = 30) -> Dict[str, Any]:
        return self._call('take_screenshot', timeout=timeout, path=path)

    def _op_get_account_info(self) -> Dict[str, Any]:
        """获取当前登录的商家账号信息。"""
        self._ensure_browser()
        js = """
        () => {
            const out = {account_name: '', groupid: '', biz_view_id: '', url: location.href};
            // 从 URL 解析参数
            const params = new URLSearchParams(location.search);
            out.groupid = params.get('groupid') || '';
            out.biz_view_id = params.get('life_biz_view_id') || '';
            // 尝试从页面顶部账号选择器获取账号名
            const selectors = [
                '[class*="account-select"]', '[class*="AccountSelect"]',
                '[class*="merchant-name"]', '[class*="shop-name"]',
            ];
            for (const sel of selectors) {
                const el = document.querySelector(sel);
                if (el && el.textContent.trim()) {
                    out.account_name = el.textContent.trim().slice(0, 50);
                    break;
                }
            }
            // 兜底：从页面标题或特定位置找
            if (!out.account_name) {
                const titleEl = document.querySelector('header [class*="name"], [class*="brand"]');
                if (titleEl) out.account_name = titleEl.textContent.trim().slice(0, 50);
            }
            return out;
        }
        """
        try:
            return self._page.evaluate(js) or {}
        except Exception as exc:
            return {'error': str(exc)}

    def get_lead_info(self) -> Dict[str, Any]:
        """读取右侧关联线索面板（手机号等）。"""
        return self._call('get_lead_info', timeout=20) or {}

    def get_account_info(self) -> Dict[str, Any]:
        r = self._call('get_account_info', timeout=20)
        return r or {}

    def list_merchant_accounts(self) -> Dict[str, Any]:
        """列出所有可用的商家账号（较慢，需逐个切换）。"""
        r = self._call('list_merchant_accounts', timeout=120)
        return r or {}

    def get_page_debug(self) -> Dict[str, Any]:
        """获取当前页面调试信息。"""
        r = self._call('get_page_debug', timeout=15)
        return r or {}

    def check_session(self) -> bool:
        r = self._call('check_session', timeout=20)
        return bool(r.get('ok'))

    def login_call(self, op: str, timeout: float = 60, **kwargs) -> Dict[str, Any]:
        """登录相关操作统一入口（login_open/login_send_code/...）。"""
        return self._call(op, timeout=timeout, **kwargs) or {}

    # ── 内部实现 ──

    def _cleanup_browser(self) -> None:
        try:
            if self._context:
                self._context.close()
        except Exception:
            pass
        self._context = None
        self._page = None

    def _ensure_browser(self) -> None:
        """确保浏览器已启动并打开来客后台。"""
        if self._page is not None:
            try:
                if not self._page.is_closed():
                    return
            except Exception:
                pass
        pw = self._playwright
        profile_dir = self.profile_dir or os.path.join(
            os.path.dirname(os.path.abspath(self.storage_path)),
            self.PROFILE_DIR_NAME,
        )
        profile_dir = os.path.abspath(profile_dir)
        self.profile_dir = profile_dir
        os.makedirs(profile_dir, exist_ok=True)
        self._context = pw.chromium.launch_persistent_context(
            user_data_dir=profile_dir,
            headless=self.headless,
            viewport={'width': 1440, 'height': 900},
            locale='zh-CN',
            args=[
                '--disable-blink-features=AutomationControlled',
                # 服务器以 root 运行，Chromium 必须禁用 sandbox（仅 Linux）
            ] + (['--no-sandbox', '--disable-dev-shm-usage'] if sys.platform.startswith('linux') else []),
        )
        pages = list(self._context.pages)
        self._page = pages[0] if pages else self._context.new_page()
        for extra in pages[1:]:
            try:
                extra.close()
            except Exception:
                pass
        self._attach_im_listeners(self._page)
        # 登录通过 login_* 操作（无头短信验证码/密码）完成，会话保存在持久化 profile 中

    def _chat_url(self) -> str:
        if self._groupid and self._biz_view_id:
            return LAIKE_CHAT_URL_TEMPLATE.format(
                groupid=self._groupid, biz_view_id=self._biz_view_id,
            )
        # 未配置 groupid 时直接进入会话页，由来客前端自动补全当前账号参数
        return LAIKE_CHAT_BASE

    def _op_open_home(self) -> Dict[str, Any]:
        """打开来客首页（不预设 groupid，供用户手动导航）。"""
        self._ensure_browser()
        try:
            self._page.goto(LAIKE_HOME, wait_until='domcontentloaded', timeout=30000)
            time.sleep(2)
            return {'ok': True, 'url': self._page.url}
        except Exception as exc:
            return {'ok': False, 'error': str(exc)}

    def _has_session_cookie(self) -> bool:
        try:
            for c in self._context.cookies():
                if c.get('name') in SESSION_COOKIE_NAMES and c.get('value') and 'douyin.com' in (c.get('domain') or ''):
                    return True
        except Exception:
            pass
        return False

    def _op_probe_editor(self) -> Dict[str, Any]:
        sel = 'textarea[placeholder*="回复内容"], .msgEditor-hzp4al textarea, [class*="msgEditor-"] textarea'
        fr = self._frame_with(sel)
        ta = fr.locator(sel)
        btn = fr.locator('button').filter(has_text='发送')
        return {'ok': True, 'textarea': ta.count(),
                'placeholder': ta.first.get_attribute('placeholder') if ta.count() else '',
                'send_buttons': [b.inner_text() for b in btn.all()][:3]}

    _ACCOUNT_JS = r"""() => {
        const vis = e => { const r = e.getBoundingClientRect(); return r.width > 0 && r.height > 0; };
        const p = new URLSearchParams(location.search);
        const out = {url: location.href, groupid: p.get('groupid') || '', biz_view_id: p.get('life_biz_view_id') || '',
                     store_name: '', user_name: ''};
        // 「账号 [头像] 门店名 ▾」选择器：找文字恰为「账号」的标签，取相邻元素文本
        const label = Array.from(document.querySelectorAll('div,span,label')).find(e => vis(e) && e.children.length === 0 && (e.innerText || '').trim() === '账号');
        if (label) {
            let sib = label.nextElementSibling || (label.parentElement && label.parentElement.nextElementSibling);
            let n = 0;
            while (sib && !(sib.innerText || '').trim() && n++ < 3) sib = sib.nextElementSibling;
            if (sib) out.store_name = (sib.innerText || '').trim().split('\n')[0].slice(0, 60);
        }
        // 右上角登录用户名
        const head = Array.from(document.querySelectorAll('[class*="user" i] [class*="name" i], [class*="account" i] [class*="name" i], [class*="avatar" i] + div'))
            .filter(vis).map(e => (e.innerText || '').trim().split('\n')[0]).filter(t => t && t.length <= 20);
        if (head.length) out.user_name = head[0];
        return out;
    }"""

    def _store_selector(self):
        return self._page.locator('xpath=//*[normalize-space(text())="账号"]/following-sibling::*[1]').first

    def _list_store_options(self) -> List[str]:
        names: List[str] = []
        try:
            sel = self._store_selector()
            if sel.count() == 0:
                return names
            self._robust_click(sel)
            time.sleep(1.2)
            names = self._page.evaluate("""() => {
                const vis = e => { const r = e.getBoundingClientRect(); return r.width > 0 && r.height > 0; };
                return Array.from(document.querySelectorAll('.byted-select-option, [role="option"], [class*="select-option" i], [class*="dropdown" i] li, .byted-popover [class*="item" i]'))
                    .filter(vis).map(e => (e.innerText || '').trim().split('\\n')[0]).filter(Boolean).slice(0, 50);
            }""") or []
        except Exception as exc:
            logger.warning('读取门店列表失败: %s', exc)
        finally:
            try:
                self._page.keyboard.press('Escape')
            except Exception:
                pass
        seen, out = set(), []
        for n in names:
            if n not in seen:
                seen.add(n)
                out.append(n)
        return out

    def _op_detect_account(self, navigate: bool = True, with_stores: bool = True) -> Dict[str, Any]:
        """识别当前登录账号：进入会话页（不带旧 groupid，由来客自动补全），解析 URL 与账号选择器。"""
        self._ensure_browser()
        if navigate:
            try:
                self._page.goto(LAIKE_CHAT_BASE, wait_until='domcontentloaded', timeout=30000)
            except Exception:
                pass
            for _ in range(15):
                time.sleep(1)
                if 'groupid=' in (self._page.url or ''):
                    break
            time.sleep(2)
            self._dismiss_overlays()
        info = self._page.evaluate(self._ACCOUNT_JS) or {}
        info['stores'] = self._list_store_options() if with_stores else []
        if info.get('groupid'):
            self._groupid = info['groupid']
            self._biz_view_id = info.get('biz_view_id', '')
        info['ok'] = True
        return info

    def _op_select_store(self, name: str) -> Dict[str, Any]:
        self._ensure_browser()
        if 'leads_cs/chat' not in (self._page.url or ''):
            self._op_detect_account(True, False)
        sel = self._store_selector()
        self._robust_click(sel)
        time.sleep(1.2)
        opt = self._page.locator('.byted-select-option, [role="option"], [class*="select-option" i], [class*="dropdown" i] li').filter(has_text=name).first
        self._robust_click(opt)
        time.sleep(4)
        self._dismiss_overlays()
        return self._op_detect_account(False, False)

    def _op_logout_clear(self) -> Dict[str, Any]:
        """退出来客：先走站点 logout，再清空 Cookie 与本地存储（本 profile 仅用于来客）。"""
        self._ensure_browser()
        try:
            self._page.goto('https://life.douyin.com/passport/web/logout/?next=https%3A%2F%2Flife.douyin.com%2Fp%2Flogin',
                            wait_until='domcontentloaded', timeout=20000)
            time.sleep(2)
        except Exception:
            pass
        try:
            self._page.evaluate("() => { try { localStorage.clear(); sessionStorage.clear(); } catch (e) {} }")
        except Exception:
            pass
        try:
            self._context.clear_cookies()
        except Exception as exc:
            logger.warning('清除 Cookie 失败: %s', exc)
        self._groupid = ''
        self._biz_view_id = ''
        return {'ok': True, 'has_session': self._has_session_cookie()}

    def _op_debug_info(self) -> Dict[str, Any]:
        """调试：列出 Cookie 名/域（不含值）与当前标签页 URL。"""
        self._ensure_browser()
        ck = [{'name': c.get('name'), 'domain': c.get('domain'), 'has_value': bool(c.get('value'))}
              for c in self._context.cookies()]
        return {'ok': True, 'cookies': ck, 'pages': [p.url for p in self._context.pages],
                'current': self._page.url if self._page else ''}

    def set_im_callback(self, cb: Optional[Callable[[str], None]]) -> None:
        """设置 IM 活动回调（在 worker 线程中被调用，回调内只能 set Event，不得操作页面）。"""
        self._im_callback = cb

    def _fire_im(self, source: str) -> None:
        cb = self._im_callback
        if cb:
            try:
                cb(source)
            except Exception:
                pass

    def _attach_im_listeners(self, page) -> None:
        """给页面挂 websocket / IM 接口响应监听；只做触发器，不解析帧内容（帧多为 protobuf）。"""
        if page is None or id(page) in self._ws_pages:
            return
        self._ws_pages.add(id(page))

        def _on_ws(ws):
            try:
                url = ws.url or ''
            except Exception:
                url = ''
            if url and url not in self.ws_urls:
                self.ws_urls.append(url)
                self.ws_urls = self.ws_urls[-10:]
                logger.warning('来客页面 WebSocket: %s', url[:160])
            self.ws_open += 1

            def _on_frame(_payload):
                self.ws_frames += 1
                self._fire_im('ws')

            def _on_close(_ws=None):
                self.ws_open = max(0, self.ws_open - 1)

            try:
                ws.on('framereceived', _on_frame)
                ws.on('close', _on_close)
            except Exception:
                pass

        im_re = re.compile(r'/(im|imapi|message|msg|conversation|chat)[/_?]', re.I)

        def _on_response(resp):
            try:
                url = resp.url or ''
                rt = resp.request.resource_type
            except Exception:
                return
            if rt in ('xhr', 'fetch', 'eventsource') and 'life.douyin.com' in url and im_re.search(url):
                self._fire_im('http')

        try:
            page.add_init_script(PEER_HOOK_JS)
            page.evaluate(PEER_HOOK_JS)
        except Exception as exc:
            logger.warning('挂载对方账号信息钩子失败: %s', exc)
        try:
            page.on('websocket', _on_ws)
            page.on('response', _on_response)
        except Exception as exc:
            logger.warning('挂载 IM 监听失败: %s', exc)

    def _op_check_session(self) -> Dict[str, Any]:
        """以 Cookie 为主判断登录态，不离开当前页面（避免打断监控中的会话页）。"""
        self._ensure_browser()
        has_cookie = self._has_session_cookie()
        url = ''
        try:
            url = self._page.url or ''
        except Exception:
            pass
        if not has_cookie:
            return {'ok': False, 'url': url, 'reason': 'no_session_cookie'}
        # 当前在登录页 → 会话无效
        if '/p/login' in url or 'passport' in url:
            return {'ok': False, 'url': url, 'reason': 'on_login_page'}
        return {'ok': True, 'url': url}

    def _op_clear_cache_reload(self) -> Dict[str, Any]:
        self._ensure_browser()
        try:
            cdp = self._context.new_cdp_session(self._page)
            try:
                cdp.send('Network.clearBrowserCache')
                try:
                    cdp.send('HeapProfiler.collectGarbage')
                except Exception:
                    pass
            finally:
                try:
                    cdp.detach()
                except Exception:
                    pass
        except Exception as exc:
            logger.warning('清理来客浏览器缓存失败: %s', exc)
        try:
            self._page.goto('about:blank')
        except Exception:
            pass
        return self._op_navigate_chat()

    def _op_navigate_chat(self) -> Dict[str, Any]:
        self._cur_open = ''
        self._ensure_browser()
        # 新开标签页进会话地址（避免 SPA 路由问题）
        try:
            ctx = self._page.context
            # 关闭其他标签页
            for p in ctx.pages:
                if p != self._page:
                    try:
                        p.close()
                    except Exception:
                        pass
            # 在新标签页打开会话地址
            new_page = ctx.new_page()
            self._attach_im_listeners(new_page)
            new_page.goto(self._chat_url(), wait_until='domcontentloaded', timeout=30000)
            time.sleep(5)
            # 切换到新页面
            try:
                self._page.close()
            except Exception:
                pass
            self._page = new_page
            logger.warning('已切换到新标签页: %s', (self._page.url or '')[:80])
        except Exception as exc:
            logger.warning('新标签页导航失败: %s', exc)
        # 等待会话列表项渲染（最多 10 秒，搜索所有 frame）
        for _ in range(10):
            try:
                n = 0
                for fr in self._page.frames:
                    try:
                        c = fr.evaluate("document.querySelectorAll('.conversationItem-RaXg9G, [class*=\"conversationItem-\"]').length") or 0
                        n += c
                    except Exception:
                        pass
                if n > 0:
                    break
            except Exception:
                pass
            time.sleep(1)
        time.sleep(1)
        try:
            self._dismiss_overlays()
        except Exception:
            pass
        return {'ok': True, 'url': self._page.url}

    def _op_list_conversations(self) -> List[Dict[str, Any]]:
        """解析左侧会话列表（选择器经 Codex 实测校准 v2）。"""
        self._ensure_browser()
        js = r"""
        async () => {
            // 虚拟列表只渲染可见的 6~7 条：逐段滚动收集全部会话，最后滚回顶部
            const seen = new Map();
            const collect = () => document.querySelectorAll('.conversationItem-RaXg9G, [class*="conversationItem-"]').forEach(el => {
                const nameEl = el.querySelector('.conversationName-DE7XW9, [class*="conversationName-"]');
                const previewEl = el.querySelector('.csUI-MessageAbstract');
                const timeEl = el.querySelector('.text-LrxJpI');
                const unreadEl = el.querySelector('.byted-badge-text');
                const nickname = (nameEl ? nameEl.innerText : '').trim().slice(0, 30);
                if (!nickname || seen.has(nickname)) return;
                let unread = 0;
                if (unreadEl) {
                    const n = parseInt((unreadEl.innerText || '').trim(), 10);
                    if (!isNaN(n)) unread = n;
                }
                seen.set(nickname, {
                    conv_id: nickname,
                    nickname: nickname,
                    preview: (previewEl ? previewEl.innerText : '').trim().slice(0, 100),
                    unread: unread,
                    has_lead: (el.innerText || '').includes('已留资'),
                    last_time: (timeEl ? timeEl.innerText : '').trim(),
                });
            });
            collect();
            const first = document.querySelector('[class*="conversationItem-"]');
            const h = first && first.closest('.rc-virtual-list-holder');
            if (h && h.scrollHeight > h.clientHeight + 10) {
                const step = Math.max(120, h.clientHeight - 60);
                for (let y = 0; y <= h.scrollHeight && y < 20000; y += step) {
                    h.scrollTop = y; h.dispatchEvent(new Event('scroll'));
                    await new Promise(r => setTimeout(r, 90));
                    collect();
                }
                h.scrollTop = 0; h.dispatchEvent(new Event('scroll'));
                await new Promise(r => setTimeout(r, 90));
            }
            const out = Array.from(seen.values());
            return {rows: out, count: out.length};
        }
        """
        # 以下旧代码已废弃
        _deprecated_js = r"""
        () => {
            const out = [];
            const dbg = {strategies: []};
            const skipTexts = new Set(['会话窗口', '私信管理', '消息', '搜索', '全部会话', '未读消息', '已留资', '线索', '设置', '账号']);
            const timeRe = /^(\d{1,2}:\d{2}|\d+分钟前|\d+小时前|昨天|前天|\d{1,2}-\d{1,2})$/;
            const isVisible = (el) => {
                if (!el) return false;
                const r = el.getBoundingClientRect();
                return r.width > 0 && r.height > 0 && r.bottom > 0 && r.top < window.innerHeight;
            };
            const isNoiseLine = (l) => timeRe.test(l) || /^\d{1,3}$/.test(l) || skipTexts.has(l);
            const parseRow = (node) => {
                const text = (node.innerText || '').trim();
                if (!text || text.length < 5 || text.length > 500) return null;
                const lines = text.split('\n').map(s => s.trim()).filter(Boolean);
                if (lines.length < 2) return null;
                const title = lines[0];
                if (skipTexts.has(title) || title.length > 40) return null;
                // 必须包含时间特征（会话行必有时间）
                const hasTime = lines.some(l => timeRe.test(l));
                if (!hasTime) return null;
                const bodyLines = lines.slice(1).filter(l => !isNoiseLine(l));
                const lastMessage = bodyLines.join(' ').trim().slice(0, 120);
                if (!lastMessage) return null;
                return {nickname: title, last_message: lastMessage};
            };
            // 多选择器策略
            const selectors = [
                '.conversationItem-RaXg9G',
                '[class*="conversationItem" i]',
                '[class*="conversation" i]',
                '[class*="session" i]',
                '[class*="chat-item" i]',
                '[class*="list"] [class*="item" i]',
                '[role="listitem"]',
            ];
            const nodes = new Set();
            let visibleCount = 0;
            for (const sel of selectors) {
                try {
                    document.querySelectorAll(sel).forEach(n => {
                        nodes.add(n);
                        if (isVisible(n)) visibleCount++;
                    });
                } catch(e) {}
            }
            dbg.strategies.push('selector-nodes:' + nodes.size + ',visible:' + visibleCount);
            // 兜底：如果选择器一个都没命中，直接扫描所有 div
            if (nodes.size === 0) {
                dbg.strategies.push('fallback:scanning-all-divs');
                document.querySelectorAll('div').forEach(n => nodes.add(n));
                dbg.strategies.push('fallback-nodes:' + nodes.size);
            }
            const seen = new Set();
            for (const node of nodes) {
                const item = parseRow(node);
                if (!item) continue;
                const key = item.nickname;
                if (seen.has(key)) continue;
                seen.add(key);
                out.push({
                    conv_id: key,
                    nickname: item.nickname,
                    preview: item.last_message,
                    unread: 0,
                    has_lead: (node.innerText || '').includes('已留资'),
                    last_time: '',
                });
            }
            dbg.found = out.length;
            return {rows: out, debug: dbg};
        }
        """
        try:
            self._dismiss_overlays()
        except Exception:
            pass
        try:
            # 搜索所有 frame（garfish 微前端可能把内容放在子 frame）
            rows = []
            for fr in self._page.frames:
                try:
                    result = fr.evaluate(js) or {}
                    fr_rows = result.get('rows', []) if isinstance(result, dict) else (result or [])
                    rows.extend(fr_rows)
                except Exception:
                    pass
            dbg = {}
            if not rows:
                logger.warning('来客会话列表为空，frames=%d', len(self._page.frames))
                # 0 会话时自动截图（在 worker 线程内安全）
                try:
                    import os as _os
                    shot_path = _os.path.join(_os.path.dirname(_os.path.abspath(__file__)), 'laike_shot.png')
                    self._page.screenshot(path=shot_path)
                    logger.warning('已保存监控截图到 laike_shot.png')
                except Exception as _e:
                    logger.warning('截图失败: %s', _e)
                # 检查账号选择器状态
                try:
                    acc_js = r"""() => {
                        const els = document.querySelectorAll('[class*="UserCard"], [class*="account"]');
                        const texts = [];
                        els.forEach(el => {
                            const t = (el.innerText || '').trim().slice(0, 50);
                            if (t) texts.push(t);
                        });
                        return texts.slice(0, 5);
                    }"""
                    acc_info = self._page.evaluate(acc_js) or []
                    logger.warning('账号区域文本: %s', acc_info)
                except Exception as _e:
                    pass
                # 导出实际 HTML 看看到底有什么
                try:
                    import os as _os
                    html_path = _os.path.join(_os.path.dirname(_os.path.abspath(__file__)), 'laike_actual.html')
                    with open(html_path, 'w', encoding='utf-8') as _f:
                        _f.write(self._page.content())
                    logger.warning('已导出实际 HTML 到 laike_actual.html (%d 字节)', _os.path.getsize(html_path))
                    # 同时输出包含 conversation 的类名
                    cls_js = """() => {
                        const classes = new Set();
                        document.querySelectorAll('[class]').forEach(el => {
                            const c = el.className.toString();
                            if (/convers|session|chat/i.test(c)) classes.add(c.slice(0, 80));
                        });
                        return [...classes].slice(0, 20);
                    }"""
                    found_classes = self._page.evaluate(cls_js) or []
                    logger.warning('页面中含 convers/session/chat 的类名: %s', found_classes)
                except Exception as _e:
                    logger.warning('导出 HTML 失败: %s', _e)
                # 保存页面快照用于调试
                try:
                    import os as _os
                    snap_path = _os.path.join(_os.path.dirname(_os.path.abspath(__file__)), 'laike_debug.html')
                    with open(snap_path, 'w', encoding='utf-8') as _f:
                        _f.write(self._page.content())
                    logger.warning('已保存页面快照到 laike_debug.html，URL: %s', self._page.url)
                except Exception as _e:
                    logger.warning('保存快照失败: %s', _e)
        except Exception as exc:
            logger.warning('读取来客会话列表失败: %s', exc)
            rows = []
        return rows

    def _frame_with(self, selector: str):
        """返回包含 selector 的 frame（来客为 garfish 微前端，内容可能在子 frame）。"""
        try:
            frames = [self._page.main_frame] + [f for f in self._page.frames if f != self._page.main_frame]
        except Exception:
            return self._page
        for fr in frames:
            try:
                if fr.locator(selector).count() > 0:
                    return fr
            except Exception:
                continue
        return self._page

    def _dismiss_overlays(self) -> List[str]:
        """关闭来客页面上的引导弹窗/公告/遮罩（如 guideModal），返回被关闭弹窗的文字摘要。"""
        closed: List[str] = []
        js = r"""() => {
            const out = [];
            const vis = e => { const r = e.getBoundingClientRect(); return r.width > 0 && r.height > 0; };
            const modals = Array.from(document.querySelectorAll(
                '.byted-modal-show, [class*="guideModal"], [class*="Modal"][class*="show"], .byted-drawer-show, [role="dialog"]'
            )).filter(vis);
            const words = ['我知道了', '知道了', '跳过', '暂不', '以后再说', '关闭', '完成', '取消', '下次再说', '我已了解', '好的'];
            for (const m of modals) {
                const text = (m.innerText || '').trim().slice(0, 60);
                let done = false;
                const btns = Array.from(m.querySelectorAll('button, [role="button"], a, span, div')).filter(vis);
                for (const w of words) {
                    const b = btns.find(x => (x.innerText || '').trim() === w);
                    if (b) { b.click(); out.push(text + ' -> ' + w); done = true; break; }
                }
                if (!done) {
                    const c = m.querySelector('.byted-modal-close-icon, .byted-modal-close, [class*="close" i], [aria-label*="close" i], [aria-label*="关闭"]');
                    if (c) { c.click(); out.push(text + ' -> close'); done = true; }
                }
                if (!done) {
                    const nb = btns.find(x => /下一步|继续/.test((x.innerText || '').trim()) && (x.innerText || '').trim().length < 6);
                    if (nb) { nb.click(); out.push(text + ' -> next'); }
                }
            }
            return out;
        }"""
        for _ in range(4):
            got = []
            for fr in self._page.frames:
                try:
                    got += fr.evaluate(js) or []
                except Exception:
                    pass
            if not got:
                break
            closed += got
            time.sleep(0.6)
        try:
            self._page.keyboard.press('Escape')
        except Exception:
            pass
        if closed:
            logger.warning('已关闭来客页面弹窗: %s', closed)
        return closed

    def _robust_click(self, loc, timeout: int = 5000) -> None:
        """普通点击 → 关弹窗后重试 → force 点击 → JS 点击。"""
        try:
            loc.scroll_into_view_if_needed(timeout=timeout)
        except Exception:
            pass
        try:
            loc.click(timeout=timeout)
            return
        except Exception as first:
            logger.warning('点击失败，尝试关闭弹窗后重试: %s', str(first).split('\n')[0])
        self._dismiss_overlays()
        try:
            loc.click(timeout=timeout)
            return
        except Exception:
            pass
        try:
            loc.click(timeout=timeout, force=True)
            return
        except Exception:
            pass
        loc.evaluate('el => el.click()')

    def _msg_area_sig(self) -> str:
        try:
            fr = self._frame_with('.csUI-NormalMessage')
            return fr.evaluate("() => { const a = document.querySelectorAll('.csUI-MessageItem'); const l = a[a.length-1]; return a.length + '|' + (l ? l.innerText.slice(0, 80) : ''); }") or ''
        except Exception:
            return ''

    def _msg_area_ready(self) -> bool:
        return bool(self._msg_area_sig())

    def _open_header_matches(self, fr, nickname: str) -> bool:
        """当前打开的会话标题是否就是该昵称（列表项被虚拟滚动卸载时仍可复用已打开会话）。"""
        try:
            return bool(fr.evaluate("""(nick) => {
                const norm = s => (s || '').normalize('NFKC').replace(/[\\s\\u200b-\\u200f\\ufe0f]/g, '');
                const sel = document.querySelector('[class*="conversationItem-"][class*="active"], [class*="conversationItem-"][class*="selected"]');
                if (sel) { const n = sel.querySelector('[class*="conversationName-"]'); if (n && norm(n.innerText).startsWith(norm(nick).slice(0, 12))) return true; }
                const t = norm(nick).slice(0, 12);
                return Array.from(document.querySelectorAll('[class*="header" i], [class*="title" i]')).some(e => e.children.length < 6 && norm(e.innerText).includes(t));
            }""", nickname))
        except Exception:
            return False

    def _search_conv(self, fr, nickname: str) -> int:
        """虚拟列表中不可见时：先滚动整个列表查找；仍找不到再用「联系人/群名称」搜索框。返回会话项下标。"""
        try:
            idx = fr.evaluate("""async ([itemSel, nameSel, nick]) => {
                const find = """ + FIND_CONV_JS + """;
                const first = document.querySelector(itemSel);
                const h = first && first.closest('.rc-virtual-list-holder');
                if (!h) return -1;
                const step = Math.max(120, h.clientHeight - 60);
                for (let y = 0; y <= h.scrollHeight && y < 20000; y += step) {
                    h.scrollTop = y; h.dispatchEvent(new Event('scroll'));
                    await new Promise(r => setTimeout(r, 200));
                    const i = find([itemSel, nameSel, nick]);
                    if (i >= 0) return i;
                }
                return -1;
            }""", [SEL_CONV_ITEM, SEL_CONV_NAME, nickname])
            if idx is not None and idx >= 0:
                logger.info('来客会话在虚拟列表下方，已滚动定位: %s', nickname)
                return idx
        except Exception as exc:
            logger.warning('滚动查找会话失败: %s', exc)
        try:
            box = fr.locator('input[placeholder="联系人/群名称"]').first
            if box.count():
                key = re.sub(r'[^\w\u4e00-\u9fff]', '', nickname)[:8] or nickname[:8]
                box.fill(key)
                box.press('Enter')
                time.sleep(1.0)
                idx = fr.evaluate(FIND_CONV_JS, [SEL_CONV_ITEM, SEL_CONV_NAME, nickname])
                if idx is not None and idx >= 0:
                    logger.info('来客会话通过搜索框定位: %s', nickname)
                    self._search_active = True
                    return idx
                box.fill('')
                box.press('Enter')
        except Exception as exc:
            logger.warning('搜索框查找会话失败: %s', exc)
        return -1

    def _op_open_conversation(self, nickname: str) -> Dict[str, Any]:
        """在左侧会话列表中按昵称精确定位并点击（只在会话项内匹配，避免误点消息内容）。"""
        self._ensure_browser()
        fr = self._frame_with(SEL_CONV_ITEM)
        try:
            if getattr(self, '_cur_open', '') == nickname and self._msg_area_ready() and self._open_header_matches(fr, nickname):
                return {'ok': True, 'reused': True}
            idx = fr.evaluate(FIND_CONV_JS, [SEL_CONV_ITEM, SEL_CONV_NAME, nickname])
            if idx is None or idx < 0:
                idx = self._search_conv(fr, nickname)
            if idx is None or idx < 0:
                return {'ok': False, 'error': f'会话列表中未找到: {nickname}'}
            self._cur_open = ''
            prev_sig = self._msg_area_sig()
            self._robust_click(fr.locator(SEL_CONV_ITEM).nth(idx))
            # 等消息区切换并稳定（最长 1.5s），代替固定 1.5s
            deadline = time.time() + 1.5
            last, stable = None, 0
            time.sleep(0.2)
            while time.time() < deadline:
                sig = self._msg_area_sig()
                if sig and sig != prev_sig:
                    stable = stable + 1 if sig == last else 0
                    if stable >= 1:
                        break
                last = sig
                time.sleep(0.15)
            self._cur_open = nickname
            if getattr(self, '_search_active', False):
                # 搜索定位后清空搜索框，恢复完整会话列表（否则后续扫描只看到搜索结果）
                self._search_active = False
                try:
                    box = fr.locator('input[placeholder="联系人/群名称"]').first
                    box.fill('')
                    box.press('Enter')
                    time.sleep(0.5)
                except Exception as exc:
                    logger.warning('清空来客搜索框失败: %s', exc)
            return {'ok': True}
        except Exception as exc:
            return {'ok': False, 'error': str(exc)}

    def _op_read_messages(self, limit: int = 20) -> List[Dict[str, Any]]:
        """读取中间聊天区的消息（csUI 组件类名稳定，无哈希）。"""
        self._ensure_browser()
        js = """
        (limit) => {
            const out = [];
            const items = document.querySelectorAll('.csUI-MessageItem .csUI-NormalMessage');
            const sliced = Array.from(items).slice(-limit);
            sliced.forEach(el => {
                const textEl = el.querySelector('.csUI-Text');
                const text = (textEl ? textEl.innerText : el.innerText || '').trim();
                if (!text) return;
                const isSelf = el.classList.contains('csUI-NormalMessage_right')
                    || !!el.closest('[class*="NormalMessage_right"]');
                const holder = el.closest('[data-id],[data-msg-id],[data-message-id]');
                const mid = holder ? (holder.getAttribute('data-id') || holder.getAttribute('data-msg-id') || holder.getAttribute('data-message-id') || '') : '';
                const box = el.closest('.csUI-MessageItem') || el;
                const auto = isSelf && /此消息由「/.test(box.innerText || '');
                out.push({text, is_self: isSelf, sender: '', msg_id: mid, auto});
            });
            return out;
        }
        """
        fr = self._frame_with('.csUI-NormalMessage')
        try:
            rows = fr.evaluate(js, limit) or []
        except Exception as exc:
            logger.warning('读取来客消息失败: %s', exc)
            rows = []
        return rows

    # ── 智能客服（机器人）接待 → 人工接管 ──

    _TAKEOVER_JS = r"""(detail) => {
        const vis = e => { const r = e.getBoundingClientRect(); const st = getComputedStyle(e);
            return r.width > 0 && r.height > 0 && st.visibility !== 'hidden' && st.display !== 'none'; };
        const body = (document.body && document.body.innerText) || '';
        const botRe = /(智能客服|机器人|AI\s*客服|智能助手|托管)[^\n]{0,8}(接待中|正在接待|接待)|(机器人|智能客服)接待/;
        const btnRe = /^\s*(人工接管|立即接管|接管会话|手动接管|转人工接管|接管)\s*$/;
        const btns = Array.from(document.querySelectorAll('button, a, span, div, [role=button]'))
            .filter(e => vis(e) && btnRe.test((e.innerText || '').trim()) && (e.children.length === 0 || e.tagName === 'BUTTON'));
        const ta = Array.from(document.querySelectorAll('textarea')).find(vis);
        const out = {bot: botRe.test(body), btn: btns.length > 0,
                     input_disabled: !!(ta && (ta.disabled || ta.readOnly)), has_input: !!ta};
        if (detail) {
            const snip = [];
            const re = /接管|智能客服|机器人|托管|接待中/g; let m;
            while ((m = re.exec(body)) && snip.length < 12) snip.push(body.slice(Math.max(0, m.index - 25), m.index + 25).replace(/\n/g, ' ⏎ '));
            out.snippets = snip; out.buttons = btns.slice(0, 5).map(b => b.tagName + ':' + (b.className || '').toString().slice(0, 60) + ':' + b.innerText.trim());
            out.url = location.href;
        }
        return out;
    }"""

    def _takeover_state(self, detail: bool = False) -> Dict[str, Any]:
        self._ensure_browser()
        agg = {'bot': False, 'btn': False, 'input_disabled': False, 'frames': []}
        for fr in self._page.frames:
            try:
                st = fr.evaluate(self._TAKEOVER_JS, detail) or {}
            except Exception:
                continue
            agg['bot'] |= bool(st.get('bot'))
            agg['btn'] |= bool(st.get('btn'))
            agg['input_disabled'] |= bool(st.get('input_disabled'))
            if detail and (st.get('snippets') or st.get('buttons') or st.get('has_input')):
                agg['frames'].append(st)
        agg['needed'] = agg['btn'] or (agg['bot'] and agg['input_disabled'])
        agg['ok'] = True
        return agg

    def _click_takeover_button(self) -> bool:
        pat = re.compile(r'^\s*(人工接管|立即接管|接管会话|手动接管|转人工接管|接管)\s*$')
        for fr in self._page.frames:
            try:
                loc = fr.locator('button, a, [role=button], span, div').filter(has_text=pat)
                n = loc.count()
                for i in range(n - 1, -1, -1):  # 取最内层（最后匹配）的元素
                    el = loc.nth(i)
                    try:
                        if el.is_visible() and pat.match((el.inner_text(timeout=1000) or '').strip()):
                            self._robust_click(el)
                            return True
                    except Exception:
                        continue
            except Exception:
                continue
        return False

    def _confirm_takeover_dialog(self) -> bool:
        """只确认文本中提到「接管」的弹窗。"""
        pat = re.compile(r'^\s*(确认接管|确定接管|立即接管|接管|确定|确认)\s*$')
        for fr in self._page.frames:
            try:
                dlgs = fr.locator('[role=dialog], [class*="modal"], [class*="Modal"], [class*="dialog"], [class*="popconfirm"], [class*="Popconfirm"]')
                for i in range(dlgs.count()):
                    d = dlgs.nth(i)
                    try:
                        if not d.is_visible() or '接管' not in (d.inner_text(timeout=1000) or ''):
                            continue
                        btn = d.locator('button').filter(has_text=pat)
                        if btn.count() > 0:
                            self._robust_click(btn.last)
                            return True
                    except Exception:
                        continue
            except Exception:
                continue
        return False

    def _op_ensure_takeover(self) -> Dict[str, Any]:
        """当前会话若处于智能客服接待，点击「人工接管」并确认，等待输入框可用。"""
        st = self._takeover_state()
        if not st['needed']:
            return {'ok': True, 'needed': False}
        for attempt in range(2):
            if not self._click_takeover_button():
                if not st['btn']:
                    return {'ok': False, 'needed': True, 'error': '检测到智能客服接待中，但未找到「人工接管」按钮'}
                return {'ok': False, 'needed': True, 'error': '「人工接管」按钮点击失败'}
            time.sleep(1.0)
            self._confirm_takeover_dialog()
            deadline = time.time() + 8
            while time.time() < deadline:
                time.sleep(0.6)
                st = self._takeover_state()
                if not st['btn'] and not st['input_disabled']:
                    return {'ok': True, 'needed': True, 'took_over': True}
                self._confirm_takeover_dialog()
        return {'ok': False, 'needed': True, 'error': '点击「人工接管」后仍处于智能客服接待/输入框不可用'}

    # ── 一键留资卡片 ──

    _TOOLBAR_JS = r"""() => {
        const vis = e => { const r = e.getBoundingClientRect(); const st = getComputedStyle(e);
            return r.width > 0 && r.height > 0 && st.visibility !== 'hidden' && st.display !== 'none'; };
        const ta = Array.from(document.querySelectorAll('textarea')).find(vis);
        const tr = ta ? ta.getBoundingClientRect() : null;
        const leaves = Array.from(document.querySelectorAll('button, [role=button], span, div, a, i'))
            .filter(e => vis(e) && (e.innerText || '').trim().length > 0 && (e.innerText || '').trim().length <= 8
                && Array.from(e.children).every(c => !(c.innerText || '').trim()));
        const near = tr ? leaves.filter(e => { const r = e.getBoundingClientRect();
            return r.top > tr.top - 140 && r.bottom < tr.top + 10 && r.left >= tr.left - 40 && r.right <= tr.right + 40; }) : [];
        const desc = e => { const r = e.getBoundingClientRect(); const p = e.closest('button,[role=button]') || e.parentElement;
            return {text: e.innerText.trim(), tag: e.tagName, cls: (e.className || '').toString().slice(0, 80),
                    parent: p ? (p.tagName + '.' + (p.className || '').toString().slice(0, 60)) : '', x: Math.round(r.x), y: Math.round(r.y)}; };
        const lead = leaves.filter(e => /留资/.test(e.innerText));
        return {input_rect: tr ? {x: Math.round(tr.x), y: Math.round(tr.y), w: Math.round(tr.width)} : null,
                toolbar: near.slice(0, 30).map(desc), lead_buttons: lead.slice(0, 5).map(desc),
                message_items: document.querySelectorAll('.csUI-MessageItem').length, url: location.href};
    }"""

    _ICONS_JS = r"""() => {
        const vis = e => { const r = e.getBoundingClientRect(); return r.width > 0 && r.height > 0; };
        const ta = Array.from(document.querySelectorAll('textarea')).find(vis);
        if (!ta) return [];
        const tr = ta.getBoundingClientRect();
        const seen = new Set(); const out = [];
        Array.from(document.querySelectorAll('svg, img, i, [class*="icon"], [class*="Icon"]')).forEach(e => {
            const r = e.getBoundingClientRect();
            if (!vis(e) || r.width > 40 || r.height > 40) return;
            if (r.top < tr.top - 70 || r.bottom > tr.top + 4 || r.left < tr.left - 60 || r.right > tr.right + 80) return;
            const host = e.closest('button,[role=button],[class*="item"],[class*="tool"],span,div') || e;
            const hr = host.getBoundingClientRect();
            const k = Math.round(hr.x) + ',' + Math.round(hr.y);
            if (seen.has(k) || hr.width > 60) return; seen.add(k);
            out.push({x: hr.x + hr.width / 2, y: hr.y + hr.height / 2, cls: (host.className || '').toString().slice(0, 80),
                      title: host.getAttribute('title') || host.getAttribute('aria-label') || e.getAttribute('title') || ''});
        });
        return out.sort((a, b) => a.x - b.x);
    }"""

    def _toolbar_icons(self) -> List[Dict[str, Any]]:
        """输入框上方的图标按钮：逐个悬停读取提示文字（只悬停，不点击）。"""
        fr = self._frame_with('textarea')
        icons = fr.evaluate(self._ICONS_JS) or []
        tip_js = """() => Array.from(document.querySelectorAll('[class*="tooltip"], [class*="Tooltip"], [role=tooltip], [class*="popover"], [class*="Popover"]'))
            .filter(e => { const r = e.getBoundingClientRect(); return r.width > 0 && r.height > 0; })
            .map(e => (e.innerText || '').trim()).filter(Boolean)"""
        try:
            base = set(fr.evaluate(tip_js) or [])
        except Exception:
            base = set()
        for ic in icons:
            try:
                self._page.mouse.move(ic['x'], ic['y'])
                time.sleep(0.6)
                ic['tooltip'] = ' | '.join(t for t in (fr.evaluate(tip_js) or []) if t not in base)[:60]
                ic['text'] = fr.evaluate('([x, y]) => { const e = document.elementFromPoint(x, y); const h = e && e.closest(".toolbar-text,.toolbar-icon"); return h ? (h.innerText || "").trim() : ""; }', [ic['x'], ic['y']])
            except Exception as exc:
                ic['tooltip'] = f'err:{exc}'
        try:
            self._page.mouse.move(5, 5)
        except Exception:
            pass
        return icons

    def _op_toolbar_probe(self) -> Dict[str, Any]:
        """只读：列出聊天输入框上方工具栏按钮与「一键留资」位置（不点击）。"""
        self._ensure_browser()
        out = {'ok': True, 'frames': []}
        for fr in self._page.frames:
            try:
                st = fr.evaluate(self._TOOLBAR_JS) or {}
            except Exception:
                continue
            if st.get('toolbar') or st.get('lead_buttons') or st.get('input_rect'):
                out['frames'].append(st)
        try:
            out['icons'] = self._toolbar_icons()
        except Exception as exc:
            out['icons_error'] = str(exc)
        return out

    _POPUP_SEL = ('[role=dialog], [class*="modal"], [class*="Modal"], [class*="popover"], [class*="Popover"], '
                  '[class*="drawer"], [class*="Drawer"], [class*="dropdown"], [class*="Dropdown"], [class*="popup"]')

    def _visible_popups(self):
        found = []
        for fr in self._page.frames:
            try:
                loc = fr.locator(self._POPUP_SEL)
                for i in range(min(loc.count(), 40)):
                    el = loc.nth(i)
                    try:
                        if el.is_visible():
                            found.append((fr, el, (el.inner_text(timeout=800) or '')[:300]))
                    except Exception:
                        continue
            except Exception:
                continue
        return found

    def _self_item_count(self) -> int:
        try:
            fr = self._frame_with('.csUI-MessageItem')
            return int(fr.evaluate("""() => document.querySelectorAll('.csUI-MessageItem').length""") or 0)
        except Exception:
            return 0

    def _last_item_is_self_card(self) -> bool:
        try:
            fr = self._frame_with('.csUI-MessageItem')
            return bool(fr.evaluate("""() => { const it = Array.from(document.querySelectorAll('.csUI-MessageItem')).pop();
                if (!it) return false; const h = it.innerHTML || '';
                return /_right|right/.test(h) || /留资|表单|填写|联系方式|信息/.test(it.innerText || ''); }"""))
        except Exception:
            return False

    def _op_peer_info(self) -> Dict[str, Any]:
        out: Dict[str, Any] = {}
        for fr in self._page.frames:
            try:
                out.update(fr.evaluate('() => window.__peerInfo || {}') or {})
            except Exception:
                pass
        return {'peers': out}

    def _op_page_exec(self, url: str = '', js: str = '', shot: str = '', wait: float = 2.0, **_k) -> Dict[str, Any]:
        """调试：可选跳转 URL，在各 frame 执行 JS，可选截图。"""
        self._ensure_browser()
        out: Dict[str, Any] = {}
        if url:
            self._page.goto(url, wait_until='domcontentloaded', timeout=30000)
            self._cur_open = ''
            time.sleep(wait)
        out['url'] = self._page.url
        if js:
            res = []
            for fr in self._page.frames:
                try:
                    r = fr.evaluate(js)
                    if r not in (None, '', [], {}):
                        res.append({'frame': fr.url[:120], 'r': r})
                except Exception as exc:
                    res.append({'frame': fr.url[:120], 'err': str(exc)[:200]})
            out['res'] = res
            time.sleep(min(wait, 3))
        if shot:
            self._page.screenshot(path=shot)
            out['shot'] = shot
        return out

    def _op_send_lead_card(self, nickname: str) -> Dict[str, Any]:
        """点击输入框上方的「一键留资」，处理弹窗（选默认表单 → 发送），并确认聊天中出现卡片。"""
        self._ensure_browser()
        try:
            if nickname:
                opened = self._op_open_conversation(nickname)
                if not opened.get('ok'):
                    return {'ok': False, 'error': opened.get('error') or '打开会话失败'}
            tk = self._op_ensure_takeover()
            if not tk.get('ok'):
                return {'ok': False, 'error': '人工接管失败：' + str(tk.get('error'))}
            before = self._self_item_count()
            popups_before = {t for _, _, t in self._visible_popups()}
            pat = re.compile(r'^\s*一键留资\s*$')
            clicked = False
            for fr in self._page.frames:
                loc = fr.locator('button, [role=button], span, div, a').filter(has_text=pat)
                for i in range(loc.count() - 1, -1, -1):
                    el = loc.nth(i)
                    try:
                        if el.is_visible() and pat.match((el.inner_text(timeout=800) or '').strip()):
                            self._robust_click(el)
                            clicked = True
                            break
                    except Exception:
                        continue
                if clicked:
                    break
            if not clicked:
                for ic in self._toolbar_icons():
                    hint = (ic.get('tooltip') or '') + (ic.get('title') or '') + (ic.get('text') or '')
                    # 实测：第 3 个图标提示为「引导用户发送手机号，一天内针对一个用户最多可发送2次」即一键留资
                    if '留资' in hint or '引导用户发送手机号' in hint:
                        self._page.mouse.click(ic['x'], ic['y'])
                        clicked = True
                        break
            if not clicked:
                return {'ok': False, 'error': '未找到「一键留资」按钮（文字与图标提示均未匹配）'}
            flow = 'direct'
            tip_pat = re.compile(r'引导用户发送手机号|最多可发送2次')
            limit_pat = re.compile(r'上限|次数已用完|已用完|超过.{0,6}次数|今日.{0,8}(已|不可|无法)|已达')
            popup_done = False
            deadline = time.time() + 9
            while time.time() < deadline:
                time.sleep(0.6)
                if self._self_item_count() > before:
                    return {'ok': True, 'flow': flow}
                for fr, el, t in self._visible_popups():
                    if t in popups_before or tip_pat.search(t or ''):
                        continue  # 忽略悬停提示（tooltip）
                    if limit_pat.search(t or ''):
                        return {'ok': True, 'limit': True, 'flow': 'limit:' + t[:60].replace('\n', ' ')}
                    if popup_done:
                        continue
                    send_pat = re.compile(r'^\s*(发送|确定|确认|确认发送|立即发送|发送卡片)\s*$')
                    try:
                        btn = el.locator('button, [role=button]').filter(has_text=send_pat)
                        if btn.count() == 0:
                            continue
                        self._robust_click(btn.last)
                        popup_done = True
                        flow = 'popup:' + t[:60].replace('\n', ' ')
                    except Exception:
                        continue
                try:
                    body = self._page.evaluate("() => Array.from(document.querySelectorAll('[class*=toast],[class*=Toast],[class*=message-notice],[class*=semi-toast],[role=alert]')).map(e => e.innerText).join(' | ')") or ''
                except Exception:
                    body = ''
                if body and limit_pat.search(body) and not tip_pat.search(body):
                    return {'ok': True, 'limit': True, 'flow': 'limit:' + body[:60]}
            return {'ok': False, 'error': f'点击后聊天中未出现留资卡片（流程：{flow}）', 'flow': flow}
        except Exception as exc:
            return {'ok': False, 'error': str(exc)}

    def _op_send_text(self, nickname: str, text: str) -> Dict[str, Any]:
        """在输入框输入并发送。"""
        self._ensure_browser()
        try:
            opened = self._op_open_conversation(nickname)
            if not opened.get('ok'):
                return {'ok': False, 'error': opened.get('error') or '打开会话失败'}
            tk = self._op_ensure_takeover()
            if not tk.get('ok'):
                return {'ok': False, 'error': tk.get('error'), 'takeover_failed': True}
            input_sel = ('textarea[placeholder*="回复内容"], .msgEditor-hzp4al textarea, '
                         '[class*="msgEditor-"] textarea')
            fr = self._frame_with(input_sel)
            fr.wait_for_selector(input_sel, timeout=10000)
            box = fr.locator(input_sel).first
            self._robust_click(box)
            box.fill(text)
            time.sleep(0.15)
            send_sel = '.msgEditor-hzp4al button, [class*="msgEditor-"] button'
            btn = fr.locator('button:not([disabled])').filter(has_text='发送')
            if btn.count() == 0:
                btn = fr.locator(f'{send_sel.split(",")[0]}:not([disabled]), [class*="msgEditor-"] button:not([disabled])')
            if btn.count() > 0:
                self._robust_click(btn.last)
            else:
                box.press('Enter')
            ok = False
            deadline = time.time() + 3.0
            while time.time() < deadline:
                time.sleep(0.25)
                if self._verify_send(text):
                    ok = True
                    break
            return {'ok': ok}
        except Exception as exc:
            return {'ok': False, 'error': str(exc)}

    def _verify_send(self, text: str) -> bool:
        """检查己方最新气泡是否包含发送文本。"""
        try:
            msgs = self._op_read_messages(limit=5)
            for m in reversed(msgs):
                if m.get('is_self') and text[:30] in (m.get('text') or ''):
                    return True
            return False
        except Exception:
            return False

    # ── 无头登录（life.douyin.com 不支持扫码：passport get_qrcode 返回 1069「不能用该app扫码」）──

    def _snapshot_b64(self) -> str:
        import base64
        try:
            return base64.b64encode(self._page.screenshot(type='png')).decode()
        except Exception:
            return ''

    def _login_state(self, extra: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        url = ''
        try:
            url = self._page.url or ''
        except Exception:
            pass
        logged = self._has_session_cookie() and '/p/login' not in url
        captcha = False
        tips = ''
        try:
            captcha = self._page.evaluate("""() => {
                const sel = '#captcha_container, [id*="captcha"], [class*="captcha"], iframe[src*="verify"], iframe[src*="captcha"], [class*="verify-wrap"], [class*="secsdk"]';
                return Array.from(document.querySelectorAll(sel)).some(e => {
                    const r = e.getBoundingClientRect(); return r.width > 50 && r.height > 50;
                });
            }""")
            tips = self._page.evaluate("""() => Array.from(document.querySelectorAll('.life-core-message, .life-core-toast, [class*="toast"], [class*="error-tip"], .life-core-form-message, [class*="message-notice"]'))
                .map(e => (e.innerText || '').trim()).filter(Boolean).slice(0, 3).join(' | ')""") or ''
        except Exception:
            pass
        out = {'ok': True, 'logged_in': bool(logged), 'captcha': bool(captcha), 'tips': tips,
               'url': url, 'screenshot': self._snapshot_b64()}
        if extra:
            out.update(extra)
        return out

    def _agree_terms(self) -> None:
        try:
            self._page.evaluate("""() => {
                const boxes = Array.from(document.querySelectorAll('input[type=checkbox]'));
                boxes.forEach(b => { if (!b.checked) { const l = b.closest('label') || b; l.click(); } });
            }""")
        except Exception:
            pass

    def _op_login_open(self) -> Dict[str, Any]:
        self._ensure_browser()
        if self._has_session_cookie():
            try:
                self._page.goto(LAIKE_HOME, wait_until='domcontentloaded', timeout=30000)
                time.sleep(3)
            except Exception:
                pass
            st = self._login_state()
            if st['logged_in'] and '/p/login' not in st['url'] and self._page.url.rstrip('/') != LAIKE_HOME:
                return st
        self._page.goto(LAIKE_LOGIN_URL, wait_until='domcontentloaded', timeout=30000)
        time.sleep(3)
        try:
            to_login = self._page.locator('[class*="to-login"]')
            if to_login.count() > 0 and to_login.first.is_visible():
                to_login.first.click(timeout=5000)
                time.sleep(1.5)
        except Exception:
            pass
        try:
            self._page.get_by_text('手机登录', exact=True).first.click(timeout=3000)
        except Exception:
            pass
        return self._login_state()

    def _op_login_send_code(self, phone: str) -> Dict[str, Any]:
        self._ensure_browser()
        phone = re.sub(r'\D', '', phone or '')
        if not re.fullmatch(r'1\d{10}', phone):
            return {'ok': False, 'error': '请输入 11 位中国大陆手机号'}
        if '/p/login' not in (self._page.url or ''):
            self._op_login_open()
        inp = self._page.locator('input[placeholder*="手机号"]').first
        inp.fill('')
        inp.type(phone, delay=60)
        self._agree_terms()
        self._page.locator('[class*="btn-send-code"]').first.click(timeout=5000)
        time.sleep(2.5)
        return self._login_state({'step': 'code_sent'})

    def _op_login_submit_code(self, code: str) -> Dict[str, Any]:
        """可靠填写短信验证码并登录：定位可见的验证码框 → 填写并校验 → 点击登录 → 等待会话 cookie。"""
        self._ensure_browser()
        code = re.sub(r'\D', '', code or '')
        if not re.fullmatch(r'\d{4,8}', code):
            return self._login_state({'step': 'code_invalid', 'result': 'invalid', 'message': '请输入 4–8 位数字验证码'})
        page = self._page
        if self._has_session_cookie() and '/p/login' not in (page.url or ''):
            return self._login_state({'step': 'already', 'result': 'success', 'message': '已登录'})
        inp = page.locator('input[placeholder*="验证码"]:visible')
        if inp.count() == 0:
            return self._login_state({'result': 'no_input', 'message': '页面上没有找到验证码输入框，请先点击「获取验证码」'})
        inp = inp.first
        filled = False
        for attempt in range(3):
            try:
                inp.scroll_into_view_if_needed(timeout=3000)
                inp.click(timeout=3000)
                inp.press('Control+A')
                inp.press('Backspace')
                if attempt == 0:
                    inp.type(code, delay=80)
                elif attempt == 1:
                    inp.fill(code)
                else:  # React 受控输入：用原生 setter 赋值并派发事件
                    inp.evaluate("""(el, v) => { const set = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value').set;
                        set.call(el, v); el.dispatchEvent(new Event('input', {bubbles: true})); el.dispatchEvent(new Event('change', {bubbles: true})); }""", code)
                time.sleep(0.3)
                if (inp.input_value(timeout=2000) or '').strip() == code:
                    filled = True
                    break
            except Exception:
                time.sleep(0.3)
        if not filled:
            return self._login_state({'result': 'fill_failed', 'message': '验证码未能写入输入框，请重试'})
        self._agree_terms()
        time.sleep(0.2)
        btn = page.locator('button:visible').filter(has_text=re.compile(r'^\s*登\s*录\s*$'))
        if btn.count() == 0:
            btn = page.locator('button:visible').filter(has_text='登录')
        try:
            btn.first.click(timeout=5000)
        except Exception:
            try:
                inp.press('Enter')
            except Exception:
                pass
        deadline = time.time() + 9
        while time.time() < deadline:
            time.sleep(0.5)
            try:
                url = page.url or ''
            except Exception:
                url = ''
            if self._has_session_cookie() and '/p/login' not in url:
                time.sleep(1)
                return self._login_state({'step': 'code_submitted', 'result': 'success', 'message': '登录成功'})
            st = self._login_state()
            if st.get('captcha'):
                st.update({'step': 'code_submitted', 'result': 'captcha', 'message': '需要安全验证：请直接在截图上拖动滑块/点击完成验证'})
                return st
            if st.get('tips') and time.time() > deadline - 7:
                st.update({'step': 'code_submitted', 'result': 'error', 'message': '登录未成功：' + st['tips']})
                return st
        st = self._login_state({'step': 'code_submitted'})
        if st.get('logged_in'):
            st.update({'result': 'success', 'message': '登录成功'})
        else:
            st.update({'result': 'pending', 'message': '已提交但仍停留在登录页' + (('：' + st['tips']) if st.get('tips') else '，请查看截图（验证码可能错误或已过期，可重新获取）')})
        return st

    def _op_login_password(self, account: str, password: str) -> Dict[str, Any]:
        self._ensure_browser()
        if '/p/login' not in (self._page.url or ''):
            self._op_login_open()
        try:
            self._page.locator('.switch-tip').filter(has_text='密码登录').first.click(timeout=4000)
            time.sleep(1)
        except Exception:
            pass
        acc = self._page.locator('input[placeholder*="手机号"]:visible, input[placeholder*="账号"]:visible, input[placeholder*="邮箱"]:visible').first
        acc.fill('')
        acc.type(account, delay=50)
        pwd = self._page.locator('input[type="password"]:visible').first
        pwd.fill('')
        pwd.type(password, delay=50)
        self._agree_terms()
        self._page.locator('button:visible').filter(has_text='登录').first.click(timeout=5000)
        time.sleep(4)
        return self._login_state({'step': 'password_submitted'})

    def _op_login_snapshot(self) -> Dict[str, Any]:
        self._ensure_browser()
        return self._login_state()

    def _op_login_mouse(self, action: str = 'click', x: float = 0, y: float = 0,
                        x2: float = 0, y2: float = 0, **_kw) -> Dict[str, Any]:
        """把用户在截图上的点击/拖动转发到无头浏览器（用于滑块/点选验证码、选择门店）。"""
        self._ensure_browser()
        m = self._page.mouse
        if action == 'drag':
            import random
            m.move(x, y)
            m.down()
            steps = 25
            for i in range(1, steps + 1):
                t = i / steps
                ease = 1 - (1 - t) ** 3
                m.move(x + (x2 - x) * ease, y + (y2 - y) * ease + random.uniform(-1.5, 1.5))
                time.sleep(random.uniform(0.01, 0.03))
            m.up()
        else:
            m.click(x, y)
        time.sleep(2)
        return self._login_state()

    def _op_login_status(self) -> Dict[str, Any]:
        self._ensure_browser()
        return self._login_state()

    def _op_import_cookies(self, cookies: List[Dict[str, Any]]) -> Dict[str, Any]:
        self._ensure_browser()
        clean = []
        for c in cookies:
            if not c.get('name'):
                continue
            item = {'name': c['name'], 'value': str(c.get('value', '')),
                    'domain': c.get('domain') or '.douyin.com', 'path': c.get('path') or '/'}
            for k in ('secure', 'httpOnly'):
                if k in c:
                    item[k] = bool(c[k])
            ss = c.get('sameSite')
            if ss in ('Strict', 'Lax', 'None'):
                item['sameSite'] = ss
            clean.append(item)
        self._context.add_cookies(clean)
        self._page.goto(LAIKE_HOME, wait_until='domcontentloaded', timeout=30000)
        time.sleep(3)
        return self._login_state({'imported': len(clean)})

    def _op_detect_chat_params(self) -> Dict[str, Any]:
        """登录后进入会话页，从地址栏解析 groupid / life_biz_view_id。"""
        self._ensure_browser()
        try:
            self._page.goto(LAIKE_CHAT_BASE, wait_until='domcontentloaded', timeout=30000)
        except Exception:
            pass
        url = ''
        for _ in range(15):
            time.sleep(1)
            url = self._page.url or ''
            if 'groupid=' in url:
                break
        gid = re.search(r'groupid=(\d+)', url)
        biz = re.search(r'life_biz_view_id=(\d+)', url)
        out = {'ok': True, 'url': url, 'groupid': gid.group(1) if gid else '',
               'biz_view_id': biz.group(1) if biz else ''}
        if out['groupid']:
            self._groupid = out['groupid']
            self._biz_view_id = out['biz_view_id']
        return out

    def _op_get_lead_info(self) -> Dict[str, Any]:
        """读取线索卡片中的手机号/微信号（已按实测 DOM 校准）。"""
        self._ensure_browser()
        js = """
        () => {
            const out = {has_lead: false, phone: '', wechat: ''};
            const phoneEl = document.querySelector('.leadsCard-oxg6iX .leadsPhone-pcW7B_, [class*="leadsCard-"] [class*="leadsPhone-"]');
            if (phoneEl) {
                out.has_lead = true;
                out.phone = (phoneEl.textContent || '').trim();
            }
            const wxEl = document.querySelector('.leadsCard-oxg6iX .leadsWechat-xvb0sN, [class*="leadsCard-"] [class*="leadsWechat-"]');
            if (wxEl) {
                out.has_lead = true;
                out.wechat = (wxEl.textContent || '').trim();
            }
            return out;
        }
        """
        try:
            return self._page.evaluate(js) or {}
        except Exception:
            return {}

