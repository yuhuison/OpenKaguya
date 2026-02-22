"""
记忆中间件 — 将记忆系统集成到 ChatEngine 中。
"""

from __future__ import annotations

import asyncio
import json

from loguru import logger

from kaguya.core.middleware import Middleware
from kaguya.core.types import UnifiedMessage
from kaguya.memory.database import Database
from kaguya.memory.retriever import MemoryRetriever


class MemoryMiddleware(Middleware):
    """
    记忆中间件。

    前置处理 (pre_process):
        1. 将用户消息保存到数据库
        2. 检索相关历史记忆
        3. 返回记忆内容注入到系统提示语

    后置处理 (post_process):
        1. 将辉夜姬的回复保存到数据库
        2. 异步触发向量化检查
    """

    def __init__(self, db: Database, retriever: MemoryRetriever, top_k: int = 5):
        self.db = db
        self.retriever = retriever
        self.top_k = top_k

    async def pre_process(self, message: UnifiedMessage) -> str | None:
        """
        保存用户消息 & 检索相关记忆。
        """
        user_id = message.sender.user_id

        # 1. 保存用户消息
        await self.db.save_message(
            user_id=user_id,
            platform=message.platform.value,
            role="user",
            content=message.content,
        )

        # 2. 检索相关记忆
        memories = await self.retriever.retrieve(
            user_id=user_id,
            query=message.content,
            top_k=self.top_k,
        )

        if not memories:
            return None

        # 3. 格式化记忆为提示语
        memory_lines = []
        for m in memories:
            role_label = "用户" if m["role"] == "user" else "你"
            content = m.get("display_content") or m["content"]
            # 截断过长的记忆
            if len(content) > 300:
                content = content[:300] + "..."
            memory_lines.append(f"  [{m['created_at']}] {role_label}: {content}")

        return (
            "以下是你回忆起的与当前话题相关的历史对话片段，"
            "你可以参考这些记忆来更好地回复：\n"
            + "\n".join(memory_lines)
        )

    async def post_process(self, message: UnifiedMessage, replies: list[str]) -> None:
        """
        保存辉夜姬的回复 & 异步检查是否需要向量化。
        """
        user_id = message.sender.user_id

        # 保存辉夜姬的回复
        if replies:
            display = "\n".join(replies)
            await self.db.save_message(
                user_id=user_id,
                platform=message.platform.value,
                role="assistant",
                content=display,
                display_content=display,
            )

        # 异步触发向量化（不阻塞回复）
        asyncio.create_task(self._safe_vectorize(user_id))

    async def _safe_vectorize(self, user_id: str) -> None:
        """安全地执行向量化，捕获异常"""
        try:
            await self.retriever.check_and_vectorize(user_id)
        except Exception as e:
            logger.error(f"后台向量化异常: {e}")
