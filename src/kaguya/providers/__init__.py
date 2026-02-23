"""
AI 能力提供者基类。

每个 Provider 为辉夜姬提供一种 AI 能力（图片生成、TTS 等），
并根据阶段（chat / consciousness）提供不同的工具和 prompt。
"""

from __future__ import annotations

import abc
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from kaguya.tools.registry import Tool


class BaseProvider(abc.ABC):
    """
    AI 能力提供者基类。

    子类需要实现 name 属性，并可选重写 get_tools / get_system_prompt / get_injected_prompt。
    """

    @property
    @abc.abstractmethod
    def name(self) -> str:
        """Provider 唯一标识名"""
        ...

    def get_tools(self, phase: str = "chat") -> list["Tool"]:
        """
        返回该 provider 提供的工具。

        Args:
            phase: 'chat'（用户聊天）或 'consciousness'（自我意识唤醒）
        """
        return []

    def get_system_prompt(self, phase: str = "chat") -> str:
        """返回能力描述 prompt"""
        return ""

    async def get_injected_prompt(self, phase: str = "chat") -> str:
        """返回实时数据注入 prompt（async，可能需要 API 调用）"""
        return ""
