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
PLUGIN_DIR = ROOT / 'plugins.v3' / 'cloudsubscribefork'
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
_package(f'{PKG}.utils')
strm = _load(f'{PKG}.utils.strm', 'utils/strm.py')

# drive_scope 在函数里按需导入 core.media 与 app.sdk.logging，这里给最小替身
_media = types.ModuleType(f'{PKG}.core.media')
_media.tmdb_id_of = lambda value: getattr(value, 'tmdbid', None)
sys.modules[f'{PKG}.core.media'] = _media
for _name in ('app', 'app.sdk'):
    sys.modules.setdefault(_name, _package(_name) if _name not in sys.modules else sys.modules[_name])
if 'app.sdk.logging' not in sys.modules:
    _logging = types.ModuleType('app.sdk.logging')
    _logging.logger = types.SimpleNamespace(
        warning=lambda *a, **k: None, info=lambda *a, **k: None, debug=lambda *a, **k: None,
    )
    sys.modules['app.sdk.logging'] = _logging

if 'starlette' not in sys.modules:
    try:
        import starlette  # noqa: F401
    except ImportError:
        _starlette = _package('starlette')
        _status = types.ModuleType('starlette.status')
        for _code, _attr in ((302, 'HTTP_302_FOUND'), (404, 'HTTP_404_NOT_FOUND'),
                             (502, 'HTTP_502_BAD_GATEWAY'), (503, 'HTTP_503_SERVICE_UNAVAILABLE')):
            setattr(_status, _attr, _code)
        _starlette.status = _status
        sys.modules['starlette.status'] = _status
_core = sys.modules[f'{PKG}.core']
_core.CloudDriveCapability = cloud.CloudDriveCapability
_core.CloudFile = cloud.CloudFile
_core.OwnerDelegator = object
_package(f'{PKG}.core.api')
strm_proxy = _load(f'{PKG}.core.api.strm_proxy', 'core/api/strm_proxy.py')


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


class M2RulesTest(unittest.TestCase):
    def test_offline_link_detection(self):
        self.assertTrue(routing.is_offline_link('magnet:?xt=urn:btih:abc'))
        self.assertTrue(routing.is_offline_link(' ED2K://|file|a|1|h|/'))
        self.assertFalse(routing.is_offline_link('https://pan.quark.cn/s/b'))

    def test_offline_goes_to_first_offline_drive(self):
        self.assertIsNone(routing.route_offline('115', True, []))
        self.assertEqual(routing.route_offline('quark', False, ['115', 'p123']), '115')
        self.assertIsNone(routing.route_offline('quark', False, []))

    def test_relay_sources_exclude_targets(self):
        self.assertEqual(
            routing.relay_sources(['115', 'quark', 'alipan'], ['115', 'quark']), ['alipan'])
        self.assertEqual(routing.relay_sources(['115', 'quark'], ['115']), ['quark'])

    def test_landed_drive_picks_latest_successful_record(self):
        records = [
            {'tmdb_id': 1, 'type': '电视剧', 'season': 1, 'status': '成功',
             'cloud_drive_key': '115', 'time': '2026-01-01 00:00:00'},
            {'tmdb_id': 1, 'type': '电视剧', 'season': 1, 'status': '成功',
             'cloud_drive_key': 'quark', 'time': '2026-02-01 00:00:00'},
            {'tmdb_id': 1, 'type': '电视剧', 'season': 1, 'status': '失败',
             'cloud_drive_key': '115', 'time': '2026-03-01 00:00:00'},
            {'tmdb_id': 1, 'type': '电视剧', 'season': 2, 'status': '成功',
             'cloud_drive_key': '115', 'time': '2026-04-01 00:00:00'},
            {'tmdb_id': 2, 'type': '电影', 'status': '成功', 'transfer_mode': 'cross',
             'cloud_drive_key': '115', 'source_drive_key': 'quark',
             'target_drive_key': '115', 'time': '2026-01-01 00:00:00'},
        ]
        self.assertEqual(routing.landed_drive_key(records, 1, season=1), 'quark')
        self.assertEqual(routing.landed_drive_key(records, 1, season=2), '115')
        self.assertIsNone(routing.landed_drive_key(records, 1, season=3))
        self.assertEqual(routing.landed_drive_key(records, 2, is_movie=True), '115')
        self.assertIsNone(routing.landed_drive_key(records, 2, is_movie=False))
        self.assertIsNone(routing.landed_drive_key(records, None, title=''))


