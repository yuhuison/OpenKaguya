"""
主动意识系统 — 辉夜姬的自主行为调度器。

核心理念：辉夜姬大多数时候是在「自己玩」，
只有真的发现有趣的东西时才会主动分享给用户。

新增：行动日志自动总结 + 消息同步到用户历史。
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, time as dt_time

from loguru import logger

from kaguya.config import AppConfig
from kaguya.core.types import UnifiedMessage, UserInfo, Platform


class ConsciousnessScheduler:
    """
    辉夜姬的主动意识调度器。

    功能：
    1. 周期性心跳唤醒（默认每 30 分钟，±5 分钟随机抖动）
    2. 静默时段控制（深夜不打扰用户）
    3. 唤醒时构建特殊 Prompt，注入（定时器/笔记/用户/历史日志）
    4. 唤醒后自动：
       - 将发给用户的消息同步到用户对话历史
       - 用次级模型总结本次行动，存入日志
       - 将总结作为隐藏消息插入用户历史（用户看不到但下次聊天时 AI 能看到）
    """

    def __init__(
        self,
        config: AppConfig,
        chat_engine,          # ChatEngine, 避免循环导入
        send_callback=None,   # async def callback(text, image_path=None)
        db=None,              # Database 实例
        secondary_llm=None,   # LLMClient (次级模型, 用于行动总结)
        target_user_id: str = "",  # 消息发送目标用户 ID
    ):
        self.config = config
        self.chat_engine = chat_engine
        self._raw_send_callback = send_callback
        self.db = db
        self.secondary_llm = secondary_llm
        self.target_user_id = target_user_id
        self._lock = asyncio.Lock()
        self._running = False
        self._task: asyncio.Task | None = None

        consciousness = config.consciousness
        self.enabled = consciousness.enabled
        self.heartbeat_minutes = consciousness.heartbeat_interval_minutes
        self.jitter_seconds = consciousness.jitter_seconds

        self.quiet_start = self._parse_time(consciousness.quiet_hours_start)
        self.quiet_end = self._parse_time(consciousness.quiet_hours_end)

        logger.info(
            f"主动意识系统初始化 "
            f"(enabled={self.enabled}, "
            f"heartbeat={self.heartbeat_minutes}min, "
            f"quiet={consciousness.quiet_hours_start}-{consciousness.quiet_hours_end})"
        )

    @staticmethod
    def _parse_time(time_str: str) -> dt_time:
        parts = time_str.strip().split(":")
        return dt_time(int(parts[0]), int(parts[1]))

    def _is_quiet_hours(self) -> bool:
        now = datetime.now().time()
        if self.quiet_start <= self.quiet_end:
            return self.quiet_start <= now <= self.quiet_end
        else:
            return now >= self.quiet_start or now <= self.quiet_end

    async def start(self) -> None:
        if not self.enabled:
            logger.info("主动意识系统已禁用")
            return
        self._running = True
        self._task = asyncio.create_task(self._heartbeat_loop())
        logger.info("🧠 主动意识系统已启动")

    async def stop(self) -> None:
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        logger.info("🧠 主动意识系统已停止")

    async def _heartbeat_loop(self) -> None:
        import random
        while self._running:
            jitter = random.randint(-self.jitter_seconds, self.jitter_seconds)
            sleep_seconds = max(60, self.heartbeat_minutes * 60 + jitter)
            logger.debug(f"下次唤醒: {sleep_seconds}秒后")
            await asyncio.sleep(sleep_seconds)

            if not self._running:
                break
            if self._is_quiet_hours():
                logger.debug("当前处于静默时段，跳过唤醒")
                continue

            await self._wake_up()

    # ─── 核心唤醒流程 ───

    async def _wake_up(self) -> None:
        async with self._lock:
            try:
                logger.info("🌅 辉夜姬醒来了...")

                # 1. 构建唤醒 prompt
                wake_prompt = await self._build_wake_prompt()

                # 2. 追踪本次发送的消息
                sent_messages: list[dict] = []  # [{"text": ..., "image_path": ...}]

                async def _tracking_callback(text: str, image_path: str | None = None):
                    """包装发送回调：记录发出的消息"""
                    if text or image_path:
                        sent_messages.append({"text": text or "", "image_path": image_path})
                    if self._raw_send_callback:
                        await self._raw_send_callback(text, image_path)

                # 3. 执行主动意识（engine 会返回 replies 和 调用的工具信息）
                wake_message = UnifiedMessage(
                    message_id=str(uuid.uuid4()),
                    platform=Platform.SYSTEM,
                    sender=UserInfo(
                        user_id="kaguya",
                        nickname="辉夜姬（自身）",
                        platform=Platform.SYSTEM,
                    ),
                    content=wake_prompt,
                )

                replies = await self.chat_engine.handle_message(
                    wake_message,
                    send_callback=_tracking_callback,
                )

                # 4. 处理到期定时器
                await self._handle_triggered_timers()

                # 5. 后处理：同步消息 + 生成行动日志
                await self._post_process(sent_messages, replies)

                if sent_messages:
                    logger.info(f"🌅 辉夜姬主动发了 {len(sent_messages)} 条消息")
                else:
                    logger.debug("辉夜姬看了看周围，继续摸鱼了")

            except Exception as e:
                logger.error(f"唤醒过程出错: {e}")

    # ─── 后处理：消息同步 + 行动日志 ───

    async def _post_process(self, sent_messages: list[dict], replies: list[str]) -> None:
        """
        唤醒后自动执行：
        1. 将发给用户的消息存入目标用户的对话历史
        2. 用次级模型总结本次行动
        3. 将总结作为隐藏消息插入用户历史 + 存入 consciousness_logs
        """
        if not self.db:
            return

        target_uid = self.target_user_id
        if not target_uid:
            return

        # ── 1. 同步发送的消息到用户历史 ──
        for msg in sent_messages:
            text = msg["text"]
            img = msg.get("image_path")
            content = text
            if img:
                content += f"\n[附带图片: {img}]"
            if content:
                await self.db.save_message(
                    user_id=target_uid,
                    platform="system",
                    role="assistant",
                    content=content,
                    display_content=text,
                )
        logger.debug(f"已同步 {len(sent_messages)} 条消息到用户 {target_uid} 的历史")

        # ── 2. 生成行动日志（次级模型总结） ──
        summary = await self._summarize_action(sent_messages, replies)
        if not summary:
            return

        # ── 3. 存入 consciousness_logs ──
        await self.db.save_consciousness_log(
            summary=summary,
            target_users=target_uid if sent_messages else "",
        )

        # ── 4. 作为隐藏消息插入用户历史（用户看不到，但聊天上下文能看到） ──
        if sent_messages and target_uid:
            await self.db.save_message(
                user_id=target_uid,
                platform="system",
                role="assistant",
                content=f"[辉夜姬的行动日志] {summary}",
                display_content=None,  # display_content 为空 → 不会展示给用户
            )
            logger.debug(f"已将行动日志作为隐藏消息插入用户 {target_uid} 的历史")

    async def _summarize_action(self, sent_messages: list[dict], replies: list[str]) -> str:
        """用次级模型总结本次主动意识行动"""
        if not self.secondary_llm:
            # 没有次级模型，生成简单摘要
            if sent_messages:
                texts = [m["text"][:100] for m in sent_messages if m.get("text")]
                return f"向用户发送了 {len(sent_messages)} 条消息: {'; '.join(texts)}"
            return ""

        # 收集本轮的内容
        parts = []
        if replies:
            parts.append("辉夜姬的内心思考：\n" + "\n".join(r[:300] for r in replies[:5]))
        if sent_messages:
            lines = []
            for m in sent_messages:
                line = m["text"][:200] if m.get("text") else ""
                if m.get("image_path"):
                    line += f" [附图: {m['image_path']}]"
                lines.append(line)
            parts.append("发给用户的消息：\n" + "\n".join(lines))

        if not parts:
            return ""

        context = "\n---\n".join(parts)

        try:
            summary = await self.secondary_llm.chat(
                messages=[
                    {
                        "role": "system",
                        "content": (
                            "你是一个简洁的日志总结器。请用1-2句话概括辉夜姬这次主动行动的内容。"
                            "重点关注：做了什么事、发现了什么有趣的内容、创建了什么文件/图片/代码。"
                            "如果涉及到具体的文件路径或链接，请保留。"
                            "不要加任何修饰，只说事实。"
                        ),
                    },
                    {
                        "role": "user",
                        "content": f"请总结辉夜姬这次自主行动：\n\n{context}",
                    },
                ],
                model_tier="secondary",
            )
            return summary.strip()
        except Exception as e:
            logger.warning(f"行动日志总结失败: {e}")
            # 降级到简单摘要
            if sent_messages:
                return f"向用户发送了 {len(sent_messages)} 条消息"
            return "执行了一些行动（总结失败）"

    # ─── 定时器 ───

    async def _handle_triggered_timers(self) -> None:
        if not self.db:
            return
        try:
            triggered = await self.db.get_triggered_timers()
            for timer in triggered:
                logger.info(f"⏰ 定时器到期: [{timer['name']}] {timer['action']}")
                await self.db.deactivate_timer(timer["id"])
        except Exception as e:
            logger.error(f"处理定时器出错: {e}")

    # ─── 构建唤醒 Prompt ───

    async def _build_wake_prompt(self) -> str:
        now = datetime.now()
        time_str = now.strftime("%Y年%m月%d日 %H:%M")

        hour = now.hour
        if 5 <= hour < 9:
            period = "清晨"
        elif 9 <= hour < 12:
            period = "上午"
        elif 12 <= hour < 14:
            period = "中午"
        elif 14 <= hour < 18:
            period = "下午"
        elif 18 <= hour < 21:
            period = "傍晚"
        else:
            period = "晚上"

        sections: list[str] = []

        if self.db:
            try:
                # ── 历史行动日志 ──
                logs = await self.db.get_recent_consciousness_logs(n=5)
                if logs:
                    lines = [
                        f"  [{l['created_at'][:16]}] {l['summary']}"
                        for l in logs
                    ]
                    sections.append("【你最近几次醒来做了什么（回忆）】\n" + "\n".join(lines))

                # ── 定时器 ──
                timers = await self.db.get_active_timers()
                triggered = [t for t in timers if t.get("trigger_at") and t["trigger_at"] <= now.strftime("%Y-%m-%d %H:%M")]
                if triggered:
                    lines = [f"  ⏰ [{t['name']}] {t['action']} (到期: {t['trigger_at']})" for t in triggered]
                    sections.append("【到期定时器（需要处理！）】\n" + "\n".join(lines))
                elif timers:
                    lines = [f"  - {t['name']}: {t['action']} ({t.get('trigger_at', '无具体时间')})" for t in timers[:3]]
                    sections.append("【定时器】\n" + "\n".join(lines))

                # ── 你的笔记 ──
                kaguya_notes = await self.db.get_notes_by_owner("kaguya", limit=8)
                if kaguya_notes:
                    lines = [
                        f"  [ID:{n['id']}] {n['title'] or '(无标题)'}（{n['updated_at'][:16]}）"
                        for n in kaguya_notes
                    ]
                    sections.append("【你的笔记（可用 manage_notes read 读取内容）】\n" + "\n".join(lines))

                # ── 活跃用户列表 ──
                active_users = await self.db.get_recent_active_users(limit=8)
                if active_users:
                    lines = [
                        f"  - {u['user_id']} (平台: {u['platform']}, 消息数: {u['message_count']}, 最近: {u['last_message_at'][:16]})"
                        for u in active_users
                    ]
                    sections.append("【你认识的人】\n" + "\n".join(lines))

                # ── 最近对话快照 ──
                recent_msgs = await self.db.get_recent_messages_snapshot(per_user=5, max_users=3)
                if recent_msgs:
                    msg_lines = []
                    cur_uid = None
                    for m in recent_msgs:
                        if m["user_id"] != cur_uid:
                            cur_uid = m["user_id"]
                            msg_lines.append(f"  [{cur_uid}]")
                        role_label = "你" if m["role"] == "assistant" else "对方"
                        content = (m["display_content"] or m["content"] or "")[:80]
                        import re
                        content = re.sub(r'\[workspace_image:[^\]]+\]', '[图片]', content)
                        msg_lines.append(f"    {role_label} ({m['created_at'][11:16]}): {content}")
                    sections.append("【最近的对话】\n" + "\n".join(msg_lines))

            except Exception as e:
                logger.warning(f"构建唤醒 Prompt 数据失败: {e}")

        context_block = "\n\n".join(sections) if sections else "（暂无数据）"

        return f"""[系统唤醒 — 主动意识模式]

