"""新入库 STRM 预读媒体信息：事件解析、单条 PlaybackInfo、限速、重试、不刷新媒体库。

不访问网络；Emby 用内存里的假接口代替。
"""

import importlib.util
import logging
import re
import sys
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[3]
PLUGIN_DIR = ROOT / 'plugins.v3' / 'cloudsubscribefork'
_spec = importlib.util.spec_from_file_location(
    'csf_mediainfo_prefill_under_test', PLUGIN_DIR / 'handlers' / 'notification' / 'mediainfo_prefill.py')
prefill = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = prefill
_spec.loader.exec_module(prefill)

API_KEY = 'emby-secret-key-123'
STREAMS = [{'Type': 'Video', 'Codec': 'hevc'}, {'Type': 'Audio', 'Codec': 'eac3'}]


class FakeEmby:
    """只实现预读会用到的接口；记录每个请求。"""

    def __init__(self):
        self.calls = []
        self.items = {}  # id -> dict
        self.series = {}  # tmdb -> series id
        self.probe_fills = True
        self.playback_status = 200

    def add_episode(self, item_id, series_id, path, streams=None, tmdb='100'):
        self.series[tmdb] = series_id
        self.items.setdefault(series_id, {'Id': series_id, 'Type': 'Series', 'Path': '/library/tv/show'})
        self.items[item_id] = {'Id': item_id, 'Type': 'Episode', 'Path': path, 'ParentId': series_id,
                               'MediaStreams': list(streams or [])}

    def __call__(self, method, url, params, headers, timeout):
        self.calls.append((method, url, dict(params), dict(headers)))
        path = '/' + url.split('/emby/', 1)[1]
        if method == 'GET' and path == '/Items':
            if 'Ids' in params:
                item = self.items.get(params['Ids'])
                return 200, {'Items': [dict(item)] if item else []}
            if 'AnyProviderIdEquals' in params:
                sid = self.series.get(params['AnyProviderIdEquals'].split('.', 1)[1])
                return 200, {'Items': [dict(self.items[sid])] if sid else []}
            if 'ParentId' in params:
                return 200, {'Items': [dict(v) for v in self.items.values()
                                       if v.get('ParentId') == params['ParentId']]}
            return 200, {'Items': []}
        match = re.fullmatch(r'/Items/(\w+)/PlaybackInfo', path)
        if method == 'POST' and match:
            item = self.items.get(match.group(1))
            if self.playback_status != 200 or not item:
                return self.playback_status, None
            if self.probe_fills:
                item['MediaStreams'] = list(STREAMS)
            return 200, {'MediaSources': [{'Id': 'src', 'LiveStreamId': 'live-1',
                                           'MediaStreams': list(STREAMS)}]}
        if method == 'POST' and path == '/LiveStreams/Close':
            return 204, None
        return 404, None

    def paths(self, method=None):
        return ['/' + url.split('/emby/', 1)[1] for m, url, _, _ in self.calls if method in (None, m)]


class FakeClock:
    def __init__(self):
        self.now = 1000.0
        self.slept = []

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.slept.append(seconds)
        self.now += seconds


def transfer_event(source, targets, tmdb='100', success=True, as_dict=False):
    if as_dict:
        return {'fileitem': {'path': source},
                'transferinfo': {'success': success, 'file_list_new': targets,
                                 'target_item': {'path': targets[0]} if targets else None},
                'mediainfo': {'tmdb_id': tmdb, 'title': '某剧'}}
    return {
        'fileitem': SimpleNamespace(path=source),
        'transferinfo': SimpleNamespace(success=success, file_list_new=targets,
                                        target_item=SimpleNamespace(path=targets[0]) if targets else None),
        'mediainfo': SimpleNamespace(tmdb_id=int(tmdb), title='某剧'),
    }


class LogCapture(logging.Handler):
    def __init__(self):
        super().__init__()
        self.lines = []

    def emit(self, record):
        self.lines.append(record.getMessage())


def make(emby, clock=None, **kwargs):
    clock = clock or FakeClock()
    log = logging.getLogger(f'prefill-test-{id(emby)}')
    log.setLevel(logging.DEBUG)
    capture = LogCapture()
    log.addHandler(capture)
    server = prefill.EmbyServer(name='emby', host='http://emby:8096', api_key=API_KEY, user_id='u1')
    options = dict(http=emby, servers=lambda: [server], initial_delay=0, logger=log,
                   clock=clock, sleep=clock.sleep)
    options.update(kwargs)
    return prefill.MediaInfoPrefiller(**options), clock, capture


