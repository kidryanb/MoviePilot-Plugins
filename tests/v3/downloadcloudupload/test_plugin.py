"""Offline tests with MP service boundaries mocked under the production namespace."""

import hashlib
import importlib
import json
import sys
import tempfile
import types
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from typing import Generic, TypeVar

from pydantic import BaseModel


ROOT = Path(__file__).resolve().parents[3]
T = TypeVar('T')


class Response(BaseModel, Generic[T]):
    success: bool
    message: str = ''
    data: T | None = None


class PluginBase:
    """Minimal contract stub, explicitly not a real MP host loading test."""

    def get_data_path(self):
        return self.test_data_path

    def update_config(self, config):
        self.saved_config = config
        return True


def module(name, **attributes):
    result = types.ModuleType(name)
    result.__dict__.update(attributes)
    sys.modules[name] = result
    return result


module('app', __path__=[])
module('app.sdk', __path__=[])
module('app.sdk.plugin', _PluginBase=PluginBase)
module('app.sdk.logging', logger=SimpleNamespace(warning=lambda *a: None))
jobs = []
module('app.sdk.scheduler', add_plugin_once_job=lambda *a, **k: jobs.append((a, k)) or True,
       remove_plugin_once_job=lambda *a: None)
module('app.sdk.services', DownloaderHelper=lambda: None, StorageHelper=lambda: None)
module('app.chain', __path__=[])
module('app.chain.storage', StorageChain=lambda: None)
module('app.schemas', __path__=[])
module('app.schemas.response', Response=Response)
module('fastapi', Depends=lambda function: function)
module('app.api', __path__=[])
module('app.api.endpoints', __path__=[])
module('app.api.endpoints.plugin', get_current_active_superuser=lambda: True)
module('app.plugins', __path__=[str(ROOT / 'plugins.v3')])
module('apscheduler', __path__=[])
module('apscheduler.triggers', __path__=[])
module('apscheduler.triggers.interval', IntervalTrigger=lambda **kw: kw)
plugin = importlib.import_module('app.plugins.downloadcloudupload')
core = importlib.import_module('app.plugins.downloadcloudupload.core')
gateway_module = importlib.import_module('app.plugins.downloadcloudupload.gateway')
cloud_module = importlib.import_module('app.plugins.downloadcloudupload.cloud115')


class FakeGateway:
    """Deterministic remote store that can fail before or after accepting bytes."""

    def __init__(self):
        self.snapshots = {'qb': [], 'tr': []}
        self.members = {}
        self.remote = {}
        self.uploads = []
        self.query_error = False
        self.upload_error = False

    def tasks(self, instance):
        if self.query_error:
            raise core.UploadError('DOWNLOADER_QUERY_FAILED')
        return self.snapshots[instance]

    def files(self, instance, hash_string):
        return self.members[(instance, hash_string)]

    def ready(self, instance, hash_string, name, size, full_path):
        return any(t.hash == hash_string and t.complete for t in self.snapshots[instance])

    def lookup(self, storage, target):
        return self.remote.get((storage, target))

    def folder(self, storage, target, root):
        return storage, target

    def upload(self, folder, path, name):
        storage, parent = folder
        self.uploads.append((folder, name))
        if self.upload_error:
            raise core.UploadError('UPLOAD_RESULT_UNKNOWN')
        size = path.stat().st_size
        receipt = {'id': f'file-{len(self.uploads)}', 'size': size}
        self.remote[(storage, parent + '/' + name)] = {**receipt, 'confirmed': True}
        return receipt


class QueueTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.local = self.root / 'downloads'
        self.local.mkdir()
        self.gateway = FakeGateway()
        self.store = core.Store(self.root / 'queue.sqlite')
        self.rules = core.validate_rules([{'name': '视频', 'source': '/data', 'local': str(self.local),
                                         'storage': '115网盘Plus', 'target': '/影视', 'instance': '', 'enabled': True}])
        self.engine = core.Engine(self.store, self.gateway, self.rules, ['qb', 'tr'], stable_seconds=0)

    def tearDown(self):
        self.temp.cleanup()

    def add(self, instance='qb', hash_string='hash', complete=True, name='剧/第01集.mkv', payload=b'video'):
        path = self.local.joinpath(*Path(name).parts)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(payload)
        task = core.Torrent(instance, hash_string, '某剧', '/data', complete, 1000)
        self.gateway.snapshots[instance].append(task)
        self.gateway.members[(instance, hash_string)] = [core.TorrentFile(name, len(payload), True, complete)]
        return task, path

    def enqueue(self, **kwargs):
        self.engine.scan()  # Baseline an initially empty list.
        task, path = self.add(**kwargs)
        self.engine.scan()
        return task, path

    def advance(self):
        for _ in range(3):
            self.engine.process(budget=100)

    def files(self):
        return self.store.rows('SELECT * FROM files ORDER BY id')

    def test_both_downloaders_copy_and_preserve_source(self):
        self.engine.scan()
        _, qb_path = self.add('qb', 'qbhash')
        _, tr_path = self.add('tr', 'trhash', name='剧/第02集.mkv')
        self.engine.scan()
        self.advance()
        self.assertEqual([row['state'] for row in self.files()], ['success', 'success'])
        self.assertEqual(qb_path.read_bytes(), b'video')
        self.assertEqual(tr_path.read_bytes(), b'video')
        self.assertEqual(len(self.gateway.uploads), 2)

    def test_old_completed_baseline_newly_completed_is_uploaded(self):
        self.add(hash_string='old')
        task, _ = self.add(hash_string='new', complete=False, name='new.mkv')
        self.engine.scan()
        self.assertEqual(self.files(), [])
        self.gateway.snapshots['qb'][1] = core.Torrent('qb', 'new', '某剧', '/data', True)
        self.gateway.members[('qb', 'new')] = [core.TorrentFile('new.mkv', 5, True, True)]
        self.engine.scan()
        self.advance()
        self.assertEqual(len(self.files()), 1)
        self.assertEqual(self.files()[0]['state'], 'success')

    def test_query_failure_does_not_make_empty_baseline(self):
        self.gateway.query_error = True
        self.engine.scan()
        self.assertIsNone(self.store.meta('baseline:qb'))
        self.gateway.query_error = False
        self.add()
        self.engine.scan()
        self.assertEqual(self.files(), [])

    def test_preview_is_read_only_and_selected_backfill_works(self):
        task, _ = self.add()
        preview = self.engine.scan(backfill='preview')
        self.assertIsNone(self.store.meta('baseline:qb'))
        self.assertEqual(len(preview), 1)
        self.assertEqual(self.files(), [])
        self.engine.scan(backfill=[task.key])
        self.advance()
        self.assertEqual(self.files()[0]['state'], 'success')

    def test_incomplete_member_prevents_upload(self):
        task, _ = self.enqueue()
        self.gateway.members[('qb', 'hash')] = [core.TorrentFile('剧/第01集.mkv', 5, True, False)]
        with patch.object(self.gateway, 'ready', return_value=False):
            self.advance()
        self.assertEqual(self.gateway.uploads, [])

    def test_repeated_scan_and_restart_do_not_duplicate_uploads(self):
        self.enqueue()
        self.advance()
        self.engine.scan()
        self.engine = core.Engine(core.Store(self.store.path), self.gateway, self.rules, ['qb', 'tr'], stable_seconds=0)
        self.engine.scan()
        self.advance()
        self.assertEqual(len(self.files()), 1)
        self.assertEqual(len(self.gateway.uploads), 1)

    def test_same_name_and_size_without_proof_is_conflict(self):
        self.enqueue()
        self.gateway.remote[('115网盘Plus', '/影视/剧/第01集.mkv')] = {'id': 'someone-else', 'size': 5, 'confirmed': True}
        self.advance()
        self.assertEqual(self.files()[0]['state'], 'conflict')
        self.assertEqual(self.gateway.uploads, [])

    def test_cross_downloader_content_and_receipt_deduplication(self):
        self.enqueue()
        self.advance()
        self.add('tr', 'different-hash')
        self.engine.scan()
        self.advance()
        self.assertEqual(len(self.gateway.uploads), 1)
        self.assertEqual(self.files()[1]['state'], 'already_exists')

    def test_uncertain_upload_is_reconciled_without_second_write(self):
        self.enqueue()
        self.gateway.upload_error = True
        self.advance()
        self.assertEqual(len(self.gateway.uploads), 1)
        with self.store.connect() as db:
            db.execute('UPDATE files SET next_at=0')
        self.gateway.upload_error = False
        self.advance()
        self.assertEqual(len(self.gateway.uploads), 1)
        self.assertEqual(self.files()[0]['state'], 'verifying')

    def test_explicit_absent_restart_and_existing_remote_refusal(self):
        self.enqueue()
        self.gateway.upload_error = True
        self.advance()
        row = self.files()[0]
        self.gateway.remote[('115网盘Plus', '/影视/剧/第01集.mkv')] = {'size': 5, 'id': 'other'}
        self.assertEqual(self.engine.action([row['id']], 'restart'), 0)
        self.gateway.remote.clear()
        self.assertEqual(self.engine.action([row['id']], 'restart'), 1)
        self.gateway.upload_error = False
        self.advance()
        self.assertEqual(self.files()[0]['state'], 'success')
        self.assertEqual(len(self.gateway.uploads), 2)

    def test_source_missing_retries_are_bounded(self):
        _, path = self.enqueue()
        path.unlink()
        for _ in range(7):
            with self.store.connect() as db:
                db.execute('UPDATE files SET next_at=0')
            self.engine.process()
        self.assertEqual(self.files()[0]['state'], 'failed')
        self.assertEqual(self.gateway.uploads, [])

    def test_recover_uploaded_receipt_after_worker_crash(self):
        self.enqueue()
        self.engine.process()
        self.engine.process()
        self.assertEqual(self.files()[0]['state'], 'verifying')
        self.engine = core.Engine(core.Store(self.store.path), self.gateway, self.rules, ['qb'], stable_seconds=0)
        self.advance()
        self.assertEqual(self.files()[0]['state'], 'success')
        self.assertEqual(len(self.gateway.uploads), 1)

    def test_configuration_change_preserves_queued_target(self):
        self.enqueue()
        changed = [{**self.rules[0], 'target': '/新文件夹'}]
        self.engine.rules = changed
        self.engine.scan()
        self.advance()
        self.assertIn(('115网盘Plus', '/影视/剧/第01集.mkv'), self.gateway.remote)

    def test_mapping_component_boundary_and_longest_prefix(self):
        nested = {**self.rules[0], 'source': '/data/tv', 'target': '/电视'}
        rules = core.validate_rules([self.rules[0], nested])
        result = core.map_file('qb', '/data/tv/episode.mkv', rules)
        self.assertEqual(result['target'], '/电视/episode.mkv')
        self.assertEqual(core.map_file('qb', '/data/tv-old/episode.mkv', rules)['target'], '/影视/tv-old/episode.mkv')
        self.assertIsNone(core.map_file('qb', '/data-old/test.mkv', rules))

    def test_windows_downloader_to_host_path(self):
        rule = {**self.rules[0], 'source': 'D:\\Downloads'}
        result = core.map_file('qb', 'D:\\Downloads\\剧\\第01集.mkv', [rule])
        self.assertEqual(result['target'], '/影视/剧/第01集.mkv')

    def test_duplicate_and_escaping_paths_rejected(self):
        with self.assertRaises(ValueError):
            core.validate_rules([self.rules[0], self.rules[0]])
        with self.assertRaises(ValueError):
            core.map_file('qb', '/data/../secret.mkv', self.rules)

    def test_partial_selection_uploads_only_selected_files(self):
        task, _ = self.enqueue()
        self.gateway.members[('qb', 'hash')].append(core.TorrentFile('not-selected.mkv', 123, False, False))
        self.engine.scan()
        self.advance()
        self.assertEqual(len(self.files()), 1)

    def test_file_identity_change_after_upload_is_not_retransmitted(self):
        _, path = self.enqueue()
        self.engine.process()
        self.engine.process()
        path.write_bytes(b'other')
        self.advance()
        self.assertEqual(self.files()[0]['state'], 'failed')
        self.assertEqual(len(self.gateway.uploads), 1)

    def test_instance_query_failure_isolated(self):
        self.engine.scan()
        self.add('tr')
        original = self.gateway.tasks
        def tasks(instance):
            if instance == 'qb':
                raise core.UploadError('DOWNLOADER_QUERY_FAILED')
            return original(instance)
        self.gateway.tasks = tasks
        self.engine.scan()
        self.advance()
        self.assertEqual(self.files()[0]['state'], 'success')

    def test_hot_reload_worker_lease(self):
        with core.worker_lease(self.root / 'worker.lock') as first:
            with core.worker_lease(self.root / 'worker.lock') as second:
                self.assertTrue(first)
                self.assertFalse(second)
        with core.worker_lease(self.root / 'worker.lock') as recovered:
            self.assertTrue(recovered)


