"""与本仓库 DownloadCloudUpload 及 P115StrmHelper 共存的保护规则。

本 fork 与「下载完成自动上传」(DownloadCloudUpload) 装在同一个 MoviePilot 里：
下载器任务上传 115 由 DownloadCloudUpload 负责，本插件只做网盘分享转存。
这里集中放置让两者互不干扰的规则，避免散落在各业务模块中。
"""

from __future__ import annotations

from http.cookies import CookieError, SimpleCookie
from pathlib import PurePosixPath
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple

DOWNLOAD_UPLOAD_PLUGIN_ID = "DownloadCloudUpload"
STRM_HELPER_PLUGIN_ID = "P115StrmHelper"

#: 115 Cookie 来源：复用 P115StrmHelper（默认）或使用本插件自己的扫码/填写结果
COOKIE_SOURCE_KEY = "p115_cookie_source"
COOKIE_SOURCE_STRM_HELPER = "strm_helper"
COOKIE_SOURCE_OWN = "own"
COOKIE_SOURCES = (COOKIE_SOURCE_STRM_HELPER, COOKIE_SOURCE_OWN)

#: 共存模式下固定的配置值。PT 洗版上传会与 DownloadCloudUpload 重复上传同一批文件。
FORCED_VALUES: Dict[str, Any] = {
    "enable_pt_upgrade": False,
}

#: 新装默认值：不拦截平台下载，不自己写 115 STRM。订阅筛选模式的默认值随
#: 「始终接管系统订阅」变化，见 ``default_subscribe_filter_mode``。
COEXIST_DEFAULTS: Dict[str, Any] = {
    "platform_download_policy": "allow",
    "strm_generate_enabled": False,
    COOKIE_SOURCE_KEY: COOKIE_SOURCE_STRM_HELPER,
}


#: 订阅筛选模式：处理所有订阅 / 仅处理所选订阅 / 排除所选订阅
SUBSCRIBE_FILTER_ALL = "all"
SUBSCRIBE_FILTER_INCLUDE = "include"
SUBSCRIBE_FILTER_EXCLUDE = "exclude"
SUBSCRIBE_FILTER_MODES = (SUBSCRIBE_FILTER_ALL, SUBSCRIBE_FILTER_INCLUDE, SUBSCRIBE_FILTER_EXCLUDE)


def default_subscribe_filter_mode(config: Dict[str, Any]) -> str:
    """
    订阅筛选模式的默认值跟随基础设置里的「始终接管系统订阅」。

    始终接管时默认处理所有订阅；否则默认只处理勾选的订阅，没勾选的照常走下载器，
    由「下载完成自动上传」负责。

    :param config: 插件配置
    :return: 默认模式
    """
    return SUBSCRIBE_FILTER_ALL if config.get("block_system_subscribe") else SUBSCRIBE_FILTER_INCLUDE


def normalize_subscribe_filter_mode(config: Dict[str, Any]) -> str:
    """
    规范化订阅筛选模式，原地写回 ``config``。

    - 缺省或未知值：按 ``default_subscribe_filter_mode`` 补齐
    - 已开启「始终接管系统订阅」且当前模式下一个订阅都没勾选：视为处理所有订阅。
      排除模式下空列表本来就等于全部处理；指定模式下空列表会让始终接管什么也不处理，
      与「始终接管」的意图相反。

    :param config: 插件配置，原地修改
    :return: 规范化后的模式
    """
    mode = str(config.get("subscribe_filter_mode") or "").strip().lower()
    if mode not in SUBSCRIBE_FILTER_MODES:
        mode = default_subscribe_filter_mode(config)
    selected_key = {
        SUBSCRIBE_FILTER_INCLUDE: "include_subscribes",
        SUBSCRIBE_FILTER_EXCLUDE: "exclude_subscribes",
    }.get(mode)
    if config.get("block_system_subscribe") and selected_key and not config.get(selected_key):
        mode = SUBSCRIBE_FILTER_ALL
    config["subscribe_filter_mode"] = mode
    return mode


def strm_helper_enabled(reader: Optional[Callable[[str], Dict[str, Any]]] = None) -> bool:
    """
    P115StrmHelper 是否已启用。启用时 115 的 STRM 由它生成，本插件不再为 115 写 STRM。

    :param reader: 读取插件配置的函数，测试时注入
    """
    config = (reader or _default_config_reader)(STRM_HELPER_PLUGIN_ID) or {}
    return bool(config.get("enabled"))


def enforce_forced_values(config: Dict[str, Any]) -> List[str]:
    """
    把共存模式下不允许打开的开关改回固定值。

    :param config: 待应用的插件配置，原地修改
    :return: 被改回的配置键
    """
    changed = []
    for key, value in FORCED_VALUES.items():
        if key in config and config.get(key) != value:
            changed.append(key)
        config[key] = value
    return changed


def normalize_cookie_source(value: Any) -> str:
    """
    规范化 115 Cookie 来源，未知值回退为复用 P115StrmHelper。

    :param value: 配置中的原始值
    :return: ``strm_helper`` 或 ``own``
    """
    text = str(value or "").strip().lower()
    return text if text in COOKIE_SOURCES else COOKIE_SOURCE_STRM_HELPER


