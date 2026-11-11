"""0.5.14 回归：特别篇不绑正季、分享优先于磁力、合集弱匹配、PanSou 目标季排序。

事故：「全职高手」S02 缺 E01–E12，自动同步三次都先搜到 115/夸克分享，却仍把 pianku 的
「全职高手 第二季」磁力整包提交 115 离线；「全职高手 特别篇」磁力被绑到 S03 的 E01–E17。

从源码按 AST 取出相关方法，配最小替身执行，不导入宿主、不访问网络。
"""

import ast
import hashlib
import importlib.util
import re
import sys
import threading
import time
import types
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, Iterable, List, Mapping, Optional, Set, Tuple

ROOT = Path(__file__).resolve().parents[3]
PLUGIN_DIR = ROOT / 'plugins.v3' / 'cloudsubscribefork'
PKG = 'csf_share_first_under_test'

_SILENT = SimpleNamespace(
    info=lambda *a, **k: None, warning=lambda *a, **k: None,
    error=lambda *a, **k: None, debug=lambda *a, **k: None,
)


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


_package(PKG)
_package(f'{PKG}.utils')
FILE_PARSER = _load(f'{PKG}.utils.file_parser', 'utils/file_parser.py')
GUARD = _load(f'{PKG}.utils.episode_guard', 'utils/episode_guard.py')
SEARCH_TYPES = _load(f'{PKG}.search_types', 'search/types.py')
MediaFileParser = FILE_PARSER.MediaFileParser
SeasonGuard = GUARD.SeasonGuard


def _extract_magnet_hash(text):
    match = re.search(r'btih:([0-9a-fA-F]{40}|[2-7a-zA-Z]{32})', str(text or ''))
    return match.group(1).upper() if match else None


_NAMESPACE = {
    'Any': Any, 'Dict': Dict, 'List': List, 'Optional': Optional, 'Set': Set,
    'Tuple': Tuple, 'Mapping': Mapping, 'Iterable': Iterable,
    're': re, 'time': time, 'hashlib': hashlib, 'logger': _SILENT, 'Path': Path,
    'MediaFileParser': MediaFileParser,
    'normalize_resource_type': SEARCH_TYPES.normalize_resource_type,
    'CANDIDATE_RESOURCE_TYPES': SEARCH_TYPES.CANDIDATE_RESOURCE_TYPES,
    'extract_magnet_hash': _extract_magnet_hash,
    'tmdb_id_of': lambda subscribe: getattr(subscribe, 'tmdbid', None),
    'MediaInfo': object,
}


def _class_members(relative_path, class_name, names, base=object):
    """取出类里指定的方法和类属性，保留 classmethod / staticmethod，组成一个新类。"""
    tree = ast.parse((PLUGIN_DIR / relative_path).read_text(encoding='utf-8'))
    body = []
    for cls in tree.body:
        if not (isinstance(cls, ast.ClassDef) and cls.name == class_name):
            continue
        for node in cls.body:
            if isinstance(node, ast.FunctionDef) and node.name in names:
                body.append(node)
            elif isinstance(node, ast.Assign) and any(
                    isinstance(target, ast.Name) and target.id in names for target in node.targets
            ):
                body.append(node)
    found = {
        node.name if isinstance(node, ast.FunctionDef) else node.targets[0].id
        for node in body
    }
    missing = set(names) - found
    if missing:
        raise AssertionError(f'{relative_path} 缺少成员：{sorted(missing)}')
    namespace = dict(_NAMESPACE)
    namespace['_Base'] = base
    module = ast.parse(f'class {class_name}Part(_Base):\n    pass\n')
    module.body[0].body = body
    exec(compile(module, relative_path, 'exec'), namespace)
    return namespace[f'{class_name}Part']


RESOURCES = _class_members('handlers/sync/resources.py', 'ResourceTransferService', {
    '_TITLE_SEASON_PATTERN', '_TITLE_CN_SEASON_PATTERN', '_SPECIAL_TITLE_PATTERN',
    '_REGULAR_PACK_PATTERN', '_OFFLINE_RESOURCE_TYPES',
    '_resource_title_seasons', '_is_special_only_title', '_movie_title_seasons_conflict',
    '_magnet_title_seasons', '_magnet_resource_title', '_resource_title_episodes',
    '_magnet_title_episodes', '_build_transfer_resource_batches',
    '_normalize_candidate_resource_type',
})
PANSOU = _class_members('search/pansou/service.py', 'PanSouSearchService', {
    '_CN_NUMBERS', '_NUM', '_SEASON_MARKER_RE', '_SEASON_RANGE_RE', '_SEASON_TOTAL_RE',
    '_cn_number', '_season_relevance', '_rank_by_season', '_select_candidates', '_round_robin',
})
QUEUE = _class_members('handlers/sync/service.py', 'SyncHandler', {'_queue_magnet_package'})

