"""CloudSubscribe 与 DownloadCloudUpload 共存规则的离线测试。

coexist.py 不依赖插件包里的其他模块，这里按文件单独加载，避免为了测试纯规则
而导入整个插件（会连带 p115client 等宿主依赖）。
"""

import ast
import importlib.util
import json
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
PLUGIN_DIR = ROOT / 'plugins.v3' / 'cloudsubscribe'

_spec = importlib.util.spec_from_file_location(
    'cloudsubscribe_coexist_under_test', PLUGIN_DIR / 'core' / 'coexist.py')
coexist = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(coexist)

COOKIE = 'UID=1_A1_123; CID=abc; SEID=def; KID=x'


def reader(configs):
    """返回按插件 ID 取配置的假读取器。"""
    return lambda plugin_id: configs.get(plugin_id) or {}


class ForcedValuesTest(unittest.TestCase):
    def test_pt_upgrade_is_forced_off(self):
        config = {'enable_pt_upgrade': True}
        self.assertEqual(coexist.enforce_forced_values(config), ['enable_pt_upgrade'])
        self.assertIs(config['enable_pt_upgrade'], False)

    def test_missing_key_is_filled_without_reporting(self):
        config = {}
        self.assertEqual(coexist.enforce_forced_values(config), [])
        self.assertIs(config['enable_pt_upgrade'], False)

    def test_new_install_defaults_do_not_take_over(self):
        self.assertEqual(coexist.COEXIST_DEFAULTS['subscribe_filter_mode'], 'include')
        self.assertEqual(coexist.COEXIST_DEFAULTS['platform_download_policy'], 'allow')
        self.assertIs(coexist.COEXIST_DEFAULTS['strm_generate_enabled'], False)


class CookieTest(unittest.TestCase):
    def test_reuses_strm_helper_cookie_by_default(self):
        cookie, notes = coexist.resolve_115_cookie(
            {'p115_transfer_path': '/转存'}, reader({'P115StrmHelper': {'cookies': COOKIE}}))
        self.assertEqual(cookie, COOKIE)
        self.assertEqual(notes, [])

    def test_strm_helper_cookie_missing_disables_115(self):
        cookie, notes = coexist.resolve_115_cookie({}, reader({}))
        self.assertEqual(cookie, '')
        self.assertTrue(notes)

    def test_own_cookie_ignores_strm_helper(self):
        cookie, _ = coexist.resolve_115_cookie(
            {'p115_cookie_source': 'own', 'cookies': ''},
            reader({'P115StrmHelper': {'cookies': COOKIE}}))
        self.assertEqual(cookie, '')
        cookie, _ = coexist.resolve_115_cookie(
            {'p115_cookie_source': 'own', 'cookies': f' {COOKIE} '}, reader({}))
        self.assertEqual(cookie, COOKIE)

    def test_incomplete_cookie_is_rejected(self):
        self.assertEqual(coexist.valid_115_cookie('UID=1; CID=2'), '')
        self.assertEqual(coexist.valid_115_cookie(COOKIE + '\nX=1'), '')
        self.assertEqual(coexist.valid_115_cookie(None), '')

    def test_empty_reader_result_means_no_cookie(self):
        self.assertEqual(coexist.strm_helper_cookie(lambda _pid: None), '')

    def test_unknown_source_falls_back_to_strm_helper(self):
        self.assertEqual(coexist.normalize_cookie_source('???'), 'strm_helper')
        self.assertEqual(coexist.normalize_cookie_source(' OWN '), 'own')


