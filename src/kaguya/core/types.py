"""核心类型定义（V2 简化版）。"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Optional


class Platform(str, Enum):
    CLI = "cli"
    SYSTEM = "system"  # 系统内部（主动意识等）


@dataclass
class UnifiedMessage:
    """统一消息格式。"""

    content: str
    platform: Platform = Platform.CLI
    sender_name: str = "用户"
    timestamp: datetime = field(default_factory=datetime.now)
    images: list[str] = field(default_factory=list)  # base64 图像列表


@dataclass
class ToolCall:
    """工具调用记录。"""

    id: str
    name: str
    arguments: dict[str, Any]
    result: Optional[str] = None


@dataclass
class ChatResponse:
    """ChatEngine 的响应。"""

    content: str  # 最终回复文字
    tool_calls: list[ToolCall] = field(default_factory=list)
