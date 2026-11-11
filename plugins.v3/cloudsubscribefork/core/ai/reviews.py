"""AI 季集裁决的待复核列表：存在插件数据里，由 MP 的 AI 助手列出和处理。"""

from __future__ import annotations

import threading
import time
import uuid
from typing import Any, Callable, Dict, List, Optional

DATA_KEY = "ai_reviews"
# 已处理或过期的记录保留天数，之后清掉
_KEEP_DONE_DAYS = 30


class ReviewStore:
    def __init__(
            self,
            get_data: Optional[Callable[[str], Any]] = None,
            save_data: Optional[Callable[[str, Any], Any]] = None,
            expire_days: int = 7,
            now: Callable[[], float] = time.time,
    ) -> None:
        self._get = get_data
        self._save = save_data
        self._expire_seconds = max(1, int(expire_days or 7)) * 86400
        self._now = now
        self._lock = threading.Lock()

    def _load(self) -> Dict[str, Dict[str, Any]]:
        if not self._get:
            return {}
        try:
            data = self._get(DATA_KEY) or {}
        except Exception:
            return {}
        return dict(data) if isinstance(data, dict) else {}

    def _store(self, data: Dict[str, Dict[str, Any]]) -> None:
        if self._save:
            self._save(DATA_KEY, data)

    def _housekeep(self, data: Dict[str, Dict[str, Any]]) -> bool:
        now = self._now()
        changed = False
        for review_id in list(data):
            record = data[review_id]
            created = float(record.get("created_at") or 0)
            if record.get("status") == "pending" and now - created > self._expire_seconds:
                record["status"] = "expired"
                record["decided_at"] = now
                changed = True
            decided = float(record.get("decided_at") or 0)
            if record.get("status") != "pending" and decided and \
                    now - decided > _KEEP_DONE_DAYS * 86400:
                data.pop(review_id)
                changed = True
        return changed

    def add(self, record: Dict[str, Any]) -> Optional[str]:
        """新增一条待复核；同一文件同一集已在待复核时不重复添加，返回 None。"""
        key = str(record.get("key") or "")
        with self._lock:
            data = self._load()
            self._housekeep(data)
            for existing in data.values():
                if key and existing.get("key") == key and existing.get("status") == "pending":
                    self._store(data)
                    return None
            review_id = uuid.uuid4().hex[:8]
            data[review_id] = {
                **record,
                "id": review_id,
                "status": "pending",
                "created_at": self._now(),
            }
            self._store(data)
            return review_id

    def get(self, review_id: str) -> Optional[Dict[str, Any]]:
        record = self._load().get(str(review_id or "").strip())
        return dict(record) if record else None

    def list(self, status: str = "pending", limit: int = 20,
             persist: bool = True) -> List[Dict[str, Any]]:
        """persist=False 时只读：过期判断只作用于返回结果，不写回数据。"""
        with self._lock:
            data = self._load()
            if self._housekeep(data) and persist:
                self._store(data)
        records = [
            dict(record) for record in data.values()
            if not status or record.get("status") == status
        ]
        records.sort(key=lambda record: float(record.get("created_at") or 0), reverse=True)
        return records[:max(1, int(limit or 20))]

    def decide(self, review_id: str, approve: bool, note: str = "") -> Optional[Dict[str, Any]]:
        """标记放行或拒绝；不是待复核状态时返回 None。"""
        review_id = str(review_id or "").strip()
        with self._lock:
            data = self._load()
            self._housekeep(data)
            record = data.get(review_id)
            if not record or record.get("status") != "pending":
                self._store(data)
                return None
            record["status"] = "approved" if approve else "rejected"
            record["decided_at"] = self._now()
            if note:
                record["note"] = str(note)[:200]
            self._store(data)
            return dict(record)

    def reopen(self, review_id: str) -> None:
        """放行后提交转存失败时，回到待复核。"""
        with self._lock:
            data = self._load()
            record = data.get(str(review_id or ""))
            if record:
                record["status"] = "pending"
                record.pop("decided_at", None)
                self._store(data)

    @staticmethod
    def public_view(record: Dict[str, Any]) -> Dict[str, Any]:
        """给 AI 助手看的字段：不含分享链接和网盘文件 ID。"""
        return {
            "review_id": record.get("id"),
            "status": record.get("status"),
            "subscribe_id": record.get("subscribe_id"),
            "title": record.get("title"),
            "season": record.get("season"),
            "episode": record.get("episode"),
            "file_name": record.get("file_name"),
            "relative_path": record.get("relative_path"),
            "share_title": record.get("share_title"),
            "source": record.get("source"),
            "rule_reasons": record.get("rule_reasons") or [],
            "ai_verdict": record.get("ai_verdict"),
            "ai_reason": record.get("ai_reason"),
            "created_at": time.strftime(
                "%Y-%m-%d %H:%M", time.localtime(float(record.get("created_at") or 0))
            ),
        }
