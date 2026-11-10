"""媒体文件树展开与季集标记解析。"""

import datetime
import re
from pathlib import Path
from typing import Any, Iterator, Optional, Set, Tuple


class MediaFileParser:
    """文件结构和命名解析"""

    DEFAULT_VIDEO_EXTENSIONS = frozenset({".mkv", ".mp4", ".avi", ".iso"})
    DEFAULT_SUBTITLE_EXTENSIONS = frozenset({
        ".srt", ".ass", ".ssa", ".vtt", ".sub", ".sup", ".idx", ".smi", ".mks",
    })
    VIDEO_EXTENSIONS = set(DEFAULT_VIDEO_EXTENSIONS)
    SUBTITLE_EXTENSIONS = set(DEFAULT_SUBTITLE_EXTENSIONS)

    _SXEX_PATTERN = re.compile(r"[Ss](\d{1,2})[Ee](\d{1,4})")
    _SEASON_EPISODE_PATTERN = re.compile(r"[Ss](\d{1,2})[Ee]")
    _CN_SEASON_PATTERN = re.compile(r"第\s*([0-9零一二两三四五六七八九十]{1,4})\s*季")
    _EN_SEASON_PATTERN = re.compile(r"[Ss]eason[\s._-]*(\d{1,2})(?!\d)", re.IGNORECASE)
    #: 独立的 S2 / S02（后面不跟 E 集号），前面不能是字母数字，避免误伤 MP4S2 这类字符串
    _BARE_SEASON_PATTERN = re.compile(r"(?<![A-Za-z0-9])[Ss](\d{1,2})(?![0-9EeA-Za-z])")
    ANY_SEASON_PATTERN = re.compile(
        r"[Ss]\d+[Ee]|第\s*[0-9零一二两三四五六七八九十]{1,4}\s*季|[Ss]eason[\s._-]*\d+"
        r"|(?<![A-Za-z0-9])[Ss]\d{1,2}(?![0-9EeA-Za-z])",
        re.IGNORECASE,
    )
    #: 文件名日期：20241130、2025.12.06、2025-12-06、2026.11.1、2026年11月8日
    _COMPACT_DATE_PATTERN = re.compile(r"(?<!\d)((?:19|20)\d{2})(\d{2})(\d{2})(?!\d)")
    _SEPARATED_DATE_PATTERN = re.compile(
        r"(?<!\d)((?:19|20)\d{2})\s*[.\-_/年]\s*(\d{1,2})\s*[.\-_/月]\s*(\d{1,2})(?!\d)"
    )
    _YEAR_PATTERN = re.compile(r"(?<!\d)((?:19|20)\d{2})(?!\d)")
    #: 只有「第N期」、没有上/下
    _BARE_ISSUE_PATTERN = re.compile(r"第\s*([0-9零一二两三四五六七八九十]{1,4})\s*期")
    #: 综艺「第N期上/下」：TMDB 通常把每期拆成两集，上 = 2N-1，下 = 2N。
    SPLIT_ISSUE_ENABLED = True
    _SPLIT_ISSUE_PATTERN = re.compile(
        r"第\s*([0-9零一二两三四五六七八九十]{1,4})\s*期\s*"
        r"[\(（\[【]?\s*([上下])\s*(?:集|篇|半场|半)?\s*[\)）\]】]?"
    )
    _CN_DIGITS = {"零": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4,
                  "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}

    @classmethod
    def configure_extensions(
            cls,
            video_extensions: Optional[Any] = None,
            subtitle_extensions: Optional[Any] = None,
    ) -> Tuple[set, set]:
        """动态配置视频与字幕扩展名，自动标准化带点小写格式。"""
        if video_extensions is not None:
            cls.VIDEO_EXTENSIONS = cls._normalize_extensions(
                video_extensions, cls.DEFAULT_VIDEO_EXTENSIONS
            )
        if subtitle_extensions is not None:
            cls.SUBTITLE_EXTENSIONS = cls._normalize_extensions(
                subtitle_extensions, cls.DEFAULT_SUBTITLE_EXTENSIONS
            )
        return set(cls.VIDEO_EXTENSIONS), set(cls.SUBTITLE_EXTENSIONS)

    @staticmethod
    def _normalize_extensions(raw: Any, default_set: Any) -> set:
        if isinstance(raw, str):
            parts = re.split(r"[,，\n\s]+", raw)
        elif isinstance(raw, (list, set, tuple)):
            parts = list(raw)
        else:
            return set(default_set)
        result = set()
        for item in parts:
            text = str(item or "").strip().lower()
            if not text:
                continue
            if not text.startswith("."):
                text = f".{text}"
            result.add(text)
        return result or set(default_set)

    @classmethod
    def is_video(cls, file_name: str) -> bool:
        ext = Path(str(file_name or "")).suffix.lower()
        if not ext:
            return False
        return ext in cls.VIDEO_EXTENSIONS or ext.lstrip(".") in cls.VIDEO_EXTENSIONS

    @classmethod
    def is_subtitle(cls, file_name: str) -> bool:
        ext = Path(str(file_name or "")).suffix.lower()
        if not ext:
            return False
        return ext in cls.SUBTITLE_EXTENSIONS or ext.lstrip(".") in cls.SUBTITLE_EXTENSIONS

    @classmethod
    def season_numbers(cls, text: str) -> Set[int]:
        """文本里出现的所有季号：S04E11、第四季、第4季、Season 4、独立的 S4/S04。"""
        value = str(text or "")
        seasons: Set[int] = set()
        for match in cls._SEASON_EPISODE_PATTERN.finditer(value):
            seasons.add(int(match.group(1)))
        for match in cls._CN_SEASON_PATTERN.finditer(value):
            number = cls._issue_number(match.group(1))
            if number:
                seasons.add(number)
        for pattern in (cls._EN_SEASON_PATTERN, cls._BARE_SEASON_PATTERN):
            for match in pattern.finditer(value):
                seasons.add(int(match.group(1)))
        seasons.discard(0)
        return seasons

    @classmethod
    def contains_other_season(cls, file_name: str, target_season: int) -> bool:
        """写了季号，但其中没有目标季。"""
        seasons = cls.season_numbers(file_name)
        return bool(seasons) and int(target_season) not in seasons

    @classmethod
    def matches_target_season(cls, file_name: str, target_season: int) -> bool:
        return int(target_season) in cls.season_numbers(file_name)

    @classmethod
    def extract_date(cls, text: str) -> Optional[datetime.date]:
        """文件名里的播出日期前缀（第一个合法日期）。"""
        value = str(text or "")
        found = []
        for pattern in (cls._SEPARATED_DATE_PATTERN, cls._COMPACT_DATE_PATTERN):
            for match in pattern.finditer(value):
                try:
                    parsed = datetime.date(
                        int(match.group(1)), int(match.group(2)), int(match.group(3))
                    )
                except ValueError:
                    continue
                found.append((match.start(), parsed))
        return min(found)[1] if found else None

    @classmethod
    def extract_years(cls, text: str) -> Set[int]:
        """文本里独立出现的年份（含日期里的年份，不含 2160p 这类数字）。"""
        value = str(text or "")
        years = {int(match.group(1)) for match in cls._YEAR_PATTERN.finditer(value)}
        date = cls.extract_date(value)
        if date:
            years.add(date.year)
        return years

    @classmethod
    def bare_issue_number(cls, file_name: str) -> Optional[int]:
        """只有「第N期」、没有上/下标记时返回 N；有上/下或没有期号时返回 None。"""
        text = str(file_name or "")
        if "期" not in text or cls._SPLIT_ISSUE_PATTERN.search(text):
            return None
        match = cls._BARE_ISSUE_PATTERN.search(text)
        if not match:
            return None
        return cls._issue_number(match.group(1))

    @classmethod
    def _issue_number(cls, text: str) -> Optional[int]:
        if text.isdigit():
            return int(text)
        if "十" in text:
            head, _, tail = text.partition("十")
            tens = cls._CN_DIGITS.get(head, 0) if head else 1
            ones = cls._CN_DIGITS.get(tail, 0) if tail else 0
            if (head and head not in cls._CN_DIGITS) or (tail and tail not in cls._CN_DIGITS):
                return None
            return tens * 10 + ones
        if len(text) == 1 and text in cls._CN_DIGITS:
            return cls._CN_DIGITS[text]
        return None

    @classmethod
    def split_issue_episode(cls, file_name: str) -> Optional[int]:
        """「第N期上」→ 2N-1，「第N期下」→ 2N；没有上/下标记或已关闭时返回 None。"""
        if not cls.SPLIT_ISSUE_ENABLED:
            return None
        match = cls._SPLIT_ISSUE_PATTERN.search(str(file_name or ""))
        if not match:
            return None
        issue = cls._issue_number(match.group(1))
        if not issue or issue <= 0:
            return None
        return issue * 2 - (1 if match.group(2) == "上" else 0)

    @classmethod
    def normalize_split_issue(cls, file_name: str) -> str:
        """把「第N期上/下」改写成 E 集号，交给平台识别前统一调用。"""
        text = str(file_name or "")
        if not cls.SPLIT_ISSUE_ENABLED or "期" not in text:
            return text

        def replace(match: "re.Match") -> str:
            issue = cls._issue_number(match.group(1))
            if not issue or issue <= 0:
                return match.group(0)
            episode = issue * 2 - (1 if match.group(2) == "上" else 0)
            return f" E{episode:02d} "

        return cls._SPLIT_ISSUE_PATTERN.sub(replace, text)

    @classmethod
    def extract_season_episode(
            cls, file_name: str
    ) -> Optional[Tuple[int, int]]:
        match = cls._SXEX_PATTERN.search(file_name)
        if not match:
            return None
        return int(match.group(1)), int(match.group(2))

    @classmethod
    def iter_files(cls, files: list, parent_path: str = "") -> Iterator[Any]:
        for item in files or []:
            name = str(item.get("name") or item.get("file_name") or "").strip()
            relative_path = "/".join(
                value for value in (parent_path.strip("/"), name) if value
            )
            if item.get("is_dir"):
                yield from cls.iter_files(
                    item.get("children") or [], relative_path
                )
            else:
                if relative_path and isinstance(item, dict):
                    item.setdefault("_relative_path", relative_path)
                    if parent_path:
                        item.setdefault("parent_path", parent_path)
                    yield item
                elif relative_path:
                    enriched = dict(item)
                    enriched["_relative_path"] = relative_path
                    if parent_path:
                        enriched["parent_path"] = parent_path
                    yield enriched
                else:
                    if isinstance(item, dict) and item.get("relative_path"):
                        item.setdefault("_relative_path", item["relative_path"])
                    yield item
