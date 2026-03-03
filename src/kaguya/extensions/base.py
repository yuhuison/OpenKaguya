"""extensions/base.py — 扩展基类、阶段枚举、扩展上下文。

扩展系统允许第三方模块为 AI 提供：
  - 通知拉取器（自定义通知源）
  - 工具集（按阶段返回不同工具）
  - Prompt 注入（按阶段动态注入）
  - 聊天接口（主动发起 AI 对话）
"""

from __future__ import annotations

import abc
from enum import Enum
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from kaguya.config import AppConfig
    from kaguya.core.engine import ChatEngine
    from kaguya.core.memory import RecursiveMemory


class Stage(str, Enum):
    """AI 处理阶段。"""

    CONSCIOUSNESS = "consciousness"  # 心跳、定时器
    NOTIFICATION = "notification"    # 通知处理
    CHAT = "chat"                    # 普通聊天


class Extension(abc.ABC):
    """扩展基类。

    子类必须实现 ``name`` 属性，其余方法均为可选。
    """

    @property
    @abc.abstractmethod
    def name(self) -> str:
        """扩展唯一标识符（如 ``'wechat'``）。"""

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------

    async def setup(self, ctx: ExtensionContext) -> None:
        """初始化。*ctx* 提供对核心服务的访问。"""

    async def teardown(self) -> None:
        """清理资源。"""

    # ------------------------------------------------------------------
    # 通知拉取
    # ------------------------------------------------------------------

    async def get_notifications(self) -> list[dict[str, Any]]:
        """返回新通知。

        格式: ``[{"pkg": "", "title": "", "text": "", "when": 0}]``
        """
        return []

    # ------------------------------------------------------------------
    # 工具集
    # ------------------------------------------------------------------

    def get_tools(self, stage: Stage) -> list[dict]:
        """返回该阶段可用的 OpenAI function schemas。"""
        return []

    async def execute_tool(self, tool_name: str, args: dict[str, Any]) -> dict[str, Any]:
        """执行工具调用。"""
        return {"error": f"未知工具: {tool_name}"}

    # ------------------------------------------------------------------
    # Prompt 注入
    # ------------------------------------------------------------------

    async def get_prompt(self, stage: Stage) -> str:
        """返回要注入到 system prompt 的文本片段。"""
        return ""

    # ------------------------------------------------------------------
    # 后台任务
    # ------------------------------------------------------------------

    async def run_background(self) -> None:
        """可选的后台协程（长期运行）。

        ``ExtensionManager`` 会在启动时检查子类是否覆盖了此方法，
        若覆盖则作为 ``asyncio.Task`` 后台运行。
        """


class ExtensionContext:
    """提供给扩展的核心服务访问入口。"""

    def __init__(
        self,
        engine: ChatEngine,
        memory: RecursiveMemory,
        app_config: AppConfig,
    ) -> None:
        self._engine = engine
        self.memory = memory
        self.config = app_config

    async def chat(
        self,
        message: str,
        trigger: str = "extension",
        pre_activate_groups: list[str] | None = None,
    ) -> str:
        """发起一轮 AI 对话并获取回复。

        经过完整的 engine 流水线（system prompt + 工具 + 记忆）。

        .. note::

            engine 内部有锁，如果正在处理其他消息会等待。
        """
        return await self._engine.handle_consciousness(
            message,
            trigger=trigger,
            pre_activate_groups=pre_activate_groups,
        )

    def get_extension_config(self, ext_name: str) -> dict[str, Any]:
        """获取 ``[extensions.<ext_name>]`` 配置段。"""
        return self.config.extensions_raw.get(ext_name, {})