SRC = '/media/quark_strm/某剧/Season 1/某剧 - S01E01.strm'
DST = '/media/library/电视剧/某剧 (2026)/Season 1/某剧 - S01E01 - 第 1 集.strm'


class EventParsingTest(unittest.TestCase):
    def test_strm_target_from_object_and_dict_events(self):
        for as_dict in (False, True):
            targets = prefill.targets_from_transfer_event(transfer_event(SRC, [DST], as_dict=as_dict))
            self.assertEqual([t.path for t in targets], [DST])
            self.assertEqual(targets[0].tmdb_id, '100')

    def test_ignores_non_strm_failed_and_other_roots(self):
        self.assertEqual(prefill.targets_from_transfer_event(
            transfer_event('/downloads/a.mkv', ['/media/library/a.mkv'])), [])
        self.assertEqual(prefill.targets_from_transfer_event(
            transfer_event(SRC, [DST], success=False)), [])
        self.assertEqual(prefill.targets_from_transfer_event(
            transfer_event('/media/p115_strm/x.strm', ['/media/library/x.strm']),
            source_roots=['/media/quark_strm']), [])
        self.assertEqual(len(prefill.targets_from_transfer_event(
            transfer_event(SRC, [DST]), source_roots=['/media/quark_strm/'])), 1)
        self.assertEqual(prefill.targets_from_transfer_event(None), [])

    def test_source_roots_and_mappings(self):
        self.assertEqual(prefill.parse_source_roots('/media/quark_strm/\n\n /a # note\n/a'),
                         ['/media/quark_strm', '/a'])
        mappings = '/mnt/lib#/media/library\n/mnt/lib/tv#/media/library/电视剧'
        self.assertEqual(prefill.map_to_server_path(DST, mappings),
                         '/mnt/lib/tv/某剧 (2026)/Season 1/某剧 - S01E01 - 第 1 集.strm')
        self.assertEqual(prefill.map_to_server_path(DST, ''), DST)


