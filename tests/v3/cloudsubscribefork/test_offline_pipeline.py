"""离线任务链路回归：提交作用域、任务 ID、轮询超时、递增复查、黑名单与手动重试。

从 service.py / postprocess.py 按 AST 取出相关方法，配最小替身执行，不导入宿主、不访问网络。
"""

import ast
import contextlib
import hashlib
import importlib.util
import json
import re
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List, Optional, Set, Tuple

ROOT = Path(__file__).resolve().parents[3]
PLUGIN_DIR = ROOT / 'plugins.v3' / 'cloudsubscribefork'

_SILENT = SimpleNamespace(
    info=lambda *a, **k: None, warning=lambda *a, **k: None,
    error=lambda *a, **k: None, debug=lambda *a, **k: None,
)


def _extract_magnet_hash(text):
    match = re.search(r'btih:([0-9a-fA-F]{40}|[2-7a-zA-Z]{32})', str(text or ''))
    return match.group(1).upper() if match else None


def _methods(relative_path, class_name, wanted):
    tree = ast.parse((PLUGIN_DIR / relative_path).read_text(encoding='utf-8'))
    found = {}
    for cls in tree.body:
        if not (isinstance(cls, ast.ClassDef) and cls.name == class_name):
            continue
        for node in cls.body:
            if isinstance(node, ast.FunctionDef) and node.name in wanted:
                node.decorator_list = []
                namespace = {
                    'Any': Any, 'Dict': Dict, 'List': List, 'Optional': Optional,
                    'Set': Set, 'Tuple': Tuple, 're': re, 'time': time,
                    'hashlib': hashlib, 'logger': _SILENT,
                    'extract_magnet_hash': _extract_magnet_hash,
                    'PostprocessBatchContext': object, 'Path': Path,
                    'DirectoryFileIndex': dict,
                }
                exec(compile(ast.Module(body=[node], type_ignores=[]), relative_path, 'exec'), namespace)
                found[node.name] = namespace[node.name]
    missing = set(wanted) - set(found)
    if missing:
        raise AssertionError(f'{relative_path} 缺少方法：{sorted(missing)}')
    return found


POST = _methods('handlers/sync/postprocess.py', 'PostprocessService', {
    '_poll_offline_ready', '_schedule_finalize_retry', '_cleanup_failed_offline_task',
    '_offline_timeout_seconds', '_lookup_offline_task',
    '_offline_task_still_running', '_offline_item_files_ready',
    '_match_by_name', '_match_by_hash',
})
SYNC = _methods('handlers/sync/service.py', 'SyncHandler', {
    '_normalize_offline_key', '_add_offline_blacklist', '_remove_offline_blacklist',
    '_is_offline_blacklisted', '_submit_offline_packages', '_rollback_offline_submission',
    '_offline_native_task_id', '_offline_hash', '_offline_key_label', '_existing_offline_task',
    '_offline_blacklist_path',
})
P115 = _methods('drive/p115/offline.py', 'OfflineDownloadService', {
    'add_offline_downloads_batch', 'add_offline_download',
})


