"""avatar.py — 辉夜姬形象/头像管理。

管理辉夜姬的头像图片：
  - 首次运行从 config/avatar.png 复制到 workspace/.avatar/
  - 提供 set_avatar 工具让 AI 更换形象
  - build_system_prompt_parts() 生成多模态 system prompt 片段
"""

from __future__ import annotations

import base64
import mimetypes
import shutil
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from loguru import logger


class AvatarManager:
    """辉夜姬形象管理器。"""

    def __init__(self, workspace_dir: Path, config_dir: Path):
        self.avatar_dir = workspace_dir / ".avatar"
        self.avatar_dir.mkdir(parents=True, exist_ok=True)
        self.avatar_path = self.avatar_dir / "avatar.png"
        self.changelog_path = self.avatar_dir / "avatar.changelog"
        self.config_dir = config_dir

        # 首次初始化：从 config/avatar.png 复制
        if not self.avatar_path.exists():
            default_avatar = config_dir / "avatar.png"
            if default_avatar.exists():
                shutil.copy2(default_avatar, self.avatar_path)
                logger.info(f"从 {default_avatar} 初始化头像")

    def get_avatar_base64(self) -> Optional[tuple[str, str]]:
        """返回 (base64_data, mime_type)，文件不存在返回 None。"""
        if not self.avatar_path.exists():
            return None
        mime = mimetypes.guess_type(str(self.avatar_path))[0] or "image/png"
        data = self.avatar_path.read_bytes()
        return base64.b64encode(data).decode(), mime

    def get_changelog(self) -> str:
        """读取形象变更记录。"""
        if not self.changelog_path.exists():
            return ""
        return self.changelog_path.read_text(encoding="utf-8")

    def set_avatar(self, source_path: Path, changelog_entry: str) -> str:
        """更换头像并记录变更。"""
        if not source_path.exists():
            raise FileNotFoundError(f"源图片不存在: {source_path}")
        shutil.copy2(source_path, self.avatar_path)

        # 追加变更记录
        now = datetime.now().strftime("%Y-%m-%d %H:%M")
        entry = f"[{now}] {changelog_entry}\n"
        with open(self.changelog_path, "a", encoding="utf-8") as f:
            f.write(entry)

        logger.info(f"头像已更换: {source_path} → {self.avatar_path}")
        return f"形象已更换！{changelog_entry}"

    def build_system_prompt_parts(self) -> list[dict]:
        """构建多模态 system prompt 片段（text + image）。"""
        result = self.get_avatar_base64()
        if result is None:
            return []

        b64, mime = result
        parts: list[dict] = []

        # 形象说明文字
        text = "【你的形象】下面是你当前的形象/头像。"
        changelog = self.get_changelog()
        if changelog:
            # 只取最近 5 条记录
            lines = changelog.strip().splitlines()[-5:]
            text += "\n形象变更记录:\n" + "\n".join(lines)

        parts.append({"type": "text", "text": text})
        parts.append({
            "type": "image_url",
            "image_url": {"url": f"data:{mime};base64,{b64}"},
        })

        return parts


# ---------------------------------------------------------------------------
# 工具 Schema 定义
# ---------------------------------------------------------------------------

AVATAR_TOOLS: list[dict] = [
    {
        "type": "function",
        "function": {
            "name": "set_avatar",
            "description": (
                "更换你的形象/头像。新图片必须先保存在 workspace 中，"
                "然后用 workspace 相对路径指定。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "image_path": {
                        "type": "string",
                        "description": "新头像的 workspace 相对路径（如 images/new_avatar.png）",
                    },
                    "changelog": {
                        "type": "string",
                        "description": "形象变更说明（如「换成了猫耳造型」）",
                    },
                },
                "required": ["image_path", "changelog"],
            },
        },
    },
]


# ---------------------------------------------------------------------------
# 工具执行器
# ---------------------------------------------------------------------------


class AvatarToolExecutor:
    """Avatar 工具执行器。"""

    def __init__(self, avatar_manager: AvatarManager, workspace_manager):
        self.avatar = avatar_manager
        self.workspace = workspace_manager

    async def execute(self, tool_name: str, args: dict[str, Any]) -> dict[str, Any]:
        if tool_name != "set_avatar":
            return {"error": f"未知工具: {tool_name}"}
        try:
            image_path = args["image_path"]
            changelog = args["changelog"]
            resolved = self.workspace.resolve_path(image_path)
            msg = self.avatar.set_avatar(resolved, changelog)
            return {"success": True, "message": msg}
        except (FileNotFoundError, PermissionError) as e:
            return {"error": str(e)}
        except Exception as e:
            logger.error(f"set_avatar 失败: {e}")
            return {"error": str(e)}
