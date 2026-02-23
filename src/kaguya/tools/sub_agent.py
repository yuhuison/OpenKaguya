"""
子 Agent 工具 — 辉夜姬可以委派子 Agent 完成特定任务。

支持两种层级：
- primary: 使用主模型，适合复杂任务，可用完整工具集（除破坏性工具外）
- secondary: 使用次级模型，速度快上下文大，适合提取/总结/格式转换，工具受限
"""

from __future__ import annotations

import json
from typing import Any, Optional

from loguru import logger

from kaguya.llm.client import LLMClient
from kaguya.tools.registry import Tool, ToolRegistry


# 次级模型禁止使用的工具（具有潜在破坏性）
SECONDARY_BLOCKED_TOOLS = {
    "run_terminal",      # 终端命令 — 破坏性
    "delete_file",       # 文件删除 — 破坏性
    "browser_open",      # 浏览器系列 — 太重，次级模型不需要
    "browser_search",
    "browser_click",
    "browser_type",
    "browser_scroll",
    "browser_screenshot",
    "browser_get_text",
    "browser_back",
    "browser_keys",
    "browser_close",
}

# 任何子 Agent 都禁止使用的工具（防递归 + 防止直接联系用户）
ALWAYS_BLOCKED_TOOLS = {
    "send_message_to_user",  # 子 Agent 不能直接给用户发消息
    "run_sub_agent",         # 防止递归
}

# 子 Agent 系统 Prompt
SUB_AGENT_SYSTEM_PROMPT = """你是一个任务执行助手。你需要完成用户给你的具体任务，并返回结果。

规则：
1. 专注完成任务，不要闲聊
2. 如果需要使用工具获取信息，可以调用可用的工具
3. 当任务完成时，在 content 中输出最终结果
4. 结果应该完整、准确、简洁
5. 如果任务无法完成，说明原因"""


class SubAgentTool(Tool):
    """
    子 Agent 工具：委派子 Agent 完成特定任务。

    辉夜姬可以调用此工具来启动一个独立的子 Agent，
    子 Agent 拥有独立的上下文和工具调用循环，
    完成任务后将结果返回给辉夜姬。
    """

    def __init__(
        self,
        primary_llm: LLMClient,
        secondary_llm: LLMClient,
        tool_registry: ToolRegistry,
    ):
        self._primary_llm = primary_llm
        self._secondary_llm = secondary_llm
        self._tool_registry = tool_registry

    @property
    def name(self):
        return "run_sub_agent"

    @property
    def description(self):
        return (
            "启动一个子 Agent 完成特定任务并返回结果。"
            "适合有明确输入输出的任务，如：提取信息、总结长文本、格式转换、复杂搜索等。"
            "model_tier 选择：'primary'=主模型（复杂任务，可用浏览器/搜索），"
            "'secondary'=次级模型（快速，上下文大，适合总结/提取/格式化，无浏览器和终端）。"
        )

    @property
    def parameters(self):
        return {
            "type": "object",
            "properties": {
                "task": {
                    "type": "string",
                    "description": (
                        "任务描述。需要清晰说明：需要做什么、输入是什么、期望的输出格式是什么。"
                        "例如：'请总结以下文本的要点，输出3-5条要点：{文本内容}'"
                    ),
                },
                "model_tier": {
                    "type": "string",
                    "enum": ["primary", "secondary"],
                    "description": (
                        "模型层级。primary=主模型（复杂任务），"
                        "secondary=次级模型（快速处理，上下文大，适合总结/提取）"
                    ),
                },
                "context": {
                    "type": "string",
                    "description": "可选的额外上下文信息，会附加在任务描述后面",
                },
            },
            "required": ["task", "model_tier"],
        }

    async def execute(self, task: str, model_tier: str = "secondary", context: str = "", **_) -> str:
        if model_tier not in ("primary", "secondary"):
            return f"错误: model_tier 必须是 'primary' 或 'secondary'，收到: {model_tier}"

        llm = self._primary_llm if model_tier == "primary" else self._secondary_llm
        max_iterations = 15 if model_tier == "primary" else 5

        # 构建可用工具列表（过滤掉禁止的工具）
        blocked = ALWAYS_BLOCKED_TOOLS.copy()
        if model_tier == "secondary":
            blocked |= SECONDARY_BLOCKED_TOOLS
        
        available_tools = self._get_filtered_tools(blocked)
        tools_schema = [t.to_openai_schema() for t in available_tools] if available_tools else None

        # 构建消息
        user_content = task
        if context:
            user_content += f"\n\n附加上下文：\n{context}"

        messages: list[dict[str, Any]] = [
            {"role": "system", "content": SUB_AGENT_SYSTEM_PROMPT},
            {"role": "user", "content": user_content},
        ]

        tool_names = [t.name for t in available_tools] if available_tools else []
        logger.info(
            f"🤖 子 Agent 启动 (tier={model_tier}, tools={len(tool_names)}, max_iter={max_iterations})"
        )
        logger.debug(f"🤖 子 Agent 任务: {task[:200]}")

        # 运行工具调用循环
        final_output = ""
        try:
            for i in range(max_iterations):
                response = await llm.chat(messages=messages, tools=tools_schema)

                content = response.get("content", "")
                tool_calls = response.get("tool_calls", [])
                raw_tool_calls = response.get("raw_tool_calls", [])

                # 构建 assistant 消息
                assistant_msg: dict[str, Any] = {"role": "assistant"}
                if content:
                    assistant_msg["content"] = content
                if raw_tool_calls:
                    assistant_msg["tool_calls"] = raw_tool_calls
                messages.append(assistant_msg)

                # 没有工具调用 → 任务完成
                if not tool_calls:
                    final_output = content
                    break

                # 执行工具调用
                for tc in tool_calls:
                    tc_name = tc["name"]
                    tc_args = tc["arguments"]
                    tc_id = tc["id"]

                    logger.debug(f"🤖 子 Agent 工具调用: {tc_name}({json.dumps(tc_args, ensure_ascii=False)[:200]})")

                    # 通过 registry 执行（确保设了用户上下文）
                    tool_result = await self._tool_registry.execute(tc_name, tc_args)

                    # 截断过长的结果
                    if isinstance(tool_result, str) and len(tool_result) > 8000:
                        tool_result = tool_result[:8000] + "\n... [结果截断]"

                    logger.debug(f"🤖 子 Agent 工具结果: {tc_name} → {str(tool_result)[:300]}")

                    messages.append({
                        "role": "tool",
                        "tool_call_id": tc_id,
                        "content": tool_result if isinstance(tool_result, str) else json.dumps(tool_result, ensure_ascii=False),
                    })

                # 记录最新的 content 作为备选输出
                if content:
                    final_output = content

            else:
                # 到达最大轮次
                final_output = final_output or "子 Agent 达到最大执行轮次，未能完成任务。"
                logger.warning(f"🤖 子 Agent 达到最大轮次 ({max_iterations})")

        except Exception as e:
            logger.error(f"🤖 子 Agent 执行失败: {e}")
            return f"子 Agent 执行失败: {e}"

        logger.info(f"🤖 子 Agent 完成 (tier={model_tier}, output={len(final_output)}字符)")
        return final_output or "子 Agent 未返回任何结果。"

    def _get_filtered_tools(self, blocked: set[str]) -> list[Tool]:
        """从主 registry 中获取过滤后的工具列表"""
        result = []
        for name in self._tool_registry.tool_names:
            if name not in blocked:
                tool = self._tool_registry.get(name)
                if tool:
                    result.append(tool)
        return result
