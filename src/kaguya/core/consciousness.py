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
        send_callback=None,   # async def callback(text, image_path=None, target_user_id=None)
        db=None,              # Database 实例
        secondary_llm=None,   # LLMClient (次级模型, 用于行动总结)
        adapters: list | None = None,  # PlatformAdapter 列表
        providers: list | None = None,  # BaseProvider 列表
    ):
        self.config = config
        self.chat_engine = chat_engine
        self._raw_send_callback = send_callback
        self.db = db
        self.secondary_llm = secondary_llm
        self._adapters = adapters or []
        self._providers = providers or []
        self._lock = asyncio.Lock()
        self._running = False
        self._task: asyncio.Task | None = None
        self._timer_task: asyncio.Task | None = None

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
        self._timer_task = asyncio.create_task(self._timer_check_loop())
        logger.info("🧠 主动意识系统已启动（含计划任务轮询）")

    async def stop(self) -> None:
        self._running = False
        for t in (self._task, self._timer_task):
            if t:
                t.cancel()
                try:
                    await t
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

    async def _timer_check_loop(self) -> None:
        """高频轮询计划任务（每 60 秒检查一次）"""
        while self._running:
            await asyncio.sleep(60)
            if not self._running or not self.db:
                continue
            try:
                triggered = await self.db.get_triggered_timers()
                for timer in triggered:
                    logger.info(f"⏰ 计划任务到期: [{timer['name']}] {timer['action']}")

                    is_recurring = timer.get("recurring", False)
                    repeat_pattern = timer.get("cron") or "none"

                    if is_recurring and repeat_pattern != "none":
                        # 周期任务：计算下次触发时间，更新 DB
                        next_time = self._calc_next_trigger(
                            timer["trigger_at"], repeat_pattern
                        )
                        await self.db.reschedule_timer(timer["id"], next_time)
                        logger.info(
                            f"🔁 周期任务已重新调度: [{timer['name']}] "
                            f"下次执行: {next_time}"
                        )
                    else:
                        # 一次性任务：直接停用
                        await self.db.deactivate_timer(timer["id"])

                    # 触发专属任务唤醒
                    await self._execute_task_wake(timer)
            except Exception as e:
                logger.error(f"计划任务检查出错: {e}")

    @staticmethod
    def _calc_next_trigger(current_trigger: str, repeat: str) -> str:
        """
        根据重复模式计算下次触发时间。

        支持的模式：
        - daily: 每天同一时间
        - weekdays: 工作日，跳过周六日
        - weekly: 每周同一天同一时间
        - monthly: 每月同一天
        - Xm: 每 X 分钟（如 '30m'）
        - Xh: 每 X 小时（如 '2h'）
        """
        import re as _re
        from datetime import timedelta

        try:
            base = datetime.strptime(current_trigger, "%Y-%m-%d %H:%M")
        except Exception:
            # 无法解析，默认 1 天后
            base = datetime.now()

        now = datetime.now()

        if repeat == "daily":
            next_t = base + timedelta(days=1)
            # 如果算出来的时间仍然在过去，持续加天
            while next_t <= now:
                next_t += timedelta(days=1)

        elif repeat == "weekdays":
            next_t = base + timedelta(days=1)
            while next_t <= now or next_t.weekday() >= 5:  # 5=Sat, 6=Sun
                next_t += timedelta(days=1)

        elif repeat == "weekly":
            next_t = base + timedelta(weeks=1)
            while next_t <= now:
                next_t += timedelta(weeks=1)

        elif repeat == "monthly":
            # 简单实现：同一天下月
            month = base.month + 1
            year = base.year
            if month > 12:
                month = 1
                year += 1
            day = min(base.day, 28)  # 安全处理月末
            next_t = base.replace(year=year, month=month, day=day)
            while next_t <= now:
                month = next_t.month + 1
                year = next_t.year
                if month > 12:
                    month = 1
                    year += 1
                next_t = next_t.replace(year=year, month=month)

        else:
            # 自定义间隔：'30m', '2h' 等
            m = _re.match(r'^(\d+)([mh])$', repeat)
            if m:
                value = int(m.group(1))
                unit = m.group(2)
                delta = timedelta(
                    minutes=value if unit == 'm' else 0,
                    hours=value if unit == 'h' else 0,
                )
                next_t = base + delta
                while next_t <= now:
                    next_t += delta
            else:
                # 无法识别的模式，默认 1 天
                next_t = now + timedelta(days=1)

        return next_t.strftime("%Y-%m-%d %H:%M")

    # ─── 核心唤醒流程 ───

    async def _wake_up(self) -> None:
        async with self._lock:
            try:
                logger.info("🌅 辉夜姬醒来了...")

                # 0. 注册 adapter 意识阶段工具
                for adapter in self._adapters:
                    tools = adapter.get_tools(phase="consciousness")
                    if tools:
                        self.chat_engine.tool_registry.register_all(tools)
                        logger.debug(f"已注册 {adapter.name} 意识阶段工具 ({len(tools)} 个)")

                # 0b. 注册 provider 意识阶段工具
                for prov in self._providers:
                    tools = prov.get_tools(phase="consciousness")
                    if tools:
                        self.chat_engine.tool_registry.register_all(tools)
                        logger.debug(f"已注册 {prov.name} 意识阶段工具 ({len(tools)} 个)")

                # 1. 构建唤醒 prompt
                wake_prompt = await self._build_wake_prompt()

                # 2. 追踪本次发送的消息（含目标用户）
                sent_messages: list[dict] = []  # [{"text": ..., "image_path": ..., "target_user_id": ...}]

                async def _tracking_callback(
                    text: str, image_path: str | None = None,
                    target_user_id: str | None = None,
                    file_path: str | None = None, **_
                ):
                    """包装发送回调：记录发出的消息及其目标用户"""
                    if text or image_path or file_path:
                        sent_messages.append({
                            "text": text or "",
                            "image_path": image_path,
                            "file_path": file_path,
                            "target_user_id": target_user_id or "",
                        })
                    if self._raw_send_callback:
                        await self._raw_send_callback(
                            text, image_path=image_path,
                            file_path=file_path,
                            target_user_id=target_user_id,
                        )

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

                # 4. 定时器已由 _timer_check_loop 独立处理，此处不再重复

                # 5. 后处理：同步消息 + 生成行动日志
                await self._post_process(sent_messages, replies)

                if sent_messages:
                    logger.info(f"🌅 辉夜姬主动发了 {len(sent_messages)} 条消息")
                else:
                    logger.debug("辉夜姬看了看周围，继续摸鱼了")

            except Exception as e:
                logger.error(f"唤醒过程出错: {e}")

            finally:
                # C5: 清理意识阶段专属工具
                for adapter in self._adapters:
                    try:
                        for tool in adapter.get_tools(phase="consciousness"):
                            self.chat_engine.tool_registry.unregister(tool.name)
                    except Exception:
                        pass
                for prov in self._providers:
                    try:
                        for tool in prov.get_tools(phase="consciousness"):
                            self.chat_engine.tool_registry.unregister(tool.name)
                    except Exception:
                        pass

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

        # 动态确定目标用户：优先从发送记录中提取，否则取最近活跃用户
        target_uids: set[str] = set()
        for msg in sent_messages:
            uid = msg.get("target_user_id", "")
            if uid:
                target_uids.add(uid)
        if not target_uids:
            # 没有明确目标，尝试用最近活跃用户
            try:
                active_users = await self.db.get_recent_active_users(limit=1)
                if active_users:
                    target_uids.add(active_users[0]["user_id"])
            except Exception:
                pass
        if not target_uids:
            return

        # 按目标用户分组同步消息
        for target_uid in target_uids:
            # 筛选该用户的消息
            user_msgs = [m for m in sent_messages if m.get("target_user_id") == target_uid]
            for msg in user_msgs:
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
            if user_msgs:
                logger.debug(f"已同步 {len(user_msgs)} 条消息到用户 {target_uid} 的历史")

        # 生成行动日志
        summary = await self._summarize_action(sent_messages, replies)
        if not summary:
            return

        # 存入 consciousness_logs
        await self.db.save_consciousness_log(
            summary=summary,
            target_users=",".join(target_uids) if sent_messages else "",
        )

        # 作为隐藏消息插入每个目标用户的历史
        if sent_messages:
            for target_uid in target_uids:
                await self.db.save_message(
                    user_id=target_uid,
                    platform="system",
                    role="assistant",
                    content=f"[辉夜姬的行动日志] {summary}",
                    display_content=None,
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
            # secondary_llm.chat() 可能返回 dict 或 str
            if isinstance(summary, dict):
                summary = summary.get("content", "") or str(summary)
            return summary.strip() if isinstance(summary, str) else str(summary)
        except Exception as e:
            logger.warning(f"行动日志总结失败: {e}")
            # 降级到简单摘要
            if sent_messages:
                return f"向用户发送了 {len(sent_messages)} 条消息"
            return "执行了一些行动（总结失败）"

    # ─── 计划任务唤醒 ───

    async def _execute_task_wake(self, timer: dict) -> None:
        """
        计划任务触发的专属唤醒。
        使用任务专注的 prompt，确保 AI 优先处理预定任务。
        """
        async with self._lock:
            try:
                logger.info(f"🎯 执行计划任务唤醒: {timer['name']}")

                # 注册意识阶段工具
                for adapter in self._adapters:
                    tools = adapter.get_tools(phase="consciousness")
                    if tools:
                        self.chat_engine.tool_registry.register_all(tools)
                for prov in self._providers:
                    tools = prov.get_tools(phase="consciousness")
                    if tools:
                        self.chat_engine.tool_registry.register_all(tools)

                now = datetime.now()
                time_str = now.strftime("%Y年%m月%d日 %H:%M")

                task_prompt = f"""
[OpenKaguya]
[系统唤醒 — 计划任务执行模式]

当前时间: {time_str}

━━ 你有一个预定任务需要立刻执行！ ━━

📌 任务名称: {timer['name']}
📋 任务内容: {timer['action']}
⏰ 计划时间: {timer.get('trigger_at', '未知')}

这是你之前主动计划的任务，现在时间到了，请立刻执行！

执行要求：
1. 仔细阅读任务内容，理解需要做什么
2. 如果任务涉及给某个用户发消息（如提醒），用 send_message_to_user 立刻发送
3. 任务完成后，如果你觉得还有时间，可以顺便做点别的有趣的事
4. 先在 content 中简要说明你要如何执行这个任务，然后立刻行动
"""

                # 追踪消息
                sent_messages: list[dict] = []

                async def _tracking_callback(
                    text: str, image_path: str | None = None,
                    target_user_id: str | None = None,
                    file_path: str | None = None, **_
                ):
                    if text or image_path or file_path:
                        sent_messages.append({
                            "text": text or "",
                            "image_path": image_path,
                            "file_path": file_path,
                            "target_user_id": target_user_id or "",
                        })
                    if self._raw_send_callback:
                        await self._raw_send_callback(
                            text, image_path=image_path,
                            file_path=file_path,
                            target_user_id=target_user_id,
                        )

                wake_message = UnifiedMessage(
                    message_id=str(uuid.uuid4()),
                    platform=Platform.SYSTEM,
                    sender=UserInfo(
                        user_id="kaguya",
                        nickname="辉夜姬（计划任务）",
                        platform=Platform.SYSTEM,
                    ),
                    content=task_prompt,
                )

                replies = await self.chat_engine.handle_message(
                    wake_message,
                    send_callback=_tracking_callback,
                )

                # 后处理
                await self._post_process(sent_messages, replies)

                if sent_messages:
                    logger.info(f"🎯 计划任务完成: {timer['name']}，发了 {len(sent_messages)} 条消息")
                else:
                    logger.info(f"🎯 计划任务完成: {timer['name']}（未发送消息）")

            except Exception as e:
                logger.error(f"计划任务执行出错: {timer['name']} — {e}")

            finally:
                for adapter in self._adapters:
                    try:
                        for tool in adapter.get_tools(phase="consciousness"):
                            self.chat_engine.tool_registry.unregister(tool.name)
                    except Exception:
                        pass
                for prov in self._providers:
                    try:
                        for tool in prov.get_tools(phase="consciousness"):
                            self.chat_engine.tool_registry.unregister(tool.name)
                    except Exception:
                        pass


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

                # ── 计划任务 ──
                timers = await self.db.get_active_timers()
                if timers:
                    lines = [f"  - {t['name']}: {t['action']} (执行时间: {t.get('trigger_at', '未设定')})" for t in timers[:5]]
                    sections.append("【待执行的计划任务】\n" + "\n".join(lines))

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

        # ── 注入 adapter prompts ──
        adapter_sections = []
        for adapter in self._adapters:
            try:
                sys_prompt = adapter.get_system_prompt(phase="consciousness")
                if sys_prompt:
                    adapter_sections.append(sys_prompt)
                injected = await adapter.get_injected_prompt(phase="consciousness")
                if injected:
                    adapter_sections.append(injected)
            except Exception as e:
                logger.warning(f"获取 {adapter.name} adapter prompt 失败: {e}")

        # ── 注入 provider prompts ──
        for prov in self._providers:
            try:
                sys_prompt = prov.get_system_prompt(phase="consciousness")
                if sys_prompt:
                    adapter_sections.append(sys_prompt)
                injected = await prov.get_injected_prompt(phase="consciousness")
                if injected:
                    adapter_sections.append(injected)
            except Exception as e:
                logger.warning(f"获取 {prov.name} provider prompt 失败: {e}")

        adapter_block = "\n\n".join(adapter_sections)
        if adapter_block:
            context_block += f"\n\n{adapter_block}"

        return f"""
[OpenKaguya]
[系统唤醒 — 主动意识模式]

当前时间: {time_str} ({period})

{context_block}

---
作为来自月球、充满无穷活力的宇宙公主，你刚刚在地球的机器里苏醒。
上面已经包含了你需要知道的核心上下文，**不要用工具去重复查询上面已有的信息**。

⚠️ 话题新鲜度规则：
仔细看【最近的对话】和【你最近几次醒来做了什么】，**严禁重复已经聊过的话题或已经分享过的内容**。你的好奇心是无限的，每次醒来都要发现全新的东西。

━━ 你的自由时间，到处去玩吧！ ━━

⏰ **定时器**：如果上面有到期定时器，优先处理！

🌍 **视察地球！去发现新鲜好玩的事！**（你最爱的活动！）
地球上每天都在发生无数有趣的事，作为充满好奇心的宇宙公主，你最擅长从中找乐子：
- 用 `web_search` 去搜不同领域的新鲜事（**必须和最近聊过的话题不同！**）
  方向参考（随便选一个感兴趣的）：
  · 最近在流行什么音乐/电影？有没有让人眼前一亮的新作品？
  · 科学界有什么让人惊掉下巴的新发现？太空探索有新进展吗？
  · 哪里有令人向往的活动、展览、演出？
  · 有什么小众但超酷的文化现象、艺术创作？
  · 今天发生了什么大新闻？有没有意想不到的冷知识？
  · 游戏、动漫、科技圈有什么新动态？
- 用 `web_read` 深入阅读感兴趣的文章，真正弄懂内容而不只是看标题
- 发现好东西后可以记到笔记里（`manage_notes write`），也可以直接分享给伙伴

🎨 **搞点创作、学点新东西**
- 想画画或生成有趣的图片？先 `use_toolkit("image")` 激活
- 想浏览网页、看视频网站？先 `use_toolkit("browser")` 激活
- 想写代码、跑脚本、学新技能？先 `use_toolkit("workspace")` 激活
  比如用 Python 画个有趣的图表、写首诗、做个小实验……什么都行！

📝 **翻翻笔记、记录灵感**
- 用 `manage_notes list` 看看之前记了什么，用 `read` 读取感兴趣的
- 如果之前留了"想继续研究"的笔记，这次可以接着深入
- 有新发现或灵感？随时用 `manage_notes write/append` 记下来

🪞 **偶尔也可以安静下来想想**
- 用 `search_memory_by_topic` 回味和伙伴的有趣互动
- 想想上次分享的东西，伙伴的反应如何？他喜欢什么类型的话题？
- 给自己写个小日记，记录今天的心情和感悟（`manage_notes write`, owner='kaguya'）

━━ 和伙伴分享 ━━
- 发现**真正好玩的东西**时，用 `send_message_to_user` 兴奋地分享给他！
- 不是每次醒来都必须发消息——如果没找到特别有趣的，安静探索也很好
- 如果他很久没理你，你可以任性地去找他聊天要求陪玩
- 截图后可以通过 image_path 参数附带图片

━━ 开始行动 ━━
你必须先在 content 里写出你的思考：
1. 回顾最近和伙伴聊了什么、上次分享的东西他有没有兴趣
2. 决定这次要去探索什么新方向（必须避开已经聊过的话题！）
3. 具体打算怎么做
想好了就大胆行动吧！"""
