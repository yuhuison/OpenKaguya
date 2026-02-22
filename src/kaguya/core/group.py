"""
群聊预判中间件 — 决定辉夜姬是否应该回复群聊消息。

核心原则：辉夜姬不是每条群消息都要回复的。
只有在被 @ 或消息内容与她相关时才回复。
"""

from __future__ import annotations

import random

from loguru import logger

from kaguya.core.middleware import Middleware
from kaguya.core.types import UnifiedMessage


class GroupFilterMiddleware(Middleware):
    """
    群聊预判过滤器。

    决定辉夜姬是否应该回复某条群聊消息。
    对于私聊消息直接放行。

    触发条件（满足任一即回复）：
    1. 消息中 @ 了辉夜姬（或包含辉夜姬的名字）
    2. 消息是对辉夜姬上一条回复的直接回应
    3. 消息包含特定关键词（可配置）
    4. 随机概率回复（模拟旁听后偶尔插嘴）
    """

    def __init__(
        self,
        bot_names: list[str] | None = None,
        trigger_keywords: list[str] | None = None,
        random_reply_chance: float = 0.05,
    ):
        """
        Args:
            bot_names: 辉夜姬的名字列表（用于检测 @），默认 ["辉夜姬", "kaguya"]
            trigger_keywords: 触发回复的关键词列表
            random_reply_chance: 随机回复概率（0~1），默认 5%
        """
        self._bot_names = bot_names or ["辉夜姬", "辉夜", "kaguya", "Kaguya"]
        self._trigger_keywords = trigger_keywords or []
        self._random_chance = random_reply_chance
        # 记录最近回复过的群组，用于检测"对话延续"
        self._recently_replied_groups: dict[str, int] = {}  # group_id -> 消息计数

    @property
    def name(self) -> str:
        return "group_filter"

    async def pre_process(self, message: UnifiedMessage) -> str | None:
        """
        前置处理：判断是否应该回复群聊消息。

        返回 None 表示正常处理。
        如果判定不应回复，设置消息的 _skip 标记。
        """
        # 私聊消息直接放行
        if not message.is_group_message:
            return None

        should_reply, reason = self._should_reply(message)

        if should_reply:
            logger.debug(f"群聊预判: 决定回复 (原因: {reason})")
            # 标记为需要回复
            message._group_reply_reason = reason  # type: ignore
            # 记录本次回复
            self._recently_replied_groups[message.group_id] = 0
            return f"[群聊上下文] 你在群 {message.group_id} 中收到消息，触发原因: {reason}"
        else:
            logger.debug(f"群聊预判: 跳过 (原因: {reason})")
            # 标记为不回复
            message._skip_reply = True  # type: ignore
            # 增加计数器
            if message.group_id in self._recently_replied_groups:
                self._recently_replied_groups[message.group_id] += 1
                # 超过 20 条没回复就清除"最近回复"状态
                if self._recently_replied_groups[message.group_id] > 20:
                    del self._recently_replied_groups[message.group_id]
            return None

    async def post_process(self, message: UnifiedMessage, replies: list[str]) -> None:
        """后置处理：无操作"""
        pass

    def _should_reply(self, message: UnifiedMessage) -> tuple[bool, str]:
        """
        判断是否应该回复。

        Returns:
            (should_reply, reason)
        """
        content = message.content.lower()

        # 1. @ 检测（最高优先级）
        for name in self._bot_names:
            if name.lower() in content:
                return True, f"被提及 ({name})"

        # 2. 对话延续（辉夜姬最近在这个群回复过，接下来几条消息更容易触发）
        if message.group_id in self._recently_replied_groups:
            count = self._recently_replied_groups[message.group_id]
            if count <= 3:
                # 最近 3 条消息内，有 40% 概率继续对话
                if random.random() < 0.4:
                    return True, "对话延续"

        # 3. 关键词触发
        for kw in self._trigger_keywords:
            if kw.lower() in content:
                return True, f"关键词匹配 ({kw})"

        # 4. 随机插嘴
        if random.random() < self._random_chance:
            return True, "随机插嘴"

        return False, "无触发条件"
