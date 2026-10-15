"""抖音来客多账号：每个商家账号一套独立的浏览器 profile 与会话数据。

路径全部用 os.path.join / abspath，Windows 与 Mac/Linux 同一套逻辑。
网关自动拉起后端看的是数据根目录里的 douyin_laike_state.json 的
monitoring_enabled（任一账号需要监控即为 true）。各账号自己的开关写在
account_monitoring_enabled，避免被「或」结果污染。
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from typing import Any, Callable, Dict, List, Optional

DEFAULT_ACCOUNT_ID = 'default'
DEFAULT_ACCOUNT_NAME = '默认账号'
ACCOUNTS_DIRNAME = 'douyin_laike_accounts'
REGISTRY_NAME = 'douyin_laike_accounts.json'
STATE_NAME = 'douyin_laike_state.json'
def _profile_dirname() -> str:
    from douyin_laike_playwright import DouyinLaikeWorker
    return DouyinLaikeWorker.PROFILE_DIR_NAME
DEFAULT_MAX_BROWSERS = 3
MIN_BROWSERS = 1
MAX_BROWSERS = 8
GENERIC_NAMES = {'', DEFAULT_ACCOUNT_NAME, '未命名', '未命名账号'}

_manager: Optional['LaikeAccountManager'] = None
_manager_lock = threading.Lock()


class LaikeAccountError(Exception):
    """账号不存在或当前没有可操作的来客账号。"""


def data_root() -> str:
    """与现有来客模块相同的数据根：用户后端目录（代码若是符号链接，不要 realpath）。"""
    import douyin_laike_reply_system as mod
    return os.path.dirname(os.path.abspath(mod.__file__))


def cmdline_has_profile(cmdline: Any, profile_dir: str) -> bool:
    """进程命令行是否包含某个浏览器 profile 目录（Windows 反斜杠与 Unix 斜杠都认）。"""
    if isinstance(cmdline, (bytes, bytearray)):
        cmdline = cmdline.decode('utf-8', 'replace')
    blob = os.path.normcase(str(cmdline or '').replace('\x00', ' ').replace('\\', '/'))
    needle = os.path.normcase(str(profile_dir or '').replace('\\', '/').rstrip('/'))
    return bool(needle) and needle in blob


def _read_json(path: str) -> Dict[str, Any]:
    try:
        with open(path, 'r', encoding='utf-8') as handle:
            data = json.load(handle)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError, TypeError):
        return {}


def _write_json(path: str, data: Dict[str, Any]) -> None:
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    tmp = path + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as handle:
        json.dump(data, handle, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def clean_account_name(name: str) -> str:
    text = re.sub(r'[\r\n\t]+', ' ', str(name or '').strip())
    text = text.replace('/', ' ').replace('\\', ' ')
    text = re.sub(r'[<>:"|?*]', '', text).strip(' .')
    text = re.sub(r'\s+', ' ', text)
    return text[:40]


def valid_account_id(account_id: str) -> bool:
    return bool(re.fullmatch(r'default|acc_[0-9a-f]{8}', str(account_id or '')))


def host_ram_mb() -> int:
    """物理内存（MB）。读不到时返回 0，调用方只做提示、不据此拒绝启动。"""
    try:
        if sys.platform.startswith('linux'):
            with open('/proc/meminfo', encoding='utf-8') as handle:
                for line in handle:
                    if line.startswith('MemTotal:'):
                        return int(line.split()[1]) // 1024
        elif sys.platform == 'darwin':
            out = subprocess.check_output(['sysctl', '-n', 'hw.memsize'], text=True, timeout=3)
            return int(out.strip()) // (1024 * 1024)
        elif sys.platform == 'win32':
            import ctypes
            from ctypes import wintypes

            class MEMORYSTATUSEX(ctypes.Structure):
                _fields_ = [
                    ('dwLength', wintypes.DWORD),
                    ('dwMemoryLoad', wintypes.DWORD),
                    ('ullTotalPhys', ctypes.c_uint64),
                    ('ullAvailPhys', ctypes.c_uint64),
                    ('ullTotalPageFile', ctypes.c_uint64),
                    ('ullAvailPageFile', ctypes.c_uint64),
                    ('ullTotalVirtual', ctypes.c_uint64),
                    ('ullAvailVirtual', ctypes.c_uint64),
                    ('ullAvailExtendedVirtual', ctypes.c_uint64),
                ]

            status = MEMORYSTATUSEX()
            status.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
            if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
                return int(status.ullTotalPhys) // (1024 * 1024)
    except Exception:
        return 0
    return 0


def memory_warning(ram_mb: int, max_browsers: int, account_count: int) -> str:
    if account_count <= 1 and (not ram_mb or ram_mb >= 3500):
        return ''
    parts: List[str] = []
    if ram_mb and ram_mb < 3500:
        parts.append(
            f'这台电脑大约只有 {ram_mb / 1024:.1f} GB 内存。每个来客浏览器大约占用数百 MB，'
            '同时打开多个容易内存不足甚至被系统杀掉。低内存云主机请只监控 1 个账号；'
            '多账号请放在内存充足、常开的 Windows 或 Mac 上。'
        )
    elif account_count > 1:
        parts.append(
            f'每个来客账号会单独打开一个 Chromium（大约数百 MB）。'
            f'当前上限是同时 {max_browsers} 个浏览器。内存吃紧时请调低上限，或先停止不用的账号。'
        )
    if account_count > 1:
        parts.append('不要在两台电脑上同时登录或监控同一个来客账号，否则会互相顶掉登录并重复回复。')
    return ''.join(parts)


_rss_cache: Dict[str, Any] = {}
_rss_lock = threading.Lock()


def chrome_profile_rss_mb(profile_dir: str) -> int:
    """某个 profile 目录对应的 Chromium 进程 RSS（MB）。失败返回 0，20 秒内不重复扫描。"""
    key = os.path.normcase(os.path.abspath(profile_dir))
    now = time.time()
    with _rss_lock:
        hit = _rss_cache.get(key)
        if hit and now - hit[0] < 20:
            return int(hit[1])
    try:
        value = _scan_profile_rss(key)
    except Exception:
        value = 0
    with _rss_lock:
        _rss_cache[key] = (now, int(value))
    return int(value)


def _scan_profile_rss(profile_dir: str) -> int:
    if sys.platform.startswith('linux') and os.path.isdir('/proc'):
        total = 0
        page = os.sysconf('SC_PAGE_SIZE')
        for pid in os.listdir('/proc'):
            if not pid.isdigit():
                continue
            try:
                with open(f'/proc/{pid}/cmdline', 'rb') as handle:
                    raw = handle.read()
                if not cmdline_has_profile(raw, profile_dir):
                    continue
                with open(f'/proc/{pid}/statm', encoding='utf-8') as handle:
                    total += int(handle.read().split()[1]) * page
            except (OSError, ValueError, IndexError):
                continue
        return total // (1024 * 1024)
    if sys.platform == 'darwin':
        out = subprocess.check_output(['ps', '-axww', '-o', 'rss=,command='], text=True, timeout=5)
        total_kb = 0
        for line in out.splitlines():
            parts = line.strip().split(None, 1)
            if len(parts) < 2:
                continue
            if cmdline_has_profile(parts[1], profile_dir):
                total_kb += int(parts[0])
        return total_kb // 1024
    if sys.platform == 'win32':
        script = (
            "Get-CimInstance Win32_Process | "
            "Where-Object { $_.Name -match 'chrome|chromium' -and $_.CommandLine } | "
            "ForEach-Object { '{0}`t{1}' -f $_.WorkingSetSize, $_.CommandLine }"
        )
        out = subprocess.check_output(
            ['powershell', '-NoProfile', '-NonInteractive', '-Command', script],
            text=True, timeout=12, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0),
        )
        total = 0
        for line in out.splitlines():
            if '\t' not in line:
                continue
            size, cmd = line.split('\t', 1)
            if cmdline_has_profile(cmd, profile_dir):
                total += int(float(size))
        return total // (1024 * 1024)
    return 0


class LaikeAccountManager:
    def __init__(self, root: Optional[str] = None):
        self.root = os.path.abspath(root or data_root())
        self._lock = threading.RLock()
        self._records: List[Dict[str, Any]] = []
        self._active_id = DEFAULT_ACCOUNT_ID
        self._max_browsers = DEFAULT_MAX_BROWSERS
        self._systems: Dict[str, Any] = {}
        self._reservations: set = set()
        self._loaded = False

    # ── 路径 ──

    def registry_path(self) -> str:
        return os.path.join(self.root, REGISTRY_NAME)

    def legacy_state_path(self) -> str:
        return os.path.join(self.root, STATE_NAME)

    def account_data_dir(self, record: Dict[str, Any]) -> str:
        if record.get('legacy'):
            return self.root
        return os.path.join(self.root, ACCOUNTS_DIRNAME, str(record['id']))

    def profile_dir_for(self, record: Dict[str, Any]) -> str:
        return os.path.join(self.account_data_dir(record), _profile_dirname())

    # ── 注册表 ──

    def load(self) -> None:
        with self._lock:
            if self._loaded:
                return
            path = self.registry_path()
            if not os.path.exists(path):
                self._migrate_locked()
            else:
                raw = _read_json(path)
                self._records = [r for r in (raw.get('accounts') or []) if isinstance(r, dict) and valid_account_id(str(r.get('id') or ''))]
                self._active_id = str(raw.get('active_id') or '')
                self._max_browsers = self._clamp_max(raw.get('max_concurrent_browsers', DEFAULT_MAX_BROWSERS))
                if self._records and self._active_id not in {r['id'] for r in self._records}:
                    self._active_id = self._records[0]['id']
            self._stamp_legacy_flag()
            self._loaded = True

    def _migrate_locked(self) -> None:
        """已有的单账号 profile / 状态原位成为第一个账号，不移动浏览器目录。"""
        state_path = self.legacy_state_path()
        state = _read_json(state_path)
        config = state.get('config') if isinstance(state.get('config'), dict) else {}
        name = clean_account_name(str(config.get('laike_account_name') or '')) or DEFAULT_ACCOUNT_NAME
        if state and 'account_monitoring_enabled' not in state:
            state['account_monitoring_enabled'] = bool(state.get('monitoring_enabled', False))
            _write_json(state_path, state)
        self._records = [{
            'id': DEFAULT_ACCOUNT_ID,
            'name': name,
            'legacy': True,
            'created_at': time.strftime('%Y-%m-%d %H:%M:%S'),
        }]
        self._active_id = DEFAULT_ACCOUNT_ID
        self._max_browsers = DEFAULT_MAX_BROWSERS
        self._write_registry_locked()

    def _stamp_legacy_flag(self) -> None:
        """老状态文件补上「本账号自己的监控开关」，便于和网关用的或结果分开。"""
        legacy = next((r for r in self._records if r.get('legacy')), None)
        if not legacy:
            return
        path = self.legacy_state_path()
        state = _read_json(path)
        if not state or 'account_monitoring_enabled' in state:
            return
        state['account_monitoring_enabled'] = bool(state.get('monitoring_enabled', False))
        _write_json(path, state)

    def _write_registry_locked(self) -> None:
        _write_json(self.registry_path(), {
            'version': 1,
            'active_id': self._active_id,
            'max_concurrent_browsers': self._max_browsers,
            'accounts': self._records,
        })

    def _clamp_max(self, value: Any) -> int:
        try:
            number = int(value)
        except (TypeError, ValueError):
            number = DEFAULT_MAX_BROWSERS
        return max(MIN_BROWSERS, min(MAX_BROWSERS, number))

    def set_max_browsers(self, value: Any) -> int:
        self.load()
        with self._lock:
            self._max_browsers = self._clamp_max(value)
            self._write_registry_locked()
            return self._max_browsers

    # ── 系统实例 ──

    def _new_system(self, record: Dict[str, Any]):
        from douyin_laike_reply_system import DouyinLaikeReplySystem
        data_dir = self.account_data_dir(record)
        os.makedirs(data_dir, exist_ok=True)
        system = DouyinLaikeReplySystem(
            account_id=str(record['id']),
            account_name=str(record.get('name') or DEFAULT_ACCOUNT_NAME),
            data_dir=data_dir,
            legacy=bool(record.get('legacy')),
        )
        system._browser_gate = self._gate_for(system.account_id)
        system._browser_release = lambda s=system: self.release(s)
        system._after_persist = lambda s=system: self.sync_gateway_state()
        system._on_store_name = self._adopt_store_name
        return system

    def _gate_for(self, account_id: str) -> Callable[[Any], Optional[str]]:
        def _gate(_system: Any = None) -> Optional[str]:
            return self.reserve_error(account_id)
        return _gate

    def system(self, account_id: str):
        self.load()
        with self._lock:
            record = self._record(account_id)
            if record is None:
                raise LaikeAccountError(f'来客账号不存在: {account_id}')
            cached = self._systems.get(account_id)
            if cached is None:
                cached = self._new_system(record)
                self._systems[account_id] = cached
            return cached

    def _record(self, account_id: str) -> Optional[Dict[str, Any]]:
        for record in self._records:
            if record.get('id') == account_id:
                return record
        return None

    def resolve(self, account_id: str = ''):
        self.load()
        with self._lock:
            if not self._records:
                return None
            aid = (account_id or '').strip() or self._active_id or self._records[0]['id']
        return self.system(aid)

    def active_system(self):
        return self.resolve('')

    def select(self, account_id: str) -> Dict[str, Any]:
        self.system(account_id)  # 校验存在
        with self._lock:
            self._active_id = account_id
            self._write_registry_locked()
        return self.public_account(account_id)

    def create(self, name: str, copy_from: str = ''):
        self.load()
        cleaned = clean_account_name(name)
        if not cleaned:
            raise LaikeAccountError('请填写账号名称')
        from laike_account_ai import AI_FILE, clone_document, empty_document, save_document
        copied_name = ''
        if str(copy_from or '').strip():
            source = self.system(str(copy_from).strip())
            source.ensure_account_ai()
            ai_doc = clone_document(source.account_ai())
            copied_name = source.account_name
        else:
            ai_doc = empty_document()
        with self._lock:
            account_id = 'acc_' + os.urandom(4).hex()
            record = {
                'id': account_id,
                'name': cleaned,
                'legacy': False,
                'created_at': time.strftime('%Y-%m-%d %H:%M:%S'),
            }
            data_dir = self.account_data_dir(record)
            os.makedirs(data_dir, exist_ok=True)
            headless, interval = True, 3.0
            if self._systems:
                sample = next(iter(self._systems.values()))
                headless = bool(sample.config.get('headless', True))
                interval = float(sample.config.get('message_check_interval', 3.0) or 3.0)
            elif self._records:
                try:
                    sample = self._new_system(self._records[0])
                    self._systems[self._records[0]['id']] = sample
                    headless = bool(sample.config.get('headless', True))
                    interval = float(sample.config.get('message_check_interval', 3.0) or 3.0)
                except Exception:
                    pass
            _write_json(os.path.join(data_dir, STATE_NAME), {
                'logged_in': False,
                'monitoring_enabled': False,
                'account_monitoring_enabled': False,
                'monitoring_since': 0,
                'config': {'headless': headless, 'message_check_interval': interval},
            })
            save_document(os.path.join(data_dir, AI_FILE), ai_doc)
            self._records.append(record)
            self._active_id = account_id
            self._write_registry_locked()
        system = self.system(account_id)
        system.add_log(f'已添加来客账号「{cleaned}」，请单独登录（不会影响其他账号）', 'info')
        if copied_name:
            system.add_log(f'已从「{copied_name}」复制 AI、知识库和话术，之后两边互不影响', 'info')
        else:
            system.add_log('这个账号的 AI、知识库和话术从空白开始，不会沿用其他账号或平台共用设置', 'info')
        return system

    def rename(self, account_id: str, name: str) -> Dict[str, Any]:
        cleaned = clean_account_name(name)
        if not cleaned:
            raise LaikeAccountError('请填写账号名称')
        system = self.system(account_id)
        with self._lock:
            record = self._record(account_id)
            if record is None:
                raise LaikeAccountError(f'来客账号不存在: {account_id}')
            record['name'] = cleaned
            self._write_registry_locked()
        system.account_name = cleaned
        system.add_log(f'账号已改名为「{cleaned}」', 'info')
        return self.public_account(account_id)

    def _adopt_store_name(self, system, store_name: str) -> None:
        cleaned = clean_account_name(store_name)
        if not cleaned:
            return
        with self._lock:
            record = self._record(system.account_id)
            if record is None:
                return
            current = str(record.get('name') or '')
            if current not in GENERIC_NAMES and not current.startswith('来客账号'):
                return
            if current == cleaned:
                return
            record['name'] = cleaned
            self._write_registry_locked()
        system.account_name = cleaned

    def remove(self, account_id: str) -> Dict[str, Any]:
        system = self.system(account_id)
        if system.monitoring:
            system.stop_monitoring('账号已删除')
            thread = system.monitor_thread
            if thread is not None:
                thread.join(timeout=20)
        try:
            system._close_browser('账号已删除')
        except Exception:
            pass
        self.release(system)
        with self._lock:
            record = self._record(account_id)
            if record is None:
                raise LaikeAccountError(f'来客账号不存在: {account_id}')
            data_dir = self.account_data_dir(record)
            profile = self.profile_dir_for(record)
            legacy = bool(record.get('legacy'))
            self._records = [r for r in self._records if r.get('id') != account_id]
            self._systems.pop(account_id, None)
            self._reservations.discard(account_id)
            if self._active_id == account_id:
                self._active_id = self._records[0]['id'] if self._records else ''
            self._write_registry_locked()
        self._delete_account_files(data_dir, profile, legacy)
        self.sync_gateway_state()
        return {'success': True, 'active_id': self._active_id}

    def _delete_account_files(self, data_dir: str, profile: str, legacy: bool) -> None:
        if not legacy:
            shutil.rmtree(data_dir, ignore_errors=True)
            return
        shutil.rmtree(profile, ignore_errors=True)
        for name in (
            'douyin_laike_storage.json',
            'laike_followup_state.json',
            'laike_blocked.json',
            'laike_greeted.json',
            'laike_leadcard_sent.json',
            'laike_settings_override.json',
            'laike_account_ai.json',
            'laike_shot.png',
            'laike_qr.png',
            'laike_now.png',
        ):
            try:
                os.remove(os.path.join(data_dir, name))
            except OSError:
                pass
        state = _read_json(self.legacy_state_path())
        config = state.get('config') if isinstance(state.get('config'), dict) else {}
        kept = {k: config.get(k) for k in ('headless', 'message_check_interval') if k in config}
        _write_json(self.legacy_state_path(), {
            'logged_in': False,
            'monitoring_enabled': False,
            'account_monitoring_enabled': False,
            'monitoring_since': 0,
            'config': kept,
        })

    # ── 浏览器名额 ──

    def live_browser_ids(self) -> set:
        ids = set(self._reservations)
        for account_id, system in self._systems.items():
            try:
                if system.monitoring or system._browser_alive():
                    ids.add(account_id)
            except Exception:
                continue
        return ids

    def reserve_error(self, account_id: str) -> Optional[str]:
        """需要新开浏览器时占用名额。同一账号已在运行则直接通过。名额不足时先关掉空闲浏览器。"""
        self.load()
        with self._lock:
            if account_id in self.live_browser_ids():
                return None
            if len(self.live_browser_ids()) >= self._max_browsers:
                self._close_idle_unlocked(except_id=account_id)
            if account_id in self.live_browser_ids():
                return None
            running = len(self.live_browser_ids())
            if running >= self._max_browsers:
                return (
                    f'同时运行的来客浏览器已达上限 {self._max_browsers}（当前 {running} 个）。'
                    '请先停止其他账号的监控，或调高「最多同时打开的浏览器」。'
                    '内存较小的电脑不建议调高，以免内存不足。'
                )
            self._reservations.add(account_id)
            return None

    def _close_idle_unlocked(self, except_id: str = '') -> None:
        for account_id, system in list(self._systems.items()):
            if account_id == except_id or system.monitoring:
                continue
            login_lock = getattr(system, '_login_lock', None)
            if login_lock is not None and login_lock.locked():
                continue
            if system._browser_alive():
                try:
                    system._close_browser('为其他来客账号腾出浏览器名额')
                except Exception:
                    pass
            self._reservations.discard(account_id)

    def release(self, system) -> None:
        with self._lock:
            self._reservations.discard(getattr(system, 'account_id', ''))

    def browsers_running(self) -> int:
        self.load()
        with self._lock:
            return len(self.live_browser_ids())

    # ── 网关状态（任一账号要监控就拉起后端）──

    def sync_gateway_state(self) -> None:
        self.load()
        with self._lock:
            path = self.legacy_state_path()
            state = _read_json(path)
            legacy = next((self._systems.get(r['id']) for r in self._records if r.get('legacy')), None)
            if legacy is not None:
                state['account_monitoring_enabled'] = bool(getattr(legacy, '_monitoring_wanted', False))
                state['logged_in'] = bool(getattr(legacy, '_laike_logged_in', False))
                if isinstance(getattr(legacy, 'config', None), dict):
                    state['config'] = {
                        'laike_groupid': legacy.config.get('laike_groupid', ''),
                        'laike_biz_view_id': legacy.config.get('laike_biz_view_id', ''),
                        'laike_account_name': legacy.config.get('laike_account_name', ''),
                        'laike_user_name': legacy.config.get('laike_user_name', ''),
                        'laike_stores': legacy.config.get('laike_stores', []),
                        'headless': legacy.config.get('headless', True),
                        'message_check_interval': legacy.config.get('message_check_interval', 3.0),
                    }
            wanted = False
            for record in self._records:
                system = self._systems.get(record['id'])
                if system is not None and getattr(system, '_monitoring_wanted', False):
                    wanted = True
                    break
                if system is None:
                    saved = _read_json(os.path.join(self.account_data_dir(record), STATE_NAME))
                    flag = saved.get('account_monitoring_enabled', saved.get('monitoring_enabled', False))
                    if flag:
                        wanted = True
                        break
            state['monitoring_enabled'] = wanted
            if 'account_monitoring_enabled' not in state:
                state['account_monitoring_enabled'] = False
            _write_json(path, state)

    # ── 对外描述 ──

    def public_account(self, account_id: str) -> Dict[str, Any]:
        system = self.system(account_id)
        record = self._record(account_id) or {}
        return {
            'id': system.account_id,
            'name': system.account_name,
            'legacy': bool(record.get('legacy')),
            'logged_in': bool(getattr(system, '_laike_logged_in', False)),
            'monitoring': bool(system.monitoring),
            'browser_open': bool(system._browser_alive()),
            'store_name': system.config.get('laike_account_name', ''),
            'user_name': system.config.get('laike_user_name', ''),
            'groupid': system.config.get('laike_groupid', ''),
            'biz_view_id': system.config.get('laike_biz_view_id', ''),
            'headless': system.config.get('headless', True),
            'active': system.account_id == self._active_id,
            'ai_enabled': bool(system.account_ai().get('ai_enabled')),
            'knowledge_count': len((system.account_ai().get('knowledge') or {}).get('bases') or []),
        }

    def public_state(self) -> Dict[str, Any]:
        self.load()
        accounts = []
        for record in list(self._records):
            try:
                accounts.append(self.public_account(record['id']))
            except Exception:
                continue
        ram = host_ram_mb()
        return {
            'success': True,
            'active_id': self._active_id,
            'max_concurrent_browsers': self._max_browsers,
            'browsers_running': self.browsers_running(),
            'host_ram_mb': ram,
            'memory_warning': memory_warning(ram, self._max_browsers, len(accounts)),
            'accounts': accounts,
        }

    def dashboard_status(self) -> Dict[str, Any]:
        self.load()
        rows = []
        for record in list(self._records):
            try:
                rows.append(self.public_account(record['id']))
            except Exception:
                continue
        monitoring = any(row['monitoring'] for row in rows)
        logged_in = any(row['logged_in'] or row['groupid'] for row in rows)
        summary = ''
        if len(rows) > 1:
            summary = '；'.join(
                f"{row['name']}{'监控中' if row['monitoring'] else '未监控'}" for row in rows
            )
        return {
            'monitoring': monitoring,
            'logged_in': logged_in,
            'rules_count': 0,
            'monitor_starting': False,
            'session_expired': False,
            'account_count': len(rows),
            'account_summary': summary,
        }

    def merged_logs(self, limit: int = 100) -> List[Dict[str, Any]]:
        rows: List[Dict[str, Any]] = []
        for record in list(self._records):
            try:
                system = self.system(record['id'])
            except Exception:
                continue
            rows.extend(system.logs[-limit:])
        rows.sort(key=lambda item: str(item.get('time') or ''))
        return rows[-limit:]

    def resume_monitoring(self) -> None:
        """重启后按各账号自己的开关恢复，账号之间错开几秒，并遵守浏览器上限。"""
        self.load()
        systems = []
        for record in list(self._records):
            try:
                system = self.system(record['id'])
            except Exception:
                continue
            if getattr(system, '_monitoring_wanted', False):
                systems.append(system)
        for index, system in enumerate(systems):
            if index:
                time.sleep(2)
            try:
                system.maybe_resume()
            except Exception as exc:  # noqa: BLE001
                try:
                    system.add_log(f'自动恢复监控失败: {exc}', 'warning')
                except Exception:
                    pass

    def start_all(self) -> Dict[str, Any]:
        self.load()
        results = []
        for record in list(self._records):
            system = self.system(record['id'])
            if system.monitoring:
                results.append({'id': system.account_id, 'name': system.account_name, 'success': True, 'skipped': True})
                continue
            result = system.start_monitoring()
            if result.get('success'):
                system._monitoring_wanted = True
                if not getattr(system, '_monitoring_since', 0):
                    system._monitoring_since = time.time()
                system._persist_state()
            results.append({'id': system.account_id, 'name': system.account_name, **result})
        ok = [r for r in results if r.get('success')]
        failed = [r for r in results if not r.get('success')]
        return {
            'success': not failed or bool(ok),
            'started': len([r for r in ok if not r.get('skipped')]),
            'results': results,
            'error': failed[0].get('error', '') if failed and not ok else '',
        }

    def stop_all(self) -> Dict[str, Any]:
        self.load()
        for record in list(self._records):
            system = self.system(record['id'])
            system._monitoring_wanted = False
            system._monitoring_since = 0
            if system.monitoring:
                system.stop_monitoring('手动停止全部账号')
            system._persist_state()
        return {'success': True}


def get_manager() -> LaikeAccountManager:
    global _manager
    with _manager_lock:
        if _manager is None:
            _manager = LaikeAccountManager()
        return _manager


def reset_manager_for_tests(manager: Optional[LaikeAccountManager] = None) -> None:
    global _manager
    with _manager_lock:
        _manager = manager
