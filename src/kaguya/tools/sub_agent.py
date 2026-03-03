"""sub_agent.py — 经理/执行者 子代理会话系统。

大模型（经理）通过 create/instruct/done 三个工具管理子代理会话。
子代理（小模型）使用桌面/浏览器工具执行任务，通过 report 工具汇报结果。
"""

from __future__ import annotations

import json
import re
import uuid
from dataclasses import dataclass, field
from typing import Any

from loguru import logger


# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

MAX_SUB_AGENT_TURNS = 30
MAX_OUTPUT_CHARS = 8000
REPORT_TOOL_NAME = "report"

SUB_AGENT_SYSTEM_PROMPT = (
    "你是辉夜姬的执行代理。你的职责是使用工具完成指定任务，然后调用 report 汇报结果。\n"
    "工作流程：\n"
    "1. 分析任务要求\n"
    "2. 使用工具逐步执行（每步操作后截图确认）\n"
    "3. 完成后调用 report 汇报\n"
    "4. 遇到困难也要调用 report(status='difficulty') 说明情况\n\n"
    "注意：\n"
    "- 每次操作后截图确认结果\n"
    "- 不要闲聊，专注于任务\n"
    "- report 是你唯一的沟通方式\n"
)


# ---------------------------------------------------------------------------
# Report 工具 Schema（注入到子代理会话）
# ---------------------------------------------------------------------------

REPORT_TOOL_SCHEMA: dict = {
    "type": "function",
    "function": {
        "name": "report",
        "description": (
            "向管理者汇报当前状态。这是你与管理者沟通的唯一方式。\n"
            "完成任务时 status='success'，遇到困难时 status='difficulty'，"
            "有疑问时 status='question'。\n"
            "如果最近截过图，可以设 include_screenshot=true 让管理者看到当前画面。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "text": {
                    "type": "string",
                    "description": "汇报内容",
                },
                "status": {
                    "type": "string",
                    "enum": ["success", "difficulty", "question"],
                    "description": "汇报状态",
                },
                "include_screenshot": {
                    "type": "boolean",
                    "description": "是否附带最近的截图给管理者查看（默认 false）",
                },
            },
            "required": ["text", "status"],
        },
    },
}


# ---------------------------------------------------------------------------
# 大模型管理工具 Schema
# ---------------------------------------------------------------------------

AGENT_MANAGEMENT_TOOLS: list[dict] = [
    {
        "type": "function",
        "function": {
            "name": "create_sub_agent_session",
            "description": (
                "创建一个子代理会话，用于执行桌面或浏览器操作任务。"
                "子代理可以截图、点击、输入等操控电脑。"
                "创建后使用 instruct_to_sub_agent 发送指令。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "tools": {
                        "type": "string",
                        "enum": ["desktop", "browser", "both"],
                        "description": (
                            "子代理可用的工具集："
                            "desktop=桌面操作, browser=浏览器, both=两者都有"
                        ),
                    },
                    "task": {
                        "type": "string",
                        "description": "任务描述，让子代理了解总体目标",
                    },
                },
                "required": ["tools", "task"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "instruct_to_sub_agent",
            "description": (
                "向子代理发送指令并等待执行结果。"
                "子代理会执行工具操作直到调用 report 汇报。"
                "返回子代理的汇报内容（可能包含截图）。"
                "可以多次调用 instruct_to_sub_agent 发送后续指令。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "session_id": {
                        "type": "string",
                        "description": "create_sub_agent_session 返回的会话 ID",
                    },
                    "message": {
                        "type": "string",
                        "description": "发送给子代理的指令",
                    },
                },
                "required": ["session_id", "message"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "done_sub_agent",
            "description": "关闭子代理会话，释放资源。任务完成后调用。",
            "parameters": {
                "type": "object",
                "properties": {
                    "session_id": {
                        "type": "string",
                        "description": "要关闭的会话 ID",
                    },
                },
                "required": ["session_id"],
            },
        },
    },
]


# ---------------------------------------------------------------------------
# 子代理会话
# ---------------------------------------------------------------------------


@dataclass
class SubAgentSession:
    """一次子代理会话，持久化消息历史。"""

    session_id: str
    task: str
    tool_group: str  # "desktop" | "browser" | "both"
    messages: list[dict[str, Any]] = field(default_factory=list)
    tools: list[dict] = field(default_factory=list)
    last_screenshot_b64: str | None = None
    last_screenshot_media_type: str = "image/jpeg"
    turn_count: int = 0
    closed: bool = False


# ---------------------------------------------------------------------------
# 子代理管理器
# ---------------------------------------------------------------------------


