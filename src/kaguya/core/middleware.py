"""
中间件系统。

用于在进入 LLM 推理之前获取上下文（如记忆、群聊状态），
并在 LLM 回复之后进行后处理（如保存记录、触发向量化）。
"""

from __future__ import annotations

import abc

from kaguya.core.types import UnifiedMessage


class Middleware(abc.ABC):
    """
    辉夜姬对话引擎的中间件基类。
    """

    @property
    def name(self) -> str:
        return self.__class__.__name__

    async def pre_process(self, message: UnifiedMessage) -> str | None:
        """
        前置处理：在引擎构建上下文之前调用。
        
        Args:
            message: 用户发送的原始消息
            
        Returns:
            如果返回字符串，这部分内容将作为附加的系统级提示语，
            注入到 LLM 的上下文中（例如查询到的记忆片段）。
            返回 None 则不注入任何内容。
        """
        return None

    async def post_process(self, message: UnifiedMessage, replies: list[str]) -> None:
        """
        后置处理：在引擎处理完所有工作，准备返回给用户前调用。
        
        Args:
            message: 用户发送的原始消息
            replies: 辉夜姬计算出的准备发送的回复列表
        """
        pass