TV_SOURCE = (PLUGIN_DIR / 'handlers/sync/television.py').read_text(encoding='utf-8')
UPGRADE_SOURCE = (PLUGIN_DIR / 'handlers/sync/upgrade.py').read_text(encoding='utf-8')
SERVICE_SOURCE = (PLUGIN_DIR / 'handlers/sync/service.py').read_text(encoding='utf-8')

HASH = 'C' * 40
S02_MAGNET = f'magnet:?xt=urn:btih:{HASH}&dn=quanzhi'
SP_MAGNET = f'magnet:?xt=urn:btih:{"D" * 40}&dn=sp'
S02_FILES = [f'全职高手S02E{episode:02d}.mp4' for episode in range(1, 13)]


def _seasons(title, **resource):
    return RESOURCES._magnet_title_seasons({'title': title, **resource})


def _tv_prefilter_skips(title, season):
    """与 television.py 磁力分支的标题季预过滤一致：特别篇包或标题季不含目标季时跳过。"""
    title_seasons = _seasons(title)
    if title_seasons == {0} and season:
        return True
    return bool(title_seasons and season not in title_seasons)


class SpecialPackTest(unittest.TestCase):
    """第 4 条：特别篇 / SP / OVA 包不绑到普通季。"""

    def test_pure_special_titles_map_to_season_zero(self):
        for title in ('全职高手 特别篇', '全职高手 特别篇.1080p', '全職高手 特別篇',
                      '工作细胞 OVA', 'xxx SP01', 'xxx SP', 'Some Show OAD 2', 'Show S00E01'):
            with self.subTest(title=title):
                self.assertEqual(_seasons(title), {0})
                self.assertTrue(_tv_prefilter_skips(title, 3))
                self.assertTrue(_tv_prefilter_skips(title, 2))

    def test_regular_season_titles_still_bind(self):
        self.assertEqual(_seasons('全职高手 第二季'), {2})
        self.assertEqual(_seasons('全职高手 S02'), {2})
        self.assertEqual(_seasons('全职高手 第2季'), {2})
        self.assertFalse(_tv_prefilter_skips('全职高手 第二季', 2))
        self.assertFalse(_tv_prefilter_skips('全职高手 S02', 2))
        self.assertTrue(_tv_prefilter_skips('全职高手 第二季', 3))

    def test_regular_pack_with_special_is_not_dropped(self):
        self.assertFalse(RESOURCES._is_special_only_title('[国漫]全职高手.全3季+电影版特别篇.国语中字.4K'))
        self.assertEqual(_seasons('[国漫]全职高手.全3季+电影版特别篇.国语中字.4K'), set())
        self.assertEqual(_seasons('全职高手 S02 + 特别篇'), {2})
        self.assertFalse(_tv_prefilter_skips('全职高手 S02 + 特别篇', 2))
        self.assertFalse(RESOURCES._is_special_only_title('全职高手 S01-S03 特别篇'))

    def test_special_package_never_fills_s03_episodes(self):
        resource = {'title': '全职高手 特别篇'}
        self.assertEqual(RESOURCES._magnet_title_episodes(resource, 3), set())
        self.assertTrue(_tv_prefilter_skips(resource['title'], 3))

    def test_words_containing_sp_are_not_specials(self):
        for title in ('Spy x Family S01', 'Inspector 2024', 'DDP5.1 WEB-DL', '全职高手 2019'):
            with self.subTest(title=title):
                self.assertFalse(RESOURCES._is_special_only_title(title))

    def test_movie_special_rules(self):
        movie = SimpleNamespace(title='全职高手之巅峰荣耀', original_title='', en_title='')
        special_movie = SimpleNamespace(title='某某 特别篇', original_title='', en_title='')
        self.assertTrue(RESOURCES._movie_title_seasons_conflict({0}, movie))
        self.assertFalse(RESOURCES._movie_title_seasons_conflict({0}, special_movie))
        self.assertTrue(RESOURCES._movie_title_seasons_conflict({2}, special_movie))
        self.assertFalse(RESOURCES._movie_title_seasons_conflict(set(), movie))

    def test_prefilters_are_wired_in_tv_and_upgrade(self):
        self.assertIn('if title_seasons == {0} and season:', TV_SOURCE)
        self.assertIn('if title_seasons == {0} and season:', UPGRADE_SOURCE)
        self.assertIn('self._movie_title_seasons_conflict(title_seasons, mediainfo)', SERVICE_SOURCE)


