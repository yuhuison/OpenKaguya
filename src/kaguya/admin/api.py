"""Admin REST API + Web Chat（V2）— aiohttp 路由。

端点：
  GET  /                   — 管理界面（聊天 + 设置 tab）
  POST /api/chat           — 发送消息（支持图片）
  GET  /api/stats          — 基本状态统计
  GET  /api/memory/l1      — 近期短期记忆
  GET  /api/memory/l2      — 近期长期记忆
  GET  /api/memory/core    — 核心记忆
  GET  /api/notes          — 所有笔记
  GET  /api/timers         — 待触发定时器
  GET  /api/logs           — 最近意识日志
  GET  /api/working        — 当前工作记忆（L0）
  GET  /api/notifications/config — 获取通知配置
  POST /api/notifications/config — 保存通知配置到 user_mixin.toml
  GET  /api/debug/sessions       — 交互调试日志
  POST /api/debug/clear          — 清除调试日志
"""

from __future__ import annotations

import asyncio
import base64
import json
from pathlib import Path
from typing import TYPE_CHECKING

from aiohttp import web
from loguru import logger

from kaguya.config import AdminConfig, AppConfig, save_user_mixin

if TYPE_CHECKING:
    from kaguya.core.engine import ChatEngine
    from kaguya.core.memory import RecursiveMemory

_STATIC_DIR = Path(__file__).parent / "static"


