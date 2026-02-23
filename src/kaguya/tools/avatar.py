"""
头像管理 — 辉夜姬的自我形象意识。

- 初始化时从 config/ 复制 avatar.png + avatar.changelog 到 workspace/.avatar/
- 通过 set_avatar 工具允许辉夜姬更换自己的形象
- engine 构建 context 时注入头像图片（vision multimodal）
"""

from __future__ import annotations

import base64
import shutil
from datetime import datetime
from pathlib import Path

from loguru import logger

from kaguya.config import CONFIG_DIR
from kaguya.tools.registry import Tool


AVATAR_DIR_NAME = ".avatar"
AVATAR_FILENAME = "avatar.png"
CHANGELOG_FILENAME = "avatar.changelog"


class AvatarManager:
    """
    头像管理器。

    管理辉夜姬的形象文件，放在 workspace/.avatar/ 下。
    """

    def __init__(self, workspace_kaguya_dir: Path):
        self._avatar_dir = workspace_kaguya_dir / AVATAR_DIR_NAME
        self._avatar_dir.mkdir(parents=True, exist_ok=True)
        self._avatar_path = self._avatar_dir / AVATAR_FILENAME
        self._changelog_path = self._avatar_dir / CHANGELOG_FILENAME

    def init_from_config(self) -> None:
        """首次运行时从 config/ 复制头像和 changelog"""
        config_avatar = CONFIG_DIR / AVATAR_FILENAME
        config_changelog = CONFIG_DIR / CHANGELOG_FILENAME

        if config_avatar.exists() and not self._avatar_path.exists():
            shutil.copy2(config_avatar, self._avatar_path)
            logger.info(f"已从 config/ 复制头像到 workspace: {self._avatar_path}")

        if config_changelog.exists() and not self._changelog_path.exists():
            shutil.copy2(config_changelog, self._changelog_path)
            logger.info(f"已从 config/ 复制 changelog 到 workspace")

    @property
    def avatar_path(self) -> Path | None:
        """返回当前头像路径，不存在则返回 None"""
        return self._avatar_path if self._avatar_path.exists() else None

    @property
    def changelog(self) -> str:
        """获取 changelog 内容"""
        if self._changelog_path.exists():
            return self._changelog_path.read_text(encoding="utf-8")
        return ""

    def get_avatar_base64(self) -> tuple[str, str] | None:
        """读取头像并返回 (base64, mime_type)，不存在返回 None"""
        if not self._avatar_path.exists():
            return None
        ext = self._avatar_path.suffix.lower().lstrip(".")
        mime = {
            "png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg",
            "gif": "image/gif", "webp": "image/webp",
        }.get(ext, "image/png")
        b64 = base64.b64encode(self._avatar_path.read_bytes()).decode("utf-8")
        return b64, mime

    def set_avatar(self, source_path: str, changelog_entry: str) -> str:
        """
        更换头像。

        Args:
            source_path: 新头像图片的路径
            changelog_entry: 本次更换的说明

        Returns:
            成功/失败消息
        """
        src = Path(source_path)
        if not src.exists():
            return f"图片文件不存在: {source_path}"

        # 复制新头像
        shutil.copy2(src, self._avatar_path)

        # 追加 changelog
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M")
        entry = f"[{timestamp}] {changelog_entry}\n"
        with open(self._changelog_path, "a", encoding="utf-8") as f:
            f.write(entry)

        logger.info(f"头像已更新: {source_path} → {self._avatar_path}")
        return f"形象已更新！新头像: {self._avatar_path}\n记录: {changelog_entry}"

    def build_system_prompt_parts(self) -> list[dict]:
        """
        构建头像注入的 system prompt content parts（多模态格式）。

        返回 OpenAI multimodal content 格式的列表：
        [{"type": "text", ...}, {"type": "image_url", ...}]
        """
        parts: list[dict] = []

        avatar_data = self.get_avatar_base64()
        if avatar_data:
            b64, mime = avatar_data
            changelog = self.changelog

            text = "【你的形象】\n下面这张图片是你当前的形象/头像。"
            if changelog:
                text += f"\n形象变更记录:\n{changelog}"
            text += f"\n头像文件路径: {self._avatar_path}"

            parts.append({"type": "text", "text": text})
            parts.append({
                "type": "image_url",
                "image_url": {"url": f"data:{mime};base64,{b64}"},
            })

        return parts


class SetAvatarTool(Tool):
    """辉夜姬可以用这个工具更换自己的形象"""

    def __init__(self, avatar_manager: AvatarManager):
        self._avatar = avatar_manager

    @property
    def name(self):
        return "set_avatar"

    @property
    def description(self):
        return (
            "更换你的形象/头像。提供一张图片文件的路径和一段更换说明。"
            "图片可以是你用工具画的、下载的、或 workspace 里已有的图片。"
        )

    @property
    def parameters(self):
        return {
            "type": "object",
            "properties": {
                "image_path": {
                    "type": "string",
                    "description": "新头像图片的文件路径（本地路径或 workspace 相对路径）",
                },
                "changelog": {
                    "type": "string",
                    "description": "简要说明为什么要换这个形象（例如：'换了一个更酷的动漫风格形象'）",
                },
            },
            "required": ["image_path", "changelog"],
        }

    async def execute(self, image_path: str, changelog: str, **_) -> str:
        return self._avatar.set_avatar(image_path, changelog)
