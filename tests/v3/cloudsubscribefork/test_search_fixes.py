"""搜索渠道修复的离线测试：大小解析、并发名额、单源代理、属性初始化。"""

import ast
import importlib.util
import sys
import threading
import time
import types
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
PLUGIN_DIR = ROOT / 'plugins.v3' / 'cloudsubscribefork'
PKG = 'csf_search_fixes_under_test'


def _module(name, **attrs):
    module = types.ModuleType(name)
    module.__path__ = []
    module.__dict__.update(attrs)
    sys.modules[name] = module
    return module


def _load(name, relative_path):
    spec = importlib.util.spec_from_file_location(name, PLUGIN_DIR / relative_path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


for _name in (PKG, f'{PKG}.utils', f'{PKG}.search'):
    _module(_name)
_module(f'{PKG}.search.matching', unique_texts=lambda values: list(dict.fromkeys(v for v in values if v)))
_module(f'{PKG}.utils.magnet', parse_magnet_metadata=lambda *a, **k: {})
magnet = _load(f'{PKG}.search.magnet', 'search/magnet.py')
slots_module = _load(f'{PKG}.utils.source_slots', 'utils/source_slots.py')
proxy_scope = _load(f'{PKG}.search.proxy_scope', 'search/proxy_scope.py')

GB = 1024 ** 3


class SizeParsingTest(unittest.TestCase):
    def test_single_letter_units_keep_their_scale(self):
        self.assertEqual(magnet.parse_size_str('58.44G'), int(58.44 * GB))
        self.assertEqual(magnet.parse_size_str('2.1T'), int(2.1 * 1024 * GB))
        self.assertEqual(magnet.parse_size_str('700MiB'), 700 * 1024 ** 2)

    def test_unit_inside_a_title(self):
        self.assertEqual(magnet.parse_size_str('流浪地球 2023 58.44G 4K'), int(58.44 * GB))
        self.assertEqual(magnet.parse_size_str('S01E01 1080p 4.2G'), int(4.2 * GB))

    def test_bare_numbers_in_titles_are_not_sizes(self):
        self.assertEqual(magnet.parse_size_str('现在就出发 2023 E08'), 0)
        self.assertEqual(magnet.parse_size_str('4K HDR'), 0)
        self.assertEqual(magnet.parse_size_str('2160p 60fps'), 0)

    def test_plain_values_still_work(self):
        self.assertEqual(magnet.parse_size_str('1024'), 1024)
        self.assertEqual(magnet.parse_size_str(12345), 12345)
        self.assertEqual(magnet.parse_size_str('12,345 KB'), 12345 * 1024)
        self.assertEqual(magnet.parse_size_str('1.5 GB'), int(1.5 * GB))
        self.assertEqual(magnet.parse_size_str(None), 0)


class SourceSlotsTest(unittest.TestCase):
    def test_abandoned_source_frees_its_slot_for_the_next_one(self):
        slots = slots_module.SourceSlots(1)
        self.assertTrue(slots.acquire('pianku'))
        started = threading.Event()

        def next_source():
            if slots.acquire('pansou', poll=0.05):
                started.set()

        worker = threading.Thread(target=next_source)
        worker.start()
        self.assertFalse(started.wait(0.2))
        slots.abandon('pianku')
        self.assertTrue(started.wait(1))
        worker.join(1)
        # 被放弃的渠道结束时再归还名额也不会多出名额
        slots.release('pianku')
        slots.release('pansou')
        self.assertTrue(slots.acquire('a', poll=0.05))
        self.assertFalse(slots._slots.acquire(blocking=False))

    def test_queued_source_stops_when_round_finishes(self):
        slots = slots_module.SourceSlots(1)
        slots.acquire('busy')
        result = []
        worker = threading.Thread(target=lambda: result.append(slots.acquire('late', poll=0.05)))
        worker.start()
        time.sleep(0.1)
        slots.finish()
        worker.join(1)
        self.assertEqual(result, [False])

    def test_abandoned_before_start_never_starts(self):
        slots = slots_module.SourceSlots(2)
        slots.abandon('uindex')
        self.assertFalse(slots.acquire('uindex', poll=0.05))
        self.assertTrue(slots.acquire('mikan', poll=0.05))


class ProxyScopeTest(unittest.TestCase):
    proxy = 'http://192.168.1.2:7890'

    def test_lan_service_always_goes_direct(self):
        config = {'pansou_url': 'http://192.168.1.10:8888'}
        self.assertIsNone(proxy_scope.source_proxy('pansou', config, self.proxy))
        for url in ('http://localhost:8888', 'http://pansou:8888', 'http://nas.local:8888', 'http://10.0.0.3'):
            self.assertTrue(proxy_scope.is_lan_url(url), url)
        self.assertFalse(proxy_scope.is_lan_url('https://uindex.org'))

    def test_public_sources_use_proxy_by_default(self):
        self.assertEqual(proxy_scope.source_proxy('uindex', {}, self.proxy), self.proxy)
        self.assertEqual(
            proxy_scope.source_proxy('mikan', {'mikan_base_url': 'https://mikanani.me'}, self.proxy),
            self.proxy,
        )

    def test_selected_sources_limit_who_uses_proxy(self):
        config = {'search_proxy_sources': ['uindex', 'mikan']}
        self.assertEqual(proxy_scope.source_proxy('uindex', config, self.proxy), self.proxy)
        self.assertIsNone(proxy_scope.source_proxy('pianku', config, self.proxy))

    def test_no_proxy_configured(self):
        self.assertIsNone(proxy_scope.source_proxy('uindex', {}, ''))

    def test_owner_attributes(self):
        owner = types.SimpleNamespace(
            _search_proxy=self.proxy, _search_proxy_sources=['hdhive'], _hdhive_base_url='https://hdhive.com',
        )
        self.assertEqual(proxy_scope.owner_source_proxy(owner, 'hdhive'), self.proxy)
        self.assertIsNone(proxy_scope.owner_source_proxy(owner, 'dian115'))


class AttributeInitTest(unittest.TestCase):
    def test_search_classes_only_read_attributes_they_set(self):
        """AnimeGarden 曾在 clear_cache 里读未初始化的 _lock / _cache。"""
        problems = []
        for path in sorted((PLUGIN_DIR / 'search').rglob('*.py')):
            tree = ast.parse(path.read_text(encoding='utf-8'))
            for cls in (node for node in tree.body if isinstance(node, ast.ClassDef)):
                if cls.bases:
                    continue
                assigned, read = set(), {}
                members = {
                    item.name for item in cls.body
                    if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef))
                }
                for item in cls.body:
                    targets = item.targets if isinstance(item, ast.Assign) else (
                        [item.target] if isinstance(item, ast.AnnAssign) else [])
                    members.update(t.id for t in targets if isinstance(t, ast.Name))
                for node in ast.walk(cls):
                    if (isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name)
                            and node.value.id == 'self'):
                        if isinstance(node.ctx, ast.Store):
                            assigned.add(node.attr)
                        else:
                            read.setdefault(node.attr, node.lineno)
                for attr, line in read.items():
                    if attr not in assigned and attr not in members:
                        problems.append(f'{path.relative_to(PLUGIN_DIR)}:{line} {cls.name}.{attr}')
        self.assertEqual(problems, [])

    def test_animegarden_clear_cache_uses_its_search_cache(self):
        text = (PLUGIN_DIR / 'search/animegarden/service.py').read_text(encoding='utf-8')
        self.assertIn('self._lock = threading.RLock()', text)
        self.assertIn('self._search_cache.clear()', text)


class WiringTest(unittest.TestCase):
    def test_registry_and_search_use_per_source_proxy_and_slots(self):
        registry = (PLUGIN_DIR / 'search/registry.py').read_text(encoding='utf-8')
        self.assertIn('source_proxy(def_cls.id, config, context.get("proxy"))', registry)
        search = (PLUGIN_DIR / 'handlers/search/service.py').read_text(encoding='utf-8')
        self.assertIn('slots.abandon(source)', search)
        self.assertIn('max_workers=len(ordered_sources)', search)

    def test_pianku_retries_5xx_once(self):
        client = (PLUGIN_DIR / 'search/pianku/client.py').read_text(encoding='utf-8')
        self.assertIn('_RETRY_STATUS = frozenset({500, 502, 503, 504})', client)
        self.assertIn('if status in _RETRY_STATUS and attempt == 0:', client)


if __name__ == '__main__':
    unittest.main()
