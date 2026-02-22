"""
记忆检索器 — 混合检索 (向量 + FTS5) 与 RRF 融合。
"""

from __future__ import annotations

import json

from loguru import logger

from kaguya.llm.client import LLMClient
from kaguya.llm.embedding import EmbeddingClient
from kaguya.memory.database import Database


class MemoryRetriever:
    """
    辉夜姬的记忆检索器。

    核心能力：
    1. 混合检索（向量 + FTS5 全文）
    2. RRF 排序融合
    3. 自动向量化（累积 N 条后触发）
    """

    def __init__(
        self,
        db: Database,
        embed_client: EmbeddingClient,
        secondary_llm: LLMClient,
        vectorize_threshold: int = 10,
    ):
        self.db = db
        self.embed_client = embed_client
        self.secondary_llm = secondary_llm
        self.vectorize_threshold = vectorize_threshold

    async def retrieve(
        self, user_id: str, query: str, top_k: int = 5
    ) -> list[dict]:
        """
        混合检索最相关的历史消息。

        1. 向量 KNN 搜索
        2. FTS5 关键词搜索
        3. RRF 融合排序
        4. 返回 top-K 条消息
        """
        # 1. 向量检索
        vec_results: list[tuple[int, float]] = []
        try:
            query_embedding = await self.embed_client.embed(query)
            vec_results = await self.db.search_vectors(query_embedding, top_k=top_k * 2)
        except Exception as e:
            logger.warning(f"向量检索失败（可能尚无向量数据）: {e}")

        # 2. FTS5 检索
        fts_results: list[tuple[int, float]] = []
        try:
            fts_results = await self.db.search_fts(query, top_k=top_k * 2)
        except Exception as e:
            logger.warning(f"FTS5 检索失败: {e}")

        # 3. RRF 融合
        scores: dict[int, float] = {}
        k = 60  # RRF 常数

        for rank, (msg_id, _) in enumerate(vec_results):
            scores[msg_id] = scores.get(msg_id, 0) + 1 / (k + rank + 1)

        for rank, (msg_id, _) in enumerate(fts_results):
            scores[msg_id] = scores.get(msg_id, 0) + 1 / (k + rank + 1)

        if not scores:
            return []

        # 4. 排序取 top-K
        top_ids = sorted(scores, key=scores.get, reverse=True)[:top_k]

        # 5. 获取完整消息内容
        messages = await self.db.fetch_messages_by_ids(top_ids)
        logger.debug(f"记忆检索: query='{query[:30]}...', 结果={len(messages)}条")
        return messages

    async def check_and_vectorize(self, user_id: str) -> None:
        """
        检查未向量化消息数量，达到阈值则批量向量化。
        这个方法应该在每次对话结束后异步调用。
        """
        count = await self.db.get_unvectorized_count(user_id)
        if count < self.vectorize_threshold:
            return

        logger.info(f"开始向量化: user={user_id}, 未向量化消息={count}条")
        unvectorized = await self.db.get_unvectorized_messages(user_id)

        if not unvectorized:
            return

        # 1. 提取文本并批量向量化
        texts = [m["content"] for m in unvectorized]
        try:
            embeddings = await self.embed_client.embed_batch(texts)
        except Exception as e:
            logger.error(f"批量向量化失败: {e}")
            return

        # 2. 存入向量数据库
        for msg, emb in zip(unvectorized, embeddings):
            await self.db.insert_vector(msg["id"], emb)

        # 3. 用次级模型生成摘要
        try:
            combined_text = "\n".join(
                f"[{m['content'][:200]}]" for m in unvectorized
            )
            summary_response = await self.secondary_llm.chat(
                messages=[
                    {
                        "role": "system",
                        "content": "你是一个日志摘要助手。请用一两句话总结以下对话片段的主题和关键信息。",
                    },
                    {"role": "user", "content": combined_text},
                ],
                temperature=0.3,
                max_tokens=200,
            )
            summary = summary_response["content"]

            await self.db.save_daily_log(
                user_id=user_id,
                summary=summary,
                range_start=unvectorized[0]["id"],
                range_end=unvectorized[-1]["id"],
            )
            logger.info(f"对话摘要已保存: {summary[:80]}...")
        except Exception as e:
            logger.error(f"生成摘要失败: {e}")

        # 4. 标记为已向量化
        ids = [m["id"] for m in unvectorized]
        await self.db.mark_vectorized(ids)
        logger.info(f"向量化完成: {len(ids)}条消息")
