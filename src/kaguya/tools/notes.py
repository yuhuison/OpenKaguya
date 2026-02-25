"""tools/notes.py — 笔记工具（AI 主动记忆）。

提供给 LLM 的工具：notes_write, notes_read, notes_delete
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from kaguya.core.memory import RecursiveMemory


# ---------------------------------------------------------------------------
# 工具 Schema
# ---------------------------------------------------------------------------

NOTES_TOOLS: list[dict] = [
    {
        "type": "function",
        "function": {
            "name": "notes_write",
            "description": (
                "在笔记本中记录重要信息。适合保存用户的生日、偏好、重要约定、"
                "需要长期记住的事实等。笔记会永久保存直到主动删除。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "title": {
                        "type": "string",
                        "description": "笔记标题（作为唯一标识，已存在则更新内容）",
                    },
                    "content": {
                        "type": "string",
                        "description": "笔记内容",
                    },
                },
                "required": ["title", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "notes_read",
            "description": "查看笔记本中的笔记。不提供 query 则返回所有笔记。",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "可选，按关键词搜索笔记标题或内容",
                    }
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "notes_delete",
            "description": "删除不再需要的笔记。",
            "parameters": {
                "type": "object",
                "properties": {
                    "title": {
                        "type": "string",
                        "description": "要删除的笔记标题",
                    }
                },
                "required": ["title"],
            },
        },
    },
]


# ---------------------------------------------------------------------------
# 工具执行器
# ---------------------------------------------------------------------------


class NotesToolExecutor:
    def __init__(self, memory: "RecursiveMemory"):
        self.memory = memory

    async def execute(self, tool_name: str, args: dict[str, Any]) -> dict[str, Any]:
        if tool_name == "notes_write":
            await self.memory.note_write(args["title"], args["content"])
            return {"success": True, "message": f"笔记「{args['title']}」已保存"}

        elif tool_name == "notes_read":
            query = args.get("query")
            notes = await self.memory.note_read(query)
            if not notes:
                return {"notes": [], "message": "笔记本是空的" if not query else f"没有找到关于「{query}」的笔记"}
            lines = [f"**{title}**\n{content}" for title, content in notes]
            return {"notes": [{"title": t, "content": c} for t, c in notes], "text": "\n\n---\n\n".join(lines)}

        elif tool_name == "notes_delete":
            deleted = await self.memory.note_delete(args["title"])
            if deleted:
                return {"success": True, "message": f"笔记「{args['title']}」已删除"}
            else:
                return {"success": False, "message": f"找不到笔记「{args['title']}」"}

        return {"error": f"未知工具: {tool_name}"}