class StrmTemplateTest(unittest.TestCase):
    G = None

    def setUp(self):
        self.G = strm.StrmGenerator

    def test_quark_default_with_pickcode_template_falls_back_to_file_id_proxy(self):
        template = self.G.drive_template(
            'quark', '{base_url}/d/{pickcode}?/{file_name}', {'file_id'}, 'file_id',
            proxy_enabled=True, route='d', is_default=True)
        self.assertEqual(template, '{base_url}/d/quark/{file_id}?/{file_name}')

    def test_default_keeps_fitting_template(self):
        self.assertEqual(
            self.G.drive_template('115', '{base_url}/d/{pickcode}?/{file_name}', {'pickcode'},
                                  'pickcode', proxy_enabled=True),
            '{base_url}/d/{pickcode}?/{file_name}')
        self.assertEqual(
            self.G.drive_template('quark', '{base_url}{file_path}', {'file_id'}, 'file_id'),
            '{base_url}{file_path}')

    def test_other_drive_needs_proxy(self):
        self.assertEqual(
            self.G.drive_template('quark', '{base_url}{file_path}', {'file_id'}, 'file_id',
                                  proxy_enabled=False, is_default=False), '')
        self.assertEqual(
            self.G.drive_template('quark', '', {'file_id'}, 'file_id',
                                  proxy_enabled=True, route='play', is_default=False),
            '{base_url}/play/quark/{file_id}?/{file_name}')


class FakeProxyOwner(strm_proxy.StrmProxyApi):
    def __init__(self, default, registry):
        self._cloud_drive = default
        self._cloud_drive_registry = registry
        self._applied_config = {'strm_proxy_enabled': True, 'strm_proxy_route': 'd'}


