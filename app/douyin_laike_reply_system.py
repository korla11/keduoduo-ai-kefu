"""抖音来客商家后台私信自动回复模块。
独立于 douyin_reply_system.py（个人版 douyin.com），专门处理 life.douyin.com。
复用 AI 回复、留资识别、二次触达逻辑，仅浏览器交互层使用 DouyinLaikeWorker。
"""
from __future__ import annotations

import logging
import os
import re
import threading
import time
from collections import defaultdict
from typing import Any, Dict, List, Optional

from douyin_laike_playwright import DouyinLaikeWorker
from dashboard_metrics import record_dashboard_event
from flask import jsonify, request, send_from_directory
import json

logger = logging.getLogger(__name__)

# 全局单例
laike_system: Optional['DouyinLaikeReplySystem'] = None

LAIKE_STATE_FILE = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    'douyin_laike_state.json',
)


def _load_laike_state() -> Dict[str, Any]:
    try:
        if os.path.exists(LAIKE_STATE_FILE):
            with open(LAIKE_STATE_FILE, 'r', encoding='utf-8') as f:
                return json.load(f)
    except Exception:
        pass
    return {}


def _save_laike_state(state: Dict[str, Any]) -> None:
    try:
        with open(LAIKE_STATE_FILE, 'w', encoding='utf-8') as f:
            json.dump(state, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


def get_laike_system() -> 'DouyinLaikeReplySystem':
    global laike_system
    if laike_system is None:
        laike_system = DouyinLaikeReplySystem()
    return laike_system

# 复用主系统的 AI 能力（延迟导入，避免循环依赖）
_ai = {}

def _get_ai():
    if not _ai:
        from ai_reply_service import (
            build_conversation_context,
            generate_reply_decision as generate_ai_decision,
            platform_enabled,
            record_conversation_exchange,
        )
        _ai['build_ctx'] = build_conversation_context
        _ai['gen'] = generate_ai_decision
        _ai['enabled'] = platform_enabled
        _ai['record'] = record_conversation_exchange
    return _ai


class DouyinLaikeReplySystem:
    """抖音来客自动回复系统。"""

    AI_PLATFORM_KEY = 'douyin_laike'

    def __init__(self, config: Optional[Dict[str, Any]] = None):
        self.config = config or {}
        # 从持久化文件恢复状态
        _saved = _load_laike_state()
        if _saved.get('config'):
            # 合并保存的配置（groupid 等）
            for k, v in _saved['config'].items():
                if k not in self.config:
                    self.config[k] = v
        self._laike_logged_in = bool(_saved.get('logged_in', False))
        self._monitoring_wanted = bool(_saved.get('monitoring_enabled', False))
        self._monitoring_since = float(_saved.get('monitoring_since') or 0) if self._monitoring_wanted else 0
        self.monitoring = False
        self.monitor_thread: Optional[threading.Thread] = None
        self._monitor_stop = threading.Event()
        self._browser_use_lock = threading.Lock()
        self._browser: Optional[DouyinLaikeWorker] = None
        self._login_lock = threading.Lock()
        self._account_detected = False
        # IM 触发：WebSocket 帧 / IM 接口响应到达时唤醒监控循环
        self._wake = threading.Event()
        self._last_trigger_ts = 0.0
        self._last_trigger_log = 0.0
        self.live: Dict[str, Any] = {'scan_count': 0, 'last_scan': '', 'last_trigger': '',
                                     'trigger_count': 0, 'conversations': 0}
        # 已回复过的客户消息指纹（user_key|消息文本），防止重复回复
        self._replied_msgs: Dict[str, set] = {}
        # 状态
        self.last_seen_preview: Dict[str, str] = {}
        self._sent_reply_texts: Dict[str, set] = {}
        self._last_reply_time: Dict[str, float] = {}
        self._last_incoming_time: Dict[str, float] = {}
        self._follow_up_sent: Dict[str, float] = {}
        self._has_left_contact: Dict[str, bool] = {}
        self._user_contact_info: Dict[str, Dict[str, str]] = {}
        self._follow_up_thread: Optional[threading.Thread] = None
        self.logs: List[Dict[str, Any]] = []
        self._storage_path = os.path.join(
            os.path.dirname(os.path.abspath(__file__)),
            'douyin_laike_storage.json',
        )

    def _persist_state(self) -> None:
        """持久化登录态和配置。"""
        _save_laike_state({
            'logged_in': bool(getattr(self, '_laike_logged_in', False)),
            'monitoring_enabled': bool(getattr(self, '_monitoring_wanted', False)),
            'monitoring_since': float(getattr(self, '_monitoring_since', 0) or 0) if getattr(self, '_monitoring_wanted', False) else 0,
            'config': {
                'laike_groupid': self.config.get('laike_groupid', ''),
                'laike_biz_view_id': self.config.get('laike_biz_view_id', ''),
                'laike_account_name': self.config.get('laike_account_name', ''),
                'laike_user_name': self.config.get('laike_user_name', ''),
                'laike_stores': self.config.get('laike_stores', []),
                'headless': self.config.get('headless', True),
                'message_check_interval': self.config.get('message_check_interval', 3.0),
            },
        })

    def get_status(self) -> Dict[str, Any]:
        """供仪表盘调用的状态。"""
        return {
            'monitoring': self.monitoring,
            'logged_in': bool(getattr(self, '_laike_logged_in', False) or self.config.get('laike_groupid')),
            'rules_count': 0,
            'monitor_starting': False,
            'session_expired': False,
        }

    # ── 日志 ──

    def add_log(self, message: str, level: str = 'info') -> None:
        entry = {
            'time': time.strftime('%Y-%m-%d %H:%M:%S'),
            'message': message,
            'level': level,
        }
        self.logs.append(entry)
        if len(self.logs) > 500:
            self.logs = self.logs[-500:]
        logger.info('[laike] %s', message)
        # 同步写文件，重启不丢
        try:
            log_file = os.path.join(
                os.path.dirname(os.path.abspath(__file__)),
                'douyin_laike.log',
            )
            with open(log_file, 'a', encoding='utf-8') as f:
                f.write(f"{entry['time']} [{level}] {message}\n")
            if time.time() - getattr(self, '_last_log_trim', 0) > 3600:
                self._last_log_trim = time.time()
                self._trim_log_file(log_file)
        except Exception:
            pass

    LOG_KEEP_DAYS = 3

    @classmethod
    def _trim_log_file(cls, log_file: str) -> None:
        """只保留最近 LOG_KEEP_DAYS 天的日志（按行首日期）。"""
        import datetime as _dt
        cutoff = (_dt.datetime.now() - _dt.timedelta(days=cls.LOG_KEEP_DAYS)).strftime('%Y-%m-%d %H:%M:%S')
        try:
            with open(log_file, 'r', encoding='utf-8', errors='replace') as f:
                lines = f.readlines()
        except Exception:
            return
        start = 0
        for i, line in enumerate(lines):
            if re.match(r'^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}', line):
                if line[:19] >= cutoff:
                    start = i
                    break
        else:
            start = len(lines)
        if start <= 0:
            return
        tmp = log_file + '.tmp'
        with open(tmp, 'w', encoding='utf-8') as f:
            f.writelines(lines[start:])
        os.replace(tmp, log_file)

    # ── 浏览器 ──

    def _ensure_browser(self, headless: Optional[bool] = None, force_visible: bool = False) -> DouyinLaikeWorker:
        if headless is None:
            headless = self.config.get('headless', True)
        # 如果已存在浏览器但 headless 模式不匹配，重建
        if self._browser is not None:
            try:
                cur_headless = getattr(self._browser, 'headless', None)
                if cur_headless != headless:
                    try:
                        self._browser.stop_worker()
                    except Exception:
                        pass
                    self._browser = None
            except Exception:
                pass
        if self._browser is None:
            self._browser = DouyinLaikeWorker(
                storage_path=self._storage_path,
                headless=headless,
            )
            self._browser.set_chat_params(
                groupid=str(self.config.get('laike_groupid') or ''),
                biz_view_id=str(self.config.get('laike_biz_view_id') or ''),
            )
        return self._browser

    # ── 留资识别（与主系统一致 + 来客原生线索面板） ──

    _LEAD_CARD_RE = re.compile(r'已留资|留资成功|提交成功|已提交.{0,8}(手机|电话|号码|信息|表单)|(手机号|电话|号码).{0,6}(已提交|已授权|已留)|授权.{0,4}手机号|获取到.{0,4}(手机号|电话)')

    def _contact_left_in_msgs(self, msgs) -> bool:
        """会话消息中是否已有客户留下的联系方式（客户发的号码/微信，或留资卡片/系统提示已提交）。"""
        for m in msgs or []:
            t = (getattr(m, 'text', '') or '')
            if not getattr(m, 'is_self', False) and self._detect_contact_info(t):
                return True
            if self._LEAD_CARD_RE.search(t) and not re.search(r'留个电话|留下.{0,4}(电话|联系方式)', t):
                return True
        return False

    _WX_ID_RE = re.compile(r'^[a-zA-Z][-_a-zA-Z0-9]{5,19}$')

    @classmethod
    def _contact_type(cls, text: str) -> str:
        """返回 '手机' / '微信' / ''。微信号：整条消息基本就是一个 ID（需含数字，或大小写混合），或带 微信/vx/wx/v信/加我 + ID。"""
        import unicodedata as _ud
        t = _ud.normalize('NFKC', text or '').strip()  # ①⑤… / 全角数字 → 半角
        if not t:
            return ''
        if re.search(r'(?<!\d)1[3-9]\d{9}(?!\d)', re.sub(r'[\s\-—_.·]', '', t)):
            return '手机'
        m = re.search(r'(微信|v信|vx|wx|weixin|加我)[号是:：\s]*([a-zA-Z][-_a-zA-Z0-9]{5,19})', t, re.I)
        if m and m.group(2).lower() not in ('weixin', 'wechat'):
            return '微信'
        tok = re.sub(r'^[\s，,。.！!～~]+|[\s，,。.！!～~]+$', '', t)
        if cls._WX_ID_RE.match(tok) and (re.search(r'\d', tok) or (re.search(r'[a-z]', tok) and re.search(r'[A-Z]', tok) and len(tok) >= 6)):
            return '微信'
        return ''

    def _detect_contact_info(self, text: str) -> bool:
        if not text:
            return False
        import unicodedata as _ud
        text = _ud.normalize('NFKC', text)
        if re.search(r'(?<!\d)1[3-9]\d{9}(?!\d)', text) or re.search(r'(?<!\d)1[3-9]\d{9}(?!\d)', re.sub(r'[\s\-—_.·]', '', text)):
            return True
        if self._contact_type(text) == '微信':
            return True
        wechat_patterns = [
            r'微信[号:：]?\s*[a-zA-Z][a-zA-Z0-9_-]{5,19}',
            r'vx[号:：]?\s*[a-zA-Z][a-zA-Z0-9_-]{5,19}',
            r'加我(微信|vx|V[Xx])',
            r'我的(微信|vx)是',
        ]
        for p in wechat_patterns:
            if re.search(p, text, re.IGNORECASE):
                return True
        phone_patterns = [
            r'电话[是号:：]?\s*\d[\d\s-]{6,}',
            r'手机[号:：]?\s*\d[\d\s-]{6,}',
        ]
        for p in phone_patterns:
            if re.search(p, text, re.IGNORECASE):
                return True
        return False

    def _check_native_lead(self, user_key: str) -> bool:
        """检查来客原生线索面板（右侧关联线索）。"""
        try:
            with self._browser_use_lock:
                browser = self._ensure_browser()
                info = browser.get_lead_info()
            if info.get('has_lead'):
                self._has_left_contact[user_key] = True
                self.add_log(
                    f'📇 [{user_key}] 来客原生面板检测到留资: '
                    f'{info.get("phone") or info.get("wechat")}', 'info',
                )
                return True
        except Exception as exc:
            logger.warning('读取原生线索失败: %s', exc)
        return False

    # ── 防自己回自己 ──

    def _is_own_message(self, user_key: str, text: str) -> bool:
        sent = self._sent_reply_texts.get(user_key) or set()
        t = (text or '').strip()
        if t in sent:
            return True
        for s in sent:
            if s and (s in t or t in s):
                return True
            prefix = s[:30]
            if len(prefix) >= 10 and prefix in t:
                return True
        return False

    # ── 监控 ──

    # ── 二次触达待办持久化（重启不丢）──

    def _fu_file(self) -> str:
        return os.path.join(os.path.dirname(os.path.abspath(__file__)), 'laike_followup_state.json')

    def _fu_snapshot(self) -> Dict[str, Any]:
        keys = set(self._last_reply_time) | set(self._follow_up_sent) | set(self._last_incoming_time)
        return {k: {'last_reply': self._last_reply_time.get(k), 'last_incoming': self._last_incoming_time.get(k),
                    'follow_up_sent': self._follow_up_sent.get(k), 'has_lead': bool(self._has_left_contact.get(k)),
                    'nickname': (self._user_contact_info.get(k) or {}).get('nickname') or k.replace('laike:', '')}
                for k in keys}

    def _save_followup_state(self) -> None:
        try:
            snap = self._fu_snapshot()
            data = json.dumps(snap, ensure_ascii=False, sort_keys=True)
            if data == getattr(self, '_fu_last_saved', None):
                return
            tmp = self._fu_file() + '.tmp'
            with open(tmp, 'w', encoding='utf-8') as f:
                f.write(data)
            os.replace(tmp, self._fu_file())
            self._fu_last_saved = data
        except Exception as exc:
            logger.warning('保存二次触达状态失败: %s', exc)

    def _restore_followup_state(self) -> None:
        """启动时恢复待二次触达：优先读持久化文件；没有记录的客户从 douyin_laike.log 重建（仅最近 6 小时）。"""
        restored = 0
        try:
            with open(self._fu_file(), 'r', encoding='utf-8') as f:
                saved = json.load(f)
        except Exception:
            saved = {}
        for k, v in (saved or {}).items():
            if v.get('last_reply'):
                self._last_reply_time[k] = float(v['last_reply'])
            if v.get('last_incoming'):
                self._last_incoming_time[k] = float(v['last_incoming'])
            if v.get('follow_up_sent'):
                self._follow_up_sent[k] = float(v['follow_up_sent'])
            if v.get('has_lead'):
                self._has_left_contact[k] = True
            if v.get('nickname'):
                self._user_contact_info.setdefault(k, {'nickname': v['nickname']})
            restored += 1
        # 从日志重建（文件里没有的客户）
        cutoff = time.time() - 6 * 3600
        log_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'douyin_laike.log')
        rec: Dict[str, Dict[str, float]] = {}
        try:
            with open(log_path, 'r', encoding='utf-8', errors='ignore') as f:
                for line in f:
                    m = re.match(r'^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}) \[\w+\] (.)\S* \[(.+?)\] (.*)$', line.strip())
                    if not m:
                        continue
                    ts = time.mktime(time.strptime(m.group(1), '%Y-%m-%d %H:%M:%S'))
                    if ts < cutoff:
                        continue
                    nick, body = m.group(3), m.group(4)
                    r = rec.setdefault(nick, {})
                    if body.startswith('新消息'):
                        r['in'] = ts
                        if self._detect_contact_info(body[3:].lstrip(':： ')):
                            r['lead'] = ts
                    elif body.startswith('来客已回复') or body.startswith('来客二次触达已发送'):
                        r['reply'] = ts
                    if body.startswith('客户未回复，准备二次触达'):
                        r['fu'] = ts
                    if '已留资' in body:
                        r['lead'] = ts
        except OSError:
            pass
        for nick, r in rec.items():
            if r.get('lead'):
                self._has_left_contact[f'laike:{nick}'] = True  # 历史消息里留过电话/微信
        for nick, r in rec.items():
            k = f'laike:{nick}'
            if k in saved or not r.get('reply'):
                continue
            self._last_reply_time[k] = r['reply']
            if r.get('in'):
                self._last_incoming_time[k] = r['in']
            # 与现有规则一致：客户发新消息后才重新允许触达
            if r.get('fu') and r['fu'] >= r.get('in', 0):
                self._follow_up_sent[k] = r['fu']
            if r.get('lead'):
                self._has_left_contact[k] = True
            self._user_contact_info.setdefault(k, {'nickname': nick})
            restored += 1
        # 黑名单/已屏蔽/已留资：永不进入待触达
        for k in list(self._last_reply_time):
            if k not in self._follow_up_sent and (self._has_left_contact.get(k) or self._is_blocked(k)):
                self._follow_up_sent[k] = time.time()
        if restored:
            pend = [k.replace('laike:', '') for k in self._last_reply_time
                    if k not in self._follow_up_sent and self._last_incoming_time.get(k, 0) <= self._last_reply_time[k]]
            self.add_log(f'已恢复 {restored} 个客户的二次触达状态（待触达: {", ".join(pend) or "无"}）', 'info')
        self._save_followup_state()

    def start_monitoring(self) -> Dict[str, Any]:
        if self.monitoring:
            return {'success': False, 'error': '已在监控中'}
        try:
            self._restore_followup_state()
        except Exception as exc:
            logger.warning('恢复二次触达状态失败: %s', exc)
        self.monitoring = True
        self._monitor_stop.clear()
        self.monitor_thread = threading.Thread(
            target=self._monitor_loop, daemon=True, name='laike-monitor',
        )
        self.monitor_thread.start()
        self.add_log('抖音来客私信监控已启动', 'info')
        return {'success': True}

    def stop_monitoring(self) -> Dict[str, Any]:
        self.monitoring = False
        self._monitor_stop.set()
        self._wake.set()
        self.add_log('抖音来客私信监控已停止', 'info')
        self._mon_stopped_at = time.time()
        self._start_browser_reaper()
        return {'success': True}

    # ── 内存保护：停止监控后关闭空闲浏览器；监控中内存过高时提前刷新/重启浏览器 ──
    MEM_CHROME_SOFT_MB = 650    # 超过则提前（空闲时）清缓存刷新
    MEM_CHROME_HARD_MB = 800    # 刷新后仍超过则重启浏览器
    MEM_SWAP_SOFT_MB = 1500
    IDLE_BROWSER_CLOSE_S = 300  # 停止监控 5 分钟后关闭浏览器（登录态保存在 profile，不会登出）

    @staticmethod
    def _chrome_rss_mb() -> int:
        total = 0
        try:
            for pid in os.listdir('/proc'):
                if not pid.isdigit():
                    continue
                try:
                    with open(f'/proc/{pid}/cmdline', 'rb') as f:
                        if b'douyin_laike_browser_profile' not in f.read():
                            continue
                    with open(f'/proc/{pid}/statm') as f:
                        total += int(f.read().split()[1]) * os.sysconf('SC_PAGE_SIZE')
                except (OSError, ValueError, IndexError):
                    continue
        except OSError:
            return 0
        return total // (1024 * 1024)

    @staticmethod
    def _swap_used_mb() -> int:
        try:
            info = {}
            with open('/proc/meminfo') as f:
                for line in f:
                    k, v = line.split(':', 1)
                    info[k] = int(v.split()[0])
            return (info.get('SwapTotal', 0) - info.get('SwapFree', 0)) // 1024
        except Exception:
            return 0

    def _browser_alive(self) -> bool:
        b = self._browser
        t = getattr(b, '_thread', None) if b else None
        return bool(t and t.is_alive())

    def _close_browser(self, reason: str) -> bool:
        b = self._browser
        if not b or not self._browser_alive():
            return False
        before = self._chrome_rss_mb()
        b.stop_worker()
        t = getattr(b, '_thread', None)
        if t:
            t.join(timeout=20)
        self._open_nick = ''
        self.add_log(f'🧹 {reason}：已关闭来客浏览器释放内存（浏览器 {before}MB → {self._chrome_rss_mb()}MB，登录状态保留）', 'info')
        return True

    def _start_browser_reaper(self) -> None:
        if getattr(self, '_reaper_started', False):
            return
        self._reaper_started = True

        def _loop():
            while True:
                time.sleep(60)
                try:
                    if self.monitoring or not self._browser_alive():
                        continue
                    if not getattr(self, '_laike_logged_in', False):
                        continue  # 可能正在扫码登录，不打扰
                    if time.time() - getattr(self, '_mon_stopped_at', 0) < self.IDLE_BROWSER_CLOSE_S:
                        continue
                    if not self._browser_use_lock.acquire(timeout=1):
                        continue
                    try:
                        if not self.monitoring:
                            self._close_browser('监控已停止')
                    finally:
                        self._browser_use_lock.release()
                except Exception as exc:  # noqa: BLE001
                    logger.warning('来客浏览器空闲回收失败: %s', exc)

        threading.Thread(target=_loop, daemon=True, name='laike-browser-reaper').start()

    def apply_account_info(self, info: Dict[str, Any], source: str = '') -> bool:
        """用检测到的账号信息更新配置；groupid 变化时记录日志。返回是否有变化。"""
        gid = str(info.get('groupid') or '')
        if not gid:
            return False
        old = str(self.config.get('laike_groupid') or '')
        changed = gid != old or str(info.get('biz_view_id') or '') != str(self.config.get('laike_biz_view_id') or '')
        if gid != old:  # 换了账号：旧门店信息作废
            for k in ('laike_account_name', 'laike_user_name', 'laike_stores'):
                self.config.pop(k, None)
        self.config['laike_groupid'] = gid
        self.config['laike_biz_view_id'] = str(info.get('biz_view_id') or '')
        if info.get('store_name'):
            self.config['laike_account_name'] = info['store_name']
        if info.get('user_name'):
            self.config['laike_user_name'] = info['user_name']
        if info.get('stores'):
            self.config['laike_stores'] = info['stores']
        if self._browser:
            self._browser.set_chat_params(gid, self.config['laike_biz_view_id'])
        if changed:
            self.add_log(f'🔄 [{source}] 来客账号已更新: groupid {old or "-"} → {gid}，门店 {self.config.get("laike_account_name", "")}', 'warning' if old else 'success')
        self._persist_state()
        return changed

    def reset_account_state(self) -> None:
        """清空与旧账号相关的配置与去重/基线状态（保留 headless、间隔等设置）。"""
        for k in ('laike_groupid', 'laike_biz_view_id', 'laike_account_name', 'laike_user_name', 'laike_stores'):
            self.config.pop(k, None)
        self._laike_logged_in = False
        self._account_detected = False
        for d in (self.last_seen_preview, self._sent_reply_texts, self._last_reply_time, self._last_incoming_time,
                  self._follow_up_sent, self._has_left_contact, self._user_contact_info, self._replied_msgs):
            d.clear()
        self._persist_state()

    def switch_account(self, dry_run: bool = False) -> Dict[str, Any]:
        """切换账号：停止监控 → 退出并清 Cookie → 重置账号状态。dry_run 只返回将执行的动作。"""
        plan = {'stop_monitoring': self.monitoring, 'logout_clear_cookies': True,
                'reset_keys': ['laike_groupid', 'laike_biz_view_id', 'laike_account_name', 'laike_user_name', 'laike_stores'],
                'keep': {k: self.config.get(k) for k in ('headless', 'message_check_interval')}}
        if dry_run:
            return {'success': True, 'dry_run': True, 'plan': plan}
        if self.monitoring:
            self.stop_monitoring()
            t = self.monitor_thread
            if t is not None:
                t.join(timeout=30)
        with self._browser_use_lock:
            b = self._ensure_browser()
            b.start_worker()
            res = b.login_call('logout_clear', timeout=60)
        self.reset_account_state()
        self.add_log('🔁 已退出旧来客账号并清空会话，请重新登录', 'warning')
        return {'success': True, 'cleared': not res.get('has_session')}

    def _on_im_activity(self, source: str = 'ws') -> None:
        """worker 线程回调：只设置事件，不做页面操作。"""
        now = time.time()
        self._last_trigger_ts = now
        self.live['trigger_count'] = self.live.get('trigger_count', 0) + 1
        self.live['last_trigger'] = time.strftime('%H:%M:%S') + f' ({source})'
        self._wake.set()

    def live_status(self) -> Dict[str, Any]:
        b = self._browser
        st = dict(self.live)
        st['monitoring'] = self.monitoring
        st['ws_connected'] = bool(b and b.ws_open > 0)
        st['ws_frames'] = b.ws_frames if b else 0
        st['ws_urls'] = list(b.ws_urls) if b else []
        return st

    def _wait_next(self, interval: float) -> bool:
        """等待下一轮：IM 触发立即唤醒（500ms 防抖），否则按兜底间隔轮询。返回 True 表示应停止。"""
        fired = self._wake.wait(timeout=interval)
        if self._monitor_stop.is_set():
            return True
        if fired:
            # 防抖：等 500ms 让同一批帧到齐
            time.sleep(0.2)
            self._wake.clear()
            # WebSocket 触发只计入状态栏计数，不写日志
        return self._monitor_stop.is_set()

    # ── 新消息判定 ──

    @staticmethod
    def _parse_conv_time(text: str):
        """把会话列表的时间文本（刚刚 / N分钟 / N小时 / HH:MM / 昨天 / 日期）换算成时间戳；无法判断返回 None。"""
        import datetime as _dt
        t = (text or '').strip()
        if not t:
            return None
        now = time.time()
        if t in ('刚刚', '刚才'):
            return now
        m = re.match(r'^(\d+)\s*秒', t)
        if m:
            return now - int(m.group(1))
        m = re.match(r'^(\d+)\s*分钟', t)
        if m:
            return now - int(m.group(1)) * 60
        m = re.match(r'^(\d+)\s*小时', t)
        if m:
            return now - int(m.group(1)) * 3600
        m = re.match(r'^(\d{1,2}):(\d{2})$', t)
        if m:
            d = _dt.datetime.now().replace(hour=int(m.group(1)), minute=int(m.group(2)), second=0, microsecond=0)
            ts = d.timestamp()
            return ts - 86400 if ts > now + 120 else ts
        return now - 86400 * 2  # 昨天 / 星期 / 日期：视为旧消息

    @staticmethod
    def _msg_signature(msgs) -> str:
        tail = msgs[-4:]
        return f'{len(msgs)}#' + '||'.join(f'{int(m.is_self)}:{(m.msg_id or "")}:{(m.text or "").strip()}' for m in tail)

    # ── 广告/商家号、黑名单、机器人循环防护 ──

    _AD_NAME_RE = re.compile(r'官方号|工作室|旗舰店|招商|专营店|企业号')
    _AD_TEXT_RE = re.compile(r'我是商家专属客服|商家专属客服|有需要随时联系|有需要随时找我|有任何需要随时|评价消息卡片|诚邀您评价|此消息由「|祝您生活愉快|祝你生活愉快|欢迎光临本店|感谢您的关注')

    def _blocked_file(self) -> str:
        return os.path.join(os.path.dirname(self._fu_file()), 'laike_blocked.json')

    def _blocked(self) -> Dict[str, Any]:
        if getattr(self, '_blocked_cache', None) is None:
            try:
                with open(self._blocked_file(), 'r', encoding='utf-8') as f:
                    self._blocked_cache = json.load(f) or {}
            except Exception:
                self._blocked_cache = {}
        return self._blocked_cache

    def _is_blocked(self, user_key: str, nickname: str = '') -> str:
        nick = (nickname or user_key.replace('laike:', '')).strip()
        try:
            from ai_reply_service import get_platform_settings
            if nick in (get_platform_settings(self.AI_PLATFORM_KEY).get('blacklist') or []):
                return '黑名单'
        except Exception:
            pass
        b = self._blocked().get(user_key)
        return (b or {}).get('reason', '') if b else ''

    def _block(self, user_key: str, nickname: str, reason: str) -> None:
        self._blocked()[user_key] = {'nickname': nickname, 'reason': reason, 'time': time.strftime('%Y-%m-%d %H:%M:%S')}
        try:
            tmp = self._blocked_file() + '.tmp'
            with open(tmp, 'w', encoding='utf-8') as f:
                json.dump(self._blocked_cache, f, ensure_ascii=False, indent=2)
            os.replace(tmp, self._blocked_file())
        except Exception as exc:
            logger.warning('保存来客屏蔽列表失败: %s', exc)
        self.add_log(f'🛑 [{nickname}] 已停止自动回复：{reason}', 'warning')

    def _handoff(self, user_key: str, nickname: str, text: str, reason: str) -> None:
        try:
            from ai_handoff_store import ai_handoff_store
            ai_handoff_store.add(self.AI_PLATFORM_KEY, f'{user_key}|{abs(hash(text))}', nickname, user_key,
                                 text or '(无文本)', reason, {'nickname': nickname})
        except Exception as exc:
            logger.warning('加入人工待回失败: %s', exc)
        self.add_log(f'🧑‍💼 [{nickname}] 不自动回复，已加入人工待回：{reason}', 'warning')

    def _peer_industry(self, nickname: str) -> int:
        """对方抖音号的 IndustryCode（>0 = 企业号/蓝V）。打开会话后页面接口返回，最多等 2 秒。"""
        cache = self.__dict__.setdefault('_peer_cache', {})
        if cache.get(nickname):
            return cache[nickname]
        b = self._browser
        if b is None:
            return 0
        tries = 1 if nickname in cache else 5  # 首次最多等 2 秒，之后只快速读一次
        for _ in range(tries):
            try:
                with self._browser_use_lock:
                    peers = (b.login_call('peer_info', timeout=10) or {}).get('peers') or {}
            except Exception:
                peers = {}
            for n, v in peers.items():
                cache[n] = int((v or {}).get('industry') or 0)
            if nickname in cache:
                return cache[nickname]
            time.sleep(0.4)
        cache.setdefault(nickname, 0)
        return 0

    def _guard_reason(self, user_key: str, nickname: str, msg_text: str) -> str:
        """按「单条消息」判断是否不自动回复（空串 = 正常回复）。
        手动黑名单/循环防护屏蔽的会话始终不回；商家机器人话术只跳过该条；
        昵称关键词、企业号/蓝V 只作为机器人嫌疑信号，不单独屏蔽。"""
        r = self._is_blocked(user_key, nickname)
        if r:
            return r
        try:
            from ai_reply_service import get_platform_settings
            _gs = get_platform_settings(self.AI_PLATFORM_KEY)
        except Exception:
            _gs = {}
        _nk = _gs.get('ad_name_keywords')
        _tk = _gs.get('ad_text_keywords')
        name_hit = any(k in (nickname or '') for k in _nk) if _nk is not None else bool(self._AD_NAME_RE.search(nickname or ''))
        text_hit = any(k in (msg_text or '') for k in _tk) if _tk is not None else bool(self._AD_TEXT_RE.search(msg_text or ''))
        ind = self._peer_industry(nickname) if _gs.get('block_enterprise', True) else 0
        suspect = name_hit or bool(ind)
        last_reply = self._last_reply_time.get(user_key, 0)
        instant = bool(last_reply) and time.time() - last_reply <= 15
        rounds = self.__dict__.setdefault('_fast_rounds', {})
        # 机器人循环：对方在我方回复后 15 秒内回、且是商家话术（或嫌疑账号秒回），连续 3 轮
        if instant and (text_hit or suspect):
            rounds[user_key] = rounds.get(user_key, 0) + 1
        else:
            rounds[user_key] = 0
        if rounds[user_key] >= 3 and _gs.get('loop_guard', True):
            sig = ('，嫌疑：' + ('昵称' if name_hit else '') + (f'企业号IndustryCode={ind}' if ind else '')) if suspect else ''
            self._block(user_key, nickname, f'疑似机器人循环（连续 3 轮秒回商家话术{sig}）')
            return '疑似机器人循环（连续 3 轮秒回）'
        if text_hit:
            return '商家机器人话术（仅跳过本条）'
        return ''

    def send_manual_reply(self, nickname: str, text: str) -> Dict[str, Any]:
        """人工待回：人工输入的回复由浏览器发送。"""
        try:
            with self._browser_use_lock:
                b = self._ensure_browser()
                ok = b.send_text(nickname, text)
            if ok:
                self._sent_reply_texts.setdefault(f'laike:{nickname}', set()).add(text.strip())
                self.add_log(f'✅ [{nickname}] 人工回复已发送', 'success')
            return {'success': bool(ok), 'error': '' if ok else '发送失败'}
        except Exception as exc:
            return {'success': False, 'error': str(exc)}

    def _process_messages(self, user_key, nickname, msgs, browser, allow_unseen=True) -> None:
        """根据消息区内容判断是否有待回复的客户新消息（不依赖未读角标）。"""
        # 平台自动发送的消息（自动回复配置/自动挽留/欢迎语）不算我方回复：忽略后再判断
        _lead_before = bool(self._has_left_contact.get(user_key))
        if msgs and not self._has_left_contact.get(user_key) and self._contact_left_in_msgs(msgs):
            self._has_left_contact[user_key] = True
            self.add_log(f'📇 [{nickname}] 会话中检测到已留联系方式，停止引导留资/二次触达', 'info')
        if msgs and allow_unseen:
            self._check_silent_entry(user_key, nickname, msgs)
        msgs = [m for m in (msgs or []) if not getattr(m, 'auto', False)]
        if not msgs:
            return
        sig = self._msg_signature(msgs)
        prev_sig = self._conv_sig.get(user_key)
        self._conv_sig[user_key] = sig
        if prev_sig == sig:
            return
        latest = msgs[-1]
        msg_text = (latest.text or '').strip()
        if not msg_text or latest.is_self or self._is_own_message(user_key, msg_text):
            return
        if prev_sig is None and not allow_unseen:
            return  # 首次看到的旧会话：仅记录
        done = self._replied_msgs.setdefault(user_key, set())
        if sig in done:
            return
        done.add(sig)
        if len(done) > 200:
            self._replied_msgs[user_key] = set(list(done)[-100:])
        fp = sig
        self.add_log(f'📩 [{nickname}] 新消息: {msg_text[:60]}', 'info')
        self._last_incoming_time[user_key] = time.time()
        self._current_event_key = f'{user_key}|{abs(hash(fp))}|{int(time.time())}'
        record_dashboard_event(self.AI_PLATFORM_KEY, 'inbound', self._current_event_key, contact_id=user_key)
        _first_contact = self._detect_contact_info(msg_text) and not _lead_before
        if self._detect_contact_info(msg_text):
            if not self._has_left_contact.get(user_key):
                record_dashboard_event(self.AI_PLATFORM_KEY, 'lead', f'{user_key}|lead', contact_id=user_key)
                self.add_log(f'📇 [{nickname}] 已留资（{self._contact_type(msg_text) or "联系方式"}），后续只致谢，不再引导留资/二次触达', 'success')
            self._has_left_contact[user_key] = True
        self._user_contact_info[user_key] = {'nickname': nickname}
        self._follow_up_sent.pop(user_key, None)
        _why = self._guard_reason(user_key, nickname, msg_text)
        if _why:
            self._handoff(user_key, nickname, msg_text, _why)
            self._follow_up_sent[user_key] = time.time()  # 跳过的消息不触发二次触达
            return
        self._takeover_failed = False
        self._reply_to_message(user_key, nickname, msg_text, browser)
        if self._takeover_failed:
            done.discard(sig)
            self._conv_sig.pop(user_key, None)  # 下次扫描重新处理
            return
        # 回复后刷新签名，避免把自己的回复当作变化再次处理
        try:
            with self._browser_use_lock:
                after = browser.read_messages(limit=10)
            if after:
                self._conv_sig[user_key] = self._msg_signature(after)
        except Exception:
            pass

    # ── 进线未发言：只有平台自动消息的新会话，主动发一句（每会话一次，持久化）──
    _PLATFORM_SELF_RE = re.compile(r'此消息由「|准备开店|已经有店|想咨询|合作吗|欢迎|您好|你好')

    def _greet_file(self) -> str:
        return os.path.join(os.path.dirname(self._fu_file()), 'laike_greeted.json')

    def _greeted(self) -> Dict[str, Any]:
        if getattr(self, '_greeted_cache', None) is None:
            try:
                with open(self._greet_file(), 'r', encoding='utf-8') as f:
                    self._greeted_cache = json.load(f) or {}
            except Exception:
                self._greeted_cache = {}
        return self._greeted_cache

    def _mark_greeted(self, user_key: str, nickname: str, status: str) -> None:
        self._greeted()[user_key] = {'nickname': nickname, 'status': status, 'time': time.strftime('%Y-%m-%d %H:%M:%S')}
        try:
            tmp = self._greet_file() + '.tmp'
            with open(tmp, 'w', encoding='utf-8') as f:
                json.dump(self._greeted_cache, f, ensure_ascii=False, indent=2)
            os.replace(tmp, self._greet_file())
        except Exception as exc:
            logger.warning('保存进线问候记录失败: %s', exc)

    def _is_platform_msg(self, m) -> bool:
        t = (m.text or '').strip()
        if getattr(m, 'auto', False) or not t:
            return True
        if '评价' in t and ('卡片' in t or '邀请' in t or '诚邀' in t):
            return True
        if m.is_self:
            # 我方侧但未带「此消息由」标记：只有欢迎语样式才算平台消息，否则视为人工已发
            return bool(self._PLATFORM_SELF_RE.search(t)) and t not in self._sent_reply_texts.get(f'laike:{m.sender}', set())
        return False

    def _greet_settings(self):
        try:
            from ai_reply_service import get_platform_settings
            ps = get_platform_settings(self.AI_PLATFORM_KEY)
        except Exception:
            ps = {}
        return (ps.get('silent_greet_enabled', True), (ps.get('silent_greet_text') or '在的，亲').strip(),
                float(ps.get('silent_greet_delay', 0) or 0))

    def _greet_skip_reason(self, user_key: str, nickname: str) -> str:
        if user_key in self._greeted():
            return '已问候过'
        if self._has_left_contact.get(user_key):
            return '已留资'
        r = self._is_blocked(user_key, nickname)
        if r:
            return r
        if self._last_reply_time.get(user_key):
            return '我方已回复过'
        return ''

    def _check_silent_entry(self, user_key: str, nickname: str, msgs) -> None:
        on, _text, delay = self._greet_settings()
        due = self.__dict__.setdefault('_greet_due', {})
        if not on or not msgs:
            return
        if not all(self._is_platform_msg(m) for m in msgs):
            due.pop(user_key, None)  # 客户已发言或有人工消息：走正常流程
            return
        if user_key in due or self._greet_skip_reason(user_key, nickname):
            return
        _ct = self._last_conv_time.get(user_key)
        if _ct is not None and time.time() - _ct > 180:
            return  # 不是刚进线（重启前/历史会话）：不补发
        due[user_key] = (time.time() + delay, nickname)
        self.__dict__.setdefault('_greet_seen', {})[user_key] = time.time()
        self.add_log(f'👀 [{nickname}] 进线未发言（仅平台自动消息），{delay:.0f} 秒后发送问候', 'info')

    def _run_due_greets(self, browser) -> None:
        due = self.__dict__.setdefault('_greet_due', {})
        now = time.time()
        for user_key, (t_due, nickname) in sorted(due.items(), key=lambda kv: kv[1][0]):
            if t_due > now or not self.monitoring:
                continue
            due.pop(user_key, None)
            _seen = self.__dict__.setdefault('_greet_seen', {}).pop(user_key, t_due)
            on, text, _d = self._greet_settings()
            why = self._greet_skip_reason(user_key, nickname) if on else '已关闭'
            if why:
                continue
            _t0 = time.time()
            try:
                with self._browser_use_lock:
                    browser.open_conversation(nickname)
                    msgs = browser.read_messages(limit=10)
                self._open_nick = nickname
                if not msgs or not all(self._is_platform_msg(m) for m in msgs):
                    continue  # 客户已开口：交给正常回复流程
                with self._browser_use_lock:
                    tk = browser.login_call('ensure_takeover', timeout=40)
                if not tk.get('ok'):
                    self.add_log(f'⚠️ [{nickname}] 人工接管失败，进线问候稍后重试：{tk.get("error", "")}', 'warning')
                    due[user_key] = (time.time() + 10, nickname)
                    continue
                with self._browser_use_lock:
                    ok = browser.send_text(nickname, text)
            except Exception as exc:
                ok = False
                self.add_log(f'❌ [{nickname}] 进线问候发送异常: {exc}', 'error')
            if ok:
                _cost = time.time() - _t0
                self._sent_reply_texts.setdefault(user_key, set()).add(text)
                self._mark_greeted(user_key, nickname, 'sent')
                # 问候不触发二次触达/一键留资：客户之后发言才走正常流程
                self._follow_up_sent[user_key] = time.time()
                self._save_followup_state()
                self.add_log(f'👋 [{nickname}] 进线未发言，已发送：{text}（检测→发送 {time.time() - _seen:.1f}s，打开+发送 {_cost:.1f}s）', 'success')
                try:
                    with self._browser_use_lock:
                        after = browser.read_messages(limit=10)
                    if after:
                        self._conv_sig[user_key] = self._msg_signature([m for m in after if not getattr(m, 'auto', False)])
                except Exception:
                    pass
            else:
                self._mark_greeted(user_key, nickname, 'failed')
                self.add_log(f'❌ [{nickname}] 进线问候发送失败（不再重试）', 'error')

    def _monitor_loop(self) -> None:
        with self._browser_use_lock:
            browser = self._ensure_browser()
            browser.set_im_callback(self._on_im_activity)
            browser.start_worker()
            # 先校验登录态（基于 Cookie，不跳转页面）
            try:
                if not browser.check_session():
                    self.add_log('⚠️ 来客未登录或会话已失效，请先在「账号配置」中完成登录', 'error')
                    self._laike_logged_in = False
                    self._persist_state()
                    self.monitoring = False
                    return
            except Exception as exc:
                self.add_log(f'检查登录态失败: {exc}', 'warning')
            browser.set_chat_params(
                groupid=str(self.config.get('laike_groupid') or ''),
                biz_view_id=str(self.config.get('laike_biz_view_id') or ''),
            )
            try:
                browser.navigate_chat()
            except Exception as exc:
                self.add_log(f'导航到来客会话页失败: {exc}', 'error')
                self.monitoring = False
                return
            # 守卫：当前登录账号的 groupid 与保存值不同时自动更新
            try:
                info = browser.login_call('detect_account', timeout=40, navigate=False, with_stores=False)
                if info.get('groupid'):
                    self.apply_account_info(info, '监控启动校验')
            except Exception as exc:
                self.add_log(f'账号校验失败（继续监控）: {exc}', 'warning')
        self.add_log('已进入来客私信监控循环', 'info')
        self._start_follow_up_checker()

        # 有 IM 触发时即时扫描；兜底轮询间隔放宽到 10 秒
        interval = max(10.0, float(self.config.get('message_check_interval', 3.0)))
        self.live.update({'scan_count': 0, 'trigger_count': 0, 'last_trigger': ''})
        baseline_done = False
        scan_count = 0
        # 监控起点：服务重启/部署后沿用重启前的起点（最长 6 小时），重启期间到达的客户消息不会被当成历史
        since = float(getattr(self, '_monitoring_since', 0) or 0)
        now = time.time()
        if not since or now - since > 6 * 3600:
            since = now
        self._monitoring_since = since
        self._persist_state()
        self._monitor_started_at = since
        self.add_log(f'监控起点：{time.strftime("%H:%M:%S", time.localtime(since))}（此后到达的客户消息都会处理）', 'info')
        self._last_conv_time = {}
        self._conv_sig = {}
        self._open_nick = ''

        while self.monitoring and not self._monitor_stop.is_set():
            try:
                scan_count += 1
                with self._browser_use_lock:
                    conversations = browser.list_conversations()
                self.live['scan_count'] = scan_count
                self.live['last_scan'] = time.strftime('%H:%M:%S')
                self.live['conversations'] = len(conversations)
                # 心跳日志：每 10 次扫描记录一次，证明监控 alive
                if scan_count % 10 == 1:
                    self.add_log(f'🔍 正在监控… (第{scan_count}次扫描, {len(conversations)}个会话)', 'info')
                # 0 会话时输出页面调试信息（每 10 次，和心跳同步）
                if len(conversations) == 0 and scan_count % 10 == 1:
                    try:
                        with self._browser_use_lock:
                            dbg = browser.get_page_debug()
                        url = dbg.get('url', '')
                        # 提取关键参数
                        import re as _re
                        gid = _re.search(r'groupid=(\d+)', url)
                        self.add_log(f'🐛 调试: 标题="{dbg.get("title","")}" | groupid={gid.group(1) if gid else "无"} | div数={dbg.get("div_count",0)} | 有会话特征={dbg.get("has_conv_hint",False)}', 'warning')
                    except Exception as _e:
                        self.add_log(f'获取页面调试信息失败: {_e}', 'warning')
                if not baseline_done:
                    # 基线：只记录“监控开始之前”的会话状态；开始后才到达的消息不计入基线
                    fresh = 0
                    for conv in conversations:
                        key = f'laike:{(conv.nickname or "").strip()}'
                        t = self._parse_conv_time(conv.last_time)
                        if conv.unread or (t is not None and t >= self._monitor_started_at - 90):
                            fresh += 1  # 不记基线 → 下面会被当作新会话检查
                            continue
                        self.last_seen_preview[key] = conv.preview or ''
                        self._last_conv_time[key] = t
                    baseline_done = True
                    self.add_log(f'已记录 {len(conversations) - fresh} 个会话基线（监控开始后的 {fresh} 个会话将正常检查），仅回复新消息', 'info')

                self._run_due_greets(browser)
                checked = set()
                open_nick = getattr(self, '_open_nick', '')
                if open_nick and self.monitoring:
                    _t0 = time.time()
                    with self._browser_use_lock:
                        msgs = browser.read_messages(limit=10)
                    self._t_detect = time.time() - _t0
                    checked.add(open_nick)
                    self._process_messages(f'laike:{open_nick}', open_nick, msgs, browser, allow_unseen=True)
                for conv in reversed(conversations):  # 等待最久的先处理
                    if not self.monitoring or self._monitor_stop.is_set():
                        break
                    nickname = (conv.nickname or '').strip()
                    if not nickname or '群' in nickname:
                        continue
                    user_key = f'laike:{nickname}'
                    preview = conv.preview or ''
                    if conv.has_lead and not self._has_left_contact.get(user_key):
                        self._has_left_contact[user_key] = True
                        record_dashboard_event(self.AI_PLATFORM_KEY, 'lead', f'{user_key}|lead', contact_id=user_key)
                        self.add_log(f'📇 [{nickname}] 会话列表显示已留资', 'info')
                    t = self._parse_conv_time(conv.last_time)
                    prev = self.last_seen_preview.get(user_key)
                    prev_t = self._last_conv_time.get(user_key)
                    newer = t is not None and (prev_t is None or t > prev_t + 90)
                    recent = t is not None and t >= self._monitor_started_at - 90
                    if prev is None:
                        # 基线之后出现的会话：有未读，或最后消息时间在监控开始之后（含已被标为已读的）
                        if not (conv.unread or recent or t is None):
                            self.last_seen_preview[user_key] = preview
                            self._last_conv_time[user_key] = t
                            continue
                    elif preview == prev and not conv.unread and not newer:
                        continue
                    self.last_seen_preview[user_key] = preview
                    if t is not None:
                        self._last_conv_time[user_key] = t
                    if nickname in checked:
                        continue
                    _t0 = time.time()
                    with self._browser_use_lock:
                        browser.open_conversation(nickname)
                        msgs = browser.read_messages(limit=10)
                    self._t_detect = time.time() - _t0
                    self._open_nick = nickname
                    checked.add(nickname)
                    self._process_messages(user_key, nickname, msgs, browser,
                                           allow_unseen=prev is not None or bool(conv.unread) or recent)
                    self._run_due_greets(browser)  # 进线问候优先：检测到就发，不等整轮扫描结束

                open_nick = getattr(self, '_open_nick', '')
                if open_nick and open_nick not in checked and self.monitoring:
                    with self._browser_use_lock:
                        msgs = browser.read_messages(limit=10)
                    self._process_messages(f'laike:{open_nick}', open_nick, msgs, browser, allow_unseen=True)
                self._run_due_greets(browser)

            except Exception as exc:
                logger.exception('来客监控循环异常: %s', exc)
                self.add_log(f'监控异常: {exc}', 'error')

            try:
                self._maybe_daily_reload(browser)
            except Exception as exc:
                self.add_log(f'每日页面刷新失败（继续监控）: {exc}', 'warning')
            _due = [d for d, _n in getattr(self, '_greet_due', {}).values()]
            _iv = max(0.5, min(interval, min(_due) - time.time())) if _due else interval
            if self._wait_next(_iv):
                break

    @staticmethod
    def _service_mem_mb() -> int:
        try:
            with open('/proc/self/cgroup') as f:
                cg = f.read().strip().split('::')[-1]
            with open(f'/sys/fs/cgroup{cg}/memory.current') as f:
                return int(f.read()) // (1024 * 1024)
        except Exception:
            return 0

    def _maybe_daily_reload(self, browser) -> None:
        """空闲时每 ~20 分钟重开会话页，释放浏览器内存（不登出、不中断监控；含原 04 点每日维护）。"""
        lt = time.localtime()
        today = time.strftime('%Y-%m-%d', lt)
        now = time.time()
        last = self.__dict__.setdefault('_last_reload_ts', now)
        chrome_mb, swap_mb = self._chrome_rss_mb(), self._swap_used_mb()
        mem_high = chrome_mb > self.MEM_CHROME_SOFT_MB or swap_mb > self.MEM_SWAP_SOFT_MB
        if now - last < (300 if mem_high else 1200):
            return
        # 有进线问候即将到期则推迟
        for t_due, _n in list(self.__dict__.get('_greet_due', {}).values()):
            if t_due - now < 90:
                return
        # 有回复在途（5 分钟内有客户消息尚未回复）则推迟
        now = time.time()
        for k, t_in in self._last_incoming_time.items():
            if now - t_in < 300 and t_in > self._last_reply_time.get(k, 0):
                return
        if not self._browser_use_lock.acquire(timeout=1):
            return
        try:
            before = self._service_mem_mb()
            try:
                browser.clear_cache_reload()
            except Exception as exc:
                logger.warning('清缓存重载失败，改为直接重开会话页: %s', exc)
                browser.navigate_chat()
            self._open_nick = ''
            self._last_reload_day = today
            self._last_reload_ts = time.time()
            self._save_followup_state()
            time.sleep(3)
            after = self._service_mem_mb()
        finally:
            self._browser_use_lock.release()
        chrome_after = self._chrome_rss_mb()
        why = f'内存偏高 浏览器{chrome_mb}MB/交换{swap_mb}MB' if mem_high else '定时'
        self.add_log(f'🔄 空闲维护（{why}）：已重新加载会话页（服务内存 {before}MB → {after}MB，浏览器 {chrome_mb}MB → {chrome_after}MB）', 'info')
        if chrome_after > self.MEM_CHROME_HARD_MB and self._browser_use_lock.acquire(timeout=1):
            try:
                if self._close_browser(f'浏览器内存 {chrome_after}MB 超过 {self.MEM_CHROME_HARD_MB}MB'):
                    browser.navigate_chat()  # 自动重新拉起浏览器并回到会话页
                    self.add_log('🔁 来客浏览器已重启并回到会话页，继续监控', 'info')
            finally:
                self._browser_use_lock.release()

    CONTACT_THANKS = ('收到，马上安排老师联系你～', '好的，安排老师跟你联系～', '收到啦，老师稍后联系你，留意下电话哈～',
                      '好的收到，马上让老师联系你～')

    AFTER_LEAD_REPLIES = ('老师会跟你联系，给你具体说明～', '这块老师联系你的时候会详细跟你讲～', '好的，老师稍后联系你具体说明～')

    def _send_contact_thanks(self, user_key, nickname, msg_text, reply_text, browser, tag='留资确认') -> None:
        try:
            with self._browser_use_lock:
                browser.open_conversation(nickname)
                tk = browser.login_call('ensure_takeover', timeout=40)
            if not tk.get('ok'):
                self.add_log(f'⚠️ [{nickname}] 人工接管失败，本次不发送，下次扫描重试：{tk.get("error", "")}', 'error')
                self._takeover_failed = True
                return
            with self._browser_use_lock:
                ok = browser.send_text(nickname, reply_text)
        except Exception as exc:
            ok = False
            self.add_log(f'❌ [{nickname}] 发送异常: {exc}', 'error')
        if ok:
            self._sent_reply_texts.setdefault(user_key, set()).add(reply_text)
            self._last_reply_time[user_key] = time.time()
            self.add_log(f'✅ [{nickname}] 来客已回复（{tag}）：{reply_text}', 'success')
            self._save_followup_state()
        else:
            self.add_log(f'❌ [{nickname}] 来客发送失败', 'error')

    def _reply_to_message(
        self, user_key: str, nickname: str, msg_text: str,
        browser: DouyinLaikeWorker,
    ) -> None:
        ai = _get_ai()
        # 抖音来客使用独立平台开关；未单独配置时 ai_reply_service 自动回退到抖音私信的开关
        if not ai['enabled'](self.AI_PLATFORM_KEY):
            self.add_log(f'[{nickname}] 抖音来客 AI 未启用（AI回复 → 抖音来客），跳过', 'warning')
            return
        try:
            ctx = ai['build_ctx'](self.AI_PLATFORM_KEY, user_key)
        except Exception:
            ctx = ''
        from ai_reply_service import get_platform_settings
        _ps = get_platform_settings(self.AI_PLATFORM_KEY)
        if self._has_left_contact.get(user_key) and not self._detect_contact_info(msg_text):
            # 已留资后继续咨询：只回固定短句（不调 AI、不引导留资、不提问、不触达）
            import random as _rnd
            reply_text = _rnd.choice(_ps.get('after_lead_replies') or self.AFTER_LEAD_REPLIES)
            self._send_contact_thanks(user_key, nickname, msg_text, reply_text, browser, tag='已留资后咨询')
            self._follow_up_sent[user_key] = time.time()
            return
        if self._detect_contact_info(msg_text):
            # 本条就是客户留的电话/微信：固定简短确认，不提问、不再引导
            import random as _rnd
            reply_text = _rnd.choice(_ps.get('contact_thanks') or self.CONTACT_THANKS)
            self._send_contact_thanks(user_key, nickname, msg_text, reply_text, browser)
            return
        instruction = (_ps.get('persona') or '') + '\n\n' + (_ps.get('reply_rules') or '')
        if self._has_left_contact.get(user_key):
            instruction += '\n\n【当前客户已留联系方式】不要再引导留资，只回一句简短的感谢陈述，例如“收到，马上安排老师联系你～”。'
        if ctx:
            instruction += '\n\n' + ctx
        _ta = time.time()
        try:
            decision = ai['gen'](msg_text, instruction, platform=self.AI_PLATFORM_KEY)
        except Exception as exc:
            self.add_log(f'❌ [{nickname}] AI 生成失败: {exc}', 'error')
            return
        _t_ai = time.time() - _ta
        reply_text = (decision.get('reply') or '').strip()
        if reply_text and self._is_question(reply_text):
            reply_text = self._fix_question(nickname, reply_text,
                                            lambda: (ai['gen'](msg_text, instruction + '\n\n上一版回复含有提问，这次只能用陈述句，绝对不能有问号或吗/呢等问句。',
                                                               platform=self.AI_PLATFORM_KEY).get('reply') or '').strip())
        if not reply_text:
            self.add_log(f'❌ [{nickname}] AI 回复为空，已跳过', 'error')
            return
        # 智能客服（机器人）接待中 → 先人工接管，失败则不发送、下次扫描重试
        _tk0 = time.time()
        try:
            with self._browser_use_lock:
                browser.open_conversation(nickname)
                tk = browser.login_call('ensure_takeover', timeout=40)
        except Exception as exc:
            tk = {'ok': False, 'error': str(exc)}
        if tk.get('took_over'):
            self.add_log(f'🤝 [{nickname}] 已人工接管（原为智能客服接待）', 'success')
        elif not tk.get('ok'):
            self.add_log(f'⚠️ [{nickname}] 人工接管失败，本次不发送，下次扫描重试：{tk.get("error", "")}', 'error')
            self._takeover_failed = True
            return
        _t_tk = time.time() - _tk0
        _ts = time.time()
        try:
            with self._browser_use_lock:
                ok = browser.send_text(nickname, reply_text)
        except Exception as exc:
            self.add_log(f'❌ [{nickname}] 发送异常: {exc}', 'error')
            return
        if ok:
            self._sent_reply_texts.setdefault(user_key, set()).add(reply_text.strip())
            self._last_reply_time[user_key] = time.time()
            try:
                ai['record'](
                    self.AI_PLATFORM_KEY, user_key, msg_text, reply_text,
                    f'laike:{user_key}:{int(time.time())}',
                )
            except Exception:
                pass
            ek = getattr(self, '_current_event_key', '') or f'{user_key}|{int(time.time())}'
            record_dashboard_event(self.AI_PLATFORM_KEY, 'reply_success', ek, contact_id=user_key, reply_mode='ai',
                                   value=max(0.0, time.time() - self._last_incoming_time.get(user_key, time.time())))
            self.add_log(f'✅ [{nickname}] 来客已回复', 'success')
            _t_send = time.time() - _ts
            _td = float(getattr(self, '_t_detect', 0) or 0)
            self.add_log(f'⏱ [{nickname}] 耗时：检测/打开 {_td:.1f}s · AI {_t_ai:.1f}s · 接管检查 {_t_tk:.1f}s · 发送 {_t_send:.1f}s · 检测→发送完成 {time.time() - self._last_incoming_time.get(user_key, time.time()):.1f}s', 'info')
            self._save_followup_state()
        else:
            ek = getattr(self, '_current_event_key', '') or f'{user_key}|{int(time.time())}'
            record_dashboard_event(self.AI_PLATFORM_KEY, 'reply_failure', ek, contact_id=user_key, reply_mode='ai')
            self.add_log(f'❌ [{nickname}] 来客发送失败', 'error')

    # ── 120秒二次触达 ──

    def _start_follow_up_checker(self) -> None:
        t = self._follow_up_thread
        if t is not None and t.is_alive():
            return
        self._follow_up_thread = threading.Thread(
            target=self._follow_up_loop, daemon=True, name='laike-followup',
        )
        self._follow_up_thread.start()

    def _follow_up_loop(self) -> None:
        from ai_reply_service import get_platform_settings
        while self.monitoring and not self._monitor_stop.is_set():
            try:
                fs = get_platform_settings(self.AI_PLATFORM_KEY)
                if not fs.get('follow_up_enabled', True):
                    if self._monitor_stop.wait(timeout=10):
                        break
                    continue
                delay = int(fs.get('follow_up_delay', 120))
                now = time.time()
                for user_key in list(self._last_reply_time.keys()):
                    if not self.monitoring or self._monitor_stop.is_set():
                        break
                    if user_key in self._follow_up_sent:
                        continue
                    if self._is_blocked(user_key):
                        continue
                    if self._has_left_contact.get(user_key):
                        continue  # 已留资：一律不二次触达
                    last_reply = self._last_reply_time.get(user_key, 0)
                    if now - last_reply < delay:
                        continue
                    if self._last_incoming_time.get(user_key, 0) > last_reply:
                        continue
                    self._send_follow_up(user_key)
            except Exception as exc:
                logger.exception('来客二次触达检查失败: %s', exc)
            self._save_followup_state()
            if self._monitor_stop.wait(timeout=10):
                break

    def _send_follow_up(self, user_key: str) -> None:
        now = time.time()
        if self._has_left_contact.get(user_key) or user_key in self._follow_up_sent:
            return
        contact = self._user_contact_info.get(user_key) or {}
        nickname = contact.get('nickname') or user_key.replace('laike:', '')
        if not nickname:
            return
        self._follow_up_sent[user_key] = now
        self._save_followup_state()  # 先落盘，避免重启后重复触达
        self.add_log(f'⏰ [{nickname}] 客户未回复，准备二次触达', 'info')
        follow_text = ''
        try:
            transcript, _msgs = self._recent_transcript(nickname, with_msgs=True)
        except Exception as exc:
            transcript, _msgs = '', []
            logger.warning('读取最近对话失败: %s', exc)
        if self._contact_left_in_msgs(_msgs):
            self._has_left_contact[user_key] = True
            self._save_followup_state()
            self.add_log(f'📇 [{nickname}] 客户已留联系方式，取消二次触达和一键留资', 'info')
            return
        for attempt in (1, 2):
            try:
                follow_text = self._gen_follow_up(user_key, transcript)
            except Exception as exc:
                follow_text = ''
                self.add_log(f'⚠️ [{nickname}] 二次触达 AI 生成失败（第{attempt}次）：{exc}', 'warning')
                continue
            if follow_text:
                break
            self.add_log(f'⚠️ [{nickname}] 二次触达 AI 返回为空（第{attempt}次）', 'warning')
        if not follow_text:
            from ai_reply_service import LAIKE_FOLLOW_UP_FALLBACK
            follow_text = ps.get('no_question_fallback') or LAIKE_FOLLOW_UP_FALLBACK
            self.add_log(f'⚠️ [{nickname}] 二次触达改用兜底话术', 'warning')
        ok = False
        for attempt in (1, 2):
            try:
                with self._browser_use_lock:
                    browser = self._ensure_browser()
                    ok = browser.send_text(nickname, follow_text)
                err = '' if ok else '发送后未在聊天中确认到消息'
            except Exception as exc:
                ok, err = False, str(exc)
            if ok:
                break
            self.add_log(f'⚠️ [{nickname}] 二次触达发送失败（第{attempt}次）：{err}', 'warning')
            time.sleep(2)
        if not ok:
            self.add_log(f'❌ [{nickname}] 二次触达发送失败，已放弃（不再重试）', 'error')
            return
        self._sent_reply_texts.setdefault(user_key, set()).add(follow_text.strip())
        self._last_reply_time[user_key] = time.time()
        record_dashboard_event(self.AI_PLATFORM_KEY, 'reply_success', f'{user_key}|followup|{int(now)}',
                               contact_id=user_key, reply_mode='follow_up')
        self.add_log(f'✅ [{nickname}] 来客二次触达已发送：{follow_text[:60]}', 'success')
        self._maybe_send_lead_card(user_key, nickname)

    def _recent_transcript(self, nickname: str, limit: int = 12, with_msgs: bool = False):
        """读取会话最近几轮（含平台自动消息）作为二次触达上下文。"""
        with self._browser_use_lock:
            b = self._ensure_browser()
            b.open_conversation(nickname)
            msgs = b.read_messages(limit=limit)
        self._open_nick = nickname
        tr = self.format_transcript(msgs)
        return (tr, msgs) if with_msgs else tr

    @staticmethod
    def format_transcript(msgs) -> str:
        lines = []
        for m in msgs or []:
            t = (getattr(m, 'text', '') or '').strip().replace('\n', ' ')
            if not t:
                continue
            who = '平台自动回复' if getattr(m, 'auto', False) else ('我们' if m.is_self else '客户')
            lines.append(f'{who}：{t[:200]}')
        return '\n'.join(lines[-12:])

    def _gen_follow_up(self, user_key: str, transcript: str) -> str:
        ai = _get_ai()
        from ai_reply_service import get_platform_settings
        ps = get_platform_settings(self.AI_PLATFORM_KEY)
        instruction = (ps.get('persona') or '').strip() + '\n\n' + (ps.get('reply_rules') or '')
        instruction += '\n\n【本条任务：二次触达】' + (ps.get('follow_up_prompt') or '')
        if transcript:
            instruction += '\n\n【最近对话（按时间顺序，最后一条是我们的回复）】\n' + transcript
        else:
            try:
                ctx = ai['build_ctx'](self.AI_PLATFORM_KEY, user_key)
            except Exception:
                ctx = ''
            if ctx:
                instruction += '\n\n' + ctx
        decision = ai['gen']('【系统】客户约两分钟没回，请按要求写一句二次触达消息，只输出这句话本身',
                             instruction, platform=self.AI_PLATFORM_KEY)
        text = (decision.get('reply') or '').strip()
        if text and self._is_question(text):
            text = self._fix_question(user_key.replace('laike:', ''), text, lambda: (ai['gen'](
                '【系统】请按要求写一句二次触达消息，只能用陈述句，绝对不能有问号或吗/呢等问句', instruction,
                platform=self.AI_PLATFORM_KEY).get('reply') or '').strip())
        return text

    _Q_TAIL_RE = re.compile(r'(吗|呢|不|么|没|呀吗|是不是|哪方面|方便吗|可以吗|好不好|行不行)[～~。！!…\s哈呀哦嘛]*$')

    @classmethod
    def _is_question(cls, text: str) -> bool:
        t = (text or '').strip()
        if '?' in t or '？' in t:
            return True
        return any(cls._Q_TAIL_RE.search(seg.strip()) for seg in re.split(r'[。！!\n]', t) if seg.strip())

    def _fix_question(self, nickname: str, text: str, regen) -> str:
        """回复含提问：重新生成一次；仍有提问则改用固定陈述句兜底。"""
        try:
            again = regen()
        except Exception:
            again = ''
        if again and not self._is_question(again):
            self.add_log(f'✏️ [{nickname}] 回复含提问，已重新生成为陈述句', 'info')
            return again
        from ai_reply_service import LAIKE_NO_QUESTION_FALLBACK, get_platform_settings
        self.add_log(f'✏️ [{nickname}] 回复含提问，已改用固定陈述句', 'info')
        try:
            return get_platform_settings(self.AI_PLATFORM_KEY).get('no_question_fallback') or LAIKE_NO_QUESTION_FALLBACK
        except Exception:
            return LAIKE_NO_QUESTION_FALLBACK

    # ── 一键留资（随二次触达发送，每个客户仅一次，持久化）──

    def _lead_card_file(self) -> str:
        return os.path.join(os.path.dirname(os.path.abspath(__file__)), 'laike_leadcard_sent.json')

    def _lead_card_sent(self) -> Dict[str, Any]:
        try:
            with open(self._lead_card_file(), 'r', encoding='utf-8') as f:
                d = json.load(f)
                return d if isinstance(d, dict) else {}
        except Exception:
            return {}

    def _maybe_send_lead_card(self, user_key: str, nickname: str) -> None:
        from ai_reply_service import get_platform_settings
        if not get_platform_settings(self.AI_PLATFORM_KEY).get('lead_card_on_follow_up', True):
            return
        if self._is_blocked(user_key, nickname) or self._has_left_contact.get(user_key):
            return
        gid = str(self.config.get('laike_groupid') or '')
        key = f'{gid}|{user_key}'
        sent = self._lead_card_sent()
        if key in sent:
            return
        try:
            with self._browser_use_lock:
                browser = self._ensure_browser()
                r = browser.login_call('send_lead_card', timeout=60, nickname=nickname)
        except Exception as exc:
            r = {'ok': False, 'error': str(exc)}
        if r.get('ok'):
            sent[key] = {'time': time.strftime('%Y-%m-%d %H:%M:%S'), 'flow': r.get('flow', '')}
            try:
                tmp = self._lead_card_file() + '.tmp'
                with open(tmp, 'w', encoding='utf-8') as f:
                    json.dump(sent, f, ensure_ascii=False, indent=2)
                os.replace(tmp, self._lead_card_file())
            except Exception as exc:
                logger.warning('保存一键留资记录失败: %s', exc)
            if r.get('limit'):
                self.add_log(f'📋 [{nickname}] 一键留资已达今日上限，跳过', 'warning')
            else:
                self.add_log(f'📋 [{nickname}] 已发送一键留资（{r.get("flow", "")}）', 'success')
        else:
            self.add_log(f'⚠️ [{nickname}] 一键留资发送失败：{r.get("error", "未知原因")}', 'error')