class BatchOrderTest(unittest.TestCase):
    """第 6 条：磁力 / ed2k 一律排在全部分享批次之后，不受 resource_type_order 影响。"""

    class Owner(RESOURCES):
        def __init__(self, order, cross_urls=()):
            self._search_handler = SimpleNamespace(_resource_type_order_config=order)
            self._cloud_drive = SimpleNamespace(key='115')
            self._cross = set(cross_urls)

        def _is_cross_drive_resource(self, resource, share_url=''):
            return resource['url'] in self._cross

        @staticmethod
        def _is_ed2k_url(url):
            return str(url).startswith('ed2k://')

        @staticmethod
        def _is_magnet_url(url):
            return str(url).startswith('magnet:')

        def _is_offline_url(self, url):
            return self._is_ed2k_url(url) or self._is_magnet_url(url)

        def _supported_resource_type(self, resource, url):
            return ''

    def test_magnet_after_all_shares_even_if_configured_first(self):
        owner = self.Owner(['magnet', 'ed2k', '115', 'quark'], cross_urls={'https://pan.quark.cn/s/x'})
        results = {
            'pianku': [{'url': S02_MAGNET, 'resource_type': 'magnet', 'title': '全职高手 第二季'}],
            'pansou': [
                {'url': 'https://115.com/s/a', 'resource_type': '115', 'title': 'a'},
                {'url': 'https://pan.quark.cn/s/x', 'resource_type': 'quark', 'title': '全职高手 全3季'},
                {'url': SP_MAGNET, 'resource_type': 'magnet', 'title': '全职高手 特别篇'},
            ],
        }
        batches = owner._build_transfer_resource_batches(['pansou', 'pianku'], results)
        kinds = [
            [owner._normalize_candidate_resource_type(item) for item in items]
            for _, items, _ in batches
        ]
        first_offline = next(i for i, k in enumerate(kinds) if set(k) & {'magnet', 'ed2k'})
        self.assertTrue(all(not (set(k) & {'magnet', 'ed2k'}) for k in kinds[:first_offline]))
        self.assertTrue(all(set(k) <= {'magnet', 'ed2k'} for k in kinds[first_offline:]))
        self.assertEqual(sum(len(items) for _, items, _ in batches), 4)
        cross_quark = [b for b in batches if b[2]]
        self.assertEqual(cross_quark[0][1][0]['resource_type'], 'quark')


class PackMatchStrengthTest(unittest.TestCase):
    """第 6 条：合集标题 + 文件名带本季 SxxExx 时按强匹配直接转存。"""

    def test_pack_title_with_season_files_is_strong(self):
        guard = SeasonGuard.build(2).with_title('[国漫]全职高手.全3季+电影版特别篇.国语中字.4K')
        for name in S02_FILES:
            strong, reasons = guard.match_strength({'name': name}, int(name[-6:-4]))
            self.assertTrue(strong, (name, reasons))
            self.assertEqual(reasons, [])

    def test_pack_title_without_file_season_is_still_weak(self):
        guard = SeasonGuard.build(2).with_title('全职高手 全3季')
        strong, reasons = guard.match_strength({'name': '全职高手 01.mp4'}, 1)
        self.assertFalse(strong)
        self.assertIn('分享标题像多季合集', reasons)

    def test_ai_gate_holds_episodes_against_magnet(self):
        self.assertIn('held_episodes=share_held_episodes', TV_SOURCE)
        self.assertIn('held = set(target_episodes) & share_held_episodes', TV_SOURCE)


