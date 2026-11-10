"""MoviePilot LLM 适配层。

只依赖 MP 内部的 ``LLMHelper``（不在 app.sdk 公开接口里），导入或调用失败时
返回不可用，插件其余功能不受影响。MP 没开智能体或没配模型时，裁决门不启用。
"""

from __future__ import annotations

import asyncio
import concurrent.futures
from typing import Any, Optional, Tuple

try:
    from app.sdk.logging import logger
except Exception:  # pragma: no cover - 离线测试
    import logging

    logger = logging.getLogger(__name__)


def _setting(name: str, default: Any = None) -> Any:
    for loader in (_runtime_setting, _sdk_setting):
        try:
            value = loader(name)
        except Exception:
            continue
        if value is not None:
            return value
    return default


def _runtime_setting(name: str) -> Any:
    from app.runtime.settings import get_runtime_setting

    return get_runtime_setting(name)


def _sdk_setting(name: str) -> Any:
    from app.sdk.config import settings

    return getattr(settings, name, None)


def _llm_helper():
    try:
        from app.agent.llm.helper import LLMHelper
    except Exception:
        from app.agent.llm import LLMHelper
    return LLMHelper


def _check_provider_runtime() -> None:
    """LLM provider 运行时由 MP 启动层组装；脱离 MP 进程（如单独跑 python）时没有，调用必然失败。"""
    try:
        from app.agent.llm.gateway import resolve_llm_provider_runtime
    except ImportError:
        return  # 旧版 MP 没有这一层
    resolve_llm_provider_runtime()


class MoviePilotLLM:
    """一次不带工具的文本调用；在独立线程和事件循环里跑，适合同步任务调用。"""

    def __init__(self, model: str = "") -> None:
        self._model = str(model or "").strip()
        self._warned = False

    def available(self) -> bool:
        if not bool(_setting("AI_AGENT_ENABLE", False)):
            return False
        if not (self._model or _setting("LLM_MODEL", "")):
            return False
        try:
            _llm_helper()
            _check_provider_runtime()
        except Exception as error:
            if not self._warned:
                self._warned = True
                logger.warning(f"MoviePilot 的 LLM 接口不可用，AI 季集裁决只走规则：{error}")
            return False
        return True

    def __call__(self, system: str, user: str, timeout: float) -> Tuple[str, int]:
        """返回 (模型输出文本, 消耗 token 数)；超时或失败抛异常。"""
        pool = concurrent.futures.ThreadPoolExecutor(max_workers=1)
        try:
            future = pool.submit(asyncio.run, self._ainvoke(system, user, timeout))
            return future.result(timeout=timeout + 5)
        finally:
            pool.shutdown(wait=False)

    async def _ainvoke(self, system: str, user: str, timeout: float) -> Tuple[str, int]:
        helper = _llm_helper()
        kwargs: dict = {"thinking_level": "off", "temperature": 0}
        if self._model:
            kwargs["model"] = self._model
        llm = await helper.get_llm(**kwargs)
        try:
            response = await asyncio.wait_for(
                llm.ainvoke([("system", system), ("human", user)]), timeout=timeout
            )
        finally:
            try:
                await helper.close_llm(llm)
            except Exception:
                pass
        content = getattr(response, "content", response)
        try:
            text = helper.extract_text_content(content, fallback_to_string=True)
        except Exception:
            text = content if isinstance(content, str) else str(content)
        usage = getattr(response, "usage_metadata", None) or {}
        tokens = int(usage.get("total_tokens") or 0) if isinstance(usage, dict) else 0
        if not tokens:
            tokens = (len(system) + len(user) + len(str(text))) // 2
        return str(text or ""), tokens
