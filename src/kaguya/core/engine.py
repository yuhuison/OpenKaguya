"""ChatEngine — 对话主循环（V2 简化版）。

单一职责：
  1. 组装 system prompt（人格 + 记忆上下文 + 当前时间 + 可选 avatar 图片）
  2. 执行 LLM 多轮 tool calling 循环（最多 MAX_TOOL_ROUNDS 次）
  3. 将对话内容同步到 RecursiveMemory
"""

from __future__ import annotations

import asyncio
import json
import re
import uuid
from collections import deque
from datetime import datetime
from typing import TYPE_CHECKING, Any, Optional

from loguru import logger

from kaguya.config import PersonaConfig
from kaguya.core.memory import RecursiveMemory
from kaguya.llm.client import LLMClient

if TYPE_CHECKING:
    from kaguya.core.router import ToolRouter
    from kaguya.tools.avatar import AvatarManager
    from kaguya.tools.task import TaskTracker

MAX_TOOL_ROUNDS = 100


class InteractionLogger:
    """记录每次交互的完整流程，用于调试。"""

    def __init__(self, max_sessions: int = 50):
        self._sessions: deque[dict] = deque(maxlen=max_sessions)
        self._active: dict[str, dict] = {}

    def start_session(self, trigger: str) -> str:
        sid = uuid.uuid4().hex[:8]
        self._active[sid] = {
            "id": sid,
            "trigger": trigger,
            "started_at": datetime.now().isoformat(timespec="seconds"),
            "events": [],
        }
        return sid

    def log(self, session_id: str | None, event_type: str, content: str, **extra) -> None:
        if not session_id:
            return
        session = self._active.get(session_id)
        if not session:
            return
        event = {
            "time": datetime.now().strftime("%H:%M:%S"),
            "type": event_type,
            "content": content,
        }
        event.update(extra)
        session["events"].append(event)

    def end_session(self, session_id: str) -> None:
        session = self._active.pop(session_id, None)
        if session and session["events"]:
            self._sessions.append(session)

    def get_sessions(self, limit: int = 50) -> list[dict]:
        return list(reversed(list(self._sessions)))[:limit]

    def clear(self) -> None:
        self._sessions.clear()
        self._active.clear()


