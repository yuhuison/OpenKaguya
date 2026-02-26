"""tools/task.py — 任务追踪工具。

让 AI 显式声明任务的开始、进度更新、完成/中断，
配合引擎循环实现「任务未完成则强制继续」的逻辑。
"""

from __future__ import annotations

from typing import Any


# ---------------------------------------------------------------------------
# 工具 Schema
# ---------------------------------------------------------------------------

TASK_TOOLS: list[dict] = [
    {
        "type": "function",
        "function": {
            "name": "start_current_task",
            "description": (
                "声明开始执行一个任务。在执行多步骤操作前调用，"
                "系统会追踪该任务直到你标记完成或中断。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {
                        "type": "string",
                        "description": "任务名称，简要描述要做什么",
                    },
                    "goal": {
                        "type": "string",
                        "description": "任务目标，具体说明完成标准",
                    },
                },
                "required": ["name", "goal"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "update_current_task_status",
            "description": "更新当前任务的进度状态，汇报已完成的步骤和下一步计划。",
            "parameters": {
                "type": "object",
                "properties": {
                    "status": {
                        "type": "string",
                        "description": "当前进度描述",
                    },
                },
                "required": ["status"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "mark_current_task_done",
            "description": (
                "标记当前任务已完成或已中断。任务结束时必须调用。"
                "如果无法完成，将 interrupted 设为 true 并说明原因。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "result": {
                        "type": "string",
                        "description": "任务结果说明",
                    },
                    "interrupted": {
                        "type": "boolean",
                        "description": "是否中断（未能完成）。默认 false 表示正常完成。",
                    },
                },
                "required": ["result"],
            },
        },
    },
]


# ---------------------------------------------------------------------------
# TaskTracker — 单次 _process() 内的任务状态
# ---------------------------------------------------------------------------


class TaskTracker:
    """追踪当前任务状态，每次 _process() 开始时 reset。"""

    def __init__(self) -> None:
        self.reset()

    def reset(self) -> None:
        self.active: bool = False
        self.task_name: str = ""
        self.task_goal: str = ""
        self.status: str = ""
        self.finished: bool = False

    def start(self, name: str, goal: str) -> None:
        self.active = True
        self.task_name = name
        self.task_goal = goal
        self.status = ""
        self.finished = False

    def update_status(self, status: str) -> None:
        self.status = status

    def finish(self) -> None:
        self.finished = True

    def needs_continuation(self) -> bool:
        """任务已开始但未标记完成/中断。"""
        return self.active and not self.finished


# ---------------------------------------------------------------------------
# 工具执行器
# ---------------------------------------------------------------------------


class TaskToolExecutor:
    def __init__(self, tracker: TaskTracker) -> None:
        self.tracker = tracker

    async def execute(self, tool_name: str, args: dict[str, Any]) -> dict[str, Any]:
        if tool_name == "start_current_task":
            name = args.get("name", "")
            goal = args.get("goal", "")
            self.tracker.start(name, goal)
            return {
                "success": True,
                "message": f"任务已开始：{name}（目标：{goal}）",
            }

        elif tool_name == "update_current_task_status":
            status = args.get("status", "")
            if not self.tracker.active:
                return {"error": "当前没有进行中的任务，请先调用 start_current_task"}
            self.tracker.update_status(status)
            return {
                "success": True,
                "message": f"任务进度已更新：{status}",
            }

        elif tool_name == "mark_current_task_done":
            result = args.get("result", "")
            interrupted = args.get("interrupted", False)
            if not self.tracker.active:
                return {"error": "当前没有进行中的任务"}
            self.tracker.finish()
            label = "中断" if interrupted else "完成"
            return {
                "success": True,
                "message": f"任务已{label}：{result}",
                "interrupted": interrupted,
            }

        return {"error": f"未知工具: {tool_name}"}
