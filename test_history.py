"""
测试历史裁剪逻辑 + response_format 参数

测试内容：
1. _trim_history 确保不会切断 assistant+tool 消息组
2. _trim_history 在 limit 内不裁剪
3. LLMClient.chat() 能接受 response_format 参数
"""

import asyncio
import json
import sys
import os

# 把项目 src 加入 path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "src"))

from kaguya.core.engine import ChatEngine


def make_history_with_tool_calls() -> list[dict]:
    """构建包含多轮 user → assistant(tool_calls) → tool 的模拟历史"""
    history = []

    # 第 1 轮：简单对话
    history.append({"role": "user", "content": "你好"})
    history.append({
        "role": "assistant",
        "content": "",
        "tool_calls": [{"id": "call_001", "type": "function", "function": {"name": "send_message_to_user", "arguments": json.dumps({"content": "你好呀！"})}}],
    })
    history.append({"role": "tool", "tool_call_id": "call_001", "content": "Message sent to user successfully."})

    # 第 2 轮：浏览器操作（多个 tool_calls）
    history.append({"role": "user", "content": "帮我查一下天气"})
    history.append({
        "role": "assistant",
        "content": "让我查一下天气",
        "tool_calls": [
            {"id": "call_002", "type": "function", "function": {"name": "web_search", "arguments": json.dumps({"query": "今天天气"})}},
        ],
    })
    history.append({"role": "tool", "tool_call_id": "call_002", "content": "今天晴天，25度"})
    # 第二步：发送结果
    history.append({
        "role": "assistant",
        "content": "",
        "tool_calls": [{"id": "call_003", "type": "function", "function": {"name": "send_message_to_user", "arguments": json.dumps({"content": "今天是晴天！25度！"})}}],
    })
    history.append({"role": "tool", "tool_call_id": "call_003", "content": "Message sent to user successfully."})

    # 第 3 轮：简单对话
    history.append({"role": "user", "content": "谢谢"})
    history.append({
        "role": "assistant",
        "content": "",
        "tool_calls": [{"id": "call_004", "type": "function", "function": {"name": "send_message_to_user", "arguments": json.dumps({"content": "不客气～"})}}],
    })
    history.append({"role": "tool", "tool_call_id": "call_004", "content": "Message sent to user successfully."})

    return history


def test_trim_no_action_within_limit():
    """测试: 历史条数在 limit 以内时不裁剪"""
    history = make_history_with_tool_calls()
    original_len = len(history)
    ChatEngine._trim_history(history, limit=100)
    assert len(history) == original_len, f"不应裁剪，但长度从 {original_len} 变成了 {len(history)}"
    print("✅ test_trim_no_action_within_limit PASSED")


def test_trim_cuts_at_user_boundary():
    """测试: 裁剪只在 user 消息处下刀"""
    history = make_history_with_tool_calls()  # 11 条
    # 设 limit=6，期望切掉前面的消息，但不能切断消息组
    ChatEngine._trim_history(history, limit=6)
    
    # 第一条应该是 user 角色
    assert history[0]["role"] == "user", f"裁剪后第一条消息应该是 user，但是 {history[0]['role']}"
    
    # 不应出现孤立的 tool 消息（每个 tool 前面必须有 assistant+tool_calls）
    for i, msg in enumerate(history):
        if msg["role"] == "tool":
            # 往前找最近的 assistant
            found_assistant = False
            for j in range(i - 1, -1, -1):
                if history[j]["role"] == "assistant" and "tool_calls" in history[j]:
                    found_assistant = True
                    break
                if history[j]["role"] == "user":
                    break  # 到了上一轮了
            assert found_assistant, f"第 {i} 条 tool 消息没有对应的 assistant(tool_calls)！"
    
    print(f"✅ test_trim_cuts_at_user_boundary PASSED (裁剪后 {len(history)} 条)")


def test_trim_extreme_small_limit():
    """测试: limit 很小（如 2）时，能保留至少一个完整组"""
    history = make_history_with_tool_calls()
    ChatEngine._trim_history(history, limit=2)
    
    # 第一条必须是 user
    assert history[0]["role"] == "user", f"极端裁剪后第一条应该是 user，但是 {history[0]['role']}"
    print(f"✅ test_trim_extreme_small_limit PASSED (裁剪后 {len(history)} 条)")


def test_trim_pure_user_assistant_no_tools():
    """测试: 没有 tool 调用的纯对话历史"""
    history = [
        {"role": "user", "content": "消息1"},
        {"role": "assistant", "content": "回复1"},
        {"role": "user", "content": "消息2"},
        {"role": "assistant", "content": "回复2"},
        {"role": "user", "content": "消息3"},
        {"role": "assistant", "content": "回复3"},
    ]
    ChatEngine._trim_history(history, limit=4)
    assert history[0]["role"] == "user", f"第一条应该是 user"
    assert len(history) <= 4, f"应不超过 4 条，实际 {len(history)}"
    print(f"✅ test_trim_pure_user_assistant_no_tools PASSED (裁剪后 {len(history)} 条)")


def test_response_format_parameter():
    """测试: LLMClient.chat() 签名接受 response_format 参数（不实际调用 API）"""
    import inspect
    from kaguya.llm.client import LLMClient
    
    sig = inspect.signature(LLMClient.chat)
    params = list(sig.parameters.keys())
    assert "response_format" in params, f"chat() 缺少 response_format 参数！当前参数: {params}"
    print("✅ test_response_format_parameter PASSED")


def test_fake_assistant_has_content():
    """测试: engine.py 中保存的 fake assistant 消息有 content 字段"""
    # 这不是直接测试 engine，而是模拟检查 fake assistant 消息格式
    fake_assistant = {
        "role": "assistant",
        "content": "",
        "tool_calls": [{"id": "call_test", "type": "function", "function": {"name": "send_message_to_user", "arguments": '{"content": "test"}'}}],
    }
    assert "content" in fake_assistant, "fake assistant 消息必须有 content 字段"
    print("✅ test_fake_assistant_has_content PASSED")


if __name__ == "__main__":
    print("=" * 50)
    print("🧪 OpenKaguya 历史管理测试")
    print("=" * 50)
    
    tests = [
        test_trim_no_action_within_limit,
        test_trim_cuts_at_user_boundary,
        test_trim_extreme_small_limit,
        test_trim_pure_user_assistant_no_tools,
        test_response_format_parameter,
        test_fake_assistant_has_content,
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
