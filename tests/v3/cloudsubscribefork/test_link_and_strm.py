"""0.5.5 修复的离线测试：渠道默认地址、自定义域名接线、STRM 原地写入、删除通知开关。"""

import ast
import importlib.util
import os
import sys
import tempfile
import types
import unittest
from unittest import mock
from enum import Enum
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[3]
PLUGIN_DIR = ROOT / 'plugins.v3' / 'cloudsubscribefork'
PKG = 'csf_link_strm_under_test'


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


def _source(relative_path):
    return (PLUGIN_DIR / relative_path).read_text(encoding='utf-8')


for _name in (PKG, f'{PKG}.utils'):
    _module(_name)
strm = _load(f'{PKG}.utils.strm', 'utils/strm.py')


class StrmWriteTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.cloud = self.root / 'cloud'
        self.library = self.root / 'library'
        self.library.mkdir()

    def tearDown(self):
        self.tmp.cleanup()

    def _write(self, base_url):
        path, _ = strm.StrmGenerator(base_url, '{base_url}{file_path}').write(
            local_root=str(self.cloud), cloud_root='/', cloud_dir='/剧集',
            file_name='S01E01.mkv',
        )
        return path

    def test_unchanged_content_is_not_rewritten(self):
        path = self._write('http://a')
        before = path.stat()
        os.utime(path, (1, 1))
        self.assertFalse(strm.StrmGenerator.write_in_place(path, path.read_text(encoding='utf-8')))
        self.assertEqual(path.stat().st_mtime, 1)
        self.assertEqual(path.stat().st_ino, before.st_ino)

    def test_changed_content_updates_hard_linked_library_copy(self):
        path = self._write('http://old-address')
        linked = self.library / 'S01E01.strm'
        os.link(path, linked)
        inode = path.stat().st_ino
        self._write('http://new')
        self.assertEqual(path.stat().st_ino, inode)
        self.assertEqual(linked.read_text(encoding='utf-8'), path.read_text(encoding='utf-8'))
        self.assertTrue(linked.read_text(encoding='utf-8').startswith('http://new'))
        self.assertNotIn('old-address', linked.read_text(encoding='utf-8'))
        self.assertFalse(list(path.parent.glob('*.tmp')))


class JuyingDefaultTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        for name in (f'{PKG}.search', f'{PKG}.search.juying', 'app', 'app.sdk'):
            _module(name)
        _module('app.sdk.logging', logger=SimpleNamespace(
            info=print, debug=lambda *a, **k: None, warning=print, error=print))
        names = [
            alias.name for node in ast.parse(_source('search/juying/client.py')).body
            if isinstance(node, ast.ImportFrom) and node.module == 'utils.http_client'
            for alias in node.names
        ]
        _module(f'{PKG}.utils.http_client', **{name: mock.MagicMock() for name in names})
        cls.client = _load(f'{PKG}.search.juying.client', 'search/juying/client.py').JuyingClient

    def test_new_default_and_legacy_values(self):
        normalize = self.client.normalize_base_url
        self.assertEqual(self.client.BASE_URL, 'https://www.jying.top')
        self.assertEqual(normalize(''), 'https://www.jying.top')
        self.assertEqual(normalize(None), 'https://www.jying.top')
        self.assertEqual(normalize('https://juying.tv'), 'https://www.jying.top')
        self.assertEqual(normalize('https://www.juying.tv/'), 'https://www.jying.top')
        self.assertEqual(normalize('https://mirror.example.com/'), 'https://mirror.example.com')

    def test_definition_default(self):
        self.assertIn('https://www.jying.top', _source('search/juying/definition.py'))
        self.assertNotIn('juying.tv"', _source('search/juying/definition.py'))


class BaseUrlWiringTest(unittest.TestCase):
    def test_hdhive_and_dian115_pass_configured_base_url(self):
        hdhive = _source('search/hdhive/definition.py')
        self.assertIn('base_url=', hdhive)
        self.assertIn('_hdhive_base_url', hdhive)
        self.assertIn('base_url=', _source('search/hdhive/service.py'))
        dian115 = _source('search/dian115/definition.py')
        self.assertIn('base_url=', dian115)
        self.assertIn('_dian115_base_url', dian115)
        self.assertIn('base_url', _source('search/dian115/service.py'))

    def test_hdhive_web_client_no_longer_hardcodes_host(self):
        client = _source('search/hdhive/web/client.py')
        self.assertIn('self.BASE_URL = self.normalize_base_url(base_url)', client)
        self.assertNotIn('"https://re0.me', client.split('def normalize_base_url', 1)[1])

    def test_placeholders(self):
        self.assertIn('https://hdhaven.com', _source('search/hdhaven/definition.py'))
        self.assertNotIn('hdhaven.org', _source('search/hdhaven/definition.py'))
        self.assertIn('m.dian115.com', _source('search/dian115/definition.py'))
        self.assertNotIn('115.dian.me', _source('search/dian115/definition.py'))


class DeleteNotifySwitchTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        tree = ast.parse(_source('handlers/notification/media_server.py'))
        notifier = next(node for node in tree.body
                        if isinstance(node, ast.ClassDef) and node.name == 'MediaServerNotifier')
        func = next(node for node in notifier.body
                    if isinstance(node, ast.FunctionDef) and node.name == 'notify_deleted_path')
        module = ast.Module(body=[func], type_ignores=[])
        namespace = {
            'Path': Path, 'Dict': dict, 'Any': object, 'SimpleNamespace': SimpleNamespace,
            'MediaType': Enum('MediaType', {'TV': '电视剧', 'MOVIE': '电影'}),
        }
        exec(compile(module, 'media_server.py', 'exec'), namespace)
        cls.func = staticmethod(namespace['notify_deleted_path'])

    def _owner(self, enabled):
        calls = []
        owner = SimpleNamespace(enabled=enabled,
                                notify=lambda *a, **k: calls.append(k) or True)
        return owner, calls

    def test_disabled_switch_sends_nothing(self):
        owner, calls = self._owner(False)
        self.assertTrue(self.func(owner, Path('/media/quark_strm/x'), {'type': '电视剧'}))
        self.assertEqual(calls, [])

    def test_enabled_switch_notifies_without_force(self):
        owner, calls = self._owner(True)
        self.func(owner, Path('/media/x'), {'type': '电视剧', 'file_name': 'a.strm'})
        self.assertEqual(len(calls), 1)
        self.assertTrue(calls[0].get('deleted'))
        self.assertFalse(calls[0].get('force', False))


if __name__ == '__main__':
    unittest.main()
