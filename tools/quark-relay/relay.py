"""
quark-relay：夸克 STRM 的服务器中转。

把「网盘订阅助手（改版）」生成的夸克 STRM 播放流量从家里的 NAS 挪到服务器上：

    播放器 ──> 本服务  /quark/<file_id>/<签名>?/<文件名>
    本服务 ──> NAS 上的 MP 插件  /relay/link（只取直链，几 KB）
    本服务 ──> 夸克直链（带 Cookie 分段拉流）──> 播放器

只依赖 Python 标准库。环境变量：

    RELAY_SECRET     必填，和插件「服务器中转密钥」相同，用来校验 STRM 链接签名
    MP_URL           服务器能访问到的 MP 地址，例如 https://mp.example.com
    MP_INTERNAL_URL  可选，内网或回源地址，例如 http://127.0.0.1:3000；取直链优先走它，
                     连不上时退回 MP_URL。MP_URL 和 MP_INTERNAL_URL 至少填一个
    MP_APIKEY        必填，MP 的 API Token（设置 → 安全 → API 令牌）
    PLUGIN_ID        默认 CloudSubscribeFork
    LISTEN_HOST      默认 0.0.0.0
    LISTEN_PORT      默认 8080
    PATH_PREFIX      反向代理挂在子路径时填写，例如 /q；默认空
    LINK_TTL         直链里读不到过期时间时的缓存秒数，默认 600
    LINK_EXPIRY_MARGIN  按直链过期时间缓存时提前作废的秒数，默认 60
    CHUNK_MB         每段拉取大小（MB），默认 4
    LOG_LEVEL        默认 INFO；DEBUG 时每个播放请求记一行耗时（不含 Cookie、令牌、签名和直链）

本文件与插件同为 GPL-3.0。
"""

from __future__ import annotations

import calendar
import hashlib
import hmac
import json
import logging
import os
import re
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, List, Mapping, Optional, Tuple
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qsl, unquote, urlencode, urlparse
from urllib.request import Request, urlopen

__version__ = "1.1.0"

LOG = logging.getLogger("quark-relay")
_CONTENT_RANGE = re.compile(r"bytes (\d+)-(\d+)/(\d+)")
_RANGE = re.compile(r"bytes=(\d*)-(\d*)$", re.IGNORECASE)
_SAFE_KEY = re.compile(r"^[a-z0-9_-]+$")
_PASS_HEADERS = ("content-type", "etag", "last-modified")
#: 上游拒绝这些状态时，视为直链失效：作废缓存、重新取一次
_EXPIRED_STATUS = (401, 403, 404, 410)
#: 内网地址连不上后，这么多秒内直接走 MP_URL
_INTERNAL_BACKOFF = 60.0
#: 按直链过期时间缓存时的上限，防止异常时间戳把坏链接留太久
_MAX_LINK_TTL = 24 * 3600
_LOG_LEVELS = {"DEBUG": logging.DEBUG, "INFO": logging.INFO, "WARNING": logging.WARNING,
               "WARN": logging.WARNING, "ERROR": logging.ERROR}


def signature(secret: str, drive: str, identifier: str) -> str:
    """与插件 StrmGenerator.relay_signature 相同的签名算法。"""
    message = f"{drive.strip().lower()}:{identifier.strip()}".encode("utf-8")
    return hmac.new(secret.encode("utf-8"), message, hashlib.sha256).hexdigest()[:32]


def _int_env(env: Mapping[str, str], name: str, default: int) -> int:
    try:
        return int(str(env.get(name, "") or default).strip())
    except ValueError:
        return default


def _base_url(value: Any) -> str:
    text = str(value or "").strip().rstrip("/")
    return text if re.match(r"^https?://[^/\s]+", text) else ""


def mask_id(identifier: str) -> str:
    """日志里只露文件标识首尾几位。"""
    text = str(identifier or "")
    return text if len(text) <= 10 else f"{text[:4]}…{text[-4:]}"


def safe_origin(url: str) -> str:
    """只保留 scheme://host[:port]，去掉账号、路径和查询串。"""
    parsed = urlparse(str(url or ""))
    if not parsed.scheme or not parsed.hostname:
        return "-"
    port = f":{parsed.port}" if parsed.port else ""
    return f"{parsed.scheme}://{parsed.hostname}{port}"


