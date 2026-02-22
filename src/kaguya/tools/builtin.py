"""
内置工具集 — 文件操作、记忆查询、笔记本、任务管理、定时器。
"""

from __future__ import annotations

import asyncio
import subprocess

from kaguya.tools.registry import Tool
from kaguya.tools.workspace import WorkspaceManager
from kaguya.memory.database import Database


# ========================= 文件工具 =========================


class ReadFileTool(Tool):
    """读取用户 workspace 中的文件"""

    def __init__(self, workspace: WorkspaceManager):
        self._workspace = workspace
        self._current_user_id: str = ""

    @property
    def name(self): return "read_file"

    @property
    def description(self): return "读取 workspace 中的文件内容。"

    @property
    def parameters(self):
        return {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "相对于 workspace 的文件路径"},
            },
            "required": ["path"],
        }

    async def execute(self, path: str, **_) -> str:
        try:
            file_path = self._workspace.resolve_path(self._current_user_id, path)
            if not file_path.exists():
                return f"文件不存在: {path}"
            content = file_path.read_text(encoding="utf-8")
            if len(content) > 5000:
                return f"[文件内容截断，共 {len(content)} 字符]\n{content[:5000]}..."
            return content
        except PermissionError as e:
            return str(e)


class WriteFileTool(Tool):
    """创建或覆盖 workspace 中的文件"""

    def __init__(self, workspace: WorkspaceManager):
        self._workspace = workspace
        self._current_user_id: str = ""

    @property
    def name(self): return "write_file"

    @property
    def description(self): return "在 workspace 中创建或覆盖文件。"

    @property
    def parameters(self):
        return {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "相对于 workspace 的文件路径"},
                "content": {"type": "string", "description": "文件内容"},
            },
            "required": ["path", "content"],
        }

    async def execute(self, path: str, content: str, **_) -> str:
        try:
            file_path = self._workspace.resolve_path(self._current_user_id, path)
            file_path.parent.mkdir(parents=True, exist_ok=True)
            file_path.write_text(content, encoding="utf-8")
            return f"文件已写入: {path} ({len(content)} 字符)"
        except PermissionError as e:
            return str(e)


class DeleteFileTool(Tool):
    """删除 workspace 中的文件"""

    def __init__(self, workspace: WorkspaceManager):
        self._workspace = workspace
        self._current_user_id: str = ""

    @property
    def name(self): return "delete_file"

    @property
    def description(self): return "删除 workspace 中的指定文件。"

    @property
    def parameters(self):
        return {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "要删除的文件路径"},
            },
            "required": ["path"],
        }

    async def execute(self, path: str, **_) -> str:
        try:
            file_path = self._workspace.resolve_path(self._current_user_id, path)
            if not file_path.exists():
                return f"文件不存在: {path}"
            file_path.unlink()
            return f"文件已删除: {path}"
        except PermissionError as e:
            return str(e)


class ListFilesTool(Tool):
    """列出 workspace 中的所有文件"""

    def __init__(self, workspace: WorkspaceManager):
        self._workspace = workspace
        self._current_user_id: str = ""

    @property
    def name(self): return "list_files"

    @property
    def description(self): return "列出 workspace 中的所有文件和目录。"

    @property
    def parameters(self):
        return {"type": "object", "properties": {}}

    async def execute(self, **_) -> str:
        files = self._workspace.list_workspace(self._current_user_id)
        if not files:
            return "workspace 中没有文件。"
        return "workspace 文件列表:\n" + "\n".join(f"  📄 {f}" for f in files)


