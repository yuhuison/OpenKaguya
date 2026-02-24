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


def create_api_routes(db: Database, consciousness=None, engine=None) -> web.RouteTableDef:
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

    # ==================== 计划日程 ====================

    @routes.get("/api/timers")
    async def get_timers(request: web.Request) -> web.Response:
        timers = await db.get_active_timers()
        return web.json_response(timers)

    @routes.post("/api/timers/{timer_id}/trigger")
    async def trigger_timer(request: web.Request) -> web.Response:
        """手动提前触发一个计划任务"""
        timer_id = int(request.match_info["timer_id"])
        # 从 DB 获取 timer 信息
        timers = await db.get_active_timers()
        timer = next((t for t in timers if t["id"] == timer_id), None)
        if not timer:
            return web.json_response({"error": "任务不存在或已完成"}, status=404)

        is_recurring = timer.get("recurring", False)
        repeat_pattern = timer.get("cron") or "none"

        if is_recurring and repeat_pattern != "none" and consciousness:
            # 周期任务：重新调度到下次
            next_time = consciousness._calc_next_trigger(
                timer["trigger_at"], repeat_pattern
            )
            await db.reschedule_timer(timer_id, next_time)
        else:
            # 一次性任务：直接停用
            await db.deactivate_timer(timer_id)

        # 触发唤醒
        if consciousness:
            import asyncio
            asyncio.create_task(consciousness._execute_task_wake(timer))
            logger.info(f"🔔 管理面板手动触发任务: [{timer['name']}]")
            return web.json_response({"ok": True, "message": f"已触发: {timer['name']}"})
        else:
            return web.json_response({"error": "意识系统未连接"}, status=503)

    @routes.delete("/api/timers/{timer_id}")
    async def delete_timer(request: web.Request) -> web.Response:
        """删除一个计划任务"""
        timer_id = int(request.match_info["timer_id"])
        await db.delete_timer(timer_id)
        return web.json_response({"ok": True, "message": f"任务 {timer_id} 已删除"})

    @routes.post("/api/wake")
    async def manual_wake(request: web.Request) -> web.Response:
        """手动唤醒辉夜姬的主动意识"""
        if not consciousness:
            return web.json_response({"error": "意识系统未连接"}, status=503)
        import asyncio
        asyncio.create_task(consciousness._wake_up())
        logger.info("🌙 管理面板手动唤醒意识")
        return web.json_response({"ok": True, "message": "已触发意识唤醒"})

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
    # ==================== 测试聊天 ====================

    @routes.post("/api/test/send")
    async def test_send(request: web.Request) -> web.Response:
        """
        测试聊天接口：发送消息给辉夜姬，返回所有回复。

        Body: {content, image_base64?, file_base64?, filename?}
        Response: {responses: [{text, image_url?, file_url?}]}
        """
        if not engine:
            return web.json_response({"error": "引擎未连接"}, status=503)

        import uuid
        import base64
        from kaguya.core.types import Platform, UnifiedMessage, UserInfo, Attachment

        try:
            body = await request.json()
        except Exception:
            return web.json_response({"error": "无效的 JSON"}, status=400)

        content = body.get("content", "").strip()
        image_b64 = body.get("image_base64")
        file_b64 = body.get("file_base64")
        filename = body.get("filename", "uploaded_file")

        if not content and not image_b64 and not file_b64:
            return web.json_response({"error": "消息内容不能为空"}, status=400)

        # 构建附件
        attachments = []
        if image_b64:
            attachments.append(Attachment(
                type="image",
                data=image_b64,
                filename="test_image.jpg",
            ))
            if not content:
                content = "[用户发送了图片]"
        if file_b64:
            attachments.append(Attachment(
                type="file",
                data=file_b64,
                filename=filename,
            ))
            if not content:
                content = f"[用户发送了文件: {filename}]"

        # 构建 UnifiedMessage
        message = UnifiedMessage(
            message_id=str(uuid.uuid4()),
            platform=Platform.WEB,
            sender=UserInfo(
                user_id="admin_test",
                nickname="管理员",
                platform=Platform.WEB,
            ),
            content=content,
            attachments=attachments,
        )

        # 收集回复
        responses = []

        async def _collect_callback(
            text: str, image_path: str | None = None,
            file_path: str | None = None, **_
        ):
            resp_item = {"text": text or ""}
            if image_path:
                resp_item["image_url"] = f"/api/test/file?path={image_path}"
            if file_path:
                from pathlib import Path as _P
                resp_item["file_url"] = f"/api/test/file?path={file_path}"
                resp_item["file_name"] = _P(file_path).name
            responses.append(resp_item)

        try:
            await engine.handle_message(message, send_callback=_collect_callback)
        except Exception as e:
            logger.error(f"测试聊天处理失败: {e}")
            return web.json_response({"error": str(e)}, status=500)

        return web.json_response({"responses": responses})

    @routes.get("/api/test/file")
    async def test_serve_file(request: web.Request) -> web.Response:
        """读取本地文件并返回（用于展示 AI 回复中的图片/文件）"""
        from pathlib import Path as _P
        import mimetypes

        file_path = request.query.get("path", "")
        if not file_path:
            return web.json_response({"error": "missing path"}, status=400)

        path = _P(file_path).resolve()

        # 安全检查：只允许访问 workspace / data / temp 目录
        allowed_roots = [
            _P(DATA_DIR).resolve(),
            _P.cwd().resolve(),
        ]
        if not any(str(path).startswith(str(root)) for root in allowed_roots):
            return web.json_response({"error": "路径不允许"}, status=403)

        if not path.exists() or not path.is_file():
            return web.json_response({"error": "文件不存在"}, status=404)

        content_type, _ = mimetypes.guess_type(str(path))
        content_type = content_type or "application/octet-stream"

        return web.FileResponse(path, headers={
            "Content-Type": content_type,
            "Content-Disposition": f'inline; filename="{path.name}"',
        })

    return routes