def _load_blacklist_module():
    spec = importlib.util.spec_from_file_location(
        'csf_offline_blacklist_under_test', PLUGIN_DIR / 'utils' / 'offline_blacklist.py'
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


BLACKLIST = _load_blacklist_module()

HASH = 'A' * 40
MAGNET = f'magnet:?xt=urn:btih:{HASH}&dn=demo'
PENDING_KEY = f'magnet:{HASH}:1'


class Handler:
    """SyncHandler / PostprocessService 的最小替身。"""

    _OFFLINE_CHECK_DELAYS = (10, 20, 40, 60, 120, 300)
    _OFFLINE_TASK_TYPES = {'magnet', 'ed2k', 'offline'}
    _OFFLINE_PENDING_KEY = 'pending_offline_strm'

    def __init__(self, timeout_minutes=30, default_offline=None, drive_offline=None):
        # 与 SyncHandler.__init__ 相同的换算：分钟、10–1440 封顶
        self._OFFLINE_TIMEOUT = max(10, min(int(timeout_minutes or 30), 1440)) * 60
        self._offline_blacklist = {}
        self._offline_pending_lock = threading.Lock()
        self.aborted: List[str] = []
        self.deleted: List[Tuple[str, bool]] = []
        self.completed: List[Dict[str, Any]] = []
        self.file_index: Dict[str, Any] = {}
        self.data: Dict[str, Any] = {}
        self._offline_tasks = SimpleNamespace(
            delete_offline_task=lambda task_id, delete_source_file=False:
            self.deleted.append((task_id, delete_source_file)),
        )
        self._default_offline = default_offline
        self._drive_offline = dict(drive_offline or {})
        self._active_drive = None

    # 作用域：与 DriveScopedAttribute 一致，非默认盘取该盘的能力，否则回默认盘
    @contextlib.contextmanager
    def drive_scope(self, key):
        key = str(key or '').strip().lower() or None
        if key not in self._drive_offline:
            key = None
        previous, self._active_drive = self._active_drive, key
        try:
            yield key
        finally:
            self._active_drive = previous

    @property
    def _offline_download(self):
        if self._active_drive:
            return self._drive_offline[self._active_drive]
        return self._default_offline

    _offline_timeout_seconds = property(POST['_offline_timeout_seconds'])
    _normalize_offline_key = staticmethod(SYNC['_normalize_offline_key'])
    _offline_hash = staticmethod(SYNC['_offline_hash'])
    _offline_task_still_running = staticmethod(POST['_offline_task_still_running'])
    _offline_key_label = classmethod(SYNC['_offline_key_label'])
    _offline_blacklist_path = classmethod(SYNC['_offline_blacklist_path'])
    _OFFLINE_BLACKLIST_FILE = 'offline_blacklist.json'

    def _snapshot(self, ctx, directory):
        return True, self.file_index

    def _task_type(self, item):
        return item.get('task_type')

    def _abort_single_item(self, item, pending_key, reason, ctx):
        self.aborted.append(reason)

    def _update_postprocess_progress(self, *args):
        pass

    def _extract_item_hash(self, item):
        return ''

    def _match_by_name(self, *args):
        return False

    _match_by_hash = _match_by_fuzzy_name = _match_by_season_episode = _match_by_name

    def _get_data(self, key):
        return self.data.get(key)

    def _save_offline_pending(self, pending):
        self.data[self._OFFLINE_PENDING_KEY] = pending

    def _complete_offline_submission(self, context):
        self.completed.append({**context, 'active_drive': self._active_drive})


for _name in ('_poll_offline_ready', '_schedule_finalize_retry',
              '_cleanup_failed_offline_task', '_lookup_offline_task', '_offline_item_files_ready'):
    setattr(Handler, _name, POST[_name])
for _name in ('_add_offline_blacklist', '_remove_offline_blacklist', '_is_offline_blacklisted',
              '_submit_offline_packages', '_rollback_offline_submission', '_offline_native_task_id',
              '_existing_offline_task'):
    setattr(Handler, _name, SYNC[_name])


class RealMatchHandler(Handler):
    """用真实的精确名 / 哈希匹配，验证「目录里有本任务的文件」才算完成。"""

    _match_by_name = staticmethod(POST['_match_by_name'])
    _match_by_hash = staticmethod(POST['_match_by_hash'])


class SeasonIndex(dict):
    """带季集索引的目录快照替身（对应 DirectoryFileIndex.get_by_season_episode）。"""

    def get_by_season_episode(self, season, episode):
        for name, value in self.items():
            match = re.search(r'S(\d{1,2})E(\d{1,3})', name, re.I)
            if match and (int(match.group(1)), int(match.group(2))) == (season, episode):
                return value
        return None


def _running(percent=0.0, file_id=''):
    return {'completed': False, 'failed': False, 'state': 'running',
            'status_text': '下载中', 'percent': percent, 'file_id': file_id}


def _completed(file_id='fid-1'):
    return {'completed': True, 'failed': False, 'state': 'completed',
            'status_text': '已完成', 'percent': 100, 'file_id': file_id}


# 0.5.14 事故现场：同一暂存目录里有特别篇完成任务的文件和更早的「失踪」残留
UNRELATED_FILES = {
    '全职高手 特别篇': {'id': 'dir-sp', 'name': '全职高手 特别篇'},
    '全职高手 特别篇 01.mp4': {'id': 'f-sp1', 'name': '全职高手 特别篇 01.mp4'},
    '失踪.2023.1080p.mkv': {'id': 'f-old', 'name': '失踪.2023.1080p.mkv'},
}


def _ctx(now, task_map=None):
    return SimpleNamespace(now=now, task_map=task_map or {})


def _item(created_at, **overrides):
    item = {
        'pending_key': PENDING_KEY, 'task_type': 'magnet', 'task_id': HASH,
        'share_url': MAGNET, 'staging_dir': '/staging', 'created_at': created_at,
        'check_index': 0,
    }
    item.update(overrides)
    return item


class Offline115:
    def __init__(self, result=True):
        self.calls = []
        self.result = result

    def add_offline_download(self, url, save_path, target_name=None):
        self.calls.append((url, save_path))
        return self.result


class TimeoutTest(unittest.TestCase):
    def test_minutes_are_converted_and_clamped(self):
        self.assertEqual(Handler(30)._offline_timeout_seconds, 1800)
        self.assertEqual(Handler(1)._offline_timeout_seconds, 600)
        self.assertEqual(Handler(99999)._offline_timeout_seconds, 1440 * 60)
        self.assertEqual(Handler(None)._offline_timeout_seconds, 1800)

    def test_timeout_boundary_cleans_task_keeps_files_and_blacklists(self):
        handler, now = Handler(30), 100000.0
        _, ready = handler._poll_offline_ready(_item(now - 1799), PENDING_KEY, _ctx(now))
        self.assertFalse(ready)
        self.assertEqual(handler.aborted, [])
        _, ready = handler._poll_offline_ready(_item(now - 1800), PENDING_KEY, _ctx(now))
        self.assertFalse(ready)
        self.assertEqual(len(handler.aborted), 1)
        self.assertIn('30 分钟', handler.aborted[0])
        self.assertEqual(handler.deleted, [(HASH, False)])
        self.assertTrue(handler._is_offline_blacklisted(share_url=MAGNET))

    def test_retry_delays_grow_and_stop_at_total_timeout(self):
        handler, created = Handler(30), 1000.0
        item, now, steps = _item(created), created, []
        for _ in range(12):
            handler._schedule_finalize_retry(item, now)
            steps.append(item['next_check_at'] - now)
            now = item['next_check_at']
        self.assertEqual(steps[:6], [20, 40, 60, 120, 300, 300])
        self.assertEqual(now, created + 1800)

    def test_magnet_directory_content_alone_is_not_ready(self):
        """0.5.13 误判：暂存目录非空就当磁力完成。现在没有完成任务时，别人的文件不算。"""
        handler, now = RealMatchHandler(), 5000.0
        handler.file_index = {'a.mkv': {'id': 'x', 'name': 'a.mkv'}}
        item = _item(now, staging_name='全职高手 第二季', season=2, target_episodes=list(range(1, 13)))
        _, ready = handler._poll_offline_ready(item, PENDING_KEY, _ctx(now))
        self.assertFalse(ready)
        self.assertNotIn('download_completed_at', item)
        self.assertEqual(handler.deleted, [])
        self.assertEqual(handler.aborted, [])


class CompletionCheckTest(unittest.TestCase):
    """第 1/3 条：只有网盘任务 completed，或目录里有本任务的文件才算完成。"""

    def _poll(self, handler, item, now, tasks=None):
        return handler._poll_offline_ready(item, PENDING_KEY, _ctx(now, tasks))

    def _s02_item(self, created_at, **extra):
        return _item(created_at, staging_name='全职高手 第二季', file_name='全职高手 第二季',
                     season=2, target_episodes=list(range(1, 13)), **extra)

    def test_running_zero_percent_with_unrelated_files_is_not_ready(self):
        handler, now = RealMatchHandler(), 5000.0
        handler.file_index = SeasonIndex(UNRELATED_FILES)
        item = self._s02_item(now - 60)
        _, ready = self._poll(handler, item, now, {HASH: _running()})
        self.assertFalse(ready)
        self.assertNotIn('download_completed_at', item)
        self.assertEqual(handler.deleted, [])
        self.assertEqual(handler.aborted, [])
        self.assertIn('next_check_at', item)

    def test_running_task_wins_even_if_directory_has_matching_name(self):
        """任务还在跑（0%、没有 file_id）时，目录里同名文件也不能提前判完成。"""
        handler, now = RealMatchHandler(), 5000.0
        handler.file_index = SeasonIndex({'全职高手 第二季': {'id': 'd', 'name': '全职高手 第二季'}})
        item = self._s02_item(now - 60)
        _, ready = self._poll(handler, item, now, {HASH: _running()})
        self.assertFalse(ready)
        self.assertNotIn('download_completed_at', item)

    def test_running_task_past_timeout_is_deleted_and_aborted(self):
        handler, now = RealMatchHandler(30), 100000.0
        handler.file_index = SeasonIndex(UNRELATED_FILES)
        item = self._s02_item(now - 1800)
        _, ready = self._poll(handler, item, now, {HASH: _running()})
        self.assertFalse(ready)
        self.assertNotIn('download_completed_at', item)
        self.assertEqual(handler.deleted, [(HASH, False)])
        self.assertEqual(len(handler.aborted), 1)
        self.assertTrue(handler._is_offline_blacklisted(share_url=MAGNET))

    def test_legacy_false_completion_is_still_cleaned_on_timeout(self):
        """0.5.13 已误写 download_completed_at 的记录：升级后超时仍会删掉 115 上 0% 的任务。"""
        handler, now = RealMatchHandler(30), 100000.0
        handler.file_index = SeasonIndex(UNRELATED_FILES)
        item = self._s02_item(now - 11 * 3600, download_completed_at=now - 11 * 3600)
        _, ready = self._poll(handler, item, now, {HASH: _running()})
        self.assertFalse(ready)
        self.assertEqual(handler.deleted, [(HASH, False)])
        self.assertEqual(len(handler.aborted), 1)

    def test_completed_task_is_ready(self):
        handler, now = RealMatchHandler(), 5000.0
        item = self._s02_item(now - 60)
        _, ready = self._poll(handler, item, now, {HASH: _completed()})
        self.assertTrue(ready)
        self.assertEqual(item['download_completed_at'], now)
        self.assertEqual(item['download_verified'], 'task')

    def test_task_missing_but_file_id_matches_is_ready(self):
        handler, now = RealMatchHandler(), 5000.0
        handler.file_index = SeasonIndex({**UNRELATED_FILES, '全职高手S02': {'id': 'fid-9', 'name': '全职高手S02'}})
        item = self._s02_item(now - 60, file_id='fid-9')
        _, ready = self._poll(handler, item, now, {})
        self.assertTrue(ready)
        self.assertEqual(item['download_verified'], 'files')

    def test_task_missing_but_exact_staging_name_is_ready(self):
        handler, now = RealMatchHandler(), 5000.0
        handler.file_index = SeasonIndex({**UNRELATED_FILES, '全职高手 第二季': {'id': 'd2', 'name': '全职高手 第二季'}})
        _, ready = self._poll(handler, self._s02_item(now - 60), now, {})
        self.assertTrue(ready)

    def test_task_missing_but_target_episode_file_is_ready(self):
        handler, now = RealMatchHandler(), 5000.0
        handler.file_index = SeasonIndex({**UNRELATED_FILES, '全职高手S02E01.mp4': {'id': 'e1', 'name': '全职高手S02E01.mp4'}})
        item = _item(now - 60, staging_name='demo', season=2, target_episodes=[1, 2])
        _, ready = self._poll(handler, item, now, {})
        self.assertTrue(ready)

    def test_task_missing_and_only_other_season_files_is_not_ready(self):
        handler, now = RealMatchHandler(), 5000.0
        handler.file_index = SeasonIndex({**UNRELATED_FILES, '全职高手S03E01.mp4': {'id': 'e', 'name': '全职高手S03E01.mp4'}})
        item = _item(now - 60, staging_name='全职高手', season=2, target_episodes=[1, 2])
        _, ready = self._poll(handler, item, now, {})
        self.assertFalse(ready)
        self.assertNotIn('download_completed_at', item)


class CleanupTest(unittest.TestCase):
    def test_unfinished_task_is_deleted_without_source_files(self):
        handler = Handler()
        handler._cleanup_failed_offline_task(_item(0), '文件名未匹配')
        self.assertEqual(handler.deleted, [(HASH, False)])

    def test_verified_completed_task_is_kept(self):
        handler = Handler()
        item = _item(0, download_completed_at=10.0, download_verified='task')
        handler._cleanup_failed_offline_task(item, 'Magnet 下载完成，但真实文件名未匹配当前订阅')
        self.assertEqual(handler.deleted, [])

    def test_unverified_completion_mark_does_not_block_cleanup(self):
        handler = Handler()
        item = _item(0, download_completed_at=10.0)
        handler._cleanup_failed_offline_task(item, '超时')
        self.assertEqual(handler.deleted, [(HASH, False)])


class TaskIdTest(unittest.TestCase):
    def test_failed_115_task_is_detected_by_info_hash(self):
        handler, now = Handler(), 1000.0
        tasks = {HASH: {'failed': True, 'status_text': '下载失败'}}
        handler._poll_offline_ready(_item(now), PENDING_KEY, _ctx(now, tasks))
        self.assertEqual(len(handler.aborted), 1)
        self.assertEqual(handler.deleted, [(HASH, False)])

    def test_legacy_pending_key_task_id_still_matches(self):
        """0.5.12 及以前在 115 只回 True 时把 pending_key 存成 task_id。"""
        handler, now = Handler(), 1000.0
        legacy = _item(now, task_id=PENDING_KEY)
        self.assertEqual(handler._offline_native_task_id(legacy), HASH)
        tasks = {HASH: {'failed': True, 'status_text': '下载失败'}}
        handler._poll_offline_ready(legacy, PENDING_KEY, _ctx(now, tasks))
        self.assertEqual(len(handler.aborted), 1)
        self.assertEqual(handler.deleted, [(HASH, False)])

    def test_native_task_id_from_provider_is_kept(self):
        handler = Handler()
        self.assertEqual(handler._offline_native_task_id(_item(0, task_id='abc123')), 'ABC123')


class SubmitScopeTest(unittest.TestCase):
    def test_tv_queue_submits_in_recorded_drive_when_default_has_no_offline(self):
        offline = Offline115()
        handler = Handler(default_offline=None, drive_offline={'115': offline})
        handler.data[handler._OFFLINE_PENDING_KEY] = {PENDING_KEY: {'status': 'submitting'}}
        ok = handler._submit_offline_packages([{
            'pending_key': PENDING_KEY, 'drive_key': '115',
            'share_url': MAGNET, 'staging_dir': '/115/转存',
        }])
        self.assertEqual(ok, {PENDING_KEY})
        self.assertEqual(offline.calls, [(MAGNET, '/115/转存')])
        self.assertEqual(handler.completed[0]['active_drive'], '115')
        self.assertFalse(handler._is_offline_blacklisted(share_url=MAGNET))

    def test_missing_offline_capability_rolls_back(self):
        handler = Handler(default_offline=None)
        handler.data[handler._OFFLINE_PENDING_KEY] = {PENDING_KEY: {'status': 'submitting'}}
        ok = handler._submit_offline_packages([{
            'pending_key': PENDING_KEY, 'share_url': MAGNET, 'staging_dir': '/x',
        }])
        self.assertEqual(ok, set())
        self.assertNotIn(PENDING_KEY, handler.data[handler._OFFLINE_PENDING_KEY])
        self.assertTrue(handler._is_offline_blacklisted(share_url=MAGNET))

    def test_items_are_spaced_by_one_and_half_seconds(self):
        offline = Offline115()
        handler = Handler(default_offline=offline)
        slept = []
        original, time.sleep = time.sleep, slept.append
        try:
            ok = handler._submit_offline_packages([
                {'pending_key': 'a', 'share_url': MAGNET, 'staging_dir': '/s'},
                {'pending_key': 'b', 'share_url': MAGNET, 'staging_dir': '/s'},
            ])
        finally:
            time.sleep = original
        self.assertEqual(ok, {'a', 'b'})
        self.assertEqual(slept, [1.5])


class BlacklistTest(unittest.TestCase):
    def test_manual_retry_by_info_hash_clears_magnet_blacklist(self):
        handler = Handler()
        handler._add_offline_blacklist(MAGNET, '超时')
        self.assertTrue(handler._is_offline_blacklisted(share_url=MAGNET))
        handler._remove_offline_blacklist(HASH.lower())
        self.assertFalse(handler._is_offline_blacklisted(share_url=MAGNET))

    def test_manual_retry_clears_ed2k_blacklist(self):
        handler = Handler()
        ed2k_hash = 'B' * 32
        ed2k = f'ed2k://|file|demo.mkv|123|{ed2k_hash}|/'
        handler._add_offline_blacklist(ed2k, '失败')
        handler._remove_offline_blacklist(ed2k_hash)
        self.assertFalse(handler._is_offline_blacklisted(share_url=ed2k))


class Clock:
    def __init__(self, now=1_000_000.0):
        self.now = now

    def __call__(self):
        return self.now


class PersistentBlacklistTest(unittest.TestCase):
    """第 2 条：黑名单写到插件数据目录，新进程 / 新 Handler 读得回来，1 天后过期。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.path = Path(self._tmp.name) / 'offline_blacklist.json'
        self.clock = Clock()

    def tearDown(self):
        self._tmp.cleanup()

    def _handler(self):
        handler = Handler()
        handler._offline_blacklist = BLACKLIST.PersistentTTLStore(
            self.path, ttl=86400, maxsize=10000, clock=self.clock,
        )
        return handler

    def test_survives_new_handler_and_expires_after_one_day(self):
        self._handler()._add_offline_blacklist(MAGNET, '离线下载超过 30 分钟未完成')
        self.assertTrue(self.path.is_file())
        self.assertTrue(self._handler()._is_offline_blacklisted(share_url=MAGNET))
        self.clock.now += 86399
        self.assertTrue(self._handler()._is_offline_blacklisted(share_url=MAGNET))
        self.clock.now += 2
        self.assertFalse(self._handler()._is_offline_blacklisted(share_url=MAGNET))

    def test_manual_retry_clears_memory_and_disk(self):
        first = self._handler()
        first._add_offline_blacklist(MAGNET, '超时')
        second = self._handler()
        second._remove_offline_blacklist(HASH)
        self.assertFalse(first._is_offline_blacklisted(share_url=MAGNET))
        self.assertFalse(self._handler()._is_offline_blacklisted(share_url=MAGNET))

    def test_plugin_data_path_is_used(self):
        plugin = SimpleNamespace(get_data_path=lambda: Path(self._tmp.name))
        self.assertEqual(Handler._offline_blacklist_path(plugin), self.path)
        self.assertIsNone(Handler._offline_blacklist_path(None))

    def test_signed_urls_are_not_written_to_disk(self):
        url = 'https://cdn.example.com/a.torrent?token=SECRET123&sign=abc'
        handler = self._handler()
        handler._add_offline_blacklist(url, '失败')
        self.assertTrue(handler._is_offline_blacklisted(share_url=url))
        text = self.path.read_text(encoding='utf-8')
        self.assertNotIn('SECRET123', text)
        self.assertNotIn('cdn.example.com', text)
        self.assertNotIn('SECRET123', Handler._offline_key_label(url))

    def test_corrupt_file_is_ignored(self):
        self.path.write_text('{not json', encoding='utf-8')
        handler = self._handler()
        self.assertFalse(handler._is_offline_blacklisted(share_url=MAGNET))
        handler._add_offline_blacklist(MAGNET, '失败')
        self.assertIn('magnet:' + HASH, json.loads(self.path.read_text(encoding='utf-8'))['entries'])


class TaskList:
    def __init__(self, tasks):
        self.tasks = tasks

    def get_offline_task_list_snapshot(self, force=False):
        return {'tasks': list(self.tasks), 'refresh_ok': True}

    def delete_offline_task(self, task_id, delete_source_file=False):
        pass


class ExistingTaskSubmitTest(unittest.TestCase):
    """第 2 条：网盘上已有同一 hash 且未完成时，不再提交，也不当成提交成功。"""

    def _submit(self, handler):
        handler.data[handler._OFFLINE_PENDING_KEY] = {PENDING_KEY: {'status': 'submitting'}}
        return handler._submit_offline_packages([{
            'pending_key': PENDING_KEY, 'share_url': MAGNET, 'staging_dir': '/s',
        }])

    def test_unfinished_existing_task_is_not_resubmitted(self):
        offline = Offline115()
        handler = Handler(default_offline=offline)
        handler._offline_tasks = TaskList([{'id': HASH, **_running()}])
        self.assertEqual(self._submit(handler), set())
        self.assertEqual(offline.calls, [])
        self.assertEqual(handler.completed, [])
        self.assertNotIn(PENDING_KEY, handler.data[handler._OFFLINE_PENDING_KEY])

    def test_completed_existing_task_goes_through_normal_dedupe(self):
        offline = Offline115()
        handler = Handler(default_offline=offline)
        handler._offline_tasks = TaskList([{'id': HASH, **_completed()}])
        self.assertEqual(self._submit(handler), {PENDING_KEY})
        self.assertEqual(len(offline.calls), 1)

    def test_manual_retry_allows_resubmit(self):
        offline = Offline115()
        handler = Handler(default_offline=offline)
        handler._offline_tasks = TaskList([])
        handler._add_offline_blacklist(MAGNET, '超时')
        handler._remove_offline_blacklist(HASH)
        self.assertFalse(handler._is_offline_blacklisted(share_url=MAGNET))
        self.assertEqual(self._submit(handler), {PENDING_KEY})


class Fake115Client:
    def __init__(self):
        self.add_calls = []

    def clouddownload_task_add_urls(self, payload, **kwargs):
        self.add_calls.append(payload)
        return {'state': True, 'data': {'result': [{'info_hash': HASH.lower(), 'state': True}]}}


class Fake115Offline:
    """OfflineDownloadService 的最小替身，只保留批量提交用到的成员。"""

    add_offline_downloads_batch = P115['add_offline_downloads_batch']
    add_offline_download = P115['add_offline_download']

    def __init__(self, tasks):
        self.client = Fake115Client()
        self._login_checked = True
        self.is_vip = True
        self._tasks = tasks
        self._offline_task_lock = threading.Lock()
        self._offline_task_cache = []
        self._offline_task_status = {}
        self._offline_task_cache_time = 0
        self._offline_task_cache_revision = 0

    @staticmethod
    def _normalize_hash(value):
        return re.sub(r'[^0-9A-Fa-f]', '', str(value or '')).upper()

    @staticmethod
    def _format_offline_task(task):
        return {'id': str(task.get('info_hash') or '').upper(), 'state': 'running'}

    @staticmethod
    def is_ed2k_url(url):
        return str(url).startswith('ed2k://')

    @staticmethod
    def parse_magnet_link(url):
        info_hash = _extract_magnet_hash(url)
        return {'hash': info_hash, 'name': 'demo', 'url': url, 'size': 0} if info_hash else None

    def get_offline_tasks(self, force=False):
        return [dict(task) for task in self._tasks]

    def get_pid_by_path(self, path, mkdir=False):
        return 1

    def _rate_limited_call(self, func, *args, **kwargs):
        return func(*args, **kwargs)

    def _ios_request_kwargs(self, app=False):
        return {}


class P115BatchTest(unittest.TestCase):
    def test_running_hash_is_not_resubmitted_nor_reported_success(self):
        service = Fake115Offline([{'id': HASH, **_running()}])
        success, failed = service.add_offline_downloads_batch([{'url': MAGNET}], save_path='/s')
        self.assertEqual(success, [])
        self.assertEqual(failed, [HASH])
        self.assertEqual(service.client.add_calls, [])
        self.assertFalse(service.add_offline_download(MAGNET, '/s'))
        self.assertEqual(service.client.add_calls, [])

    def test_completed_hash_is_reused(self):
        service = Fake115Offline([{'id': HASH, **_completed()}])
        success, failed = service.add_offline_downloads_batch([{'url': MAGNET}], save_path='/s')
        self.assertEqual(success, [HASH])
        self.assertEqual(service.client.add_calls, [])

    def test_new_hash_is_submitted(self):
        service = Fake115Offline([])
        success, _ = service.add_offline_downloads_batch([{'url': MAGNET}], save_path='/s')
        self.assertEqual(success, [HASH])
        self.assertEqual(len(service.client.add_calls), 1)


class StaticWiringTest(unittest.TestCase):
    """源码层面的接线：提交上下文带 drive_key，完成记录用链接 hash 作 task_id。"""

    source = (PLUGIN_DIR / 'handlers/sync/service.py').read_text(encoding='utf-8')

    def test_submit_context_and_pending_carry_drive_key(self):
        self.assertIn('drive_key = str(self.scoped_drive_key() or "")', self.source)
        self.assertIn('"drive_key": str(context.get("drive_key") or ""),', self.source)

    def test_pending_task_id_prefers_link_hash(self):
        self.assertIn('or self._offline_hash(share_url)', self.source)


if __name__ == '__main__':
    unittest.main()
