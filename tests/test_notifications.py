"""测试 WinRT 通知源。"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from kaguya.desktop.notifications import WinRTNotificationSource


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_mock_notification(
    nid: int, app_name: str, title: str, body: str,
) -> MagicMock:
    """创建一个 mock WinRT UserNotification。"""
    n = MagicMock()
    n.id = nid
    n.app_info.display_info.display_name = app_name
    # WinRT DateTime: 100-ns ticks since 1601-01-01 (模拟 2024-01-01)
    n.creation_time.universal_time = 133_475_520_000_000_000

    title_elem = MagicMock()
    title_elem.text = title
    body_elem = MagicMock()
    body_elem.text = body

    binding = MagicMock()
    binding.get_text_elements.return_value = [title_elem, body_elem]
    n.notification.visual.get_binding.return_value = binding
    return n


def _patch_winrt_notifications():
    """Patch WinRT 通知导入。"""
    mock_mod = MagicMock()
    mock_mod.KnownNotificationBindings.TOAST_GENERIC = "toast_generic"
    mock_mod.NotificationKinds.TOAST = 0
    return patch.dict("sys.modules", {
        "winrt.windows.ui.notifications": mock_mod,
    })


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


async def test_import_error_returns_empty():
    """WinRT 包未安装时应返回空列表。"""
    source = WinRTNotificationSource()
    with patch.dict("sys.modules", {
        "winrt": None,
        "winrt.windows": None,
        "winrt.windows.ui": None,
        "winrt.windows.ui.notifications": None,
        "winrt.windows.ui.notifications.management": None,
    }):
        source._initialized = False
        result = await source.get_notifications()
    assert result == []
    assert source._available is False


async def test_permission_denied_returns_empty():
    """访问被拒绝时应返回空列表。"""
    source = WinRTNotificationSource()

    mock_listener = MagicMock()
    mock_listener.request_access_async = AsyncMock(return_value=1)  # DENIED

    mock_mgmt = MagicMock()
    mock_mgmt.UserNotificationListener.get_current.return_value = mock_listener
    mock_mgmt.UserNotificationListenerAccessStatus.ALLOWED = 0
    mock_mgmt.UserNotificationListenerAccessStatus.DENIED = 1
    mock_mgmt.UserNotificationListenerAccessStatus.UNSPECIFIED = 2

    with patch.dict("sys.modules", {
        "winrt.windows.ui.notifications.management": mock_mgmt,
    }):
        source._initialized = False
        result = await source.get_notifications()
    assert result == []
    assert source._available is False


async def test_first_poll_silent_seed():
    """首次轮询应静默 seed ID 集合，不返回通知。"""
    source = WinRTNotificationSource()
    source._initialized = True
    source._available = True
    source._first_poll = True

    n1 = _make_mock_notification(101, "微信", "张三", "你好")
    n2 = _make_mock_notification(102, "QQ", "李四", "在吗")

    mock_listener = MagicMock()
    mock_listener.get_notifications_async = AsyncMock(return_value=[n1, n2])
    source._listener = mock_listener

    with _patch_winrt_notifications():
        results = await source.get_notifications()

    assert results == []
    assert source._seen_ids == {101, 102}
    assert source._first_poll is False


async def test_new_notifications_returned():
    """应只返回新的（未见过的）通知。"""
    source = WinRTNotificationSource()
    source._initialized = True
    source._available = True
    source._first_poll = False

    n1 = _make_mock_notification(201, "微信", "张三", "你好")
    n2 = _make_mock_notification(202, "QQ", "李四", "在吗")

    mock_listener = MagicMock()
    mock_listener.get_notifications_async = AsyncMock(return_value=[n1, n2])
    source._listener = mock_listener

    with _patch_winrt_notifications():
        results = await source.get_notifications()

    assert len(results) == 2
    assert results[0]["pkg"] == "微信"
    assert results[0]["title"] == "张三"
    assert results[0]["text"] == "你好"
    assert results[1]["pkg"] == "QQ"
    assert results[1]["title"] == "李四"
    assert results[1]["text"] == "在吗"
    assert isinstance(results[0]["when"], int)

    # 第二次调用，相同通知 → 返回空
    mock_listener.get_notifications_async = AsyncMock(return_value=[n1, n2])
    with _patch_winrt_notifications():
        results2 = await source.get_notifications()
    assert results2 == []


async def test_seen_ids_cleanup_on_dismissal():
    """通知被清除后，其 ID 应从 seen set 中移除。"""
    source = WinRTNotificationSource()
    source._initialized = True
    source._available = True
    source._first_poll = False

    n1 = _make_mock_notification(301, "微信", "A", "消息1")
    n2 = _make_mock_notification(302, "QQ", "B", "消息2")

    mock_listener = MagicMock()
    source._listener = mock_listener

    # 第一次：两条通知
    mock_listener.get_notifications_async = AsyncMock(return_value=[n1, n2])
    with _patch_winrt_notifications():
        await source.get_notifications()
    assert source._seen_ids == {301, 302}

    # 第二次：n1 被清除
    mock_listener.get_notifications_async = AsyncMock(return_value=[n2])
    with _patch_winrt_notifications():
        await source.get_notifications()
    assert source._seen_ids == {302}  # 301 被清理

    # 第三次：新通知出现
    n3 = _make_mock_notification(303, "微信", "C", "新消息")
    mock_listener.get_notifications_async = AsyncMock(return_value=[n2, n3])
    with _patch_winrt_notifications():
        results = await source.get_notifications()
    assert len(results) == 1
    assert results[0]["title"] == "C"
    assert source._seen_ids == {302, 303}


async def test_extract_notification_missing_fields():
    """通知缺失部分字段时应优雅降级。"""
    source = WinRTNotificationSource()
    source._initialized = True
    source._available = True
    source._first_poll = False

    # 用普通对象模拟字段缺失的通知（MagicMock 不支持属性异常）
    class BrokenNotification:
        id = 401

        @property
        def app_info(self):
            raise AttributeError("no app_info")

        @property
        def creation_time(self):
            raise AttributeError("no creation_time")

        @property
        def notification(self):
            return MagicMock(visual=MagicMock(
                get_binding=MagicMock(return_value=None),
            ))

    mock_listener = MagicMock()
    mock_listener.get_notifications_async = AsyncMock(return_value=[BrokenNotification()])
    source._listener = mock_listener

    with _patch_winrt_notifications():
        results = await source.get_notifications()

    assert len(results) == 1
    assert results[0]["pkg"] == ""
    assert results[0]["title"] == ""
    assert results[0]["text"] == ""
    assert isinstance(results[0]["when"], int)
