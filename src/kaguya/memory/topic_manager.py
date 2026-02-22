"""
话题管理器 — 由次级模型将对话归档到话题摘要中。
"""

from __future__ import annotations

import json
import uuid

from loguru import logger

from kaguya.llm.client import LLMClient
from kaguya.llm.embedding import EmbeddingClient
from kaguya.memory.database import Database

# 话题摘要最大字符数，超过则触发再压缩
TOPIC_MAX_CHARS = 5000

# 归档 prompt
ARCHIVE_SYSTEM_PROMPT = """你是一个对话记忆归档助手。
任务：将给定的对话消息归类到合适的话题中，并为每个话题生成简洁的摘要增量。

规则：
- 一条消息可以属于多个话题
- 如果某些消息不属于任何已有话题，则创建话题（标题应简洁、具体，如"小马宝莉讨论"、"用户工作情况"）
- 摘要增量应保留有价值的信息（用户偏好、事件、情绪、重要结论），过滤无意义的闲聊
- 纯打招呼、随意寒暄可忽略，不必强行归入任何话题
- 如果所有消息都是无意义的闲聊，输出 {"actions": []}
- 输出严格为 JSON，不要有其他任何文字

输出格式：
{
  "actions": [
    {
      "operation": "append",
      "topic_id": "已有话题的ID（若为新话题则为null）",
      "topic_title": "话题标题（operation=create时填写，append时可省略）",
      "summary_delta": "本次的摘要增量（简洁，不超过500字）",
      "message_ids": [101, 102]
    }
  ]
}"""

COMPRESS_SYSTEM_PROMPT = """你是一个文本压缩助手。
请将以下话题摘要压缩到5000字以内，保留所有重要信息，删除冗余和重复内容。
只输出压缩后的摘要内容，不要有任何其他文字。"""


class TopicManager:
    """
    话题管理器。

    负责：
    1. 将未归档的消息归类到话题（由次级模型完成）
    2. 维护话题摘要（超过5000字时自动压缩）
    3. 更新话题向量（用于语义检索）
    """

    def __init__(
        self,
        db: Database,
        embed_client: EmbeddingClient,
        secondary_llm: LLMClient,
    ):
        self.db = db
        self.embed_client = embed_client
        self.secondary_llm = secondary_llm

    async def archive_messages(self, user_id: str) -> None:
        """
        将用户的未归档消息归入话题。
        这个方法应该在每次对话结束后异步调用。
        """
        unarchived = await self.db.get_unarchived_messages(user_id)
        if not unarchived:
            return

        logger.info(f"开始归档: user={user_id}, 未归档消息={len(unarchived)}条")

        # 获取当前话题列表（只传标题，不传摘要正文，避免 prompt 过长）
        all_topics = await self.db.get_all_topics(user_id)

        # 构建消息文本（使用 display_content，即用户实际看到的内容）
        msg_lines = []
        for m in unarchived:
            role_label = "用户" if m["role"] == "user" else "辉夜姬"
            actual_content = (m.get("display_content") or m["content"])[:300]
            # 跳过纯系统占位符
            if actual_content in ("[用户发送了图片]", ""):
                actual_content = "[发送了图片]"
            msg_lines.append(f"[ID:{m['id']}] [{m['created_at']}] {role_label}: {actual_content}")

        topics_text = ""
        if all_topics:
            topics_lines = [
                f"- ID:{t['id']} | {t['title']} | 最后更新:{t['updated_at']}"
                for t in all_topics
            ]
            topics_text = "当前已有话题：\n" + "\n".join(topics_lines)
        else:
            topics_text = "当前无已有话题，请根据内容创建新话题。"

        user_prompt = f"{topics_text}\n\n待归档对话：\n" + "\n".join(msg_lines)

        # 调用次级模型
        try:
            response = await self.secondary_llm.chat(
                messages=[
                    {"role": "system", "content": ARCHIVE_SYSTEM_PROMPT},
                    {"role": "user", "content": user_prompt},
                ],
                temperature=0.2,
                max_tokens=1000,
                response_format={"type": "json_object"},
            )
            result = json.loads(response["content"])
            actions = result.get("actions", [])
        except Exception as e:
            logger.error(f"归档模型调用失败: {e}")
            return

        if not actions:
            logger.info("归档完成（无有效话题内容）")
            await self.db.mark_archived([m["id"] for m in unarchived])
            return

        # 处理每个 action
        all_processed_msg_ids: set[int] = set()

        for action in actions:
            try:
                operation = action.get("operation", "create")
                topic_id = action.get("topic_id") or None
                topic_title = action.get("topic_title", "未命名话题")
                summary_delta = action.get("summary_delta", "")
                msg_ids = [int(i) for i in action.get("message_ids", [])]

                if not summary_delta:
                    continue

                # 获取或初始化话题
                if operation == "append" and topic_id:
                    existing = await self.db.get_topic_by_id(topic_id)
                    if existing:
                        old_summary = existing["summary"]
                        old_count = existing["message_count"]
                        title = existing["title"]
                    else:
                        # topic_id 失效（可能已删除），降级为 create
                        topic_id = None
                        old_summary = ""
                        old_count = 0
                        title = topic_title
                else:
                    topic_id = str(uuid.uuid4())
                    old_summary = ""
                    old_count = 0
                    title = topic_title

                # 追加摘要增量
                new_summary = (old_summary + "\n\n" + summary_delta).strip() if old_summary else summary_delta
                new_count = old_count + len(msg_ids)

                # 超过 5000 字则压缩
                if len(new_summary) > TOPIC_MAX_CHARS:
                    new_summary = await self._compress_summary(new_summary)

                # 写入 DB
                await self.db.upsert_topic(
                    topic_id=topic_id,
                    user_id=user_id,
                    title=title,
                    summary=new_summary,
                    message_count=new_count,
                )
                await self.db.link_messages_to_topic(topic_id, msg_ids)

                # 更新话题向量（对更新后的摘要做 embedding）
                try:
                    embedding = await self.embed_client.embed(f"{title}：{new_summary[:1000]}")
                    await self.db.upsert_topic_vector(topic_id, embedding)
                except Exception as e:
                    logger.warning(f"话题向量更新失败 topic={topic_id}: {e}")

                all_processed_msg_ids.update(msg_ids)
                logger.info(f"话题已更新: [{title}] +{len(msg_ids)}条消息")

            except Exception as e:
                logger.error(f"处理归档 action 失败: {e}, action={action}")

        # 标记已归档（所有未归档消息，即使没被归入任何话题）
        await self.db.mark_archived([m["id"] for m in unarchived])
        logger.info(f"归档完成: {len(unarchived)}条消息已标记, {len(actions)}个话题已更新")

    async def _compress_summary(self, summary: str) -> str:
        """当话题摘要超过5000字时，调用次级模型进行压缩"""
        try:
            response = await self.secondary_llm.chat(
                messages=[
                    {"role": "system", "content": COMPRESS_SYSTEM_PROMPT},
                    {"role": "user", "content": summary},
                ],
                temperature=0.1,
                max_tokens=2000,
            )
            compressed = response["content"]
            logger.info(f"话题摘要已压缩: {len(summary)}字 → {len(compressed)}字")
            return compressed
        except Exception as e:
            logger.error(f"摘要压缩失败: {e}")
            # 压缩失败时截断
            return summary[:TOPIC_MAX_CHARS]
