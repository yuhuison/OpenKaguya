"""
群聊过滤器 — 决定辉夜姬是否应该回复群聊消息。

核心原则：辉夜姬不是每条群消息都要回复的。
只有在被 @ 或消息内容与她相关时才回复。

过滤在 Adapter 层（消息聚合完成后、进入 ChatEngine 之前）执行，
不相关的群消息直接丢弃，不会触发锁竞争、DB 查询或中间件链。
"""

from __future__ import annotations

import random
import time

from loguru import logger


class GroupFilter:
    """
    群聊消息过滤器。

    触发条件（满足任一即回复）：
    1. 消息中提及了辉夜姬的名字（@ 检测）
    2. 辉夜姬最近在该群回复过，且仍在活跃时间窗口内（时间戳判断）
    3. 消息包含特定关键词（可配置）
    4. 随机概率回复（模拟偶尔插嘴）
    """

    def __init__(
        self,
        bot_names: list[str] | None = None,
        trigger_keywords: list[str] | None = None,
        random_reply_chance: float = 0.05,
        active_window_seconds: float = 120.0,
    ):
        """
        Args:
            bot_names: 辉夜姬的名字列表，默认 ["辉夜姬", "辉夜", "kaguya", "Kaguya"]
            trigger_keywords: 触发回复的关键词列表
            random_reply_chance: 随机回复概率（0~1），默认 5%
            active_window_seconds: 回复后保持"活跃状态"的时间窗口（秒），默认 120 秒
        """
        self._bot_names = bot_names or ["辉夜姬", "辉夜", "kaguya", "Kaguya"]
        self._trigger_keywords = trigger_keywords or []
        self._random_chance = random_reply_chance
        self._active_window = active_window_seconds
        # group_id -> 上次回复时的 monotonic 时间戳
        self._last_replied: dict[str, float] = {}

    def should_reply(self, content: str, group_id: str) -> tuple[bool, str]:
        """
        判断是否应该回复该群消息。

        Returns:
            (should_reply, reason)
        """
        content_lower = content.lower()

        # 1. 名字提及检测（最高优先级）
        for name in self._bot_names:
            if name.lower() in content_lower:
                return True, f"被提及 ({name})"

        # 2. 活跃对话窗口（时间戳判断，比消息计数更自然）
        last_ts = self._last_replied.get(group_id)
        if last_ts is not None:
            elapsed = time.monotonic() - last_ts
            if elapsed <= self._active_window:
                if random.random() < 0.4:
                    return True, f"对话延续 ({elapsed:.0f}s 前回复过)"

        # 3. 关键词触发
        for kw in self._trigger_keywords:
            if kw.lower() in content_lower:
                return True, f"关键词匹配 ({kw})"

        # 4. 随机插嘴
        if random.random() < self._random_chance:
            return True, "随机插嘴"

        return False, "无触发条件"

    def mark_replied(self, group_id: str) -> None:
        """标记辉夜姬刚刚回复了该群，更新活跃时间窗口起点。"""
        self._last_replied[group_id] = time.monotonic()
        logger.debug(f"群聊活跃窗口已更新: {group_id}")