class RunTerminalTool(Tool):
    """执行终端命令（沙箱目录下）"""

    def __init__(self, workspace: WorkspaceManager):
        self._workspace = workspace
        self._current_user_id: str = ""

    @property
    def name(self): return "run_terminal"

    @property
    def description(self):
        return "在 workspace 目录下执行终端命令。用于运行脚本、查看系统信息等。注意：命令在沙箱目录中执行。"

    @property
    def parameters(self):
        return {
            "type": "object",
            "properties": {
                "command": {"type": "string", "description": "要执行的命令"},
            },
            "required": ["command"],
        }

    async def execute(self, command: str, **_) -> str:
        workspace_dir = self._workspace.get_user_workspace(self._current_user_id)
        try:
            result = await asyncio.to_thread(
                subprocess.run,
                command,
                shell=True,
                capture_output=True,
                text=True,
                timeout=30,
                cwd=str(workspace_dir),
            )
            output = result.stdout
            if result.stderr:
                output += f"\n[stderr] {result.stderr}"
            if len(output) > 3000:
                output = output[:3000] + "\n... [输出截断]"
            return output or "(无输出)"
        except subprocess.TimeoutExpired:
            return "命令执行超时（30秒限制）"
        except Exception as e:
            return f"命令执行失败: {e}"


# ========================= 记忆工具 =========================


class SearchMemoryTool(Tool):
    """主动搜索历史记忆"""

    def __init__(self, retriever):
        self._retriever = retriever
        self._current_user_id: str = ""

    @property
    def name(self): return "search_memory"

    @property
    def description(self):
        return "搜索与某个用户的历史对话记忆。当你想回忆过去聊过的事情时使用。"

    @property
    def parameters(self):
        return {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "搜索关键词或问题"},
            },
            "required": ["query"],
        }

    async def execute(self, query: str, **_) -> str:
        results = await self._retriever.retrieve(
            user_id=self._current_user_id,
            query=query,
            top_k=5,
        )
        if not results:
            return "没有找到相关的历史记忆。"

        lines = ["找到以下相关记忆:"]
        for m in results:
            role = "用户" if m["role"] == "user" else "你"
            content = m.get("display_content") or m["content"]
            if len(content) > 200:
                content = content[:200] + "..."
            lines.append(f"  [{m['created_at']}] {role}: {content}")
        return "\n".join(lines)


class QueryMessagesTool(Tool):
    """查询最近的消息记录"""

    def __init__(self, db: Database):
        self._db = db
        self._current_user_id: str = ""

    @property
    def name(self): return "query_messages"

    @property
    def description(self):
        return "查询与某个用户最近的 N 条对话记录。"

    @property
    def parameters(self):
        return {
            "type": "object",
            "properties": {
                "limit": {"type": "integer", "description": "返回条数（默认 10）"},
            },
        }

    async def execute(self, limit: int = 10, **_) -> str:
        messages = await self._db.get_recent_messages(self._current_user_id, limit)
        if not messages:
            return "没有历史消息记录。"
        lines = [f"最近 {len(messages)} 条消息:"]
        for m in messages:
            role = "用户" if m["role"] == "user" else "你"
            content = m.get("display_content") or m["content"]
            if len(content) > 150:
                content = content[:150] + "..."
            lines.append(f"  [{m['created_at']}] {role}: {content}")
        return "\n".join(lines)


class QueryLogsTool(Tool):
    """查询日志摘要"""

    def __init__(self, db: Database):
        self._db = db
        self._current_user_id: str = ""

    @property
    def name(self): return "query_logs"

    @property
    def description(self):
        return "查询对话日志摘要（自动生成的对话总结）。"

    @property
    def parameters(self):
        return {
            "type": "object",
            "properties": {
                "limit": {"type": "integer", "description": "返回条数（默认 5）"},
            },
        }

    async def execute(self, limit: int = 5, **_) -> str:
        logs = await self._db.get_daily_logs(self._current_user_id, limit)
        if not logs:
            return "没有日志摘要。"
        lines = ["📋 对话日志摘要:"]
        for log in logs:
            lines.append(f"  [{log['created_at']}] {log['summary']}")
        return "\n".join(lines)


# ========================= 笔记本工具 =========================


class WriteNoteTool(Tool):
    """辉夜姬的笔记本：写下重要的事情"""

    def __init__(self, db: Database):
        self._db = db

    @property
    def name(self): return "write_note"

    @property
    def description(self):
        return (
            "在你的笔记本上写下一条笔记。"
            "用于记录重要信息、有趣的发现、用户的喜好等你不想忘记的事情。"
        )

    @property
    def parameters(self):
        return {
            "type": "object",
            "properties": {
                "title": {"type": "string", "description": "笔记标题"},
                "content": {"type": "string", "description": "笔记内容"},
                "tags": {"type": "string", "description": "标签（逗号分隔），如：美食,推荐"},
            },
            "required": ["content"],
        }

    async def execute(self, content: str, title: str = "", tags: str = "", **_) -> str:
        await self._db.save_note(title, content, tags)
        return f"笔记已保存: {title or '(无标题)'}"


