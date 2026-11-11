"""新入库 STRM 条目的 Emby 媒体信息预读。

MP 把 STRM 整理进媒体库（TransferComplete）后，逐条找到对应的 Emby 条目，
调用一次单条 PlaybackInfo 让 Emby 提前分析媒体流，避免第一次播放时现场 ffprobe。

约束：
- 只针对单个条目，不发任何 Refresh / 全库扫描请求，不改元数据；
- 并发 1，两条之间至少间隔 ``min_interval`` 秒；
- 失败的条目进入下一轮，最多 ``max_attempts`` 次；
- 以条目 MediaStreams 非空作为成功依据；
- 日志不出现 API Key、Cookie 或签名 URL（只记路径和条目 ID）。

本模块不依赖 MoviePilot，HTTP 与服务器列表由调用方注入，便于离线测试。
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from pathlib import PurePosixPath
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple

#: (method, url, params, headers, timeout) -> (status_code, json_body)
HttpCall = Callable[[str, str, Dict[str, Any], Dict[str, str], float], Tuple[Optional[int], Any]]


@dataclass(frozen=True)
class EmbyServer:
    name: str
    host: str
    api_key: str
    user_id: str


@dataclass
class PrefillTarget:
    """一条待预读的媒体文件（MP 侧路径）。"""

    path: str
    tmdb_id: Optional[str] = None
    title: str = ""
    attempts: int = 0
    next_at: float = 0.0
    last_error: str = ""
    item_ids: Dict[str, str] = field(default_factory=dict)


def _get(obj: Any, *names: str) -> Any:
    for name in names:
        if obj is None:
            return None
        value = obj.get(name) if isinstance(obj, dict) else getattr(obj, name, None)
        if value is not None:
            return value
    return None


def _norm(path: Any) -> str:
    text = str(path or "").strip().replace("\\", "/")
    return text.rstrip("/") or ("/" if text else "")


def _under(path: str, root: str) -> bool:
    return path == root or path.startswith(f"{root}/")


def parse_source_roots(value: Any) -> List[str]:
    """设置项「只预读这些来源目录」：每行一个，留空表示所有 STRM。"""
    if isinstance(value, (list, tuple)):
        lines = value
    else:
        lines = str(value or "").splitlines()
    roots = []
    for line in lines:
        root = _norm(str(line).split("#", 1)[0])
        if root and root not in roots:
            roots.append(root)
    return roots


def map_to_server_path(path: str, mappings: str) -> str:
    """按「媒体服务器路径#本地路径」映射，取最长前缀；没有命中时原样返回。"""
    source = _norm(path)
    best = None
    for line in str(mappings or "").splitlines():
        parts = [part.strip() for part in line.split("#", 1)]
        if len(parts) != 2 or not all(parts):
            continue
        server_root, local_root = _norm(parts[0]), _norm(parts[1])
        if _under(source, local_root) and (best is None or len(local_root) > len(best[1])):
            best = (server_root, local_root)
    if not best:
        return source
    relative = source[len(best[1]):].lstrip("/")
    return f"{best[0]}/{relative}" if relative else best[0]


def targets_from_transfer_event(event_data: Any, source_roots: Iterable[str] = ()) -> List[PrefillTarget]:
    """从 MP 的 TransferComplete 事件里取出新整理进媒体库的 STRM 文件。

    只认来源是 .strm 的整理结果；配置了来源目录时，来源必须在其中之一下面。
    """
    if not event_data:
        return []
    transferinfo = _get(event_data, "transferinfo")
    if transferinfo is None or _get(transferinfo, "success") is False:
        return []
    source = _norm(_get(_get(event_data, "fileitem"), "path")
                   or _get(_get(transferinfo, "fileitem"), "path"))
    roots = [_norm(root) for root in source_roots if _norm(root)]
    if roots and not (source and any(_under(source, root) for root in roots)):
        return []

    candidates: List[str] = []
    for path in _get(transferinfo, "file_list_new") or []:
        candidates.append(_norm(path))
    target_item = _get(transferinfo, "target_item")
    if target_item is not None:
        candidates.append(_norm(_get(target_item, "path")))
    paths = [path for path in dict.fromkeys(candidates)
             if path and PurePosixPath(path).suffix.lower() == ".strm"]
    if not paths and source and not source.lower().endswith(".strm"):
        return []

    mediainfo = _get(event_data, "mediainfo")
    tmdb_id = _get(mediainfo, "tmdb_id", "tmdbid")
    title = str(_get(mediainfo, "title") or "")
    return [PrefillTarget(path=path, tmdb_id=str(tmdb_id) if tmdb_id else None, title=title)
            for path in paths]


def _streams_of(item: Dict[str, Any]) -> List[Any]:
    streams = item.get("MediaStreams") or []
    if streams:
        return list(streams)
    for source in item.get("MediaSources") or []:
        if isinstance(source, dict) and source.get("MediaStreams"):
            return list(source["MediaStreams"])
    return []


