"""离线资源黑名单：带 TTL 的持久化键值表，插件重载 / MoviePilot 重启 / 版本切换后仍然有效。

存储为插件数据目录下的一个 JSON 文件。键只保存规范指纹（``magnet:HASH``、``ed2k:HASH``）
或其 SHA1 摘要，不落盘任何带签名、token 的原始链接。
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Callable, Dict, Iterator, Optional, Union

_PLAIN_KEY = re.compile(
    r"^(?:(?:magnet|ed2k):)?(?:[0-9A-F]{40}|[0-9A-F]{32}|[2-7A-Z]{32})$", re.IGNORECASE
)


def storage_key(key: Any) -> str:
    """把黑名单键转成可落盘形式：hash 类键原样，其余（URL、任意文本）取 SHA1 摘要。"""
    text = str(key or "").strip()
    if not text:
        return ""
    upper = text.upper()
    if _PLAIN_KEY.match(upper):
        prefix, sep, rest = text.partition(":")
        return f"{prefix.lower()}:{rest.upper()}" if sep else upper
    return "sha1:" + hashlib.sha1(text.encode("utf-8")).hexdigest()


class PersistentTTLStore:
    """线程安全、按条目时间戳淘汰的小型持久化表；接口与 dict / TTLCache 的常用部分一致。

    多个实例指向同一文件时，每次读写前按文件修改时间重新加载，彼此可见。
    path 为空时退化为纯内存表（单测或宿主没有数据目录时）。
    """

    def __init__(
            self,
            path: Optional[Union[str, Path]],
            ttl: float = 86400,
            maxsize: int = 10000,
            clock: Callable[[], float] = time.time,
    ):
        self._path = Path(path) if path else None
        self._ttl = max(1.0, float(ttl or 1))
        self._maxsize = max(1, int(maxsize or 1))
        self._clock = clock
        self._lock = threading.RLock()
        self._data: Dict[str, Dict[str, Any]] = {}
        self._loaded_mtime: Optional[tuple] = None

    # ---- 持久层 ----

    def _reload_locked(self) -> None:
        if not self._path:
            return
        try:
            stat = self._path.stat()
            mtime = (stat.st_mtime_ns, stat.st_size)
        except OSError:
            if self._loaded_mtime is not None:
                self._data = {}
                self._loaded_mtime = None
            return
        if self._loaded_mtime == mtime:
            return
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8") or "{}")
        except (OSError, ValueError):
            raw = {}
        entries = raw.get("entries") if isinstance(raw, dict) else None
        data: Dict[str, Dict[str, Any]] = {}
        for key, value in (entries or {}).items():
            if not isinstance(value, dict):
                continue
            try:
                value = {**value, "time": float(value.get("time") or 0)}
            except (TypeError, ValueError):
                continue
            data[str(key)] = value
        self._data = data
        self._loaded_mtime = mtime

    def _save_locked(self) -> None:
        if not self._path:
            return
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(
                prefix=f".{self._path.name}.", dir=str(self._path.parent)
            )
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as handle:
                    json.dump({"version": 1, "entries": self._data}, handle, ensure_ascii=False)
                os.replace(tmp, self._path)
            except Exception:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
                raise
            stat = self._path.stat()
            self._loaded_mtime = (stat.st_mtime_ns, stat.st_size)
        except OSError:
            # 写盘失败时内存表仍然生效，下一次写入再尝试落盘
            self._loaded_mtime = None

    def _purge_locked(self) -> bool:
        deadline = self._clock() - self._ttl
        expired = [key for key, value in self._data.items() if float(value.get("time") or 0) <= deadline]
        for key in expired:
            self._data.pop(key, None)
        overflow = len(self._data) - self._maxsize
        if overflow > 0:
            for key, _ in sorted(self._data.items(), key=lambda kv: float(kv[1].get("time") or 0))[:overflow]:
                self._data.pop(key, None)
        return bool(expired) or overflow > 0

    # ---- dict 接口 ----

    def __contains__(self, key: Any) -> bool:
        return self.get(key) is not None

    def get(self, key: Any, default: Any = None) -> Any:
        skey = storage_key(key)
        if not skey:
            return default
        with self._lock:
            self._reload_locked()
            value = self._data.get(skey)
            if value is None:
                return default
            if float(value.get("time") or 0) <= self._clock() - self._ttl:
                return default
            return dict(value)

    def __getitem__(self, key: Any) -> Dict[str, Any]:
        value = self.get(key)
        if value is None:
            raise KeyError(key)
        return value

    def __setitem__(self, key: Any, value: Any) -> None:
        skey = storage_key(key)
        if not skey:
            return
        entry = dict(value) if isinstance(value, dict) else {"reason": str(value or "")}
        # 过期一律按本表时钟起算，调用方传入的时间只作参考
        entry["time"] = float(self._clock())
        entry["reason"] = str(entry.get("reason") or "")[:200]
        with self._lock:
            self._reload_locked()
            self._data[skey] = entry
            self._purge_locked()
            self._save_locked()

    def pop(self, key: Any, default: Any = None) -> Any:
        skey = storage_key(key)
        if not skey:
            return default
        with self._lock:
            self._reload_locked()
            value = self._data.pop(skey, None)
            if value is None:
                return default
            self._save_locked()
            return value

    def __len__(self) -> int:
        with self._lock:
            self._reload_locked()
            if self._purge_locked():
                self._save_locked()
            return len(self._data)

    def __iter__(self) -> Iterator[str]:
        with self._lock:
            self._reload_locked()
            self._purge_locked()
            return iter(list(self._data))

    def clear(self) -> None:
        with self._lock:
            self._data = {}
            self._save_locked()
