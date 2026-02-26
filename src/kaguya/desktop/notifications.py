"""WinRT 通知监听源 — 通过 UserNotificationListener 获取系统 Toast 通知。

替代旧的窗口标题变化检测方案，提供实际的通知内容（应用名、标题、正文、时间戳）。
仅在 Windows 上可用，需要用户在系统设置中授予通知访问权限。

所有重依赖（winrt）均为懒导入。
"""

from __future__ import annotations

import time
from typing import Any

from loguru import logger

# Windows FILETIME epoch 到 Unix epoch 的偏移量（100-ns 间隔）
_EPOCH_DIFF = 116_444_736_000_000_000


class WinRTNotificationSource:
    """通过 WinRT UserNotificationListener 获取系统 Toast 通知。

    实现 notification_source 协议：async get_notifications() -> list[dict]
    每个 dict 包含 {"pkg", "title", "text", "when"}。

    - pkg: 应用显示名称（如 "微信"、"QQ"）
    - title: 通知标题
    - text: 通知正文
    - when: 毫秒 Unix 时间戳
    """

    def __init__(self) -> None:
        self._listener: Any = None
        self._available: bool = False
        self._initialized: bool = False
        self._first_poll: bool = True
        self._seen_ids: set[int] = set()

    async def _ensure_initialized(self) -> bool:
        """懒初始化：首次调用时请求 WinRT 通知访问权限。"""
        if self._initialized:
            return self._available

        self._initialized = True

        try:
            from winrt.windows.ui.notifications.management import (
                UserNotificationListener,
                UserNotificationListenerAccessStatus,
            )
        except ImportError:
            logger.warning(
                "WinRT 通知包未安装，通知功能已禁用。"
                "请运行: uv pip install "
                "winrt-Windows.UI.Notifications.Management "
                "winrt-Windows.UI.Notifications"
            )
            return False

        try:
            self._listener = UserNotificationListener.get_current()
            status = await self._listener.request_access_async()

            if status == UserNotificationListenerAccessStatus.ALLOWED:
                self._available = True
                logger.info("WinRT 通知监听已启用")
            else:
                status_name = {
                    UserNotificationListenerAccessStatus.DENIED: "DENIED",
                    UserNotificationListenerAccessStatus.UNSPECIFIED: "UNSPECIFIED",
                }.get(status, str(status))
                logger.warning(
                    f"WinRT 通知访问被拒绝 (status={status_name})。"
                    "请在 设置 > 隐私和安全性 > 通知 中授予访问权限。"
                )
        except Exception as e:
            logger.warning(f"WinRT 通知监听初始化失败: {e}")

        return self._available

    async def get_notifications(self) -> list[dict[str, Any]]:
        """获取自上次调用以来的新通知。

        返回格式：[{"pkg": "微信", "title": "张三", "text": "你好", "when": 1700000000000}, ...]
        """
        if not await self._ensure_initialized():
            return []

        try:
            from winrt.windows.ui.notifications import (
                KnownNotificationBindings,
                NotificationKinds,
            )

            raw_notifications = await self._listener.get_notifications_async(
                NotificationKinds.TOAST,
            )
        except Exception as e:
            logger.debug(f"获取 WinRT 通知失败: {e}")
            return []

        results: list[dict[str, Any]] = []
        current_ids: set[int] = set()

        for n in raw_notifications:
            nid = n.id
            current_ids.add(nid)

            if nid in self._seen_ids:
                continue

            try:
                result = self._extract_notification(n)
                results.append(result)
            except Exception as e:
                logger.debug(f"解析通知 {nid} 失败: {e}")

        # 首次轮询：仅 seed ID 集合，不返回通知（避免旧通知涌入）
        if self._first_poll:
            self._first_poll = False
            self._seen_ids = current_ids
            return []

        # 更新 seen ID 集合（通知被清除后 ID 自动移除）
        self._seen_ids = current_ids

        if results:
            logger.debug(f"发现 {len(results)} 条新 WinRT 通知")

        return results

    @staticmethod
    def _extract_notification(n: Any) -> dict[str, Any]:
        """从 WinRT UserNotification 提取结构化数据。"""
        # 应用显示名
        app_name = ""
        try:
            app_name = n.app_info.display_info.display_name
        except Exception:
            pass

        # 时间戳（WinRT DateTime → Unix ms）
        try:
            ticks = n.creation_time.universal_time
            unix_us = (ticks - _EPOCH_DIFF) // 10
            when_ms = unix_us // 1000
        except Exception:
            when_ms = int(time.time() * 1000)

        # 通知文本
        title = ""
        body = ""
        try:
            from winrt.windows.ui.notifications import KnownNotificationBindings

            binding = n.notification.visual.get_binding(
                KnownNotificationBindings.TOAST_GENERIC,
            )
            if binding:
                text_elements = binding.get_text_elements()
                texts = [e.text for e in text_elements if e.text]
                if texts:
                    title = texts[0]
                if len(texts) > 1:
                    body = "\n".join(texts[1:])
        except Exception:
            pass

        return {
            "pkg": app_name,
            "title": title,
            "text": body,
            "when": when_ms,
        }