class PrefillTest(unittest.TestCase):
    def setUp(self):
        self.emby = FakeEmby()

    def test_success_calls_single_playbackinfo_and_verifies_streams(self):
        self.emby.add_episode('e1', 's1', DST)
        worker, _, capture = make(self.emby)
        self.assertEqual(worker.enqueue_transfer(transfer_event(SRC, [DST])), 1)
        self.assertEqual(worker.run_due(), 1)
        self.assertEqual(worker.pending(), [])
        self.assertEqual(worker.stats['succeeded'], 1)
        posts = self.emby.paths('POST')
        self.assertEqual(posts, ['/Items/e1/PlaybackInfo', '/LiveStreams/Close'])
        self.assertTrue(self.emby.items['e1']['MediaStreams'])
        # 补完后再按 ID 读一次条目核对 MediaStreams
        self.assertEqual(self.emby.calls[-1][2].get('Ids'), 'e1')
        self.assertTrue(any('媒体信息预读完成' in line for line in capture.lines))

    def test_never_refreshes_library_or_leaks_token(self):
        self.emby.add_episode('e1', 's1', DST)
        worker, _, capture = make(self.emby)
        worker.enqueue_transfer(transfer_event(SRC, [DST]))
        worker.run_due()
        for method, url, params, headers in self.emby.calls:
            self.assertNotRegex(url, r'(?i)refresh|/Library/')
            self.assertNotIn(API_KEY, url)
            self.assertNotIn(API_KEY, str(params))
            self.assertNotIn('ReplaceAllMetadata', params)
            self.assertNotIn('MetadataRefreshMode', params)
            self.assertEqual(headers['X-Emby-Token'], API_KEY)
        self.assertFalse(any(API_KEY in line for line in capture.lines))

    def test_item_with_streams_skips_playbackinfo(self):
        self.emby.add_episode('e1', 's1', DST, streams=STREAMS)
        worker, _, _ = make(self.emby)
        worker.enqueue_transfer(transfer_event(SRC, [DST]))
        worker.run_due()
        self.assertEqual(self.emby.paths('POST'), [])
        self.assertEqual(worker.stats['already'], 1)

    def test_not_indexed_yet_retries_next_round(self):
        worker, clock, _ = make(self.emby, retry_delays=(180, 600))
        worker.enqueue_transfer(transfer_event(SRC, [DST]))
        worker.run_due()
        [target] = worker.pending()
        self.assertEqual(target.attempts, 1)
        self.assertEqual(worker.run_due(), 0)  # 还没到下一轮
        self.emby.add_episode('e1', 's1', DST)
        clock.now += 180
        self.assertEqual(worker.run_due(), 1)
        self.assertEqual(worker.pending(), [])
        self.assertEqual(worker.stats['succeeded'], 1)

    def test_gives_up_after_three_attempts(self):
        self.emby.add_episode('e1', 's1', DST)
        self.emby.probe_fills = False  # PlaybackInfo 返回 200 但条目仍无 MediaStreams
        worker, clock, capture = make(self.emby, retry_delays=(180, 600))
        worker.enqueue_transfer(transfer_event(SRC, [DST]))
        for _ in range(5):
            worker.run_due()
            clock.now += 600
        self.assertEqual(worker.pending(), [])
        self.assertEqual(worker.stats['gave_up'], 1)
        self.assertEqual(self.emby.paths('POST').count('/Items/e1/PlaybackInfo'), 3)
        self.assertTrue(any('已试 3 次' in line for line in capture.lines))

    def test_http_error_counts_as_failure(self):
        self.emby.add_episode('e1', 's1', DST)
        self.emby.playback_status = 500
        worker, _, _ = make(self.emby)
        worker.enqueue_transfer(transfer_event(SRC, [DST]))
        worker.run_due()
        self.assertEqual(worker.pending()[0].last_error, 'PlaybackInfo HTTP 500')

    def test_one_at_a_time_with_two_second_gap(self):
        paths = [DST.replace('E01', f'E0{n}') for n in (1, 2, 3)]
        for n, path in enumerate(paths):
            self.emby.add_episode(f'e{n}', 's1', path)
        worker, clock, _ = make(self.emby, min_interval=0.5)  # 下限仍是 2 秒
        stamps = []
        original = self.emby.__call__

        def recording(method, url, params, headers, timeout):
            if url.endswith('/PlaybackInfo'):
                stamps.append(clock.now)
            return original(method, url, params, headers, timeout)

        worker._http = recording
        worker.enqueue([prefill.PrefillTarget(path=path, tmdb_id='100') for path in paths])
        self.assertEqual(worker.run_due(), 3)
        self.assertEqual(len(stamps), 3)
        self.assertTrue(all(b - a >= 2 for a, b in zip(stamps, stamps[1:])), stamps)

    def test_duplicate_events_and_close(self):
        worker, _, _ = make(self.emby, initial_delay=3600)
        event = transfer_event(SRC, [DST])
        self.assertEqual(worker.enqueue_transfer(event), 1)
        self.assertEqual(worker.enqueue_transfer(event), 0)
        worker.close()
        self.assertEqual(worker.pending(), [])
        self.assertEqual(worker.enqueue_transfer(event), 0)

    def test_background_thread_processes_queue(self):
        self.emby.add_episode('e1', 's1', DST)
        server = prefill.EmbyServer(name='emby', host='http://emby-host:8096', api_key=API_KEY, user_id='u1')
        worker = prefill.MediaInfoPrefiller(http=self.emby, servers=lambda: [server], initial_delay=0)
        try:
            worker.enqueue_transfer(transfer_event(SRC, [DST]))
            deadline = time.monotonic() + 5
            while worker.pending() and time.monotonic() < deadline:
                time.sleep(0.05)
            self.assertEqual(worker.pending(), [])
            self.assertEqual(worker.stats['succeeded'], 1)
        finally:
            worker.close()

    def test_no_server_is_a_retryable_failure(self):
        worker, _, _ = make(self.emby, servers=lambda: [])
        worker.enqueue_transfer(transfer_event(SRC, [DST]))
        worker.run_due()
        self.assertEqual(worker.pending()[0].last_error, '没有可用的 Emby 服务器')


class ConfigDefaultsTest(unittest.TestCase):
    def test_new_settings_default_off_and_do_not_touch_existing(self):
        config_src = (PLUGIN_DIR / 'core' / 'config.py').read_text(encoding='utf-8')
        self.assertIn('"prefill_mediainfo_enabled": False', config_src)
        self.assertIn('"prefill_mediainfo_source_paths": ""', config_src)
        init_src = (PLUGIN_DIR / '__init__.py').read_text(encoding='utf-8')
        # 预读只读取自己的两个键；没有往配置里写回任何东西
        body = init_src.split('def _apply_prefill_config', 1)[1].split('\n    def ', 1)[0]
        self.assertNotIn('update_config', body)
        self.assertNotIn('_persist_config_values', body)
        self.assertNotIn('subscribe_filter_mode', body)
        self.assertNotIn('exclude_subscribes', body)


if __name__ == '__main__':
    unittest.main()
