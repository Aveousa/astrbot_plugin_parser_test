# debounce.py

import time

from .config import PluginConfig
from .session_scope import normalize_session_scope


class Debouncer:
    """
    会话级防抖器
    - 支持 link 防抖
    - 支持 resource_id 防抖
    """

    def __init__(self, config: PluginConfig):
        self.cfg = config
        self.interval = self.cfg.debounce_interval
        self._cache: dict[str, dict[str, float]] = {}  # {session: {key: ts}}

    @staticmethod
    def _normalize_session(session: str) -> str:
        """将群成员隔离 UMO 归一化为群聊 UMO。"""
        return normalize_session_scope(session)

    def _hit(self, session: str, key: str) -> bool:
        # 禁用
        if self.interval <= 0:
            return False

        session = self._normalize_session(session)
        now = time.time()
        bucket = self._cache.setdefault(session, {})

        # 1. 清理过期
        expire = now - self.interval
        for k, ts in list(bucket.items()):
            if ts < expire:
                bucket.pop(k, None)

        # 2. 命中判断
        if key in bucket:
            return True

        # 3. 记录
        bucket[key] = now
        return False

    def hit_link(self, session: str, link: str) -> bool:
        """基于 link 的防抖"""
        return self._hit(session, f"link:{link}")

    def hit_resource(self, session: str, resource_id: str) -> bool:
        """基于资源 ID 的防抖"""
        return self._hit(session, f"res:{resource_id}")