def redact_path(path: str) -> str:
    """请求路径脱敏：去掉查询串，签名段与长标识打码。"""
    raw = urlparse(str(path or "")).path
    parts = raw.split("/")
    for index, part in enumerate(parts):
        if re.fullmatch(r"[0-9a-fA-F]{16,}", part):
            parts[index] = "***"
        elif len(part) > 10:
            parts[index] = mask_id(part)
    return "/".join(parts)


def redact_text(text: str) -> str:
    """任意日志文本脱敏：去掉查询串，长十六进制串（签名）打码。"""
    text = re.sub(r"\?\S*", "?…", str(text or ""))
    return re.sub(r"[0-9a-fA-F]{16,}", "***", text)


def link_expiry(url: str, data: Optional[Mapping[str, Any]] = None,
                now: Optional[float] = None) -> Optional[float]:
    """
    直链过期时间（Unix 秒）。依次看 MP 返回的 expires_at / expires_in，
    再看直链查询串里的 Expires、x-oss-expires、X-Amz-Date+X-Amz-Expires、auth_key；都读不到返回 None。
    """
    now = time.time() if now is None else now
    data = data or {}

    def plausible(value: float) -> Optional[float]:
        # 一天前到一周后之外的时间戳当作读错了
        return value if now - 86400 <= value <= now + 7 * 86400 else None

    try:
        if data.get("expires_at"):
            return plausible(float(data["expires_at"]))
        if data.get("expires_in"):
            return now + float(data["expires_in"])
    except (TypeError, ValueError):
        pass
    query = {k.lower(): v for k, v in parse_qsl(urlparse(str(url or "")).query, keep_blank_values=True)}
    for name in ("expires", "x-oss-expires"):
        if query.get(name, "").isdigit():
            return plausible(float(query[name]))
    if query.get("x-amz-expires", "").isdigit() and query.get("x-amz-date"):
        try:
            signed = calendar.timegm(time.strptime(query["x-amz-date"], "%Y%m%dT%H%M%SZ"))
            return plausible(signed + float(query["x-amz-expires"]))
        except ValueError:
            pass
    auth_key = query.get("auth_key", "")
    if re.match(r"^\d{10}-", auth_key):
        return plausible(float(auth_key.split("-", 1)[0]))
    return None


class RelayConfig:
    def __init__(self, env: Optional[Dict[str, str]] = None):
        env = dict(os.environ if env is None else env)
        self.secret = env.get("RELAY_SECRET", "")
        self.mp_url = _base_url(env.get("MP_URL", ""))
        self.mp_internal_url = _base_url(env.get("MP_INTERNAL_URL", ""))
        self.mp_apikey = env.get("MP_APIKEY", "")
        self.plugin_id = env.get("PLUGIN_ID", "CloudSubscribeFork")
        self.host = env.get("LISTEN_HOST", "0.0.0.0")
        self.port = _int_env(env, "LISTEN_PORT", 8080)
        self.prefix = "/" + env.get("PATH_PREFIX", "").strip("/") if env.get("PATH_PREFIX", "").strip("/") else ""
        self.link_ttl = max(30, _int_env(env, "LINK_TTL", 600))
        self.expiry_margin = max(0, _int_env(env, "LINK_EXPIRY_MARGIN", 60))
        self.chunk = max(1, _int_env(env, "CHUNK_MB", 4)) * 1024 * 1024
        level = str(env.get("LOG_LEVEL", "INFO") or "INFO").strip().upper()
        self.log_level = _LOG_LEVELS.get(level, logging.INFO)

    @property
    def link_bases(self) -> List[str]:
        """取直链的 MP 地址，内网优先，去重。"""
        bases: List[str] = []
        for base in (self.mp_internal_url, self.mp_url):
            if base and base not in bases:
                bases.append(base)
        return bases

    def validate(self) -> None:
        missing = [name for name, value in (
            ("RELAY_SECRET", self.secret), ("MP_URL 或 MP_INTERNAL_URL", self.link_bases),
            ("MP_APIKEY", self.mp_apikey),
        ) if not value]
        if missing:
            raise SystemExit(f"缺少环境变量：{', '.join(missing)}")


