"""RecursiveMemory — 递归摘要记忆系统（V2）。

三层持久化记忆 + 工作记忆（L0）：
  L0: 内存中的当前对话消息（最近 N 条）
  L1: SQLite short_term_memory — 对话摘要（每条 200-500 字）
  L2: SQLite long_term_memory  — L1 的再摘要（每条 500-1000 字）
  L3: SQLite core_memory       — 单条终极核心记忆（1000-2000 字）

另有：notes（主动笔记）、timers（定时器）、consciousness_log（意识日志）
"""

from __future__ import annotations

import asyncio
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from loguru import logger

from kaguya.config import MemoryConfig


# ---------------------------------------------------------------------------
# 数据库初始化
# ---------------------------------------------------------------------------

SCHEMA = """
CREATE TABLE IF NOT EXISTS short_term_memory (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    summary    TEXT NOT NULL,
    start_time DATETIME,
    end_time   DATETIME,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS long_term_memory (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    summary          TEXT NOT NULL,
    time_range_start DATETIME,
    time_range_end   DATETIME,
    created_at       DATETIME DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS core_memory (
    id           INTEGER PRIMARY KEY DEFAULT 1,
    summary      TEXT NOT NULL,
    last_updated DATETIME DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS notes (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    title      TEXT NOT NULL UNIQUE,
    content    TEXT NOT NULL,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS timers (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    label       TEXT NOT NULL,
    trigger_at  DATETIME NOT NULL,
    recurrence  TEXT,
    created_at  DATETIME DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS consciousness_log (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    action_summary TEXT NOT NULL,
    created_at     DATETIME DEFAULT CURRENT_TIMESTAMP
);
"""


# ---------------------------------------------------------------------------
# RecursiveMemory
# ---------------------------------------------------------------------------


