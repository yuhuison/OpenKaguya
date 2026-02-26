"""测试 RecursiveMemory 基本功能。"""

import asyncio
from unittest.mock import AsyncMock

import pytest

from kaguya.config import MemoryConfig
from kaguya.core.memory import RecursiveMemory


@pytest.fixture
def tmp_db(tmp_path):
    return tmp_path / "test.db"


@pytest.fixture
def memory(tmp_db):
    mock_llm = AsyncMock()
    mock_llm.summarize = AsyncMock(return_value="摘要内容")
    cfg = MemoryConfig(working_memory_size=5, l1_summarize_batch=3)
    return RecursiveMemory(tmp_db, mock_llm, cfg)


async def test_add_message(memory):
    await memory.add_message("user", "你好")
    await memory.add_message("assistant", "你好！有什么我可以帮你的吗？")
    wm = memory.get_working_memory()
    assert len(wm) == 2
    assert wm[0]["role"] == "user"
    assert wm[1]["role"] == "assistant"


async def test_notes_crud(memory):
    await memory.note_write("生日", "主人的生日是3月15日")
    notes = await memory.note_read()
    assert len(notes) == 1
    assert notes[0][0] == "生日"

    # 更新
    await memory.note_write("生日", "主人的生日是3月15日（已确认）")
    notes = await memory.note_read()
    assert len(notes) == 1
    assert "已确认" in notes[0][1]

    # 搜索
    found = await memory.note_read("生日")
    assert len(found) == 1

    # 删除
    deleted = await memory.note_delete("生日")
    assert deleted
    notes = await memory.note_read()
    assert len(notes) == 0


async def test_working_memory_compression_trigger(memory):
    """超过 working_memory_size 时应触发压缩。"""
    # working_memory_size=5，添加 6 条消息
    for i in range(6):
        await memory.add_message("user", f"消息{i}")
    # 压缩后 working memory 长度应小于 6
    wm = memory.get_working_memory()
    assert len(wm) < 6
    # 等待后台级联任务完成
    await asyncio.sleep(0.1)


async def test_build_context_empty(memory):
    ctx = await memory.build_context()
    # 没有记忆时返回空字符串
    assert ctx == ""


async def test_consciousness_log(memory):
    await memory.log_consciousness("浏览了微博，发现一些有趣的内容")
    logs = await memory.get_recent_consciousness_logs(5)
    assert len(logs) == 1
    assert "微博" in logs[0]


# ------------------------------------------------------------------
# 新增测试
# ------------------------------------------------------------------


async def test_compression_failure_preserves_messages(tmp_db):
    """LLM 摘要失败时，工作记忆中的消息不应丢失。"""
    mock_llm = AsyncMock()
    mock_llm.summarize = AsyncMock(side_effect=Exception("LLM error"))
    cfg = MemoryConfig(working_memory_size=5, l1_summarize_batch=3)
    mem = RecursiveMemory(tmp_db, mock_llm, cfg)

    for i in range(6):
        await mem.add_message("user", f"消息{i}")

    # 所有 6 条消息应仍在工作记忆中（压缩失败，不应移除）
    wm = mem.get_working_memory()
    assert len(wm) == 6


async def test_consciousness_log_trimming(memory):
    """意识日志应被截断到配置的最大数量。"""
    memory.config.max_consciousness_logs = 5
    for i in range(10):
        await memory.log_consciousness(f"动作{i}")
    logs = await memory.get_recent_consciousness_logs(20)
    assert len(logs) == 5


async def test_build_context_notes_limit(memory):
    """build_context 应限制注入的笔记数量和长度。"""
    memory.config.max_notes = 2
    memory.config.max_note_length = 10
    for i in range(5):
        await memory.note_write(f"笔记{i}", "A" * 100)
    ctx = await memory.build_context()
    # 应只包含 2 条笔记
    assert ctx.count("**笔记") == 2
    # 内容应被截断
    assert "..." in ctx


async def test_build_context_includes_consciousness_logs(memory):
    """build_context 应包含最近的意识日志。"""
    await memory.log_consciousness("查看了微信消息")
    await memory.log_consciousness("浏览了新闻")
    ctx = await memory.build_context()
    assert "最近自主行动" in ctx
    assert "微信消息" in ctx
    assert "新闻" in ctx


async def test_working_memory_persistence(tmp_db):
    """工作记忆应在重启后从数据库恢复。"""
    mock_llm = AsyncMock()
    mock_llm.summarize = AsyncMock(return_value="摘要内容")
    cfg = MemoryConfig(working_memory_size=50)

    mem1 = RecursiveMemory(tmp_db, mock_llm, cfg)
    await mem1.add_message("user", "你好")
    await mem1.add_message("assistant", "你好！")

    # 创建新实例指向同一个数据库（模拟重启）
    mem2 = RecursiveMemory(tmp_db, mock_llm, cfg)
    wm = mem2.get_working_memory()
    assert len(wm) == 2
    assert wm[0]["content"] == "你好"
    assert wm[1]["content"] == "你好！"
