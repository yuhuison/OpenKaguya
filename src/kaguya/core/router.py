"""core/router.py — 工具路由器：按需激活工具组，节省 token。

Gateway 工具（use_desktop / use_browser）调用后解锁对应工具组，
该组工具在当前对话轮次内持续可见。
基础工具（笔记、定时器、图片等）始终可见。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from loguru import logger


@dataclass
class ToolGroup:
    """一组相关工具及其执行器。"""

    name: str
    tools: list[dict]           # OpenAI function schemas
    executor: Any               # 需有 async execute(tool_name, args) -> dict
    is_base: bool = False       # True = 始终可见, False = 需要 gateway 激活


@dataclass
class GatewayDef:
    """Gateway 工具定义：调用后激活对应的 gated group。"""

    tool_name: str              # e.g. "use_desktop"
    activates: str              # group name, e.g. "desktop"
    description: str            # 工具描述（给 LLM 看）
    result_message: str         # 激活后返回给 LLM 的消息


class ToolRouter:
    """工具路由器 — 管理工具组的注册、gateway 激活和 per-turn 可见性。"""

    def __init__(self):
        self._groups: dict[str, ToolGroup] = {}
        self._gateways: dict[str, GatewayDef] = {}
        self._active_groups: set[str] = set()
        self._gateway_tools: list[dict] = []

    # ------------------------------------------------------------------
    # 注册
    # ------------------------------------------------------------------

    def register_group(self, group: ToolGroup) -> None:
        """注册一个工具组。"""
        self._groups[group.name] = group
        logger.debug(f"工具组注册: {group.name} ({len(group.tools)} tools, base={group.is_base})")

    def register_gateway(self, gateway: GatewayDef) -> None:
        """注册一个 gateway 工具（自动生成 OpenAI function schema）。"""
        self._gateways[gateway.tool_name] = gateway
        schema = {
            "type": "function",
            "function": {
                "name": gateway.tool_name,
                "description": gateway.description,
                "parameters": {"type": "object", "properties": {}, "required": []},
            },
        }
        self._gateway_tools.append(schema)
        logger.debug(f"Gateway 注册: {gateway.tool_name} → 激活 [{gateway.activates}]")

    # ------------------------------------------------------------------
    # Per-turn 状态管理
    # ------------------------------------------------------------------

    def reset(self) -> None:
        """重置当前轮次的激活状态（每次 _process 开始时调用）。"""
        self._active_groups.clear()

    def pre_activate(self, *group_names: str) -> None:
        """预激活指定 group（意识系统用，避免多一轮 gateway 调用）。"""
        for name in group_names:
            if name in self._groups:
                self._active_groups.add(name)
                logger.debug(f"预激活工具组: {name}")

    # ------------------------------------------------------------------
    # 工具列表
    # ------------------------------------------------------------------

    def get_active_tools(self) -> list[dict]:
        """返回当前可见的所有工具 schema（base + 已激活 gated + 未激活的 gateway）。"""
        tools: list[dict] = []
        for group in self._groups.values():
            if group.is_base or group.name in self._active_groups:
                tools.extend(group.tools)
        # gateway 工具：只展示尚未激活的（已激活的就不需要了）
        for gw_schema in self._gateway_tools:
            gw_name = gw_schema["function"]["name"]
            gw_def = self._gateways[gw_name]
            if gw_def.activates not in self._active_groups:
                tools.append(gw_schema)
        return tools

    def get_all_tools(self) -> list[dict]:
        """返回所有工具 schema（SubAgent 用，忽略 gateway 状态，不含 gateway 工具）。"""
        tools: list[dict] = []
        for group in self._groups.values():
            tools.extend(group.tools)
        return tools

    def get_all_executors(self) -> list:
        """返回所有 executor（SubAgent 用）。"""
        return [group.executor for group in self._groups.values()]

    # ------------------------------------------------------------------
    # 工具执行
    # ------------------------------------------------------------------

    async def execute_tool(self, tool_name: str, args: dict) -> dict[str, Any]:
        """在所有已注册 executor 中查找并执行工具（含 gateway）。"""
        # 先尝试 gateway
        if tool_name in self._gateways:
            return await self._execute_gateway(tool_name)

        # 再遍历各组 executor
        for group in self._groups.values():
            executor = group.executor
            if hasattr(executor, "execute"):
                try:
                    result = await executor.execute(tool_name, args)
                    if result.get("error") != f"未知工具: {tool_name}":
                        return result
                except Exception as e:
                    logger.error(f"工具 [{tool_name}] 执行异常: {e}")
                    return {"error": str(e)}
        return {"error": f"未知工具: {tool_name}"}

    async def _execute_gateway(self, tool_name: str) -> dict[str, Any]:
        """处理 gateway 工具调用。"""
        gw = self._gateways[tool_name]
        self._active_groups.add(gw.activates)
        logger.info(f"Gateway [{tool_name}] 激活工具组: {gw.activates}")

        group = self._groups.get(gw.activates)
        tool_names = [t["function"]["name"] for t in group.tools] if group else []
        return {
            "activated": gw.activates,
            "message": gw.result_message,
            "available_tools": tool_names,
        }
