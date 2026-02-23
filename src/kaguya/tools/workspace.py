"""
Workspace 沙箱管理器 — 文件操作隔离与路径穿越保护。
"""

from __future__ import annotations

import base64
import uuid
from pathlib import Path

from loguru import logger

from kaguya.config import DATA_DIR


class WorkspaceManager:
    """
    Workspace 沙箱管理器。

    每个用户有独立的 workspace 目录，辉夜姬自己也有一个。
    所有文件操作都必须在 workspace 内，防止路径穿越攻击。

    目录结构：
        data/workspaces/
        ├── kaguya/          # 辉夜姬自己的空间（笔记、截图等）
        ├── shared/          # 共享文件区
        ├── user_cli:xxx/    # 用户 workspace
        └── ...
    """

    def __init__(self, base_dir: Path | None = None):
        self.base_dir = base_dir or DATA_DIR / "workspaces"
        self.base_dir.mkdir(parents=True, exist_ok=True)

        # 辉夜姬自己的 workspace
        self.kaguya_dir = self.base_dir / "kaguya"
        self.kaguya_dir.mkdir(exist_ok=True)

        # 共享 workspace
        self.shared_dir = self.base_dir / "shared"
        self.shared_dir.mkdir(exist_ok=True)

    def get_user_workspace(self, user_id: str) -> Path:
        """获取用户的 workspace 路径（自动创建）"""
        # 替换不安全字符
        safe_id = user_id.replace(":", "_").replace("/", "_").replace("\\", "_")
        workspace = self.base_dir / f"user_{safe_id}"
        workspace.mkdir(parents=True, exist_ok=True)
        return workspace

    def resolve_path(self, user_id: str, relative_path: str) -> Path:
        """
        解析文件路径，确保不逃出 workspace。

        Args:
            user_id: 用户 ID
            relative_path: 相对路径（相对于用户 workspace）

        Returns:
            绝对路径

        Raises:
            PermissionError: 路径超出 workspace 范围
        """
        workspace = self.get_user_workspace(user_id)
        resolved = (workspace / relative_path).resolve()

        # 路径穿越保护
        if not str(resolved).startswith(str(workspace.resolve())):
            raise PermissionError(
                f"路径 '{relative_path}' 超出了你的 workspace 范围！"
                f"你只能访问 workspace 内的文件。"
            )
        return resolved

    def resolve_kaguya_path(self, relative_path: str) -> Path:
        """解析辉夜姬自己的 workspace 内的路径"""
        resolved = (self.kaguya_dir / relative_path).resolve()
        if not str(resolved).startswith(str(self.kaguya_dir.resolve())):
            raise PermissionError(f"路径 '{relative_path}' 超出了辉夜姬的 workspace 范围！")
        return resolved

    def list_workspace(self, user_id: str) -> list[str]:
        """列出用户 workspace 中的所有文件"""
        workspace = self.get_user_workspace(user_id)
        files = []
        for p in workspace.rglob("*"):
            if p.is_file():
                files.append(str(p.relative_to(workspace)))
        return files

    def save_image(
        self,
        user_id: str,
        data: str | bytes,
        mime_type: str = "image/jpeg",
    ) -> str:
        """
        将图片保存到 workspace/.images/ 目录。

        Args:
            user_id: 用户 ID（用于确定存储位置）
            data: base64 字符串或原始字节
            mime_type: MIME 类型

        Returns:
            filename（不含目录），如 "abc123.jpg"
        """
        ext = {
            "image/jpeg": "jpg",
            "image/png": "png",
            "image/gif": "gif",
            "image/webp": "webp",
        }.get(mime_type, "jpg")

        images_dir = self.get_user_workspace(user_id) / ".images"
        images_dir.mkdir(exist_ok=True)

        filename = f"{uuid.uuid4().hex[:12]}.{ext}"
        filepath = images_dir / filename

        if isinstance(data, str):
            # base64 字符串
            raw = base64.b64decode(data)
        else:
            raw = data

        filepath.write_bytes(raw)
        logger.debug(f"图片已保存: {filepath}")
        return filename

    def save_file(
        self,
        user_id: str,
        filename: str,
        data: str | bytes,
    ) -> str:
        """
        将文件保存到 workspace/.files/ 目录。

        Args:
            user_id: 用户 ID
            filename: 原始文件名（如 "report.pdf"）
            data: base64 字符串或原始字节

        Returns:
            保存后的文件名（含 UUID 前缀避免冲突），如 "a1b2c3d4_report.pdf"
        """
        files_dir = self.get_user_workspace(user_id) / ".files"
        files_dir.mkdir(exist_ok=True)

        # 用短 UUID 前缀避免文件名冲突
        safe_name = f"{uuid.uuid4().hex[:8]}_{filename}"
        filepath = files_dir / safe_name

        if isinstance(data, str):
            raw = base64.b64decode(data)
        else:
            raw = data

        filepath.write_bytes(raw)
        logger.debug(f"文件已保存: {filepath} ({len(raw)} bytes)")
        return safe_name

    def get_file_path(self, user_id: str, filename: str) -> Path | None:
        """根据文件名获取文件的完整路径，不存在则返回 None"""
        filepath = self.get_user_workspace(user_id) / ".files" / filename
        return filepath if filepath.exists() else None

    def get_image_path(self, user_id: str, filename: str) -> Path | None:
        """根据文件名获取图片的完整路径，不存在则返回 None"""
        filepath = self.get_user_workspace(user_id) / ".images" / filename
        return filepath if filepath.exists() else None

    def read_image_as_base64(self, user_id: str, filename: str) -> tuple[str, str] | None:
        """
        读取图片并返回 (base64_str, mime_type)，文件不存在则返回 None。
        """
        filepath = self.get_image_path(user_id, filename)
        if not filepath:
            return None
        ext = filepath.suffix.lower().lstrip(".")
        mime = {
            "jpg": "image/jpeg", "jpeg": "image/jpeg",
            "png": "image/png", "gif": "image/gif", "webp": "image/webp",
        }.get(ext, "image/jpeg")
        b64 = base64.b64encode(filepath.read_bytes()).decode("utf-8")
        return b64, mime
