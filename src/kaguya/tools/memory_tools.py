"""
记忆工具 — 供辉夜姬主动检索和读取记忆话题的工具集。

工具列表：
1. search_memory_by_topic  - 在话题摘要中语义搜索（向量）
2. search_messages_in_topics - 在指定话题的原始消息中关键词检索
3. get_topic_summary       - 读取指定话题的完整摘要
4. get_topic_messages      - 读取指定话题下的原始消息列表
"""

from __future__ import annotations

import json

from kaguya.llm.embedding import EmbeddingClient
from kaguya.memory.database import Database


class MemoryTools:
    """记忆检索工具集，需要注册到 ToolRegistry"""

    def __init__(self, db: Database, embed_client: EmbeddingClient):
        self.db = db
        self.embed_client = embed_client
        # 当前用户 ID，由 ToolRegistry 注入
        self._user_id: str = ""

    def set_user_id(self, user_id: str) -> None:
        self._user_id = user_id

    # ==================== 工具实现 ====================

    async def search_memory_by_topic(self, query: str, top_k: int = 3) -> str:
        """在记忆话题中语义搜索，返回最相关的话题列表"""
        try:
            emb = await self.embed_client.embed(query)
            results = await self.db.search_topic_vectors(emb, top_k=top_k)
            if not results:
                return "未找到相关话题记忆。"

            output = []
            for topic_id, distance in results:
                topic = await self.db.get_topic_by_id(topic_id)
                if topic:
                    output.append(
                        f"话题ID: {topic_id}\n"
                        f"标题: {topic['title']}\n"
                        f"最后更新: {topic['updated_at']}\n"
                        f"消息数: {topic['message_count']}"
                    )
            return "\n\n".join(output) if output else "未找到相关话题记忆。"
        except Exception as e:
            return f"检索失败: {e}"

    async def search_messages_in_topics(self, topic_ids: list[str], keyword: str) -> str:
        """在指定话题的原始消息中进行关键词检索"""
        try:
            results = await self.db.search_messages_in_topics(topic_ids, keyword, limit=10)
            if not results:
                return f"在指定话题中未找到包含「{keyword}」的消息。"

            output = []
            for m in results:
                role_label = "用户" if m["role"] == "user" else "辉夜姬"
                output.append(f"[{m['created_at'][:16]}] {role_label}: {m['content']}")
            return "\n".join(output)
        except Exception as e:
            return f"检索失败: {e}"

    async def get_topic_summary(self, topic_id: str) -> str:
        """读取指定话题的完整摘要内容"""
        try:
            topic = await self.db.get_topic_by_id(topic_id)
            if not topic:
                return f"话题 {topic_id} 不存在。"
            return (
                f"话题：{topic['title']}\n"
                f"最后更新：{topic['updated_at']}\n"
                f"消息总数：{topic['message_count']}\n\n"
                f"摘要内容：\n{topic['summary']}"
            )
        except Exception as e:
            return f"读取失败: {e}"

    async def get_topic_messages(self, topic_id: str, limit: int = 20) -> str:
        """读取指定话题下的原始消息记录"""
        try:
            topic = await self.db.get_topic_by_id(topic_id)
            if not topic:
                return f"话题 {topic_id} 不存在。"

            messages = await self.db.get_messages_by_topic(topic_id, limit=limit)
            if not messages:
                return f"话题「{topic['title']}」暂无消息记录。"

            output = [f"话题「{topic['title']}」的消息记录（最近{limit}条）："]
            for m in messages:
                role_label = "用户" if m["role"] == "user" else "辉夜姬"
                content = (m.get("display_content") or m["content"])[:200]
                output.append(f"[{m['created_at'][:16]}] {role_label}: {content}")
            return "\n".join(output)
        except Exception as e:
            return f"读取失败: {e}"

    # ==================== OpenAI 工具定义 ====================

    TOOL_DEFINITIONS = [
        {
            "type": "function",
            "function": {
                "name": "search_memory_by_topic",
                "description": (
                    "在记忆话题中进行语义搜索，找到与查询内容最相关的话题。"
                    "当你想回忆某类信息（如用户喜好、历史事件）但上下文中没有时使用。"
                    "返回话题ID和标题，可进一步用 get_topic_summary 读取详情。"
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {
                            "type": "string",
                            "description": "搜索内容，用自然语言描述你想找的记忆",
                        },
                        "top_k": {
                            "type": "integer",
                            "description": "返回最相关的话题数量（默认3）",
                        },
                    },
                    "required": ["query"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "search_messages_in_topics",
                "description": (
                    "在指定话题的原始消息中进行关键词字符串检索。"
                    "先用 search_memory_by_topic 找到话题ID，再用此工具在该话题内精确搜索具体说了什么。"
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "topic_ids": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "要搜索的话题ID列表",
                        },
                        "keyword": {
                            "type": "string",
                            "description": "要搜索的关键词",
                        },
                    },
                    "required": ["topic_ids", "keyword"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "get_topic_summary",
                "description": "读取指定话题的完整摘要内容和基本信息。",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "topic_id": {
                            "type": "string",
                            "description": "话题ID",
                        },
                    },
                    "required": ["topic_id"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "get_topic_messages",
                "description": "读取指定话题下的原始对话消息记录，按时间正序排列。",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "topic_id": {
                            "type": "string",
                            "description": "话题ID",
                        },
                        "limit": {
                            "type": "integer",
                            "description": "最多返回的消息条数（默认20）",
                        },
                    },
                    "required": ["topic_id"],
                },
            },
        },
    ]
