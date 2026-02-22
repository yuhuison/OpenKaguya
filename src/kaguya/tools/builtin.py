"""
内置工具集 — 文件操作、记忆查询、笔记本。
"""

from __future__ import annotations

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
        import asyncio

        def _save():
            self._db._conn.execute(
                "INSERT INTO notebook (title, content, tags) VALUES (?, ?, ?)",
                (title, content, tags),
            )
            self._db._conn.commit()

        await asyncio.to_thread(_save)
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
        import asyncio

        def _query():
            if tag:
                return self._db._conn.execute(
                    "SELECT title, content, tags, created_at FROM notebook "
                    "WHERE tags LIKE ? ORDER BY id DESC LIMIT ?",
                    (f"%{tag}%", limit),
                ).fetchall()
            return self._db._conn.execute(
                "SELECT title, content, tags, created_at FROM notebook "
                "ORDER BY id DESC LIMIT ?",
                (limit,),
            ).fetchall()

        rows = await asyncio.to_thread(_query)
        if not rows:
            return "笔记本是空的。"

        lines = ["📒 你的笔记:"]
        for title, content, tags, created_at in rows:
            header = f"  [{created_at}] {title or '(无标题)'}"
            if tags:
                header += f" #{tags}"
            lines.append(header)
            lines.append(f"    {content[:200]}")
        return "\n".join(lines)


# ========================= 工厂函数 =========================


def create_builtin_tools(
    workspace: WorkspaceManager,
    db: Database,
    retriever,
) -> list[Tool]:
    """创建所有内置工具"""
    return [
        ReadFileTool(workspace),
        WriteFileTool(workspace),
        ListFilesTool(workspace),
        SearchMemoryTool(retriever),
        WriteNoteTool(db),
        ReadNotesTool(db),
    ]