class LinkFetchError(LookupError):
    """向 MP 取直链失败；消息里不含地址和令牌。"""


class LinkCache:
    """
    按 (网盘, 文件标识) 缓存直链。

    有效期取直链过期时间减去安全余量，读不到过期时间时用 LINK_TTL。
    同一个文件同时只向 MP 取一次（播放器起播常并发发几个 Range 请求）。
    """

    _MAX_ITEMS = 512

    def __init__(self, config: RelayConfig):
        self._config = config
        self._lock = threading.Lock()
        # key -> (到期的 monotonic 时间, 直链, 请求头)
        self._items: Dict[Tuple[str, str], Tuple[float, str, Dict[str, str]]] = {}
        self._key_locks: Dict[Tuple[str, str], threading.Lock] = {}
        self._internal_down_until = 0.0
        self.mp_calls = 0

    def _key_lock(self, key: Tuple[str, str]) -> threading.Lock:
        with self._lock:
            lock = self._key_locks.get(key)
            if lock is None:
                lock = self._key_locks[key] = threading.Lock()
            return lock

    def _cached(self, key: Tuple[str, str]) -> Optional[Tuple[str, Dict[str, str]]]:
        with self._lock:
            item = self._items.get(key)
            if item and time.monotonic() < item[0]:
                return item[1], item[2]
        return None

    def get(self, drive: str, identifier: str, sig: str) -> Tuple[str, Dict[str, str], bool]:
        """返回 (直链, 请求头, 是否命中缓存)。"""
        key = (drive, identifier)
        hit = self._cached(key)
        if hit:
            return hit[0], hit[1], True
        with self._key_lock(key):
            hit = self._cached(key)  # 等锁期间别的请求已经取好了
            if hit:
                return hit[0], hit[1], True
            url, headers, ttl = self._fetch(drive, identifier, sig)
            with self._lock:
                if ttl > 0:
                    self._items[key] = (time.monotonic() + ttl, url, headers)
                if len(self._items) > self._MAX_ITEMS:
                    oldest = sorted(self._items.items(), key=lambda item: item[1][0])[:128]
                    for old_key, _ in oldest:
                        self._items.pop(old_key, None)
                        self._key_locks.pop(old_key, None)
        return url, headers, False

    def invalidate(self, drive: str, identifier: str, stale_url: Optional[str] = None) -> None:
        """作废缓存；给了 stale_url 时，只有缓存的还是这条旧链接才作废（别的请求可能已经续上）。"""
        with self._lock:
            item = self._items.get((drive, identifier))
            if item and (stale_url is None or item[1] == stale_url):
                self._items.pop((drive, identifier), None)

    def ttl_for(self, url: str, data: Mapping[str, Any]) -> float:
        expires = link_expiry(url, data)
        if expires is None:
            return float(self._config.link_ttl)
        return max(0.0, min(expires - time.time() - self._config.expiry_margin, _MAX_LINK_TTL))

    def _fetch(self, drive: str, identifier: str, sig: str) -> Tuple[str, Dict[str, str], float]:
        query = urlencode({
            "drive": drive, "file_id": identifier, "sig": sig, "apikey": self._config.mp_apikey,
        })
        bases = self._config.link_bases
        internal = self._config.mp_internal_url
        if internal and len(bases) > 1 and time.monotonic() < self._internal_down_until:
            bases = [b for b in bases if b != internal]
        payload: Dict[str, Any] = {}
        for index, base in enumerate(bases):
            url = f"{base}/api/v1/plugin/{self._config.plugin_id}/relay/link?{query}"
            self.mp_calls += 1
            try:
                with urlopen(Request(url, headers={"Accept": "application/json"}),
                             timeout=10 if index + 1 < len(bases) else 20) as response:
                    payload = json.loads(response.read().decode("utf-8") or "{}")
                break
            except HTTPError as error:
                # MP 有响应（令牌错、插件报错），换地址也没用
                raise LinkFetchError(f"MP 返回 HTTP {error.code}") from None
            except (URLError, OSError, ValueError) as error:
                if base == internal:
                    self._internal_down_until = time.monotonic() + _INTERNAL_BACKOFF
                if index + 1 < len(bases):
                    LOG.warning("内网取链连不上 %s（%s），改走 MP_URL", safe_origin(base), type(error).__name__)
                    continue
                raise LinkFetchError(f"连不上 MP {safe_origin(base)}：{type(error).__name__}") from None
        if not payload.get("success"):
            raise LinkFetchError(str(payload.get("message") or "MP 未返回直链")[:100])
        data = payload.get("data") or {}
        link = str(data.get("url") or "")
        if not link:
            raise LinkFetchError("MP 未返回直链")
        headers = {
            str(k): str(v) for k, v in (data.get("headers") or {}).items()
            if str(k).lower() not in {"host", "content-length", "accept-encoding", "range", "if-range"}
        }
        return link, headers, self.ttl_for(link, data)