class SubAgentManager:
    """管理子代理会话，执行工具调用循环。"""

    def __init__(
        self,
        agent_llm: Any,
        desktop_tools: list[dict] | None = None,
        desktop_executor: Any | None = None,
        browser_tools: list[dict] | None = None,
        browser_executor: Any | None = None,
        max_turns: int = MAX_SUB_AGENT_TURNS,
    ):
        self.agent_llm = agent_llm
        self.desktop_tools = desktop_tools or []
        self.desktop_executor = desktop_executor
        self.browser_tools = browser_tools or []
        self.browser_executor = browser_executor
        self.max_turns = max_turns
        self._sessions: dict[str, SubAgentSession] = {}

    # -- 公开 API --

    def create_session(self, tools: str, task: str) -> str:
        """创建子代理会话，返回 session_id。"""
        session_id = uuid.uuid4().hex[:8]

        available_tools: list[dict] = []
        if tools in ("desktop", "both"):
            available_tools.extend(self.desktop_tools)
        if tools in ("browser", "both"):
            available_tools.extend(self.browser_tools)
        available_tools.append(REPORT_TOOL_SCHEMA)

        system_msg = SUB_AGENT_SYSTEM_PROMPT + f"\n\n当前任务：{task}"
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": system_msg},
        ]

        session = SubAgentSession(
            session_id=session_id,
            task=task,
            tool_group=tools,
            messages=messages,
            tools=available_tools,
        )
        self._sessions[session_id] = session
        logger.info(
            f"子代理会话创建: {session_id}, tools={tools}, task={task[:80]}"
        )
        return session_id

    async def instruct(
        self, session_id: str, message: str,
    ) -> dict[str, Any]:
        """向子代理发送指令，运行工具循环直到 report 或超时。"""
        session = self._sessions.get(session_id)
        if not session:
            return {"error": f"会话不存在: {session_id}"}
        if session.closed:
            return {"error": f"会话已关闭: {session_id}"}

        session.messages.append({"role": "user", "content": message})
        return await self._tool_loop(session)

    def close_session(self, session_id: str) -> dict[str, Any]:
        """关闭并移除子代理会话。"""
        session = self._sessions.pop(session_id, None)
        if not session:
            return {"error": f"会话不存在: {session_id}"}
        session.closed = True
        logger.info(f"子代理会话关闭: {session_id}")
        return {"success": True, "message": f"会话 {session_id} 已关闭"}

    def close_all(self) -> None:
        """关闭所有会话（router.reset 时调用）。"""
        for session in self._sessions.values():
            session.closed = True
        self._sessions.clear()

    # -- 核心工具循环 --

    async def _tool_loop(
        self, session: SubAgentSession,
    ) -> dict[str, Any]:
        """执行 LLM 工具调用循环，返回 report 数据。"""
        for _ in range(self.max_turns):
            session.turn_count += 1

            try:
                response = await self.agent_llm.chat(
                    session.messages, tools=session.tools,
                )
            except Exception as e:
                logger.error(
                    f"子代理[{session.session_id}] LLM 请求失败: {e}"
                )
                return {
                    "text": f"子代理 LLM 请求失败: {e}",
                    "status": "difficulty",
                    "turns_used": session.turn_count,
                }

            raw_content = response.get("content", "")
            tool_calls = response.get("tool_calls", [])

            # 添加 assistant 消息
            assistant_msg: dict[str, Any] = {
                "role": "assistant",
                "content": raw_content,
            }
            if response.get("raw_tool_calls"):
                assistant_msg["tool_calls"] = response["raw_tool_calls"]
            session.messages.append(assistant_msg)

            if not tool_calls:
                # 纯文本回复 → 隐式 success report
                content_text = re.sub(r"</?think>", "", raw_content).strip()
                return {
                    "text": content_text or "(子代理未返回内容)",
                    "status": "success",
                    "turns_used": session.turn_count,
                }

            # 执行工具调用
            report_result = None
            for tc in tool_calls:
                t_name = tc["name"]
                t_args = (
                    tc["arguments"]
                    if isinstance(tc["arguments"], dict)
                    else {}
                )
                t_id = tc["id"]

                # report 之后的工具调用跳过
                if report_result is not None:
                    session.messages.append({
                        "role": "tool",
                        "tool_call_id": t_id,
                        "content": '{"skipped": true}',
                    })
                    continue

                if t_name == REPORT_TOOL_NAME:
                    report_result = self._handle_report(session, t_args)
                    session.messages.append({
                        "role": "tool",
                        "tool_call_id": t_id,
                        "content": '{"acknowledged": true}',
                    })
                    continue

                # 执行实际工具
                logger.debug(
                    f"子代理[{session.session_id}] 工具: "
                    f"{t_name}({json.dumps(t_args, ensure_ascii=False)[:200]})"
                )
                result = await self._execute_tool(session, t_name, t_args)

                # 跟踪截图
                if "image_base64" in result:
                    session.last_screenshot_b64 = result["image_base64"]
                    session.last_screenshot_media_type = result.get(
                        "image_media_type", "image/jpeg",
                    )

                tool_messages = _build_tool_result_messages(
                    t_id, t_name, result,
                )
                session.messages.extend(tool_messages)

            if report_result is not None:
                return report_result

        # 超过最大轮数
        logger.warning(
            f"子代理[{session.session_id}] "
            f"超过最大轮数 ({self.max_turns})"
        )
        return {
            "text": "（子代理执行超时，已达到最大操作轮数）",
            "status": "difficulty",
            "turns_used": session.turn_count,
            "max_turns_exceeded": True,
        }

    def _handle_report(
        self, session: SubAgentSession, args: dict,
    ) -> dict[str, Any]:
        """处理 report 工具调用，构建返回值。"""
        text = args.get("text", "")
        status = args.get("status", "success")
        include_screenshot = args.get("include_screenshot", False)

        result: dict[str, Any] = {
            "text": text,
            "status": status,
            "turns_used": session.turn_count,
        }

        if include_screenshot and session.last_screenshot_b64:
            result["image_base64"] = session.last_screenshot_b64
            result["image_media_type"] = session.last_screenshot_media_type

        return result

    async def _execute_tool(
        self,
        session: SubAgentSession,
        tool_name: str,
        args: dict,
    ) -> dict[str, Any]:
        """按会话的 tool_group 路由到对应 executor。"""
        executors: list = []
        if session.tool_group in ("desktop", "both") and self.desktop_executor:
            executors.append(self.desktop_executor)
        if session.tool_group in ("browser", "both") and self.browser_executor:
            executors.append(self.browser_executor)

        for executor in executors:
            if hasattr(executor, "execute"):
                try:
                    result = await executor.execute(tool_name, args)
                    if result.get("error") != f"未知工具: {tool_name}":
                        return result
                except Exception as e:
                    return {"error": str(e)}

        return {"error": f"未知工具: {tool_name}"}


