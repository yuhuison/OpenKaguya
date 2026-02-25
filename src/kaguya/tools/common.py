"""tools/common.py — 通用工具（定时器）。"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from kaguya.core.memory import RecursiveMemory


# ---------------------------------------------------------------------------
# 工具 Schema
# ---------------------------------------------------------------------------

COMMON_TOOLS: list[dict] = [
    {
        "type": "function",
        "function": {
            "name": "set_timer",
            "description": (
                "设置一个定时提醒。到时间后会自动唤醒 AI 处理。"
                "可以设置一次性或周期性提醒（daily/weekly）。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "label": {
                        "type": "string",
                        "description": "定时器描述，说明到时要做什么",
                    },
                    "delay_minutes": {
                        "type": "integer",
                        "description": "从现在起多少分钟后触发（与 trigger_at 二选一）",
                    },
                    "trigger_at": {
                        "type": "string",
                        "description": "触发时间，格式 'YYYY-MM-DD HH:MM'（与 delay_minutes 二选一）",
                    },
                    "recurrence": {
                        "type": "string",
                        "enum": ["daily", "weekly", None],
                        "description": "重复频率，不填则为一次性",
                    },
                },
                "required": ["label"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_timers",
            "description": "查看所有待触发的定时器列表。",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
]


# ---------------------------------------------------------------------------
# 工具执行器
# ---------------------------------------------------------------------------


class CommonToolExecutor:
    def __init__(self, memory: "RecursiveMemory"):
        self.memory = memory

    async def execute(self, tool_name: str, args: dict[str, Any]) -> dict[str, Any]:
        if tool_name == "set_timer":
            label = args.get("label", "")
            recurrence = args.get("recurrence")

            if "delay_minutes" in args:
                trigger_at = datetime.now() + timedelta(minutes=int(args["delay_minutes"]))
            elif "trigger_at" in args:
                try:
                    trigger_at = datetime.strptime(args["trigger_at"], "%Y-%m-%d %H:%M")
                except ValueError:
                    return {"error": f"时间格式无效: {args['trigger_at']}，请使用 'YYYY-MM-DD HH:MM'"}
            else:
                return {"error": "需要提供 delay_minutes 或 trigger_at"}

            timer_id = await self.memory.timer_set(label, trigger_at, recurrence)
            return {
                "success": True,
                "timer_id": timer_id,
                "message": f"定时器已设置：{label}，触发时间：{trigger_at.strftime('%Y-%m-%d %H:%M')}",
            }

        elif tool_name == "list_timers":
            timers = await self.memory.timer_list()
            if not timers:
                return {"timers": [], "message": "当前没有待触发的定时器"}
            lines = []
            for t in timers:
                rec = f"（{t['recurrence']}）" if t.get("recurrence") else ""
                lines.append(f"[{t['id']}] {t['label']} — {t['trigger_at']}{rec}")
            return {"timers": timers, "text": "\n".join(lines)}

        return {"error": f"未知工具: {tool_name}"}
