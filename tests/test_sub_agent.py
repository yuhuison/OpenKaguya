"""
测试子 Agent 工具过滤和安全限制

测试内容：
1. ALWAYS_BLOCKED_TOOLS 始终被过滤
2. secondary 模式额外过滤 SECONDARY_BLOCKED_TOOLS
3. primary 模式可以使用浏览器/终端工具
4. 参数校验
"""

import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "src"))

from kaguya.tools.sub_agent import (
    SubAgentTool,
    ALWAYS_BLOCKED_TOOLS,
    SECONDARY_BLOCKED_TOOLS,
)
from kaguya.tools.registry import Tool, ToolRegistry


# ─── Mock 工具 ───

def _make_mock_tool(name: str) -> Tool:
    class MockTool(Tool):
        @property
        def name(self): return name
        @property
        def description(self): return f"Mock {name}"
        @property
        def parameters(self): return {"type": "object", "properties": {}}
        async def execute(self, **_): return "ok"
    return MockTool()


def _setup() -> tuple[ToolRegistry, SubAgentTool]:
    """构建含全部类型工具的 registry"""
    registry = ToolRegistry()
    all_tools = [
        "read_file", "write_file", "delete_file", "list_files",
        "run_terminal", "view_image", "query_messages",
        "manage_notes", "set_timer",
        "browser_open", "browser_search", "browser_click", "browser_get_text",
        "browser_screenshot", "browser_scroll", "browser_back", "browser_keys",
        "browser_close",
        "web_search", "memory_search", "topic_search",
        "send_message_to_user",  # 应始终被过滤
        "run_sub_agent",         # 应始终被过滤（防递归）
    ]
    for name in all_tools:
        registry.register(_make_mock_tool(name))

    tool = SubAgentTool(
        primary_llm=None,       # 不实际调用
        secondary_llm=None,
        tool_registry=registry,
    )
    return registry, tool


def test_always_blocked():
    """测试: send_message_to_user 和 run_sub_agent 在两种模式下都被过滤"""
    _, tool = _setup()

    for blocked in ALWAYS_BLOCKED_TOOLS:
        # primary
        primary_tools = tool._get_filtered_tools(ALWAYS_BLOCKED_TOOLS)
        primary_names = {t.name for t in primary_tools}
        assert blocked not in primary_names, f"primary 模式不应包含 {blocked}"

        # secondary
        secondary_blocked = ALWAYS_BLOCKED_TOOLS | SECONDARY_BLOCKED_TOOLS
        secondary_tools = tool._get_filtered_tools(secondary_blocked)
        secondary_names = {t.name for t in secondary_tools}
        assert blocked not in secondary_names, f"secondary 模式不应包含 {blocked}"

    print("✅ test_always_blocked PASSED")


def test_secondary_blocks_destructive():
    """测试: secondary 模式过滤浏览器和终端工具"""
    _, tool = _setup()
    secondary_blocked = ALWAYS_BLOCKED_TOOLS | SECONDARY_BLOCKED_TOOLS
    available = tool._get_filtered_tools(secondary_blocked)
    available_names = {t.name for t in available}

    for blocked in SECONDARY_BLOCKED_TOOLS:
        assert blocked not in available_names, f"secondary 不应包含 {blocked}"

    # 但应该包含安全工具
    for safe in ["read_file", "write_file", "list_files", "manage_notes", "web_search"]:
        assert safe in available_names, f"secondary 应包含 {safe}"

    print(f"✅ test_secondary_blocks_destructive PASSED (可用 {len(available)} 个工具)")


def test_primary_has_browser():
    """测试: primary 模式可以使用浏览器工具"""
    _, tool = _setup()
    primary_tools = tool._get_filtered_tools(ALWAYS_BLOCKED_TOOLS)
    primary_names = {t.name for t in primary_tools}

    for browser_tool in ["browser_open", "browser_search", "browser_get_text"]:
        assert browser_tool in primary_names, f"primary 应包含 {browser_tool}"

    assert "run_terminal" in primary_names, "primary 应包含 run_terminal"
    print(f"✅ test_primary_has_browser PASSED (可用 {len(primary_tools)} 个工具)")


def test_tool_schema_generation():
    """测试: 签名正确，可正常序列化"""
    _, tool = _setup()
    schema = tool.to_openai_schema()

    assert schema["type"] == "function"
    assert schema["function"]["name"] == "run_sub_agent"
    params = schema["function"]["parameters"]
    assert "task" in params["properties"]
    assert "model_tier" in params["properties"]
    assert "context" in params["properties"]
    assert params["properties"]["model_tier"]["enum"] == ["primary", "secondary"]
    print("✅ test_tool_schema_generation PASSED")


if __name__ == "__main__":
    print("=" * 50)
    print("🧪 SubAgent 工具测试")
    print("=" * 50)

    tests = [
        test_always_blocked,
        test_secondary_blocks_destructive,
        test_primary_has_browser,
        test_tool_schema_generation,
    ]

    passed = 0
    failed = 0
    for test in tests:
        try:
            test()
            passed += 1
        except Exception as e:
            print(f"❌ {test.__name__} FAILED: {e}")
            failed += 1

    print()
    print(f"结果: {passed} passed, {failed} failed")
    if failed > 0:
        sys.exit(1)
    print("🎉 全部通过！")
