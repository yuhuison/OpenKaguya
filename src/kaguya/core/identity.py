"""
跨平台用户身份管理 — 统一 ID + 元信息。

将各平台的原始用户 ID（如 wxid_abc、qq:12345）映射到
辉夜姬认识的统一用户身份，附带昵称、备注、角色等元信息。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from loguru import logger


@dataclass
class UserIdentity:
    """一个辉夜姬认识的人"""

    id: str                           # 统一 ID（全局唯一）
    nickname: str = ""                # 辉夜姬对她的称呼
    note: str = ""                    # 人物备注（注入 system prompt）
    role: str = "friend"              # admin / friend / acquaintance
    accounts: list[str] = field(default_factory=list)  # 各平台 ID 列表


class UserIdentityManager:
    """
    跨平台用户身份管理器。

    配置驱动：从 config 中的 [[identity.users]] 构建映射表。

    示例配置:
        [[identity.users]]
        id       = "alice"
        nickname = "小爱"
        note     = "喜欢猫和咖啡"
        role     = "admin"
        accounts = ["wechat:wxid_abc", "qq:12345", "cli:local_user"]
    """

    def __init__(self, users: list[UserIdentity] | None = None):
        self._users: dict[str, UserIdentity] = {}       # unified_id → identity
        self._account_map: dict[str, str] = {}           # platform_id → unified_id

        for user in (users or []):
            self.register(user)

        logger.info(f"用户身份管理器初始化: {len(self._users)} 个用户, {len(self._account_map)} 条映射")

    def register(self, identity: UserIdentity) -> None:
        """注册一个用户身份"""
        self._users[identity.id] = identity
        for account in identity.accounts:
            self._account_map[account] = identity.id

    def resolve(self, platform: str, raw_id: str) -> str:
        """
        将平台原始 ID 映射到统一 ID。

        Args:
            platform: 平台名称（如 "wechat", "qq", "cli"）
            raw_id: 平台原始 ID（如 "wxid_abc"）

        Returns:
            统一 ID（如 "alice"），未配置时退化为 "wechat:wxid_abc"
        """
        platform_id = f"{platform}:{raw_id}"
        return self._account_map.get(platform_id, platform_id)

    def get_identity(self, unified_id: str) -> Optional[UserIdentity]:
        """根据统一 ID 获取完整身份信息"""
        return self._users.get(unified_id)

    def get_nickname(self, platform: str, raw_id: str, fallback: str = "") -> str:
        """获取用户昵称（优先使用配置中的 nickname，兜底用 fallback）"""
        unified_id = self.resolve(platform, raw_id)
        identity = self.get_identity(unified_id)
        if identity and identity.nickname:
            return identity.nickname
        return fallback or raw_id

    def get_note(self, unified_id: str) -> str:
        """获取用户备注（空则返回空字符串）"""
        identity = self.get_identity(unified_id)
        return identity.note if identity else ""

    def get_role(self, unified_id: str) -> str:
        """获取用户角色"""
        identity = self.get_identity(unified_id)
        return identity.role if identity else "unknown"

    def get_platform_ids(self, unified_id: str) -> list[str]:
        """获取某个统一 ID 对应的所有平台 ID"""
        identity = self.get_identity(unified_id)
        return identity.accounts if identity else [unified_id]

    def build_user_context(self, unified_id: str) -> str:
        """
        构建注入 system prompt 的用户上下文字符串。

        Returns:
            如 "你正在和小爱聊天。关于她: 喜欢猫和咖啡" 或 ""
        """
        identity = self.get_identity(unified_id)
        if not identity:
            return ""

        parts = []
        name = identity.nickname or identity.id
        parts.append(f"你正在和 {name} 聊天。")
        if identity.note:
            parts.append(f"关于{name}: {identity.note}")
        if identity.role == "admin":
            parts.append(f"{name} 是你的管理员。")

        return " ".join(parts)