class StrmProxyDriveTest(unittest.TestCase):
    def setUp(self):
        ref115 = types.SimpleNamespace(proxy_identifier='pickcode')
        refq = types.SimpleNamespace(proxy_identifier='file_id')
        self.p115 = types.SimpleNamespace(
            key='115', supports=lambda c: True, require=lambda c: ref115)
        self.quark = types.SimpleNamespace(
            key='quark', supports=lambda c: True, require=lambda c: refq)
        self.owner = FakeProxyOwner(self.p115, FakeRegistry(self.p115, self.quark))

    def test_unprefixed_link_uses_default_drive(self):
        params = self.owner.short_params('/d/abc', {})
        self.assertEqual(params.get('pickcode'), 'abc')
        self.assertIs(self.owner._proxy_drive(params), self.p115)

    def test_prefixed_link_uses_named_drive(self):
        params = self.owner.short_params('/d/quark/fid123', {'/电影.mkv': ''})
        self.assertEqual(params.get('drive'), 'quark')
        self.assertEqual(params.get('file_id'), 'fid123')
        self.assertEqual(params.get('name'), '电影.mkv')
        self.assertIs(self.owner._proxy_drive(params), self.quark)
        item = self.owner._resolve_file(params, self.quark)
        self.assertEqual(item.playback_values, {'file_id': 'fid123'})

    def test_unknown_prefix_is_not_a_drive(self):
        self.assertIsNone(self.owner._proxy_drive({'drive': 'baidu'}))


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

    def test_magnet_goes_to_offline_drive_only(self):
        h = Handler(self.quark, FakeRegistry(self.p115, self.quark), {})
        h._offline_download = None
        h._build_drive_profiles(['115'], 50)
        with h.drive_scope_for_url('magnet:?xt=urn:btih:abc') as key:
            self.assertEqual(key, '115')
            self.assertIsNotNone(h._offline_download)
        # 默认盘能离线时留在默认盘
        self.handler._offline_download = self.p115.require(Cap.OFFLINE_DOWNLOAD)
        with self.handler.drive_scope_for_url('magnet:?xt=urn:btih:abc') as key:
            self.assertIsNone(key)

    def test_subscription_scope_uses_landed_drive_and_pins_it(self):
        h = self.handler
        h._get_data = lambda name: [
            {'tmdb_id': 7, 'type': '电视剧', 'season': 1, 'status': '成功',
             'cloud_drive_key': 'quark', 'time': '2026-01-01 00:00:00'},
        ]
        sub = types.SimpleNamespace(tmdbid=7, name='剧', season=1, type='电视剧')
        with h.subscription_drive_scope(sub) as key:
            self.assertEqual(key, 'quark')
            self.assertIs(h._cloud_drive, self.quark)
            # 洗版期间 115 分享不再切回 115，交给跨盘转进夸克
            with h.drive_scope_for_url('https://115.com/s/a'):
                self.assertIs(h._cloud_drive, self.quark)
        other = types.SimpleNamespace(tmdbid=8, name='新剧', season=1, type='电视剧')
        with h.subscription_drive_scope(other) as key:
            self.assertIsNone(key)
            self.assertIs(h._cloud_drive, self.p115)

    def test_quark_profile_gets_prefixed_strm_when_proxy_enabled(self):
        self.quark.capabilities.add(Cap.PLAYBACK_REFERENCE)
        h = Handler(self.p115, FakeRegistry(self.p115, self.quark), {})
        ref = types.SimpleNamespace(template_variables=frozenset({'file_id'}), proxy_identifier='file_id')
        self.quark.require = lambda capability: ref if capability == Cap.PLAYBACK_REFERENCE else FakeService('quark', capability)
        h._strm_settings = {'enabled': True, 'proxy_enabled': True, 'route': 'd',
                            'base_url': 'http://10.0.0.2', 'port': 9527}
        h._build_drive_profiles(['quark'], 50)
        with h.drive_scope('quark'):
            self.assertTrue(h._strm_generate_enabled)
            self.assertEqual(h._strm_generator.template, '{base_url}/d/quark/{file_id}?/{file_name}')
            self.assertEqual(h._strm_generator.base_url, 'http://10.0.0.2:9527')
            self.assertFalse(h._strm_by_helper)
        h._strm_settings['proxy_enabled'] = False
        h._build_drive_profiles(['quark'], 50)
        with h.drive_scope('quark'):
            self.assertFalse(h._strm_generate_enabled)

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

    def test_upgrade_and_retry_follow_landed_drive(self):
        self.assertIn('with self.subscription_drive_scope(subscribe):', self.read('handlers/sync/television.py'))
        self.assertIn('with self.subscription_drive_scope(subscribe):', self.read('handlers/sync/upgrade.py'))
        self.assertIn('self.subscription_drive_scope(subscribe) if upgrade_target_exists',
                      self.read('handlers/sync/movie.py'))
        retry = self.read('handlers/sync/retry.py')
        self.assertIn('with self.fixed_drive_scope(target_key) as active:', retry)
        self.assertNotIn('跨盘历史的目标网盘与当前转存网盘不一致', retry)

    def test_helper_owned_115_skips_duplicate_refresh(self):
        self.assertIn('and not self._strm_by_helper', self.read('handlers/sync/history.py'))
        self.assertIn('and not self._strm_by_helper', self.read('handlers/sync/postprocess.py'))
        self.assertIn('self._strm_by_helper = True', self.read('handlers/sync/service.py'))

    def test_relay_drives_join_search_types(self):
        manager = self.read('core/drive_manager.py')
        self.assertIn('relay_keys = self.relay_drive_keys()', manager)
        self.assertIn('*self._plugin._same_cloud_drive_keys, *relay_keys', manager)


if __name__ == '__main__':
    unittest.main()
