"""sub_agent.py — 子 Agent 委派工具。

允许辉夜姬将复杂任务委派给子 Agent 执行。
子 Agent 拥有主 Agent 的大部分工具（有黑名单限制）。
"""

from __future__ import annotations

import json
from typing import Any

from loguru import logger

# ---------------------------------------------------------------------------
# 工具黑名单
# ---------------------------------------------------------------------------

# 所有子 Agent 都不能使用的工具
ALWAYS_BLOCKED = {
    "run_sub_agent",   # 防递归
    "set_avatar",      # 子 Agent 不应改形象
    "use_phone",       # 子 Agent 已有所有工具，不需要 gateway
    "use_browser",     # 同上
}

# fast 模式额外屏蔽（较重的工具）
FAST_BLOCKED = {
    "workspace_terminal",
    "generate_image",
    "edit_image",
}

MAX_OUTPUT_CHARS = 8000

SUB_AGENT_SYSTEM = (
    "你是辉夜姬的子代理，负责完成指定任务。"
    "直接返回任务结果，不要闲聊。简洁明了。"
)


# ---------------------------------------------------------------------------
# 工具 Schema 定义
# ---------------------------------------------------------------------------

SUB_AGENT_TOOLS: list[dict] = [
    {
        "type": "function",
        "function": {
            "name": "run_sub_agent",
            "description": (
                "启动子 Agent 来执行复杂任务。子 Agent 可以使用手机工具、文件操作等。"
                "适合耗时较长或需要多步操作的任务（如搜索信息、处理文件等）。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "task": {
                        "type": "string",
                        "description": "要完成的任务描述",
                    },
                    "model": {
                        "type": "string",
                        "enum": ["primary", "fast"],
                        "description": (
                            "使用的模型。primary: 主模型（更强但更慢更贵），"
                            "fast: 快速模型（更快更便宜，适合简单任务）。默认 primary"
                        ),
                        "default": "primary",
                    },
                    "context": {
                        "type": "string",
                        "description": "额外上下文信息（可选）",
                    },
                },
                "required": ["task"],
            },
        },
    },
]


# ---------------------------------------------------------------------------
# 工具执行器
# ---------------------------------------------------------------------------


class SubAgentToolExecutor:
    """子 Agent 委派执行器。"""

    def __init__(
        self,
        primary_llm,   # LLMClient
        secondary_llm,  # LLMClient (summarizer / fast)
        all_tools: list[dict],
        all_executors: list,
    ):
        self.primary_llm = primary_llm
        self.secondary_llm = secondary_llm
        self.all_tools = all_tools
        self.all_executors = all_executors

    async def execute(self, tool_name: str, args: dict[str, Any]) -> dict[str, Any]:
        if tool_name != "run_sub_agent":
            return {"error": f"未知工具: {tool_name}"}
        try:
            return await self._run_sub_agent(
                task=args["task"],
                model=args.get("model", "primary"),
                context=args.get("context", ""),
            )
        except Exception as e:
            logger.error(f"子 Agent 执行失败: {e}")
            return {"error": str(e)}

    async def _run_sub_agent(
        self, task: str, model: str = "primary", context: str = ""
    ) -> dict[str, Any]:
        """运行子 Agent 多轮工具调用循环。"""
        # 选择 LLM 和配置
        is_fast = model == "fast"
        llm = self.secondary_llm if is_fast else self.primary_llm
        max_iters = 5 if is_fast else 15

        # 过滤工具
        blocked = ALWAYS_BLOCKED | (FAST_BLOCKED if is_fast else set())
        tools = [t for t in self.all_tools if t["function"]["name"] not in blocked]

        # 构建消息
        system_msg = SUB_AGENT_SYSTEM
        if context:
            system_msg += f"\n\n上下文信息：{context}"

        messages: list[dict[str, Any]] = [
            {"role": "system", "content": system_msg},
            {"role": "user", "content": task},
        ]

        logger.info(f"子 Agent 启动: model={model}, task={task[:80]}...")

        reply = ""
        for i in range(max_iters):
            response = await llm.chat(messages, tools=tools)
            content_text = response.get("content", "")
            tool_calls = response.get("tool_calls", [])

            assistant_msg: dict[str, Any] = {"role": "assistant", "content": content_text}
            if response.get("raw_tool_calls"):
                assistant_msg["tool_calls"] = response["raw_tool_calls"]
            messages.append(assistant_msg)

            if not tool_calls:
                reply = content_text
                break

            # 执行工具
            for tc in tool_calls:
                t_name = tc["name"]
                t_args = tc["arguments"] if isinstance(tc["arguments"], dict) else {}
                t_id = tc["id"]

                logger.debug(f"子 Agent 工具: {t_name}({json.dumps(t_args, ensure_ascii=False)[:200]})")

                result = await self._execute_tool(t_name, t_args)

                # 截断过长结果
                result_str = json.dumps(result, ensure_ascii=False)
                if len(result_str) > MAX_OUTPUT_CHARS:
                    result = {
                        "truncated": True,
                        "summary": result_str[:MAX_OUTPUT_CHARS] + "...(截断)",
                    }

                content = json.dumps(result, ensure_ascii=False, indent=2)
                messages.append({"role": "tool", "tool_call_id": t_id, "content": content})
        else:
            if not reply:
                reply = "（子 Agent 处理超时）"

        logger.info(f"子 Agent 完成: {reply[:100]}...")
        return {"success": True, "result": reply}

    async def _execute_tool(self, tool_name: str, args: dict) -> dict[str, Any]:
        """在所有执行器中查找并执行工具。"""
        for executor in self.all_executors:
            if hasattr(executor, "execute"):
                try:
                    result = await executor.execute(tool_name, args)
                    if result.get("error") != f"未知工具: {tool_name}":
                        return result
                except Exception as e:
                    return {"error": str(e)}
        return {"error": f"未知工具: {tool_name}"}
