"""
LLM API 客户端 — 基于 OpenAI 兼容接口。
"""

from __future__ import annotations

from typing import Any, Optional

from loguru import logger
from openai import AsyncOpenAI

from kaguya.config import LLMModelConfig


class LLMClient:
    """
    OpenAI 兼容的 LLM 客户端。

    支持所有 OpenAI-compatible API（DeepSeek、Qwen、本地 vLLM 等）。
    """

    def __init__(self, config: LLMModelConfig, name: str = "primary"):
        self.config = config
        self.name = name
        self._client = AsyncOpenAI(
            api_key=config.api_key,
            base_url=config.base_url,
        )
        # 累计 Token 用量统计
        self.total_prompt_tokens = 0
        self.total_completion_tokens = 0
        self.total_requests = 0
        logger.info(f"LLM 客户端 [{name}] 初始化: model={config.model}, base_url={config.base_url}")

    async def chat(
        self,
        messages: list[dict[str, Any]],
        tools: Optional[list[dict]] = None,
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
        response_format: Optional[dict] = None,
        **kwargs,
    ) -> dict[str, Any]:
        """
        发送聊天请求，返回完整的响应。

        Args:
            messages: OpenAI 格式的消息列表
            tools: OpenAI 格式的工具定义列表
            temperature: 温度参数（留空则使用配置默认值）
            max_tokens: 最大 token 数

        Returns:
            OpenAI 格式的响应字典
        """
        kwargs: dict[str, Any] = {
            "model": self.config.model,
            "messages": messages,
            "temperature": temperature or self.config.temperature,
            "max_tokens": max_tokens or self.config.max_tokens,
        }

        # 控制 reasoning/thinking（通过 extra_body 传递给 OpenRouter 等兼容 API）
        if self.config.reasoning_effort is not None:
            kwargs["extra_body"] = {"reasoning": {"effort": self.config.reasoning_effort}}

        if tools:
            kwargs["tools"] = tools
            kwargs["tool_choice"] = "auto"

        if response_format:
            kwargs["response_format"] = response_format

        try:
            response = await self._client.chat.completions.create(**kwargs)

            usage = response.usage
            if usage:
                self.total_prompt_tokens += usage.prompt_tokens
                self.total_completion_tokens += usage.completion_tokens
                self.total_requests += 1
                logger.debug(
                    f"[{self.name}] Token 用量: "
                    f"prompt={usage.prompt_tokens}, "
                    f"completion={usage.completion_tokens}, "
                    f"total={usage.total_tokens} "
                    f"(累计: {self.total_prompt_tokens + self.total_completion_tokens})"
                )

            choice = response.choices[0]
            result: dict[str, Any] = {
                "content": choice.message.content or "",
                "tool_calls": [],
                "usage": {
                    "prompt_tokens": usage.prompt_tokens if usage else 0,
                    "completion_tokens": usage.completion_tokens if usage else 0,
                    "total_tokens": usage.total_tokens if usage else 0,
                },
                "finish_reason": choice.finish_reason,
            }

            # 解析工具调用
            if getattr(choice.message, "tool_calls", None):
                import json

                result["raw_tool_calls"] = [tc.model_dump() for tc in choice.message.tool_calls]
                for tc in choice.message.tool_calls:
                    # 尝试解析 JSON，如果失败则记录原始字符串
                    try:
                        args = json.loads(tc.function.arguments)
                    except Exception:
                        args = tc.function.arguments
                    
                    result["tool_calls"].append({
                        "id": tc.id,
                        "name": tc.function.name,
                        "arguments": args,
                    })

            return result

        except Exception as e:
            logger.error(f"[{self.name}] LLM 请求失败: {e}")
            raise

    async def quick_judge(self, prompt: str) -> str:
        """
        快速判断（用于群聊预判等场景）。
        使用低 token 限制快速得出结论。
        """
        response = await self.chat(
            messages=[{"role": "user", "content": prompt}],
            max_tokens=100,
            temperature=0.3,
        )
        return response["content"]

    async def summarize(self, texts: list[str], instruction: str = "") -> str:
        """总结文本内容。"""
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
