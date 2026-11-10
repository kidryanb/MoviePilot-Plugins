"""剧集季归属与播出日校验：拦住旧季文件占位、未播出集被搜索或转存。"""

from __future__ import annotations

import datetime
from dataclasses import dataclass, field, replace
from typing import Dict, Iterable, List, Mapping, Optional, Set, Tuple

from .file_parser import MediaFileParser


def parse_air_date(value) -> Optional[datetime.date]:
    if isinstance(value, datetime.datetime):
        return value.date()
    if isinstance(value, datetime.date):
        return value
    text = str(value or "").strip()[:10]
    if not text:
        return None
    try:
        return datetime.date.fromisoformat(text)
    except ValueError:
        return None


def today_in(tz_name: str = "") -> datetime.date:
    """按插件/系统时区取「今天」，避免 UTC 日期差一天。"""
    try:
        from zoneinfo import ZoneInfo

        if tz_name:
            return datetime.datetime.now(ZoneInfo(tz_name)).date()
    except Exception:
        pass
    return datetime.date.today()


@dataclass(frozen=True)
class SeasonGuard:
    """一个订阅季的校验上下文。所有方法返回空字符串表示放行，否则是拒绝原因。"""

    season: int
    season_year: Optional[int] = None
    show_year: Optional[int] = None
    air_dates: Mapping[int, datetime.date] = field(default_factory=dict)
    today: datetime.date = field(default_factory=datetime.date.today)
    filter_unaired: bool = True
    grace_days: int = 0
    date_tolerance_days: int = 1
    require_season_evidence: bool = True
    share_title: str = ""

    @classmethod
    def build(
            cls,
            season: int,
            season_year=None,
            show_year=None,
            air_dates: Optional[Mapping[int, object]] = None,
            today: Optional[datetime.date] = None,
            **options,
    ) -> "SeasonGuard":
        def as_year(value) -> Optional[int]:
            try:
                year = int(str(value or "").strip()[:4])
            except ValueError:
                return None
            return year if 1900 <= year <= 2100 else None

        parsed = {}
        for episode, value in dict(air_dates or {}).items():
            try:
                parsed[int(episode)] = parse_air_date(value)
            except (TypeError, ValueError):
                continue
        return cls(
            season=int(season or 1),
            season_year=as_year(season_year),
            show_year=as_year(show_year),
            air_dates=parsed,
            today=today or datetime.date.today(),
            **options,
        )

    def with_title(self, title: str) -> "SeasonGuard":
        return replace(self, share_title=str(title or ""))

    # ---- 播出日 ----

    def is_aired(self, episode: int) -> bool:
        if not self.filter_unaired:
            return True
        air_date = self.air_dates.get(int(episode))
        if air_date is None:
            return False
        return air_date <= self.today + datetime.timedelta(days=max(0, int(self.grace_days)))

    def split_aired(self, episodes: Iterable[int]) -> Tuple[List[int], List[int]]:
        aired, unaired = [], []
        for episode in sorted({int(value) for value in episodes}):
            (aired if self.is_aired(episode) else unaired).append(episode)
        return aired, unaired

    def next_air_date(self, episodes: Iterable[int]) -> Optional[datetime.date]:
        dates = [self.air_dates.get(int(e)) for e in episodes]
        dates = [value for value in dates if value]
        return min(dates) if dates else None

    def air_date_strings(self) -> Dict[int, str]:
        return {
            episode: value.isoformat()
            for episode, value in self.air_dates.items() if value
        }

    # ---- 季归属 ----

    def _levels(self, name: str, relative_path: str) -> List[str]:
        """由内到外：文件名、各级目录（最近的在前）。"""
        parts = [part for part in str(relative_path or "").replace("\\", "/").split("/") if part]
        if parts and parts[-1] == name:
            parts = parts[:-1]
        return [name, *reversed(parts)]

    def _years(self, text: str) -> Set[int]:
        """年份线索，节目首播年（与本季年不同时）不算。"""
        years = MediaFileParser.extract_years(text)
        if self.show_year and self.show_year != self.season_year:
            years.discard(self.show_year)
        return years

    def title_reject_reason(self, title: Optional[str] = None) -> str:
        text = self.share_title if title is None else str(title or "")
        seasons = MediaFileParser.season_numbers(text)
        if seasons and self.season not in seasons:
            return f"分享标题是其他季（{text}）"
        return ""

    def file_reject_reason(
            self, name: str, relative_path: str = "", require_evidence: bool = False
    ) -> str:
        """文件本身是否属于目标季：季号 > 日期前缀 > 年份线索 > 分享标题。

        require_evidence：文件名里没有作品名、要按订阅季补季号时传 True，
        此时季号和本季日期都没有就拒绝，不按订阅季猜。
        """
        name = str(name or "")
        levels = self._levels(name, relative_path)
        season_confirmed = False
        for level in levels:
            seasons = MediaFileParser.season_numbers(level)
            if not seasons:
                continue
            if self.season not in seasons:
                where = "文件名" if level is name else f"目录「{level}」"
                return f"{where}是其他季（S{self.season:02d} 以外）"
            season_confirmed = True
            break

        file_date = None
        for level in levels:
            file_date = MediaFileParser.extract_date(level)
            if file_date:
                break
        if file_date and self.season_year and file_date.year != self.season_year:
            return f"日期前缀 {file_date.isoformat()} 不在本季年份 {self.season_year}"

        year_evidence = bool(file_date and self.season_year and file_date.year == self.season_year)
        if not season_confirmed and self.season_year:
            years = set()
            for level in levels:
                years |= self._years(level)
            if years and self.season_year not in years:
                return f"年份 {','.join(map(str, sorted(years)))} 不是本季年份 {self.season_year}"
            if self.season_year in years:
                year_evidence = True

        if not season_confirmed:
            title_seasons = MediaFileParser.season_numbers(self.share_title)
            if title_seasons and self.season not in title_seasons and not year_evidence:
                return self.title_reject_reason()
            if self.season in title_seasons:
                season_confirmed = True
            elif self.season_year and self.season_year in self._years(self.share_title):
                year_evidence = True

        if (
                require_evidence
                and self.require_season_evidence
                and self.season > 1
                and not season_confirmed
                and not year_evidence
        ):
            return "文件名、目录和分享标题都没有季号或本季日期，不按订阅季猜测"
        return ""

    # ---- 集号 ----

    def bare_issue_episode(self, name: str) -> Optional[int]:
        """只有「第N期」没有上/下时，只在日期与某一集播出日完全一致时给出集号。"""
        file_date = MediaFileParser.extract_date(name)
        if not file_date:
            return None
        hits = [episode for episode, value in self.air_dates.items() if value == file_date]
        return hits[0] if len(hits) == 1 else None

    def episode_reject_reason(self, name: str, episode: int, relative_path: str = "") -> str:
        episode = int(episode)
        if not self.is_aired(episode):
            air_date = self.air_dates.get(episode)
            return (
                f"E{episode:02d} 尚未播出（播出日 {air_date.isoformat()}）"
                if air_date else f"E{episode:02d} 没有播出日期，按未播出处理"
            )
        air_date = self.air_dates.get(episode)
        if not air_date:
            return ""
        file_date = None
        for level in self._levels(str(name or ""), relative_path):
            file_date = MediaFileParser.extract_date(level)
            if file_date:
                break
        if file_date and abs((file_date - air_date).days) > max(0, int(self.date_tolerance_days)):
            return (
                f"日期前缀 {file_date.isoformat()} 与 E{episode:02d} 播出日 "
                f"{air_date.isoformat()} 相差超过 {self.date_tolerance_days} 天"
            )
        return ""

    def reject_reason(self, item: Mapping, episode: Optional[int] = None) -> str:
        name = str((item or {}).get("name") or "")
        relative_path = str(
            (item or {}).get("_relative_path") or (item or {}).get("relative_path") or ""
        )
        reason = self.file_reject_reason(name, relative_path)
        if reason or episode is None:
            return reason
        return self.episode_reject_reason(name, episode, relative_path)


def existing_union(*groups: Optional[Iterable[int]]) -> Set[int]:
    result: Set[int] = set()
    for group in groups:
        result |= {int(value) for value in (group or [])}
    return result