# ---------------------------------------------------------------------------
# 工具结果消息构建（镜像 ChatEngine._build_tool_result_messages）
# ---------------------------------------------------------------------------


def _build_tool_result_messages(
    tool_id: str, tool_name: str, result: dict[str, Any],
) -> list[dict[str, Any]]:
    """构建工具结果消息，处理多模态（图像）结果。"""
    if "image_base64" in result:
        img_b64 = result["image_base64"]
        media_type = result.get("image_media_type", "image/jpeg")
        text = result.get("text") or result.get("elements_text", "")
        if not text:
            extra = {
                k: v for k, v in result.items()
                if k not in ("image_base64", "image_media_type")
            }
            text = json.dumps(extra, ensure_ascii=False, indent=2)
        return [{
            "role": "tool",
            "tool_call_id": tool_id,
            "content": [
                {
                    "type": "image_url",
                    "image_url": {
                        "url": f"data:{media_type};base64,{img_b64}",
                    },
                },
                {"type": "text", "text": text},
            ],
        }]

    # 纯文本结果
    content = json.dumps(result, ensure_ascii=False, indent=2)
    if len(content) > MAX_OUTPUT_CHARS:
        content = content[:MAX_OUTPUT_CHARS] + "...(截断)"
    return [{"role": "tool", "tool_call_id": tool_id, "content": content}]


# ---------------------------------------------------------------------------
# 大模型侧的执行器（注册到 ToolRouter）
# ---------------------------------------------------------------------------


class AgentManagementExecutor:
    """大模型调用的子代理管理工具执行器。"""

    def __init__(self, manager: SubAgentManager):
        self.manager = manager

    async def execute(
        self, tool_name: str, args: dict[str, Any],
    ) -> dict[str, Any]:
        if tool_name == "create_sub_agent_session":
            return self._create(args)
        elif tool_name == "instruct_to_sub_agent":
            return await self._instruct(args)
        elif tool_name == "done_sub_agent":
            return self._done(args)
        return {"error": f"未知工具: {tool_name}"}

    def _create(self, args: dict) -> dict[str, Any]:
        tools = args.get("tools", "")
        task = args.get("task", "")
        if not task:
            return {"error": "task 参数不能为空"}
        if tools not in ("desktop", "browser", "both"):
            return {
                "error": f"tools 参数无效: {tools}，应为 desktop/browser/both",
            }
        session_id = self.manager.create_session(tools, task)
        return {
            "success": True,
            "session_id": session_id,
            "message": f"子代理会话已创建，工具集: {tools}",
        }

    async def _instruct(self, args: dict) -> dict[str, Any]:
        session_id = args.get("session_id", "")
        message = args.get("message", "")
        if not session_id:
            return {"error": "session_id 参数不能为空"}
        if not message:
            return {"error": "message 参数不能为空"}
        return await self.manager.instruct(session_id, message)

    def _done(self, args: dict) -> dict[str, Any]:
        session_id = args.get("session_id", "")
        if not session_id:
            return {"error": "session_id 参数不能为空"}
        return self.manager.close_session(session_id)
