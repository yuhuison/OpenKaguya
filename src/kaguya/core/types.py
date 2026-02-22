"""
核心类型定义：统一消息格式、用户信息等。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Optional


class Platform(str, Enum):
    """支持的平台"""

    TELEGRAM = "telegram"
    QQ = "qq"
    WECHAT = "wechat"
    CLI = "cli"
    SYSTEM = "system"  # 系统内部（主动意识等）


@dataclass
class UserInfo:
    """用户信息 — 全平台统一 ID"""

    user_id: str  # 全平台统一的用户 ID（格式: {platform}:{platform_user_id}）
    nickname: str  # 用户昵称
    platform: Platform  # 来源平台


@dataclass
class Attachment:
    """消息附件（图片、文件等）"""

    type: str  # "image" / "file" / "audio" / "video"
    url: Optional[str] = None  # 远程 URL
    local_path: Optional[str] = None  # 本地路径
    filename: Optional[str] = None  # 文件名
    mime_type: Optional[str] = None
    data: Optional[str] = None  # base64 编码数据（如微信图片的 img_buf）


@dataclass
class UnifiedMessage:
    """统一消息格式 — 屏蔽平台差异"""

    message_id: str  # 全局唯一 ID
    platform: Platform  # 来源平台
    sender: UserInfo  # 发送者信息
    content: str  # 消息文本
    timestamp: datetime = field(default_factory=datetime.now)
    group_id: Optional[str] = None  # 群组 ID（私聊为 None）
    attachments: list[Attachment] = field(default_factory=list)

    @property
    def is_group_message(self) -> bool:
        return self.group_id is not None


class ConsciousnessState(str, Enum):
    """辉夜姬的意识状态"""

    SLEEPING = "sleeping"  # 睡眠中
    SELF_PLAYING = "self_playing"  # 自娱自乐
    CHATTING = "chatting"  # 对话中
    WORKING = "working"  # 执行任务中


@dataclass
class KaguyaState:
    """辉夜姬的全局状态（跨用户共享）"""

    consciousness: ConsciousnessState = ConsciousnessState.SLEEPING
    current_activity: str = ""  # 当前在做什么
    mood: str = "neutral"  # 心情
    last_wake_time: Optional[datetime] = None

    # Token 用量统计
    tokens_today: dict[str, int] = field(
        default_factory=lambda: {"primary": 0, "secondary": 0, "embedding": 0}
    )


@dataclass
class ToolCall:
    """工具调用记录"""

    id: str
    name: str
    arguments: dict[str, Any]
    result: Optional[str] = None


@dataclass
class ChatResponse:
    """ChatEngine 的响应"""

    thinking: str  # 思考过程（不发给用户）
    messages: list[str]  # 发给用户的消息列表
    tool_calls: list[ToolCall] = field(default_factory=list)
    attachments: list[Attachment] = field(default_factory=list)
