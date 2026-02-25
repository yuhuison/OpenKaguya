"""LLM API 客户端 — 基于 OpenAI 兼容接口。"""

from __future__ import annotations

import json
from typing import Any, Optional

from loguru import logger
from openai import AsyncOpenAI

from kaguya.config import LLMModelConfig


class LLMClient:
    """OpenAI 兼容的 LLM 客户端。"""

    def __init__(self, config: LLMModelConfig, name: str = "primary"):
        self.config = config
        self.name = name
        self._client = AsyncOpenAI(
            api_key=config.api_key or "dummy",
            base_url=config.base_url or None,
        )
        self.total_prompt_tokens = 0
        self.total_completion_tokens = 0
        self.total_requests = 0
        logger.info(f"LLM [{name}] 初始化: model={config.model}, base_url={config.base_url}")

    async def chat(
        self,
        messages: list[dict[str, Any]],
        tools: Optional[list[dict]] = None,
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
    ) -> dict[str, Any]:
        """发送聊天请求，返回完整响应。"""
        kwargs: dict[str, Any] = {
            "model": self.config.model,
            "messages": messages,
            "temperature": temperature if temperature is not None else self.config.temperature,
            "max_tokens": max_tokens or self.config.max_tokens,
        }

        if tools:
            kwargs["tools"] = tools
            kwargs["tool_choice"] = "auto"

        try:
            response = await self._client.chat.completions.create(**kwargs)
        except Exception as e:
            logger.error(f"[{self.name}] LLM 请求失败: {e}")
            raise

        usage = response.usage
        if usage:
            self.total_prompt_tokens += usage.prompt_tokens
            self.total_completion_tokens += usage.completion_tokens
            self.total_requests += 1
            logger.debug(
                f"[{self.name}] tokens: prompt={usage.prompt_tokens}, "
                f"completion={usage.completion_tokens} "
                f"(累计 {self.total_prompt_tokens + self.total_completion_tokens})"
            )

        choice = response.choices[0]
        result: dict[str, Any] = {
            "content": choice.message.content or "",
            "tool_calls": [],
            "finish_reason": choice.finish_reason,
        }

        if getattr(choice.message, "tool_calls", None):
            result["raw_tool_calls"] = [tc.model_dump() for tc in choice.message.tool_calls]
            for tc in choice.message.tool_calls:
                try:
                    args = json.loads(tc.function.arguments)
                except Exception:
                    args = {}
                result["tool_calls"].append({
                    "id": tc.id,
                    "name": tc.function.name,
                    "arguments": args,
                })

        return result

    async def summarize(self, texts: list[str], instruction: str = "") -> str:
        """将多段文本按 instruction 要求进行摘要。"""
        content = "\n---\n".join(texts)
        system_msg = instruction or "请简明扼要地总结以下内容。"
        response = await self.chat(
            messages=[
                {"role": "system", "content": system_msg},
                {"role": "user", "content": content},
            ],
            temperature=0.3,
        )
        return response["content"]