class DirectCloudTest(unittest.TestCase):
    def setUp(self):
        self.items = {'0': [{'cid': '10', 'pid': '0', 'n': '影视'}], '10': []}
        self.created = []
        self.uploaded = []
        def listing(payload, **kwargs):
            parent = str(payload['cid'])
            return {'state': True, 'path': [{'cid': parent}], 'offset': payload['offset'],
                    'count': len(self.items[parent]), 'data': self.items[parent]}
        def mkdir(payload, **kwargs):
            identifier = str(20 + len(self.created))
            self.items[str(payload['pid'])].append({'cid': identifier, 'pid': str(payload['pid']), 'n': payload['cname']})
            self.items[identifier] = []
            self.created.append(payload)
            return {'state': True}
        def upload(**kwargs):
            self.uploaded.append(kwargs)
            self.items[str(kwargs['pid'])].append({'fid': '90', 'cid': str(kwargs['pid']),
                          'n': kwargs['filename'], 's': kwargs['filesize'], 'pc': 'pickcode'})
            return {'state': True, 'data': {'pickcode': 'pickcode'}}
        self.client = SimpleNamespace(fs_files=listing, fs_mkdir=mkdir, upload_file=upload)
        self.cloud = cloud_module.Cloud115()
        self.cloud.client = lambda: (self.client, 'account')

    def test_cookie_read_uses_documented_field_and_does_not_write(self):
        config = {'cookies': 'UID=123_A1_token; CID=private; SEID=secret', 'enabled': False}
        original = dict(config)
        manager = SimpleNamespace(get_plugin_config=lambda pid: config if pid == 'P115StrmHelper' else {})
        with patch.object(sys.modules['app.sdk.plugin'], 'PluginManager', lambda: manager, create=True):
            cookie, account = cloud_module.strm_cookie()
        self.assertEqual(config, original)
        self.assertEqual(account, '123')
        self.assertEqual(cookie, config['cookies'])

    def test_missing_cookie_and_no_automatic_login(self):
        manager = SimpleNamespace(get_plugin_config=lambda pid: {'cookies': ''})
        with patch.object(sys.modules['app.sdk.plugin'], 'PluginManager', lambda: manager, create=True):
            with self.assertRaisesRegex(core.UploadError, '115_STRM_COOKIE_REQUIRED'):
                cloud_module.strm_cookie()
        calls = []
        def factory(cookies=None, app='', app_id=0, console_qrcode=True):
            calls.append(cookies)
            return self.client
        with patch.object(cloud_module, 'strm_cookie', return_value=('UID=123; CID=c; SEID=s', '123')):
            with patch.dict(sys.modules, {'p115client': SimpleNamespace(P115Client=factory)}):
                client, account = cloud_module.Cloud115().client()
        self.assertEqual(calls, ['UID=123; CID=c; SEID=s'])
        self.assertIs(client, self.client)
        self.assertEqual(account, '123')

    def test_missing_cookie_never_constructs_client(self):
        with patch.object(cloud_module, 'strm_cookie', side_effect=core.UploadError('115_STRM_COOKIE_REQUIRED', False)):
            with patch.dict(sys.modules, {'p115client': SimpleNamespace(P115Client=None)}):
                with self.assertRaisesRegex(core.UploadError, '115_STRM_COOKIE_REQUIRED'):
                    cloud_module.Cloud115().client()

    def test_query_failure_and_parent_fallback_are_not_absence(self):
        for response in [{'state': False}, {'state': True, 'path': [{'cid': '0'}], 'data': [], 'count': 0, 'offset': 0}]:
            self.client.fs_files = lambda payload, **k: ({'state': True, 'path': [{'cid': '0'}],
                'data': self.items['0'], 'count': 1, 'offset': 0} if str(payload['cid']) == '0' else response)
            with self.assertRaises(core.UploadError):
                self.cloud.resolve(self.client, '/影视/video.mkv')

    def test_incomplete_list_rejected(self):
        self.client.fs_files = lambda *a, **k: {'state': True, 'path': [{'cid': '0'}], 'data': [], 'count': 2, 'offset': 0}
        with self.assertRaisesRegex(core.UploadError, '115_INCOMPLETE_FILE_LIST'):
            self.cloud.lookup('/missing.mkv')

    def test_lookup_checks_later_pages(self):
        values = [{'fid': str(i + 1), 'cid': '0', 'n': f'{i}.mkv', 's': 5} for i in range(1001)]
        values[-1]['n'] = 'last.mkv'
        offsets = []
        def listing(payload, **kwargs):
            offset = payload['offset']
            offsets.append(offset)
            return {'state': True, 'path': [{'cid': '0'}], 'count': len(values),
                    'offset': offset, 'data': values[offset:offset + 1000]}
        self.client.fs_files = listing
        self.assertEqual(self.cloud.lookup('/last.mkv')['id'], 'account:1001')
        self.assertEqual(offsets, [0, 1000])

    def test_same_name_ambiguity_blocks_upload(self):
        self.items['10'] = [{'fid': fid, 'cid': '10', 'n': 'video.mkv', 's': 5} for fid in ['1', '2']]
        with self.assertRaisesRegex(core.UploadError, '115_AMBIGUOUS_PATH'):
            self.cloud.lookup('/影视/video.mkv')
        self.assertEqual(self.uploaded, [])

    def test_create_only_descendants_of_existing_root(self):
        with self.assertRaisesRegex(core.UploadError, 'CONFIGURED_ROOT_MISSING'):
            self.cloud.folder('/missing/sub', '/missing')
        self.assertEqual(self.created, [])
        folder = self.cloud.folder('/影视/剧集/第一季', '/影视')
        self.assertEqual(len(self.created), 2)
        self.assertEqual(folder['account'], 'account')

    def test_direct_upload_receipt_and_account_scoped_lookup(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'video.mkv'
            path.write_bytes(b'video')
            folder = self.cloud.folder('/影视', '/影视')
            receipt = self.cloud.upload(folder, path, 'video.mkv')
            remote = self.cloud.lookup('/影视/video.mkv')
        self.assertEqual(receipt['id'], 'account:pickcode')
        self.assertIn(receipt['id'], remote['ids'])
        self.assertEqual(receipt['size'], remote['size'])
        self.assertEqual(self.uploaded[0]['filename'], 'video.mkv')

    def test_no_qr_or_storage_configuration_and_no_cookie_in_form(self):
        instance = plugin.DownloadCloudUpload()
        with patch.object(plugin, 'strm_cookie', return_value=('UID=private; CID=secret; SEID=secret', 'private')):
            form, defaults = instance.get_form()
        self.assertNotIn('secret', json.dumps(form))
        self.assertNotIn('rule_storage', defaults)
        self.assertFalse(any('/115/' in route['path'] for route in instance.get_api()))
        self.assertNotIn('扫码', json.dumps(instance.get_page(), ensure_ascii=False))

    def test_browse_only_folders_and_empty_directory_without_writes(self):
        self.items['0'].append({'fid': '90', 'cid': '0', 'n': 'video.mkv', 's': 5})
        self.assertEqual(self.cloud.browse('/'), {'path': '/', 'parent': '/',
                         'folders': [{'name': '影视', 'path': '/影视'}]})
        self.assertEqual(self.cloud.browse('/影视')['folders'], [])
        self.assertEqual(self.created, [])
        self.assertEqual(self.uploaded, [])

    def test_browse_missing_file_traversal_and_ambiguous_directory(self):
        self.items['0'].append({'fid': '90', 'cid': '0', 'n': 'video.mkv', 's': 5})
        for path in ['/missing', '/video.mkv', '/../影视', '影视']:
            with self.assertRaises(core.UploadError):
                self.cloud.browse(path)
        self.items['0'].append({'cid': '11', 'pid': '0', 'n': '影视'})
        with self.assertRaisesRegex(core.UploadError, '115_AMBIGUOUS_PATH'):
            self.cloud.browse('/')

    def test_folder_api_hides_raw_service_errors(self):
        with patch.object(plugin.Cloud115, 'browse', side_effect=RuntimeError('COOKIE_SECRET')):
            result = plugin.DownloadCloudUpload().api_folders(plugin.FolderRequest(kind='cloud'))
        self.assertFalse(result.success)
        self.assertNotIn('COOKIE_SECRET', result.model_dump_json())


class BoundaryTest(unittest.TestCase):
    def test_local_folder_browser_uses_host_and_filters_files(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / '影视').mkdir()
            (root / 'video.mkv').write_bytes(b'video')
            result = plugin.DownloadCloudUpload().api_folders(plugin.FolderRequest(kind='local', path=str(root)))
            self.assertTrue(result.success)
            self.assertEqual(result.data.path, str(root.resolve()))
            self.assertEqual([entry.name for entry in result.data.folders], ['影视'])
            empty = plugin.local_folders(str(root / '影视'))
            self.assertEqual(empty['folders'], [])
            for invalid in [str(root / 'missing'), str(root / 'video.mkv'), 'relative', str(root / '..')]:
                self.assertFalse(plugin.DownloadCloudUpload().api_folders(plugin.FolderRequest(kind='local', path=invalid)).success)

    def test_picker_transient_state_removed_on_save(self):
        with tempfile.TemporaryDirectory() as directory:
            instance = plugin.DownloadCloudUpload()
            instance.test_data_path = Path(directory)
            with patch.object(plugin, 'MPGateway'):
                instance.init_plugin({'enabled': False, 'picker_open': True, 'picker_items': [{'name': 'private'}]})
            self.assertFalse(any(key.startswith('picker_') for key in instance.saved_config))

    def test_disabled_initialization_has_no_polling_service(self):
        instance = plugin.DownloadCloudUpload()
        with tempfile.TemporaryDirectory() as folder:
            instance.test_data_path = Path(folder)
            with patch.object(plugin, 'MPGateway'):
                instance.init_plugin({'enabled': False})
            self.assertEqual(instance._error, '')
            self.assertFalse(instance.get_state())
            self.assertEqual(instance.get_service(), [])
            self.assertFalse(instance.api_check().success)

    def test_enabled_without_rules_rejected(self):
        instance = plugin.DownloadCloudUpload()
        with tempfile.TemporaryDirectory() as folder:
            instance.test_data_path = Path(folder)
            with patch.object(plugin, 'MPGateway'):
                instance.init_plugin({'enabled': True, 'downloaders': ['qb']})
            self.assertFalse(instance.get_state())
            self.assertIn('文件夹规则', instance._error)

    def test_unknown_file_selection_rejected(self):
        for kind in ['qbittorrent', 'transmission']:
            with self.assertRaises(core.UploadError):
                gateway_module.normalize_member(kind, {'name': 'video.mkv', 'size': 20})

    def test_qb_complete_pause_and_missing_counters(self):
        raw = {'hash': 'hash', 'name': 'video', 'save_path': '/data', 'state': 'pausedUP', 'amount_left': 0, 'progress': 1}
        self.assertTrue(gateway_module.normalize_task('qb', 'qbittorrent', raw).complete)
        for state in ['checkingUP', 'moving', 'error', 'pausedDL']:
            self.assertFalse(gateway_module.normalize_task('qb', 'qbittorrent', {**raw, 'state': state}).complete)
        raw.pop('amount_left')
        self.assertFalse(gateway_module.normalize_task('qb', 'qbittorrent', raw).complete)

    def test_transmission_complete_stopped_and_selected_members(self):
        raw = SimpleNamespace(hashString='hash', name='video', download_dir='/data', status='stopped', left_until_done=0, error=0)
        self.assertTrue(gateway_module.normalize_task('tr', 'transmission', raw).complete)
        raw.left_until_done = 1
        self.assertFalse(gateway_module.normalize_task('tr', 'transmission', raw).complete)
        member = gateway_module.normalize_member('transmission', SimpleNamespace(name='file.mkv', size=20, completed=20, selected=True))
        self.assertTrue(member.complete)
        self.assertTrue(member.selected)

    def test_public_plugin_contract_and_version(self):
        instance = plugin.DownloadCloudUpload()
        with patch.object(plugin, 'MPGateway') as factory:
            factory.return_value.services.return_value = {'qb': None, 'tr': None}
            factory.return_value.storage_options.return_value = [{'title': '115', 'value': '115网盘Plus'}]
            form, defaults = instance.get_form()
        self.assertEqual(defaults['downloaders'], [])
        self.assertIn('rules', defaults)
        json.dumps(form, ensure_ascii=False)
        routes = instance.get_api()
        self.assertTrue(all(route['auth'] == 'bear' and route.get('response_model') for route in routes))
        self.assertTrue(all(route['dependencies'] for route in routes))
        metadata = json.loads((ROOT / 'package.v3.json').read_text(encoding='utf-8'))
        self.assertEqual(metadata['DownloadCloudUpload']['version'], instance.plugin_version)


if __name__ == '__main__':
    unittest.main()
