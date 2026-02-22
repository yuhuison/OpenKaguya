"""
浏览器工具集 — 基于 browser-use。

将 browser-use 封装为辉夜姬的 Function Calling 工具，
支持本地 Chrome / Browser-Use Cloud / CDP 三种模式。
"""

from __future__ import annotations

import time
from pathlib import Path
from urllib.parse import quote

from loguru import logger

from kaguya.tools.registry import Tool


class BrowserToolkit:
    """
    将 browser-use 封装为辉夜姬的工具集。

    需要延迟导入 browser_use，因为它可能未安装或不需要。
    """

    def __init__(
        self,
        mode: str = "local",
        chrome_path: str = "",
        cdp_url: str = "",
        headless: bool = True,
        screenshot_dir: Path | None = None,
    ):
        self.mode = mode
        self.chrome_path = chrome_path
        self.cdp_url = cdp_url
        self.headless = headless
        self.screenshot_dir = screenshot_dir or Path("data/workspaces/kaguya/screenshots")
        self.screenshot_dir.mkdir(parents=True, exist_ok=True)

        self._browser = None
        self._page = None

    async def _ensure_browser(self):
        """延迟初始化浏览器"""
        if self._browser is not None:
            return

        from browser_use import Browser

        kwargs = {}
        if self.mode == "cdp" and self.cdp_url:
            kwargs["cdp_url"] = self.cdp_url
        elif self.mode == "local":
            kwargs["headless"] = self.headless
            if self.chrome_path:
                kwargs["executable_path"] = self.chrome_path

        self._browser = Browser(**kwargs)
        await self._browser.start()
        logger.info(f"浏览器已启动 (模式: {self.mode})")

    async def _ensure_page(self):
        """确保有一个活跃的页面"""
        await self._ensure_browser()
        if self._page is None:
            self._page = await self._browser.new_page("about:blank")
        return self._page

    async def _get_current_page(self):
        """获取当前页面"""
        await self._ensure_browser()
        try:
            page = await self._browser.get_current_page()
            if page:
                self._page = page
                return page
        except Exception:
            pass
        return await self._ensure_page()

    async def close(self):
        """关闭浏览器"""
        if self._browser:
            try:
                await self._browser.stop()
            except Exception:
                pass
            self._browser = None
            self._page = None

    def get_tools(self) -> list[Tool]:
        """获取所有浏览器工具"""
        return [
            BrowserOpenTool(self),
            BrowserSearchTool(self),
            BrowserClickTool(self),
            BrowserTypeTool(self),
            BrowserScrollTool(self),
            BrowserScreenshotTool(self),
            BrowserGetTextTool(self),
            BrowserBackTool(self),
            BrowserKeysTool(self),
            BrowserCloseTool(self),
        ]


# ========================= 浏览器工具实现 =========================


class BrowserOpenTool(Tool):
    def __init__(self, toolkit: BrowserToolkit):
        self._tk = toolkit

    @property
    def name(self): return "browser_open"

    @property
    def description(self): return "打开指定的 URL 网页，返回页面标题。"

    @property
    def parameters(self):
        return {
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "要打开的网址"},
            },
            "required": ["url"],
        }

    async def execute(self, url: str, **_) -> str:
        try:
            page = await self._tk._ensure_page()
            await page.goto(url)
            title = await page.get_title()
            current_url = await page.get_url()
            return f"已打开页面: {title}\nURL: {current_url}"
        except Exception as e:
            return f"打开网页失败: {e}"


class BrowserSearchTool(Tool):
    def __init__(self, toolkit: BrowserToolkit):
        self._tk = toolkit

    @property
    def name(self): return "browser_search"

    @property
    def description(self): return "使用搜索引擎搜索关键词，返回搜索结果页面标题。"

    @property
    def parameters(self):
        return {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "搜索关键词"},
            },
            "required": ["query"],
        }

    async def execute(self, query: str, **_) -> str:
        try:
            url = f"https://www.google.com/search?q={quote(query)}"
            page = await self._tk._ensure_page()
            await page.goto(url)
            title = await page.get_title()
            # 尝试提取文本摘要
            try:
                text = await page.evaluate(
                    "() => document.body.innerText.substring(0, 3000)"
                )
                return f"搜索结果: {title}\n\n{text[:2000]}"
            except Exception:
                return f"搜索结果: {title}"
        except Exception as e:
            return f"搜索失败: {e}"


class BrowserClickTool(Tool):
    def __init__(self, toolkit: BrowserToolkit):
        self._tk = toolkit

    @property
    def name(self): return "browser_click"

    @property
    def description(self): return "点击页面上匹配 CSS 选择器的元素。"

    @property
    def parameters(self):
        return {
            "type": "object",
            "properties": {
                "selector": {"type": "string", "description": "CSS 选择器，如 'button.submit' 或 'a[href]'"},
            },
            "required": ["selector"],
        }

    async def execute(self, selector: str, **_) -> str:
        try:
            page = await self._tk._get_current_page()
            elements = await page.get_elements_by_css_selector(selector)
            if not elements:
                return f"未找到匹配 '{selector}' 的元素"
            await elements[0].click()
            return f"已点击元素: {selector}"
        except Exception as e:
            return f"点击失败: {e}"