class ReadNotesTool(Tool):
    """查看辉夜姬的笔记本"""

    def __init__(self, db: Database):
        self._db = db

    @property
    def name(self): return "read_notes"

    @property
    def description(self):
        return "查看你的笔记本中的笔记。可以搜索特定标签或查看最近的笔记。"

    @property
    def parameters(self):
        return {
            "type": "object",
            "properties": {
                "tag": {"type": "string", "description": "按标签过滤（可选）"},
                "limit": {"type": "integer", "description": "最多返回几条（默认 5）"},
            },
        }

    async def execute(self, tag: str = "", limit: int = 5, **_) -> str:
        rows = await self._db.get_notes(tag or None, limit)
        if not rows:
            return "笔记本是空的。"
        lines = ["📒 你的笔记:"]
        for r in rows:
            header = f"  [{r['created_at']}] {r['title'] or '(无标题)'}"
            if r['tags']:
                header += f" #{r['tags']}"
            lines.append(header)
            lines.append(f"    {r['content'][:200]}")
        return "\n".join(lines)


# ========================= 任务管理工具 =========================


class ManageTasksTool(Tool):
    """管理待办任务"""

    def __init__(self, db: Database):
        self._db = db

    @property
    def name(self): return "manage_tasks"

    @property
    def description(self):
        return "管理你的待办任务列表。可以添加、查看、更新状态或删除任务。"

    @property
    def parameters(self):
        return {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["add", "list", "update", "delete"],
                    "description": "操作类型",
                },
                "title": {"type": "string", "description": "任务标题（add 时必填）"},
                "description": {"type": "string", "description": "任务描述"},
                "task_id": {"type": "integer", "description": "任务 ID（update/delete 时必填）"},
                "status": {"type": "string", "enum": ["pending", "in_progress", "done", "cancelled"], "description": "新状态（update 时必填）"},
                "priority": {"type": "integer", "description": "优先级（0-10，默认 0）"},
            },
            "required": ["action"],
        }

    async def execute(self, action: str, **kwargs) -> str:
        if action == "add":
            title = kwargs.get("title", "未命名任务")
            desc = kwargs.get("description", "")
            priority = kwargs.get("priority", 0)
            task_id = await self._db.save_task(title, desc, priority)
            return f"任务已创建 (ID: {task_id}): {title}"

        elif action == "list":
            status = kwargs.get("status")
            tasks = await self._db.get_tasks(status)
            if not tasks:
                return "没有待办任务。"
            lines = ["📝 任务列表:"]
            for t in tasks:
                emoji = {"pending": "⬜", "in_progress": "🔄", "done": "✅", "cancelled": "❌"}.get(t["status"], "⬜")
                lines.append(f"  {emoji} [{t['id']}] {t['title']} (优先级: {t['priority']})")
                if t["description"]:
                    lines.append(f"      {t['description'][:100]}")
            return "\n".join(lines)

        elif action == "update":
            task_id = kwargs.get("task_id")
            status = kwargs.get("status")
            if not task_id or not status:
                return "需要 task_id 和 status 参数。"
            await self._db.update_task_status(task_id, status)
            return f"任务 {task_id} 状态已更新为 {status}"

        elif action == "delete":
            task_id = kwargs.get("task_id")
            if not task_id:
                return "需要 task_id 参数。"
            await self._db.delete_task(task_id)
            return f"任务 {task_id} 已删除"

        return f"未知操作: {action}"


# ========================= 技能管理工具 =========================