class MediaInfoPrefiller:
    """单线程队列：逐条请求 PlaybackInfo，并核对 MediaStreams。"""

    MAX_QUEUE = 500

    def __init__(
            self,
            http: HttpCall,
            servers: Callable[[], List[EmbyServer]],
            path_mappings: str = "",
            source_roots: Iterable[str] = (),
            min_interval: float = 2.0,
            initial_delay: float = 60.0,
            retry_delays: Tuple[float, ...] = (180.0, 600.0),
            max_attempts: int = 3,
            logger: Optional[logging.Logger] = None,
            clock: Callable[[], float] = time.monotonic,
            sleep: Optional[Callable[[float], None]] = None,
            background: bool = True,
    ):
        """background=False 时不启后台线程，只由 run_due 同步处理（单测用，避免与后台线程抢同一条）。"""
        self._background = bool(background)
        self._http = http
        self._servers = servers
        self.path_mappings = str(path_mappings or "")
        self.source_roots = parse_source_roots(list(source_roots))
        self.min_interval = max(2.0, float(min_interval))
        self.initial_delay = max(0.0, float(initial_delay))
        self.retry_delays = tuple(retry_delays) or (180.0,)
        self.max_attempts = max(1, int(max_attempts))
        self._log = logger or logging.getLogger(__name__)
        self._clock = clock
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._sleep = sleep or (lambda seconds: self._stop.wait(seconds))
        self._lock = threading.Lock()
        # 后台线程与 run_due（手动触发）串行取条目并处理，同一条不会被两边同时预读
        self._process_lock = threading.Lock()
        self._queue: Dict[str, PrefillTarget] = {}
        self._thread: Optional[threading.Thread] = None
        self._last_request_at: Optional[float] = None
        self.stats = {"queued": 0, "succeeded": 0, "already": 0, "failed": 0, "gave_up": 0}

    # ---------- 入队 ----------

    def configure(self, path_mappings: str = "", source_roots: Iterable[str] = ()) -> None:
        self.path_mappings = str(path_mappings or "")
        self.source_roots = parse_source_roots(list(source_roots))

    def enqueue_transfer(self, event_data: Any) -> int:
        return self.enqueue(targets_from_transfer_event(event_data, self.source_roots))

    def enqueue(self, targets: Iterable[PrefillTarget]) -> int:
        added = 0
        now = self._clock()
        with self._lock:
            if self._stop.is_set():
                return 0
            for target in targets:
                if target.path in self._queue:
                    continue
                if len(self._queue) >= self.MAX_QUEUE:
                    self._log.warning(f"媒体信息预读：队列已满，跳过 {target.path}")
                    break
                target.next_at = now + self.initial_delay
                self._queue[target.path] = target
                added += 1
            self.stats["queued"] += added
        if added:
            self._log.info(f"媒体信息预读：新入库 {added} 条，{self.initial_delay:.0f} 秒后开始")
            self._ensure_worker()
            self._wake.set()
        return added

    def pending(self) -> List[PrefillTarget]:
        with self._lock:
            return list(self._queue.values())

    # ---------- 后台线程 ----------

    def _ensure_worker(self) -> None:
        if not self._background:
            return
        with self._lock:
            if self._thread and self._thread.is_alive():
                return
            self._thread = threading.Thread(
                target=self._run, name="cloudsubscribe-mediainfo-prefill", daemon=True
            )
            self._thread.start()

    def close(self) -> None:
        self._stop.set()
        self._wake.set()
        with self._lock:
            self._queue.clear()

    def _next_due(self) -> Tuple[Optional[PrefillTarget], float]:
        with self._lock:
            if not self._queue:
                return None, 3600.0
            target = min(self._queue.values(), key=lambda value: value.next_at)
            wait = target.next_at - self._clock()
            return (target, 0.0) if wait <= 0 else (None, wait)

    def _run(self) -> None:
        while not self._stop.is_set():
            with self._process_lock:
                target, wait = self._next_due()
                if target is not None:
                    self.run_once(target)
                    continue
            self._wake.wait(min(wait, 3600.0))
            self._wake.clear()

    def run_due(self) -> int:
        """同步处理当前到期的全部条目（测试与手动触发用）。"""
        handled = 0
        while not self._stop.is_set():
            with self._process_lock:
                target, _ = self._next_due()
                if target is None:
                    return handled
                self.run_once(target)
            handled += 1
        return handled

    def run_once(self, target: PrefillTarget) -> bool:
        ok = False
        try:
            ok = self._process(target)
        except Exception as error:  # noqa: BLE001 - 后台线程不能被单条异常打断
            target.last_error = type(error).__name__
            self._log.warning(f"媒体信息预读异常：{target.path}（{type(error).__name__}）")
        with self._lock:
            if self._queue.get(target.path) is not target:
                return ok
            if ok:
                self._queue.pop(target.path, None)
                return True
            target.attempts += 1
            self.stats["failed"] += 1
            if target.attempts >= self.max_attempts:
                self._queue.pop(target.path, None)
                self.stats["gave_up"] += 1
                self._log.warning(
                    f"媒体信息预读放弃：{target.path}，已试 {target.attempts} 次"
                    f"（{target.last_error or '未知原因'}），首次播放时由 Emby 现场分析")
                return False
            delay = self.retry_delays[min(target.attempts - 1, len(self.retry_delays) - 1)]
            target.next_at = self._clock() + delay
            self._log.info(f"媒体信息预读未完成：{target.path}（{target.last_error}），"
                           f"{delay:.0f} 秒后第 {target.attempts + 1} 次重试")
        return False

    # ---------- 单条处理 ----------

    def _throttle(self) -> None:
        if self._last_request_at is not None:
            wait = self.min_interval - (self._clock() - self._last_request_at)
            if wait > 0:
                self._sleep(wait)
        self._last_request_at = self._clock()

    def _call(self, method: str, server: EmbyServer, path: str, params: Dict[str, Any],
              timeout: float = 15.0) -> Tuple[Optional[int], Any]:
        headers = {"X-Emby-Token": server.api_key, "Accept": "application/json"}
        return self._http(method, f"{server.host}/emby{path}", dict(params), headers, timeout)

    def _process(self, target: PrefillTarget) -> bool:
        servers = list(self._servers() or [])
        if not servers:
            target.last_error = "没有可用的 Emby 服务器"
            return False
        server_path = map_to_server_path(target.path, self.path_mappings)
        done = 0
        for server in servers:
            if self._process_on(server, target, server_path):
                done += 1
        return done == len(servers)

    def _process_on(self, server: EmbyServer, target: PrefillTarget, server_path: str) -> bool:
        item = None
        item_id = target.item_ids.get(server.name)
        if item_id:
            item = self._get_item(server, item_id)
        if not item:
            item = self._find_item(server, target, server_path)
        if not item:
            target.last_error = f"{server.name} 中还没有这一条"
            return False
        item_id = str(item.get("Id"))
        target.item_ids[server.name] = item_id
        if _streams_of(item):
            self.stats["already"] += 1
            self._log.debug(f"媒体信息预读：{server_path} 已有媒体信息，跳过 (Item {item_id})")
            return True

        self._throttle()
        started = self._clock()
        status, body = self._call("POST", server, f"/Items/{item_id}/PlaybackInfo", {
            "UserId": server.user_id,
            "IsPlayback": "true",
            "AutoOpenLiveStream": "true",
            "MaxStreamingBitrate": 200_000_000,
        }, timeout=60.0)
        elapsed = self._clock() - started
        sources = body.get("MediaSources") if isinstance(body, dict) else None
        for source in sources or []:
            live_id = source.get("LiveStreamId") if isinstance(source, dict) else None
            if live_id:
                self._call("POST", server, "/LiveStreams/Close", {"LiveStreamId": live_id})
        if status != 200:
            target.last_error = f"PlaybackInfo HTTP {status}"
            return False

        item = self._get_item(server, item_id)
        if item and _streams_of(item):
            self.stats["succeeded"] += 1
            self._log.info(f"媒体信息预读完成：{server_path} (Item {item_id}，{elapsed:.1f} 秒)")
            return True
        target.last_error = "PlaybackInfo 后 MediaStreams 仍为空"
        return False

    _ITEM_FIELDS = "Path,MediaStreams,MediaSources,ProviderIds"

    def _items(self, server: EmbyServer, params: Dict[str, Any]) -> List[Dict[str, Any]]:
        status, body = self._call("GET", server, "/Items", params)
        if status != 200 or not isinstance(body, dict):
            return []
        return [item for item in body.get("Items") or [] if isinstance(item, dict) and item.get("Id")]

    def _get_item(self, server: EmbyServer, item_id: str) -> Optional[Dict[str, Any]]:
        items = self._items(server, {"Ids": item_id, "Fields": self._ITEM_FIELDS})
        return items[0] if items else None

    def _find_item(self, server: EmbyServer, target: PrefillTarget, server_path: str
                   ) -> Optional[Dict[str, Any]]:
        """按 TMDB ID（没有时按标题）找到剧或电影，再在其下按路径精确匹配。"""
        if target.tmdb_id:
            parents = self._items(server, {
                "AnyProviderIdEquals": f"tmdb.{target.tmdb_id}",
                "IncludeItemTypes": "Movie,Series",
                "Recursive": "true",
                "Fields": self._ITEM_FIELDS,
            })
        elif target.title:
            parents = self._items(server, {
                "SearchTerm": target.title,
                "IncludeItemTypes": "Movie,Series",
                "Recursive": "true",
                "Limit": 5,
                "Fields": self._ITEM_FIELDS,
            })
        else:
            return None
        for parent in parents:
            if parent.get("Type") != "Series":
                if _norm(parent.get("Path")) == server_path:
                    return parent
                continue
            for episode in self._items(server, {
                "ParentId": parent["Id"],
                "IncludeItemTypes": "Episode",
                "Recursive": "true",
                "Fields": self._ITEM_FIELDS,
            }):
                if _norm(episode.get("Path")) == server_path:
                    return episode
        return None
