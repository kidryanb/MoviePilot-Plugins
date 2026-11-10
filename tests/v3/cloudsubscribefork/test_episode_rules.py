"""综艺上下期集号与 MP 订阅缺集口径的离线测试。"""

import importlib.util
import sys
import types
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
PLUGIN_DIR = ROOT / 'plugins.v3' / 'cloudsubscribefork'


def _load(name, relative_path):
    spec = importlib.util.spec_from_file_location(name, PLUGIN_DIR / relative_path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


file_parser = _load('csf_episode_rules_file_parser', 'utils/file_parser.py')
sync_utils = _load('csf_episode_rules_sync_utils', 'handlers/sync/utils.py')
Parser = file_parser.MediaFileParser


def _info(season, episodes):
    return types.SimpleNamespace(season=season, episodes=episodes)


class SplitIssueTest(unittest.TestCase):
    def tearDown(self):
        Parser.SPLIT_ISSUE_ENABLED = True

    def test_upper_and_lower_issue_map_to_tmdb_episodes(self):
        self.assertEqual(Parser.split_issue_episode('现在就出发 第四季 第4期上.mp4'), 7)
        self.assertEqual(Parser.split_issue_episode('现在就出发.S04.第4期(下).1080p.mkv'), 8)
        self.assertEqual(Parser.split_issue_episode('第5期（下）.mp4'), 10)
        self.assertEqual(Parser.split_issue_episode('第十期上集.mp4'), 19)
        self.assertEqual(Parser.split_issue_episode('第二十一期下篇.mp4'), 42)

    def test_plain_issue_and_extras_are_untouched(self):
        for name in ('第3期.mp4', '第4期加更.mp4', 'S04E03.mkv', '上海滩 第3集.mp4'):
            self.assertIsNone(Parser.split_issue_episode(name), name)
            self.assertEqual(Parser.normalize_split_issue(name), name)

    def test_normalize_rewrites_issue_as_episode_marker(self):
        normalized = Parser.normalize_split_issue('现在就出发.第4期下.1080p.mkv')
        self.assertIn(' E08 ', normalized)
        self.assertNotIn('期', normalized)

    def test_switch_turns_mapping_off(self):
        Parser.SPLIT_ISSUE_ENABLED = False
        self.assertIsNone(Parser.split_issue_episode('第4期上.mp4'))
        self.assertEqual(Parser.normalize_split_issue('第4期上.mp4'), '第4期上.mp4')


class SubscriptionMissingTest(unittest.TestCase):
    expected = set(range(1, 11))

    def test_media_server_episodes_count_as_present(self):
        no_exists = {'tmdb': {4: _info(4, [8, 10])}}
        missing = sync_utils.subscription_missing_episodes(
            self.expected, [], 4, exists=False, no_exists=no_exists)
        self.assertEqual(missing, {8, 10})

    def test_subscription_note_also_counts_as_present(self):
        no_exists = {'tmdb': {4: _info(4, [8, 10])}}
        missing = sync_utils.subscription_missing_episodes(
            self.expected, ['8'], 4, exists=False, no_exists=no_exists)
        self.assertEqual(missing, {10})

    def test_empty_episode_list_means_whole_season_missing(self):
        no_exists = {'tmdb': {4: _info(4, [])}}
        missing = sync_utils.subscription_missing_episodes(
            self.expected, [1, 2], 4, exists=False, no_exists=no_exists)
        self.assertEqual(missing, self.expected - {1, 2})

    def test_fully_present_season_has_nothing_missing(self):
        missing = sync_utils.subscription_missing_episodes(
            self.expected, [], 4, exists=True, no_exists={})
        self.assertEqual(missing, set())

    def test_other_seasons_are_ignored(self):
        no_exists = {'tmdb': {3: _info(3, [1, 2]), 4: _info(4, [9])}}
        missing = sync_utils.subscription_missing_episodes(
            self.expected, [], 4, exists=False, no_exists=no_exists)
        self.assertEqual(missing, {9})

    def test_media_server_failure_falls_back_to_note(self):
        missing = sync_utils.subscription_missing_episodes(
            self.expected, list(range(1, 8)), 4, server_checked=False)
        self.assertEqual(missing, {8, 9, 10})


class WiringTest(unittest.TestCase):
    def test_tv_flow_uses_mp_missing_and_no_longer_rewrites_progress(self):
        television = (PLUGIN_DIR / 'handlers/sync/television.py').read_text(encoding='utf-8')
        service = (PLUGIN_DIR / 'handlers/sync/service.py').read_text(encoding='utf-8')
        self.assertIn('self._mp_subscription_missing(', television)
        self.assertNotIn('_reconcile_subscribe_physical_episodes', television)
        self.assertNotIn('_reconcile_subscribe_physical_episodes', service)

    def test_parsers_normalize_split_issue(self):
        for relative in ('utils/file_matcher.py', 'handlers/sync/naming.py', 'utils/magnet.py'):
            text = (PLUGIN_DIR / relative).read_text(encoding='utf-8')
            self.assertIn('normalize_split_issue', text, relative)
        init = (PLUGIN_DIR / '__init__.py').read_text(encoding='utf-8')
        self.assertIn('SPLIT_ISSUE_ENABLED = bool(config.get("split_issue_episode", True))', init)


if __name__ == '__main__':
    unittest.main()
