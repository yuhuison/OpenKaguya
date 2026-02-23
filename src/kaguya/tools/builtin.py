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
        # 危险命令检测
        _DANGEROUS_PATTERNS = [
            "rm -rf /", "rm -rf ~", "mkfs", "dd if=",
            ":(){ :|:& };:", "format c:", "del /f /s /q c:",
            "shutdown", "reboot", "> /dev/sda",
        ]
        cmd_lower = command.lower().strip()
        for pattern in _DANGEROUS_PATTERNS:
            if pattern in cmd_lower:
                return f"⚠️ 命令被阻止（包含危险操作 '{pattern}'）。如确需执行，请通知用户手动操作。"

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


# ========================= 图片查看工具 =========================


class ViewImageTool(Tool):
    """
    让辉夜姬查看存储在 workspace/.images/ 中的图片。

    当 AI 在历史中看到 [workspace_image:user_id:filename] 时，
    可以主动调用此工具来加载图片内容进行查看。
    """

    def __init__(self, workspace: WorkspaceManager):
        self._workspace = workspace
        self._current_user_id: str = ""

    @property
    def name(self): return "view_image"

    @property
    def description(self):
        return (
            "查看保存在 workspace 中的图片。如果你在对话历史中看到 [workspace_image:user_id:filename] 占位符，"
            "可以用此工具加载图片实际内容来查看。若不指定 user_id，默认使用当前对话用户。"
        )

    @property
    def parameters(self):
        return {
            "type": "object",
            "properties": {
                "filename": {
                    "type": "string",
                    "description": "图片文件名（如 abc123def456.jpg），从 [workspace_image:user_id:filename] 中提取",
                },
                "user_id": {
                    "type": "string",
                    "description": "图片所属用户 ID（可选，默认当前用户）",
                },
            },
            "required": ["filename"],
        }

    async def execute(self, filename: str, user_id: str = "", **_) -> str | list:
        uid = user_id or self._current_user_id
        result = self._workspace.read_image_as_base64(uid, filename)
        if not result:
            return f"图片 {filename} 不存在（user_id={uid}）"
        b64, mime = result
        # 返回 multimodal 格式，engine 会直接将其作为 tool result content
        # 注意：OpenAI tool message 支持 content 为 list（含 image_url 块）
        return [
            {"type": "text", "text": f"图片 {filename} 已加载："},
            {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}"}},
        ]


# ========================= 消息查询工具 =========================



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

# ========================= 笔记本工具 =========================


class ManageNotesTool(Tool):
    """辉夜姬的笔记本管理工具（支持为自己或用户创建/读取/追加/删除笔记）"""

    def __init__(self, db: Database):
        self._db = db
        self._current_user_id: str = ""  # 当前对话用户，用于 owner 参数默认值

    @property
    def name(self): return "manage_notes"

    @property
    def description(self):
        return (
            "管理笔记本。支持创建、查看列表、读取内容、追加内容、删除笔记。"
            "每条笔记属于一个 owner：'kaguya'（你自己的私人笔记）或某个用户ID（为该用户记录的信息）。"
            "write 用于创建新笔记，append 用于在已有笔记后追加内容，read 用于读取某条笔记的完整内容。"
        )

    @property
    def parameters(self):
        return {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["write", "list", "read", "append", "delete"],
                    "description": (
                        "操作类型：\n"
                        "- write: 创建新笔记\n"
                        "- list: 列出指定 owner 的所有笔记标题\n"
                        "- read: 读取某条笔记的完整内容（需要 note_id）\n"
                        "- append: 向已有笔记追加内容（需要 note_id 和 content）\n"
                        "- delete: 删除某条笔记（需要 note_id）"
                    ),
                },
                "owner": {
                    "type": "string",
                    "description": (
                        "笔记归属：'kaguya' 表示你自己的私人笔记，"
                        "或填写用户ID表示关于该用户的笔记（默认 'kaguya'）"
                    ),
                },
                "title": {
                    "type": "string",
                    "description": "笔记标题（write 时使用）",
                },
                "content": {
                    "type": "string",
                    "description": "笔记内容（write 时为初始内容，append 时为要追加的内容）",
                },
                "tags": {
                    "type": "string",
                    "description": "标签（逗号分隔，write 时可选）",
                },
                "note_id": {
                    "type": "integer",
                    "description": "笔记 ID（read/append/delete 时必填）",
                },
            },
            "required": ["action"],
        }

    async def execute(self, action: str, **kwargs) -> str:
        owner = kwargs.get("owner", "kaguya")

        if action == "write":
            title = kwargs.get("title", "")
            content = kwargs.get("content", "")
            tags = kwargs.get("tags", "")
            if not content:
                return "需要 content 参数。"
            note_id = await self._db.save_note(title, content, tags, owner_id=owner)
            return f"笔记已保存 (ID: {note_id}, owner: {owner}): {title or '(无标题)'}"

        elif action == "list":
            notes = await self._db.get_notes_by_owner(owner)
            if not notes:
                return f"没有属于 {owner} 的笔记。"
            lines = [f"📒 {owner} 的笔记列表："]
            for n in notes:
                tag_str = f" #{n['tags']}" if n.get("tags") else ""
                lines.append(f"  [ID:{n['id']}] {n['title'] or '(无标题)'}{tag_str}（{n['updated_at'][:16]}）")
            return "\n".join(lines)

        elif action == "read":
            note_id = kwargs.get("note_id")
            if not note_id:
                return "需要 note_id 参数。"
            note = await self._db.get_note_by_id(int(note_id))
            if not note:
                return f"笔记 ID:{note_id} 不存在。"
            tag_str = f"\n标签：{note['tags']}" if note.get("tags") else ""
            return (
                f"📒 笔记 [ID:{note['id']}]\n"
                f"标题：{note['title'] or '(无标题)'}\n"
                f"归属：{note['owner_id']}"
                f"{tag_str}\n"
                f"更新于：{note['updated_at']}\n\n"
                f"{note['content']}"
            )

        elif action == "append":
            note_id = kwargs.get("note_id")
            content = kwargs.get("content", "")
            if not note_id:
                return "需要 note_id 参数。"
            if not content:
                return "需要 content 参数（要追加的内容）。"
            ok = await self._db.append_note_content(int(note_id), content)
            if ok:
                return f"笔记 ID:{note_id} 已追加内容（{len(content)} 字符）"
            return f"笔记 ID:{note_id} 不存在或更新失败。"

        elif action == "delete":
            note_id = kwargs.get("note_id")
            if not note_id:
                return "需要 note_id 参数。"
            ok = await self._db.delete_note(int(note_id))
            if ok:
                return f"笔记 ID:{note_id} 已删除。"
            return f"笔记 ID:{note_id} 不存在。"

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
) -> list[Tool]:
    """创建所有内置工具"""
    return [
        # 文件工具
        ReadFileTool(workspace),
        WriteFileTool(workspace),
        DeleteFileTool(workspace),
        ListFilesTool(workspace),
        RunTerminalTool(workspace),
        # 图片查看
        ViewImageTool(workspace),
        # 消息查询
        QueryMessagesTool(db),
        # 笔记本（统一工具）
        ManageNotesTool(db),
        # 定时器
        SetTimerTool(db),
    ]
