"""
数据库管理 — SQLite + sqlite-vec + FTS5。

由于 aiosqlite 和 sqlite-vec 扩展的线程兼容性问题，
我们使用 sqlite3 + asyncio.to_thread 实现异步数据库操作。
"""

from __future__ import annotations

import asyncio
import sqlite3
import struct
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
        self._write_lock = asyncio.Lock()

    async def connect(self) -> None:
        """连接数据库并初始化"""
        await asyncio.to_thread(self._sync_connect)
        logger.info(f"数据库已连接: {self.db_path}")
        await asyncio.to_thread(self._create_tables)

    def _sync_connect(self) -> None:
        """同步连接（在线程池中执行）"""
        self._conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")  # WAL 模式，支持读写并发
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.enable_load_extension(True)
        sqlite_vec.load(self._conn)
        self._conn.enable_load_extension(False)

        # 验证
        version = self._conn.execute("SELECT vec_version()").fetchone()[0]
        logger.debug(f"sqlite-vec v{version} 加载成功 (WAL mode)")

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
            -- 消息表（is_archived: 是否已被归入话题）
            CREATE TABLE IF NOT EXISTS messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id TEXT NOT NULL,
                platform TEXT NOT NULL,
                role TEXT NOT NULL,
                content TEXT NOT NULL,
                display_content TEXT,
                tool_calls TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                is_archived BOOLEAN DEFAULT FALSE
            );

            -- 话题表（每个用户的记忆按话题组织）
            CREATE TABLE IF NOT EXISTS topics (
                id TEXT PRIMARY KEY,
                user_id TEXT NOT NULL,
                title TEXT NOT NULL,
                summary TEXT NOT NULL DEFAULT '',
                message_count INTEGER DEFAULT 0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );

            -- 话题↔消息关联表
            CREATE TABLE IF NOT EXISTS topic_messages (
                topic_id TEXT NOT NULL,
                message_id INTEGER NOT NULL,
                PRIMARY KEY (topic_id, message_id)
            );

            -- 笔记本（owner_id: 'kaguya' 或用户ID）
            CREATE TABLE IF NOT EXISTS notebook (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                owner_id TEXT NOT NULL DEFAULT 'kaguya',
                title TEXT,
                content TEXT NOT NULL,
                tags TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );

            -- 技能
            CREATE TABLE IF NOT EXISTS skills (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL UNIQUE,
                description TEXT NOT NULL,
                trigger_keywords TEXT,
                is_active BOOLEAN DEFAULT TRUE,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );

            -- 任务
            CREATE TABLE IF NOT EXISTS tasks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT NOT NULL,
                description TEXT,
                status TEXT DEFAULT 'pending',
                priority INTEGER DEFAULT 0,
                due_at TIMESTAMP,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                completed_at TIMESTAMP
            );

            -- 定时器
            CREATE TABLE IF NOT EXISTS timers (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                cron_expression TEXT,
                trigger_at TIMESTAMP,
                action TEXT NOT NULL,
                is_recurring BOOLEAN DEFAULT FALSE,
                is_active BOOLEAN DEFAULT TRUE,
                last_triggered_at TIMESTAMP,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );

            -- 主动意识行动日志
            CREATE TABLE IF NOT EXISTS consciousness_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                summary TEXT NOT NULL,
                target_users TEXT,
                artifacts TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
        """)

        # 话题向量表（user_id 做 partition key，支持按用户隔离搜索）
        try:
            c.execute(
                f"CREATE VIRTUAL TABLE topic_vectors USING vec0("
                f"  user_id TEXT partition key,"
                f"  topic_id TEXT,"
                f"  embedding float[{dim}]"
                f")"
            )
        except sqlite3.OperationalError:
            pass  # 已存在

        # FTS5（消息全文检索，用于在话题内关键词搜索）
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

        # FTS5 自动同步触发器（替代手动 INSERT）
        c.executescript("""
            CREATE TRIGGER IF NOT EXISTS messages_fts_ai AFTER INSERT ON messages BEGIN
                INSERT INTO messages_fts(rowid, content) VALUES (new.id, new.content);
            END;
            CREATE TRIGGER IF NOT EXISTS messages_fts_ad AFTER DELETE ON messages BEGIN
                INSERT INTO messages_fts(messages_fts, rowid, content)
                    VALUES('delete', old.id, old.content);
            END;
        """)

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
            # FTS5 由触发器自动同步，无需手动 INSERT
            self._conn.commit()
            return msg_id

        async with self._write_lock:
            return await asyncio.to_thread(_save)

    async def get_recent_messages(self, user_id: str, limit: int = 20) -> list[dict]:
        """获取最近 N 条消息（按时间正序）"""
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

    # ==================== 未归档消息操作 ====================

    async def get_unarchived_count(self, user_id: str) -> int:
        """获取未归档消息数量"""
        def _get():
            return self._conn.execute(
                "SELECT COUNT(*) FROM messages WHERE user_id = ? AND is_archived = FALSE",
                (user_id,),
            ).fetchone()[0]

        return await asyncio.to_thread(_get)

    async def get_unarchived_messages(self, user_id: str) -> list[dict]:
        """获取所有未归档消息（含 display_content，按时间正序）"""
        def _get():
            rows = self._conn.execute(
                """SELECT id, role, content, display_content, created_at
                   FROM messages
                   WHERE user_id = ? AND is_archived = FALSE
                   ORDER BY id ASC""",
                (user_id,),
            ).fetchall()
            return [
                {
                    "id": r[0], "role": r[1], "content": r[2],
                    "display_content": r[3], "created_at": r[4],
                }
                for r in rows
            ]

        return await asyncio.to_thread(_get)

    async def mark_archived(self, message_ids: list[int]) -> None:
        """将消息标记为已归档"""
        if not message_ids:
            return

        def _mark():
            placeholders = ",".join("?" * len(message_ids))
            self._conn.execute(
                f"UPDATE messages SET is_archived = TRUE WHERE id IN ({placeholders})",
                message_ids,
            )
            self._conn.commit()

        async with self._write_lock:
            await asyncio.to_thread(_mark)

    async def get_recent_active_users(self, limit: int = 10) -> list[dict]:
        """
        获取最近有过对话的用户列表（用于主动意识的用户感知）。
        返回字段：user_id, platform, last_message_at, message_count
        """
        def _get():
            rows = self._conn.execute(
                """SELECT user_id, platform,
                          MAX(created_at) as last_message_at,
                          COUNT(*) as message_count
                   FROM messages
                   WHERE user_id NOT IN ('__system__', 'kaguya')
                   GROUP BY user_id, platform
                   ORDER BY last_message_at DESC
                   LIMIT ?""",
                (limit,),
            ).fetchall()
            return [
                {
                    "user_id": r[0], "platform": r[1],
                    "last_message_at": r[2], "message_count": r[3],
                }
                for r in rows
            ]
        return await asyncio.to_thread(_get)

    async def get_recent_messages_snapshot(
        self, per_user: int = 5, max_users: int = 5
    ) -> list[dict]:
        """
        获取最近活跃用户的最新 N 条消息（用于主动意识的对话感知）。
        返回字段：user_id, role, content, display_content, created_at
        """
        def _get():
            # 先找最近 max_users 个活跃用户
            active_users = self._conn.execute(
                """SELECT user_id FROM messages
                   WHERE user_id NOT IN ('__system__', 'kaguya')
                   GROUP BY user_id
                   ORDER BY MAX(created_at) DESC
                   LIMIT ?""",
                (max_users,),
            ).fetchall()

            result = []
            for (uid,) in active_users:
                rows = self._conn.execute(
                    """SELECT user_id, role, content, display_content, created_at
                       FROM messages
                       WHERE user_id = ?
                       ORDER BY id DESC LIMIT ?""",
                    (uid, per_user),
                ).fetchall()
                for r in reversed(rows):
                    result.append({
                        "user_id": r[0], "role": r[1], "content": r[2],
                        "display_content": r[3], "created_at": r[4],
                    })
            return result
        return await asyncio.to_thread(_get)

    # ==================== 意识日志 ====================

    async def save_consciousness_log(
        self, summary: str, target_users: str = "", artifacts: str = ""
    ) -> None:
        """保存一条主动意识行动日志"""
        def _save():
            self._conn.execute(
                """INSERT INTO consciousness_logs (summary, target_users, artifacts)
                   VALUES (?, ?, ?)""",
                (summary, target_users, artifacts),
            )
            self._conn.commit()
        async with self._write_lock:
            await asyncio.to_thread(_save)

    async def get_recent_consciousness_logs(self, n: int = 5) -> list[dict]:
        """获取最近 n 条意识日志"""
        def _get():
            rows = self._conn.execute(
                """SELECT summary, target_users, artifacts, created_at
                   FROM consciousness_logs
                   ORDER BY id DESC LIMIT ?""",
                (n,),
            ).fetchall()
            return [
                {
                    "summary": r[0],
                    "target_users": r[1],
                    "artifacts": r[2],
                    "created_at": r[3],
                }
                for r in reversed(rows)
            ]
        return await asyncio.to_thread(_get)

    # ==================== 话题操作 ====================

    async def get_all_topics(self, user_id: str) -> list[dict]:
        """获取用户所有话题（只含 id/title/updated_at/message_count，不含摘要正文）"""
        def _get():
            rows = self._conn.execute(
                """SELECT id, title, message_count, updated_at
                   FROM topics WHERE user_id = ?
                   ORDER BY updated_at DESC""",
                (user_id,),
            ).fetchall()
            return [
                {"id": r[0], "title": r[1], "message_count": r[2], "updated_at": r[3]}
                for r in rows
            ]

        return await asyncio.to_thread(_get)

    async def get_topic_by_id(self, topic_id: str) -> dict | None:
        """获取话题完整内容（含摘要）"""
        def _get():
            row = self._conn.execute(
                """SELECT id, user_id, title, summary, message_count, created_at, updated_at
                   FROM topics WHERE id = ?""",
                (topic_id,),
            ).fetchone()
            if not row:
                return None
            return {
                "id": row[0], "user_id": row[1], "title": row[2],
                "summary": row[3], "message_count": row[4],
                "created_at": row[5], "updated_at": row[6],
            }

        return await asyncio.to_thread(_get)

    async def upsert_topic(
        self,
        topic_id: str,
        user_id: str,
        title: str,
        summary: str,
        message_count: int,
    ) -> None:
        """创建或更新话题（upsert）"""
        def _upsert():
            self._conn.execute(
                """INSERT INTO topics (id, user_id, title, summary, message_count, updated_at)
                   VALUES (?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                   ON CONFLICT(id) DO UPDATE SET
                       title=excluded.title,
                       summary=excluded.summary,
                       message_count=excluded.message_count,
                       updated_at=CURRENT_TIMESTAMP""",
                (topic_id, user_id, title, summary, message_count),
            )
            self._conn.commit()

        async with self._write_lock:
            await asyncio.to_thread(_upsert)

    async def get_recent_updated_topics(self, user_id: str, n: int = 1) -> list[dict]:
        """获取最近更新的 N 个话题（含完整摘要）"""
        def _get():
            rows = self._conn.execute(
                """SELECT id, title, summary, message_count, updated_at
                   FROM topics WHERE user_id = ?
                   ORDER BY updated_at DESC LIMIT ?""",
                (user_id, n),
            ).fetchall()
            return [
                {
                    "id": r[0], "title": r[1], "summary": r[2],
                    "message_count": r[3], "updated_at": r[4],
                }
                for r in rows
            ]

        return await asyncio.to_thread(_get)

    # ==================== 话题↔消息关联操作 ====================

    async def link_messages_to_topic(self, topic_id: str, message_ids: list[int]) -> None:
        """将消息 ID 列表关联到指定话题"""
        if not message_ids:
            return

        def _link():
            self._conn.executemany(
                "INSERT OR IGNORE INTO topic_messages (topic_id, message_id) VALUES (?, ?)",
                [(topic_id, mid) for mid in message_ids],
            )
            self._conn.commit()

        async with self._write_lock:
            await asyncio.to_thread(_link)

    async def get_messages_by_topic(self, topic_id: str, limit: int = 50) -> list[dict]:
        """获取话题下所有原始消息（时间正序）"""
        def _get():
            rows = self._conn.execute(
                """SELECT m.id, m.role, m.content, m.display_content, m.created_at
                   FROM messages m
                   JOIN topic_messages tm ON m.id = tm.message_id
                   WHERE tm.topic_id = ?
                   ORDER BY m.id ASC
                   LIMIT ?""",
                (topic_id, limit),
            ).fetchall()
            return [
                {
                    "id": r[0], "role": r[1], "content": r[2],
                    "display_content": r[3], "created_at": r[4],
                }
                for r in rows
            ]

        return await asyncio.to_thread(_get)

    async def search_messages_in_topics(
        self, topic_ids: list[str], keyword: str, limit: int = 10
    ) -> list[dict]:
        """在指定话题的消息中进行关键词检索（FTS5）"""
        if not topic_ids or not keyword:
            return []

        def _search():
            placeholders = ",".join("?" * len(topic_ids))
            # 先用 FTS5 找命中的消息 ID，再过滤属于指定话题的消息
            fts_query = f'"{keyword}"'
            try:
                rows = self._conn.execute(
                    f"""SELECT m.id, m.role, m.display_content, m.content, m.created_at
                        FROM messages_fts f
                        JOIN messages m ON m.id = f.rowid
                        JOIN topic_messages tm ON m.id = tm.message_id
                        WHERE f.messages_fts MATCH ?
                          AND tm.topic_id IN ({placeholders})
                        ORDER BY f.rank
                        LIMIT ?""",
                    [fts_query] + topic_ids + [limit],
                ).fetchall()
            except Exception:
                return []
            return [
                {
                    "id": r[0], "role": r[1],
                    "content": (r[2] or r[3])[:300],
                    "created_at": r[4],
                }
                for r in rows
            ]

        return await asyncio.to_thread(_search)

    # ==================== 话题向量操作 ====================

    async def upsert_topic_vector(self, topic_id: str, user_id: str, embedding: list[float]) -> None:
        """插入或更新话题向量"""
        def _upsert():
            # sqlite-vec 的 vec0 表不支持 ON CONFLICT，需要先删再插
            try:
                self._conn.execute(
                    "DELETE FROM topic_vectors WHERE topic_id = ?", (topic_id,)
                )
            except Exception:
                pass
            self._conn.execute(
                "INSERT INTO topic_vectors(user_id, topic_id, embedding) VALUES (?, ?, ?)",
                (user_id, topic_id, serialize_f32(embedding)),
            )
            self._conn.commit()

        async with self._write_lock:
            await asyncio.to_thread(_upsert)

    async def search_topic_vectors(
        self, query_embedding: list[float], top_k: int = 3, user_id: str = ""
    ) -> list[tuple[str, float]]:
        """在话题向量中做 KNN 搜索，返回 [(topic_id, distance), ...]"""
        def _search():
            try:
                if user_id:
                    # 使用 partition key 过滤，只搜索该用户的向量
                    return self._conn.execute(
                        """SELECT topic_id, distance
                           FROM topic_vectors
                           WHERE embedding MATCH ?
                             AND k = ?
                             AND user_id = ?""",
                        (serialize_f32(query_embedding), top_k, user_id),
                    ).fetchall()
                else:
                    return self._conn.execute(
                        """SELECT topic_id, distance
                           FROM topic_vectors
                           WHERE embedding MATCH ?
                           ORDER BY distance
                           LIMIT ?""",
                        (serialize_f32(query_embedding), top_k),
                    ).fetchall()
            except Exception:
                return []

        rows = await asyncio.to_thread(_search)
        return [(r[0], r[1]) for r in rows]

    # ==================== 笔记本操作 ====================

    async def save_note(self, title: str, content: str, tags: str = "", owner_id: str = "kaguya") -> int:
        """保存笔记，返回 ID"""
        def _save():
            cursor = self._conn.execute(
                "INSERT INTO notebook (owner_id, title, content, tags) VALUES (?, ?, ?, ?)",
                (owner_id, title, content, tags),
            )
            self._conn.commit()
            return cursor.lastrowid
        async with self._write_lock:
            return await asyncio.to_thread(_save)

    async def get_note_by_id(self, note_id: int) -> dict | None:
        """根据 ID 获取笔记完整内容"""
        def _get():
            row = self._conn.execute(
                "SELECT id, owner_id, title, content, tags, created_at, updated_at FROM notebook WHERE id = ?",
                (note_id,),
            ).fetchone()
            if not row:
                return None
            return {"id": row[0], "owner_id": row[1], "title": row[2], "content": row[3], "tags": row[4], "created_at": row[5], "updated_at": row[6]}
        return await asyncio.to_thread(_get)

    async def get_notes_by_owner(self, owner_id: str, limit: int = 20) -> list[dict]:
        """获取指定 owner 的笔记列表（含标题和时间，不含正文）"""
        def _get():
            rows = self._conn.execute(
                "SELECT id, title, tags, updated_at FROM notebook WHERE owner_id = ? ORDER BY updated_at DESC LIMIT ?",
                (owner_id, limit),
            ).fetchall()
            return [{"id": r[0], "title": r[1], "tags": r[2], "updated_at": r[3]} for r in rows]
        return await asyncio.to_thread(_get)

    async def append_note_content(self, note_id: int, additional_content: str) -> bool:
        """向笔记追加内容，返回是否成功"""
        def _append():
            result = self._conn.execute(
                "UPDATE notebook SET content = content || ? || ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                ("\n\n", additional_content, note_id),
            )
            self._conn.commit()
            return result.rowcount > 0
        async with self._write_lock:
            return await asyncio.to_thread(_append)

    async def delete_note(self, note_id: int) -> bool:
        """删除笔记，返回是否成功"""
        def _del():
            result = self._conn.execute("DELETE FROM notebook WHERE id = ?", (note_id,))
            self._conn.commit()
            return result.rowcount > 0
        async with self._write_lock:
            return await asyncio.to_thread(_del)

    async def get_notes(self, tag: str | None = None, limit: int = 20) -> list[dict]:
        """获取笔记列表（兼容旧接口）"""
        def _get():
            if tag:
                rows = self._conn.execute(
                    "SELECT id, owner_id, title, content, tags, updated_at FROM notebook WHERE tags LIKE ? ORDER BY updated_at DESC LIMIT ?",
                    (f"%{tag}%", limit),
                ).fetchall()
            else:
                rows = self._conn.execute(
                    "SELECT id, owner_id, title, content, tags, updated_at FROM notebook ORDER BY updated_at DESC LIMIT ?",
                    (limit,),
                ).fetchall()
            return [{"id": r[0], "owner_id": r[1], "title": r[2], "content": r[3], "tags": r[4], "updated_at": r[5]} for r in rows]
        return await asyncio.to_thread(_get)

    # ==================== 技能操作 ====================

    async def save_skill(self, name: str, description: str, trigger_keywords: str = "") -> int:
        def _save():
            cursor = self._conn.execute(
                "INSERT OR REPLACE INTO skills (name, description, trigger_keywords) VALUES (?, ?, ?)",
                (name, description, trigger_keywords),
            )
            self._conn.commit()
            return cursor.lastrowid
        async with self._write_lock:
            return await asyncio.to_thread(_save)

    async def get_skills(self, active_only: bool = True) -> list[dict]:
        def _get():
            sql = "SELECT id, name, description, trigger_keywords, is_active FROM skills"
            if active_only:
                sql += " WHERE is_active = TRUE"
            return self._conn.execute(sql).fetchall()
        rows = await asyncio.to_thread(_get)
        return [{"id": r[0], "name": r[1], "description": r[2], "trigger_keywords": r[3], "is_active": r[4]} for r in rows]

    async def delete_skill(self, name: str) -> bool:
        def _del():
            self._conn.execute("DELETE FROM skills WHERE name = ?", (name,))
            self._conn.commit()
            return self._conn.total_changes > 0
        async with self._write_lock:
            return await asyncio.to_thread(_del)

    # ==================== 任务操作 ====================

    async def save_task(self, title: str, description: str = "", priority: int = 0, due_at: str | None = None) -> int:
        def _save():
            cursor = self._conn.execute(
                "INSERT INTO tasks (title, description, priority, due_at) VALUES (?, ?, ?, ?)",
                (title, description, priority, due_at),
            )
            self._conn.commit()
            return cursor.lastrowid
        async with self._write_lock:
            return await asyncio.to_thread(_save)

    async def get_tasks(self, status: str | None = None, limit: int = 20) -> list[dict]:
        def _get():
            if status:
                rows = self._conn.execute(
                    "SELECT id, title, description, status, priority, due_at, created_at FROM tasks WHERE status = ? ORDER BY priority DESC, id DESC LIMIT ?",
                    (status, limit),
                ).fetchall()
            else:
                rows = self._conn.execute(
                    "SELECT id, title, description, status, priority, due_at, created_at FROM tasks ORDER BY priority DESC, id DESC LIMIT ?",
                    (limit,),
                ).fetchall()
            return [{"id": r[0], "title": r[1], "description": r[2], "status": r[3], "priority": r[4], "due_at": r[5], "created_at": r[6]} for r in rows]
        return await asyncio.to_thread(_get)

    async def update_task_status(self, task_id: int, status: str) -> None:
        def _update():
            extra = ", completed_at = CURRENT_TIMESTAMP" if status == "done" else ""
            self._conn.execute(f"UPDATE tasks SET status = ?{extra} WHERE id = ?", (status, task_id))
            self._conn.commit()
        async with self._write_lock:
            await asyncio.to_thread(_update)

    async def delete_task(self, task_id: int) -> None:
        def _del():
            self._conn.execute("DELETE FROM tasks WHERE id = ?", (task_id,))
            self._conn.commit()
        async with self._write_lock:
            await asyncio.to_thread(_del)

    # ==================== 定时器操作 ====================

    async def save_timer(
        self, name: str, action: str,
        trigger_at: str | None = None,
        cron_expression: str | None = None,
        is_recurring: bool = False,
    ) -> int:
        def _save():
            cursor = self._conn.execute(
                "INSERT INTO timers (name, action, trigger_at, cron_expression, is_recurring) VALUES (?, ?, ?, ?, ?)",
                (name, action, trigger_at, cron_expression, is_recurring),
            )
            self._conn.commit()
            return cursor.lastrowid
        async with self._write_lock:
            return await asyncio.to_thread(_save)

    async def get_active_timers(self) -> list[dict]:
        def _get():
            return self._conn.execute(
                "SELECT id, name, action, trigger_at, cron_expression, is_recurring, last_triggered_at FROM timers WHERE is_active = TRUE",
            ).fetchall()
        rows = await asyncio.to_thread(_get)
        return [{"id": r[0], "name": r[1], "action": r[2], "trigger_at": r[3], "cron": r[4], "recurring": r[5], "last_triggered": r[6]} for r in rows]

    async def get_triggered_timers(self) -> list[dict]:
        """获取所有已到期的活跃定时器（含一次性和周期性）"""
        def _get():
            return self._conn.execute(
                "SELECT id, name, action, trigger_at, cron_expression, is_recurring FROM timers WHERE is_active = TRUE AND trigger_at <= datetime('now', 'localtime')",
            ).fetchall()
        rows = await asyncio.to_thread(_get)
        return [{"id": r[0], "name": r[1], "action": r[2], "trigger_at": r[3], "cron": r[4], "recurring": r[5]} for r in rows]

    async def deactivate_timer(self, timer_id: int) -> None:
        def _update():
            self._conn.execute("UPDATE timers SET is_active = FALSE, last_triggered_at = CURRENT_TIMESTAMP WHERE id = ?", (timer_id,))
            self._conn.commit()
        async with self._write_lock:
            await asyncio.to_thread(_update)

    async def delete_timer(self, timer_id: int) -> None:
        def _del():
            self._conn.execute("DELETE FROM timers WHERE id = ?", (timer_id,))
            self._conn.commit()
        async with self._write_lock:
            await asyncio.to_thread(_del)

    async def reschedule_timer(self, timer_id: int, next_trigger_at: str) -> None:
        """更新周期任务的下次触发时间（并记录本次触发时间）"""
        def _update():
            self._conn.execute(
                "UPDATE timers SET trigger_at = ?, last_triggered_at = CURRENT_TIMESTAMP WHERE id = ?",
                (next_trigger_at, timer_id),
            )
            self._conn.commit()
        async with self._write_lock:
            await asyncio.to_thread(_update)

    # ==================== 管理面板 API ====================

    async def admin_get_all_users(self) -> list[dict]:
        """获取所有用户列表（含统计）"""
        def _get():
            rows = self._conn.execute("""
                SELECT user_id, platform, COUNT(*) as msg_count,
                       MAX(created_at) as last_active
                FROM messages
                GROUP BY user_id, platform
                ORDER BY last_active DESC
            """).fetchall()
            return [
                {"user_id": r[0], "platform": r[1], "msg_count": r[2], "last_active": r[3]}
                for r in rows
            ]
        return await asyncio.to_thread(_get)

    async def admin_get_messages(
        self, user_id: str, limit: int = 50, offset: int = 0
    ) -> list[dict]:
        """分页获取用户消息"""
        def _get():
            rows = self._conn.execute("""
                SELECT id, user_id, platform, role, content,
                       display_content, tool_calls, created_at, is_archived
                FROM messages
                WHERE user_id = ?
                ORDER BY created_at DESC
                LIMIT ? OFFSET ?
            """, (user_id, limit, offset)).fetchall()
            return [
                {
                    "id": r[0], "user_id": r[1], "platform": r[2], "role": r[3],
                    "content": r[4], "display_content": r[5], "tool_calls": r[6],
                    "created_at": r[7], "is_archived": r[8],
                }
                for r in rows
            ]
        return await asyncio.to_thread(_get)

    async def admin_get_stats(self) -> dict:
        """获取仪表盘统计数据"""
        def _get():
            c = self._conn
            total_msgs = c.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
            total_topics = c.execute("SELECT COUNT(*) FROM topics").fetchone()[0]
            total_users = c.execute(
                "SELECT COUNT(DISTINCT user_id) FROM messages"
            ).fetchone()[0]
            today_msgs = c.execute(
                "SELECT COUNT(*) FROM messages WHERE date(created_at) = date('now')"
            ).fetchone()[0]
            total_notes = c.execute("SELECT COUNT(*) FROM notebook").fetchone()[0]
            total_timers = c.execute(
                "SELECT COUNT(*) FROM timers WHERE is_active = TRUE"
            ).fetchone()[0]
            return {
                "total_messages": total_msgs,
                "total_topics": total_topics,
                "total_users": total_users,
                "today_messages": today_msgs,
                "total_notes": total_notes,
                "active_timers": total_timers,
            }
        return await asyncio.to_thread(_get)

    async def admin_get_all_notes(self) -> list[dict]:
        """获取所有笔记"""
        def _get():
            rows = self._conn.execute("""
                SELECT id, owner_id, title, content, tags, created_at, updated_at
                FROM notebook
                ORDER BY updated_at DESC
            """).fetchall()
            return [
                {
                    "id": r[0], "owner_id": r[1], "title": r[2], "content": r[3],
                    "tags": r[4], "created_at": r[5], "updated_at": r[6],
                }
                for r in rows
            ]
        return await asyncio.to_thread(_get)

