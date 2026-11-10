import re
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import unquote

try:
    from app.domain.episode import format_ranges as platform_format_ranges
except Exception:
    platform_format_ranges = None


def extract_ed2k_filename(url: str) -> str:
    """从 ED2K 链接中解析提取原始文件名。"""
    if not url or not str(url).strip().lower().startswith("ed2k://|file|"):
        return ""
    parts = str(url).strip().split("|")
    if len(parts) > 2 and parts[2]:
        return unquote(parts[2]).strip()
    return ""


def candidate_progress(processed: int, total: int, *, start: int, span: int) -> int:
    """候选校验进度：按全局已完成候选线性推进。"""
    processed = max(0, int(processed or 0))
    total = max(processed, int(total or 0))
    if total <= 0:
        return start
    return start + min(span, int(processed / total * span))


# 洗版旧文件备份命名：<原名>-<10 位任务短ID><后缀>
_UPGRADE_BACKUP_RE = re.compile(r"-[A-Za-z0-9]{10}\.[A-Za-z0-9]+$")


def is_upgrade_backup_name(file_name: str) -> bool:
    """判断文件名是否为洗版旧文件备份（<原名>-<10位短ID>.ext）。"""
    return bool(_UPGRADE_BACKUP_RE.search(str(file_name or "")))


def strip_upgrade_backup_suffix(file_name: str) -> str:
    """返回备份名去掉短ID后的原主干；非备份名返回空串。"""
    name = str(file_name or "")
    if not _UPGRADE_BACKUP_RE.search(name):
        return ""
    return re.sub(r"-[A-Za-z0-9]{10}$", "", Path(name).stem)


def format_episode_ranges(episodes: Iterable[Any]) -> str:
    """把集数集合压缩为 E01-E03、E05 形式，优先使用平台原生 format_ranges。"""
    if not episodes:
        return "无"
    numbers = sorted({int(ep) for ep in episodes if str(ep).isdigit() or isinstance(ep, int)})
    if not numbers:
        return "无"
    if platform_format_ranges:
        try:
            result = platform_format_ranges(numbers)
            if result:
                return result
        except Exception:
            pass
    ranges = []
    start = previous = numbers[0]
    for number in numbers[1:]:
        if number == previous + 1:
            previous = number
            continue
        ranges.append(f"E{start:02d}" if start == previous else f"E{start:02d}-E{previous:02d}")
        start = previous = number
    ranges.append(f"E{start:02d}" if start == previous else f"E{start:02d}-E{previous:02d}")
    return "、".join(ranges)


def normalize_season(value: Any, default: int = 1) -> int:
    """规范化季号：None 或空字符串回退默认季号；0 表示特别篇 S00。"""
    try:
        return max(0, int(default if value is None or value == "" else value))
    except (TypeError, ValueError):
        return default


def subscription_missing_episodes(
        expected: Iterable[Any],
        note: Iterable[Any],
        season: int,
        exists: Any = None,
        no_exists: Any = None,
        server_checked: bool = True,
) -> set:
    """
    按 MP 订阅口径算缺集：媒体服务器里没有、订阅也没记为已下载的集。

    :param expected: 订阅范围内的集号
    :param note: 订阅已下载记录
    :param season: 季号
    :param exists: ``DownloadChain.get_no_exists_info`` 的第一个返回值
    :param no_exists: 同上第二个返回值（{媒体: {季: NotExistMediaInfo}}）
    :param server_checked: 媒体服务器查询是否成功；失败时只按 note 计算
    """
    expected_set = {int(value) for value in expected or []}
    note_set = {int(value) for value in note or [] if str(value).isdigit()}
    missing = set(expected_set)
    if server_checked:
        server_missing = set()
        for seasons in (no_exists or {}).values():
            for info in (seasons or {}).values():
                if int(getattr(info, "season", -1) or -1) != int(season):
                    continue
                episodes = getattr(info, "episodes", None) or []
                server_missing.update(
                    expected_set if not episodes else (int(value) for value in episodes)
                )
        if exists and not server_missing:
            missing = set()
        elif exists or no_exists:
            missing = server_missing & expected_set
    return missing - note_set
