"""
平台适配器基类。
"""

from __future__ import annotations

import abc
from typing import Callable, Coroutine, Any, TYPE_CHECKING

if TYPE_CHECKING:
    from kaguya.tools.registry import Tool

from kaguya.core.types import UnifiedMessage


# 消息处理器类型：接收 UnifiedMessage，返回要发送的消息列表
MessageHandler = Callable[[UnifiedMessage], Coroutine[Any, Any, list[str]]]


class PlatformAdapter(abc.ABC):
    """
    平台适配器基类。

    每个平台（Telegram、QQ、微信、CLI）都需要实现这个接口。
    适配器负责：
    1. 将平台特定消息转换为 UnifiedMessage
    2. 将回复消息发送到平台
    3. 提供平台专属工具和 prompt（可选）
    """

    def __init__(self, name: str):
        self.name = name
        self._handler: MessageHandler | None = None

    def set_handler(self, handler: MessageHandler) -> None:
        """设置消息处理器（由 ChatEngine 提供）"""
        self._handler = handler

    @abc.abstractmethod
    async def start(self) -> None:
        """启动适配器，开始监听消息"""
        ...

    @abc.abstractmethod
    async def stop(self) -> None:
        """停止适配器"""
        ...

    @abc.abstractmethod
    async def send_messages(
        self,
        user_id: str,
        messages: list[str],
        group_id: str | None = None,
    ) -> None:
        """发送消息到平台"""
        ...

    # ─── 平台专属能力（子类可选重写） ───

    def get_tools(self, phase: str = "chat") -> list["Tool"]:
        """
        返回该 adapter 提供的平台专属工具。

        Args:
            phase: 'chat'（用户聊天阶段）或 'consciousness'（自我意识阶段）,
                   不同阶段可返回不同工具集。
        """
        return []

    def get_system_prompt(self, phase: str = "chat") -> str:
        """
        返回平台能力描述 prompt（静态说明辉夜姬在该平台能做什么）。

        Args:
            phase: 'chat' 或 'consciousness'
        """
        return ""

    async def get_injected_prompt(self, phase: str = "chat") -> str:
        """
        返回实时数据注入 prompt（如朋友圈内容、通知等）。
        async 因为可能需要调用外部 API 获取数据。

        Args:
            phase: 'chat' 或 'consciousness'
        """
        return ""