class ShareFirstScenarioTest(unittest.TestCase):
    """组合：分享批次在前、分享覆盖全部缺集后，磁力批次不再处理（television 遇到缺集已齐即 break）。"""

    def _run(self, share_files, share_title):
        owner = BatchOrderTest.Owner(['quark', '115', 'magnet'])
        results = {
            'pansou': [{'url': 'https://pan.quark.cn/s/pack', 'resource_type': 'quark', 'title': share_title}],
            'pianku': [{'url': S02_MAGNET, 'resource_type': 'magnet', 'title': '全职高手 第二季'}],
        }
        missing = set(range(1, 13))
        guard = SeasonGuard.build(2, filter_unaired=False)
        queued_magnets = []
        for _, resources, _ in owner._build_transfer_resource_batches(['pansou', 'pianku'], results):
            if not missing:
                break
            for resource in resources:
                if resource['resource_type'] == 'magnet':
                    queued_magnets.append(resource['title'])
                    continue
                resource_guard = guard.with_title(resource['title'])
                for name in share_files:
                    seasons_eps = MediaFileParser.extract_season_episode(name)
                    if not seasons_eps or seasons_eps[0] != 2:
                        continue
                    episode = seasons_eps[1]
                    if resource_guard.reject_reason({'name': name}, episode):
                        continue
                    strong, _ = resource_guard.match_strength({'name': name}, episode)
                    if strong:
                        missing.discard(episode)
        return missing, queued_magnets

    def test_pack_share_fills_s02_and_magnet_is_not_queued(self):
        missing, magnets = self._run(S02_FILES, '[国漫]全职高手.全3季+电影版特别篇.国语中字.4K')
        self.assertEqual(missing, set())
        self.assertEqual(magnets, [])

    def test_magnet_only_after_unusable_shares(self):
        missing, magnets = self._run(['全职高手S03E01.mp4', '游戏入侵 第1集.mp4'], '游戏入侵')
        self.assertEqual(missing, set(range(1, 13)))
        self.assertEqual(magnets, ['全职高手 第二季'])


class _Lock:
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class QueueOwner(QUEUE, RESOURCES):
    """_queue_magnet_package 的最小运行环境。"""

    _OFFLINE_PENDING_KEY = 'pending_offline_strm'

    def __init__(self, fetched_metadata=None):
        self.data = {}
        self._offline_pending_lock = threading.Lock()
        self._cloud_transfer_path = '/115/转存'
        self.fetch_calls = 0
        fetched = fetched_metadata or {}

        def parse_magnet_link(url, fetch_metadata=False):
            self.fetch_calls += 1
            return {'metadata': dict(fetched)}

        self._offline_download = SimpleNamespace(parse_magnet_link=parse_magnet_link)

    def _get_data(self, key):
        return self.data.get(key)

    def _save_offline_pending(self, pending):
        self.data[self._OFFLINE_PENDING_KEY] = pending

    @staticmethod
    def _prepare_magnet_resource(resource, share_url):
        return RESOURCES._magnet_resource_title(resource)

    @staticmethod
    def _resource_preview_episodes(resource, season):
        return set(resource.get('preview_episodes') or ())

    @staticmethod
    def _format_episode_ranges(values):
        return ','.join(str(v) for v in sorted(values))

    @staticmethod
    def _is_magnet_url(url):
        return str(url).startswith('magnet:')

    @staticmethod
    def _is_ed2k_url(url):
        return str(url).startswith('ed2k://')

    @staticmethod
    def _resource_log_reference(url):
        return 'ref'

    @staticmethod
    def scoped_drive_key():
        return '115'

    @staticmethod
    def _unreserved_episodes(pending, subscribe_id=None, season=None, targets=(), **kwargs):
        return sorted(targets)

    @staticmethod
    def _serialize_mediainfo(mediainfo):
        return {}


