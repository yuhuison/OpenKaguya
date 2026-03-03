"""测试子代理会话系统。"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from kaguya.tools.sub_agent import (
    AGENT_MANAGEMENT_TOOLS,
    REPORT_TOOL_SCHEMA,
    AgentManagementExecutor,
    SubAgentManager,
    SubAgentSession,
    _build_tool_result_messages,
)


# ---------------------------------------------------------------------------
# Schema 测试
# ---------------------------------------------------------------------------


def test_agent_management_tools_count():
    """大模型管理工具应有 3 个。"""
    assert len(AGENT_MANAGEMENT_TOOLS) == 3


def test_agent_management_tools_names():
    """管理工具名称应正确。"""
    names = {t["function"]["name"] for t in AGENT_MANAGEMENT_TOOLS}
    assert names == {"create_sub_agent_session", "instruct_to_sub_agent", "done_sub_agent"}


def test_agent_management_tools_schema_valid():
    """每个管理工具的 schema 结构应正确。"""
    for tool in AGENT_MANAGEMENT_TOOLS:
        assert tool["type"] == "function"
        fn = tool["function"]
        assert "name" in fn
        assert "description" in fn
        assert "parameters" in fn
        assert fn["parameters"]["type"] == "object"


def test_report_tool_schema():
    """report 工具 schema 应有 text/status/include_screenshot。"""
    fn = REPORT_TOOL_SCHEMA["function"]
    assert fn["name"] == "report"
    props = fn["parameters"]["properties"]
    assert "text" in props
    assert "status" in props
    assert "include_screenshot" in props
    assert fn["parameters"]["required"] == ["text", "status"]


# ---------------------------------------------------------------------------
# SubAgentSession
# ---------------------------------------------------------------------------


def test_session_defaults():
    """SubAgentSession 应有正确的默认值。"""
    session = SubAgentSession(session_id="abc", task="测试", tool_group="desktop")
    assert session.session_id == "abc"
    assert session.messages == []
    assert session.tools == []
    assert session.last_screenshot_b64 is None
    assert session.turn_count == 0
    assert session.closed is False


# ---------------------------------------------------------------------------
# SubAgentManager — create_session
# ---------------------------------------------------------------------------


def _make_manager(
    desktop_tools=None, browser_tools=None, max_turns=5,
):
    """构建测试用的 SubAgentManager。"""
    mock_llm = AsyncMock()
    mock_desktop_exec = MagicMock()
    mock_browser_exec = MagicMock()

    if desktop_tools is None:
        desktop_tools = [
            {"type": "function", "function": {"name": "desktop_screenshot", "parameters": {"type": "object"}}},
            {"type": "function", "function": {"name": "desktop_click", "parameters": {"type": "object"}}},
        ]

    if browser_tools is None:
        browser_tools = [
            {"type": "function", "function": {"name": "browser_open", "parameters": {"type": "object"}}},
        ]

    mgr = SubAgentManager(
        agent_llm=mock_llm,
        desktop_tools=desktop_tools,
        desktop_executor=mock_desktop_exec,
        browser_tools=browser_tools,
        browser_executor=mock_browser_exec,
        max_turns=max_turns,
    )
    return mgr, mock_llm, mock_desktop_exec, mock_browser_exec


def test_create_session_desktop():
    """创建 desktop 会话应包含桌面工具 + report。"""
    mgr, _, _, _ = _make_manager()
    sid = mgr.create_session("desktop", "测试桌面")
    session = mgr._sessions[sid]
    tool_names = {t["function"]["name"] for t in session.tools}
    assert "desktop_screenshot" in tool_names
    assert "desktop_click" in tool_names
    assert "report" in tool_names
    assert "browser_open" not in tool_names
    assert session.tool_group == "desktop"


def test_create_session_browser():
    """创建 browser 会话应包含浏览器工具 + report。"""
    mgr, _, _, _ = _make_manager()
    sid = mgr.create_session("browser", "测试浏览器")
    session = mgr._sessions[sid]
    tool_names = {t["function"]["name"] for t in session.tools}
    assert "browser_open" in tool_names
    assert "report" in tool_names
    assert "desktop_screenshot" not in tool_names


def test_create_session_both():
    """创建 both 会话应包含所有工具 + report。"""
    mgr, _, _, _ = _make_manager()
    sid = mgr.create_session("both", "测试全部")
    session = mgr._sessions[sid]
    tool_names = {t["function"]["name"] for t in session.tools}
    assert "desktop_screenshot" in tool_names
    assert "browser_open" in tool_names
    assert "report" in tool_names


def test_create_session_has_system_prompt():
    """创建会话后应有 system message 包含任务描述。"""
    mgr, _, _, _ = _make_manager()
    sid = mgr.create_session("desktop", "帮我打开微信")
    session = mgr._sessions[sid]
    assert len(session.messages) == 1
    assert session.messages[0]["role"] == "system"
    assert "帮我打开微信" in session.messages[0]["content"]


# ---------------------------------------------------------------------------
# SubAgentManager — instruct
# ---------------------------------------------------------------------------


async def test_instruct_simple_report():
    """子代理直接调用 report → instruct 应返回报告数据。"""
    mgr, mock_llm, _, _ = _make_manager()
    sid = mgr.create_session("desktop", "测试")

    mock_llm.chat = AsyncMock(return_value={
        "content": "",
        "tool_calls": [{
            "id": "call_1",
            "name": "report",
            "arguments": {"text": "任务完成", "status": "success"},
        }],
        "raw_tool_calls": [{"id": "call_1", "type": "function", "function": {
            "name": "report", "arguments": '{"text":"任务完成","status":"success"}',
        }}],
    })

    result = await mgr.instruct(sid, "开始执行")
    assert result["text"] == "任务完成"
    assert result["status"] == "success"
    assert "turns_used" in result


async def test_instruct_tools_then_report():
    """子代理先调用工具再 report。"""
    mgr, mock_llm, mock_desktop, _ = _make_manager()
    sid = mgr.create_session("desktop", "截图")

    # 第 1 轮：调用 desktop_screenshot
    # 第 2 轮：调用 report
    mock_desktop.execute = AsyncMock(return_value={
        "image_base64": "abc123",
        "image_media_type": "image/jpeg",
        "text": "检测到 5 个元素",
    })

    call_count = 0

    async def mock_chat(messages, tools=None):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return {
                "content": "",
                "tool_calls": [{"id": "c1", "name": "desktop_screenshot", "arguments": {}}],
                "raw_tool_calls": [{"id": "c1", "type": "function", "function": {
                    "name": "desktop_screenshot", "arguments": "{}",
                }}],
            }
        return {
            "content": "",
            "tool_calls": [{"id": "c2", "name": "report", "arguments": {
                "text": "截图完成", "status": "success",
            }}],
            "raw_tool_calls": [{"id": "c2", "type": "function", "function": {
                "name": "report", "arguments": '{"text":"截图完成","status":"success"}',
            }}],
        }

    mock_llm.chat = mock_chat

    result = await mgr.instruct(sid, "请截图")
    assert result["text"] == "截图完成"
    assert result["status"] == "success"
    assert result["turns_used"] == 2


async def test_instruct_report_with_screenshot():
    """include_screenshot=true 时应附带最近的截图。"""
    mgr, mock_llm, mock_desktop, _ = _make_manager()
    sid = mgr.create_session("desktop", "测试")

    # 模拟截图
    session = mgr._sessions[sid]
    session.last_screenshot_b64 = "screenshot_data_here"
    session.last_screenshot_media_type = "image/jpeg"

    mock_llm.chat = AsyncMock(return_value={
        "content": "",
        "tool_calls": [{
            "id": "c1",
            "name": "report",
            "arguments": {
                "text": "完成",
                "status": "success",
                "include_screenshot": True,
            },
        }],
        "raw_tool_calls": [{"id": "c1", "type": "function", "function": {
            "name": "report",
            "arguments": '{"text":"完成","status":"success","include_screenshot":true}',
        }}],
    })

    result = await mgr.instruct(sid, "操作完成了吗")
    assert result["image_base64"] == "screenshot_data_here"
    assert result["image_media_type"] == "image/jpeg"


async def test_instruct_report_no_screenshot_available():
    """include_screenshot=true 但无截图时，不附带图片。"""
    mgr, mock_llm, _, _ = _make_manager()
    sid = mgr.create_session("desktop", "测试")

    mock_llm.chat = AsyncMock(return_value={
        "content": "",
        "tool_calls": [{
            "id": "c1",
            "name": "report",
            "arguments": {"text": "完成", "status": "success", "include_screenshot": True},
        }],
        "raw_tool_calls": [{"id": "c1", "type": "function", "function": {
            "name": "report",
            "arguments": '{"text":"完成","status":"success","include_screenshot":true}',
        }}],
    })

    result = await mgr.instruct(sid, "操作")
    assert "image_base64" not in result


async def test_instruct_max_turns_exceeded():
    """超过最大轮数应返回 difficulty 报告。"""
    mgr, mock_llm, mock_desktop, _ = _make_manager(max_turns=2)
    sid = mgr.create_session("desktop", "测试")

    mock_desktop.execute = AsyncMock(return_value={"success": True})
    mock_llm.chat = AsyncMock(return_value={
        "content": "",
        "tool_calls": [{"id": "c1", "name": "desktop_click", "arguments": {"label": 1}}],
        "raw_tool_calls": [{"id": "c1", "type": "function", "function": {
            "name": "desktop_click", "arguments": '{"label":1}',
        }}],
    })

    result = await mgr.instruct(sid, "不断点击")
    assert result["status"] == "difficulty"
    assert result.get("max_turns_exceeded") is True


async def test_instruct_llm_error():
    """LLM 请求异常应返回 difficulty 报告。"""
    mgr, mock_llm, _, _ = _make_manager()
    sid = mgr.create_session("desktop", "测试")

    mock_llm.chat = AsyncMock(side_effect=RuntimeError("API error"))

    result = await mgr.instruct(sid, "开始")
    assert result["status"] == "difficulty"
    assert "API error" in result["text"]


async def test_instruct_text_only_response():
    """LLM 无工具调用（纯文本）→ 隐式 success。"""
    mgr, mock_llm, _, _ = _make_manager()
    sid = mgr.create_session("desktop", "测试")

    mock_llm.chat = AsyncMock(return_value={
        "content": "我分析了一下，觉得不需要操作",
        "tool_calls": [],
        "raw_tool_calls": [],
    })

    result = await mgr.instruct(sid, "看看桌面")
    assert result["status"] == "success"
    assert "不需要操作" in result["text"]


async def test_instruct_preserves_history():
    """多次 instruct 应保留消息历史。"""
    mgr, mock_llm, _, _ = _make_manager()
    sid = mgr.create_session("desktop", "测试")

    mock_llm.chat = AsyncMock(return_value={
        "content": "好的",
        "tool_calls": [],
        "raw_tool_calls": [],
    })

    await mgr.instruct(sid, "第一步")
    await mgr.instruct(sid, "第二步")

    session = mgr._sessions[sid]
    # system + user("第一步") + assistant("好的") + user("第二步") + assistant("好的")
    assert len(session.messages) == 5
    user_messages = [m for m in session.messages if m["role"] == "user"]
    assert len(user_messages) == 2
    assert user_messages[0]["content"] == "第一步"
    assert user_messages[1]["content"] == "第二步"


async def test_instruct_report_mid_batch():
    """report 在批次中间时，后续工具应被跳过。"""
    mgr, mock_llm, mock_desktop, _ = _make_manager()
    sid = mgr.create_session("desktop", "测试")

    mock_desktop.execute = AsyncMock(return_value={"success": True})

    mock_llm.chat = AsyncMock(return_value={
        "content": "",
        "tool_calls": [
            {"id": "c1", "name": "desktop_click", "arguments": {"label": 1}},
            {"id": "c2", "name": "report", "arguments": {"text": "完成", "status": "success"}},
            {"id": "c3", "name": "desktop_type", "arguments": {"text": "hello"}},
        ],
        "raw_tool_calls": [
            {"id": "c1", "type": "function", "function": {"name": "desktop_click", "arguments": '{"label":1}'}},
            {"id": "c2", "type": "function", "function": {"name": "report", "arguments": '{"text":"完成","status":"success"}'}},
            {"id": "c3", "type": "function", "function": {"name": "desktop_type", "arguments": '{"text":"hello"}'}},
        ],
    })

    result = await mgr.instruct(sid, "执行")

    assert result["text"] == "完成"
    # desktop_click 应被执行 1 次
    mock_desktop.execute.assert_awaited_once()
    # desktop_type (c3) 应被跳过 → 检查消息中有 skipped
    session = mgr._sessions[sid]
    tool_msgs = [m for m in session.messages if m["role"] == "tool"]
    skipped = [m for m in tool_msgs if '"skipped"' in str(m.get("content", ""))]
    assert len(skipped) == 1


async def test_instruct_tracks_screenshot():
    """工具返回 image_base64 时应更新 session.last_screenshot_b64。"""
    mgr, mock_llm, mock_desktop, _ = _make_manager()
    sid = mgr.create_session("desktop", "测试")

    call_count = 0

    async def mock_chat(messages, tools=None):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return {
                "content": "",
                "tool_calls": [{"id": "c1", "name": "desktop_screenshot", "arguments": {}}],
                "raw_tool_calls": [{"id": "c1", "type": "function", "function": {
                    "name": "desktop_screenshot", "arguments": "{}",
                }}],
            }
        return {
            "content": "",
            "tool_calls": [{"id": "c2", "name": "report", "arguments": {
                "text": "done", "status": "success",
            }}],
            "raw_tool_calls": [{"id": "c2", "type": "function", "function": {
                "name": "report", "arguments": '{"text":"done","status":"success"}',
            }}],
        }

    mock_llm.chat = mock_chat
    mock_desktop.execute = AsyncMock(return_value={
        "image_base64": "new_screenshot_data",
        "image_media_type": "image/png",
        "text": "截图",
    })

    await mgr.instruct(sid, "截图")
    session = mgr._sessions[sid]
    assert session.last_screenshot_b64 == "new_screenshot_data"
    assert session.last_screenshot_media_type == "image/png"


# ---------------------------------------------------------------------------
# SubAgentManager — close
# ---------------------------------------------------------------------------


async def test_close_session():
    """关闭后再 instruct 应返回错误。"""
    mgr, _, _, _ = _make_manager()
    sid = mgr.create_session("desktop", "测试")
    result = mgr.close_session(sid)
    assert result["success"] is True

    result = await mgr.instruct(sid, "继续")
    assert "error" in result


def test_close_session_not_found():
    """关闭不存在的会话应返回错误。"""
    mgr, _, _, _ = _make_manager()
    result = mgr.close_session("nonexistent")
    assert "error" in result


def test_close_all():
    """close_all 应关闭所有会话。"""
    mgr, _, _, _ = _make_manager()
    mgr.create_session("desktop", "任务1")
    mgr.create_session("browser", "任务2")
    assert len(mgr._sessions) == 2
    mgr.close_all()
    assert len(mgr._sessions) == 0


# ---------------------------------------------------------------------------
# AgentManagementExecutor
# ---------------------------------------------------------------------------


async def test_executor_create():
    """execute('create_sub_agent_session') 应创建会话。"""
    mgr, _, _, _ = _make_manager()
    executor = AgentManagementExecutor(mgr)
    result = await executor.execute(
        "create_sub_agent_session",
        {"tools": "desktop", "task": "测试任务"},
    )
    assert result["success"] is True
    assert "session_id" in result


async def test_executor_create_missing_task():
    """缺少 task 应返回错误。"""
    mgr, _, _, _ = _make_manager()
    executor = AgentManagementExecutor(mgr)
    result = await executor.execute(
        "create_sub_agent_session",
        {"tools": "desktop", "task": ""},
    )
    assert "error" in result


async def test_executor_create_invalid_tools():
    """无效 tools 值应返回错误。"""
    mgr, _, _, _ = _make_manager()
    executor = AgentManagementExecutor(mgr)
    result = await executor.execute(
        "create_sub_agent_session",
        {"tools": "invalid", "task": "测试"},
    )
    assert "error" in result


async def test_executor_instruct():
    """execute('instruct_to_sub_agent') 应委托到 manager。"""
    mgr, mock_llm, _, _ = _make_manager()
    executor = AgentManagementExecutor(mgr)

    # 先创建会话
    create_result = await executor.execute(
        "create_sub_agent_session",
        {"tools": "desktop", "task": "测试"},
    )
    sid = create_result["session_id"]

    mock_llm.chat = AsyncMock(return_value={
        "content": "OK",
        "tool_calls": [],
        "raw_tool_calls": [],
    })

    result = await executor.execute(
        "instruct_to_sub_agent",
        {"session_id": sid, "message": "做点什么"},
    )
    assert result["status"] == "success"


async def test_executor_instruct_missing_params():
    """缺少参数应返回错误。"""
    mgr, _, _, _ = _make_manager()
    executor = AgentManagementExecutor(mgr)

    result = await executor.execute(
        "instruct_to_sub_agent",
        {"session_id": "", "message": "test"},
    )
    assert "error" in result

    result = await executor.execute(
        "instruct_to_sub_agent",
        {"session_id": "abc", "message": ""},
    )
    assert "error" in result


async def test_executor_done():
    """execute('done_sub_agent') 应关闭会话。"""
    mgr, _, _, _ = _make_manager()
    executor = AgentManagementExecutor(mgr)

    create_result = await executor.execute(
        "create_sub_agent_session",
        {"tools": "desktop", "task": "测试"},
    )
    sid = create_result["session_id"]

    result = await executor.execute("done_sub_agent", {"session_id": sid})
    assert result["success"] is True


async def test_executor_unknown_tool():
    """未知工具名应返回错误。"""
    mgr, _, _, _ = _make_manager()
    executor = AgentManagementExecutor(mgr)
    result = await executor.execute("nonexistent", {})
    assert "error" in result


# ---------------------------------------------------------------------------
# _build_tool_result_messages
# ---------------------------------------------------------------------------


def test_build_tool_result_text():
    """纯文本结果应构建为 JSON content。"""
    msgs = _build_tool_result_messages(
        "call_1", "desktop_click", {"success": True, "clicked": "(100, 200)"},
    )
    assert len(msgs) == 1
    assert msgs[0]["role"] == "tool"
    assert msgs[0]["tool_call_id"] == "call_1"
    parsed = json.loads(msgs[0]["content"])
    assert parsed["success"] is True


def test_build_tool_result_with_image():
    """含图像的结果应构建为多模态消息。"""
    msgs = _build_tool_result_messages(
        "call_2", "desktop_screenshot",
        {
            "image_base64": "abc123",
            "image_media_type": "image/jpeg",
            "text": "5 个元素",
        },
    )
    assert len(msgs) == 1
    content = msgs[0]["content"]
    assert isinstance(content, list)
    assert content[0]["type"] == "image_url"
    assert "abc123" in content[0]["image_url"]["url"]
    assert content[1]["type"] == "text"
    assert "5 个元素" in content[1]["text"]


def test_build_tool_result_truncation():
    """超长结果应被截断。"""
    long_result = {"data": "x" * 10000}
    msgs = _build_tool_result_messages("call_3", "tool", long_result)
    assert "截断" in msgs[0]["content"]


# ---------------------------------------------------------------------------
# Router reset callback
# ---------------------------------------------------------------------------


def test_router_reset_callback():
    """Router reset 应调用注册的回调。"""
    from kaguya.core.router import ToolRouter

    router = ToolRouter()
    callback_called = []
    router.register_reset_callback(lambda: callback_called.append(True))

    router.reset()
    assert len(callback_called) == 1


def test_router_reset_callback_error_isolation():
    """回调异常不应影响其他回调和 reset 流程。"""
    from kaguya.core.router import ToolRouter

    router = ToolRouter()
    results = []

    def bad_callback():
        raise ValueError("boom")

    def good_callback():
        results.append("ok")

    router.register_reset_callback(bad_callback)
    router.register_reset_callback(good_callback)

    router.reset()  # 不应抛异常
    assert results == ["ok"]
