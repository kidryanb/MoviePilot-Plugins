"""按分享类型选择目标网盘（M1「同盘优先」）。

上游只有一个「当前转存网盘」(``cloud_drive``)，所有转存、整理、STRM 都围绕它。
本模块只做纯规则：从配置得出兜底顺序与可作为同盘目标的网盘，再按分享类型决定
一条资源落到哪个盘。真正切换转存上下文的是 ``SyncHandler.drive_scope``。
"""

from __future__ import annotations

from typing import Any, Iterable, List, Optional, Sequence

#: 默认兜底顺序；只有出现在这个列表里且已登录的网盘才会被当成同盘目标
DEFAULT_PREFERRED_DRIVES = ("115", "quark")


def _clean_keys(values: Iterable[Any]) -> List[str]:
    return list(dict.fromkeys(
        str(value).strip().lower() for value in values if str(value or "").strip()
    ))


def normalize_preferred_drives(value: Any, legacy_cloud_drive: Any = None) -> List[str]:
    """
    读取有序的 ``preferred_drives``，兼容只有旧 ``cloud_drive`` 的配置。

    :param value: 配置中的 ``preferred_drives``（列表或逗号分隔字符串）
    :param legacy_cloud_drive: 旧配置 ``cloud_drive``，存在时放在最前
    :return: 去重后的网盘标识列表，至少包含默认值
    """
    if isinstance(value, str):
        value = value.split(",")
    keys = _clean_keys(value or [])
    legacy = str(legacy_cloud_drive or "").strip().lower()
    if not keys:
        keys = list(DEFAULT_PREFERRED_DRIVES)
        if legacy:
            keys = _clean_keys([legacy, *keys])
    return keys


def pick_default_drive(preferred: Sequence[str], ready: Iterable[str], fallback: str = "115") -> str:
    """
    选出「默认转存网盘」：兜底顺序里第一个已就绪的盘。

    只登录了夸克时，默认盘就是夸克，不会再停在未登录的 115 上。

    :param preferred: 兜底顺序
    :param ready: 已注册（凭据有效）的网盘标识
    :param fallback: 都没就绪时沿用的标识
    :return: 默认网盘标识
    """
    ready_set = {str(key).lower() for key in ready}
    for key in preferred:
        if key in ready_set:
            return key
    return str(preferred[0] if preferred else fallback).lower()


def same_cloud_targets(
        preferred: Sequence[str],
        ready_share_drives: Iterable[str],
        same_cloud_first: bool,
) -> List[str]:
    """
    可作为同盘目标的网盘：已登录、支持分享转存、且在兜底顺序里。

    :param preferred: 兜底顺序
    :param ready_share_drives: 已注册且支持分享转存的网盘标识
    :param same_cloud_first: 是否开启同盘优先
    :return: 按兜底顺序排列的目标盘
    """
    if not same_cloud_first:
        return []
    ready_set = {str(key).lower() for key in ready_share_drives}
    return [key for key in preferred if key in ready_set]


def route_share(
        source_key: Optional[str],
        default_key: Optional[str],
        targets: Sequence[str],
) -> Optional[str]:
    """
    决定一条分享资源落到哪个盘。

    分享所属网盘本身是同盘目标时落到它自己；否则返回 None，表示沿用默认盘
    （默认盘能直接接住，或者交给已有的跨盘逻辑处理）。

    :param source_key: 分享所属网盘，例如 115 分享为 ``115``、夸克分享为 ``quark``
    :param default_key: 默认转存网盘
    :param targets: ``same_cloud_targets`` 的结果
    :return: 需要切换到的网盘标识；无需切换时为 None
    """
    key = str(source_key or "").strip().lower()
    if not key or key == str(default_key or "").strip().lower():
        return None
    return key if key in targets else None


def is_offline_link(url: Any) -> bool:
    """磁力或 ed2k 链接（需要网盘离线下载，而不是分享转存）。"""
    text = str(url or "").strip().lower()
    return text.startswith("magnet:") or text.startswith("ed2k://")