def valid_115_cookie(value: Any) -> str:
    """
    校验 115 Cookie 至少包含 UID、CID、SEID，并返回去除首尾空白后的值。

    :param value: Cookie 字符串
    :return: 合法时返回 Cookie，否则返回空字符串
    """
    if not isinstance(value, str):
        return ""
    text = value.strip()
    if not text or "\r" in text or "\n" in text:
        return ""
    try:
        parsed = SimpleCookie(text)
    except CookieError:
        return ""
    if any(key not in parsed or not parsed[key].value for key in ("UID", "CID", "SEID")):
        return ""
    return text


def _default_config_reader(plugin_id: str) -> Dict[str, Any]:
    """通过宿主插件管理器只读获取其他插件配置。"""
    try:
        from app.sdk.plugin import PluginManager

        return PluginManager().get_plugin_config(plugin_id) or {}
    except Exception:
        return {}


def strm_helper_cookie(reader: Optional[Callable[[str], Dict[str, Any]]] = None) -> str:
    """
    只读取 P115StrmHelper 的 ``cookies`` 字段，不复制、不回写对方配置。

    :param reader: 读取插件配置的函数，测试时注入
    :return: 合法 Cookie，取不到时返回空字符串
    """
    config = (reader or _default_config_reader)(STRM_HELPER_PLUGIN_ID) or {}
    return valid_115_cookie(config.get("cookies"))


def _posix(path: Any) -> Optional[PurePosixPath]:
    text = str(path or "").strip().replace("\\", "/")
    if not text:
        return None
    if not text.startswith("/"):
        text = "/" + text
    return PurePosixPath(text)


def upload_targets(reader: Optional[Callable[[str], Dict[str, Any]]] = None) -> List[str]:
    """
    读取 DownloadCloudUpload 已启用规则的 115 目标目录。

    :param reader: 读取插件配置的函数，测试时注入
    :return: 目标目录列表；插件未启用或未安装时为空
    """
    config = (reader or _default_config_reader)(DOWNLOAD_UPLOAD_PLUGIN_ID) or {}
    if not config.get("enabled"):
        return []
    targets = []
    for rule in config.get("rules") or []:
        if isinstance(rule, dict) and rule.get("enabled", True) and rule.get("target"):
            targets.append(str(rule["target"]))
    return targets


def overlapping_targets(transfer_path: Any, targets: Iterable[str]) -> List[str]:
    """
    找出与本插件 115 转存目录重叠的上传目录。

    转存目录等于上传目录、位于上传目录之内，或把上传目录包在里面（根目录 ``/`` 除外）
    都算重叠。重叠时转存进来的文件会被 DownloadCloudUpload 的核对当成它自己的文件。

    :param transfer_path: 本插件的 ``p115_transfer_path``
    :param targets: DownloadCloudUpload 的目标目录
    :return: 发生重叠的目标目录
    """
    mine = _posix(transfer_path)
    if mine is None:
        return []
    root = PurePosixPath("/")
    result = []
    for target in targets:
        other = _posix(target)
        if other is None:
            continue
        if mine == other or mine.is_relative_to(other) or (mine != root and other.is_relative_to(mine)):
            result.append(str(other))
    return result


def resolve_115_cookie(
        config: Dict[str, Any],
        reader: Optional[Callable[[str], Dict[str, Any]]] = None,
) -> Tuple[str, List[str]]:
    """
    按共存规则决定本次运行使用的 115 Cookie。

    :param config: 本插件配置
    :param reader: 读取其他插件配置的函数，测试时注入
    :return: (Cookie，空字符串表示 115 不可用；给用户看的提示)
    """
    notes: List[str] = []
    source = normalize_cookie_source(config.get(COOKIE_SOURCE_KEY))
    if source == COOKIE_SOURCE_STRM_HELPER:
        cookie = strm_helper_cookie(reader)
        if not cookie:
            notes.append("115 Cookie 设为复用 P115StrmHelper，但没有读到有效 Cookie，115 暂不可用")
    else:
        cookie = valid_115_cookie(config.get("cookies"))
        if not cookie and str(config.get("cookies") or "").strip():
            notes.append("本插件填写的 115 Cookie 缺少 UID/CID/SEID，115 暂不可用")
    if not cookie:
        return "", notes
    overlaps = overlapping_targets(config.get("p115_transfer_path", "/"), upload_targets(reader))
    if overlaps:
        notes.append(
            "115 转存目录与「下载完成自动上传」的上传目录重叠（"
            + "、".join(overlaps)
            + "），为避免互相干扰已停用 115，请换一个独立目录"
        )
        return "", notes
    return cookie, notes


def qrcode_blocked(provider: str, config: Dict[str, Any]) -> Optional[str]:
    """
    复用 P115StrmHelper Cookie 时禁止在本插件扫码登录 115，避免把对方会话挤下线。

    :param provider: 扫码的网盘标识
    :param config: 本插件配置
    :return: 被禁止时返回原因，否则 None
    """
    if str(provider or "").strip().lower() != "115":
        return None
    if normalize_cookie_source(config.get(COOKIE_SOURCE_KEY)) == COOKIE_SOURCE_STRM_HELPER:
        return "115 Cookie 正在复用 P115StrmHelper，扫码会让它的登录失效。如需单独登录，请先把 Cookie 来源改为「本插件自己登录」"
    return None
