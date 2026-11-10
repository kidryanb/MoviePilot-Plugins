"""SeedHub 反盾协议：配置了外部求解服务（FlareSolverr 等）时先用它取凭证，否则直连优先，
命中 Cloudflare 盾或直连失败时再回退到求解服务 / 平台浏览器仿真。

SeedHub 会对频繁请求返回 Cloudflare 质询；本模块负责：

* 维护浏览器仿真得到的 User-Agent / Cookie，并在后续直连中复用；
* 识别质询响应并按需调起平台浏览器快照（含失效后的冷却）；
* 统一的文本获取入口：重试、盾回退与错误归类。
"""

import threading
import time
from typing import Any, Callable, Dict, Optional
from urllib.parse import urlparse

from ...utils.cloudflare import (
    browser_proxy,
    is_cf_solver_enabled,
    is_cloudflare_challenge,
    playwright_snapshot,
    solve_via_external_solver,
)
from ...utils.http_client import RequestGate, gated_request, requests


#: 外部求解服务失败后，暂停「抢先求解」的秒数。
_SOLVER_FAILURE_COOLDOWN = 300.0


class SeedHubSecurity:
    """SeedHub 请求协议：直连 + 浏览器仿真回退，并复用仿真出的指纹。

    ``error_type`` 由调用方注入渠道错误类型，避免为拆分再造一套异常体系。
    """

    def __init__(
            self,
            *,
            base_url: str,
            proxy: Any = None,
            request_timeout: int = 20,
            headers: Optional[Dict[str, str]] = None,
            proxies: Optional[Any] = None,
            error_type: Callable[..., BaseException],
    ) -> None:
        self.base_url = str(base_url or "").rstrip("/")
        self._proxy = proxy
        self._browser_proxy = browser_proxy(proxy)
        self._request_timeout = max(5, min(int(request_timeout or 20), 60))
        self._headers = dict(headers or {})
        self._proxies = proxies
        self._error_type = error_type
        self._gate: Optional[RequestGate] = None
        self._lock = threading.RLock()
        self._state_version = 0
        self._cookie_header = ""
        self._user_agent = ""
        self._solver_failed_until = 0.0

    def bind_gate(self, gate: RequestGate) -> None:
        """绑定共享请求门控（用于浏览器快照与风控冷却）。"""
        self._gate = gate

    def update_config(
            self,
            base_url: Optional[str] = None,
            proxy: Any = None,
            request_timeout: Optional[int] = None,
            proxies: Optional[Any] = None,
    ) -> None:
        if base_url is not None:
            self.base_url = str(base_url or "").rstrip("/")
        if proxy is not None:
            self._proxy = proxy
            self._browser_proxy = browser_proxy(proxy)
        if request_timeout is not None:
            self._request_timeout = max(5, min(int(request_timeout or 20), 60))
        if proxies is not None:
            self._proxies = proxies

    @staticmethod
    def is_challenge(response: Any) -> bool:
        """统一判断响应是否命中 Cloudflare 质询页（可直接用作门控挑战识别器）。"""
        if response is None:
            return False
        return is_cloudflare_challenge(
            str(getattr(response, "text", "") or ""),
            int(getattr(response, "status_code", 0) or 0),
            getattr(response, "headers", None) or {},
        )

    @property
    def state_version(self) -> int:
        with self._lock:
            return self._state_version

    def request_headers(self) -> Dict[str, str]:
        """基础请求头 + 浏览器仿真得到的 UA / Cookie。"""
        with self._lock:
            headers = dict(self._headers)
            if self._user_agent:
                headers["User-Agent"] = self._user_agent
            if self._cookie_header:
                headers["Cookie"] = self._cookie_header
            return headers

    def request_once(self, url: str):
        return gated_request(
            self._gate,
            requests.get,
            url,
            impersonate="chrome",
            headers=self.request_headers(),
            proxies=self._proxies,
            timeout=(8, self._request_timeout),
            allow_redirects=True,
        )

    def get_text(self, url: str) -> str:
        """带重试与盾回退的页面文本获取。"""
        if is_cf_solver_enabled() and not self._has_session() and not self._solver_cooling():
            # 还没有可复用的过盾凭证：先走外部求解服务，省掉一次注定被拦的直连。
            solver_text = self._solver_text(url, self.state_version)
            if solver_text:
                return solver_text
        last_error = ""
        for attempt in range(2):
            try:
                observed_version = self.state_version
                response = self.request_once(url)
                text = response.text or ""
                if self.is_challenge(response):
                    browser_text = self._browser_text(url, observed_version)
                    if browser_text:
                        return browser_text
                    raise self._error_type("SeedHub 浏览器仿真未通过 Cloudflare 验证")
                if response.status_code == 429 or response.status_code >= 500:
                    last_error = f"HTTP {response.status_code}"
                    if attempt == 0:
                        time.sleep(0.3)
                        continue
                response.raise_for_status()
                return text
            except self._error_type:
                raise
            except requests.exceptions.RequestException as error:
                last_error = type(error).__name__
                if attempt == 0:
                    time.sleep(0.3)
                    continue
        if is_cf_solver_enabled():
            # 直连超时或连续出错时也交给外部求解服务，而不是直接报失败。
            solver_text = self._solver_text(url, self.state_version)
            if solver_text:
                return solver_text
        raise self._error_type(f"SeedHub 请求失败：{last_error or '未知错误'}")

    def _solver_cooling(self) -> bool:
        with self._lock:
            return time.monotonic() < self._solver_failed_until

    def _has_session(self) -> bool:
        with self._lock:
            return bool(self._cookie_header)

    def _store_session(self, cookies: Any, user_agent: str) -> None:
        """把过盾得到的 Cookie / UA 写回后续直连（调用方持锁）。"""
        host = str(urlparse(self.base_url).hostname or "").lower()
        values = [
            f"{cookie.get('name')}={cookie.get('value')}"
            for cookie in (cookies or [])
            if cookie.get("name") and cookie.get("value") is not None
            if (
                    not cookie.get("domain")
                    or host == str(cookie.get("domain")).lstrip(".").lower()
                    or host.endswith(f".{str(cookie.get('domain')).lstrip('.').lower()}")
            )
        ]
        self._cookie_header = "; ".join(values)
        self._user_agent = str(user_agent or "")
        self._state_version += 1

    def _solver_text(self, url: str, observed_version: int) -> str:
        """用外部求解服务（FlareSolverr / Byparr）取页面，并把凭证写回后续直连。"""
        with self._lock:
            if self._state_version != observed_version:
                try:
                    response = self.request_once(url)
                    if response.ok and not self.is_challenge(response):
                        return response.text or ""
                except requests.exceptions.RequestException:
                    pass
            try:
                result = solve_via_external_solver(
                    url, proxy=self._proxy, timeout=max(30, self._request_timeout * 2)
                )
            except Exception:
                result = None
            text = str(result.get("html") or "") if isinstance(result, dict) else ""
            if not text or is_cloudflare_challenge(text):
                # 求解失败后 5 分钟内不再抢先走求解服务，避免每次请求都多等一轮。
                self._solver_failed_until = time.monotonic() + _SOLVER_FAILURE_COOLDOWN
                return ""
            self._solver_failed_until = 0.0
            self._store_session(result.get("cookies"), str(result.get("user_agent") or ""))
            return text

    def _browser_text(self, url: str, observed_version: int) -> str:
        """命中质询：外部求解服务优先，其次平台浏览器仿真，并把指纹写回后续直连。"""
        if is_cf_solver_enabled():
            solver_text = self._solver_text(url, observed_version)
            if solver_text:
                return solver_text
            observed_version = self.state_version
        with self._lock:
            if self._state_version != observed_version:
                # 其它线程已刷新过指纹，先按新指纹重试一次直连。
                try:
                    response = self.request_once(url)
                    text = response.text or ""
                    if response.ok and not self.is_challenge(response):
                        return text
                except requests.exceptions.RequestException:
                    pass

            result = playwright_snapshot(
                url, self._browser_proxy, max(30, self._request_timeout), self._gate
            )
            if not isinstance(result, dict):
                return ""
            text = str(result.get("text") or "")
            if not text or is_cloudflare_challenge(text):
                self._gate.activate_cooldown(30, reason="SeedHub 浏览器验证")
                return ""
            self._store_session(result.get("cookies"), str(result.get("user_agent") or ""))
            return text
