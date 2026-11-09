"""M1 同盘优先：选盘规则与转存作用域的离线测试。

只加载不依赖宿主的几个模块（cloud.py、drive_routing.py、drive_scope.py），
用占位包承接相对导入，避免为了纯规则导入整个插件。
"""

import importlib.util
import sys
import threading
import types
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
PLUGIN_DIR = ROOT / 'plugins.v3' / 'cloudsubscribe'
PKG = 'cloudsubscribe_routing_under_test'


def _package(name):
    module = types.ModuleType(name)
    module.__path__ = []
    sys.modules[name] = module
    return module


def _load(name, relative_path):
    spec = importlib.util.spec_from_file_location(name, PLUGIN_DIR / relative_path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


for _name in (PKG, f'{PKG}.core', f'{PKG}.handlers', f'{PKG}.handlers.sync'):
    _package(_name)
cloud = _load(f'{PKG}.core.cloud', 'core/cloud.py')
routing = _load(f'{PKG}.core.drive_routing', 'core/drive_routing.py')
scope = _load(f'{PKG}.handlers.sync.drive_scope', 'handlers/sync/drive_scope.py')

Cap = cloud.CloudDriveCapability


class RoutingRulesTest(unittest.TestCase):
    def test_preferred_drives_keep_legacy_cloud_drive_first(self):
        self.assertEqual(routing.normalize_preferred_drives(None, 'quark'), ['quark', '115'])
        self.assertEqual(routing.normalize_preferred_drives(None, None), ['115', 'quark'])
        self.assertEqual(routing.normalize_preferred_drives('Quark, 115,quark', '115'), ['quark', '115'])
        self.assertEqual(routing.normalize_preferred_drives(['115'], 'quark'), ['115'])

    def test_default_drive_is_first_logged_in(self):
        self.assertEqual(routing.pick_default_drive(['115', 'quark'], ['quark']), 'quark')
        self.assertEqual(routing.pick_default_drive(['115', 'quark'], ['115', 'quark']), '115')
        self.assertEqual(routing.pick_default_drive(['115', 'quark'], []), '115')

    def test_targets_require_login_list_and_switch(self):
        self.assertEqual(routing.same_cloud_targets(['115', 'quark'], ['quark', '115', 'baidu'], True),
                         ['115', 'quark'])
        self.assertEqual(routing.same_cloud_targets(['115', 'quark'], ['115'], True), ['115'])
        self.assertEqual(routing.same_cloud_targets(['115', 'quark'], ['115', 'quark'], False), [])

    def test_route_share(self):
        targets = ['115', 'quark']
        self.assertEqual(routing.route_share('quark', '115', targets), 'quark')
        self.assertIsNone(routing.route_share('115', '115', targets))
        self.assertIsNone(routing.route_share('baidu', '115', targets))
        self.assertIsNone(routing.route_share(None, '115', targets))
        # 只登录了 115：夸克分享不会被写进未登录的夸克
        self.assertIsNone(routing.route_share('quark', '115', ['115']))

    def test_search_types_and_order(self):
        allowed = routing.search_resource_types({'115', 'magnet'}, [{'quark'}])
        self.assertEqual(allowed, frozenset({'115', 'magnet', 'quark'}))
        self.assertEqual(routing.extend_resource_order(['115', 'ed2k'], ['quark']), ['115', 'ed2k', 'quark'])
        self.assertEqual(routing.extend_resource_order(['quark', '115'], ['quark']), ['quark', '115'])


class FakeService:
    def __init__(self, drive, capability):
        self.drive = drive
        self.capability = capability


class FakeProvider:
    def __init__(self, key, capabilities):
        self.key = key
        self.name = key
        self.capabilities = set(capabilities)
        self.policy = types.SimpleNamespace(max_batch_size=10, supports_batch=True)

    def supports(self, capability):
        return capability in self.capabilities

    def require(self, capability):
        return FakeService(self.key, capability)


class FakeRegistry:
    def __init__(self, *providers):
        self.providers = {p.key: p for p in providers}

    def get(self, key):
        return self.providers[key]


class Handler(scope.DriveScopeMixin):
    """最小 SyncHandler 替身：只保留作用域相关属性。"""

    _CLOUD_MEDIA_ROOT = '/'

    def __init__(self, default, registry, sources):
        self._cloud_drive = default
        self._cloud_transfer_path = '/115转存'
        self._share_transfer = default.require(Cap.SHARE_TRANSFER)
        self._batch_size = 20
        self._strm_generate_enabled = True
        self._strm_generator = 'strm-115'
        self._cloud_drive_registry = registry
        self._cloud_transfer_paths = {'115': '/115转存', 'quark': '/夸克转存/'}
        self._cloud_media_paths = {'quark': '/夸克媒体'}
        self._sources = sources

    @staticmethod
    def _normalize_cloud_path(value):
        return '/' + str(value).strip('/') if str(value).strip('/') else '/'

    def _resource_provider_for_url(self, url):
        return self._sources.get(url)


scope.install_scoped_attributes(Handler)


class DriveScopeTest(unittest.TestCase):
    def setUp(self):
        share = {Cap.SHARE_TRANSFER, Cap.FILE_QUERY, Cap.LOCAL_UPLOAD}
        self.p115 = FakeProvider('115', share | {Cap.OFFLINE_DOWNLOAD})
        self.quark = FakeProvider('quark', share)
        registry = FakeRegistry(self.p115, self.quark)
        self.handler = Handler(self.p115, registry, {
            'https://115.com/s/a': self.p115,
            'https://pan.quark.cn/s/b': self.quark,
        })
        self.handler._build_drive_profiles(['quark'], 50)

    def test_default_values_outside_scope(self):
        h = self.handler
        self.assertIs(h._cloud_drive, self.p115)
        self.assertEqual(h._cloud_transfer_path, '/115转存')
        self.assertEqual(h._CLOUD_MEDIA_ROOT, '/')
        self.assertEqual(h._share_transfer.drive, '115')

    def test_quark_scope_switches_drive_paths_and_services(self):
        h = self.handler
        with h.drive_scope_for_url('https://pan.quark.cn/s/b') as key:
            self.assertEqual(key, 'quark')
            self.assertIs(h._cloud_drive, self.quark)
            self.assertEqual(h._cloud_transfer_path, '/夸克转存')
            self.assertEqual(h._CLOUD_MEDIA_ROOT, '/夸克媒体')
            self.assertEqual(h._share_transfer.drive, 'quark')
            self.assertIsNone(h._offline_download)
            self.assertEqual(h._batch_size, 10)
            self.assertFalse(h._strm_generate_enabled)
            self.assertEqual(h.scoped_drive_key(), 'quark')
        self.assertIs(h._cloud_drive, self.p115)
        self.assertIsNone(h.scoped_drive_key())

    def test_115_share_stays_on_default(self):
        with self.handler.drive_scope_for_url('https://115.com/s/a') as key:
            self.assertIsNone(key)
            self.assertIs(self.handler._cloud_drive, self.p115)

    def test_nested_default_scope_switches_back(self):
        h = self.handler
        with h.drive_scope('quark'):
            with h.drive_scope(None):
                self.assertIs(h._cloud_drive, self.p115)
            self.assertIs(h._cloud_drive, self.quark)

    def test_no_routing_pins_default(self):
        h = self.handler
        with h.no_drive_routing():
            with h.drive_scope_for_url('https://pan.quark.cn/s/b') as key:
                self.assertIsNone(key)
                self.assertIs(h._cloud_drive, self.p115)

    def test_scope_is_per_thread_and_propagates_explicitly(self):
        h = self.handler
        seen = {}

        def other_thread():
            seen['other'] = h._cloud_drive.key

        with h.drive_scope('quark'):
            thread = threading.Thread(target=other_thread)
            thread.start()
            thread.join()
            with ThreadPoolExecutor(max_workers=2) as pool:
                seen['worker'] = pool.submit(
                    scope.run_in_current_scope(lambda: h._cloud_drive.key)
                ).result()
        self.assertEqual(seen, {'other': '115', 'worker': 'quark'})

    def test_unknown_or_unready_drive_is_ignored(self):
        h = self.handler
        with h.drive_scope('baidu') as key:
            self.assertIsNone(key)
            self.assertIs(h._cloud_drive, self.p115)
        empty = Handler(self.p115, FakeRegistry(self.p115), {})
        empty._build_drive_profiles(['quark'], 50)
        self.assertEqual(empty.same_cloud_drive_keys(), [])


class WiringTest(unittest.TestCase):
    """静态检查关键路径确实接入了作用域。"""

    def read(self, relative):
        return (PLUGIN_DIR / relative).read_text(encoding='utf-8')

    def test_sync_handler_uses_scope(self):
        service = self.read('handlers/sync/service.py')
        self.assertIn('class SyncHandler(DriveScopeMixin)', service)
        self.assertIn('install_scoped_attributes(SyncHandler)', service)
        self.assertIn('run_in_current_scope(transfer_one, item)', service)
        self.assertIn('"drive_key": self.scoped_drive_key()', service)

    def test_resource_loops_enter_scope(self):
        for relative in ('handlers/sync/movie.py', 'handlers/sync/television.py'):
            source = self.read(relative)
            self.assertIn('with ExitStack() as resource_scope:', source)
            self.assertIn('self.drive_scope_for_url(share_url)', source)

    def test_postprocess_groups_by_drive(self):
        self.assertIn('"drive_key": drive_key', self.read('handlers/sync/postprocess.py'))
        self.assertIn('monitor_group', self.read('core/services/runtime.py'))

    def test_upgrade_stays_on_default_drive(self):
        self.assertIn('with self.no_drive_routing():', self.read('handlers/sync/television.py'))
        self.assertIn('with self.no_drive_routing():', self.read('handlers/sync/upgrade.py'))


if __name__ == '__main__':
    unittest.main()
