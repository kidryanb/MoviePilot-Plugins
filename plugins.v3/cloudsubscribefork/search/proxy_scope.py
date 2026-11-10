"""按渠道决定是否走搜索代理：局域网地址自动直连，也可以只让指定渠道走代理。"""

import ipaddress
from typing import Any, Iterable, Mapping, Optional
from urllib.parse import urlparse

#: 配置项：走搜索代理的渠道；留空表示所有非局域网渠道都走代理。
PROXY_SOURCES_KEY = "search_proxy_sources"
_LAN_SUFFIXES = (".local", ".lan", ".home", ".internal", ".localdomain", ".home.arpa")


def is_lan_url(url: Any) -> bool:
    """局域网、本机或容器内地址（私有 IP、localhost、.local、不带点的主机名）。"""
    text = str(url or "").strip()
    if not text:
        return False
    if "://" not in text:
        text = f"http://{text}"
    host = (urlparse(text).hostname or "").strip().lower()
    if not host:
        return False
    if host == "localhost" or host.endswith(_LAN_SUFFIXES):
        return True
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return "." not in host
    return address.is_private or address.is_loopback or address.is_link_local


def source_base_url(source_id: str, config: Mapping[str, Any]) -> str:
    """渠道配置里的服务地址（各渠道键名略有不同）。"""
    for suffix in ("_url", "_base_url", "_sync_url"):
        value = str(config.get(f"{source_id}{suffix}") or "").strip()
        if value:
            return value
    return ""


def _selected_sources(config: Mapping[str, Any]) -> set:
    raw = config.get(PROXY_SOURCES_KEY) or []
    if isinstance(raw, str):
        raw = [part for part in raw.replace("，", ",").split(",")]
    items: Iterable[Any] = raw if isinstance(raw, (list, tuple, set)) else []
    return {str(item).strip().lower() for item in items if str(item).strip()}


def source_proxy(source_id: str, config: Mapping[str, Any], proxy: Any) -> Optional[Any]:
    """某个渠道实际使用的代理；不该走代理时返回 None。"""
    if not proxy:
        return None
    source = str(source_id or "").strip().lower()
    selected = _selected_sources(config)
    if selected and source not in selected:
        return None
    if is_lan_url(source_base_url(source, config)):
        return None
    return proxy


def owner_source_proxy(owner: Any, source_id: str) -> Optional[Any]:
    """从搜索处理器属性（``_search_proxy`` 等）推算某个渠道的代理。"""
    source = str(source_id or "").strip().lower()
    config = {PROXY_SOURCES_KEY: getattr(owner, f"_{PROXY_SOURCES_KEY}", None)}
    for suffix in ("_url", "_base_url", "_sync_url"):
        config[f"{source}{suffix}"] = getattr(owner, f"_{source}{suffix}", "")
    return source_proxy(source, config, getattr(owner, "_search_proxy", None))