class MagnetEvidenceTest(unittest.TestCase):
    """第 6 条：仅凭「第 N 季」、没有标题集数也没有种子文件列表的磁力，不整包绑定全部缺集。"""

    subscribe = SimpleNamespace(id=7, name='全职高手', tmdbid=1)

    def _queue(self, owner, resource, targets, **kwargs):
        queue: List[Dict[str, Any]] = []
        key = owner._queue_magnet_package(
            resource, S02_MAGNET, self.subscribe, SimpleNamespace(title='全职高手'),
            season=2, target_episodes=targets, submit_queue=queue, **kwargs,
        )
        return key, queue

    def test_season_only_title_without_files_is_skipped(self):
        owner = QueueOwner(fetched_metadata={'metadata_available': True, 'display_name': '全职高手 第二季'})
        key, queue = self._queue(owner, {'title': '全职高手 第二季'}, list(range(1, 13)))
        self.assertEqual(key, '')
        self.assertEqual(queue, [])
        self.assertFalse(owner.data.get(owner._OFFLINE_PENDING_KEY))

    def test_torrent_file_list_binds_only_listed_episodes(self):
        owner = QueueOwner(fetched_metadata={
            'metadata_available': True, 'preview_episodes': {2: [1, 2, 3]},
        })
        resource = {'title': '全职高手 第二季', 'preview_episodes': {1, 2, 3}}
        targets = list(range(1, 13))
        key, queue = self._queue(owner, resource, targets)
        self.assertTrue(key)
        self.assertEqual(queue[0]['target_episodes'], [1, 2, 3])

    def test_special_title_is_rejected_for_s03(self):
        owner = QueueOwner()
        queue: List[Dict[str, Any]] = []
        key = owner._queue_magnet_package(
            {'title': '全职高手 特别篇'}, SP_MAGNET, self.subscribe, SimpleNamespace(title='全职高手'),
            season=3, target_episodes=list(range(1, 18)), submit_queue=queue,
        )
        self.assertEqual(key, '')
        self.assertEqual(queue, [])
        self.assertEqual(owner.fetch_calls, 0)

    def test_manual_choice_keeps_old_behaviour(self):
        owner = QueueOwner(fetched_metadata={'metadata_available': True})
        key, queue = self._queue(
            owner, {'title': '全职高手 第二季'}, list(range(1, 13)), require_episode_evidence=False,
        )
        self.assertTrue(key)
        self.assertEqual(queue[0]['target_episodes'], list(range(1, 13)))

    def test_tv_auto_path_requires_evidence(self):
        self.assertIn('require_episode_evidence=not manual_resources', TV_SOURCE)


class PanSouSeasonRankTest(unittest.TestCase):
    """第 6 条：自动订阅 10 条限额下，含目标季的合集不能被短剧和别的季挤掉。"""

    def test_relevance(self):
        rel = PANSOU._season_relevance
        self.assertEqual(rel({'title': '全职高手 第二季'}, 2), 3)
        self.assertEqual(rel({'title': '全职高手S02E01'}, 2), 3)
        self.assertEqual(rel({'title': '[国漫]全职高手.全3季+电影版特别篇.国语中字.4K'}, 2), 2)
        self.assertEqual(rel({'title': '全职高手 S01-S03'}, 2), 2)
        self.assertEqual(rel({'title': '全职高手 1-3季'}, 2), 2)
        self.assertEqual(rel({'title': '全职高手2019'}, 2), 1)
        self.assertEqual(rel({'title': '全职高手 第三季'}, 2), 0)
        self.assertEqual(rel({'title': '全职高手 全3季'}, 4), 1)

    def test_pack_in_tenth_place_is_selected(self):
        quark = [
            {'title': f'游戏入侵 短剧 {index}', 'update_time': f'2026-11-{20 - index:02d}', 'resource_type': 'quark'}
            for index in range(9)
        ]
        quark.append({
            'title': '[国漫]全职高手.全3季+电影版特别篇.国语中字.4K',
            'update_time': '2026-01-01', 'resource_type': 'quark',
        })
        p115 = [
            {'title': f'全职高手2019 版本{index}', 'update_time': f'2026-10-{10 + index:02d}', 'resource_type': '115'}
            for index in range(9)
        ]
        magnets = [{'title': '全职高手 第二季', 'update_time': '2026-11-11', 'resource_type': 'magnet'}]
        selected = PANSOU._select_candidates([p115, quark, magnets], 10, 2)
        titles = [row['title'] for row in selected]
        self.assertIn('[国漫]全职高手.全3季+电影版特别篇.国语中字.4K', titles)
        # 同组内覆盖目标季的合集排在短剧前面
        quark_titles = [t for t in titles if t.startswith(('游戏入侵', '[国漫]'))]
        self.assertEqual(quark_titles[0], '[国漫]全职高手.全3季+电影版特别篇.国语中字.4K')

    def test_resource_list_mode_is_unchanged(self):
        rows = [[{'title': 'a', 'update_time': '1'}, {'title': 'b', 'update_time': '2'}]]
        self.assertEqual([r['title'] for r in PANSOU._select_candidates(rows, 1, None)], ['a'])


if __name__ == '__main__':
    unittest.main()
