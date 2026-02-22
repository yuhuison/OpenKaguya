"""
Embedding 客户端 — 通过 OpenAI 兼容 API 获取向量。
"""

from __future__ import annotations

from loguru import logger
from openai import AsyncOpenAI

from kaguya.config import LLMModelConfig


class EmbeddingClient:
    """
    Embedding 向量化客户端。

    使用 OpenAI 兼容 API（如 Qwen3-Embedding 通过 OpenRouter）。
    """

    def __init__(self, config: LLMModelConfig):
        self.config = config
        self._client = AsyncOpenAI(
            api_key=config.api_key,
            base_url=config.base_url,
        )
        logger.info(
            f"Embedding 客户端初始化: model={config.model}, dim={config.dimensions}"
        )

    async def embed(self, text: str) -> list[float]:
        """对单个文本进行向量化"""
        response = await self._client.embeddings.create(
            model=self.config.model,
            input=text,
            dimensions=self.config.dimensions,
        )
        return response.data[0].embedding

    async def embed_batch(self, texts: list[str]) -> list[list[float]]:
        """批量向量化"""
        if not texts:
            return []

        response = await self._client.embeddings.create(
            model=self.config.model,
            input=texts,
            dimensions=self.config.dimensions,
        )

        # 按照 index 排序确保顺序一致
        sorted_data = sorted(response.data, key=lambda x: x.index)
        return [d.embedding for d in sorted_data]