def register_laike_routes(app) -> None:
    """注册抖音来客路由。"""
    from app_paths import get_static_root

    def _auto_resume():
        import time as _t
        _t.sleep(8)
        try:
            s = get_laike_system()
            if not getattr(s, '_monitoring_wanted', False) or s.monitoring:
                return
            ok = getattr(s, '_laike_logged_in', False)
            if not ok:
                with s._browser_use_lock:
                    b = s._ensure_browser()
                    b.start_worker()
                    ok = bool(b.check_session())
                if ok:
                    s._laike_logged_in = True
                    s._persist_state()
            if ok and not s.monitoring:
                s.add_log('服务重启：重启前监控处于运行状态，已自动恢复监控', 'info')
                s.start_monitoring()
            elif not ok:
                s.add_log('服务重启：来客未登录，未自动恢复监控（登录后请点击开始监控）', 'warning')
        except Exception as exc:  # noqa: BLE001
            print('laike auto-resume failed:', exc)

    threading.Thread(target=_auto_resume, daemon=True, name='laike-auto-resume').start()

    @app.route('/laike')
    def laike_page():
        return send_from_directory(get_static_root(), 'laike_reply.html')

    @app.route('/api/laike-screenshot')
    def laike_screenshot():
        """返回最近一次监控截图（监控循环在 0 会话时自动保存）。"""
        import os
        shot_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'laike_shot.png')
        if not os.path.exists(shot_path):
            return jsonify({'ok': False, 'error': '暂无截图，请先开始监控并等待一次扫描'})
        from flask import send_file
        return send_file(shot_path, mimetype='image/png')

    @app.route('/api/laike-status')
    def laike_status():
        s = get_laike_system()
        return jsonify({
            'monitoring': s.monitoring,
            'log_count': len(s.logs),
            'logged_in': bool(getattr(s, '_laike_logged_in', False)),
            'store_name': s.config.get('laike_account_name', ''),
            'user_name': s.config.get('laike_user_name', ''),
            'groupid': s.config.get('laike_groupid', ''),
            'biz_view_id': s.config.get('laike_biz_view_id', ''),
            'stores': s.config.get('laike_stores', []),
        })

    @app.route('/api/laike-start', methods=['POST'])
    def laike_start():
        s = get_laike_system()
        data = request.get_json(silent=True) or {}
        # 允许前端传入 groupid/biz_view_id
        if data.get('groupid'):
            s.config['laike_groupid'] = data['groupid']
        if data.get('biz_view_id'):
            s.config['laike_biz_view_id'] = data['biz_view_id']
        result = s.start_monitoring()
        s._monitoring_wanted = True
        s._persist_state()
        return jsonify(result)

    @app.route('/api/laike-stop', methods=['POST'])
    def laike_stop():
        s = get_laike_system()
        s._monitoring_wanted = False
        s._monitoring_since = 0
        s._persist_state()
        return jsonify(s.stop_monitoring())

    @app.route('/api/laike-logs')
    def laike_logs():
        s = get_laike_system()
        return jsonify({'logs': s.logs[-100:], 'status': s.live_status()})

    @app.route('/api/laike-config', methods=['GET', 'POST'])
    def laike_config():
        s = get_laike_system()
        if request.method == 'GET':
            return jsonify({
                'groupid': s.config.get('laike_groupid', ''),
                'biz_view_id': s.config.get('laike_biz_view_id', ''),
                'laike_account_name': s.config.get('laike_account_name', ''),
                'headless': s.config.get('headless', True),
                'message_check_interval': s.config.get('message_check_interval', 3.0),
            })
        data = request.get_json(silent=True) or {}
        if 'groupid' in data:
            s.config['laike_groupid'] = str(data['groupid'])
        if 'biz_view_id' in data:
            s.config['laike_biz_view_id'] = str(data['biz_view_id'])
        if 'headless' in data:
            s.config['headless'] = bool(data['headless'])
        if 'message_check_interval' in data:
            try:
                s.config['message_check_interval'] = max(1.0, float(data['message_check_interval']))
            except (ValueError, TypeError):
                pass
        s._persist_state()
        return jsonify({'success': True})

    # ── 无头登录（短信验证码 / 密码 / Cookie 导入）──
    # 说明：life.douyin.com 的 passport 对扫码登录返回 error_code=1069「不能用该app扫码」，
    # 页面也只提供 手机验证码/密码/邮箱 登录，因此这里在无头浏览器里驱动官方登录表单，
    # 把截图回传前端；遇到滑块/点选验证码时，前端可在截图上点击/拖动，坐标转发到浏览器。

    def _login_browser():
        if s_monitoring():
            raise RuntimeError('监控运行中，请先停止监控再登录')
        b = get_laike_system()._ensure_browser()
        b.start_worker()
        return b

    def s_monitoring():
        return bool(get_laike_system().monitoring)

    def _save_shot(res):
        import base64
        b64 = (res or {}).get('screenshot') or ''
        if b64:
            try:
                with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), 'laike_qr.png'), 'wb') as f:
                    f.write(base64.b64decode(b64))
            except Exception:
                pass

    def _after_login(res):
        """登录成功：标记状态、解析 groupid、持久化。"""
        s = get_laike_system()
        if not (res or {}).get('logged_in'):
            return res
        was_logged = getattr(s, '_laike_logged_in', False)
        if not was_logged:
            s.add_log('✅ 抖音来客登录成功，会话已保存到浏览器 profile', 'success')
            s._account_detected = False  # 新登录（可能是新账号）：必须重新识别
        s._laike_logged_in = True
        if not s._account_detected:
            try:
                info = s._browser.login_call('detect_account', timeout=90, navigate=True, with_stores=True)
                s._account_detected = True
                s.apply_account_info(info, '登录后识别')
                res.update({'groupid': info.get('groupid', ''), 'biz_view_id': info.get('biz_view_id', ''),
                            'store_name': info.get('store_name', ''), 'user_name': info.get('user_name', ''),
                            'stores': info.get('stores', [])})
            except Exception as exc:
                s.add_log(f'自动识别来客账号失败（可手动填写）: {exc}', 'warning')
        s._persist_state()
        return res

    def _login_route(op, timeout=60, **kw):
        s = get_laike_system()
        if not s._login_lock.acquire(timeout=1 if op == 'login_snapshot' else 25):
            if op == 'login_snapshot':
                return jsonify({'success': True, 'busy': True})
            return jsonify({'success': False, 'error': '另一个登录操作正在进行，请稍候'})
        try:
            with s._browser_use_lock:
                b = _login_browser()
                res = b.login_call(op, timeout=timeout, **kw)
                _save_shot(res)
                res = _after_login(res)
            res['success'] = True
            return jsonify(res)
        except Exception as exc:
            return jsonify({'success': False, 'error': str(exc)})
        finally:
            s._login_lock.release()

    @app.route('/api/laike-login/open', methods=['POST'])
    def laike_login_open():
        return _login_route('login_open', timeout=60)

    @app.route('/api/laike-login/send-code', methods=['POST'])
    def laike_login_send_code():
        data = request.get_json(silent=True) or {}
        return _login_route('login_send_code', timeout=60, phone=str(data.get('phone', '')))

    @app.route('/api/laike-login/submit-code', methods=['POST'])
    def laike_login_submit_code():
        data = request.get_json(silent=True) or {}
        return _login_route('login_submit_code', timeout=60, code=str(data.get('code', '')))

    @app.route('/api/laike-login/password', methods=['POST'])
    def laike_login_password():
        data = request.get_json(silent=True) or {}
        return _login_route('login_password', timeout=60,
                            account=str(data.get('account', '')), password=str(data.get('password', '')))

    @app.route('/api/laike-login/mouse', methods=['POST'])
    def laike_login_mouse():
        data = request.get_json(silent=True) or {}
        try:
            kw = {'action': 'drag' if data.get('action') == 'drag' else 'click',
                  'x': float(data.get('x', 0)), 'y': float(data.get('y', 0)),
                  'x2': float(data.get('x2', 0)), 'y2': float(data.get('y2', 0))}
        except (TypeError, ValueError):
            return jsonify({'success': False, 'error': '坐标无效'})
        return _login_route('login_mouse', timeout=45, **kw)

    @app.route('/api/laike-login/snapshot')
    def laike_login_snapshot():
        return _login_route('login_snapshot', timeout=30)

    @app.route('/api/laike-login/import-cookies', methods=['POST'])
    def laike_login_import_cookies():
        data = request.get_json(silent=True) or {}
        raw = data.get('cookies')
        cookies = []
        if isinstance(raw, list):
            cookies = raw
        elif isinstance(raw, str) and raw.strip():
            txt = raw.strip()
            try:
                parsed = json.loads(txt)
                cookies = parsed if isinstance(parsed, list) else parsed.get('cookies', [])
            except Exception:
                # "k=v; k2=v2" 格式
                for part in txt.split(';'):
                    if '=' in part:
                        k, v = part.split('=', 1)
                        cookies.append({'name': k.strip(), 'value': v.strip(), 'domain': '.douyin.com'})
        if not cookies:
            return jsonify({'success': False, 'error': '未解析到 Cookie'})
        return _login_route('import_cookies', timeout=60, cookies=cookies)

    # 兼容旧接口
    @app.route('/api/laike-qr-login', methods=['POST'])
    def laike_qr_login():
        return _login_route('login_open', timeout=60)

    @app.route('/api/laike-qr-image')
    def laike_qr_image():
        from flask import send_file
        shot_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'laike_qr.png')
        if not os.path.exists(shot_path):
            return jsonify({'success': False, 'error': '尚未生成登录截图'})
        return send_file(shot_path, mimetype='image/png')

    @app.route('/api/laike-qr-check')
    def laike_qr_check():
        return _login_route('login_status', timeout=30)

    @app.route('/api/laike-login-window', methods=['POST'])
    def laike_login_window():
        return _login_route('login_open', timeout=60)

    @app.route('/api/laike-list-accounts', methods=['POST'])
    def laike_list_accounts():
        """读取当前登录账号及其门店列表（复用识别逻辑；监控中不跳转页面）。"""
        s = get_laike_system()
        if not getattr(s, '_laike_logged_in', False):
            return jsonify({'accounts': [], 'error': '尚未登录抖音来客，请先在「账号配置」中登录'})
        if not s._browser_use_lock.acquire(timeout=20):
            return jsonify({'accounts': [], 'error': '浏览器正忙（登录或识别操作进行中），请稍后再试'})
        try:
            b = s._ensure_browser()
            b.start_worker()
            info = b.login_call('detect_account', timeout=60, navigate=not s.monitoring, with_stores=True)
        except Exception as exc:
            logger.warning('[laike] 读取账号列表失败: %s', exc)
            return jsonify({'accounts': [], 'error': f'读取失败：{exc}'})
        finally:
            s._browser_use_lock.release()
        if not info.get('groupid'):
            return jsonify({'accounts': [], 'error': '未能从来客页面识别账号（页面可能未加载完成或登录已失效），请点击「刷新账号」或重新登录'})
        s.apply_account_info(info, '读取账号列表')
        names = info.get('stores') or ([info['store_name']] if info.get('store_name') else [])
        accounts = [{'name': n, 'groupid': info['groupid'], 'biz_view_id': info.get('biz_view_id', ''),
                     'current': n == info.get('store_name')} for n in names] or \
                   [{'name': info.get('store_name') or f"账号_{info['groupid'][-6:]}", 'groupid': info['groupid'],
                     'biz_view_id': info.get('biz_view_id', ''), 'current': True}]
        return jsonify({'accounts': accounts, 'current': info})

    @app.route('/api/laike-account-info')
    def laike_account_info():
        """获取当前浏览器中的商家账号信息。"""
        s = get_laike_system()
        try:
            browser = s._ensure_browser()
            try:
                browser.start_worker()
            except Exception:
                pass
            info = browser.get_account_info()
            # 附带当前配置的账号名（如果已保存）
            info['configured_name'] = s.config.get('laike_account_name', '')
            info['configured_groupid'] = s.config.get('laike_groupid', '')
            return jsonify(info)
        except Exception as exc:
            return jsonify({'error': str(exc)})

    @app.route('/api/laike-sync-account', methods=['POST'])
    def laike_sync_account():
        """将浏览器当前账号同步为监控目标。
        简化版：用户手动在输入框填入 groupid，系统只做保存和显示。
        如需自动检测，需先通过「打开登录窗口」打开浏览器并进入会话页。
        """
        s = get_laike_system()
        try:
            data = request.get_json(silent=True) or {}
            # 优先用前端传来的 groupid（用户手动输入）
            groupid = (data.get('groupid') or '').strip()
            account_name = (data.get('account_name') or '').strip()
            if groupid:
                s.config['laike_groupid'] = groupid
                if account_name:
                    s.config['laike_account_name'] = account_name
                if data.get('biz_view_id'):
                    s.config['laike_biz_view_id'] = str(data['biz_view_id'])
                s._persist_state()
                s.add_log(f'已设置监控账号: {account_name or groupid}', 'success')
                return jsonify({'success': True, 'account_name': account_name, 'groupid': groupid})
            # 尝试从运行中的浏览器自动检测
            browser = getattr(s, '_sync_browser', None) or getattr(s, '_login_browser', None)
            if browser is not None:
                try:
                    info = browser.get_account_info()
                    groupid = info.get('groupid', '')
                    if groupid:
                        s.config['laike_groupid'] = groupid
                        if info.get('account_name'):
                            s.config['laike_account_name'] = info['account_name']
                        if info.get('biz_view_id'):
                            s.config['laike_biz_view_id'] = info['biz_view_id']
                        s._persist_state()
                        return jsonify({'success': True, 'account_name': info.get('account_name', ''), 'groupid': groupid})
                except Exception:
                    pass
            return jsonify({
                'success': False,
                'error': '请在下方 Group ID 输入框中手动填入目标账号的 groupid（从浏览器地址栏复制）',
            })
        except Exception as exc:
            return jsonify({'success': False, 'error': str(exc)})

    @app.route('/api/laike-debug')
    def laike_debug():
        s = get_laike_system()
        try:
            with s._browser_use_lock:
                b = s._ensure_browser()
                b.start_worker()
                return jsonify(b.login_call('debug_info', timeout=30))
        except Exception as exc:
            return jsonify({'error': str(exc)})

    @app.route('/api/laike-toolbar-probe')
    def laike_toolbar_probe():
        """只读：当前打开会话输入框上方工具栏（含「一键留资」）位置与文字，不点击。"""
        s = get_laike_system()
        try:
            with s._browser_use_lock:
                b = s._ensure_browser()
                b.start_worker()
                return jsonify(b.login_call('toolbar_probe', timeout=30))
        except Exception as exc:
            return jsonify({'error': str(exc)})

    @app.route('/api/laike-takeover-probe')
    def laike_takeover_probe():
        """只读：检查当前打开会话是否处于智能客服接待（不点击）。"""
        s = get_laike_system()
        try:
            with s._browser_use_lock:
                b = s._ensure_browser()
                b.start_worker()
                return jsonify(b.login_call('takeover_probe', timeout=30))
        except Exception as exc:
            return jsonify({'error': str(exc)})

    @app.route('/api/laike-page-exec', methods=['POST'])
    def laike_page_exec():
        """调试（仅网关内部）：跳转/执行 JS/截图。"""
        s = get_laike_system()
        data = request.get_json(silent=True) or {}
        try:
            with s._browser_use_lock:
                b = s._ensure_browser()
                b.start_worker()
                return jsonify(b.login_call('page_exec', timeout=90, **{k: data.get(k) for k in ('url', 'js', 'shot', 'wait') if data.get(k) is not None}))
        except Exception as exc:
            return jsonify({'error': str(exc)})

    @app.route('/api/laike-peek')
    def laike_peek():
        """只读调试：列会话、打开第一个、读消息、检查输入框/发送按钮是否存在（不发送）。"""
        s = get_laike_system()
        try:
            with s._browser_use_lock:
                b = s._ensure_browser()
                b.start_worker()
                convs = b.list_conversations()
                if not convs:
                    b.navigate_chat()
                    convs = b.list_conversations()
                out = {'conversations': [c.__dict__ for c in convs]}
                if convs:
                    out['opened'] = b.open_conversation(convs[0].nickname)
                    out['messages'] = [m.__dict__ for m in b.read_messages(limit=8)]
                out['editor'] = b.login_call('probe_editor', timeout=20)
            return jsonify(out)
        except Exception as exc:
            return jsonify({'error': str(exc)})

    @app.route('/api/laike-switch-account', methods=['POST'])
    def laike_switch_account():
        s = get_laike_system()
        data = request.get_json(silent=True) or {}
        dry = bool(data.get('dry_run')) or request.args.get('dry_run') == '1'
        if not s._login_lock.acquire(timeout=5):
            return jsonify({'success': False, 'error': '登录操作进行中，请稍候'})
        try:
            return jsonify(s.switch_account(dry_run=dry))
        except Exception as exc:
            return jsonify({'success': False, 'error': str(exc)})
        finally:
            s._login_lock.release()

    @app.route('/api/laike-detect-account', methods=['POST'])
    def laike_detect_account():
        """识别当前登录账号与门店列表。监控中只读取当前页面（不跳转）。"""
        s = get_laike_system()
        data = request.get_json(silent=True) or {}
        try:
            with s._browser_use_lock:
                b = s._ensure_browser()
                b.start_worker()
                nav = not s.monitoring
                stores = bool(data.get('with_stores', not s.monitoring))
                info = b.login_call('detect_account', timeout=90, navigate=nav, with_stores=stores)
            if data.get('apply', True):
                s.apply_account_info(info, '手动识别')
            info['success'] = True
            return jsonify(info)
        except Exception as exc:
            return jsonify({'success': False, 'error': str(exc)})

    @app.route('/api/laike-select-store', methods=['POST'])
    def laike_select_store():
        s = get_laike_system()
        name = ((request.get_json(silent=True) or {}).get('name') or '').strip()
        if not name:
            return jsonify({'success': False, 'error': '缺少门店名'})
        if s.monitoring:
            return jsonify({'success': False, 'error': '请先停止监控再切换门店'})
        try:
            with s._browser_use_lock:
                b = s._ensure_browser()
                b.start_worker()
                info = b.login_call('select_store', timeout=90, name=name)
            s.apply_account_info(info, '切换门店')
            info['success'] = True
            return jsonify(info)
        except Exception as exc:
            return jsonify({'success': False, 'error': str(exc)})

    @app.route('/api/laike-screenshot-now')
    def laike_screenshot_now():
        """监控中也可用：对 worker 当前页面截图（只读）。"""
        from flask import send_file
        s = get_laike_system()
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'laike_now.png')
        try:
            with s._browser_use_lock:
                b = s._ensure_browser()
                b.start_worker()
                b.take_screenshot(path=path, timeout=30)
            return send_file(path, mimetype='image/png')
        except Exception as exc:
            return jsonify({'error': str(exc)})

    @app.route('/api/laike-login-status')
    def laike_login_status():
        """检查来客登录状态（复用同一个无头浏览器，不另开实例以免 profile 冲突）。"""
        s = get_laike_system()
        if s.monitoring:
            return jsonify({'logged_in': bool(getattr(s, '_laike_logged_in', False)), 'monitoring': True})
        try:
            with s._browser_use_lock:
                b = s._ensure_browser()
                b.start_worker()
                ok = b.check_session()
            if bool(ok) != bool(getattr(s, '_laike_logged_in', False)):
                s._laike_logged_in = bool(ok)
                s._persist_state()
            return jsonify({'logged_in': bool(ok)})
        except Exception as exc:
            return jsonify({'logged_in': bool(getattr(s, '_laike_logged_in', False)), 'error': str(exc)})