class BrowserTypeTool(Tool):
    def __init__(self, toolkit: BrowserToolkit):
        self._tk = toolkit

    @property
    def name(self): return "browser_type"

    @property
    def description(self): return "在匹配 CSS 选择器的输入框中输入文本。"

    @property
    def parameters(self):
        return {
            "type": "object",
            "properties": {
                "selector": {"type": "string", "description": "CSS 选择器"},
                "text": {"type": "string", "description": "要输入的文本"},
            },
            "required": ["selector", "text"],
        }

    async def execute(self, selector: str, text: str, **_) -> str:
        try:
            page = await self._tk._get_current_page()
            elements = await page.get_elements_by_css_selector(selector)
            if not elements:
                return f"未找到匹配 '{selector}' 的输入框"
            await elements[0].fill(text)
            return f"已在 '{selector}' 中输入: {text}"
        except Exception as e:
            return f"输入失败: {e}"


class BrowserScrollTool(Tool):
    def __init__(self, toolkit: BrowserToolkit):
        self._tk = toolkit

    @property
    def name(self): return "browser_scroll"

    @property
    def description(self): return "滚动当前页面。"

    @property
    def parameters(self):
        return {
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
                },
            },
            "required": ["direction"],
        }

    async def execute(self, direction: str = "down", pixels: int = 500, **_) -> str:
        try:
            page = await self._tk._get_current_page()
            y = pixels if direction == "down" else -pixels
            await page.evaluate(f"() => window.scrollBy(0, {y})")
            return f"已向{'下' if direction == 'down' else '上'}滚动 {pixels} 像素"
        except Exception as e:
            return f"滚动失败: {e}"


class BrowserScreenshotTool(Tool):
    def __init__(self, toolkit: BrowserToolkit):
        self._tk = toolkit

    @property
    def name(self): return "browser_screenshot"

    @property
    def description(self): return "截取当前页面的截图并保存。"

    @property
    def parameters(self):
        return {"type": "object", "properties": {}}

    async def execute(self, **_) -> str:
        try:
            page = await self._tk._get_current_page()
            filename = f"screenshot_{int(time.time())}.png"
            filepath = self._tk.screenshot_dir / filename
            screenshot_data = await page.screenshot(format="png")
            if isinstance(screenshot_data, bytes):
                filepath.write_bytes(screenshot_data)
            else:
                # base64 编码
                import base64
                filepath.write_bytes(base64.b64decode(screenshot_data))
            return f"截图已保存: {filepath}"
        except Exception as e:
            return f"截图失败: {e}"


class BrowserGetTextTool(Tool):
    def __init__(self, toolkit: BrowserToolkit):
        self._tk = toolkit

    @property
    def name(self): return "browser_get_text"

    @property
    def description(self): return "获取当前页面的文本内容（截取前 3000 字符避免过长）。"

    @property
    def parameters(self):
        return {"type": "object", "properties": {}}

    async def execute(self, **_) -> str:
        try:
            page = await self._tk._get_current_page()
            text = await page.evaluate(
                "() => document.body.innerText.substring(0, 5000)"
            )
            title = await page.get_title()
            url = await page.get_url()
            return f"页面: {title}\nURL: {url}\n\n{text[:3000]}"
        except Exception as e:
            return f"获取页面文本失败: {e}"


class BrowserBackTool(Tool):
    def __init__(self, toolkit: BrowserToolkit):
        self._tk = toolkit

    @property
    def name(self): return "browser_back"

    @property
    def description(self): return "浏览器后退到上一页。"

    @property
    def parameters(self):
        return {"type": "object", "properties": {}}

    async def execute(self, **_) -> str:
        try:
            page = await self._tk._get_current_page()
            await page.go_back()
            title = await page.get_title()
            return f"已后退到: {title}"
        except Exception as e:
            return f"后退失败: {e}"


class BrowserKeysTool(Tool):
    def __init__(self, toolkit: BrowserToolkit):
        self._tk = toolkit

    @property
    def name(self): return "browser_keys"

    @property
    def description(self): return "发送键盘按键（如 Enter, Tab, Escape, ArrowDown 等）。"

    @property
    def parameters(self):
        return {
            "type": "object",
            "properties": {
                "keys": {"type": "string", "description": "按键名称"},
            },
            "required": ["keys"],
        }

    async def execute(self, keys: str, **_) -> str:
        try:
            page = await self._tk._get_current_page()
            await page.press(keys)
            return f"已发送按键: {keys}"
        except Exception as e:
            return f"按键失败: {e}"


class BrowserCloseTool(Tool):
    def __init__(self, toolkit: BrowserToolkit):
        self._tk = toolkit

    @property
    def name(self): return "browser_close"

    @property
    def description(self): return "关闭浏览器。用完浏览器后记得关闭。"

    @property
    def parameters(self):
        return {"type": "object", "properties": {}}

    async def execute(self, **_) -> str:
        try:
            await self._tk.close()
            return "浏览器已关闭"
        except Exception as e:
            return f"关闭失败: {e}"
