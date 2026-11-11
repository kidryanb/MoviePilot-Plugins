"""离线任务链路回归：提交作用域、任务 ID、轮询超时、递增复查、黑名单与手动重试。

从 service.py / postprocess.py 按 AST 取出相关方法，配最小替身执行，不导入宿主、不访问网络。
"""

import ast
import contextlib
import hashlib
import re
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
                    'PostprocessBatchContext': object,
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
})
SYNC = _methods('handlers/sync/service.py', 'SyncHandler', {
    '_normalize_offline_key', '_add_offline_blacklist', '_remove_offline_blacklist',
    '_is_offline_blacklisted', '_submit_offline_packages', '_rollback_offline_submission',
    '_offline_native_task_id', '_offline_hash',
})

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
              '_cleanup_failed_offline_task', '_lookup_offline_task'):
    setattr(Handler, _name, POST[_name])
for _name in ('_add_offline_blacklist', '_remove_offline_blacklist', '_is_offline_blacklisted',
              '_submit_offline_packages', '_rollback_offline_submission', '_offline_native_task_id'):
    setattr(Handler, _name, SYNC[_name])


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

    def test_magnet_directory_content_means_ready(self):
        handler, now = Handler(), 5000.0
        handler.file_index = {'a.mkv': {}}
        _, ready = handler._poll_offline_ready(_item(now), PENDING_KEY, _ctx(now))
        self.assertTrue(ready)


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
