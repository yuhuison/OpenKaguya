"""测试 desktop 模块基本功能。"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from PIL import Image

from kaguya.desktop.screen import DEFAULT_GRID_SIZE, DesktopScreenReader, ScreenState
from kaguya.desktop.tools import DESKTOP_TOOLS, DesktopToolExecutor


# ---------------------------------------------------------------------------
# ScreenState
# ---------------------------------------------------------------------------


def test_screen_state_grid_info():
    state = ScreenState(
        image=Image.new("RGB", (100, 100)),
        screen_width=1920,
        screen_height=1080,
        grid_cols=16,
        grid_rows=9,
        grid_spacing=120,
        total_points=144,
    )
    info = state.grid_info_text()
    assert "1920×1080" in info
    assert "16列×9行" in info
    assert "144" in info
    assert "120px" in info


def test_screen_state_grid_info_empty():
    state = ScreenState(image=Image.new("RGB", (10, 10)), total_points=0)
    assert state.grid_info_text() == ""


# ---------------------------------------------------------------------------
# DesktopScreenReader — 网格生成（纯逻辑）
# ---------------------------------------------------------------------------


def test_generate_grid_basic():
    """测试网格坐标生成。"""
    mock_ctrl = MagicMock()
    reader = DesktopScreenReader(mock_ctrl, scale=1.0, grid_size=100)
    points, cols, rows = reader._generate_grid(300, 200)

    # 间距100, 起始50: x=[50,150,250] → 3列, y=[50,150] → 2行
    assert cols == 3
    assert rows == 2
    assert len(points) == 6

    # 编号从 1 开始，行优先
    assert points[0] == (1, 50, 50)
    assert points[1] == (2, 150, 50)
    assert points[2] == (3, 250, 50)
    assert points[3] == (4, 50, 150)
    assert points[4] == (5, 150, 150)
    assert points[5] == (6, 250, 150)


def test_generate_grid_default_spacing():
    mock_ctrl = MagicMock()
    reader = DesktopScreenReader(mock_ctrl, scale=0.5)
    # 1920x1080 默认间距120: x=[60,180,...] → 16列, y=[60,180,...] → 9行
    points, cols, rows = reader._generate_grid(1920, 1080)
    assert cols == 16
    assert rows == 9
    assert len(points) == 144


# ---------------------------------------------------------------------------
# DesktopScreenReader — coord 映射
# ---------------------------------------------------------------------------


def test_get_coord_center():
    mock_ctrl = MagicMock()
    reader = DesktopScreenReader(mock_ctrl, scale=1.0, grid_size=100)
    # 模拟 _last_coord_map
    reader._last_coord_map = {1: (50, 50), 2: (150, 50)}
    reader._window_offset = (0, 0)

    assert reader.get_coord_center(1) == (50, 50)
    assert reader.get_coord_center(2) == (150, 50)


def test_get_coord_center_with_window_offset():
    mock_ctrl = MagicMock()
    reader = DesktopScreenReader(mock_ctrl, scale=1.0, grid_size=100)
    reader._last_coord_map = {1: (50, 50)}
    reader._window_offset = (100, 200)

    assert reader.get_coord_center(1) == (150, 250)


def test_get_coord_center_invalid_label():
    mock_ctrl = MagicMock()
    reader = DesktopScreenReader(mock_ctrl)
    reader._last_coord_map = {}
    with pytest.raises(ValueError, match="找不到标签"):
        reader.get_coord_center(999)


# ---------------------------------------------------------------------------
# DESKTOP_TOOLS schema
# ---------------------------------------------------------------------------


def test_desktop_tools_count():
    """确认工具数量为 13。"""
    assert len(DESKTOP_TOOLS) == 13


def test_desktop_tools_names():
    names = {t["function"]["name"] for t in DESKTOP_TOOLS}
    expected = {
        "desktop_screenshot",
        "desktop_click",
        "desktop_click_coord",
        "desktop_double_click",
        "desktop_right_click",
        "desktop_type",
        "desktop_hotkey",
        "desktop_scroll",
        "desktop_drag",
        "desktop_list_windows",
        "desktop_focus_window",
        "desktop_clipboard_read",
        "desktop_clipboard_write",
    }
    assert names == expected


def test_desktop_tools_schema_valid():
    """每个工具都有 function.name, function.description, function.parameters。"""
    for tool in DESKTOP_TOOLS:
        assert tool["type"] == "function"
        fn = tool["function"]
        assert "name" in fn
        assert "description" in fn
        assert "parameters" in fn
        assert fn["parameters"]["type"] == "object"


# ---------------------------------------------------------------------------
# DesktopToolExecutor — mock 测试
# ---------------------------------------------------------------------------


@pytest.fixture
def executor():
    ctrl = MagicMock()
    ctrl.click = AsyncMock()
    ctrl.double_click = AsyncMock()
    ctrl.type_text = AsyncMock()
    ctrl.hotkey = AsyncMock()
    ctrl.scroll = AsyncMock()
    ctrl.drag = AsyncMock()
    ctrl.list_windows = AsyncMock(return_value=[
        {"process": "WeChat.exe", "title": "微信"},
        {"process": "chrome.exe", "title": "Google Chrome"},
    ])
    ctrl.focus_window = AsyncMock(return_value=True)
    ctrl.clipboard_read = AsyncMock(return_value="测试文本")
    ctrl.clipboard_write = AsyncMock()
    ctrl._find_window_by_title = MagicMock(return_value=None)

    reader = MagicMock()
    reader.get_coord_center = MagicMock(return_value=(500, 300))
    reader.read = AsyncMock(return_value=ScreenState(
        image=Image.new("RGB", (960, 540)),
        screen_width=1920,
        screen_height=1080,
        grid_cols=16,
        grid_rows=9,
        grid_spacing=120,
        total_points=144,
    ))

    return DesktopToolExecutor(ctrl, reader)


async def test_tool_click(executor):
    result = await executor.execute("desktop_click", {"label": 5, "x_offset": 10, "y_offset": -5})
    assert result["success"] is True
    assert "标记点 5" in result["clicked"]
    executor.controller.click.assert_awaited_once_with(510, 295)


async def test_tool_click_coord(executor):
    result = await executor.execute("desktop_click_coord", {"x": 100, "y": 200})
    assert result["success"] is True
    executor.controller.click.assert_awaited_once_with(100, 200)


async def test_tool_double_click(executor):
    result = await executor.execute("desktop_double_click", {"label": 3})
    assert result["success"] is True
    executor.controller.double_click.assert_awaited_once_with(500, 300)


async def test_tool_type(executor):
    result = await executor.execute("desktop_type", {"text": "你好世界"})
    assert result["success"] is True
    assert result["typed"] == "你好世界"


async def test_tool_hotkey(executor):
    result = await executor.execute("desktop_hotkey", {"keys": "ctrl+c"})
    assert result["success"] is True
    executor.controller.hotkey.assert_awaited_once_with("ctrl", "c")


async def test_tool_scroll(executor):
    result = await executor.execute("desktop_scroll", {
        "label": 10, "direction": "down", "clicks": 5,
    })
    assert result["success"] is True
    executor.controller.scroll.assert_awaited_once_with(500, 300, 5, "down")


async def test_tool_list_windows(executor):
    result = await executor.execute("desktop_list_windows", {})
    assert result["count"] == 2
    assert "微信" in result["summary"]
    assert "chrome.exe" in result["summary"]


async def test_tool_focus_window(executor):
    result = await executor.execute("desktop_focus_window", {"title": "微信"})
    assert result["success"] is True


async def test_tool_clipboard_read(executor):
    result = await executor.execute("desktop_clipboard_read", {})
    assert result["text"] == "测试文本"


async def test_tool_clipboard_write(executor):
    result = await executor.execute("desktop_clipboard_write", {"text": "复制的内容"})
    assert result["success"] is True
    executor.controller.clipboard_write.assert_awaited_once_with("复制的内容")


async def test_tool_unknown(executor):
    result = await executor.execute("nonexistent_tool", {})
    assert "error" in result
