"""测试 RecursiveMemory 基本功能。"""

import tempfile
from pathlib import Path
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


async def test_build_context_empty(memory):
    ctx = await memory.build_context()
    # 没有记忆时返回空字符串
    assert ctx == ""


async def test_consciousness_log(memory):
    await memory.log_consciousness("浏览了微博，发现一些有趣的内容")
    logs = await memory.get_recent_consciousness_logs(5)
    assert len(logs) == 1
    assert "微博" in logs[0]
