"""tools/browser.py — 浏览器原子操作工具集（基于 Playwright + Stealth）。

提供一整套浏览器元操作，让 AI 自己看截图、做决策，
类似桌面工具的模式（截图 → 判断 → 操作）。

反检测策略：
  1. 优先使用系统 Chrome（channel="chrome"）
  2. 注入 stealth JS 抹除自动化指纹
  3. 反自动化启动参数

所有重依赖（playwright）均为懒导入。
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
# Stealth JS — 注入到每个页面，抹除自动化指纹
# ---------------------------------------------------------------------------

_STEALTH_JS = """\
// --- navigator.webdriver ---
Object.defineProperty(navigator, 'webdriver', { get: () => undefined });

// --- chrome.runtime ---
window.chrome = window.chrome || {};
window.chrome.runtime = { id: undefined };

// --- navigator.plugins（模拟真实 Chrome 插件列表）---
Object.defineProperty(navigator, 'plugins', {
    get: () => {
        const plugins = [
            { name: 'Chrome PDF Plugin', filename: 'internal-pdf-viewer',
              description: 'Portable Document Format' },
            { name: 'Chrome PDF Viewer', filename: 'mhjfbmdgcfjbbpaeojofohoefgiehjai',
              description: '' },
            { name: 'Native Client', filename: 'internal-nacl-plugin',
              description: '' },
        ];
        plugins.length = 3;
        return plugins;
    }
});

// --- navigator.languages ---
Object.defineProperty(navigator, 'languages', {
    get: () => ['zh-CN', 'zh', 'en-US', 'en']
});

// --- permissions.query ---
const originalQuery = window.navigator.permissions.query;
window.navigator.permissions.query = (params) =>
    params.name === 'notifications'
        ? Promise.resolve({ state: Notification.permission })
        : originalQuery(params);

// --- WebGL 渲染器伪装 ---
const getParameter = WebGLRenderingContext.prototype.getParameter;
WebGLRenderingContext.prototype.getParameter = function(param) {
    if (param === 37445) return 'Intel Inc.';          // UNMASKED_VENDOR_WEBGL
    if (param === 37446) return 'Intel Iris OpenGL Engine';  // UNMASKED_RENDERER_WEBGL
    return getParameter.call(this, param);
};

