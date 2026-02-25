"""workspace.py — Workspace 文件操作 + 终端命令工具。

提供安全隔离的文件读写和命令执行能力：
  - 所有文件操作限制在 data/workspaces/kaguya/ 目录内
  - 路径穿越防护（.. 等不能逃出 workspace）
  - 终端命令有超时、输出截断、危险命令拦截
"""

from __future__ import annotations

import asyncio
import re
import shutil
from pathlib import Path
from typing import Any

from loguru import logger

# ---------------------------------------------------------------------------
# 危险命令检测
# ---------------------------------------------------------------------------

_DANGEROUS_PATTERNS: list[re.Pattern] = [
    re.compile(r"rm\s+(-\w*\s+)*-\w*r\w*\s+/", re.IGNORECASE),  # rm -rf /
    re.compile(r":\(\)\s*\{.*\|.*&\s*\}\s*;", re.IGNORECASE),    # fork bomb
    re.compile(r"\b(shutdown|reboot|halt|poweroff)\b", re.IGNORECASE),
    re.compile(r"\bformat\s+[a-zA-Z]:", re.IGNORECASE),           # format C:
    re.compile(r"\bmkfs\b", re.IGNORECASE),
    re.compile(r">\s*/dev/sd[a-z]", re.IGNORECASE),               # write to disk
    re.compile(r"\bdd\s+.*of=/dev/", re.IGNORECASE),
]

MAX_OUTPUT_CHARS = 8000


def _is_dangerous(command: str) -> str | None:
    """检查命令是否危险，返回原因或 None。"""
    for pattern in _DANGEROUS_PATTERNS:
        if pattern.search(command):
            return f"检测到危险命令模式: {pattern.pattern}"
    return None


# ---------------------------------------------------------------------------
# WorkspaceManager
# ---------------------------------------------------------------------------


class WorkspaceManager:
    """管理 data/workspaces/ 目录。"""

    def __init__(self, base_dir: Path):
        self.base_dir = base_dir
        self.kaguya_dir = base_dir / "kaguya"
        self.kaguya_dir.mkdir(parents=True, exist_ok=True)

    def resolve_path(self, relative_path: str) -> Path:
        """解析相对路径，防止路径穿越。"""
        resolved = (self.kaguya_dir / relative_path).resolve()
        kaguya_resolved = self.kaguya_dir.resolve()
        if not str(resolved).startswith(str(kaguya_resolved)):
            raise PermissionError(f"路径穿越被拒绝: {relative_path}")
        return resolved

    def list_files(self) -> list[str]:
        """递归列出 workspace 中所有文件（相对路径）。"""
        files = []
        kaguya_resolved = self.kaguya_dir.resolve()
        for p in self.kaguya_dir.rglob("*"):
            if p.is_file():
                files.append(str(p.resolve().relative_to(kaguya_resolved)))
        return sorted(files)


# ---------------------------------------------------------------------------
# 工具 Schema 定义
# ---------------------------------------------------------------------------

WORKSPACE_TOOLS: list[dict] = [
    {
        "type": "function",
        "function": {
            "name": "workspace_read",
            "description": "读取你的 workspace 中的文件内容。",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "文件相对路径（相对于 workspace 根目录）",
                    },
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "workspace_write",
            "description": "写入文件到你的 workspace（自动创建目录）。",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "文件相对路径",
                    },
                    "content": {
                        "type": "string",
                        "description": "文件内容",
                    },
                },
                "required": ["path", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "workspace_delete",
            "description": "删除 workspace 中的文件。",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "文件相对路径",
                    },
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "workspace_list",
            "description": "列出 workspace 中的所有文件。",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "workspace_terminal",
            "description": (
                "在 workspace 目录下执行命令行命令。"
                "有安全限制：30 秒超时、输出截断、危险命令拦截。"
                "适合运行脚本、查看系统信息等操作。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {
                        "type": "string",
                        "description": "要执行的命令",
                    },
                    "timeout": {
                        "type": "integer",
                        "description": "超时时间（秒），默认 30，最大 120",
                        "default": 30,
                    },
                },
                "required": ["command"],
            },
        },
    },
]