class DirectoryOverlapTest(unittest.TestCase):
    targets = ['/媒体/上传', '/备份']

    def test_overlap_cases(self):
        self.assertEqual(coexist.overlapping_targets('/媒体/上传', self.targets), ['/媒体/上传'])
        self.assertEqual(coexist.overlapping_targets('/媒体/上传/子目录', self.targets), ['/媒体/上传'])
        self.assertEqual(coexist.overlapping_targets('/媒体', self.targets), ['/媒体/上传'])
        self.assertEqual(coexist.overlapping_targets('媒体/上传/', self.targets), ['/媒体/上传'])

    def test_separate_directories_and_root(self):
        self.assertEqual(coexist.overlapping_targets('/转存', self.targets), [])
        self.assertEqual(coexist.overlapping_targets('/', self.targets), [])
        self.assertEqual(coexist.overlapping_targets('', self.targets), [])

    def test_only_enabled_upload_rules_count(self):
        configs = {'DownloadCloudUpload': {'enabled': True, 'rules': [
            {'target': '/媒体/上传', 'enabled': True},
            {'target': '/停用', 'enabled': False},
        ]}}
        self.assertEqual(coexist.upload_targets(reader(configs)), ['/媒体/上传'])
        configs['DownloadCloudUpload']['enabled'] = False
        self.assertEqual(coexist.upload_targets(reader(configs)), [])

    def test_overlap_disables_115(self):
        configs = {
            'P115StrmHelper': {'cookies': COOKIE},
            'DownloadCloudUpload': {'enabled': True, 'rules': [{'target': '/媒体/上传', 'enabled': True}]},
        }
        cookie, notes = coexist.resolve_115_cookie({'p115_transfer_path': '/媒体/上传/分享'}, reader(configs))
        self.assertEqual(cookie, '')
        self.assertIn('/媒体/上传', notes[0])
        cookie, notes = coexist.resolve_115_cookie({'p115_transfer_path': '/分享转存'}, reader(configs))
        self.assertEqual(cookie, COOKIE)
        self.assertEqual(notes, [])


class QRCodeGuardTest(unittest.TestCase):
    def test_115_scan_blocked_while_reusing_strm_helper(self):
        self.assertTrue(coexist.qrcode_blocked('115', {}))
        self.assertIsNone(coexist.qrcode_blocked('115', {'p115_cookie_source': 'own'}))
        self.assertIsNone(coexist.qrcode_blocked('quark', {}))


class WiringTest(unittest.TestCase):
    """静态检查插件入口确实接入了共存规则，且清单与版本一致。"""

    source = (PLUGIN_DIR / '__init__.py').read_text(encoding='utf-8')

    def test_entry_uses_sdk_base_and_coexist(self):
        self.assertIn('from app.sdk.plugin import _PluginBase', self.source)
        self.assertNotIn('from app.plugins import _PluginBase', self.source)
        self.assertIn('enforce_forced_values(config)', self.source)
        self.assertIn('self._apply_coexist_config(config)', self.source)

    def test_manifest_keeps_download_upload_and_matches_version(self):
        package = json.loads((ROOT / 'package.v3.json').read_text(encoding='utf-8'))
        self.assertIn('DownloadCloudUpload', package)
        version = next(
            node.value.value for node in ast.walk(ast.parse(self.source))
            if isinstance(node, ast.Assign)
            and any(getattr(t, 'id', '') == 'plugin_version' for t in node.targets)
        )
        self.assertEqual(package['CloudSubscribe']['version'], version)
        self.assertEqual(next(iter(package['CloudSubscribe']['history'])), f'v{version}')

    def test_empty_115_cookie_is_not_registered(self):
        definition = (PLUGIN_DIR / 'drive' / 'p115' / 'definition.py').read_text(encoding='utf-8')
        self.assertIn('if not cookies:\n            # 空 Cookie 不注册 115', definition)

    def test_include_mode_never_blocks_global_platform_search(self):
        hook = (PLUGIN_DIR / 'core' / 'hook' / 'subscription.py').read_text(encoding='utf-8')
        self.assertIn('self._subscribe_filter_mode != "include"', hook)

    def test_built_frontend_is_present(self):
        self.assertTrue((PLUGIN_DIR / 'dist' / 'assets' / 'remoteEntry.js').is_file())


if __name__ == '__main__':
    unittest.main()