def _split_path(path: str, prefix: str) -> Optional[List[str]]:
    raw = unquote(urlparse(path).path)
    if prefix:
        if not (raw == prefix or raw.startswith(prefix + "/")):
            return None
        raw = raw[len(prefix):]
    return [part for part in raw.split("/") if part]


def parse_target(path: str, prefix: str) -> Optional[Tuple[str, str, str]]:
    """``/quark/<file_id>/<sig>`` → (网盘, 文件标识, 签名)；``?/文件名`` 只给播放器看，忽略。"""
    parts = _split_path(path, prefix)
    if not parts or len(parts) != 3:
        return None
    drive, identifier, sig = parts[0].lower(), parts[1], parts[2].lower()
    if not _SAFE_KEY.match(drive) or not identifier:
        return None
    return drive, identifier, sig


def parse_prefetch(path: str, prefix: str) -> Optional[Tuple[str, str, str]]:
    """``/prefetch/quark/<file_id>/<sig>``：只把直链取进缓存，不拉流。"""
    parts = _split_path(path, prefix)
    if not parts or len(parts) != 4 or parts[0].lower() != "prefetch":
        return None
    return parse_target("/" + "/".join(parts[1:]), "")


def parse_range(value: str) -> Optional[Tuple[Optional[int], Optional[int]]]:
    """解析单段 Range；不支持或格式不对时返回 None（按整文件处理）。"""
    match = _RANGE.match(str(value or "").strip())
    if not match or (not match.group(1) and not match.group(2)):
        return None
    start = int(match.group(1)) if match.group(1) else None
    end = int(match.group(2)) if match.group(2) else None
    return start, end


class RequestStats:
    """单个播放请求的耗时与流量，DEBUG 时记一行（不含 Cookie、令牌、签名和直链）。"""

    def __init__(self, method: str):
        self.method = method
        self.started = time.monotonic()
        self.target = "-"
        self.range = "-"
        self.status = 0
        self.link_ms: Optional[float] = None
        self.cache = "-"
        self.ttfb_ms: Optional[float] = None
        self.bytes = 0
        self.renewed = 0
        self.note = ""

    def mark_first_byte(self) -> None:
        if self.ttfb_ms is None:
            self.ttfb_ms = (time.monotonic() - self.started) * 1000

    def line(self) -> str:
        def ms(value: Optional[float]) -> str:
            return "-" if value is None else f"{value:.0f}ms"

        total = (time.monotonic() - self.started) * 1000
        text = (f"{self.method} {self.target} range={self.range} status={self.status} "
                f"cache={self.cache} link={ms(self.link_ms)} ttfb={ms(self.ttfb_ms)} "
                f"bytes={self.bytes} total={total:.0f}ms renewed={self.renewed}")
        return f"{text} {self.note}".rstrip()


