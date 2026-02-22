"""
数据库管理 — SQLite + sqlite-vec + FTS5。

由于 aiosqlite 和 sqlite-vec 扩展的线程兼容性问题，
我们使用 sqlite3 + asyncio.to_thread 实现异步数据库操作。
"""

from __future__ import annotations

import asyncio
import sqlite3
import struct
from functools import partial
from pathlib import Path
from typing import Any

import sqlite_vec
from loguru import logger

from kaguya.config import DATA_DIR

_DEFAULT_DIM = 4096


def serialize_f32(vector: list[float]) -> bytes:
    """将 float 列表序列化为 sqlite-vec 所需的紧凑二进制格式"""
    return struct.pack(f"{len(vector)}f", *vector)


class Database:
    """
    辉夜姬的数据库管理器。

    使用 sqlite3 + asyncio.to_thread，确保 sqlite-vec 扩展可以正确加载。
    所有方法都是异步的，内部通过线程池执行同步 sqlite3 操作。
    """

    def __init__(self, db_path: Path | None = None, embedding_dim: int = _DEFAULT_DIM):
        self.db_path = db_path or DATA_DIR / "kaguya.db"
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.embedding_dim = embedding_dim
        self._conn: sqlite3.Connection | None = None

    async def connect(self) -> None:
        """连接数据库并初始化"""
        await asyncio.to_thread(self._sync_connect)
        logger.info(f"数据库已连接: {self.db_path}")
        await asyncio.to_thread(self._create_tables)

    def _sync_connect(self) -> None:
        """同步连接（在线程池中执行）"""
        self._conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        self._conn.enable_load_extension(True)
        sqlite_vec.load(self._conn)
        self._conn.enable_load_extension(False)
        
        # 验证
        version = self._conn.execute("SELECT vec_version()").fetchone()[0]
        logger.debug(f"sqlite-vec v{version} 加载成功")

    async def close(self) -> None:
        """关闭数据库连接"""
        if self._conn:
            await asyncio.to_thread(self._conn.close)
            self._conn = None

    def _create_tables(self) -> None:
        """创建所有表（如果不存在）— 同步版本"""
        dim = self.embedding_dim
        c = self._conn

        c.executescript(f"""
            CREATE TABLE IF NOT EXISTS messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id TEXT NOT NULL,
                platform TEXT NOT NULL,
                role TEXT NOT NULL,
                content TEXT NOT NULL,
                display_content TEXT,
                tool_calls TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                is_vectorized BOOLEAN DEFAULT FALSE
            );

            CREATE TABLE IF NOT EXISTS daily_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id TEXT NOT NULL,
                summary TEXT NOT NULL,
                message_range_start INTEGER,
                message_range_end INTEGER,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS notebook (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT,
                content TEXT NOT NULL,
                tags TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
        """)

        # vec0 虚拟表（不支持 IF NOT EXISTS）
        try:
            c.execute(
                f"CREATE VIRTUAL TABLE message_vectors USING vec0("
                f"  message_id INTEGER, embedding float[{dim}]"
                f")"
            )
        except sqlite3.OperationalError:
            pass  # 已存在

        try:
            c.execute(
                f"CREATE VIRTUAL TABLE notebook_vectors USING vec0("
                f"  note_id INTEGER, embedding float[{dim}]"
                f")"
            )
        except sqlite3.OperationalError:
            pass

        # FTS5（使用 trigram 分词器，支持中文子串匹配）
        try:
            c.execute("""
                CREATE VIRTUAL TABLE messages_fts USING fts5(
                    content,
                    content='messages',
                    content_rowid='id',
                    tokenize='trigram'
                )
            """)
        except sqlite3.OperationalError:
            pass

        c.commit()
        logger.debug("数据库表初始化完成")

    # ==================== 消息操作 ====================

    async def save_message(
        self,
        user_id: str,
        platform: str,
        role: str,
        content: str,
        display_content: str | None = None,
        tool_calls: str | None = None,
    ) -> int:
        """保存一条消息，返回消息 ID"""
        def _save():
            cursor = self._conn.execute(
                """INSERT INTO messages (user_id, platform, role, content, display_content, tool_calls)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (user_id, platform, role, content, display_content, tool_calls),
            )
            msg_id = cursor.lastrowid
            # 同步更新 FTS5 索引
            self._conn.execute(
                "INSERT INTO messages_fts(rowid, content) VALUES (?, ?)",
                (msg_id, content),
            )
            self._conn.commit()
            return msg_id

        return await asyncio.to_thread(_save)

    async def get_recent_messages(self, user_id: str, limit: int = 10) -> list[dict]:
        """获取最近 N 条消息"""
        def _get():
            rows = self._conn.execute(
                """SELECT id, role, content, display_content, created_at
                   FROM messages WHERE user_id = ? ORDER BY id DESC LIMIT ?""",
                (user_id, limit),
            ).fetchall()
            return [
                {
                    "id": r[0], "role": r[1], "content": r[2],
                    "display_content": r[3], "created_at": r[4],
                }
                for r in reversed(rows)
            ]

        return await asyncio.to_thread(_get)

    # ==================== 向量操作 ====================

    async def insert_vector(self, message_id: int, embedding: list[float]) -> None:
        """插入消息向量"""
        def _insert():
            self._conn.execute(
                "INSERT INTO message_vectors(rowid, message_id, embedding) VALUES (?, ?, ?)",
                (message_id, message_id, serialize_f32(embedding)),
            )
            self._conn.commit()

        await asyncio.to_thread(_insert)

    async def search_vectors(
        self, query_embedding: list[float], top_k: int = 10
    ) -> list[tuple[int, float]]:
        """向量 KNN 搜索，返回 [(message_id, distance), ...]"""
        def _search():
            return self._conn.execute(
                """SELECT message_id, distance
                   FROM message_vectors
                   WHERE embedding MATCH ?
                   ORDER BY distance
                   LIMIT ?""",
                (serialize_f32(query_embedding), top_k),
            ).fetchall()

        rows = await asyncio.to_thread(_search)
        return [(r[0], r[1]) for r in rows]

    # ==================== FTS5 操作 ====================

    async def search_fts(self, query: str, top_k: int = 10) -> list[tuple[int, float]]:
        """FTS5 全文检索（trigram 分词，支持中文子串匹配）"""
        query = query.strip()
        if not query:
            return []
        # trigram tokenizer 直接匹配子串
        fts_query = f'"{query}"'

        def _search():
            try:
                return self._conn.execute(
                    """SELECT rowid, rank
                       FROM messages_fts
                       WHERE messages_fts MATCH ?
                       ORDER BY rank
                       LIMIT ?""",
                    (fts_query, top_k),
                ).fetchall()
            except Exception:
                return []

        rows = await asyncio.to_thread(_search)
        return [(r[0], r[1]) for r in rows]

    # ==================== 未向量化消息 ====================

    async def get_unvectorized_messages(self, user_id: str) -> list[dict]:
        """获取指定用户的所有未向量化消息"""
        def _get():
            return self._conn.execute(
                """SELECT id, content FROM messages
                   WHERE user_id = ? AND is_vectorized = FALSE
                   ORDER BY id ASC""",
                (user_id,),
            ).fetchall()

        rows = await asyncio.to_thread(_get)
        return [{"id": r[0], "content": r[1]} for r in rows]

    async def get_unvectorized_count(self, user_id: str) -> int:
        """获取未向量化消息数量"""
        def _get():
            return self._conn.execute(
                "SELECT COUNT(*) FROM messages WHERE user_id = ? AND is_vectorized = FALSE",
                (user_id,),
            ).fetchone()[0]

        return await asyncio.to_thread(_get)

    async def mark_vectorized(self, message_ids: list[int]) -> None:
        """将消息标记为已向量化"""
        if not message_ids:
            return
        def _mark():
            placeholders = ",".join("?" * len(message_ids))
            self._conn.execute(
                f"UPDATE messages SET is_vectorized = TRUE WHERE id IN ({placeholders})",
                message_ids,
            )
            self._conn.commit()

        await asyncio.to_thread(_mark)

    async def fetch_messages_by_ids(self, ids: list[int]) -> list[dict]:
        """根据 ID 列表获取消息"""
        if not ids:
            return []
        def _fetch():
            placeholders = ",".join("?" * len(ids))
            return self._conn.execute(
                f"""SELECT id, user_id, role, content, display_content, created_at
                    FROM messages WHERE id IN ({placeholders})
                    ORDER BY id ASC""",
                ids,
            ).fetchall()

        rows = await asyncio.to_thread(_fetch)
        return [
            {
                "id": r[0], "user_id": r[1], "role": r[2],
                "content": r[3], "display_content": r[4], "created_at": r[5],
            }
            for r in rows
        ]

    # ==================== 日志操作 ====================

    async def save_daily_log(
        self, user_id: str, summary: str, range_start: int, range_end: int
    ) -> None:
        """保存对话摘要日志"""
        def _save():
            self._conn.execute(
                """INSERT INTO daily_logs (user_id, summary, message_range_start, message_range_end)
                   VALUES (?, ?, ?, ?)""",
                (user_id, summary, range_start, range_end),
            )
            self._conn.commit()

        await asyncio.to_thread(_save)