def route_offline(
        default_key: Optional[str],
        default_has_offline: bool,
        offline_targets: Sequence[str],
) -> Optional[str]:
    """
    决定磁力 / ed2k 交给哪个盘离线下载。

    默认盘能离线就留在默认盘；否则按兜底顺序交给第一个支持离线下载的同盘目标。
    夸克、阿里、UC、天翼没有离线能力，不会出现在 ``offline_targets`` 里。

    :param default_key: 默认转存网盘
    :param default_has_offline: 默认盘是否支持离线下载
    :param offline_targets: 支持离线下载的同盘目标，按兜底顺序
    :return: 需要切换到的网盘标识；留在默认盘时为 None
    """
    if default_has_offline:
        return None
    default = str(default_key or "").strip().lower()
    for key in offline_targets:
        normalized = str(key or "").strip().lower()
        if normalized and normalized != default:
            return normalized
    return None


def relay_sources(ready_download_drives: Iterable[str], targets: Iterable[str]) -> List[str]:
    """
    跨盘中转来源：已登录、能转存分享并下载文件、但不是转存目标的网盘。

    :param ready_download_drives: 已登录且支持分享转存与文件下载的网盘
    :param targets: 转存目标（默认盘与同盘目标）
    :return: 中转来源，保持传入顺序
    """
    target_set = {str(key).strip().lower() for key in targets}
    return [key for key in _clean_keys(ready_download_drives) if key not in target_set]


#: 不代表文件已落盘的历史状态
_NOT_LANDED_STATUSES = frozenset({"失败", "转存失败", "已删除"})


def landed_drive_key(
        records: Iterable[dict],
        tmdb_id: Any = None,
        title: Any = None,
        season: Any = None,
        is_movie: bool = False,
) -> Optional[str]:
    """
    从插件转存历史里找出一部影片（剧集按季）最近一次落到的网盘。

    :param records: 插件转存历史
    :param tmdb_id: 影片 TMDB ID，优先用它匹配
    :param title: 没有 TMDB ID 时按标题匹配
    :param season: 剧集季号（电影忽略）
    :param is_movie: 是否电影
    :return: 网盘标识；没有可用记录时为 None
    """
    def as_int(value: Any) -> int:
        try:
            return int(value or 0)
        except (TypeError, ValueError):
            return 0

    want_tmdb = as_int(tmdb_id)
    want_title = str(title or "").strip()
    want_season = as_int(season) or 1
    best_time, best_key = "", None
    for record in records or []:
        if not isinstance(record, dict):
            continue
        if str(record.get("status") or "") in _NOT_LANDED_STATUSES:
            continue
        if want_tmdb:
            if as_int(record.get("tmdb_id")) != want_tmdb:
                continue
        elif not want_title or str(record.get("title") or "").strip() != want_title:
            continue
        record_is_movie = str(record.get("type") or "") == "电影"
        if record_is_movie != bool(is_movie):
            continue
        if not is_movie and (as_int(record.get("season")) or 1) != want_season:
            continue
        key = str(record.get("target_drive_key") or record.get("cloud_drive_key") or "").strip().lower()
        if not key:
            continue
        stamp = str(record.get("time") or "")
        if best_key is None or stamp >= best_time:
            best_time, best_key = stamp, key
    return best_key


def search_resource_types(default_types: Iterable[str], target_types: Iterable[Iterable[str]]) -> frozenset:
    """
    订阅搜索允许的分享类型：默认盘支持的类型并上所有同盘目标支持的类型。

    :param default_types: 默认盘的 ``resource_types``
    :param target_types: 各同盘目标的 ``resource_types``
    :return: 允许的类型集合
    """
    allowed = set(default_types or ())
    for types in target_types:
        allowed.update(types or ())
    return frozenset(allowed)


def extend_resource_order(order: Sequence[str], drive_keys: Iterable[str]) -> List[str]:
    """
    把同盘目标补进资源类型优先级（缺失时追加到末尾，不打乱用户已排好的顺序）。

    :param order: 用户配置的 ``resource_type_order``
    :param drive_keys: 同盘目标
    :return: 新的优先级列表
    """
    result = list(order or [])
    for key in drive_keys:
        if key not in result:
            result.append(key)
    return result