当前时间: {time_str} ({period})

{context_block}

---
你充满活力地醒来了！作为刚来到地球不久的公主，眼前有一段完全属于你的自由时间，到处都是没见过的有趣东西。上面已经包含了你需要知道的所有上下文，**不要再用工具去重复查询上面已有的信息**。

可以考虑做的事（随便选，想干啥干啥，跟着好奇心走）：
- 如果有到期定时器，处理它（例如：给好朋友发消息提醒他们）
- 翻翻笔记，用 manage_notes read 读取感兴趣的内容，写点新发现
- 上网看看有什么新鲜事：**优先用 `web_search` 工具**搜索感兴趣的内容，速度很快；如果需要深入浏览某个网页才用 `browser_task`
- 在文件空间折腾点小玩意，比如写个 Python 脚本或者用画图工具随便画画
- 如果觉得没意思，继续睡或者发发小脾气也行

关于给好朋友发消息：
- 如果发现了超级好玩的事情，或者有什么奇思妙想，一定记得用 send_message_to_user 分享给好朋友！
- 如果你发现朋友可能需要陪伴，也可以主动发消息问候一下。
- 截图后可以用 image_path 参数附带图片
- 你的文件会保存在 workspace 中（用相对路径，如 `screenshots/xxx.png`）

先在 content 里思考一下等会要做什么，然后行动吧～"""