# ---------------------------------------------------------------------------
# 工具执行器
# ---------------------------------------------------------------------------


class WorkspaceToolExecutor:
    """Workspace 文件操作 + 终端执行器。"""

    def __init__(self, workspace: WorkspaceManager):
        self.workspace = workspace

    async def execute(self, tool_name: str, args: dict[str, Any]) -> dict[str, Any]:
        handler = getattr(self, f"_tool_{tool_name}", None)
        if handler is None:
            return {"error": f"未知工具: {tool_name}"}
        try:
            return await handler(**args)
        except PermissionError as e:
            return {"error": f"权限被拒绝: {e}"}
        except Exception as e:
            logger.error(f"Workspace 工具 [{tool_name}] 执行失败: {e}")
            return {"error": str(e)}

    async def _tool_workspace_read(self, path: str) -> dict[str, Any]:
        resolved = self.workspace.resolve_path(path)
        if not resolved.exists():
            return {"error": f"文件不存在: {path}"}
        if not resolved.is_file():
            return {"error": f"不是文件: {path}"}
        content = await asyncio.to_thread(resolved.read_text, encoding="utf-8")
        return {"success": True, "path": path, "content": content, "size": len(content)}

    async def _tool_workspace_write(self, path: str, content: str) -> dict[str, Any]:
        resolved = self.workspace.resolve_path(path)
        resolved.parent.mkdir(parents=True, exist_ok=True)
        await asyncio.to_thread(resolved.write_text, content, encoding="utf-8")
        logger.info(f"Workspace 写入: {path} ({len(content)} 字符)")
        return {"success": True, "path": path, "size": len(content)}

    async def _tool_workspace_delete(self, path: str) -> dict[str, Any]:
        resolved = self.workspace.resolve_path(path)
        if not resolved.exists():
            return {"error": f"文件不存在: {path}"}
        if resolved.is_dir():
            await asyncio.to_thread(shutil.rmtree, resolved)
        else:
            resolved.unlink()
        logger.info(f"Workspace 删除: {path}")
        return {"success": True, "deleted": path}

    async def _tool_workspace_list(self) -> dict[str, Any]:
        files = self.workspace.list_files()
        return {"success": True, "files": files, "count": len(files)}

    async def _tool_workspace_terminal(
        self, command: str, timeout: int = 30
    ) -> dict[str, Any]:
        # 安全检查
        danger = _is_dangerous(command)
        if danger:
            return {"error": f"命令被拦截: {danger}"}

        timeout = min(max(timeout, 1), 120)

        try:
            proc = await asyncio.create_subprocess_shell(
                command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=str(self.workspace.kaguya_dir),
            )
            stdout_bytes, stderr_bytes = await asyncio.wait_for(
                proc.communicate(), timeout=timeout
            )
        except asyncio.TimeoutError:
            proc.kill()
            return {"error": f"命令超时 ({timeout}s): {command}"}

        stdout = stdout_bytes.decode("utf-8", errors="replace")
        stderr = stderr_bytes.decode("utf-8", errors="replace")

        # 截断输出
        if len(stdout) > MAX_OUTPUT_CHARS:
            stdout = stdout[:MAX_OUTPUT_CHARS] + f"\n...(输出被截断，共 {len(stdout_bytes)} 字符)"
        if len(stderr) > MAX_OUTPUT_CHARS:
            stderr = stderr[:MAX_OUTPUT_CHARS] + f"\n...(输出被截断)"

        logger.info(f"Workspace 终端: {command} (exit={proc.returncode})")
        return {
            "success": proc.returncode == 0,
            "exit_code": proc.returncode,
            "stdout": stdout,
            "stderr": stderr,
        }
