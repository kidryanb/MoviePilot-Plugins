"""
quark-relay：夸克 STRM 的服务器中转。

把「网盘订阅助手（改版）」生成的夸克 STRM 播放流量从家里的 NAS 挪到服务器上：

    播放器 ──> 本服务  /quark/<file_id>/<签名>?/<文件名>
    本服务 ──> NAS 上的 MP 插件  /relay/link（只取直链，几 KB）
    本服务 ──> 夸克直链（带 Cookie 分段拉流）──> 播放器

只依赖 Python 标准库。环境变量：

    RELAY_SECRET   必填，和插件「服务器中转密钥」相同，用来校验 STRM 链接签名
    MP_URL         必填，服务器能访问到的 MP 地址，例如 http://nas.example.com:3000
    MP_APIKEY      必填，MP 的 API Token（设置 → 安全 → API 令牌）
    PLUGIN_ID      默认 CloudSubscribeFork
    LISTEN_HOST    默认 0.0.0.0
    LISTEN_PORT    默认 8080
    PATH_PREFIX    反向代理挂在子路径时填写，例如 /q；默认空
    LINK_TTL       直链缓存秒数，默认 600
    CHUNK_MB       每段拉取大小（MB），默认 4

本文件与插件同为 GPL-3.0。
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import re
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Dict, Optional, Tuple
from urllib.error import HTTPError
from urllib.parse import unquote, urlencode, urlparse
from urllib.request import Request, urlopen

LOG = logging.getLogger("quark-relay")
_CONTENT_RANGE = re.compile(r"bytes (\d+)-(\d+)/(\d+)")
_RANGE = re.compile(r"bytes=(\d*)-(\d*)$", re.IGNORECASE)
_SAFE_KEY = re.compile(r"^[a-z0-9_-]+$")
_PASS_HEADERS = ("content-type", "etag", "last-modified")


def signature(secret: str, drive: str, identifier: str) -> str:
    """与插件 StrmGenerator.relay_signature 相同的签名算法。"""
    message = f"{drive.strip().lower()}:{identifier.strip()}".encode("utf-8")
    return hmac.new(secret.encode("utf-8"), message, hashlib.sha256).hexdigest()[:32]


class RelayConfig:
    def __init__(self, env: Optional[Dict[str, str]] = None):
        env = dict(os.environ if env is None else env)
        self.secret = env.get("RELAY_SECRET", "")
        self.mp_url = env.get("MP_URL", "").rstrip("/")
        self.mp_apikey = env.get("MP_APIKEY", "")
        self.plugin_id = env.get("PLUGIN_ID", "CloudSubscribeFork")
        self.host = env.get("LISTEN_HOST", "0.0.0.0")
        self.port = int(env.get("LISTEN_PORT", "8080"))
        self.prefix = "/" + env.get("PATH_PREFIX", "").strip("/") if env.get("PATH_PREFIX", "").strip("/") else ""
        self.link_ttl = max(30, int(env.get("LINK_TTL", "600")))
        self.chunk = max(1, int(env.get("CHUNK_MB", "4"))) * 1024 * 1024

    def validate(self) -> None:
        missing = [name for name, value in (
            ("RELAY_SECRET", self.secret), ("MP_URL", self.mp_url), ("MP_APIKEY", self.mp_apikey),
        ) if not value]
        if missing:
            raise SystemExit(f"缺少环境变量：{', '.join(missing)}")


class LinkCache:
    """按 (网盘, 文件标识) 缓存直链，过期或上游拒绝后重新向 MP 取。"""

    def __init__(self, config: RelayConfig):
        self._config = config
        self._lock = threading.Lock()
        self._items: Dict[Tuple[str, str], Tuple[float, str, Dict[str, str]]] = {}

    def get(self, drive: str, identifier: str, sig: str, refresh: bool = False) -> Tuple[str, Dict[str, str]]:
        key = (drive, identifier)
        now = time.monotonic()
        with self._lock:
            cached = self._items.get(key)
            if cached and not refresh and now - cached[0] < self._config.link_ttl:
                return cached[1], cached[2]
        url, headers = self._fetch(drive, identifier, sig)
        with self._lock:
            self._items[key] = (now, url, headers)
            if len(self._items) > 512:
                oldest = sorted(self._items.items(), key=lambda item: item[1][0])[:128]
                for old_key, _ in oldest:
                    self._items.pop(old_key, None)
        return url, headers

    def _fetch(self, drive: str, identifier: str, sig: str) -> Tuple[str, Dict[str, str]]:
        query = urlencode({
            "drive": drive, "file_id": identifier, "sig": sig, "apikey": self._config.mp_apikey,
        })
        url = f"{self._config.mp_url}/api/v1/plugin/{self._config.plugin_id}/relay/link?{query}"
        with urlopen(Request(url, headers={"Accept": "application/json"}), timeout=20) as response:
            payload = json.loads(response.read().decode("utf-8") or "{}")
        if not payload.get("success"):
            raise LookupError(payload.get("message") or "MP 未返回直链")
        data = payload.get("data") or {}
        link = str(data.get("url") or "")
        if not link:
            raise LookupError("MP 未返回直链")
        headers = {
            str(k): str(v) for k, v in (data.get("headers") or {}).items()
            if str(k).lower() not in {"host", "content-length", "accept-encoding", "range", "if-range"}
        }
        return link, headers


def parse_target(path: str, prefix: str) -> Optional[Tuple[str, str, str]]:
    """``/quark/<file_id>/<sig>`` → (网盘, 文件标识, 签名)；``?/文件名`` 只给播放器看，忽略。"""
    raw = unquote(urlparse(path).path)
    if prefix:
        if not (raw == prefix or raw.startswith(prefix + "/")):
            return None
        raw = raw[len(prefix):]
    parts = [part for part in raw.split("/") if part]
    if len(parts) != 3:
        return None
    drive, identifier, sig = parts[0].lower(), parts[1], parts[2].lower()
    if not _SAFE_KEY.match(drive) or not identifier:
        return None
    return drive, identifier, sig


def parse_range(value: str) -> Optional[Tuple[Optional[int], Optional[int]]]:
    """解析单段 Range；不支持或格式不对时返回 None（按整文件处理）。"""
    match = _RANGE.match(str(value or "").strip())
    if not match or (not match.group(1) and not match.group(2)):
        return None
    start = int(match.group(1)) if match.group(1) else None
    end = int(match.group(2)) if match.group(2) else None
    return start, end


class RelayHandler(BaseHTTPRequestHandler):
    server_version = "quark-relay/1.0"
    protocol_version = "HTTP/1.1"

    config: RelayConfig
    links: LinkCache

    def do_HEAD(self) -> None:
        self._serve(head=True)

    def do_GET(self) -> None:
        self._serve(head=False)

    def log_message(self, fmt: str, *args) -> None:
        LOG.debug("%s - %s", self.address_string(), fmt % args)

    def _error(self, code: int, message: str) -> None:
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

    def _serve(self, head: bool) -> None:
        target = parse_target(self.path, self.config.prefix)
        if not target:
            return self._error(404, "not found")
        drive, identifier, sig = target
        if not hmac.compare_digest(signature(self.config.secret, drive, identifier), sig):
            return self._error(403, "bad signature")
        requested = parse_range(self.headers.get("Range", ""))
        try:
            url, headers = self.links.get(drive, identifier, sig)
        except Exception as error:  # noqa: BLE001 - 取链失败统一 502
            LOG.warning("取链失败 %s/%s：%s", drive, identifier, error)
            return self._error(502, "link unavailable")

        # 先用一个小范围探测文件大小与类型；直链过期（403/410）时重新取一次
        start = requested[0] if requested and requested[0] is not None else 0
        probe_end = start + self.config.chunk - 1
        upstream = self._upstream(url, headers, start, probe_end)
        if upstream.status in (401, 403, 404, 410):
            upstream.close()
            try:
                url, headers = self.links.get(drive, identifier, sig, refresh=True)
            except Exception as error:  # noqa: BLE001
                LOG.warning("重新取链失败 %s/%s：%s", drive, identifier, error)
                return self._error(502, "link unavailable")
            upstream = self._upstream(url, headers, start, probe_end)
        match = _CONTENT_RANGE.fullmatch(upstream.headers.get("Content-Range", "") or "")
        if upstream.status != 206 or not match:
            status = upstream.status
            upstream.close()
            if status == 416:
                return self._error(416, "range not satisfiable")
            LOG.warning("上游未返回分段响应 %s/%s：HTTP %s", drive, identifier, status)
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

        self.send_response(206 if requested else 200)
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
            upstream.close()
            return

        position = start
        try:
            while position <= end:
                part_end = min(position + self.config.chunk - 1, end)
                if upstream is None:
                    upstream = self._upstream(url, headers, position, part_end)
                    part = _CONTENT_RANGE.fullmatch(upstream.headers.get("Content-Range", "") or "")
                    if upstream.status != 206 or not part or int(part.group(1)) != position:
                        LOG.warning("分段响应异常 %s/%s：HTTP %s", drive, identifier, upstream.status)
                        return
                remaining = part_end - position + 1
                while remaining > 0:
                    data = upstream.read(min(256 * 1024, remaining))
                    if not data:
                        LOG.warning("上游提前结束 %s/%s", drive, identifier)
                        return
                    self.wfile.write(data)
                    remaining -= len(data)
                    position += len(data)
                upstream.close()
                upstream = None
        except (BrokenPipeError, ConnectionResetError):
            pass  # 播放器拖动或关闭
        finally:
            if upstream is not None:
                upstream.close()


def build_server(config: RelayConfig) -> ThreadingHTTPServer:
    handler = type("Handler", (RelayHandler,), {"config": config, "links": LinkCache(config)})
    server = ThreadingHTTPServer((config.host, config.port), handler)
    server.daemon_threads = True
    return server


def main() -> None:
    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(message)s")
    config = RelayConfig()
    config.validate()
    server = build_server(config)
    LOG.info("quark-relay 已启动：%s:%s%s，MP=%s", config.host, config.port, config.prefix or "", config.mp_url)
    server.serve_forever()


if __name__ == "__main__":
    main()
