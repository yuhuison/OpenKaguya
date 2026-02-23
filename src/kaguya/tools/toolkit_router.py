"""
Toolkit 路由器 — 按需激活工具组。

将工具分为两层：
- Core Tools: 每轮始终发送给 LLM
- Toolkit Groups: 通过 use_toolkit 工具按需激活

减少每轮发送给 LLM 的 tool 定义数量，节省 token，提高准确率。
"""

from __future__ import annotations

from loguru import logger

from kaguya.tools.registry import Tool, ToolRegistry


# ===================== 使用指南 =====================

TOOLKIT_GUIDES: dict[str, str] = {
    "workspace": (
        "✅ workspace 工具组已激活\n\n"
        "可用工具：\n"
        "- read_file(path): 读取 workspace 中的文件内容\n"
        "- write_file(path, content): 创建或覆盖文件\n"
        "- delete_file(path): 删除文件\n"
        "- list_files(): 列出所有文件和目录\n"
        "- run_terminal(command): 在 workspace 目录下执行终端命令\n\n"
        "⚠️ 所有路径相对于 workspace，不能访问外部文件系统"
    ),
    "browser": (
        "✅ browser 工具组已激活\n\n"
        "可用工具：\n"
        "- browser_task(task): 用自然语言描述浏览任务，browser-use Agent 会自动完成\n"
        "- browser_open(url): 打开指定网页\n"
        "- browser_search(query): 用搜索引擎搜索\n"
        "- browser_click(selector): 点击页面元素\n"
        "- browser_type(selector, text): 在输入框中输入文字\n"
        "- browser_read_page(): 读取当前页面内容\n"
        "- browser_screenshot(): 截取当前页面截图\n\n"
        "💡 推荐用 browser_task 进行复杂浏览任务，它会自动操作浏览器完成整个流程"
    ),
    "image": (
        "✅ image 工具组已激活\n\n"
        "可用工具：\n"
        "- generate_image(prompt, size?): 文字生成图片（支持中英文提示词）\n"
        "- edit_image(image_paths, instruction, size?): 编辑图片或多图融合\n"
        "- view_image(filename): 查看 workspace 中的图片\n"
        "- set_avatar(image_path, changelog): 更换自己的头像形象\n\n"
        "🎨 生成/编辑的图片保存在 workspace/images/ 中，可用 send_message_to_user 的 image_path 发送"
    ),
    "sns": (
        "✅ sns 工具组已激活\n\n"
        "可用工具：\n"
        "- sns_post(content, image_paths?): 发朋友圈\n"
        "- sns_interact(action, sns_id, comment?): 点赞/评论朋友圈\n"
        "- sns_view_detail(sns_id): 查看朋友圈详情和评论\n\n"
        "💡 发朋友圈前可以用 generate_image 生成配图"
    ),
}

# 各 toolkit 包含的工具名列表
TOOLKIT_TOOL_NAMES: dict[str, list[str]] = {
    "workspace": ["read_file", "write_file", "delete_file", "list_files", "run_terminal"],
    "browser": [
        "browser_task", "browser_open", "browser_search",
        "browser_click", "browser_type", "browser_read_page", "browser_screenshot",
    ],
    "image": ["generate_image", "edit_image", "view_image", "set_avatar"],
    "sns": ["sns_post", "sns_interact", "sns_view_detail"],
}


class ToolkitRouter:
    """
    Toolkit 路由器。
    
    管理工具分组，控制哪些工具在当前轮次对 LLM 可见。
    """

    def __init__(self, registry: ToolRegistry):
        self._registry = registry
        # per-conversation 激活状态：{history_key: {toolkit_names}}
        self._active_toolkits: dict[str, set[str]] = {}
        self._current_key: str = ""
        # toolkit 工具名 → toolkit 名的反查表
        self._tool_to_toolkit: dict[str, str] = {}
        for tk_name, tool_names in TOOLKIT_TOOL_NAMES.items():
            for tn in tool_names:
                self._tool_to_toolkit[tn] = tk_name

    def set_context(self, key: str) -> None:
        """
        设置当前上下文 key 并重置该 key 的激活状态。
        应在每次 _process_message 开头调用。
        """
        self._current_key = key
        self._active_toolkits[key] = set()

    def activate(self, toolkit_name: str) -> str:
        """
        激活一个 toolkit，返回使用指南。
        """
        if toolkit_name not in TOOLKIT_GUIDES:
            available = ", ".join(TOOLKIT_GUIDES.keys())
            return f"❌ 未知工具组: {toolkit_name}。可用: {available}"

        self._active_toolkits.setdefault(self._current_key, set()).add(toolkit_name)
        logger.info(f"🔧 Toolkit 已激活: {toolkit_name} (context={self._current_key})")
        return TOOLKIT_GUIDES[toolkit_name]

    def is_active(self, toolkit_name: str) -> bool:
        return toolkit_name in self._active_toolkits.get(self._current_key, set())

    def get_visible_tools(self) -> list[dict]:
        """
        获取当前可见的工具定义（OpenAI schema 格式）。
        返回 core tools + 当前上下文中已激活 toolkit 的工具。
        """
        active = self._active_toolkits.get(self._current_key, set())
        visible = []
        for name, tool in self._registry._tools.items():
            tk = self._tool_to_toolkit.get(name)
            if tk is None:
                visible.append(tool.to_openai_schema())
            elif tk in active:
                visible.append(tool.to_openai_schema())
        return visible

    def can_execute(self, tool_name: str) -> bool:
        """检查工具是否当前可执行（核心工具或已激活 toolkit 的工具）"""
        tk = self._tool_to_toolkit.get(tool_name)
        if tk is None:
            return True
        return tk in self._active_toolkits.get(self._current_key, set())

    @property
    def active_toolkit_names(self) -> list[str]:
        return list(self._active_toolkits.get(self._current_key, set()))


class UseToolkitTool(Tool):
    """激活工具组的元工具"""

    def __init__(self, router: ToolkitRouter):
        self._router = router

    @property
    def name(self):
        return "use_toolkit"

    @property
    def description(self):
        return (
            "激活一组专用工具。激活后该组工具立刻可用。"
            "可用工具组：workspace（文件读写+终端）、browser（浏览器）、"
            "image（图片生成/编辑）、sns（朋友圈）"
        )

    @property
    def parameters(self):
        return {
            "type": "object",
            "properties": {
                "toolkit": {
                    "type": "string",
                    "enum": list(TOOLKIT_GUIDES.keys()),
                    "description": "要激活的工具组",
                },
            },
            "required": ["toolkit"],
        }

    async def execute(self, toolkit: str, **_) -> str:
        return self._router.activate(toolkit)
