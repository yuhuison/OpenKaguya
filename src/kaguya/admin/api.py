"""
管理面板 REST API — aiohttp 路由。
"""

from __future__ import annotations

import json
from pathlib import Path

from aiohttp import web
from loguru import logger

from kaguya.config import CONFIG_DIR, DATA_DIR
from kaguya.memory.database import Database

_EDITABLE_CONFIGS = {"default.toml", "persona.toml"}
_LOG_DIR = DATA_DIR / "logs"


def create_api_routes(db: Database) -> web.RouteTableDef:
    """创建 API 路由，绑定到数据库实例"""
    routes = web.RouteTableDef()

    # ==================== 系统统计 ====================

    @routes.get("/api/stats")
    async def get_stats(request: web.Request) -> web.Response:
        stats = await db.admin_get_stats()
        return web.json_response(stats)

    # ==================== 用户 & 消息 ====================

    @routes.get("/api/users")
    async def get_users(request: web.Request) -> web.Response:
        users = await db.admin_get_all_users()
        return web.json_response(users)

    @routes.get("/api/messages")
    async def get_messages(request: web.Request) -> web.Response:
        user_id = request.query.get("user_id", "")
        limit = int(request.query.get("limit", "50"))
        offset = int(request.query.get("offset", "0"))
        if not user_id:
            return web.json_response({"error": "missing user_id"}, status=400)
        messages = await db.admin_get_messages(user_id, limit, offset)
        return web.json_response(messages)

    # ==================== 话题 ====================

    @routes.get("/api/topics")
    async def get_topics(request: web.Request) -> web.Response:
        user_id = request.query.get("user_id", "")
        if not user_id:
            return web.json_response({"error": "missing user_id"}, status=400)
        topics = await db.get_all_topics(user_id)
        return web.json_response(topics)

    @routes.get("/api/topics/{topic_id}")
    async def get_topic_detail(request: web.Request) -> web.Response:
        topic_id = request.match_info["topic_id"]
        topic = await db.get_topic_by_id(topic_id)
        if not topic:
            return web.json_response({"error": "not found"}, status=404)
        return web.json_response(topic)

    @routes.get("/api/topics/{topic_id}/messages")
    async def get_topic_messages(request: web.Request) -> web.Response:
        topic_id = request.match_info["topic_id"]
        limit = int(request.query.get("limit", "50"))
        messages = await db.get_messages_by_topic(topic_id, limit=limit)
        return web.json_response(messages)

    # ==================== 笔记 ====================

    @routes.get("/api/notes")
    async def get_notes(request: web.Request) -> web.Response:
        notes = await db.admin_get_all_notes()
        return web.json_response(notes)

    # ==================== 意识日志 ====================

    @routes.get("/api/consciousness-logs")
    async def get_consciousness_logs(request: web.Request) -> web.Response:
        limit = int(request.query.get("limit", "20"))
        logs = await db.get_recent_consciousness_logs(n=limit)
        return web.json_response(logs)

    # ==================== 配置 ====================

    @routes.get("/api/config")
    async def get_config(request: web.Request) -> web.Response:
        result = {}
        for name in _EDITABLE_CONFIGS:
            path = CONFIG_DIR / name
            if path.exists():
                result[name] = path.read_text(encoding="utf-8")
        return web.json_response(result)

    @routes.put("/api/config/{filename}")
    async def update_config(request: web.Request) -> web.Response:
        filename = request.match_info["filename"]
        if filename not in _EDITABLE_CONFIGS:
            return web.json_response(
                {"error": f"不允许编辑 {filename}"}, status=403
            )
        try:
            body = await request.json()
            content = body.get("content", "")
            if not content:
                return web.json_response({"error": "内容为空"}, status=400)

            # 验证 TOML 语法
            import tomllib
            tomllib.loads(content)

            path = CONFIG_DIR / filename
            path.write_text(content, encoding="utf-8")
            logger.info(f"📝 管理面板更新配置: {filename}")
            return web.json_response({"ok": True, "message": f"{filename} 已保存"})
        except Exception as e:
            return web.json_response({"error": str(e)}, status=400)

    # ==================== 日志 ====================

    @routes.get("/api/logs")
    async def list_logs(request: web.Request) -> web.Response:
        if not _LOG_DIR.exists():
            return web.json_response([])
        files = sorted(
            [f.name for f in _LOG_DIR.iterdir() if f.suffix == ".log"],
            reverse=True,
        )
        return web.json_response(files)

    @routes.get("/api/logs/{filename}")
    async def get_log_content(request: web.Request) -> web.Response:
        filename = request.match_info["filename"]
        # 安全检查
        if "/" in filename or "\\" in filename or ".." in filename:
            return web.json_response({"error": "invalid filename"}, status=400)
        path = _LOG_DIR / filename
        if not path.exists():
            return web.json_response({"error": "not found"}, status=404)

        lines = int(request.query.get("lines", "300"))
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
            all_lines = text.splitlines()
            tail = all_lines[-lines:] if len(all_lines) > lines else all_lines
            return web.json_response({
                "filename": filename,
                "total_lines": len(all_lines),
                "lines": tail,
            })
        except Exception as e:
            return web.json_response({"error": str(e)}, status=500)

    return routes
