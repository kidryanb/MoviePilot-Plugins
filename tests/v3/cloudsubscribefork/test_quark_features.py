"""夸克补齐的能力：按 md5/fid 校验改名、缓存统计与清理。"""

import ast
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List, Optional

ROOT = Path(__file__).resolve().parents[3]
PLUGIN_DIR = ROOT / 'plugins.v3' / 'cloudsubscribefork'


def _source(relative_path):
    return (PLUGIN_DIR / relative_path).read_text(encoding='utf-8')


def _extract(relative_path, class_name, names, namespace=None):
    tree = ast.parse(_source(relative_path))
    cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == class_name)
    funcs = [n for n in cls.body if isinstance(n, ast.FunctionDef) and n.name in names]
    for func in funcs:
        func.decorator_list = []
    ns = {'Any': Any, 'Dict': Dict, 'List': List, 'Optional': Optional, 'time': time,
          'logger': SimpleNamespace(info=lambda *a: None, warning=lambda *a: None,
                                    error=lambda *a: None, debug=lambda *a: None)}
    ns.update(namespace or {})
    exec(compile(ast.Module(body=funcs, type_ignores=[]), relative_path, 'exec'), ns)
    return {name: ns[name] for name in names}


class FakeFiles:
    CHECKSUM_ALGORITHMS = frozenset({'md5', 'fid'})

    def __init__(self, items):
        self.items = items
        self.renamed = []

    def resolve_directory(self, path):
        return SimpleNamespace(checked=True, directory_id='dir1')

    def _invalidate_directory_cache(self):
        pass

    def _list(self, directory_id):
        return list(self.items)

    def rename_file(self, path, item, target_name):
        self.renamed.append((item.id, target_name))
        return True


def _file(fid, name, md5='', is_directory=False):
    return SimpleNamespace(id=fid, name=name, md5=md5, is_directory=is_directory)


class ChecksumRenameTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.rename = staticmethod(_extract(
            'drive/quark/files.py', 'QuarkFileService', ['rename_file_by_checksum'],
        )['rename_file_by_checksum'])

    def test_locate_by_md5(self):
        files = FakeFiles([_file('a', 'x.mkv', 'AB' * 16), _file('b', 'y.mkv', 'cd' * 16)])
        self.assertTrue(self.rename(files, '/tv', 'CD' * 16, 'S01E02.mkv', algorithm='md5'))
        self.assertEqual(files.renamed, [('b', 'S01E02.mkv')])

    def test_locate_by_fid(self):
        files = FakeFiles([_file('a', 'x.mkv'), _file('b', 'y.mkv')])
        self.assertTrue(self.rename(files, '/tv', 'a', 'S01E01.mkv', algorithm='fid'))
        self.assertEqual(files.renamed, [('a', 'S01E01.mkv')])

    def test_already_named_is_success_without_rename(self):
        files = FakeFiles([_file('a', 'S01E01.mkv', 'ab' * 16)])
        self.assertTrue(self.rename(files, '/tv', 'ab' * 16, 'S01E01.mkv'))
        self.assertEqual(files.renamed, [])

    def test_sha1_is_rejected(self):
        with self.assertRaises(ValueError):
            self.rename(FakeFiles([]), '/tv', 'a' * 40, 'x.mkv', algorithm='sha1')

    def test_missing_or_bad_md5(self):
        files = FakeFiles([_file('a', 'x.mkv', 'ab' * 16)])
        self.assertFalse(self.rename(files, '/tv', 'ef' * 16, 'y.mkv'))
        self.assertFalse(self.rename(files, '/tv', 'short', 'y.mkv'))
        self.assertEqual(files.renamed, [])

    def test_directories_are_ignored(self):
        files = FakeFiles([_file('d', 'folder', 'ab' * 16, is_directory=True)])
        self.assertFalse(self.rename(files, '/tv', 'ab' * 16, 'y.mkv'))


class FakeCache:
    def __init__(self, entries):
        self.entries = dict(entries)

    def items(self):
        return list(self.entries.items())

    def clear(self):
        self.entries.clear()


class CacheMaintenanceTest(unittest.TestCase):
    def test_share_cache_stats_and_clear(self):
        funcs = _extract('drive/quark/share.py', 'QuarkShareService',
                         ['get_cache_stats', 'clear_cache'])
        import threading
        share = SimpleNamespace(
            _share_items={'s1': {'f1': {}, 'f2': {}}, 's2': {'f3': {}}},
            _share_tokens=FakeCache({'k': 't'}),
            _share_items_lock=threading.RLock(),
        )
        share.get_cache_stats = lambda: funcs['get_cache_stats'](share)
        self.assertEqual(share.get_cache_stats(),
                         {'share_tokens': 1, 'share_info': 2, 'share_files': 3})
        self.assertEqual(funcs['clear_cache'](share),
                         {'share_tokens': 1, 'share_info': 2, 'share_files': 3})
        self.assertEqual(share._share_items, {})
        self.assertEqual(share._share_tokens.items(), [])

    def test_provider_registers_new_capabilities(self):
        provider = _source('drive/quark/provider.py')
        self.assertIn('CloudDriveCapability.CHECKSUM_RENAME: files', provider)
        self.assertIn('CloudDriveCapability.CACHE_MAINTENANCE: QuarkCacheMaintenance(files, share)', provider)

    def test_platform_covers_every_logged_in_drive(self):
        platform = _source('core/services/platform.py')
        self.assertIn('def _cache_drive_providers', platform)
        self.assertIn('registry.available()', platform)
        self.assertIn('self._cache_drive_providers()', _source('core/api/runtime.py'))


if __name__ == '__main__':
    unittest.main()