class ManageSkillsTool(Tool):
    """管理技能列表"""

    def __init__(self, db: Database):
        self._db = db

    @property
    def name(self): return "manage_skills"

    @property
    def description(self):
        return "管理你的技能列表。可以添加新技能、查看已有技能或删除技能。"

    @property
    def parameters(self):
        return {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["add", "list", "delete"],
                    "description": "操作类型",
                },
                "name": {"type": "string", "description": "技能名称（add/delete 时必填）"},
                "description": {"type": "string", "description": "技能描述（add 时必填）"},
                "trigger_keywords": {"type": "string", "description": "触发关键词（逗号分隔）"},
            },
            "required": ["action"],
        }

    async def execute(self, action: str, **kwargs) -> str:
        if action == "add":
            name = kwargs.get("name", "")
            desc = kwargs.get("description", "")
            keywords = kwargs.get("trigger_keywords", "")
            if not name or not desc:
                return "需要 name 和 description 参数。"
            await self._db.save_skill(name, desc, keywords)
            return f"技能已添加: {name}"

        elif action == "list":
            skills = await self._db.get_skills()
            if not skills:
                return "还没有任何技能。"
            lines = ["🎯 技能列表:"]
            for s in skills:
                lines.append(f"  • {s['name']}: {s['description'][:80]}")
                if s["trigger_keywords"]:
                    lines.append(f"    关键词: {s['trigger_keywords']}")
            return "\n".join(lines)

        elif action == "delete":
            name = kwargs.get("name", "")
            if not name:
                return "需要 name 参数。"
            await self._db.delete_skill(name)
            return f"技能已删除: {name}"

        return f"未知操作: {action}"


# ========================= 定时器工具 =========================


class SetTimerTool(Tool):
    """设置定时器/闹钟"""

    def __init__(self, db: Database):
        self._db = db

    @property
    def name(self): return "set_timer"

    @property
    def description(self):
        return "设置定时器或闹钟。可以创建一次性提醒或查看已有定时器。"

    @property
    def parameters(self):
        return {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["add", "list", "delete"],
                    "description": "操作类型",
                },
                "name": {"type": "string", "description": "定时器名称（add 时必填）"},
                "action_desc": {"type": "string", "description": "到期时要做的事情（add 时必填）"},
                "trigger_at": {"type": "string", "description": "触发时间，格式 YYYY-MM-DD HH:MM"},
                "timer_id": {"type": "integer", "description": "定时器 ID（delete 时必填）"},
            },
            "required": ["action"],
        }

    async def execute(self, action: str, **kwargs) -> str:
        if action == "add":
            name = kwargs.get("name", "未命名提醒")
            action_desc = kwargs.get("action_desc", "")
            trigger_at = kwargs.get("trigger_at")
            if not action_desc:
                return "需要 action_desc 参数（到期要做什么）。"
            timer_id = await self._db.save_timer(name, action_desc, trigger_at=trigger_at)
            return f"定时器已设置 (ID: {timer_id}): {name} @ {trigger_at or '无具体时间'}"

        elif action == "list":
            timers = await self._db.get_active_timers()
            if not timers:
                return "没有活跃的定时器。"
            lines = ["⏰ 定时器列表:"]
            for t in timers:
                lines.append(f"  [{t['id']}] {t['name']}: {t['action']}")
                if t["trigger_at"]:
                    lines.append(f"      触发时间: {t['trigger_at']}")
            return "\n".join(lines)

        elif action == "delete":
            timer_id = kwargs.get("timer_id")
            if not timer_id:
                return "需要 timer_id 参数。"
            await self._db.delete_timer(timer_id)
            return f"定时器 {timer_id} 已删除"

        return f"未知操作: {action}"


# ========================= 工厂函数 =========================


def create_builtin_tools(
    workspace: WorkspaceManager,
    db: Database,
    retriever,
) -> list[Tool]:
    """创建所有内置工具"""
    tools = [
        # 文件工具
        ReadFileTool(workspace),
        WriteFileTool(workspace),
        DeleteFileTool(workspace),
        ListFilesTool(workspace),
        RunTerminalTool(workspace),
        # 记忆工具
        SearchMemoryTool(retriever),
        QueryMessagesTool(db),
        QueryLogsTool(db),
        # 笔记本
        WriteNoteTool(db),
        ReadNotesTool(db),
        # 任务 & 技能 & 定时器
        ManageTasksTool(db),
        ManageSkillsTool(db),
        SetTimerTool(db),
    ]
    return tools