// --- iframe contentWindow ---
try {
    const elementDescriptor = Object.getOwnPropertyDescriptor(
        HTMLIFrameElement.prototype, 'contentWindow'
    );
    if (elementDescriptor) {
        Object.defineProperty(HTMLIFrameElement.prototype, 'contentWindow', {
            get: function() {
                const win = elementDescriptor.get.call(this);
                if (win) {
                    try {
                        Object.defineProperty(win.navigator, 'webdriver', {
                            get: () => undefined
                        });
                    } catch(e) {}
                }
                return win;
            }
        });
    }
} catch(e) {}
"""

# 反自动化 Chromium 启动参数
_STEALTH_ARGS = [
    "--disable-blink-features=AutomationControlled",
    "--disable-infobars",
    "--no-first-run",
    "--no-default-browser-check",
    "--disable-component-update",
]


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
                        "description": (
                            "CSS 选择器，如 'button.submit'、'a[href]'、'#login-btn'"
                        ),
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
                    "selector": {
                        "type": "string",
                        "description": "输入框的 CSS 选择器",
                    },
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
    """浏览器原子操作执行器（Playwright + Stealth）。

    惰性初始化：首次调用时才导入 playwright 并启动浏览器。
    反检测策略：系统 Chrome + stealth JS + 反自动化启动参数。
    """

    def __init__(self, browser_config: "BrowserConfig", screenshot_dir: Path):
        self.browser_config = browser_config
        self.screenshot_dir = screenshot_dir
        self.screenshot_dir.mkdir(parents=True, exist_ok=True)
        self._playwright = None   # playwright 实例
        self._browser = None      # Playwright Browser
        self._context = None      # BrowserContext（注入 stealth）
        self._page = None         # 当前 Page

    async def execute(self, tool_name: str, args: dict[str, Any]) -> dict[str, Any]:
        if tool_name not in _TOOL_NAMES:
            return {"error": f"未知工具: {tool_name}"}
        if not self.browser_config.enabled:
            return {
                "error": "浏览器功能未启用，请在配置中设置 [browser] enabled = true",
            }
        try:
            handler = getattr(self, f"_do_{tool_name.removeprefix('browser_')}")
            return await handler(args)
        except Exception as e:
            logger.error(f"浏览器工具 [{tool_name}] 执行失败: {e}")
            return {"error": str(e)}

    # ------------------------------------------------------------------
    # 浏览器生命周期
    # ------------------------------------------------------------------

    async def _ensure_browser(self) -> None:
        """惰性启动 Playwright + 浏览器（含 stealth 配置）。"""
        if self._browser is not None:
            return

        from playwright.async_api import async_playwright

        self._playwright = await async_playwright().start()

        mode = self.browser_config.mode

        if mode == "cdp" and self.browser_config.cdp_url:
            # CDP 模式：连接已运行的浏览器，天然无自动化痕迹
            self._browser = await self._playwright.chromium.connect_over_cdp(
                self.browser_config.cdp_url,
            )
            # CDP 连接后使用已有 context
            contexts = self._browser.contexts
            if contexts:
                self._context = contexts[0]
            else:
                self._context = await self._browser.new_context()
            logger.info(f"浏览器已连接 (CDP: {self.browser_config.cdp_url})")
        else:
            # 本地启动模式：系统 Chrome + stealth 参数
            launch_kwargs: dict[str, Any] = {
                "headless": self.browser_config.headless,
                "args": _STEALTH_ARGS,
            }
            # 优先用系统 Chrome
            if self.browser_config.browser_path:
                launch_kwargs["executable_path"] = self.browser_config.browser_path
            else:
                launch_kwargs["channel"] = "chrome"

            self._browser = await self._playwright.chromium.launch(**launch_kwargs)
            # 创建 context 并注入 stealth
            self._context = await self._browser.new_context(
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/131.0.0.0 Safari/537.36"
                ),
                locale="zh-CN",
            )
            logger.info(f"浏览器已启动 (模式: {mode}, stealth: on)")

        # 注入 stealth JS 到所有页面（包括后续新建的页面）
        await self._context.add_init_script(_STEALTH_JS)

    async def _ensure_page(self):
        """确保有一个活跃的页面。"""
        await self._ensure_browser()
        if self._page is None or self._page.is_closed():
            self._page = await self._context.new_page()
        return self._page

    async def close(self) -> None:
        """关闭浏览器和 Playwright 实例。"""
        if self._browser is not None:
            try:
                await self._browser.close()
            except Exception as e:
                logger.warning(f"关闭浏览器时出错: {e}")
            self._browser = None
            self._context = None
            self._page = None
        if self._playwright is not None:
            try:
                await self._playwright.stop()
            except Exception as e:
                logger.warning(f"关闭 Playwright 时出错: {e}")
            self._playwright = None

    # ------------------------------------------------------------------
    # 各工具实现
    # ------------------------------------------------------------------

    async def _do_open(self, args: dict) -> dict[str, Any]:
        url = args["url"]
        page = await self._ensure_page()
        await page.goto(url, wait_until="domcontentloaded", timeout=30000)
        await asyncio.sleep(1)
        title = await page.title()
        return {"message": f"已打开页面: {title}", "url": page.url}

    async def _do_search(self, args: dict) -> dict[str, Any]:
        query = args["query"]
        url = f"https://www.google.com/search?q={quote(query)}"
        page = await self._ensure_page()
        await page.goto(url, wait_until="domcontentloaded", timeout=30000)
        await asyncio.sleep(1)

        title = await page.title()
        try:
            text = await page.evaluate(
                "() => document.body.innerText.substring(0, 3000)",
            )
            return {"message": f"搜索结果: {title}", "text": text[:2000]}
        except Exception:
            return {"message": f"搜索结果: {title}"}

    async def _do_click(self, args: dict) -> dict[str, Any]:
        selector = args["selector"]
        page = await self._ensure_page()
        element = await page.query_selector(selector)
        if not element:
            return {"error": f"未找到匹配 '{selector}' 的元素"}
        await element.click()
        await asyncio.sleep(1)
        return {"message": f"已点击元素: {selector}"}

    async def _do_type(self, args: dict) -> dict[str, Any]:
        selector = args["selector"]
        text = args["text"]
        page = await self._ensure_page()
        element = await page.query_selector(selector)
        if not element:
            return {"error": f"未找到匹配 '{selector}' 的输入框"}
        await element.fill(text)
        return {"message": f"已在 '{selector}' 中输入: {text}"}

    async def _do_scroll(self, args: dict) -> dict[str, Any]:
        direction = args.get("direction", "down")
        pixels = args.get("pixels", 500)
        page = await self._ensure_page()
        y = pixels if direction == "down" else -pixels
        await page.evaluate(f"() => window.scrollBy(0, {y})")
        label = "下" if direction == "down" else "上"
        return {"message": f"已向{label}滚动 {pixels} 像素"}

    async def _do_screenshot(self, _args: dict) -> dict[str, Any]:
        page = await self._ensure_page()

        if page.url != "about:blank":
            await asyncio.sleep(0.5)

        raw_bytes = await page.screenshot(type="png")
        b64_str = base64.b64encode(raw_bytes).decode()

        # 保存到 workspace
        filename = f"browser_{int(time.time())}.png"
        filepath = self.screenshot_dir / filename
        filepath.write_bytes(raw_bytes)

        title = await page.title()
        msg = f"截图已保存: screenshots/{filename}"
        if title:
            msg = f"当前页面: {title}\n{msg}"

        return {
            "message": msg,
            "image_base64": b64_str,
            "image_media_type": "image/png",
        }

    async def _do_get_text(self, _args: dict) -> dict[str, Any]:
        page = await self._ensure_page()
        text = await page.evaluate(
            "() => document.body.innerText.substring(0, 5000)",
        )
        title = await page.title()
        return {"message": f"页面: {title}\nURL: {page.url}", "text": text[:3000]}

    async def _do_back(self, _args: dict) -> dict[str, Any]:
        page = await self._ensure_page()
        await page.go_back()
        await asyncio.sleep(1)
        title = await page.title()
        return {"message": f"已后退到: {title}"}

    async def _do_keys(self, args: dict) -> dict[str, Any]:
        keys = args["keys"]
        page = await self._ensure_page()
        await page.keyboard.press(keys)
        return {"message": f"已发送按键: {keys}"}

    async def _do_close(self, _args: dict) -> dict[str, Any]:
        await self.close()
        return {"message": "浏览器已关闭"}
