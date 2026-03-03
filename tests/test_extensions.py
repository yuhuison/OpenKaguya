"""测试扩展系统。"""

from __future__ import annotations

import textwrap
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from kaguya.extensions.base import Extension, ExtensionContext, Stage
from kaguya.extensions.manager import ExtensionManager


# ---------------------------------------------------------------------------
# 测试用扩展
# ---------------------------------------------------------------------------


class DummyExtension(Extension):
    """空扩展，验证默认行为。"""

    @property
    def name(self) -> str:
        return "dummy"


class NotifExtension(Extension):
    """提供通知的扩展。"""

    @property
    def name(self) -> str:
        return "notif"

    async def get_notifications(self) -> list[dict[str, Any]]:
        return [
            {"pkg": "微信", "title": "张三", "text": "你好", "when": 1700000000000},
        ]


class ToolExtension(Extension):
    """按阶段提供不同工具的扩展。"""

    CHAT_TOOLS = [
        {
            "type": "function",
            "function": {
                "name": "ext_send_msg",
                "description": "发送消息",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "to": {"type": "string"},
                        "text": {"type": "string"},
                    },
                    "required": ["to", "text"],
                },
            },
        },
    ]

    CONSCIOUSNESS_TOOLS = [
        {
            "type": "function",
            "function": {
                "name": "ext_check_status",
                "description": "检查状态",
                "parameters": {"type": "object", "properties": {}, "required": []},
            },
        },
    ]

    @property
    def name(self) -> str:
        return "tool_ext"

    def get_tools(self, stage: Stage) -> list[dict]:
        if stage == Stage.CHAT:
            return self.CHAT_TOOLS
        elif stage == Stage.CONSCIOUSNESS:
            return self.CONSCIOUSNESS_TOOLS
        return []

    async def execute_tool(self, tool_name: str, args: dict) -> dict:
        if tool_name == "ext_send_msg":
            return {"success": True, "message": f"已发送给 {args['to']}"}
        if tool_name == "ext_check_status":
            return {"success": True, "status": "ok"}
        return {"error": f"未知工具: {tool_name}"}


class PromptExtension(Extension):
    """按阶段注入 prompt 的扩展。"""

    @property
    def name(self) -> str:
        return "prompt_ext"

    async def get_prompt(self, stage: Stage) -> str:
        if stage == Stage.CHAT:
            return "你现在可以使用微信工具发送消息。"
        if stage == Stage.NOTIFICATION:
            return "收到通知后可直接通过微信回复。"
        return ""


class BackgroundExtension(Extension):
    """有后台任务的扩展。"""

    @property
    def name(self) -> str:
        return "bg_ext"

    async def run_background(self) -> None:
        pass  # 仅用于检测是否被收集


class BrokenExtension(Extension):
    """每个方法都抛异常的扩展。"""

    @property
    def name(self) -> str:
        return "broken"

    async def get_notifications(self) -> list[dict]:
        raise RuntimeError("通知拉取炸了")

    def get_tools(self, stage: Stage) -> list[dict]:
        raise RuntimeError("工具获取炸了")

    async def execute_tool(self, tool_name: str, args: dict) -> dict:
        raise RuntimeError("执行炸了")

    async def get_prompt(self, stage: Stage) -> str:
        raise RuntimeError("prompt 炸了")


# ---------------------------------------------------------------------------
# Extension 基类
# ---------------------------------------------------------------------------


async def test_dummy_extension_defaults():
    """默认方法应返回空值。"""
    ext = DummyExtension()
    assert ext.name == "dummy"
    assert await ext.get_notifications() == []
    assert ext.get_tools(Stage.CHAT) == []
    result = await ext.execute_tool("foo", {})
    assert "error" in result
    assert await ext.get_prompt(Stage.CHAT) == ""


# ---------------------------------------------------------------------------
# ExtensionManager — 注册与聚合
# ---------------------------------------------------------------------------


async def test_register_and_get_tools():
    """注册扩展后应能按阶段获取工具。"""
    mgr = ExtensionManager()
    mgr.register(ToolExtension())

    chat_tools = mgr.get_all_tools(Stage.CHAT)
    assert len(chat_tools) == 1
    assert chat_tools[0]["function"]["name"] == "ext_send_msg"

    con_tools = mgr.get_all_tools(Stage.CONSCIOUSNESS)
    assert len(con_tools) == 1
    assert con_tools[0]["function"]["name"] == "ext_check_status"

    notif_tools = mgr.get_all_tools(Stage.NOTIFICATION)
    assert notif_tools == []