class RelayHandler(BaseHTTPRequestHandler):
    server_version = f"quark-relay/{__version__}"
    protocol_version = "HTTP/1.1"

    config: RelayConfig
    links: LinkCache

    def do_HEAD(self) -> None:
        self._run(head=True)

    def do_GET(self) -> None:
        self._run(head=False)

    def do_POST(self) -> None:
        if parse_prefetch(self.path, self.config.prefix):
            return self._run(head=False)
        self._error(405, "method not allowed")

    def log_message(self, fmt: str, *args) -> None:
        # 默认访问日志带完整路径（含签名），这里统一脱敏
        LOG.debug("%s - %s", self.address_string(), redact_text(fmt % args))

    def log_request(self, code: Any = "-", size: Any = "-") -> None:
        pass  # 逐请求日志由 RequestStats 记录

    def _error(self, code: int, message: str) -> None:
        stats = getattr(self, "_stats", None)
        if stats is not None:
            stats.status = code
            stats.note = message
        body = message.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _upstream(self, url: str, headers: Dict[str, str], start: int, end: Optional[int]):
        request_headers = dict(headers)
        request_headers["Accept-Encoding"] = "identity"
        request_headers["Range"] = f"bytes={start}-{'' if end is None else end}"
        try:
            return urlopen(Request(url, headers=request_headers), timeout=30)
        except HTTPError as error:
            return error

    def _link(self, drive: str, identifier: str, sig: str) -> Tuple[str, Dict[str, str]]:
        began = time.monotonic()
        try:
            url, headers, hit = self.links.get(drive, identifier, sig)
        finally:
            spent = (time.monotonic() - began) * 1000
            self._stats.link_ms = (self._stats.link_ms or 0) + spent
        if self._stats.cache == "-":
            self._stats.cache = "hit" if hit else "miss"
        return url, headers

    def _renew(self, drive: str, identifier: str, sig: str, stale_url: str):
        """直链被拒：作废缓存并重新取一次（每个请求只续一次）。"""
        if self._stats.renewed:
            return None
        self._stats.renewed = 1
        self.links.invalidate(drive, identifier, stale_url)
        try:
            return self._link(drive, identifier, sig)
        except Exception as error:  # noqa: BLE001
            LOG.warning("重新取链失败 %s/%s：%s", drive, mask_id(identifier), error)
            return None

    def _run(self, head: bool) -> None:
        self._stats = RequestStats(self.command)
        self._stats.target = redact_path(self.path)
        self._stats.range = (self.headers.get("Range", "") or "-")[:40]
        try:
            prefetch = parse_prefetch(self.path, self.config.prefix)
            if prefetch:
                self._prefetch(*prefetch)
            else:
                self._serve(head)
        finally:
            if LOG.isEnabledFor(logging.DEBUG):
                LOG.debug("请求 %s", self._stats.line())

    def _prefetch(self, drive: str, identifier: str, sig: str) -> None:
        if not hmac.compare_digest(signature(self.config.secret, drive, identifier), sig):
            return self._error(403, "bad signature")
        try:
            self._link(drive, identifier, sig)
        except Exception as error:  # noqa: BLE001
            LOG.warning("预取直链失败 %s/%s：%s", drive, mask_id(identifier), error)
            return self._error(502, "link unavailable")
        self._stats.status = 204
        self.send_response(204)
        self.send_header("Content-Length", "0")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()

    def _serve(self, head: bool) -> None:
        target = parse_target(self.path, self.config.prefix)
        if not target:
            return self._error(404, "not found")
        drive, identifier, sig = target
        self._stats.target = f"{drive}/{mask_id(identifier)}"
        if not hmac.compare_digest(signature(self.config.secret, drive, identifier), sig):
            return self._error(403, "bad signature")
        requested = parse_range(self.headers.get("Range", ""))
        try:
            url, headers = self._link(drive, identifier, sig)
        except Exception as error:  # noqa: BLE001 - 取链失败统一 502
            LOG.warning("取链失败 %s/%s：%s", drive, mask_id(identifier), error)
            return self._error(502, "link unavailable")

        # 先用一个小范围探测文件大小与类型；直链过期（403/410 等）时作废缓存重新取一次
        start = requested[0] if requested and requested[0] is not None else 0
        probe_end = start + self.config.chunk - 1
        upstream = self._upstream(url, headers, start, probe_end)
        if upstream.status in _EXPIRED_STATUS:
            upstream.close()
            renewed = self._renew(drive, identifier, sig, url)
            if not renewed:
                return self._error(502, "link unavailable")
            url, headers = renewed
            upstream = self._upstream(url, headers, start, probe_end)
        match = _CONTENT_RANGE.fullmatch(upstream.headers.get("Content-Range", "") or "")
        if upstream.status != 206 or not match:
            status = upstream.status
            upstream.close()
            if status in _EXPIRED_STATUS:
                self.links.invalidate(drive, identifier, url)
            if status == 416:
                return self._error(416, "range not satisfiable")
            LOG.warning("上游未返回分段响应 %s/%s：HTTP %s", drive, mask_id(identifier), status)
            return self._error(502, "upstream error")
        total = int(match.group(3))

        if requested and requested[0] is None:
            # bytes=-N：最后 N 字节
            start = max(0, total - (requested[1] or 0))
            end = total - 1
            upstream.close()
            upstream = self._upstream(url, headers, start, min(start + self.config.chunk - 1, end))
        else:
            end = total - 1
            if requested and requested[1] is not None:
                end = min(requested[1], total - 1)
        if start > end:
            upstream.close()
            return self._error(416, "range not satisfiable")

        self._stats.status = 206 if requested else 200
        self.send_response(self._stats.status)
        for name in _PASS_HEADERS:
            value = upstream.headers.get(name)
            if value:
                self.send_header(name.title(), value)
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(end - start + 1))
        if requested:
            self.send_header("Content-Range", f"bytes {start}-{end}/{total}")
        self.end_headers()
        if head:
            self._stats.mark_first_byte()
            upstream.close()
            return

        position = start
        try:
            while position <= end:
                part_end = min(position + self.config.chunk - 1, end)
                if upstream is None:
                    fetched = self._next_part(drive, identifier, sig, url, headers, position, part_end)
                    if fetched is None:
                        return
                    upstream, url, headers = fetched
                remaining = part_end - position + 1
                while remaining > 0:
                    data = upstream.read(min(256 * 1024, remaining))
                    if not data:
                        LOG.warning("上游提前结束 %s/%s", drive, mask_id(identifier))
                        self._stats.note = "upstream ended early"
                        return
                    self.wfile.write(data)
                    self._stats.mark_first_byte()
                    self._stats.bytes += len(data)
                    remaining -= len(data)
                    position += len(data)
                upstream.close()
                upstream = None
        except (BrokenPipeError, ConnectionResetError):
            self._stats.note = "client closed"  # 播放器拖动或关闭
        finally:
            if upstream is not None:
                upstream.close()

    def _next_part(self, drive: str, identifier: str, sig: str, url: str, headers: Dict[str, str],
                   start: int, end: int):
        """拉下一段；播放途中直链过期时续一次再拉，播放器不会断。"""
        for attempt in range(2):
            upstream = self._upstream(url, headers, start, end)
            part = _CONTENT_RANGE.fullmatch(upstream.headers.get("Content-Range", "") or "")
            if upstream.status == 206 and part and int(part.group(1)) == start:
                return upstream, url, headers
            status = upstream.status
            upstream.close()
            if attempt == 0 and status in _EXPIRED_STATUS:
                renewed = self._renew(drive, identifier, sig, url)
                if renewed:
                    url, headers = renewed
                    continue
            LOG.warning("分段响应异常 %s/%s：HTTP %s", drive, mask_id(identifier), status)
            self._stats.note = f"part HTTP {status}"
            return None
        return None


def build_server(config: RelayConfig) -> ThreadingHTTPServer:
    handler = type("Handler", (RelayHandler,), {"config": config, "links": LinkCache(config)})
    server = ThreadingHTTPServer((config.host, config.port), handler)
    server.daemon_threads = True
    return server


def main() -> None:
    config = RelayConfig()
    logging.basicConfig(level=config.log_level, format="%(asctime)s %(levelname)s %(message)s")
    config.validate()
    server = build_server(config)
    LOG.info("quark-relay %s 已启动：%s:%s%s，取链地址=%s，日志级别=%s", __version__, config.host, config.port,
             config.prefix or "", " → ".join(safe_origin(b) for b in config.link_bases),
             logging.getLevelName(config.log_level))
    server.serve_forever()


if __name__ == "__main__":
    main()
