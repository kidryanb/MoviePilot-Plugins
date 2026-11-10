"""《现在就出发》S04 事故回归：旧季文件占位、未播出集、只有期号不猜集号。

用真实文件名；MetaInfo 用一个最小的假实现（只认 S/E、年份和片名），不访问网络。
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
PKG = 'csf_season_guard_under_test'


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

TODAY = datetime.date(2026, 11, 10)
# S04：每周六上、周日下；E01/E02=10-10/10-11 … E09/E10=11-07/11-08，E11=11-14 起未播出
AIR_DATES = {}
for issue in range(1, 11):
    saturday = datetime.date(2026, 10, 10) + datetime.timedelta(days=7 * (issue - 1))
    AIR_DATES[issue * 2 - 1] = saturday
    AIR_DATES[issue * 2] = saturday + datetime.timedelta(days=1)
AIR_DATES[7] = datetime.date(2026, 10, 31)
AIR_DATES[8] = datetime.date(2026, 11, 1)
AIR_DATES[10] = datetime.date(2026, 11, 8)
AIR_DATES[11] = datetime.date(2026, 11, 14)
AIR_DATES[12] = datetime.date(2026, 11, 15)

MEDIA = SimpleNamespace(title='现在就出发', year='2023', season_years={4: '2026'}, type=None,
                        en_title=None, original_title=None, original_name=None, names=[])

OLD_SEASON_FILES = {
    11: '20241130-第6期上.mp4',
    12: '20241201-第6期下.mp4',
    13: '2025.12.06-第7期上.mp4',
    14: '20241208-第7期下.mkv',
    15: '2025.12.13-第8期上.mp4',
    16: '20241215-第8期下.mp4',
    17: '20241221-第9期上.mkv',
    18: '20241222-第9期下.mp4',
    19: '20241228-第10期上.mkv',
    20: '20241229-第10期下.mkv',
}


def guard(title='', grace=0, **options):
    return SeasonGuard.build(
        season=4, season_year=2026, show_year=2023, air_dates=AIR_DATES,
        today=TODAY, grace_days=grace, **options,
    ).with_title(title)


def episode(name, title='', grace=0, mediainfo=None, path=''):
    item = {'name': name}
    if path:
        item['_relative_path'] = path
    return FileMatcher.episode_from_file(item, 4, mediainfo, guard(title, grace))


class OldSeasonRejectTest(unittest.TestCase):
    """用例 1–8：旧季占位必须拒绝。"""

    def test_old_files_never_map_into_season_4(self):
        for slot, name in OLD_SEASON_FILES.items():
            for mediainfo in (None, MEDIA):
                for grace in (0, 30):
                    with self.subTest(name=name, grace=grace, media=bool(mediainfo)):
                        self.assertIsNone(episode(name, grace=grace, mediainfo=mediainfo))

    def test_split_mapping_itself_is_unchanged(self):
        self.assertEqual(P.split_issue_episode('20241130-第6期上.mp4'), 11)
        self.assertEqual(P.split_issue_episode('20241229-第10期下.mkv'), 20)

    def test_share_title_of_other_season_rejects_titleless_files(self):
        g = guard('现在就出发 第二季', grace=30)
        for name in OLD_SEASON_FILES.values():
            self.assertTrue(g.file_reject_reason(name))
        self.assertIn('其他季', g.file_reject_reason('第6期上.mp4'))

    def test_correct_title_but_wrong_year_uses_file_date(self):
        reason = guard('【现在就出发 第四季】更新至11月10日', grace=30).file_reject_reason(
            '2025.12.06-第7期上.mp4')
        self.assertIn('日期前缀 2025-12-06', reason)

    def test_no_search_or_transfer_for_unaired_episodes(self):
        g = guard()
        aired, unaired = g.split_aired(range(1, 21))
        self.assertEqual(aired, list(range(1, 11)))
        self.assertEqual(unaired, list(range(11, 21)))
        self.assertIn('尚未播出', g.episode_reject_reason('2026.11.14-第6期上.mp4', 11))

    def test_candidates_never_offer_old_files(self):
        files = [{'name': name} for name in OLD_SEASON_FILES.values()]
        found = FileMatcher.episode_candidates(files, 4, list(range(1, 21)), None, guard(grace=30))
        self.assertTrue(all(not value for value in found.values()))


class CurrentSeasonAcceptTest(unittest.TestCase):
    """用例 9–12：本季正确文件仍然接受。"""

    def test_known_good_files(self):
        self.assertEqual(episode('2026.11.08-第5期下.mp4'), 10)
        self.assertEqual(episode('2026.11.1 第4期下.mp4'), 8)
        self.assertEqual(episode('2026.11.08-第5期下.mp4', mediainfo=MEDIA), 10)

    def test_chinese_season_number_in_name(self):
        self.assertEqual(episode('现在就出发 第四季 2026.11.08-第5期下.mp4'), 10)

    def test_grace_days_open_e11_but_old_files_stay_out(self):
        self.assertFalse(guard().is_aired(11))
        self.assertTrue(guard(grace=4).is_aired(11))
        self.assertEqual(episode('2026.11.14-第6期上.mp4', grace=4), 11)
        self.assertIsNone(episode('20241130-第6期上.mp4', grace=4))
        self.assertIsNone(episode('2025.12.06-第7期上.mp4', grace=4))

    def test_file_date_must_match_air_date(self):
        # 本季年份对，但日期和 E10 播出日差了一个月
        self.assertIsNone(episode('2026.10.08-第5期下.mp4'))

    def test_correct_season_in_directory_is_enough(self):
        self.assertEqual(episode('第5期下.mp4', path='现在就出发 第四季/第5期下.mp4'), 10)
        self.assertIsNone(episode('第5期下.mp4', path='第三季/第5期下.mp4'))


class BareIssueTest(unittest.TestCase):
    """用例 13–15：只有期号不猜集号；中文期号。"""

    def test_with_lower_half(self):
        name = '2026-10-11 2160p.60fps.第1期下：目击狼人杀升级，小白安宇猜词再共脑.mp4'
        self.assertEqual(episode(name), 2)

    def test_without_upper_or_lower_is_not_guessed(self):
        name = '2026-10-11 2160p.60fps.第1期：目击狼人杀升级.mp4'
        g = SeasonGuard.build(season=4, season_year=2026, show_year=2023,
                              air_dates={1: '2026-10-10', 2: '2026-10-12'}, today=TODAY)
        self.assertIsNone(FileMatcher.episode_from_file({'name': name}, 4, None, g))
        self.assertIsNone(FileMatcher.episode_from_file({'name': '第1期.mp4'}, 4, None, None))
        self.assertEqual(P.bare_issue_number(name), 1)

    def test_bare_issue_only_used_when_date_hits_exactly_one_air_date(self):
        self.assertEqual(episode('2026-10-11 第1期 加更.mp4'), 2)

    def test_chinese_issue_number(self):
        self.assertEqual(episode('2026.11.08-第五期下.mp4'), 10)


class SeasonNumberTest(unittest.TestCase):
    """用例 16–18：季号识别。"""

    def test_other_season(self):
        for text in ('第二季', '第2季', 'S2', 'Season 2', '第02季', '第 2 季', '第十二季'):
            with self.subTest(text=text):
                self.assertTrue(P.contains_other_season(text, 4))

    def test_target_season(self):
        for text in ('第四季', '第4季', 'S04', 'Season 4', 'S04E11'):
            with self.subTest(text=text):
                self.assertTrue(P.matches_target_season(text, 4))
                self.assertFalse(P.contains_other_season(text, 4))

    def test_sxxexx_is_not_split_wrong(self):
        self.assertEqual(P.season_numbers('现在就出发.S04E11.2160p'), {4})
        self.assertEqual(P.season_numbers('第4期下'), set())
        self.assertEqual(P.season_numbers('MP4S2.2160p'), set())

    def test_show_year_is_not_a_season_year(self):
        g = guard('现在就出发（2023） 第四季')
        self.assertEqual(g.file_reject_reason('2026.11.08-第5期下.mp4'), '')

    def test_no_evidence_is_not_guessed(self):
        self.assertIn('不按订阅季猜测', guard().file_reject_reason('E11.mp4', require_evidence=True))
        self.assertIsNone(episode('E05.mp4'))
        season_one = SeasonGuard.build(season=1, season_year=2023, today=TODAY)
        self.assertEqual(season_one.file_reject_reason('E05.mp4', require_evidence=True), '')
        relaxed = guard(require_season_evidence=False)
        self.assertEqual(relaxed.file_reject_reason('E05.mp4', require_evidence=True), '')
        self.assertEqual(
            FileMatcher.episode_from_file({'name': 'E05.mp4'}, 4, None, relaxed), 5)

    def test_files_with_their_own_title_skip_evidence_rule(self):
        # 有作品名的文件交给平台识别，不要求季号证据（例如番剧第二季单独起名）
        self.assertEqual(guard().file_reject_reason('某番 第二部 - 05.mkv'), '')


class MissingAndUnairedTest(unittest.TestCase):
    """用例 19–20：缺集并入已有、未播出不搜。"""

    @staticmethod
    def search_targets(mp_missing, cloud, local, g):
        already = guard_module.existing_union(cloud, local) & set(mp_missing)
        remaining = [e for e in sorted(mp_missing) if e not in already]
        return g.split_aired(remaining)[0]

    def test_unaired_never_searched(self):
        self.assertEqual(self.search_targets(range(1, 21), [], range(1, 11), guard()), [])

    def test_placeholder_blocks_search_and_unaired_blocks_rest(self):
        self.assertEqual(self.search_targets(range(1, 21), [11], range(1, 11), guard()), [])
        # 占位存在 → 不搜；即使放宽宽限天数让 E11 算已播出，也不搜被占的 E11
        self.assertEqual(self.search_targets(range(1, 21), [11], range(1, 11), guard(grace=5)), [12])

    def test_missing_air_dates_mean_unaired(self):
        g = SeasonGuard.build(season=4, season_year=2026, air_dates={}, today=TODAY)
        self.assertEqual(g.split_aired([1, 2]), ([], [1, 2]))
        off = SeasonGuard.build(season=4, season_year=2026, air_dates={}, today=TODAY,
                                filter_unaired=False)
        self.assertEqual(off.split_aired([1, 2]), ([1, 2], []))


class WiringTest(unittest.TestCase):
    def test_television_uses_guard(self):
        source = (PLUGIN_DIR / 'handlers/sync/television.py').read_text(encoding='utf-8')
        self.assertIn('season_guard.split_aired(missing_episodes)', source)
        self.assertIn('guard=resource_guard', source)
        self.assertIn('resource_guard.reject_reason(', source)
        self.assertIn('target_episode_air_dates.update(season_guard.air_date_strings())', source)

    def test_config_defaults_registered(self):
        config = (PLUGIN_DIR / 'core/config.py').read_text(encoding='utf-8')
        for key in ('"filter_unaired_episodes": True', '"unaired_episode_grace_days": 0',
                    '"filename_date_tolerance_days": 1', '"require_season_evidence": True'):
            self.assertIn(key, config)


if __name__ == '__main__':
    unittest.main()
