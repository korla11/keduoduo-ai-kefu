"""抖音来客多账号隔离。不启动浏览器。"""
import json
import os
import shutil
import tempfile
import time
import unittest

from laike_accounts import (
    LaikeAccountManager,
    cmdline_has_profile,
    host_ram_mb,
    memory_warning,
)
from douyin_laike_reply_system import (
    DouyinLaikeReplySystem,
    conversation_open_reason,
    incoming_signature_changed,
)


class LaikeMultiAccountTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.mkdtemp(prefix='laike-accounts-')
        self.root = self._tmp

    def tearDown(self):
        shutil.rmtree(self._tmp, ignore_errors=True)

    def _seed_legacy(self, monitoring=True, name='老门店'):
        profile = os.path.join(self.root, 'douyin_laike_browser_profile')
        os.makedirs(profile, exist_ok=True)
        with open(os.path.join(profile, 'marker.txt'), 'w', encoding='utf-8') as handle:
            handle.write('keep-me')
        state = {
            'logged_in': True,
            'monitoring_enabled': monitoring,
            'monitoring_since': time.time(),
            'config': {
                'laike_account_name': name,
                'laike_groupid': '1824515348029440',
                'headless': False,
                'message_check_interval': 4,
            },
        }
        with open(os.path.join(self.root, 'douyin_laike_state.json'), 'w', encoding='utf-8') as handle:
            json.dump(state, handle)

    def test_legacy_profile_stays_put_and_becomes_first_account(self):
        self._seed_legacy()
        manager = LaikeAccountManager(self.root)
        manager.load()
        system = manager.system('default')
        marker = os.path.join(self.root, 'douyin_laike_browser_profile', 'marker.txt')
        self.assertTrue(os.path.exists(marker))
        self.assertEqual(os.path.abspath(system.profile_dir), os.path.abspath(os.path.join(self.root, 'douyin_laike_browser_profile')))
        self.assertEqual(system.account_name, '老门店')
        self.assertTrue(system._monitoring_wanted)
        self.assertFalse(system.config.get('headless', True))
        self.assertTrue(system.legacy_keys)
        self.assertEqual(system.user_key('张三'), 'laike:张三')
        with open(os.path.join(self.root, 'douyin_laike_state.json'), encoding='utf-8') as handle:
            saved = json.load(handle)
        self.assertTrue(saved['account_monitoring_enabled'])

    def test_second_account_has_its_own_profile_state_and_keys(self):
        self._seed_legacy(monitoring=False)
        manager = LaikeAccountManager(self.root)
        first = manager.system('default')
        second = manager.create('门店乙')
        self.assertNotEqual(os.path.abspath(first.profile_dir), os.path.abspath(second.profile_dir))
        parts = os.path.normpath(second.profile_dir).split(os.sep)
        self.assertEqual(parts[-1], 'douyin_laike_browser_profile')
        self.assertEqual(parts[-3], 'douyin_laike_accounts')
        self.assertEqual(parts[-2], second.account_id)
        self.assertTrue(second.profile_dir.startswith(os.path.abspath(self.root)))
        self.assertNotEqual(first.user_key('张三'), second.user_key('张三'))
        self.assertEqual(second.user_key('张三'), f'laike:{second.account_id}:张三')
        self.assertEqual(second.nick_from_key(second.user_key('张三')), '张三')
        self.assertFalse(cmdline_has_profile('--user-data-dir=' + first.profile_dir, second.profile_dir))
        self.assertTrue(cmdline_has_profile('--user-data-dir=' + second.profile_dir, second.profile_dir))

        second._monitoring_wanted = True
        second._persist_state()
        with open(os.path.join(self.root, 'douyin_laike_state.json'), encoding='utf-8') as handle:
            gateway = json.load(handle)
        self.assertTrue(gateway['monitoring_enabled'])
        self.assertFalse(gateway['account_monitoring_enabled'])

        reloaded = LaikeAccountManager(self.root)
        reloaded.load()
        self.assertFalse(reloaded.system('default')._monitoring_wanted)
        self.assertTrue(reloaded.system(second.account_id)._monitoring_wanted)
        self.assertEqual(reloaded.system(second.account_id).profile_dir, second.profile_dir)

    def test_windows_and_posix_command_lines_do_not_cross(self):
        win_a = r'C:\KeDuoDuo\users\u\douyin_laike_accounts\acc_abcd1234\douyin_laike_browser_profile'
        win_b = r'C:\KeDuoDuo\users\u\douyin_laike_accounts\acc_ffffffff\douyin_laike_browser_profile'
        cmd = '--user-data-dir=' + win_a + ' --lang=zh-CN'
        self.assertTrue(cmdline_has_profile(cmd, win_a))
        self.assertFalse(cmdline_has_profile(cmd, win_b))
        self.assertTrue(cmdline_has_profile(cmd.replace('\\', '/'), win_a))
        posix_a = '/home/u/douyin_laike_accounts/acc_abcd1234/douyin_laike_browser_profile'
        posix_b = '/home/u/douyin_laike_browser_profile'
        self.assertTrue(cmdline_has_profile('--user-data-dir=' + posix_a, posix_a))
        self.assertFalse(cmdline_has_profile('--user-data-dir=' + posix_a, posix_b))

    def test_browser_cap_blocks_a_second_profile(self):
        self._seed_legacy(monitoring=False)
        manager = LaikeAccountManager(self.root)
        manager.set_max_browsers(1)
        first = manager.system('default')
        second = manager.create('门店乙')

        class _Alive:
            def is_alive(self):
                return True

        first._browser = type('Browser', (), {'_thread': _Alive()})()
        denied = manager.reserve_error(second.account_id)
        self.assertIsNotNone(denied)
        self.assertIn('上限', denied)
        self.assertIsNone(manager.reserve_error(first.account_id))

    def _write_shared_config(self):
        config = {
            'ai_provider': {
                'enabled': True,
                'api_key': 'test-key',
                'platforms': {'douyin_laike': True, 'douyin': False},
                'platform_settings': {
                    'douyin_laike': {'persona': '默认店专属人设', 'follow_up_delay': 333, 'reply_rules': '只对默认店'},
                },
            },
            'ai_knowledge_base': {
                'enabled': True,
                'bases': [{'id': 'b1', 'name': '价目', 'text': '宴会桌价 1999', 'documents': []}],
                'platform_assignments': {'douyin_laike': ['b1'], 'douyin': []},
            },
        }
        with open(os.path.join(self.root, 'config.json'), 'w', encoding='utf-8') as handle:
            json.dump(config, handle, ensure_ascii=False)

    def test_default_account_keeps_shared_ai_and_new_account_starts_empty(self):
        self._seed_legacy(monitoring=False)
        self._write_shared_config()
        manager = LaikeAccountManager(self.root)
        default = manager.system('default')
        self.assertTrue(default.account_ai().get('ai_enabled'))
        self.assertIn('默认店专属人设', default._platform_settings()['persona'])
        self.assertEqual(default._platform_settings()['follow_up_delay'], 333)
        self.assertIn('宴会桌价 1999', default._knowledge()['bases'][0]['text'])
        self.assertTrue(default.account_ai().get('migrated_from_shared'))
        with open(os.path.join(self.root, 'config.json'), encoding='utf-8') as handle:
            shared = json.load(handle)
        self.assertEqual(shared['ai_knowledge_base']['bases'][0]['id'], 'b1')

        second = manager.create('门店乙')
        self.assertFalse(second.account_ai().get('ai_enabled'))
        self.assertEqual(second.account_ai().get('source'), 'empty')
        self.assertEqual(second._knowledge()['bases'], [])
        self.assertNotIn('默认店专属人设', second._platform_settings()['persona'])
        self.assertNotIn('宴会桌价 1999', json.dumps(second.account_ai(), ensure_ascii=False))

        default.save_account_settings({'persona': '改完仍只属于默认店', 'follow_up_delay': 400}, ai_enabled=True)
        second.save_account_knowledge_base('', '乙店资料', '乙店只卖鲜花')
        second.save_account_knowledge_settings(True, [second._knowledge()['bases'][0]['id']])
        second.save_account_settings({'persona': '乙店人设', 'silent_greet_text': '乙店在的'}, ai_enabled=True)
        self.assertIn('改完仍只属于默认店', manager.system('default')._platform_settings()['persona'])
        self.assertNotIn('乙店', manager.system('default')._platform_settings()['persona'])
        self.assertNotIn('鲜花', json.dumps(manager.system('default')._knowledge(), ensure_ascii=False))
        self.assertIn('乙店人设', second._platform_settings()['persona'])
        self.assertEqual(second._platform_settings()['silent_greet_text'], '乙店在的')
        self.assertNotEqual(
            default._knowledge()['bases'][0]['id'],
            second._knowledge()['bases'][0]['id'],
        )

        copied = manager.create('门店丙', copy_from=second.account_id)
        self.assertTrue(copied.account_ai().get('ai_enabled'))
        self.assertIn('乙店人设', copied._platform_settings()['persona'])
        self.assertIn('乙店只卖鲜花', copied._knowledge()['bases'][0]['text'])
        self.assertNotEqual(copied._knowledge()['bases'][0]['id'], second._knowledge()['bases'][0]['id'])
        copied.save_account_settings({'persona': '丙店人设'}, ai_enabled=False)
        self.assertIn('乙店人设', manager.system(second.account_id)._platform_settings()['persona'])
        self.assertFalse(copied.account_ai().get('ai_enabled'))

    def test_existing_account_without_ai_file_gets_its_own_snapshot(self):
        self._seed_legacy(monitoring=False)
        self._write_shared_config()
        manager = LaikeAccountManager(self.root)
        second = manager.create('门店乙')
        os.remove(second._account_ai_path())
        second._ai_doc = None
        second.ensure_account_ai()
        self.assertTrue(str(second.account_ai().get('source') or '').startswith('snapshot'))
        self.assertIn('默认店专属人设', second._platform_settings()['persona'])
        self.assertIn('宴会桌价 1999', second._knowledge()['bases'][0]['text'])
        self.assertNotEqual(
            manager.system('default')._knowledge()['bases'][0]['id'],
            second._knowledge()['bases'][0]['id'],
        )
        second.save_account_settings({'persona': '乙的副本已改'}, ai_enabled=True)
        self.assertIn('默认店专属人设', manager.system('default')._platform_settings()['persona'])

    def test_stopping_one_account_leaves_the_other_monitoring(self):
        self._seed_legacy(monitoring=False, name='信阳盛世百花礼宴艺术中心')
        manager = LaikeAccountManager(self.root)
        first = manager.system('default')
        second = manager.create('郑州云上花开宴会艺术中心旗舰店')
        first.monitoring = True
        second.monitoring = True
        first.live.update({'scan_count': 6, 'conversations': 8, 'last_scan': '21:18:21'})
        second.live.update({'scan_count': 91, 'conversations': 20, 'last_scan': '21:19:30'})
        manager.select(second.account_id)
        self.assertTrue(first.monitoring)
        self.assertTrue(second.monitoring)
        first.stop_monitoring('手动停止当前账号')
        self.assertFalse(first.monitoring)
        self.assertTrue(second.monitoring)
        self.assertEqual(second.live['scan_count'], 91)
        statuses = {row.account_id: row.live_status() for row in (first, second)}
        self.assertFalse(statuses[first.account_id]['monitoring'])
        self.assertTrue(statuses[second.account_id]['monitoring'])
        self.assertEqual(statuses[second.account_id]['conversations'], 20)
        self.assertTrue(any('手动停止当前账号' in item['message'] for item in first.logs))
        self.assertFalse(any('监控已停止' in item['message'] for item in second.logs))

    def test_repeated_identical_message_is_still_opened(self):
        now = time.time()
        fresh = conversation_open_reason(
            preview='测试监控', unread=0, last_time='刚刚', parsed_time=now,
            seen=True, seen_preview='测试监控', seen_time=now - 5, seen_time_text='刚刚',
            monitor_started_at=now - 120, allow_fresh=True,
        )
        self.assertEqual(fresh, 'fresh')
        held = conversation_open_reason(
            preview='测试监控', unread=0, last_time='刚刚', parsed_time=now,
            seen=True, seen_preview='测试监控', seen_time=now - 5, seen_time_text='刚刚',
            monitor_started_at=now - 120, allow_fresh=False,
        )
        self.assertEqual(held, '')
        changed = conversation_open_reason(
            preview='测试监控', unread=0, last_time='刚刚', parsed_time=now,
            seen=True, seen_preview='测试监控', seen_time=now - 4000, seen_time_text='19:51',
            monitor_started_at=now - 120, allow_fresh=False,
        )
        self.assertEqual(changed, 'time')
        old = conversation_open_reason(
            preview='测试监控', unread=0, last_time='昨天', parsed_time=now - 86400 * 2,
            seen=False, seen_preview='', seen_time=None, seen_time_text=None,
            monitor_started_at=now, allow_fresh=False,
        )
        self.assertEqual(old, 'baseline')
        sig = '1#0::测试监控'
        self.assertFalse(incoming_signature_changed(sig, sig, 4, 4))
        self.assertFalse(incoming_signature_changed(sig, sig, None, 5))
        self.assertTrue(incoming_signature_changed(sig, sig, 4, 5))

    def test_logs_and_followup_stay_on_that_account(self):
        self._seed_legacy(monitoring=False)
        manager = LaikeAccountManager(self.root)
        first = manager.system('default')
        second = manager.create('门店乙')
        stamp = time.strftime('%Y-%m-%d %H:%M:%S')
        with open(second._file('douyin_laike.log'), 'w', encoding='utf-8') as handle:
            handle.write(f'{stamp} [success] ✅ [张三] 来客已回复｜账号:门店乙\n')
        second._restore_followup_state()
        self.assertIn(second.user_key('张三'), second._last_reply_time)
        self.assertNotIn(second.user_key('张三'), first._last_reply_time)
        second.add_log('📩 [李四] 新消息: 你好', 'info')
        with open(second._file('douyin_laike.log'), encoding='utf-8') as handle:
            own_log = handle.read()
        self.assertIn('｜账号:门店乙', own_log)
        self.assertIn('新消息', own_log)
        legacy_log = os.path.join(self.root, 'douyin_laike.log')
        if os.path.exists(legacy_log):
            self.assertNotIn('李四', open(legacy_log, encoding='utf-8').read())

    def test_delete_removes_only_that_profile(self):
        self._seed_legacy(monitoring=False)
        manager = LaikeAccountManager(self.root)
        second = manager.create('门店乙')
        profile = second.profile_dir
        os.makedirs(profile, exist_ok=True)
        with open(os.path.join(profile, 'cookie.txt'), 'w', encoding='utf-8') as handle:
            handle.write('secret-session')
        legacy_marker = os.path.join(self.root, 'douyin_laike_browser_profile', 'marker.txt')
        manager.remove(second.account_id)
        self.assertFalse(os.path.exists(profile))
        self.assertTrue(os.path.exists(legacy_marker))
        ids = [row['id'] for row in manager.public_state()['accounts']]
        self.assertNotIn(second.account_id, ids)
        self.assertIn('default', ids)

    def test_memory_warning_mentions_small_hosts(self):
        self.assertIn('内存', memory_warning(1700, 3, 2))
        self.assertEqual(memory_warning(16000, 3, 1), '')
        self.assertGreaterEqual(host_ram_mb(), 0)

    def test_z_http_page_lists_isolated_accounts(self):
        os.environ['LAIKE_DISABLE_AUTO_RESUME'] = '1'
        from flask import Flask
        from laike_accounts import reset_manager_for_tests
        from douyin_laike_reply_system import laike_dashboard_status, register_laike_routes
        self.addCleanup(lambda: os.environ.pop('LAIKE_DISABLE_AUTO_RESUME', None))
        self.addCleanup(lambda: reset_manager_for_tests(None))
        self._seed_legacy(monitoring=False, name='老门店')
        manager = LaikeAccountManager(self.root)
        reset_manager_for_tests(manager)
        app = Flask(__name__)
        register_laike_routes(app)
        client = app.test_client()
        page = client.get('/laike')
        self.assertEqual(page.status_code, 200)
        html = page.get_data(as_text=True)
        self.assertIn('来客账号', html)
        self.assertIn('laike-max-browsers', html)
        self.assertIn('laike-copy-from', html)
        self.assertNotIn('laike-override-enabled', html)
        created = client.post('/api/laike-accounts', json={'name': '门店乙'})
        body = created.get_json()
        self.assertTrue(body['success'], body)
        second_id = body['account']['id']
        status = client.get('/api/laike-status?account_id=' + second_id).get_json()
        self.assertEqual(status['account_id'], second_id)
        self.assertEqual(len(status['accounts']), 2)
        profiles = {row['id']: manager.system(row['id']).profile_dir for row in status['accounts']}
        self.assertNotEqual(profiles['default'], profiles[second_id])
        self.assertTrue(profiles['default'].endswith(os.path.join('douyin_laike_browser_profile')))
        renamed = client.post('/api/laike-accounts/rename', json={'id': second_id, 'name': '乙店'}).get_json()
        self.assertEqual(renamed['account']['name'], '乙店')
        self.assertEqual(client.post('/api/laike-accounts/limits', json={'max_concurrent_browsers': 1}).get_json()['max_concurrent_browsers'], 1)
        saved = client.post(
            '/api/laike-accounts/settings?account_id=' + second_id,
            json={'enabled': True, 'settings': {'follow_up_delay': 88, 'persona': '乙店人设'}},
        ).get_json()
        self.assertEqual(saved['effective']['follow_up_delay'], 88)
        self.assertFalse(saved['shared'])
        default_ai = client.get('/api/laike-accounts/ai?account_id=default').get_json()
        self.assertNotIn('乙店人设', default_ai['settings']['persona'])
        self.assertNotEqual(default_ai['settings']['follow_up_delay'], 88)
        kb = client.post(
            '/api/laike-accounts/knowledge?account_id=' + second_id,
            json={'name': '乙店知识库', 'text': '只属于乙店的桌价'},
        ).get_json()
        self.assertTrue(kb['success'], kb)
        other_kb = client.get('/api/laike-accounts/knowledge?account_id=default').get_json()
        self.assertEqual(other_kb['bases'], [])
        self.assertNotIn('只属于乙店的桌价', json.dumps(other_kb, ensure_ascii=False))
        manager.system('default').monitoring = True
        manager.system(second_id).monitoring = True
        manager.system('default').live['scan_count'] = 4
        manager.system(second_id).live['scan_count'] = 9
        client.post('/api/laike-accounts/select', json={'id': 'default'})
        stopped = client.post('/api/laike-stop?account_id=default').get_json()
        self.assertTrue(stopped['success'])
        self.assertFalse(manager.system('default').monitoring)
        self.assertTrue(manager.system(second_id).monitoring)
        merged = client.get('/api/laike-logs?scope=all').get_json()
        by_id = {row['account_id']: row for row in merged['statuses']}
        self.assertIn('default', by_id)
        self.assertIn(second_id, by_id)
        self.assertFalse(by_id['default']['monitoring'])
        self.assertTrue(by_id[second_id]['monitoring'])
        self.assertEqual(by_id[second_id]['scan_count'], 9)
        dash = laike_dashboard_status()
        self.assertEqual(dash['account_count'], 2)
        self.assertIn('乙店', dash['account_summary'])
        self.assertIn('老门店', dash['account_summary'])


if __name__ == '__main__':
    unittest.main()