async def test_get_all_notifications():
    """聚合多个扩展的通知。"""
    mgr = ExtensionManager()
    mgr.register(NotifExtension())
    mgr.register(DummyExtension())  # 无通知

    notifications = await mgr.get_all_notifications()
    assert len(notifications) == 1
    assert notifications[0]["pkg"] == "微信"


async def test_execute_tool_routes_correctly():
    """工具执行应正确路由到扩展。"""
    mgr = ExtensionManager()
    mgr.register(ToolExtension())

    result = await mgr.execute_tool("ext_send_msg", {"to": "李四", "text": "嗨"})
    assert result is not None
    assert result["success"] is True
    assert "李四" in result["message"]


async def test_execute_tool_returns_none_for_unknown():
    """没有扩展能处理的工具应返回 None。"""
    mgr = ExtensionManager()
    mgr.register(DummyExtension())

    result = await mgr.execute_tool("nonexistent_tool", {})
    assert result is None


async def test_get_all_prompts():
    """按阶段聚合 prompt。"""
    mgr = ExtensionManager()
    mgr.register(PromptExtension())
    mgr.register(DummyExtension())  # 返回空 prompt

    chat_prompts = await mgr.get_all_prompts(Stage.CHAT)
    assert len(chat_prompts) == 1
    assert "微信" in chat_prompts[0]

    notif_prompts = await mgr.get_all_prompts(Stage.NOTIFICATION)
    assert len(notif_prompts) == 1
    assert "回复" in notif_prompts[0]

    con_prompts = await mgr.get_all_prompts(Stage.CONSCIOUSNESS)
    assert con_prompts == []


async def test_background_coroutines():
    """应只收集覆盖了 run_background 的扩展。"""
    mgr = ExtensionManager()
    mgr.register(DummyExtension())       # 未覆盖
    mgr.register(BackgroundExtension())   # 已覆盖

    coros = mgr.get_background_coroutines()
    assert len(coros) == 1
    # 清理协程避免 warning
    coros[0].close()


# ---------------------------------------------------------------------------
# 异常隔离
# ---------------------------------------------------------------------------


async def test_broken_extension_isolation_notifications():
    """异常扩展不应影响其他扩展的通知聚合。"""
    mgr = ExtensionManager()
    mgr.register(BrokenExtension())
    mgr.register(NotifExtension())

    notifications = await mgr.get_all_notifications()
    assert len(notifications) == 1
    assert notifications[0]["title"] == "张三"


async def test_broken_extension_isolation_tools():
    """异常扩展不应影响其他扩展的工具获取。"""
    mgr = ExtensionManager()
    mgr.register(BrokenExtension())
    mgr.register(ToolExtension())

    tools = mgr.get_all_tools(Stage.CHAT)
    assert len(tools) == 1
    assert tools[0]["function"]["name"] == "ext_send_msg"


async def test_broken_extension_isolation_prompts():
    """异常扩展不应影响其他扩展的 prompt 聚合。"""
    mgr = ExtensionManager()
    mgr.register(BrokenExtension())
    mgr.register(PromptExtension())

    prompts = await mgr.get_all_prompts(Stage.CHAT)
    assert len(prompts) == 1


async def test_broken_extension_execute_tool():
    """异常扩展的工具执行应返回 error。"""
    mgr = ExtensionManager()
    mgr.register(BrokenExtension())

    result = await mgr.execute_tool("anything", {})
    assert result is not None
    assert "error" in result


# ---------------------------------------------------------------------------
# 生命周期
# ---------------------------------------------------------------------------


async def test_setup_and_teardown():
    """setup 和 teardown 应被调用。"""
    ext = DummyExtension()
    ext.setup = AsyncMock()
    ext.teardown = AsyncMock()

    mgr = ExtensionManager()
    mgr.register(ext)

    mock_ctx = MagicMock(spec=ExtensionContext)
    await mgr.setup_all(mock_ctx)
    ext.setup.assert_awaited_once_with(mock_ctx)

    await mgr.teardown_all()
    ext.teardown.assert_awaited_once()