class ChatEngine:
    """辉夜姬对话引擎。"""

    def __init__(
        self,
        llm: LLMClient,
        memory: RecursiveMemory,
        router: "ToolRouter",
        persona: PersonaConfig,
        avatar_manager: Optional["AvatarManager"] = None,
        task_tracker: Optional["TaskTracker"] = None,
    ):
        self.llm = llm
        self.memory = memory
        self.router = router
        self.persona = persona
        self.avatar_manager = avatar_manager
        self.task_tracker = task_tracker
        self._lock = asyncio.Lock()
        self.interaction_log = InteractionLogger()

    # ------------------------------------------------------------------
    # 公开接口
    # ------------------------------------------------------------------

    async def handle_message(
        self,
        content: str,
        images: Optional[list[str]] = None,  # base64 图像列表
        sender_name: str = "用户",
    ) -> str:
        """处理一条用户消息，返回 AI 回复文字。"""
        async with self._lock:
            sid = self.interaction_log.start_session("chat")
            self.interaction_log.log(sid, "user_message", content)
            try:
                return await self._process(
                    content, images, sender_name, session_id=sid, stage="chat",
                )
            finally:
                self.interaction_log.end_session(sid)

    _TRIGGER_TO_STAGE: dict[str, str] = {
        "heartbeat": "consciousness",
        "notification": "notification",
        "timer": "consciousness",
        "extension": "chat",
    }

    async def handle_consciousness(
        self,
        prompt: str,
        trigger: str = "consciousness",
        pre_activate_groups: list[str] | None = None,
    ) -> str:
        """主动意识唤醒入口（加锁，防止与 handle_message 并发冲突）。"""
        stage = self._TRIGGER_TO_STAGE.get(trigger, "chat")
        async with self._lock:
            sid = self.interaction_log.start_session(trigger)
            self.interaction_log.log(sid, "system", prompt[:500])
            try:
                return await self._process(
                    prompt, sender_name="[系统唤醒]", session_id=sid,
                    pre_activate_groups=pre_activate_groups, stage=stage,
                )
            finally:
                self.interaction_log.end_session(sid)

    # ------------------------------------------------------------------
    # 内部处理
    # ------------------------------------------------------------------

    async def handle_message_stream(
        self,
        content: str,
        images: Optional[list[str]] = None,
        sender_name: str = "用户",
        on_event=None,  # async callable receiving event dicts
    ) -> str:
        """流式处理消息，实时通过 on_event 推送中间事件，返回最终回复。"""
        async with self._lock:
            sid = self.interaction_log.start_session("chat")
            self.interaction_log.log(sid, "user_message", content)
            try:
                return await self._process(
                    content, images, sender_name,
                    session_id=sid, on_event=on_event, stage="chat",
                )
            finally:
                self.interaction_log.end_session(sid)

    async def _process(
        self,
        content: str,
        images: Optional[list[str]] = None,
        sender_name: str = "用户",
        session_id: str | None = None,
        on_event=None,  # async callable for streaming events
        pre_activate_groups: list[str] | None = None,
        stage: str = "chat",
    ) -> str:
        # 0. 重置路由器状态 + 设置阶段 + 预激活
        self.router.reset()
        self.router.set_stage(stage)
        if pre_activate_groups:
            self.router.pre_activate(*pre_activate_groups)

        # 1. 构建消息历史（working memory + 本次输入）
        history = self._build_history(content, images)

        # 2. 构建 system 消息（纯文本）
        system_text = await self._build_system_prompt_text(stage=stage)

        messages: list[dict[str, Any]] = [
            {"role": "system", "content": system_text},
        ]

        # 3. avatar 图片注入到历史最前（user 消息），Qwen 不支持 system 含图片
        if self.avatar_manager:
            avatar_parts = self.avatar_manager.build_system_prompt_parts()
            if avatar_parts:
                messages += [
                    {"role": "user", "content": avatar_parts + [{"type": "text", "text": "这是你当前的形象。"}]},
                    {"role": "assistant", "content": "好的，我记住了。"},
                ]

        messages += history

        # 4. 多轮工具调用循环（含任务续发）
        reply = ""
        if self.task_tracker:
            self.task_tracker.reset()
        continuation_count = 0

        while True:
            reply = await self._tool_loop(messages, session_id, on_event)

            # 任务完成检查：如果没有 task_tracker 或任务已完成，正常退出
            if not self.task_tracker or not self.task_tracker.needs_continuation():
                break

            # 任务未完成，拼接续发消息
            continuation_count += 1
            tracker = self.task_tracker
            continuation_msg = (
                f"【系统提醒】你之前声明了任务「{tracker.task_name}」"
                f"（目标：{tracker.task_goal}），"
                f"当前进度：{tracker.status or '未更新'}。\n"
                f"该任务尚未完成（第 {continuation_count} 次提醒）。"
                f"请继续执行，或者如果确实无法完成，"
                f"请调用 mark_current_task_done(interrupted=true) 标记为中断。"
            )
            messages.append({"role": "user", "content": continuation_msg})
            self.interaction_log.log(
                session_id, "task_continuation",
                f"任务未完成，第 {continuation_count} 次续发",
            )
            logger.info(f"任务「{tracker.task_name}」未完成，第 {continuation_count} 次续发")

        # 4. 更新记忆
        await self.memory.add_message("user", f"{sender_name}: {content}")
        if reply:
            await self.memory.add_message("assistant", reply)

        return reply

    async def _tool_loop(
        self,
        messages: list[dict[str, Any]],
        session_id: str | None = None,
        on_event=None,
    ) -> str:
        """执行多轮工具调用循环，返回最终文字回复。"""
        reply = ""
        for _ in range(MAX_TOOL_ROUNDS):
            # 每轮重新获取（gateway 可能刚激活新组）
            active_tools = self.router.get_active_tools()
            response = await self.llm.chat(messages, tools=active_tools)
            raw_content = response.get("content", "")
            tool_calls = response.get("tool_calls", [])

            # 添加 assistant 消息到本轮 messages（保留原始内容）
            assistant_msg: dict[str, Any] = {"role": "assistant", "content": raw_content}
            if response.get("raw_tool_calls"):
                assistant_msg["tool_calls"] = response["raw_tool_calls"]
            messages.append(assistant_msg)

            # 去除 Qwen <think>...</think> 标签（仅影响显示，不影响消息历史）
            content_text = re.sub(r"</?think>", "", raw_content).strip()

            if not tool_calls:
                reply = content_text
                if reply:
                    self.interaction_log.log(session_id, "ai_text", reply)
                break

            # 中间思考文本
            if content_text:
                self.interaction_log.log(session_id, "ai_text", content_text)
                if on_event:
                    await on_event({"type": "thinking", "text": content_text})

            # 执行所有工具调用
            for tc in tool_calls:
                tool_name = tc["name"]
                tool_args = tc["arguments"] if isinstance(tc["arguments"], dict) else {}
                tool_id = tc["id"]

                args_str = json.dumps(tool_args, ensure_ascii=False)
                logger.info(f"工具调用: {tool_name}({args_str})")
                self.interaction_log.log(
                    session_id, "tool_call", f"{tool_name}({args_str})",
                )
                if on_event:
                    await on_event({
                        "type": "tool_call", "id": tool_id,
                        "name": tool_name, "args": tool_args,
                    })

                result = await self.router.execute_tool(tool_name, tool_args)

                # 记录工具结果（去掉 base64 图片数据）
                log_result = {k: v for k, v in result.items() if k != "image_base64"}
                if "image_base64" in result:
                    log_result["_image"] = f"[图片 {len(result['image_base64'])} chars]"
                result_log = json.dumps(log_result, ensure_ascii=False)
                if len(result_log) > 1000:
                    result_log = result_log[:1000] + "..."
                self.interaction_log.log(
                    session_id, "tool_result", result_log, tool_name=tool_name,
                )
                if on_event:
                    ev: dict = {"type": "tool_result", "id": tool_id, "name": tool_name}
                    if "image_base64" in result:
                        ev["image_base64"] = result["image_base64"]
                        ev["image_media_type"] = result.get(
                            "image_media_type", "image/jpeg",
                        )
                        ev["text"] = result.get("text", "")
                    else:
                        ev["text"] = result_log
                    await on_event(ev)

                # 构建工具结果消息（可能包含图像）
                tool_messages = self._build_tool_result_messages(
                    tool_id, tool_name, result,
                )
                messages.extend(tool_messages)
        else:
            # 超过最大轮数
            logger.warning(f"超过最大工具调用轮数 ({MAX_TOOL_ROUNDS})")
            if not reply:
                reply = "（处理超时，请稍后再试）"

        return reply

    def _build_history(
        self,
        new_content: str,
        images: Optional[list[str]] = None,
    ) -> list[dict[str, Any]]:
        """将 working memory 转换为 OpenAI 消息格式，并追加本次用户消息。"""
        msgs: list[dict[str, Any]] = []

        for m in self.memory.get_working_memory():
            msgs.append({"role": m["role"], "content": m["content"]})

        # 本次用户消息（可能含图像）
        if images:
            content_parts: list[dict] = [{"type": "text", "text": new_content}]
            for img_b64 in images:
                content_parts.append({
                    "type": "image_url",
                    "image_url": {"url": f"data:image/jpeg;base64,{img_b64}"},
                })
            msgs.append({"role": "user", "content": content_parts})
        else:
            msgs.append({"role": "user", "content": new_content})

        return msgs

    async def _build_system_prompt_text(self, stage: str = "chat") -> str:
        """组装 system prompt 纯文本：人格 + 记忆上下文 + 行为准则 + 扩展信息 + 当前时间。"""
        parts: list[str] = []

        # 人格定义
        p = self.persona
        persona_text = f"你是{p.name}。{p.description}"
        if p.personality:
            persona_text += f"\n性格：{p.personality}"
        if p.speaking_style:
            persona_text += f"\n说话风格：{p.speaking_style}"
        if p.interests:
            persona_text += f"\n兴趣：{'、'.join(p.interests)}"
        parts.append(persona_text)

        # 记忆上下文
        memory_ctx = await self.memory.build_context()
        if memory_ctx:
            parts.append(memory_ctx)

        # 行为准则
        chat_guidelines = p.get_guidelines("chat")
        if chat_guidelines:
            parts.append(f"【行为准则】\n{chat_guidelines}")

        # 扩展 Prompt 注入
        if self.router._extension_manager:
            from kaguya.extensions.base import Stage
            try:
                ext_prompts = await self.router._extension_manager.get_all_prompts(
                    Stage(stage),
                )
                for ep in ext_prompts:
                    if ep.strip():
                        parts.append(ep)
            except (ValueError, Exception):
                pass

        # 当前时间
        now = datetime.now().strftime("%Y年%m月%d日 %H:%M")
        parts.append(f"当前时间：{now}")

        return "\n\n".join(parts)

    def _build_tool_result_messages(
        self,
        tool_id: str,
        tool_name: str,
        result: dict[str, Any],
    ) -> list[dict[str, Any]]:
        """构建工具结果消息。统一处理含图像的结果（image_base64 字段）。"""
        if "image_base64" in result:
            # 多模态结果：图像 + 文字
            img_b64 = result["image_base64"]
            media_type = result.get("image_media_type", "image/jpeg")
            # 提取文字说明（优先用 text 字段，否则用 elements_text 等）
            text = result.get("text") or result.get("elements_text", "")
            if not text:
                # 排除图像字段后 dump 其余内容
                extra = {
                    k: v for k, v in result.items()
                    if k not in ("image_base64", "image_media_type")
                }
                text = json.dumps(extra, ensure_ascii=False, indent=2)
            return [{
                "role": "tool",
                "tool_call_id": tool_id,
                "content": [
                    {
                        "type": "image_url",
                        "image_url": {"url": f"data:{media_type};base64,{img_b64}"},
                    },
                    {"type": "text", "text": text},
                ],
            }]

        # 纯文本结果
        content = json.dumps(result, ensure_ascii=False, indent=2)
        return [{"role": "tool", "tool_call_id": tool_id, "content": content}]
