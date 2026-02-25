"""tools/browser.py — 浏览器原子操作工具集（基于 browser-use Browser API）。

提供一整套浏览器元操作，让 AI 自己看截图、做决策，
类似手机工具的模式（截图 → 判断 → 操作）。

所有重依赖（browser-use, playwright）均为懒导入。
"""

from __future__ import annotations

import asyncio
import base64
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import quote

from loguru import logger

if TYPE_CHECKING:
    from kaguya.config import BrowserConfig


# ---------------------------------------------------------------------------
# 工具 Schema 定义
# ---------------------------------------------------------------------------

BROWSER_TOOLS: list[dict] = [
    {
        "type": "function",
        "function": {
            "name": "browser_open",
            "description": "打开指定的 URL 网页，返回页面标题。",
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {"type": "string", "description": "要打开的网址"},
                },
                "required": ["url"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "browser_search",
            "description": "使用搜索引擎搜索关键词，返回搜索结果页面。",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "搜索关键词"},
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "browser_click",
            "description": "点击页面上匹配 CSS 选择器的元素。",
            "parameters": {
                "type": "object",
                "properties": {
                    "selector": {
                        "type": "string",
                        "description": "CSS 选择器，如 'button.submit'、'a[href]'、'#login-btn'",
                    },
                },
                "required": ["selector"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "browser_type",
            "description": "在匹配 CSS 选择器的输入框中输入文本。",
            "parameters": {
                "type": "object",
                "properties": {
                    "selector": {"type": "string", "description": "输入框的 CSS 选择器"},
                    "text": {"type": "string", "description": "要输入的文本"},
                },
                "required": ["selector", "text"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "browser_scroll",
            "description": "滚动当前页面。",
            "parameters": {
                "type": "object",
                "properties": {
                    "direction": {
                        "type": "string",
                        "enum": ["up", "down"],
                        "description": "滚动方向",
                    },
                    "pixels": {
                        "type": "integer",
                        "description": "滚动像素数（默认 500）",
                        "default": 500,
                    },
                },
                "required": ["direction"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "browser_screenshot",
            "description": "截取当前浏览器页面的截图。截图会直接返回给你查看。",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "browser_get_text",
            "description": "获取当前页面的文本内容（截取前 3000 字符）。",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "browser_back",
            "description": "浏览器后退到上一页。",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "browser_keys",
            "description": "发送键盘按键（如 Enter、Tab、Escape、ArrowDown 等）。",
            "parameters": {
                "type": "object",
                "properties": {
                    "keys": {"type": "string", "description": "按键名称"},
                },
                "required": ["keys"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "browser_close",
            "description": "关闭浏览器。用完浏览器后记得关闭以释放资源。",
            "parameters": {"type": "object", "properties": {}},
        },
    },
]

# 工具名称集合，用于快速查找
_TOOL_NAMES = {t["function"]["name"] for t in BROWSER_TOOLS}


# ---------------------------------------------------------------------------
# 工具执行器
# ---------------------------------------------------------------------------


class BrowserToolExecutor:
    """浏览器原子操作执行器。

    惰性初始化：首次调用时才导入 browser-use 并创建 Browser 实例。
    Browser 实例在工具调用之间保持存活，直到 browser_close 或 close()。
    """

    def __init__(self, browser_config: "BrowserConfig", screenshot_dir: Path):
        self.browser_config = browser_config
        self.screenshot_dir = screenshot_dir
        self.screenshot_dir.mkdir(parents=True, exist_ok=True)
        self._browser = None  # browser_use.Browser (lazy)
        self._page = None     # browser_use Page

    async def execute(self, tool_name: str, args: dict[str, Any]) -> dict[str, Any]:
        if tool_name not in _TOOL_NAMES:
            return {"error": f"未知工具: {tool_name}"}
        if not self.browser_config.enabled:
            return {"error": "浏览器功能未启用，请在配置中设置 [browser] enabled = true"}
        try:
            handler = getattr(self, f"_do_{tool_name.removeprefix('browser_')}")
            return await handler(args)
        except Exception as e:
            logger.error(f"浏览器工具 [{tool_name}] 执行失败: {e}")
            return {"error": str(e)}

    # ------------------------------------------------------------------
    # 浏览器生命周期
    # ------------------------------------------------------------------

    async def _ensure_browser(self):
        """惰性创建 browser-use Browser 实例。"""
        if self._browser is not None:
            return

        from browser_use import Browser

        mode = self.browser_config.mode
        kwargs: dict[str, Any] = {}

        if mode == "cdp" and self.browser_config.cdp_url:
            kwargs["cdp_url"] = self.browser_config.cdp_url
        elif mode == "local":
            kwargs["headless"] = self.browser_config.headless
            if self.browser_config.browser_path:
                kwargs["executable_path"] = self.browser_config.browser_path
        elif mode == "cloud":
            if self.browser_config.cloud_api_key:
                import os
                os.environ["BROWSER_USE_API_KEY"] = self.browser_config.cloud_api_key
                kwargs["use_cloud"] = True
            elif self.browser_config.cdp_url:
                kwargs["cdp_url"] = self.browser_config.cdp_url

        self._browser = Browser(**kwargs)
        await self._browser.start()
        logger.info(f"浏览器已启动 (模式: {mode})")

    async def _ensure_page(self):
        """确保有一个活跃的页面。"""
        await self._ensure_browser()
        if self._page is None:
            self._page = await self._browser.new_page("about:blank")
        return self._page

    async def _get_current_page(self):
        """获取当前页面。"""
        await self._ensure_browser()
        try:
            page = await self._browser.get_current_page()
            if page:
                self._page = page
                return page
        except Exception:
            pass
        return await self._ensure_page()

    async def close(self) -> None:
        """关闭浏览器实例。"""
        if self._browser is not None:
            try:
                await self._browser.stop()
            except Exception as e:
                logger.warning(f"关闭浏览器时出错: {e}")
            self._browser = None
            self._page = None

    # ------------------------------------------------------------------
    # 各工具实现
    # ------------------------------------------------------------------

    async def _do_open(self, args: dict) -> dict[str, Any]:
        url = args["url"]
        page = await self._ensure_page()
        await page.goto(url)

        # 刷新 page 引用（cloud 模式下 goto 后可能切换了内部 tab）
        try:
            fresh = await self._browser.get_current_page()
            if fresh:
                self._page = fresh
                page = fresh
        except Exception:
            pass

        # 等待页面加载
        for _ in range(30):
            try:
                current_url = await page.get_url()
                if current_url and current_url != "about:blank":
                    break
            except Exception:
                pass
            await asyncio.sleep(0.5)

        await asyncio.sleep(2)
        title = await page.get_title()
        current_url = await page.get_url()
        return {"message": f"已打开页面: {title}", "url": current_url}

    async def _do_search(self, args: dict) -> dict[str, Any]:
        query = args["query"]
        url = f"https://www.google.com/search?q={quote(query)}"
        page = await self._ensure_page()
        await page.goto(url)
        await asyncio.sleep(2)

        title = await page.get_title()
        try:
            text = await page.evaluate(
                "() => document.body.innerText.substring(0, 3000)"
            )
            return {"message": f"搜索结果: {title}", "text": text[:2000]}
        except Exception:
            return {"message": f"搜索结果: {title}"}

    async def _do_click(self, args: dict) -> dict[str, Any]:
        selector = args["selector"]
        page = await self._get_current_page()
        elements = await page.get_elements_by_css_selector(selector)
        if not elements:
            return {"error": f"未找到匹配 '{selector}' 的元素"}
        await elements[0].click()
        await asyncio.sleep(1)
        return {"message": f"已点击元素: {selector}"}

    async def _do_type(self, args: dict) -> dict[str, Any]:
        selector = args["selector"]
        text = args["text"]
        page = await self._get_current_page()
        elements = await page.get_elements_by_css_selector(selector)
        if not elements:
            return {"error": f"未找到匹配 '{selector}' 的输入框"}
        await elements[0].fill(text)
        return {"message": f"已在 '{selector}' 中输入: {text}"}

    async def _do_scroll(self, args: dict) -> dict[str, Any]:
        direction = args.get("direction", "down")
        pixels = args.get("pixels", 500)
        page = await self._get_current_page()
        y = pixels if direction == "down" else -pixels
        await page.evaluate(f"() => window.scrollBy(0, {y})")
        label = "下" if direction == "down" else "上"
        return {"message": f"已向{label}滚动 {pixels} 像素"}

    async def _do_screenshot(self, _args: dict) -> dict[str, Any]:
        page = await self._get_current_page()

        try:
            current_url = await page.get_url()
            if current_url and current_url != "about:blank":
                await asyncio.sleep(1)
        except Exception:
            pass

        screenshot_data = await page.screenshot(format="png")

        if isinstance(screenshot_data, bytes):
            raw_bytes = screenshot_data
            b64_str = base64.b64encode(raw_bytes).decode()
        else:
            b64_str = screenshot_data
            raw_bytes = base64.b64decode(b64_str)

        # 保存到 workspace（AI 后续可通过 phone_push_file 发到手机）
        filename = f"browser_{int(time.time())}.png"
        filepath = self.screenshot_dir / filename
        filepath.write_bytes(raw_bytes)

        title = ""
        try:
            title = await page.get_title()
        except Exception:
            pass

        msg = f"截图已保存: screenshots/{filename}"
        if title:
            msg = f"当前页面: {title}\n{msg}"

        return {
            "message": msg,
            "image_base64": b64_str,
            "image_media_type": "image/png",
        }

    async def _do_get_text(self, _args: dict) -> dict[str, Any]:
        page = await self._get_current_page()
        text = await page.evaluate(
            "() => document.body.innerText.substring(0, 5000)"
        )
        title = await page.get_title()
        url = await page.get_url()
        return {"message": f"页面: {title}\nURL: {url}", "text": text[:3000]}

    async def _do_back(self, _args: dict) -> dict[str, Any]:
        page = await self._get_current_page()
        await page.go_back()
        await asyncio.sleep(1)
        title = await page.get_title()
        return {"message": f"已后退到: {title}"}

    async def _do_keys(self, args: dict) -> dict[str, Any]:
        keys = args["keys"]
        page = await self._get_current_page()
        await page.press(keys)
        return {"message": f"已发送按键: {keys}"}

    async def _do_close(self, _args: dict) -> dict[str, Any]:
        await self.close()
        return {"message": "浏览器已关闭"}