async def test_setup_failure_doesnt_block_others():
    """单个扩展 setup 失败不应影响其他扩展。"""
    broken = DummyExtension()
    broken.setup = AsyncMock(side_effect=RuntimeError("setup 炸了"))

    good = DummyExtension()
    good.setup = AsyncMock()

    mgr = ExtensionManager()
    mgr.register(broken)
    mgr.register(good)

    mock_ctx = MagicMock(spec=ExtensionContext)
    await mgr.setup_all(mock_ctx)

    good.setup.assert_awaited_once()


# ---------------------------------------------------------------------------
# ExtensionContext
# ---------------------------------------------------------------------------


async def test_extension_context_chat():
    """ExtensionContext.chat() 应调用 engine.handle_consciousness()。"""
    mock_engine = MagicMock()
    mock_engine.handle_consciousness = AsyncMock(return_value="AI 回复")
    mock_memory = MagicMock()
    mock_config = MagicMock()
    mock_config.extensions_raw = {"test": {"key": "value"}}

    ctx = ExtensionContext(engine=mock_engine, memory=mock_memory, app_config=mock_config)

    reply = await ctx.chat("你好", trigger="extension")
    assert reply == "AI 回复"
    mock_engine.handle_consciousness.assert_awaited_once_with(
        "你好",
        trigger="extension",
        pre_activate_groups=None,
    )


async def test_extension_context_get_config():
    """ExtensionContext.get_extension_config() 应返回扩展配置段。"""
    mock_config = MagicMock()
    mock_config.extensions_raw = {
        "wechat": {"api_url": "http://localhost:9090"},
    }

    ctx = ExtensionContext(
        engine=MagicMock(), memory=MagicMock(), app_config=mock_config,
    )

    assert ctx.get_extension_config("wechat") == {"api_url": "http://localhost:9090"}
    assert ctx.get_extension_config("nonexistent") == {}


# ---------------------------------------------------------------------------
# load_from_directory
# ---------------------------------------------------------------------------


async def test_load_from_directory(tmp_path: Path):
    """从目录加载 .py 扩展文件。"""
    ext_file = tmp_path / "my_ext.py"
    ext_file.write_text(textwrap.dedent("""\
        from kaguya.extensions.base import Extension, Stage

        class MyTestExtension(Extension):
            @property
            def name(self):
                return "my_test"

            def get_tools(self, stage):
                if stage == Stage.CHAT:
                    return [{"type": "function", "function": {"name": "my_tool", "description": "test", "parameters": {"type": "object", "properties": {}, "required": []}}}]
                return []
    """), encoding="utf-8")

    mgr = ExtensionManager()
    mgr.load_from_directory(tmp_path)

    assert len(mgr._extensions) == 1
    assert mgr._extensions[0].name == "my_test"
    assert len(mgr.get_all_tools(Stage.CHAT)) == 1


async def test_load_skips_underscore_files(tmp_path: Path):
    """_ 开头的文件应被跳过。"""
    (tmp_path / "_private.py").write_text("class Foo: pass", encoding="utf-8")
    (tmp_path / "__init__.py").write_text("", encoding="utf-8")

    mgr = ExtensionManager()
    mgr.load_from_directory(tmp_path)
    assert len(mgr._extensions) == 0


async def test_load_skips_broken_files(tmp_path: Path):
    """加载失败的文件应被跳过，不影响其他。"""
    (tmp_path / "broken.py").write_text("raise RuntimeError('boom')", encoding="utf-8")

    good_file = tmp_path / "good.py"
    good_file.write_text(textwrap.dedent("""\
        from kaguya.extensions.base import Extension

        class GoodExtension(Extension):
            @property
            def name(self):
                return "good"
    """), encoding="utf-8")

    mgr = ExtensionManager()
    mgr.load_from_directory(tmp_path)
    assert len(mgr._extensions) == 1
    assert mgr._extensions[0].name == "good"


async def test_load_nonexistent_directory():
    """不存在的目录应静默跳过。"""
    mgr = ExtensionManager()
    mgr.load_from_directory(Path("/nonexistent/path"))
    assert len(mgr._extensions) == 0


# ---------------------------------------------------------------------------
# has_notification_extensions
# ---------------------------------------------------------------------------


def test_has_notification_extensions():
    """有扩展注册时应返回 True。"""
    mgr = ExtensionManager()
    assert mgr.has_notification_extensions() is False

    mgr.register(DummyExtension())
    assert mgr.has_notification_extensions() is True
