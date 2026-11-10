"""搜索渠道并发名额：超时的渠道立即让出名额，不拖慢后面的渠道。"""

import threading
from typing import Set


class SourceSlots:
    """按渠道记账的并发名额；同一渠道的名额只会归还一次。"""

    def __init__(self, concurrency: int):
        self._slots = threading.Semaphore(max(1, int(concurrency or 1)))
        self._lock = threading.Lock()
        self._holders: Set[str] = set()
        self._abandoned: Set[str] = set()
        self._finished = threading.Event()

    def acquire(self, source: str, poll: float = 0.5) -> bool:
        """等待名额；整轮已结束或该渠道已被放弃时返回 False。"""
        while not self._slots.acquire(timeout=poll):
            if self._finished.is_set():
                return False
        with self._lock:
            if self._finished.is_set() or source in self._abandoned:
                self._slots.release()
                return False
            self._holders.add(source)
        return True

    def release(self, source: str) -> None:
        """归还名额（渠道正常结束或被判超时时调用，重复调用无副作用）。"""
        with self._lock:
            if source not in self._holders:
                return
            self._holders.discard(source)
        self._slots.release()

    def abandon(self, source: str) -> None:
        """不再等待该渠道：已占名额立即归还，尚未开始的不再开始。"""
        with self._lock:
            self._abandoned.add(source)
        self.release(source)

    def finish(self) -> None:
        """整轮结束：还在排队的渠道不再开始。"""
        self._finished.set()
