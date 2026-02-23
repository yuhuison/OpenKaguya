"""
记忆中间件 — 将话题化记忆系统集成到 ChatEngine 中。
"""

from __future__ import annotations

import asyncio

from loguru import logger

from kaguya.core.middleware import Middleware
from kaguya.core.types import UnifiedMessage
from kaguya.memory.database import Database
from kaguya.memory.topic_manager import TopicManager

# 触发归档的未归档消息阈值
ARCHIVE_THRESHOLD = 10

# 注入上下文时，最多显示的未归档消息条数
MAX_UNARCHIVED_IN_CONTEXT = 20

# 注入上下文时，最多显示的话题标题数
MAX_TOPICS_IN_CONTEXT = 20


def _format_topic_list(topics: list[dict]) -> str:
    """格式化话题标题列表（轻量，始终注入）"""
    if not topics:
        return ""
    lines = [f"  - [{t['title']}]（{t['updated_at'][:16]}，共{t['message_count']}条）" for t in topics]
    return "你与该用户共有以下记忆话题：\n" + "\n".join(lines)


def _format_topic_summary(topic: dict, label: str) -> str:
    """格式化单个话题的完整摘要"""
    return (
        f"【{label}】话题「{topic['title']}」摘要：\n"
        f"{topic['summary'][:2000]}"  # 最多 2000 字，避免单个话题撑爆 context
    )


def _format_unarchived(messages: list[dict]) -> str:
    """格式化未归档的原始消息（保持对话连贯性）"""
    if not messages:
        return ""
    lines = []
    for m in messages:
        role_label = "用户" if m["role"] == "user" else "你"
        content = (m.get("display_content") or m["content"])[:200]
        lines.append(f"  [{m['created_at'][11:16]}] {role_label}: {content}")
    return "最近尚未归档的对话（保持连贯性）：\n" + "\n".join(lines)


class MemoryMiddleware(Middleware):
    """
    话题化记忆中间件。

    前置处理 (pre_process)：
        1. 保存用户消息到数据库
        2. 获取所有话题标题列表（轻量）
        3. 获取最近更新的 1 个话题摘要（时间连续性）
        4. 获取未归档消息列表（连贯性）
        5. 合并注入 System Prompt

    后置处理 (post_process)：
        1. 保存辉夜姬的回复到数据库
        2. 异步触发话题归档（累积 10 条未归档时）
    """

    def __init__(
        self,
        db: Database,
        topic_manager: TopicManager,
        top_k: int = 3,
        embed_client=None,
    ):
        self.db = db
        self.topic_manager = topic_manager
        self.archive_threshold = top_k
        self.embed_client = embed_client

    async def pre_process(self, message: UnifiedMessage) -> str | None:
        user_id = message.sender.user_id

        # 1. 保存用户消息
        await self.db.save_message(
            user_id=user_id,
            platform=message.platform.value,
            role="user",
            content=message.content,
        )

        # 2. 所有话题标题列表（始终注入，轻量，截断为最多 MAX_TOPICS_IN_CONTEXT 个）
        all_topics = await self.db.get_all_topics(user_id)
        all_topics = all_topics[:MAX_TOPICS_IN_CONTEXT]

        # 3. 最近更新的 1 个话题（完整摘要）
        recent_topics = await self.db.get_recent_updated_topics(user_id, n=1)
        recent_topic = recent_topics[0] if recent_topics else None

        # 4. 未归档消息（保持连贯性）
        unarchived = await self.db.get_unarchived_messages(user_id)
        unarchived = unarchived[-MAX_UNARCHIVED_IN_CONTEXT:]

        # 5. 笔记标题列表：当前用户的笔记 + kaguya 的笔记
        user_notes = await self.db.get_notes_by_owner(user_id, limit=10)
        kaguya_notes = await self.db.get_notes_by_owner("kaguya", limit=10)

        # 6. 拼装上下文
        parts = []

        topic_list_text = _format_topic_list(all_topics)
        if topic_list_text:
            parts.append(topic_list_text)

        if recent_topic:
            parts.append(_format_topic_summary(recent_topic, "最近更新的话题"))

        # 语义召回：基于用户当前消息搜索最相关的话题
        if self.embed_client and message.content:
            try:
                emb = await self.embed_client.embed(message.content)
                results = await self.db.search_topic_vectors(
                    emb, top_k=1, user_id=user_id
                )
                if results:
                    sem_topic_id = results[0][0]
                    # 避免重复注入（如果已经是最近更新话题则跳过）
                    if not recent_topic or sem_topic_id != recent_topic["id"]:
                        sem_topic = await self.db.get_topic_by_id(sem_topic_id)
                        if sem_topic:
                            parts.append(
                                _format_topic_summary(sem_topic, "与当前对话相关的话题")
                            )
            except Exception as e:
                logger.warning(f"语义召回失败: {e}")

        unarchived_text = _format_unarchived(unarchived)
        if unarchived_text:
            parts.append(unarchived_text)

        # 笔记标题列表（两个 owner）
        note_lines = []
        if user_notes:
            note_lines.append(f"  [{user_id} 的笔记]")
            for n in user_notes:
                tag_str = f" #{n['tags']}" if n.get("tags") else ""
                note_lines.append(f"    [ID:{n['id']}] {n['title'] or '(\u65e0\u6807\u9898)'}{tag_str}（{n['updated_at'][:16]}）")
        if kaguya_notes:
            note_lines.append("  [我的私人笔记]")
            for n in kaguya_notes:
                tag_str = f" #{n['tags']}" if n.get("tags") else ""
                note_lines.append(f"    [ID:{n['id']}] {n['title'] or '(\u65e0\u6807\u9898)'}{tag_str}（{n['updated_at'][:16]}）")
        if note_lines:
            parts.append("笔记本（可用 manage_notes 工具读取内容）：\n" + "\n".join(note_lines))

        if not parts:
            return None

        return "\n\n".join(parts)

    async def post_process(self, message: UnifiedMessage, replies: list[str]) -> None:
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

        # 异步触发话题归档（不阻塞回复）
        asyncio.create_task(self._safe_archive(user_id))

    async def _safe_archive(self, user_id: str) -> None:
        """安全地执行话题归档，捕获异常"""
        try:
            count = await self.db.get_unarchived_count(user_id)
            if count >= self.archive_threshold:
                await self.topic_manager.archive_messages(user_id)
        except Exception as e:
            logger.error(f"后台话题归档异常: {e}")
