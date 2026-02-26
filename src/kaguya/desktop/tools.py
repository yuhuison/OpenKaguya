"""desktop/tools.py — 暴露给 LLM 的桌面操作工具。

V2 桌面版：AI 通过截图上的编号圆圈标记点进行交互。
"""

from __future__ import annotations

import asyncio
import base64
import io
from typing import Any

from loguru import logger

from kaguya.desktop.controller import DesktopController
from kaguya.desktop.screen import DesktopScreenReader, ScreenState


# ---------------------------------------------------------------------------
# 工具 Schema 定义
# ---------------------------------------------------------------------------

DESKTOP_TOOLS: list[dict] = [
    {
        "type": "function",
        "function": {
            "name": "desktop_screenshot",
            "description": (
                "截取电脑桌面屏幕，返回带编号圆圈标记的截图。"
                "截图上覆盖红色圆圈标记点，每个圆圈旁有数字编号（从左到右、从上到下递增）。"
                "可选参数 window_title 指定截取某个窗口（模糊匹配）。不填则全屏截图。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "window_title": {
                        "type": "string",
                        "description": "可选，窗口标题关键词（模糊匹配），截取该窗口。不填则全屏。",
                    },
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "desktop_click",
            "description": (
                "点击截图中编号标记点附近的位置。"
                "选择离目标最近的标记点编号，然后用 x_offset/y_offset 偏移到目标实际位置。"
                "大多数按钮不在标记点正上方，务必估算偏移量！"
                "偏移方向：x正=右，x负=左，y正=下，y负=上。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "label": {
                        "type": "integer",
                        "description": "标记点编号",
                    },
                    "x_offset": {
                        "type": "integer",
                        "description": "水平偏移像素（正=右，负=左），默认0",
                    },
                    "y_offset": {
                        "type": "integer",
                        "description": "垂直偏移像素（正=下，负=上），默认0",
                    },
                },
                "required": ["label"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "desktop_click_coord",
            "description": (
                "按像素坐标直接点击屏幕。"
                "适合精确点击：根据截图中标记点的位置推算目标坐标。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "x": {"type": "integer", "description": "横坐标"},
                    "y": {"type": "integer", "description": "纵坐标"},
                },
                "required": ["x", "y"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "desktop_double_click",
            "description": "双击截图中编号标记点附近的位置（打开文件、选中词语等）。",
            "parameters": {
                "type": "object",
                "properties": {
                    "label": {
                        "type": "integer",
                        "description": "标记点编号",
                    },
                    "x_offset": {
                        "type": "integer",
                        "description": "水平偏移像素，默认0",
                    },
                    "y_offset": {
                        "type": "integer",
                        "description": "垂直偏移像素，默认0",
                    },
                },
                "required": ["label"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "desktop_right_click",
            "description": "右键点击截图中编号标记点附近的位置（打开右键菜单）。",
            "parameters": {
                "type": "object",
                "properties": {
                    "label": {
                        "type": "integer",
                        "description": "标记点编号",
                    },
                    "x_offset": {
                        "type": "integer",
                        "description": "水平偏移像素，默认0",
                    },
                    "y_offset": {
                        "type": "integer",
                        "description": "垂直偏移像素，默认0",
                    },
                },
                "required": ["label"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "desktop_type",
            "description": "在当前焦点处输入文字（支持中文）。调用前请先点击输入框。",
            "parameters": {
                "type": "object",
                "properties": {
                    "text": {
                        "type": "string",
                        "description": "要输入的文字内容",
                    },
                },
                "required": ["text"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "desktop_hotkey",
            "description": (
                "发送键盘快捷键组合。"
                "例如：'ctrl+c' 复制、'ctrl+v' 粘贴、'alt+tab' 切换窗口、"
                "'ctrl+a' 全选、'alt+f4' 关闭窗口、'win+d' 显示桌面。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "keys": {
                        "type": "string",
                        "description": "快捷键组合，用 + 分隔，如 'ctrl+c', 'ctrl+shift+s'",
                    },
                },
                "required": ["keys"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "desktop_scroll",
            "description": (
                "在标记点位置滚动鼠标滚轮。"
                "适用于滚动网页、文档、列表等。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "label": {
                        "type": "integer",
                        "description": "滚动位置的标记点编号",
                    },
                    "direction": {
                        "type": "string",
                        "enum": ["up", "down"],
                        "description": "滚动方向",
                    },
                    "clicks": {
                        "type": "integer",
                        "description": "滚动格数（默认3）",
                    },
                },
                "required": ["label", "direction"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "desktop_drag",
            "description": (
                "从一个标记点拖动到另一个标记点。"
                "适用于拖拽文件、选择文本范围、移动窗口等。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "from_label": {
                        "type": "integer",
                        "description": "起始标记点编号",
                    },
                    "to_label": {
                        "type": "integer",
                        "description": "目标标记点编号",
                    },
                },
                "required": ["from_label", "to_label"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "desktop_list_windows",
            "description": "列出电脑上所有可见的窗口（标题、进程名）。",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "desktop_focus_window",
            "description": (
                "聚焦指定窗口（将其置于最前方）。"
                "支持窗口标题关键词（模糊匹配）。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "title": {
                        "type": "string",
                        "description": "窗口标题关键词",
                    },
                },
                "required": ["title"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "desktop_clipboard_read",
            "description": "读取电脑剪贴板中的文本内容。",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "desktop_clipboard_write",
            "description": "将文本写入电脑剪贴板。之后可以用 Ctrl+V 粘贴。",
            "parameters": {
                "type": "object",
                "properties": {
                    "text": {
                        "type": "string",
                        "description": "要写入剪贴板的文本",
                    },
                },
                "required": ["text"],
            },
        },
    },
]


# ---------------------------------------------------------------------------
# 工具执行器
# ---------------------------------------------------------------------------


class DesktopToolExecutor:
    """将 LLM 工具调用路由到 DesktopController 和 DesktopScreenReader。"""

    def __init__(
        self,
        controller: DesktopController,
        screen_reader: DesktopScreenReader,
    ):
        self.controller = controller
        self.screen_reader = screen_reader

    async def execute(self, tool_name: str, args: dict[str, Any]) -> dict[str, Any]:
        handler = getattr(self, f"_tool_{tool_name}", None)
        if handler is None:
            return {"error": f"未知工具: {tool_name}"}
        try:
            return await handler(**args)
        except Exception as e:
            logger.error(f"桌面工具 [{tool_name}] 执行失败: {e}")
            return {"error": str(e)}

    # --- 截图 ---

    async def _tool_desktop_screenshot(
        self, window_title: str = "",
    ) -> dict[str, Any]:
        await asyncio.sleep(0.5)  # 等待 UI 动画完成

        hwnd = None
        if window_title:
            hwnd = self.controller._find_window_by_title(window_title)
            if not hwnd:
                return {"error": f"找不到标题包含「{window_title}」的窗口"}
            # 先聚焦窗口
            self.controller.focus_window_sync(hwnd)
            await asyncio.sleep(0.3)

        state: ScreenState = await self.screen_reader.read(hwnd)
        buf = io.BytesIO()
        state.image.save(buf, format="JPEG", quality=85)
        img_b64 = base64.b64encode(buf.getvalue()).decode()
        return {
            "image_base64": img_b64,
            "image_media_type": "image/jpeg",
            "text": state.grid_info_text(),
        }

    # --- 点击 ---

    async def _tool_desktop_click(
        self, label: int, x_offset: int = 0, y_offset: int = 0,
    ) -> dict[str, Any]:
        try:
            x, y = self.screen_reader.get_coord_center(label)
        except ValueError as e:
            return {"error": str(e)}
        x += x_offset
        y += y_offset
        await self.controller.click(x, y)
        offset_str = ""
        if x_offset or y_offset:
            offset_str = f" +偏移({x_offset},{y_offset})"
        return {"success": True, "clicked": f"标记点 {label}{offset_str} → ({x}, {y})"}

    async def _tool_desktop_click_coord(self, x: int, y: int) -> dict[str, Any]:
        await self.controller.click(x, y)
        return {"success": True, "clicked": f"({x}, {y})"}

    async def _tool_desktop_double_click(
        self, label: int, x_offset: int = 0, y_offset: int = 0,
    ) -> dict[str, Any]:
        try:
            x, y = self.screen_reader.get_coord_center(label)
        except ValueError as e:
            return {"error": str(e)}
        x += x_offset
        y += y_offset
        await self.controller.double_click(x, y)
        return {"success": True, "double_clicked": f"标记点 {label} → ({x}, {y})"}

    async def _tool_desktop_right_click(
        self, label: int, x_offset: int = 0, y_offset: int = 0,
    ) -> dict[str, Any]:
        try:
            x, y = self.screen_reader.get_coord_center(label)
        except ValueError as e:
            return {"error": str(e)}
        x += x_offset
        y += y_offset
        await self.controller.click(x, y, button="right")
        return {"success": True, "right_clicked": f"标记点 {label} → ({x}, {y})"}

    # --- 输入 ---

    async def _tool_desktop_type(self, text: str) -> dict[str, Any]:
        await self.controller.type_text(text)
        return {"success": True, "typed": text}

    async def _tool_desktop_hotkey(self, keys: str) -> dict[str, Any]:
        parts = [k.strip() for k in keys.split("+")]
        await self.controller.hotkey(*parts)
        return {"success": True, "hotkey": keys}

    # --- 滚动/拖动 ---

    async def _tool_desktop_scroll(
        self, label: int, direction: str, clicks: int = 3,
    ) -> dict[str, Any]:
        try:
            x, y = self.screen_reader.get_coord_center(label)
        except ValueError as e:
            return {"error": str(e)}
        await self.controller.scroll(x, y, clicks, direction)
        return {"success": True, "scrolled": f"标记点 {label} {direction} ×{clicks}"}

    async def _tool_desktop_drag(
        self, from_label: int, to_label: int,
    ) -> dict[str, Any]:
        try:
            x1, y1 = self.screen_reader.get_coord_center(from_label)
            x2, y2 = self.screen_reader.get_coord_center(to_label)
        except ValueError as e:
            return {"error": str(e)}
        await self.controller.drag(x1, y1, x2, y2)
        return {
            "success": True,
            "dragged": f"标记点 {from_label}({x1},{y1}) → {to_label}({x2},{y2})",
        }

    # --- 窗口管理 ---

    async def _tool_desktop_list_windows(self) -> dict[str, Any]:
        windows = await self.controller.list_windows()
        if not windows:
            return {"windows": [], "summary": "没有检测到可见窗口"}
        lines = []
        for i, w in enumerate(windows, 1):
            lines.append(f"{i}. [{w['process']}] {w['title']}")
        return {"summary": "\n".join(lines), "count": len(windows)}

    async def _tool_desktop_focus_window(self, title: str) -> dict[str, Any]:
        ok = await self.controller.focus_window(title)
        if ok:
            return {"success": True, "focused": title}
        return {"error": f"找不到标题包含「{title}」的窗口"}

    # --- 剪贴板 ---

    async def _tool_desktop_clipboard_read(self) -> dict[str, Any]:
        text = await self.controller.clipboard_read()
        if not text:
            return {"text": "", "message": "剪贴板为空"}
        return {"text": text}

    async def _tool_desktop_clipboard_write(self, text: str) -> dict[str, Any]:
        await self.controller.clipboard_write(text)
        return {"success": True, "message": f"已写入剪贴板（{len(text)}字符）"}
