"""DesktopController — Windows 桌面控制（纯 ctypes + PIL）。

所有阻塞操作均通过 asyncio.to_thread 包装。
"""

from __future__ import annotations

import asyncio
import ctypes
import ctypes.wintypes as wt
import time
from typing import Any

from loguru import logger
import mss
from PIL import Image

# ---------------------------------------------------------------------------
# Win32 常量
# ---------------------------------------------------------------------------

INPUT_MOUSE = 0
INPUT_KEYBOARD = 1

MOUSEEVENTF_MOVE = 0x0001
MOUSEEVENTF_LEFTDOWN = 0x0002
MOUSEEVENTF_LEFTUP = 0x0004
MOUSEEVENTF_RIGHTDOWN = 0x0008
MOUSEEVENTF_RIGHTUP = 0x0010
MOUSEEVENTF_MIDDLEDOWN = 0x0020
MOUSEEVENTF_MIDDLEUP = 0x0040
MOUSEEVENTF_WHEEL = 0x0800
MOUSEEVENTF_ABSOLUTE = 0x8000

KEYEVENTF_KEYUP = 0x0002
KEYEVENTF_UNICODE = 0x0004

WHEEL_DELTA = 120

SM_CXSCREEN = 0
SM_CYSCREEN = 1

CF_UNICODETEXT = 13
GMEM_MOVEABLE = 0x0002

SW_RESTORE = 9
GW_OWNER = 4
GWL_EXSTYLE = -20
WS_EX_TOOLWINDOW = 0x00000080

PROCESS_QUERY_LIMITED_INFORMATION = 0x1000

# VK 常量
VK_MAP: dict[str, int] = {
    "ctrl": 0x11, "control": 0x11,
    "alt": 0x12, "menu": 0x12,
    "shift": 0x10,
    "win": 0x5B, "lwin": 0x5B, "rwin": 0x5C,
    "tab": 0x09,
    "enter": 0x0D, "return": 0x0D,
    "esc": 0x1B, "escape": 0x1B,
    "backspace": 0x08, "back": 0x08,
    "delete": 0x2E, "del": 0x2E,
    "space": 0x20,
    "up": 0x26, "down": 0x28, "left": 0x25, "right": 0x27,
    "home": 0x24, "end": 0x23,
    "pageup": 0x21, "pagedown": 0x22,
    "insert": 0x2D,
    "printscreen": 0x2C,
    "f1": 0x70, "f2": 0x71, "f3": 0x72, "f4": 0x73,
    "f5": 0x74, "f6": 0x75, "f7": 0x76, "f8": 0x77,
    "f9": 0x78, "f10": 0x79, "f11": 0x7A, "f12": 0x7B,
}

# ---------------------------------------------------------------------------
# Win32 结构体
# ---------------------------------------------------------------------------

user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32
psapi = ctypes.windll.psapi


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [
        ("dx", wt.LONG),
        ("dy", wt.LONG),
        ("mouseData", wt.DWORD),
        ("dwFlags", wt.DWORD),
        ("time", wt.DWORD),
        ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong)),
    ]


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [
        ("wVk", wt.WORD),
        ("wScan", wt.WORD),
        ("dwFlags", wt.DWORD),
        ("time", wt.DWORD),
        ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong)),
    ]


class _INPUT_UNION(ctypes.Union):
    _fields_ = [("mi", MOUSEINPUT), ("ki", KEYBDINPUT)]


class INPUT(ctypes.Structure):
    _fields_ = [("type", wt.DWORD), ("union", _INPUT_UNION)]


def _send_input(*inputs: INPUT) -> int:
    n = len(inputs)
    arr = (INPUT * n)(*inputs)
    return user32.SendInput(n, arr, ctypes.sizeof(INPUT))


# ---------------------------------------------------------------------------
# DesktopController
# ---------------------------------------------------------------------------


