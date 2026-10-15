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
from douyin_laike_reply_system import DouyinLaikeReplySystem


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

    def test_settings_override_falls_back_to_shared(self):
        system = DouyinLaikeReplySystem(account_id='acc_1234abcd', account_name='门店乙', data_dir=self.root, legacy=False)
        shared = system._platform_settings()
        system.set_settings_override(True, {'follow_up_delay': 77, 'persona': '只属于乙店的人设'})
        overridden = system._platform_settings()
        self.assertEqual(overridden['follow_up_delay'], 77)
        self.assertIn('只属于乙店', overridden['persona'])
        self.assertEqual(overridden.get('reply_rules'), shared.get('reply_rules'))
        system.set_settings_override(False, {'follow_up_delay': 77})
        self.assertNotEqual(system._platform_settings()['follow_up_delay'], 77)

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
        self.assertIn('来客账号', page.get_data(as_text=True))
        self.assertIn('laike-max-browsers', page.get_data(as_text=True))
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
        dash = laike_dashboard_status()
        self.assertEqual(dash['account_count'], 2)
        self.assertIn('乙店', dash['account_summary'])
        self.assertIn('老门店', dash['account_summary'])


if __name__ == '__main__':
    unittest.main()
