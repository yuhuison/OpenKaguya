"""phone/tools.py — 暴露给 LLM 的手机操作工具。

V2 纯视觉版：AI 通过截图上的编号圆圈标记点进行交互。
点击支持 label + 偏移量微调，滑动支持两点之间滑动。
"""

from __future__ import annotations

import asyncio
import base64
import io
from typing import TYPE_CHECKING, Any

from loguru import logger

from kaguya.phone.controller import PhoneController
from kaguya.phone.screen import ScreenReader, ScreenState

if TYPE_CHECKING:
    from kaguya.tools.workspace import WorkspaceManager


# ---------------------------------------------------------------------------
# 工具 Schema 定义
# ---------------------------------------------------------------------------

PHONE_TOOLS: list[dict] = [
    {
        "type": "function",
        "function": {
            "name": "phone_screenshot",
            "description": (
                "截取手机屏幕，返回带编号圆圈标记的截图。"
                "截图上覆盖红色圆圈标记点，每个圆圈旁有数字编号（从左到右、从上到下递增）。"
                "截图还会返回网格间距信息，你可以据此估算偏移量。"
                "如果发现屏幕处于锁屏/息屏状态，先调用 phone_unlock。"
            ),
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "phone_unlock",
            "description": "唤醒手机屏幕并上滑解锁（适用于无密码锁屏）。",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "phone_tap",
            "description": (
                "点击截图中编号标记点附近的位置。"
                "选择离目标最近的标记点编号，然后用 x_offset/y_offset 偏移到目标实际位置。"
                "大多数按钮不在标记点正上方，务必估算偏移量！"
                "例如：目标在标记点 15 右边约半格 → phone_tap(label=15, x_offset=60)。"
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
            "name": "phone_tap_coord",
            "description": (
                "按像素坐标直接点击屏幕（原始分辨率）。"
                "适合精确点击：根据截图中标记点的位置推算目标坐标。"
                "例如：目标在标记点 5(540,60) 和标记点 14(540,180) 中间偏右 → phone_tap_coord(x=600, y=120)。"
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
            "name": "phone_long_press",
            "description": (
                "长按截图中编号圆圈对应的位置（触发上下文菜单等）。"
                "支持 x_offset/y_offset 微调。"
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
            "name": "phone_long_press_coord",
            "description": "按像素坐标长按屏幕（原始分辨率）。",
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
            "name": "phone_type",
            "description": "在当前焦点处输入文字（支持中文）。调用前请先点击输入框。",
            "parameters": {
                "type": "object",
                "properties": {
                    "text": {
                        "type": "string",
                        "description": "要输入的文字内容",
                    }
                },
                "required": ["text"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "phone_swipe",
            "description": (
                "从一个标记点滑动到另一个标记点。"
                "适用于滚动页面、切换标签页等操作。"
                "例如：向下滚动可从靠上的点滑到靠下的点。"
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
            "name": "phone_back",
            "description": "按返回键。",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "phone_home",
            "description": "按主屏幕键，回到桌面。",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "phone_open_app",
            "description": (
                "打开指定 App 的主界面。支持中文名称（如「微信」）或包名。"
                "注意：这只会打开 App 首页，不会跳转到特定聊天或页面。"
                "如果要打开通知对应的页面，请改用 phone_pull_notifications 下拉通知栏后点击通知。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {
                        "type": "string",
                        "description": "App 名称或包名",
                    }
                },
                "required": ["name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "phone_pull_notifications",
            "description": (
                "下拉展开手机通知栏。展开后请截图查看通知，然后点击目标通知即可跳转到对应页面。"
                "这是处理通知的推荐方式：先下拉通知栏 → 截图 → 点击通知 → 自动跳转到对应页面。"
                "处理完毕后可用 phone_back 关闭通知栏。"
            ),
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "phone_notifications",
            "description": "获取手机当前通知列表（纯文本，用于了解有哪些通知）。",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "phone_save_screenshot",
            "description": (
                "截取手机原始屏幕（无标记覆盖）并保存到 workspace。"
                "适合保存截图作为记录，或需要原始画质的场景。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "filename": {
                        "type": "string",
                        "description": "保存文件名（如 'screenshot.png'），保存到 workspace",
                    },
                },
                "required": ["filename"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "phone_pull_file",
            "description": (
                "从手机拉取文件到 workspace。"
                "例如：用户通过微信发了图片，可以从手机存储中拉取到 workspace 进行查看或处理。"
                "常用手机路径：/sdcard/Pictures/（图片）、/sdcard/Download/（下载）、"
                "/sdcard/DCIM/Camera/（相机照片）、/sdcard/Android/data/com.tencent.mm/（微信）。"
                "如果不确定文件路径，可以先用 phone_list_files 查看目录内容。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "remote_path": {
                        "type": "string",
                        "description": "手机上的文件绝对路径",
                    },
                    "save_as": {
                        "type": "string",
                        "description": "保存到 workspace 的文件名（如 'photo.jpg'）",
                    },
                },
                "required": ["remote_path", "save_as"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "phone_push_file",
            "description": (
                "将 workspace 中的文件推送到手机。"
                "例如：将生成的图片推送到手机以便通过微信发送。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "workspace_path": {
                        "type": "string",
                        "description": "workspace 中的文件相对路径",
                    },
                    "remote_path": {
                        "type": "string",
                        "description": "手机上的目标路径（如 '/sdcard/Download/image.png'）",
                    },
                },
                "required": ["workspace_path", "remote_path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "phone_list_files",
            "description": (
                "列出手机上指定目录的文件列表。"
                "用于查找文件路径，然后通过 phone_pull_file 拉取到 workspace。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "手机上的目录路径（如 '/sdcard/Download/'）",
                    },
                },
                "required": ["path"],
            },
        },
    },
]


# ---------------------------------------------------------------------------
# 工具执行器
# ---------------------------------------------------------------------------


class PhoneToolExecutor:
    """将 LLM 工具调用路由到 PhoneController 和 ScreenReader。"""

    def __init__(
        self,
        controller: PhoneController,
        screen_reader: ScreenReader,
        workspace: "WorkspaceManager | None" = None,
    ):
        self.controller = controller
        self.screen_reader = screen_reader
        self.workspace = workspace

    async def execute(self, tool_name: str, args: dict[str, Any]) -> dict[str, Any]:
        handler = getattr(self, f"_tool_{tool_name}", None)
        if handler is None:
            return {"error": f"未知工具: {tool_name}"}
        try:
            return await handler(**args)
        except Exception as e:
            logger.error(f"手机工具 [{tool_name}] 执行失败: {e}")
            return {"error": str(e)}

    # --- 截图 ---

    async def _tool_phone_screenshot(self) -> dict[str, Any]:
        await asyncio.sleep(2)  # 等待 UI 动画完成
        state: ScreenState = await self.screen_reader.read()
        buf = io.BytesIO()
        state.image.save(buf, format="JPEG", quality=85)
        img_b64 = base64.b64encode(buf.getvalue()).decode()
        return {
            "image_base64": img_b64,
            "image_media_type": "image/jpeg",
            "text": state.grid_info_text(),
        }

    async def _tool_phone_unlock(self) -> dict[str, Any]:
        msg = await self.controller.wake_and_unlock()
        return {"success": True, "message": msg}

    # --- 点击 ---

    async def _tool_phone_tap(
        self, label: int, x_offset: int = 0, y_offset: int = 0,
    ) -> dict[str, Any]:
        try:
            x, y = self.screen_reader.get_coord_center(label)
        except ValueError as e:
            return {"error": str(e)}
        x += x_offset
        y += y_offset
        await self.controller.tap(x, y)
        offset_str = ""
        if x_offset or y_offset:
            offset_str = f" +偏移({x_offset},{y_offset})"
        return {"success": True, "tapped": f"标记点 {label}{offset_str} → ({x}, {y})"}

    async def _tool_phone_tap_coord(self, x: int, y: int) -> dict[str, Any]:
        await self.controller.tap(x, y)
        return {"success": True, "tapped": f"({x}, {y})"}

    # --- 长按 ---

    async def _tool_phone_long_press(
        self, label: int, x_offset: int = 0, y_offset: int = 0,
    ) -> dict[str, Any]:
        try:
            x, y = self.screen_reader.get_coord_center(label)
        except ValueError as e:
            return {"error": str(e)}
        x += x_offset
        y += y_offset
        await self.controller.long_press(x, y)
        return {"success": True, "long_pressed": f"标记点 {label} → ({x}, {y})"}

    async def _tool_phone_long_press_coord(self, x: int, y: int) -> dict[str, Any]:
        await self.controller.long_press(x, y)
        return {"success": True, "long_pressed": f"({x}, {y})"}

    # --- 输入/导航 ---

    async def _tool_phone_type(self, text: str) -> dict[str, Any]:
        await self.controller.type_text(text)
        return {"success": True, "typed": text}

    async def _tool_phone_swipe(
        self, from_label: int, to_label: int,
    ) -> dict[str, Any]:
        try:
            x1, y1 = self.screen_reader.get_coord_center(from_label)
            x2, y2 = self.screen_reader.get_coord_center(to_label)
        except ValueError as e:
            return {"error": str(e)}
        await self.controller.swipe_between(x1, y1, x2, y2)
        return {
            "success": True,
            "swiped": f"标记点 {from_label}({x1},{y1}) → {to_label}({x2},{y2})",
        }

    async def _tool_phone_back(self) -> dict[str, Any]:
        await self.controller.press_key("back")
        return {"success": True}

    async def _tool_phone_home(self) -> dict[str, Any]:
        await self.controller.press_key("home")
        return {"success": True}

    async def _tool_phone_open_app(self, name: str) -> dict[str, Any]:
        await self.controller.open_app(name)
        return {"success": True, "opened": name, "hint": "已打开 App 主界面，建议截图确认"}

    async def _tool_phone_pull_notifications(self) -> dict[str, Any]:
        await self.controller.expand_notification_shade()
        await asyncio.sleep(0.5)  # 等待下拉动画完成
        return {"success": True, "hint": "通知栏已展开，请截图查看并点击目标通知"}

    async def _tool_phone_notifications(self) -> dict[str, Any]:
        notifications = await self.controller.get_notifications()
        if not notifications:
            return {"notifications": [], "summary": "当前没有通知"}
        lines = []
        for i, n in enumerate(notifications, 1):
            pkg = n.get("pkg", "未知")
            title = n.get("title", "")
            text = n.get("text", "")
            lines.append(f"{i}. [{pkg}] {title}: {text}")
        return {"summary": "\n".join(lines), "count": len(notifications)}

    # --- 文件传输 ---

    def _require_workspace(self) -> "WorkspaceManager":
        if self.workspace is None:
            raise RuntimeError("Workspace 未配置")
        return self.workspace

    async def _tool_phone_save_screenshot(self, filename: str) -> dict[str, Any]:
        ws = self._require_workspace()
        img = await self.controller.screenshot()
        resolved = ws.resolve_path(filename)
        resolved.parent.mkdir(parents=True, exist_ok=True)
        await asyncio.to_thread(img.save, str(resolved))
        logger.info(f"原始截图已保存: {filename}")
        return {"success": True, "saved": filename, "size": f"{img.size[0]}x{img.size[1]}"}

    async def _tool_phone_pull_file(
        self, remote_path: str, save_as: str,
    ) -> dict[str, Any]:
        ws = self._require_workspace()
        resolved = ws.resolve_path(save_as)
        resolved.parent.mkdir(parents=True, exist_ok=True)
        await self.controller.pull_file(remote_path, str(resolved))
        size = resolved.stat().st_size
        logger.info(f"手机文件已拉取: {remote_path} → {save_as} ({size} bytes)")
        return {"success": True, "saved": save_as, "size_bytes": size}

    async def _tool_phone_push_file(
        self, workspace_path: str, remote_path: str,
    ) -> dict[str, Any]:
        ws = self._require_workspace()
        resolved = ws.resolve_path(workspace_path)
        if not resolved.exists():
            return {"error": f"Workspace 文件不存在: {workspace_path}"}
        await self.controller.push_file(str(resolved), remote_path)
        logger.info(f"文件已推送到手机: {workspace_path} → {remote_path}")
        return {"success": True, "pushed": remote_path}

    async def _tool_phone_list_files(self, path: str) -> dict[str, Any]:
        lines = await self.controller.list_dir(path)
        return {"success": True, "path": path, "files": "\n".join(lines)}
