"""0.5.15：「第N集（上/下）」按播出日期复核后折算 2N-1/2N；无集号正片按播出日期唯一兜底。

用《女子推理社》S01 夸克分享的真实命名；MetaInfo 用最小假实现，不访问网络。
"""

import datetime
import importlib.util
import re
import sys
import types
import unittest
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[3]
PLUGIN_DIR = ROOT / 'plugins.v3' / 'cloudsubscribefork'
PKG = 'csf_split_episode_under_test'


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


class FakeMeta:
    """足够覆盖本测试的 MetaInfo：片名（中文/英文开头）、年份、SxxEyy / Eyy。"""

    def __init__(self, text):
        stem = re.sub(r'\.(mkv|mp4|avi|iso)$', '', str(text), flags=re.I)
        se = re.search(r'S(\d{1,2})E(\d{1,4})', stem, re.I)
        ep = re.search(r'(?<![A-Za-z0-9])E(\d{1,4})(?!\d)', stem)
        self.begin_season = int(se.group(1)) if se else None
        cn_season = re.search(r'第([一二三四五六七八九十\d]+)季', stem)
        if cn_season and self.begin_season is None:
            self.begin_season = P._issue_number(cn_season.group(1))
        self.begin_episode = int(se.group(2)) if se else (int(ep.group(1)) if ep else None)
        year = re.search(r'(?<!\d)((?:19|20)\d{2})(?!\d)', stem)
        self.year = year.group(1) if year else None
        title = re.match(r'\s*([一-鿿]+)', stem)
        self.cn_name = title.group(1) if title else None
        self.en_name = None
        self.original_name = None
        self.type = None


_module('app')
_module('app.sdk')
_module('app.sdk.media', MetaInfo=FakeMeta)
_module('app.sdk.logging', logger=SimpleNamespace(
    debug=lambda *a, **k: None, info=lambda *a, **k: None,
    warning=lambda *a, **k: None, error=lambda *a, **k: None))
for _name in (PKG, f'{PKG}.utils', f'{PKG}.search'):
    _module(_name)
parser_module = _load(f'{PKG}.utils.file_parser', 'utils/file_parser.py')
_load(f'{PKG}.search.matching', 'search/matching.py')
guard_module = _load(f'{PKG}.utils.episode_guard', 'utils/episode_guard.py')
matcher_module = _load(f'{PKG}.utils.file_matcher', 'utils/file_matcher.py')

P = parser_module.MediaFileParser
FileMatcher = matcher_module.FileMatcher
SeasonGuard = guard_module.SeasonGuard

TODAY = datetime.date(2026, 11, 26)
AIR = {}
for n, (up, down) in enumerate([('05-30', '05-31'), ('06-06', '06-07'), ('06-13', '06-14'),
                                ('06-20', '06-21'), ('06-27', '06-28'), ('07-04', '07-05')], 7):
    AIR[2 * n - 1], AIR[2 * n] = f'2023-{up}', f'2023-{down}'


def guard(air=AIR):
    return SeasonGuard.build(season=1, season_year=2023, show_year=2023, air_dates=air, today=TODAY)


def files(*names):
    return [{'id': str(i), 'name': name, 'size': 1} for i, name in enumerate(names)]


def picked(result):
    return {ep: [f['name'] for f in items] for ep, items in result.items() if items}


class SplitEpisodeTest(unittest.TestCase):
    def test_patterns(self):
        parse = P.split_episode_number
        self.assertEqual(parse('2023-05-31第7集：死亡罚单（下）.mp4'), 14)
        self.assertEqual(parse('2023-05-30第7集：死亡罚单（上）.mp4'), 13)
        self.assertEqual(parse('第十二集上.mp4'), 23)
        self.assertEqual(parse('第8集【下】.mkv'), 16)
        self.assertIsNone(parse('第8集 上海往事.mkv'))
        self.assertIsNone(parse('第8期上.mkv'))
        self.assertIsNone(parse('S01E07.mkv'))

    def test_dated_halves_map_and_extras_do_not_displace(self):
        names = ['2023-05-30第7集：死亡罚单（上）.mp4', '2023-05-31第7集：死亡罚单（下）.mp4',
                 '2023-06-06第8集：郁金香遗愿（上）.mp4', '2023-06-07第8集：郁金香遗愿（下）.mp4',
                 '2023-07-05第12集：再见 三班岛（下）.mp4',
                 '2023-05-31便利店第7集.mp4', '2023-05-31超前彩蛋.mp4']
        result = FileMatcher.episode_candidates(files(*names), 1, [14, 15, 16, 24], None, guard())
        self.assertEqual(picked(result), {
            14: ['2023-05-31第7集：死亡罚单（下）.mp4'],
            15: ['2023-06-06第8集：郁金香遗愿（上）.mp4'],
            16: ['2023-06-07第8集：郁金香遗愿（下）.mp4'],
            24: ['2023-07-05第12集：再见 三班岛（下）.mp4'],
        })

    def test_not_converted_without_matching_date(self):
        air = {7: '2023-05-31', 14: '2023-07-12'}
        result = FileMatcher.episode_candidates(
            files('2023-05-31第7集：死亡罚单（下）.mp4'), 1, [7, 14], None, guard(air))
        self.assertEqual(set(picked(result)), {7})
        result = FileMatcher.episode_candidates(files('第7集：死亡罚单（下）.mp4'), 1, [14], None, guard())
        self.assertEqual(picked(result), {})

    def test_date_fallback_only_when_unambiguous(self):
        g = guard()
        one = FileMatcher.episode_candidates(files('女子推理社 2023-06-06.mp4'), 1, [15], None, g)
        self.assertEqual(picked(one), {15: ['女子推理社 2023-06-06.mp4']})
        two = FileMatcher.episode_candidates(
            files('女子推理社 2023-06-06.mp4', '女子推理社 2023-06-06 便利店.mp4'), 1, [15], None, g)
        self.assertEqual(picked(two), {})
        extra = FileMatcher.episode_candidates(files('2023-06-06 花絮.mp4'), 1, [15], None, g)
        self.assertEqual(picked(extra), {})
        sxe = FileMatcher.episode_candidates(files('Show.S01E07.2023-06-06.mp4'), 1, [15], None, g)
        self.assertEqual(picked(sxe), {})


if __name__ == '__main__':
    unittest.main()
