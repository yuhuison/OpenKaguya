"""PhoneController — ADB 操作封装。

所有 ADB 操作均通过 asyncio.to_thread 包装同步 subprocess 调用。
"""

from __future__ import annotations

import asyncio
import io
import re
import subprocess
import tempfile
from pathlib import Path
from typing import Optional

from loguru import logger
from PIL import Image

from kaguya.config import PhoneConfig


class PhoneController:
    """通过 ADB 控制 Android 手机。"""

    def __init__(self, config: PhoneConfig):
        self.config = config
        self._prefix = [config.adb_path]
        if config.device_serial:
            self._prefix += ["-s", config.device_serial]

    # ------------------------------------------------------------------
    # 内部辅助
    # ------------------------------------------------------------------

    def _run_sync(self, args: list[str], input_data: Optional[bytes] = None) -> str:
        """同步执行 ADB 命令，返回 stdout 字符串。"""
        cmd = self._prefix + args
        logger.debug(f"ADB: {' '.join(cmd)}")
        result = subprocess.run(
            cmd,
            capture_output=True,
            input=input_data,
            timeout=30,
        )
        if result.returncode != 0:
            err = result.stderr.decode("utf-8", errors="replace").strip()
            raise RuntimeError(f"ADB 命令失败 ({result.returncode}): {err}")
        return result.stdout.decode("utf-8", errors="replace")

    async def _run(self, args: list[str], input_data: Optional[bytes] = None) -> str:
        """异步执行 ADB 命令。"""
        return await asyncio.to_thread(self._run_sync, args, input_data)

    async def _run_bytes(self, args: list[str]) -> bytes:
        """异步执行 ADB 命令，返回原始字节。"""
        cmd = self._prefix + args

        def _run_sync_bytes():
            result = subprocess.run(cmd, capture_output=True, timeout=30)
            if result.returncode != 0:
                err = result.stderr.decode("utf-8", errors="replace").strip()
                raise RuntimeError(f"ADB 命令失败 ({result.returncode}): {err}")
            return result.stdout

        return await asyncio.to_thread(_run_sync_bytes)

    # ------------------------------------------------------------------
    # 屏幕操作
    # ------------------------------------------------------------------

    async def screenshot(self) -> Image.Image:
        """截取当前屏幕，返回 PIL Image。"""
        raw = await self._run_bytes(["exec-out", "screencap", "-p"])
        return Image.open(io.BytesIO(raw))

    async def dump_ui(self) -> str:
        """通过 uiautomator dump 获取 UI XML。"""
        await self._run(["shell", "uiautomator", "dump", "/sdcard/ui_dump.xml"])
        xml_bytes = await self._run_bytes(["exec-out", "cat", "/sdcard/ui_dump.xml"])
        return xml_bytes.decode("utf-8", errors="replace")

    # ------------------------------------------------------------------
    # 屏幕解锁
    # ------------------------------------------------------------------

    async def wake_and_unlock(self) -> str:
        """唤醒屏幕并上滑解锁（适用于无密码锁屏）。"""
        # 按电源键唤醒屏幕
        await self._run(["shell", "input", "keyevent", "KEYCODE_WAKEUP"])
        # 等待屏幕点亮
        await asyncio.sleep(0.5)
        # 从屏幕底部上滑解锁
        w, h = await self.get_screen_size()
        x_center = w // 2
        await self._run([
            "shell", "input", "swipe",
            str(x_center), str(int(h * 0.8)),
            str(x_center), str(int(h * 0.2)),
            "300",
        ])
        logger.info("屏幕已唤醒并上滑解锁")
        return "已唤醒屏幕并上滑解锁"

    # ------------------------------------------------------------------
    # 触摸操作
    # ------------------------------------------------------------------

    async def tap(self, x: int, y: int) -> None:
        """点击屏幕坐标 (x, y)。"""
        await self._run(["shell", "input", "tap", str(x), str(y)])
        logger.debug(f"点击: ({x}, {y})")

    async def long_press(self, x: int, y: int, duration_ms: int = 1000) -> None:
        """长按屏幕坐标 (x, y)。"""
        x2, y2 = x, y
        await self._run([
            "shell", "input", "swipe",
            str(x), str(y), str(x2), str(y2), str(duration_ms),
        ])

    async def swipe_between(
        self, x1: int, y1: int, x2: int, y2: int, duration_ms: int = 300,
    ) -> None:
        """从 (x1,y1) 滑动到 (x2,y2)。"""
        await self._run([
            "shell", "input", "swipe",
            str(x1), str(y1), str(x2), str(y2), str(duration_ms),
        ])

    async def swipe(self, direction: str, duration_ms: int = 300) -> None:
        """滑动屏幕。direction: up / down / left / right。自动适配屏幕分辨率。"""
        w, h = await self.get_screen_size()
        cx, cy = w // 2, h // 2
        swipe_map = {
            "up":    (cx, int(h * 0.7), cx, int(h * 0.3)),
            "down":  (cx, int(h * 0.3), cx, int(h * 0.7)),
            "left":  (int(w * 0.8), cy, int(w * 0.2), cy),
            "right": (int(w * 0.2), cy, int(w * 0.8), cy),
        }
        coords = swipe_map.get(direction.lower())
        if not coords:
            raise ValueError(f"无效方向: {direction}，有效值: up/down/left/right")
        x1, y1, x2, y2 = coords
        await self.swipe_between(x1, y1, x2, y2, duration_ms)

    # ------------------------------------------------------------------
    # 键盘输入
    # ------------------------------------------------------------------

    async def type_text(self, text: str) -> None:
        """在当前焦点处输入文字。通过 ADBKeyboard 广播输入，支持中文。"""
        await self._run([
            "shell", "am", "broadcast",
            "-a", "ADB_INPUT_TEXT",
            "--es", "msg", text,
        ])

    async def press_key(self, key: str) -> None:
        """按下按键。key: BACK / HOME / ENTER / VOLUME_UP / VOLUME_DOWN 等。"""
        keycode_map = {
            "back": "KEYCODE_BACK",
            "home": "KEYCODE_HOME",
            "enter": "KEYCODE_ENTER",
            "volume_up": "KEYCODE_VOLUME_UP",
            "volume_down": "KEYCODE_VOLUME_DOWN",
            "app_switch": "KEYCODE_APP_SWITCH",
        }
        keycode = keycode_map.get(key.lower(), key.upper())
        await self._run(["shell", "input", "keyevent", keycode])

    # ------------------------------------------------------------------
    # App 管理
    # ------------------------------------------------------------------

    async def open_app(self, package_or_name: str) -> None:
        """打开 App。支持包名或 App 名称（模糊匹配）。"""
        # 常见 App 包名映射
        known_apps = {
            "微信": "com.tencent.mm",
            "wechat": "com.tencent.mm",
            "qq": "com.tencent.mobileqq",
            "微博": "com.sina.weibo",
            "淘宝": "com.taobao.taobao",
            "京东": "com.jingdong.app.mall",
            "支付宝": "com.eg.android.AlipayGphone",
            "抖音": "com.ss.android.ugc.aweme",
            "哔哩哔哩": "tv.danmaku.bili",
            "bilibili": "tv.danmaku.bili",
            "chrome": "com.android.chrome",
            "浏览器": "com.android.browser",
            "相机": "com.android.camera2",
            "短信": "com.android.mms",
            "电话": "com.android.dialer",
            "设置": "com.android.settings",
        }

        pkg = known_apps.get(package_or_name.lower(), package_or_name)
        try:
            await self._run([
                "shell", "monkey",
                "-p", pkg,
                "-c", "android.intent.category.LAUNCHER",
                "1",
            ])
            logger.info(f"打开 App: {pkg}")
        except Exception as e:
            logger.warning(f"monkey 启动失败，尝试 am start: {e}")
            await self._run(["shell", "am", "start", "-n", f"{pkg}/.MainActivity"])

    async def get_installed_packages(self) -> list[str]:
        """获取已安装的第三方包名列表。"""
        output = await self._run(["shell", "pm", "list", "packages", "-3"])
        return [line.replace("package:", "").strip() for line in output.splitlines() if line.strip()]

    # ------------------------------------------------------------------
    # 通知
    # ------------------------------------------------------------------

    async def get_notifications(self) -> list[dict]:
        """获取当前通知列表，解析为结构化数据。"""
        try:
            output = await self._run(["shell", "dumpsys", "notification", "--noredact"])
        except Exception as e:
            logger.warning(f"获取通知失败: {e}")
            return []

        notifications = []
        current: dict = {}

        for line in output.splitlines():
            line = line.strip()
            # 匹配通知条目起始
            if "NotificationRecord" in line or "StatusBarNotification" in line:
                if current:
                    notifications.append(current)
                current = {}
            # 提取 package
            pkg_match = re.search(r"pkg=(\S+)", line)
            if pkg_match and "pkg" not in current:
                current["pkg"] = pkg_match.group(1)
            # 提取 title/text
            if "android.title=" in line:
                current["title"] = line.split("android.title=", 1)[-1].strip()
            if "android.text=" in line:
                current["text"] = line.split("android.text=", 1)[-1].strip()
            # 提取时间
            time_match = re.search(r"when=(\d+)", line)
            if time_match and "when" not in current:
                current["when"] = int(time_match.group(1))

        if current:
            notifications.append(current)

        return [n for n in notifications if n.get("pkg") or n.get("title")]

    # ------------------------------------------------------------------
    # 通知栏
    # ------------------------------------------------------------------

    async def expand_notification_shade(self) -> None:
        """展开通知栏。"""
        await self._run(["shell", "cmd", "statusbar", "expand-notifications"])
        logger.debug("展开通知栏")

    async def collapse_notification_shade(self) -> None:
        """收起通知栏。"""
        await self._run(["shell", "cmd", "statusbar", "collapse"])
        logger.debug("收起通知栏")

    # ------------------------------------------------------------------
    # 文件传输
    # ------------------------------------------------------------------

    async def push_file(self, local: str, remote: str) -> None:
        await self._run(["push", local, remote])

    async def pull_file(self, remote: str, local: str) -> None:
        await self._run(["pull", remote, local])

    async def list_dir(self, remote_path: str) -> list[str]:
        """列出手机上指定目录的内容。"""
        output = await self._run(["shell", "ls", "-la", remote_path])
        return [line.strip() for line in output.splitlines() if line.strip()]

    # ------------------------------------------------------------------
    # 设备信息
    # ------------------------------------------------------------------

    async def get_screen_size(self) -> tuple[int, int]:
        """获取屏幕分辨率，返回 (width, height)。"""
        output = await self._run(["shell", "wm", "size"])
        match = re.search(r"(\d+)x(\d+)", output)
        if match:
            return int(match.group(1)), int(match.group(2))
        return 1080, 1920  # 默认值

    async def check_connected(self) -> bool:
        """检查设备是否已连接。"""
        try:
            output = await self._run(["devices"])
            lines = [l for l in output.splitlines() if l.strip() and "List of devices" not in l]
            return any("device" in l and "offline" not in l for l in lines)
        except Exception:
            return False