class DesktopController:
    """通过 Win32 API 控制 Windows 桌面。"""

    def __init__(self) -> None:
        self._prev_titles: dict[int, str] = {}  # hwnd → 上次窗口标题（通知检测用）

    # ------------------------------------------------------------------
    # 截图
    # ------------------------------------------------------------------

    def screenshot_sync(self, hwnd: int | None = None) -> Image.Image:
        """同步截图（mss/DXGI，无需物理显示器）。hwnd=None 时全屏，否则截取指定窗口。"""
        with mss.mss() as sct:
            if hwnd is not None:
                rect = wt.RECT()
                user32.GetWindowRect(hwnd, ctypes.byref(rect))
                monitor = {
                    "left": rect.left, "top": rect.top,
                    "width": rect.right - rect.left,
                    "height": rect.bottom - rect.top,
                }
            else:
                monitor = sct.monitors[1]  # 主屏
            shot = sct.grab(monitor)
            return Image.frombytes("RGB", shot.size, shot.bgra, "raw", "BGRX")

    async def screenshot(self, hwnd: int | None = None) -> Image.Image:
        return await asyncio.to_thread(self.screenshot_sync, hwnd)

    def get_screen_size(self) -> tuple[int, int]:
        w = user32.GetSystemMetrics(SM_CXSCREEN)
        h = user32.GetSystemMetrics(SM_CYSCREEN)
        return w, h

    # ------------------------------------------------------------------
    # 鼠标操作
    # ------------------------------------------------------------------

    def _mouse_input(self, flags: int, dx: int = 0, dy: int = 0, data: int = 0) -> INPUT:
        inp = INPUT()
        inp.type = INPUT_MOUSE
        inp.union.mi.dx = dx
        inp.union.mi.dy = dy
        inp.union.mi.mouseData = data
        inp.union.mi.dwFlags = flags
        return inp

    def click_sync(self, x: int, y: int, button: str = "left") -> None:
        """点击屏幕坐标。button: left/right/middle。"""
        user32.SetCursorPos(x, y)
        time.sleep(0.02)
        if button == "left":
            down, up = MOUSEEVENTF_LEFTDOWN, MOUSEEVENTF_LEFTUP
        elif button == "right":
            down, up = MOUSEEVENTF_RIGHTDOWN, MOUSEEVENTF_RIGHTUP
        elif button == "middle":
            down, up = MOUSEEVENTF_MIDDLEDOWN, MOUSEEVENTF_MIDDLEUP
        else:
            down, up = MOUSEEVENTF_LEFTDOWN, MOUSEEVENTF_LEFTUP
        _send_input(self._mouse_input(down), self._mouse_input(up))
        logger.debug(f"点击: ({x}, {y}) [{button}]")

    async def click(self, x: int, y: int, button: str = "left") -> None:
        await asyncio.to_thread(self.click_sync, x, y, button)

    def double_click_sync(self, x: int, y: int) -> None:
        user32.SetCursorPos(x, y)
        time.sleep(0.02)
        down = self._mouse_input(MOUSEEVENTF_LEFTDOWN)
        up = self._mouse_input(MOUSEEVENTF_LEFTUP)
        _send_input(down, up)
        time.sleep(0.05)
        _send_input(down, up)
        logger.debug(f"双击: ({x}, {y})")

    async def double_click(self, x: int, y: int) -> None:
        await asyncio.to_thread(self.double_click_sync, x, y)

    def scroll_sync(self, x: int, y: int, clicks: int = 3, direction: str = "down") -> None:
        """滚轮滚动。direction: up/down。clicks: 滚动格数。"""
        user32.SetCursorPos(x, y)
        time.sleep(0.02)
        delta = clicks * WHEEL_DELTA * (1 if direction == "up" else -1)
        _send_input(self._mouse_input(MOUSEEVENTF_WHEEL, data=delta))
        logger.debug(f"滚动: ({x}, {y}) {direction} ×{clicks}")

    async def scroll(self, x: int, y: int, clicks: int = 3,
                     direction: str = "down") -> None:
        await asyncio.to_thread(self.scroll_sync, x, y, clicks, direction)

    def drag_sync(self, x1: int, y1: int, x2: int, y2: int,
                  duration: float = 0.3) -> None:
        """从 (x1,y1) 拖动到 (x2,y2)。"""
        user32.SetCursorPos(x1, y1)
        time.sleep(0.05)
        _send_input(self._mouse_input(MOUSEEVENTF_LEFTDOWN))

        steps = max(10, int(duration * 60))
        for i in range(1, steps + 1):
            t = i / steps
            cx = int(x1 + (x2 - x1) * t)
            cy = int(y1 + (y2 - y1) * t)
            user32.SetCursorPos(cx, cy)
            time.sleep(duration / steps)

        _send_input(self._mouse_input(MOUSEEVENTF_LEFTUP))
        logger.debug(f"拖动: ({x1},{y1}) → ({x2},{y2})")

    async def drag(self, x1: int, y1: int, x2: int, y2: int,
                   duration: float = 0.3) -> None:
        await asyncio.to_thread(self.drag_sync, x1, y1, x2, y2, duration)

    # ------------------------------------------------------------------
    # 键盘输入
    # ------------------------------------------------------------------

    def _key_input(self, vk: int = 0, scan: int = 0, flags: int = 0) -> INPUT:
        inp = INPUT()
        inp.type = INPUT_KEYBOARD
        inp.union.ki.wVk = vk
        inp.union.ki.wScan = scan
        inp.union.ki.dwFlags = flags
        return inp

    def type_text_sync(self, text: str) -> None:
        """输入文字（直接发 Unicode，天然支持中文）。批量发送以提高效率。"""
        if not text:
            return
        inputs: list[INPUT] = []
        for ch in text:
            code = ord(ch)
            inputs.append(self._key_input(scan=code, flags=KEYEVENTF_UNICODE))
            inputs.append(
                self._key_input(scan=code, flags=KEYEVENTF_UNICODE | KEYEVENTF_KEYUP)
            )
        _send_input(*inputs)
        logger.debug(f"输入文字: {text[:50]}")

    async def type_text(self, text: str) -> None:
        await asyncio.to_thread(self.type_text_sync, text)

    def hotkey_sync(self, *keys: str) -> None:
        """发送组合键，如 hotkey("ctrl", "c")。"""
        vk_codes: list[int] = []
        for key in keys:
            key_lower = key.lower()
            if key_lower in VK_MAP:
                vk_codes.append(VK_MAP[key_lower])
            elif len(key) == 1 and key.isalnum():
                vk_codes.append(ord(key.upper()))
            else:
                logger.warning(f"未知按键: {key}")
                return

        # 按下所有键
        for vk in vk_codes:
            _send_input(self._key_input(vk=vk))
        time.sleep(0.05)
        # 释放（倒序）
        for vk in reversed(vk_codes):
            _send_input(self._key_input(vk=vk, flags=KEYEVENTF_KEYUP))
        logger.debug(f"快捷键: {'+'.join(keys)}")

    async def hotkey(self, *keys: str) -> None:
        await asyncio.to_thread(self.hotkey_sync, *keys)

    # ------------------------------------------------------------------
    # 窗口管理
    # ------------------------------------------------------------------

    def list_windows_sync(self) -> list[dict[str, Any]]:
        """列出所有可见的顶层窗口。"""
        windows: list[dict[str, Any]] = []

        @ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)
        def _enum_cb(hwnd: int, _lparam: int) -> bool:
            if not user32.IsWindowVisible(hwnd):
                return True
            # 跳过工具窗口
            ex_style = user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
            if ex_style & WS_EX_TOOLWINDOW:
                return True
            # 跳过有 owner 的窗口（弹出对话框等）
            if user32.GetWindow(hwnd, GW_OWNER):
                return True

            title_buf = ctypes.create_unicode_buffer(512)
            user32.GetWindowTextW(hwnd, title_buf, 512)
            title = title_buf.value
            if not title:
                return True

            cls_buf = ctypes.create_unicode_buffer(256)
            user32.GetClassNameW(hwnd, cls_buf, 256)

            rect = wt.RECT()
            user32.GetWindowRect(hwnd, ctypes.byref(rect))

            # 获取进程名
            pid = wt.DWORD()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            proc_name = self._get_process_name(pid.value)

            windows.append({
                "hwnd": hwnd,
                "title": title,
                "class_name": cls_buf.value,
                "process": proc_name,
                "pid": pid.value,
                "rect": {
                    "left": rect.left, "top": rect.top,
                    "right": rect.right, "bottom": rect.bottom,
                },
            })
            return True

        user32.EnumWindows(_enum_cb, 0)
        return windows

    async def list_windows(self) -> list[dict[str, Any]]:
        return await asyncio.to_thread(self.list_windows_sync)

    def focus_window_sync(self, title_or_hwnd: str | int) -> bool:
        """聚焦窗口。支持标题关键词（模糊匹配）或 hwnd。"""
        if isinstance(title_or_hwnd, int):
            hwnd = title_or_hwnd
        else:
            hwnd = self._find_window_by_title(title_or_hwnd)
            if not hwnd:
                return False

        user32.ShowWindow(hwnd, SW_RESTORE)
        user32.SetForegroundWindow(hwnd)
        logger.debug(f"聚焦窗口: {title_or_hwnd}")
        return True

    async def focus_window(self, title_or_hwnd: str | int) -> bool:
        return await asyncio.to_thread(self.focus_window_sync, title_or_hwnd)

    def get_foreground_window_sync(self) -> dict[str, Any]:
        hwnd = user32.GetForegroundWindow()
        title_buf = ctypes.create_unicode_buffer(512)
        user32.GetWindowTextW(hwnd, title_buf, 512)
        return {"hwnd": hwnd, "title": title_buf.value}

    def _find_window_by_title(self, keyword: str) -> int | None:
        keyword_lower = keyword.lower()
        for w in self.list_windows_sync():
            if keyword_lower in w["title"].lower():
                return w["hwnd"]
        return None

    def _get_process_name(self, pid: int) -> str:
        if pid == 0:
            return ""
        handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            return ""
        try:
            buf = ctypes.create_unicode_buffer(1024)
            size = wt.DWORD(1024)
            if kernel32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
                path = buf.value
                return path.rsplit("\\", 1)[-1] if "\\" in path else path
            return ""
        finally:
            kernel32.CloseHandle(handle)

    # ------------------------------------------------------------------
    # 剪贴板
    # ------------------------------------------------------------------

    def clipboard_read_sync(self) -> str:
        """读取剪贴板文本。"""
        user32.OpenClipboard(0)
        try:
            handle = user32.GetClipboardData(CF_UNICODETEXT)
            if not handle:
                return ""
            ptr = kernel32.GlobalLock(handle)
            if not ptr:
                return ""
            try:
                return ctypes.wstring_at(ptr)
            finally:
                kernel32.GlobalUnlock(handle)
        finally:
            user32.CloseClipboard()

    async def clipboard_read(self) -> str:
        return await asyncio.to_thread(self.clipboard_read_sync)

    def clipboard_write_sync(self, text: str) -> None:
        """写入剪贴板文本。"""
        data = text.encode("utf-16-le") + b"\x00\x00"
        h_mem = kernel32.GlobalAlloc(GMEM_MOVEABLE, len(data))
        ptr = kernel32.GlobalLock(h_mem)
        ctypes.memmove(ptr, data, len(data))
        kernel32.GlobalUnlock(h_mem)

        user32.OpenClipboard(0)
        try:
            user32.EmptyClipboard()
            user32.SetClipboardData(CF_UNICODETEXT, h_mem)
        finally:
            user32.CloseClipboard()
        logger.debug(f"写入剪贴板: {text[:50]}")

    async def clipboard_write(self, text: str) -> None:
        await asyncio.to_thread(self.clipboard_write_sync, text)

    # ------------------------------------------------------------------
    # 通知检测（窗口标题变化监控）
    # ------------------------------------------------------------------

    def get_notifications_sync(self) -> list[dict[str, Any]]:
        """检测窗口标题变化，返回通知格式的字典列表。"""
        current_titles: dict[int, str] = {}
        notifications: list[dict[str, Any]] = []

        windows = self.list_windows_sync()
        for w in windows:
            hwnd = w["hwnd"]
            title = w["title"]
            current_titles[hwnd] = title

            prev_title = self._prev_titles.get(hwnd)
            if prev_title is not None and prev_title != title:
                notifications.append({
                    "pkg": w.get("process", ""),
                    "title": title,
                    "text": "",
                    "when": int(time.time() * 1000),
                })

        self._prev_titles = current_titles
        return notifications

    async def get_notifications(self) -> list[dict[str, Any]]:
        return await asyncio.to_thread(self.get_notifications_sync)
