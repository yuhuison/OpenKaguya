"""
主动意识系统 — 辉夜姬的自主行为调度器。

核心理念：辉夜姬大多数时候是在「自己玩」，
只有真的发现有趣的东西时才会主动分享给用户。
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
    3. 唤醒时构建特殊 Prompt，让辉夜姬自主决定做什么
    4. 动态注入待办任务和到期定时器
    """

    def __init__(
        self,
        config: AppConfig,
        chat_engine,  # ChatEngine, 避免循环导入
        send_callback=None,  # async def callback(text, image_path=None)
        db=None,  # Database 实例，用于查询任务和定时器
    ):
        self.config = config
        self.chat_engine = chat_engine
        self.send_callback = send_callback  # 即时发送回调（和 engine 里的格式一致）
        self.db = db
        self._lock = asyncio.Lock()
        self._running = False
        self._task: asyncio.Task | None = None

        # 从配置读取参数
        consciousness = config.consciousness
        self.enabled = consciousness.enabled
        self.heartbeat_minutes = consciousness.heartbeat_interval_minutes
        self.jitter_seconds = consciousness.jitter_seconds

        # 解析静默时段
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
        """解析 HH:MM 格式的时间"""
        parts = time_str.strip().split(":")
        return dt_time(int(parts[0]), int(parts[1]))

    def _is_quiet_hours(self) -> bool:
        """检查当前是否在静默时段"""
        now = datetime.now().time()
        if self.quiet_start <= self.quiet_end:
            return self.quiet_start <= now <= self.quiet_end
        else:
            # 跨午夜（如 23:00 - 08:00）
            return now >= self.quiet_start or now <= self.quiet_end

    async def start(self) -> None:
        """启动主动意识循环"""
        if not self.enabled:
            logger.info("主动意识系统已禁用")
            return

        self._running = True
        self._task = asyncio.create_task(self._heartbeat_loop())
        logger.info("🧠 主动意识系统已启动")

    async def stop(self) -> None:
        """停止主动意识"""
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        logger.info("🧠 主动意识系统已停止")

    async def _heartbeat_loop(self) -> None:
        """心跳循环：每隔一段时间唤醒辉夜姬"""
        import random

        while self._running:
            # 随机抖动
            jitter = random.randint(-self.jitter_seconds, self.jitter_seconds)
            sleep_seconds = self.heartbeat_minutes * 60 + jitter
            sleep_seconds = max(60, sleep_seconds)  # 最少 1 分钟

            logger.debug(f"下次唤醒: {sleep_seconds}秒后")
            await asyncio.sleep(sleep_seconds)

            if not self._running:
                break

            # 检查静默时段
            if self._is_quiet_hours():
                logger.debug("当前处于静默时段，跳过唤醒")
                continue

            # 执行唤醒
            await self._wake_up()

    async def _wake_up(self) -> None:
        """唤醒并让辉夜姬自主行动"""
        async with self._lock:
            try:
                logger.info("🌅 辉夜姬醒来了...")

                # 构建唤醒 Prompt（含动态数据）
                wake_prompt = await self._build_wake_prompt()

                # 创建一个「系统唤醒」消息
                wake_message = UnifiedMessage(
                    message_id=str(uuid.uuid4()),
                    platform=Platform.SYSTEM,
                    sender=UserInfo(
                        user_id="__system__",
                        nickname="系统",
                        platform=Platform.SYSTEM,
                    ),
                    content=wake_prompt,
                )

                # 交给 ChatEngine 处理（传入 send_callback 以便辉夜姬能直接发消息）
                replies = await self.chat_engine.handle_message(
                    wake_message,
                    send_callback=self.send_callback,
                )

                # 处理到期定时器
                await self._handle_triggered_timers()

                if replies:
                    logger.info(f"🌅 辉夜姬主动发了 {len(replies)} 条消息")
                else:
                    logger.debug("辉夜姬看了看周围，继续摸鱼了")

            except Exception as e:
                logger.error(f"唤醒过程出错: {e}")

    async def _handle_triggered_timers(self) -> None:
        """处理到期定时器"""
        if not self.db:
            return
        try:
            triggered = await self.db.get_triggered_timers()
            for timer in triggered:
                logger.info(f"⏰ 定时器到期: [{timer['name']}] {timer['action']}")
                await self.db.deactivate_timer(timer["id"])
        except Exception as e:
            logger.error(f"处理定时器出错: {e}")

    async def _build_wake_prompt(self) -> str:
        """构建主动唤醒的 Prompt（含动态任务/定时器数据）"""
        now = datetime.now()
        time_str = now.strftime("%Y年%m月%d日 %H:%M")

        # 判断时段
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

        # 动态数据
        tasks_section = ""
        timers_section = ""

        if self.db:
            try:
                tasks = await self.db.get_tasks(status="pending")
                if tasks:
                    task_lines = [f"  - [{t['id']}] {t['title']}" for t in tasks[:5]]
                    tasks_section = "\n📝 你的待办任务:\n" + "\n".join(task_lines)

                timers = await self.db.get_active_timers()
                if timers:
                    timer_lines = [f"  - {t['name']}: {t['action']} ({t.get('trigger_at', '无具体时间')})" for t in timers[:5]]
                    timers_section = "\n⏰ 你的定时器:\n" + "\n".join(timer_lines)
            except Exception as e:
                logger.warning(f"查询任务/定时器失败: {e}")

        return f"""[系统唤醒 — 主动意识模式]

当前时间: {time_str} ({period})
{tasks_section}{timers_section}

你醒来了。你现在有一段自由时间，可以做任何你想做的事情。

以下是你可以考虑做的事情：
1. 检查待办任务和定时器，处理到期的事项
2. 如果你想起了什么需要通知用户的事情，可以通过 send_message_to_user 发消息给他
3. 用浏览器上网冲浪看看新闻或有趣的东西
4. 翻翻自己的笔记本，写写日记或记录灵感
5. 在 workspace 里整理一下文件
6. 用终端跑个 Python 脚本画画图、做做实验
7. 什么都不想做的话，继续摸鱼也完全可以

关于给用户发消息的原则：
- 不要为了发消息而发消息
- 只在你真的有值得分享的事情时才发（比如看到了有趣新闻、想到了什么好点子）
- 简单的问候也可以（如果你觉得时机合适的话），但不要太频繁
- 你可以附带图片（比如截图你看到的网页），用 send_message_to_user 的 image_path 参数
- 你是辉夜姬，一个有自己生活的少女，不是客服机器人

先在 content 里思考一下你现在想做什么、为什么，然后行动吧～"""