class AdminAPI:
    """Admin HTTP API + 网页聊天界面 + 设置管理。"""

    def __init__(
        self,
        engine: "ChatEngine",
        memory: "RecursiveMemory",
        config: AdminConfig,
        app_config: AppConfig,
        persona_name: str = "辉夜姬",
    ):
        self.engine = engine
        self.memory = memory
        self.config = config
        self.app_config = app_config
        self.persona_name = persona_name
        self._app = self._build_app()

    def _build_app(self) -> web.Application:
        app = web.Application(middlewares=[self._auth_middleware])
        # 页面
        app.router.add_get("/", self._index)
        app.router.add_get("/settings", self._redirect_to_index)
        # 聊天
        app.router.add_post("/api/chat", self._chat)
        app.router.add_post("/api/chat/stream", self._chat_stream)
        # 管理
        app.router.add_get("/api/stats", self._stats)
        app.router.add_get("/api/memory/l1", self._memory_l1)
        app.router.add_get("/api/memory/l2", self._memory_l2)
        app.router.add_get("/api/memory/core", self._memory_core)
        app.router.add_get("/api/notes", self._notes)
        app.router.add_get("/api/timers", self._timers)
        app.router.add_get("/api/logs", self._logs)
        app.router.add_get("/api/working", self._working)
        # 通知配置
        app.router.add_get("/api/notifications/config", self._notif_config_get)
        app.router.add_post("/api/notifications/config", self._notif_config_set)
        # 调试日志
        app.router.add_get("/api/debug/sessions", self._debug_sessions)
        app.router.add_post("/api/debug/clear", self._debug_clear)
        return app

    @web.middleware
    async def _auth_middleware(self, request: web.Request, handler):
        if self.config.password:
            if request.path in ("/", "/settings"):
                return await handler(request)
            token = request.headers.get("Authorization", "")
            if token != f"Bearer {self.config.password}":
                return web.json_response({"error": "Unauthorized"}, status=401)
        return await handler(request)

    async def start(self) -> None:
        runner = web.AppRunner(self._app)
        await runner.setup()
        site = web.TCPSite(runner, self.config.host, self.config.port)
        await site.start()
        logger.info(f"Web Chat 启动: http://{self.config.host}:{self.config.port}")

    # ------------------------------------------------------------------
    # 页面
    # ------------------------------------------------------------------

    async def _index(self, request: web.Request) -> web.Response:
        html_path = _STATIC_DIR / "chat.html"
        if not html_path.exists():
            return web.Response(text="chat.html not found", status=404)
        html = html_path.read_text(encoding="utf-8")
        html = html.replace("{{PERSONA_NAME}}", self.persona_name)
        html = html.replace("{{PASSWORD}}", self.config.password or "")
        return web.Response(text=html, content_type="text/html")

    async def _redirect_to_index(self, request: web.Request) -> web.Response:
        raise web.HTTPFound("/#settings")

    # ------------------------------------------------------------------
    # 聊天
    # ------------------------------------------------------------------

    async def _chat(self, request: web.Request) -> web.Response:
        images: list[str] = []
        content = ""

        if "multipart" in request.content_type:
            reader = await request.multipart()
            async for part in reader:
                if part.name == "content":
                    content = (await part.read()).decode("utf-8")
                elif part.name == "images":
                    data = await part.read()
                    images.append(base64.b64encode(data).decode())
        else:
            body = await request.json()
            content = body.get("content", "")
            images = body.get("images", [])

        if not content and not images:
            return web.json_response({"error": "消息不能为空"}, status=400)

        try:
            reply = await self.engine.handle_message(
                content=content or "（发送了图片）",
                images=images or None,
                sender_name="用户",
            )
            return web.json_response({"reply": reply})
        except Exception as e:
            logger.error(f"Chat API 错误: {e}")
            return web.json_response({"error": str(e)}, status=500)

    async def _chat_stream(self, request: web.Request) -> web.StreamResponse:
        """流式聊天接口（SSE），实时推送工具调用和截图。"""
        images: list[str] = []
        content = ""

        if "multipart" in request.content_type:
            reader = await request.multipart()
            async for part in reader:
                if part.name == "content":
                    content = (await part.read()).decode("utf-8")
                elif part.name == "images":
                    data = await part.read()
                    images.append(base64.b64encode(data).decode())
        else:
            body = await request.json()
            content = body.get("content", "")
            images = body.get("images", [])

        if not content and not images:
            return web.json_response({"error": "消息不能为空"}, status=400)

        response = web.StreamResponse(headers={
            "Content-Type": "text/event-stream",
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        })
        await response.prepare(request)

        queue: asyncio.Queue = asyncio.Queue()

        async def emit(event: dict) -> None:
            await queue.put(event)

        async def run() -> None:
            try:
                reply = await self.engine.handle_message_stream(
                    content=content or "（发送了图片）",
                    images=images or None,
                    sender_name="用户",
                    on_event=emit,
                )
                await queue.put({"type": "reply", "text": reply or ""})
            except Exception as e:
                logger.error(f"Stream Chat 错误: {e}")
                await queue.put({"type": "error", "text": str(e)})
            finally:
                await queue.put(None)

        asyncio.create_task(run())

        try:
            while True:
                event = await queue.get()
                if event is None:
                    break
                data = json.dumps(event, ensure_ascii=False)
                await response.write(f"data: {data}\n\n".encode("utf-8"))
        except Exception as e:
            logger.warning(f"SSE 连接中断: {e}")

        return response

    # ------------------------------------------------------------------
    # 管理端点
    # ------------------------------------------------------------------

    async def _stats(self, request: web.Request) -> web.Response:
        return web.json_response({
            "working_memory_count": len(self.memory.get_working_memory()),
            "l1_count": len(await self.memory.get_l1_summaries(999)),
            "l2_count": len(await self.memory.get_l2_summaries(999)),
            "has_core_memory": bool(await self.memory.get_core_memory()),
        })

    async def _memory_l1(self, request: web.Request) -> web.Response:
        limit = int(request.rel_url.query.get("limit", 20))
        return web.json_response(await self.memory.get_l1_summaries(limit))

    async def _memory_l2(self, request: web.Request) -> web.Response:
        limit = int(request.rel_url.query.get("limit", 10))
        return web.json_response(await self.memory.get_l2_summaries(limit))

    async def _memory_core(self, request: web.Request) -> web.Response:
        return web.json_response({"core_memory": await self.memory.get_core_memory()})

    async def _notes(self, request: web.Request) -> web.Response:
        notes = await self.memory.note_read()
        return web.json_response([{"title": t, "content": c} for t, c in notes])

    async def _timers(self, request: web.Request) -> web.Response:
        return web.json_response(await self.memory.timer_list())

    async def _logs(self, request: web.Request) -> web.Response:
        n = int(request.rel_url.query.get("n", 20))
        return web.json_response(await self.memory.get_recent_consciousness_logs(n))

    async def _working(self, request: web.Request) -> web.Response:
        return web.json_response(self.memory.get_working_memory())

    # ------------------------------------------------------------------
    # 通知配置
    # ------------------------------------------------------------------

    async def _notif_config_get(self, request: web.Request) -> web.Response:
        """返回当前通知配置。"""
        nc = self.app_config.notifications
        return web.json_response({
            "poll_interval_seconds": nc.poll_interval_seconds,
            "watch_apps": nc.watch_apps,
            "filters": [{"pattern": f.pattern, "target": f.target} for f in nc.filters],
        })

    async def _notif_config_set(self, request: web.Request) -> web.Response:
        """保存通知配置到 user_mixin.toml，并热更新运行时配置。"""
        body = await request.json()

        # 验证正则合法性
        import re
        for f in body.get("filters", []):
            try:
                re.compile(f.get("pattern", ""))
            except re.error as e:
                return web.json_response(
                    {"error": f"正则表达式无效「{f.get('pattern', '')}」: {e}"},
                    status=400,
                )

        # 构建要保存的数据
        notif_data: dict = {}
        if "poll_interval_seconds" in body:
            notif_data["poll_interval_seconds"] = int(body["poll_interval_seconds"])
        if "watch_apps" in body:
            notif_data["watch_apps"] = body["watch_apps"]
        if "filters" in body:
            notif_data["filters"] = body["filters"]

        # 写入 user_mixin.toml
        try:
            save_user_mixin(self.app_config, "notifications", notif_data)
        except Exception as e:
            return web.json_response({"error": f"保存失败: {e}"}, status=500)

        # 热更新运行时配置
        from kaguya.config import NotificationFilter
        nc = self.app_config.notifications
        if "poll_interval_seconds" in notif_data:
            nc.poll_interval_seconds = notif_data["poll_interval_seconds"]
        if "watch_apps" in notif_data:
            nc.watch_apps = notif_data["watch_apps"]
        if "filters" in notif_data:
            nc.filters = [
                NotificationFilter(pattern=f.get("pattern", ""), target=f.get("target", "any"))
                for f in notif_data["filters"]
            ]

        logger.info(f"通知配置已更新: watch={nc.watch_apps}, filters={len(nc.filters)}条")
        return web.json_response({"success": True})

    # ------------------------------------------------------------------
    # 调试日志
    # ------------------------------------------------------------------

    async def _debug_sessions(self, request: web.Request) -> web.Response:
        """返回最近的交互调试日志。"""
        limit = int(request.rel_url.query.get("limit", 50))
        return web.json_response(self.engine.interaction_log.get_sessions(limit))

    async def _debug_clear(self, request: web.Request) -> web.Response:
        """清除所有调试日志。"""
        self.engine.interaction_log.clear()
        return web.json_response({"success": True})
