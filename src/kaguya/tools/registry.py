"""
工具注册中心 — 插件化的工具管理系统。
"""

from __future__ import annotations

import abc
from typing import Any, Callable, Coroutine

from loguru import logger


class Tool(abc.ABC):
    """
    工具基类。

    每个工具需要定义：
    - name: 工具名称（Function Calling 用）
    - description: 工具描述
    - parameters: OpenAI JSON Schema 格式的参数定义
    - execute(): 异步执行方法
    """

    @property
    @abc.abstractmethod
    def name(self) -> str: ...

    @property
    @abc.abstractmethod
    def description(self) -> str: ...

    @property
    @abc.abstractmethod
    def parameters(self) -> dict: ...

    @abc.abstractmethod
    async def execute(self, **kwargs) -> str:
        """执行工具，返回结果字符串"""
        ...

    def to_openai_schema(self) -> dict:
        """转换为 OpenAI Function Calling 格式"""
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }


class ToolRegistry:
    """
    工具注册中心。

    管理所有可用工具，提供 OpenAI 格式输出和统一执行接口。
    """

    def __init__(self):
        self._tools: dict[str, Tool] = {}

    def register(self, tool: Tool) -> None:
        """注册一个工具"""
        self._tools[tool.name] = tool
        logger.debug(f"工具已注册: {tool.name}")

    def unregister(self, name: str) -> None:
        """注销一个工具"""
        if name in self._tools:
            del self._tools[name]
            logger.debug(f"工具已注销: {name}")

    def register_all(self, tools: list[Tool]) -> None:
        """批量注册工具"""
        for tool in tools:
            self.register(tool)

    def get(self, name: str) -> Tool | None:
        """获取工具"""
        return self._tools.get(name)

    def get_openai_tools(self) -> list[dict]:
        """生成 OpenAI Function Calling 格式的工具定义列表"""
        return [t.to_openai_schema() for t in self._tools.values()]

    async def execute(self, name: str, arguments: dict) -> str:
        """
        执行工具。

        Args:
            name: 工具名称
            arguments: 工具参数

        Returns:
            工具执行结果（字符串）
        """
        tool = self._tools.get(name)
        if not tool:
            return f"错误: 未知工具 '{name}'"

        try:
            result = await tool.execute(**arguments)
            logger.debug(f"工具执行成功: {name}")
            return result
        except Exception as e:
            logger.error(f"工具执行失败: {name} — {e}")
            return f"工具执行出错: {e}"

    def set_user_context(self, user_id: str) -> None:
        """设置当前用户上下文（传播给需要用户信息的工具）"""
        for tool in self._tools.values():
            if hasattr(tool, "_current_user_id"):
                tool._current_user_id = user_id
            # 兼容 MemoryTools 等使用 _user_id 的工具
            if hasattr(tool, "_user_id"):
                tool._user_id = user_id

    @property
    def tool_names(self) -> list[str]:
        return list(self._tools.keys())