class RecursiveMemory:
    """递归摘要记忆系统。"""

    def __init__(self, db_path: str | Path, llm, config: MemoryConfig):
        self.db_path = Path(db_path)
        self.llm = llm  # LLMClient（summarizer）
        self.config = config

        # L0: 工作记忆（内存中的消息列表）
        # 格式: {"role": str, "content": str, "timestamp": str}
        self._working: list[dict[str, Any]] = []
        self._lock = asyncio.Lock()

        # 初始化数据库（同步，在 __init__ 中）
        self._init_db()

    def _init_db(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.db_path)
        conn.executescript(SCHEMA)
        conn.commit()
        conn.close()
        logger.info(f"RecursiveMemory 数据库初始化: {self.db_path}")

    # ------------------------------------------------------------------
    # 公开接口
    # ------------------------------------------------------------------

    async def add_message(self, role: str, content: str) -> None:
        """向工作记忆添加一条消息，并在必要时触发压缩。"""
        async with self._lock:
            self._working.append({
                "role": role,
                "content": content,
                "timestamp": datetime.now().isoformat(timespec="seconds"),
            })
            if len(self._working) > self.config.working_memory_size:
                await self._compress_l0_to_l1()

    def get_working_memory(self) -> list[dict[str, Any]]:
        """返回当前工作记忆（L0）中的消息列表。"""
        return list(self._working)

    async def build_context(self) -> str:
        """构建注入 system prompt 的记忆上下文字符串。"""
        parts: list[str] = []

        core = await asyncio.to_thread(self._db_get_l3)
        if core:
            parts.append(f"## 核心记忆\n{core}")

        l2_list = await asyncio.to_thread(self._db_get_recent_l2, self.config.inject_l2_count)
        if l2_list:
            parts.append("## 近期长期记忆\n" + "\n---\n".join(l2_list))

        l1_list = await asyncio.to_thread(self._db_get_recent_l1, self.config.inject_l1_count)
        if l1_list:
            parts.append("## 近期短期记忆\n" + "\n---\n".join(l1_list))

        notes = await asyncio.to_thread(self._db_get_all_notes)
        if notes:
            lines = [f"- **{t}**: {c}" for t, c in notes]
            parts.append("## 笔记本\n" + "\n".join(lines))

        return "\n\n".join(parts)

    # ------------------------------------------------------------------
    # 笔记操作
    # ------------------------------------------------------------------

    async def note_write(self, title: str, content: str) -> None:
        await asyncio.to_thread(self._db_upsert_note, title, content)

    async def note_read(self, query: Optional[str] = None) -> list[tuple[str, str]]:
        return await asyncio.to_thread(self._db_get_notes, query)

    async def note_delete(self, title: str) -> bool:
        return await asyncio.to_thread(self._db_delete_note, title)

    # ------------------------------------------------------------------
    # 定时器操作
    # ------------------------------------------------------------------

    async def timer_set(self, label: str, trigger_at: datetime, recurrence: Optional[str] = None) -> int:
        return await asyncio.to_thread(self._db_insert_timer, label, trigger_at, recurrence)

    async def timer_get_triggered(self) -> list[dict]:
        return await asyncio.to_thread(self._db_get_triggered_timers)

    async def timer_delete(self, timer_id: int) -> None:
        await asyncio.to_thread(self._db_delete_timer, timer_id)

    async def timer_list(self) -> list[dict]:
        return await asyncio.to_thread(self._db_list_timers)

    # ------------------------------------------------------------------
    # 意识日志
    # ------------------------------------------------------------------

    async def log_consciousness(self, action_summary: str) -> None:
        await asyncio.to_thread(self._db_insert_consciousness_log, action_summary)

    async def get_recent_consciousness_logs(self, n: int = 5) -> list[str]:
        return await asyncio.to_thread(self._db_get_recent_logs, n)

    # ------------------------------------------------------------------
    # Admin 查询接口
    # ------------------------------------------------------------------

    async def get_l1_summaries(self, limit: int = 20) -> list[dict]:
        return await asyncio.to_thread(self._db_get_l1_records, limit)

    async def get_l2_summaries(self, limit: int = 10) -> list[dict]:
        return await asyncio.to_thread(self._db_get_l2_records, limit)

    async def get_core_memory(self) -> str:
        return await asyncio.to_thread(self._db_get_l3) or ""

    # ------------------------------------------------------------------
    # 压缩逻辑
    # ------------------------------------------------------------------

    async def _compress_l0_to_l1(self) -> None:
        """将工作记忆中最旧的 batch 条消息压缩为 L1 摘要。"""
        batch = self.config.l1_summarize_batch
        to_compress = self._working[:batch]
        self._working = self._working[batch:]

        texts = [f"[{m['timestamp']}] {m['role']}: {m['content']}" for m in to_compress]
        start_time = to_compress[0]["timestamp"] if to_compress else None
        end_time = to_compress[-1]["timestamp"] if to_compress else None

        try:
            summary = await self.llm.summarize(
                texts,
                instruction=(
                    "用 200-500 字概括这段对话的要点，"
                    "包括：讨论了什么话题、做出了什么决定、"
                    "用户表达了什么偏好或情绪、有什么重要信息。"
                    "只输出摘要内容，不要添加前缀说明。"
                ),
            )
            await asyncio.to_thread(self._db_insert_l1, summary, start_time, end_time)
            logger.debug(f"L0→L1 压缩完成，共 {len(texts)} 条消息")

            l1_count = await asyncio.to_thread(self._db_count_l1)
            if l1_count > self.config.l1_max:
                await self._compress_l1_to_l2()
        except Exception as e:
            logger.error(f"L0→L1 压缩失败: {e}")

    async def _compress_l1_to_l2(self) -> None:
        """将最旧的若干条 L1 摘要合并为一条 L2 记忆。"""
        batch = self.config.l2_summarize_batch
        oldest = await asyncio.to_thread(self._db_pop_oldest_l1, batch)
        if not oldest:
            return

        texts = [r["summary"] for r in oldest]
        start_time = oldest[0].get("start_time")
        end_time = oldest[-1].get("end_time")

        try:
            summary = await self.llm.summarize(
                texts,
                instruction=(
                    "将这些对话摘要进一步压缩为 500-1000 字的长期记忆，"
                    "保留最重要的事实、用户偏好、关键事件和关系变化。"
                    "只输出摘要内容，不要添加前缀说明。"
                ),
            )
            await asyncio.to_thread(self._db_insert_l2, summary, start_time, end_time)
            logger.debug(f"L1→L2 压缩完成，共 {len(oldest)} 条 L1 记忆")

            l2_count = await asyncio.to_thread(self._db_count_l2)
            if l2_count > self.config.l2_max:
                await self._compress_l2_to_l3()
        except Exception as e:
            logger.error(f"L1→L2 压缩失败: {e}")

    async def _compress_l2_to_l3(self) -> None:
        """将溢出的 L2 记忆合并更新到核心记忆（L3）。"""
        batch = self.config.l2_summarize_batch
        oldest = await asyncio.to_thread(self._db_pop_oldest_l2, batch)
        if not oldest:
            return

        current_core = await asyncio.to_thread(self._db_get_l3) or ""
        texts = ([current_core] if current_core else []) + [r["summary"] for r in oldest]

        try:
            updated = await self.llm.summarize(
                texts,
                instruction=(
                    f"更新这份核心记忆档案。这是关于用户的终极总结，"
                    f"包含最重要的个人信息、长期偏好、关系状态、重大事件。"
                    f"新信息如果与旧信息冲突，以新信息为准。"
                    f"控制在 {self.config.l3_max_tokens // 2} 字以内。"
                    f"只输出核心记忆内容，不要添加前缀说明。"
                ),
            )
            await asyncio.to_thread(self._db_update_l3, updated)
            logger.debug("L2→L3 压缩完成，核心记忆已更新")
        except Exception as e:
            logger.error(f"L2→L3 压缩失败: {e}")

    # ------------------------------------------------------------------
    # 数据库操作（同步，用于 asyncio.to_thread）
    # ------------------------------------------------------------------

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _db_insert_l1(self, summary: str, start_time, end_time) -> None:
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO short_term_memory (summary, start_time, end_time) VALUES (?, ?, ?)",
                (summary, start_time, end_time),
            )

    def _db_count_l1(self) -> int:
        with self._conn() as conn:
            return conn.execute("SELECT COUNT(*) FROM short_term_memory").fetchone()[0]

    def _db_pop_oldest_l1(self, n: int) -> list[dict]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM short_term_memory ORDER BY id ASC LIMIT ?", (n,)
            ).fetchall()
            ids = [r["id"] for r in rows]
            if ids:
                conn.execute(
                    f"DELETE FROM short_term_memory WHERE id IN ({','.join('?' * len(ids))})",
                    ids,
                )
            return [dict(r) for r in rows]

    def _db_get_recent_l1(self, n: int) -> list[str]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT summary FROM short_term_memory ORDER BY id DESC LIMIT ?", (n,)
            ).fetchall()
        return [r["summary"] for r in reversed(rows)]

    def _db_get_l1_records(self, limit: int) -> list[dict]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM short_term_memory ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
        return [dict(r) for r in rows]

    def _db_insert_l2(self, summary: str, start_time, end_time) -> None:
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO long_term_memory (summary, time_range_start, time_range_end) VALUES (?, ?, ?)",
                (summary, start_time, end_time),
            )

    def _db_count_l2(self) -> int:
        with self._conn() as conn:
            return conn.execute("SELECT COUNT(*) FROM long_term_memory").fetchone()[0]

    def _db_pop_oldest_l2(self, n: int) -> list[dict]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM long_term_memory ORDER BY id ASC LIMIT ?", (n,)
            ).fetchall()
            ids = [r["id"] for r in rows]
            if ids:
                conn.execute(
                    f"DELETE FROM long_term_memory WHERE id IN ({','.join('?' * len(ids))})",
                    ids,
                )
            return [dict(r) for r in rows]

    def _db_get_recent_l2(self, n: int) -> list[str]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT summary FROM long_term_memory ORDER BY id DESC LIMIT ?", (n,)
            ).fetchall()
        return [r["summary"] for r in reversed(rows)]

    def _db_get_l2_records(self, limit: int) -> list[dict]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM long_term_memory ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
        return [dict(r) for r in rows]

    def _db_get_l3(self) -> Optional[str]:
        with self._conn() as conn:
            row = conn.execute("SELECT summary FROM core_memory WHERE id = 1").fetchone()
        return row["summary"] if row else None

    def _db_update_l3(self, summary: str) -> None:
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO core_memory (id, summary, last_updated) VALUES (1, ?, CURRENT_TIMESTAMP) "
                "ON CONFLICT(id) DO UPDATE SET summary=excluded.summary, last_updated=excluded.last_updated",
                (summary,),
            )

    def _db_upsert_note(self, title: str, content: str) -> None:
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO notes (title, content, updated_at) VALUES (?, ?, CURRENT_TIMESTAMP) "
                "ON CONFLICT(title) DO UPDATE SET content=excluded.content, updated_at=CURRENT_TIMESTAMP",
                (title, content),
            )

    def _db_get_notes(self, query: Optional[str]) -> list[tuple[str, str]]:
        with self._conn() as conn:
            if query:
                rows = conn.execute(
                    "SELECT title, content FROM notes WHERE title LIKE ? OR content LIKE ?",
                    (f"%{query}%", f"%{query}%"),
                ).fetchall()
            else:
                rows = conn.execute("SELECT title, content FROM notes ORDER BY updated_at DESC").fetchall()
        return [(r["title"], r["content"]) for r in rows]

    def _db_get_all_notes(self) -> list[tuple[str, str]]:
        return self._db_get_notes(None)

    def _db_delete_note(self, title: str) -> bool:
        with self._conn() as conn:
            cur = conn.execute("DELETE FROM notes WHERE title = ?", (title,))
        return cur.rowcount > 0

    def _db_insert_timer(self, label: str, trigger_at: datetime, recurrence: Optional[str]) -> int:
        with self._conn() as conn:
            cur = conn.execute(
                "INSERT INTO timers (label, trigger_at, recurrence) VALUES (?, ?, ?)",
                (label, trigger_at.isoformat(), recurrence),
            )
        return cur.lastrowid

    def _db_get_triggered_timers(self) -> list[dict]:
        now = datetime.now().isoformat()
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM timers WHERE trigger_at <= ?", (now,)
            ).fetchall()
        return [dict(r) for r in rows]

    def _db_delete_timer(self, timer_id: int) -> None:
        with self._conn() as conn:
            conn.execute("DELETE FROM timers WHERE id = ?", (timer_id,))

    def _db_list_timers(self) -> list[dict]:
        now = datetime.now().isoformat()
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM timers WHERE trigger_at > ? ORDER BY trigger_at ASC", (now,)
            ).fetchall()
        return [dict(r) for r in rows]

    def _db_insert_consciousness_log(self, action_summary: str) -> None:
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO consciousness_log (action_summary) VALUES (?)",
                (action_summary,),
            )

    def _db_get_recent_logs(self, n: int) -> list[str]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT action_summary FROM consciousness_log ORDER BY id DESC LIMIT ?", (n,)
            ).fetchall()
        return [r["action_summary"] for r in reversed(rows)]
