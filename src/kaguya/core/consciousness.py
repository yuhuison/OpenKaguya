"""ConsciousnessScheduler — 主动意识调度器（V2 简化版）。

三个后台循环：
  1. heartbeat_loop    — 定时唤醒 AI，让她自主决定做什么
  2. notification_loop — 轮询通知，有新消息时唤醒 AI
  3. timer_loop        — 轮询到期的定时器
"""

from __future__ import annotations

import asyncio
import re
import random
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any, Optional

from loguru import logger

from kaguya.config import ConsciousnessConfig, NotificationsConfig, PersonaConfig

if TYPE_CHECKING:
    from kaguya.core.engine import ChatEngine
    from kaguya.core.memory import RecursiveMemory


class ConsciousnessScheduler:
    """辉夜姬的自主意识调度器。

    notification_source: 任何具有 async get_notifications() -> list[dict] 的对象
                        （如 DesktopController）。
    """

    def __init__(
        self,
        engine: "ChatEngine",
        memory: "RecursiveMemory",
        notification_source: Any | None = None,
        consciousness_config: ConsciousnessConfig | None = None,
        notifications_config: NotificationsConfig | None = None,
        persona: Optional[PersonaConfig] = None,
        platform: str = "desktop",
    ):
        self.engine = engine
        self.memory = memory
        self.notification_source = notification_source
        self.con_cfg = consciousness_config or ConsciousnessConfig()
        self.notif_cfg = notifications_config or NotificationsConfig()
        self.persona = persona
        self.platform = platform
        self._last_notification_ids: set[str] = set()

    # ------------------------------------------------------------------
    # 公开接口
    # ------------------------------------------------------------------

    async def heartbeat_loop(self) -> None:
        """定时心跳循环，唤醒 AI 自主行动。"""
        if not self.con_cfg.enabled:
            logger.info("主动意识已禁用")
            return

        logger.info(
            f"主动意识启动: 间隔 {self.con_cfg.interval_minutes} 分钟 "
            f"± {self.con_cfg.jitter_minutes} 分钟"
        )
        while True:
            jitter = random.uniform(
                -self.con_cfg.jitter_minutes * 60,
                self.con_cfg.jitter_minutes * 60,
            )
            sleep_secs = self.con_cfg.interval_minutes * 60 + jitter
            await asyncio.sleep(max(60, sleep_secs))

            if self._is_quiet_hours():
                logger.debug("静默时段，跳过心跳")
                continue

            try:
                await self._heartbeat_tick()
            except Exception as e:
                logger.error(f"心跳处理失败: {e}")

    async def notification_loop(self) -> None:
        """通知轮询循环，有新消息时唤醒 AI。"""
        if not self.notification_source:
            logger.info("未配置通知源，通知轮询跳过")
            return

        logger.info(f"通知轮询启动: 每 {self.notif_cfg.poll_interval_seconds} 秒")
        while True:
            await asyncio.sleep(self.notif_cfg.poll_interval_seconds)
            try:
                await self._check_notifications()
            except Exception as e:
                logger.debug(f"通知检查失败: {e}")

    async def timer_loop(self) -> None:
        """定时器检查循环，处理到期的定时器。"""
        logger.info("定时器循环启动")
        while True:
            await asyncio.sleep(60)
            try:
                await self._check_timers()
            except Exception as e:
                logger.error(f"定时器检查失败: {e}")

    # ------------------------------------------------------------------
    # 内部逻辑
    # ------------------------------------------------------------------

    async def _heartbeat_tick(self) -> None:
        """执行一次心跳：构建唤醒 prompt，调用引擎。"""
        prompt = await self._build_wakeup_prompt()
        logger.info("主动意识唤醒")
        reply = await self.engine.handle_consciousness(
            prompt, trigger="heartbeat", pre_activate_groups=[self.platform],
        )

        # 记录意识日志
        if reply:
            await self.memory.log_consciousness(reply[:200])

    async def _check_notifications(self) -> None:
        """检查通知，有新消息时唤醒 AI（带防抖）。"""
        notifications = await self.notification_source.get_notifications()
        if not notifications:
            return

        notifications = self._filter_notifications(notifications)
        if not notifications:
            return

        # 生成通知指纹，检测是否有新通知
        fingerprints = self._build_fingerprints(notifications)
        new_fps = fingerprints - self._last_notification_ids
        if not new_fps:
            return

        # 防抖：等待 2 秒后重新拉取，直到没有新增通知
        logger.debug(f"发现 {len(new_fps)} 条新通知，开始防抖等待")
        while True:
            await asyncio.sleep(2)
            fresh = await self.notification_source.get_notifications()
            fresh = self._filter_notifications(fresh) if fresh else []
            fresh_fps = self._build_fingerprints(fresh) if fresh else set()
            if not (fresh_fps - fingerprints):
                notifications = fresh or notifications
                fingerprints = fresh_fps or fingerprints
                break
            logger.debug(f"防抖期间又有 {len(fresh_fps - fingerprints)} 条新通知，继续等待")
            notifications = fresh
            fingerprints = fresh_fps

        new_count = len(fingerprints - self._last_notification_ids)
        self._last_notification_ids = fingerprints
        logger.info(f"防抖结束，{new_count} 条新通知，唤醒 AI")

        lines = []
        for n in notifications:
            pkg = n.get("pkg", "未知")
            title = n.get("title", "")
            text = n.get("text", "")
            lines.append(f"- [{pkg}] {title}: {text}")

        prompt = self._build_notification_prompt(new_count, lines)
        if self.persona:
            notif_guidelines = self.persona.get_guidelines("notification")
            if notif_guidelines:
                prompt += f"\n\n【通知处理准则】\n{notif_guidelines}"
        reply = await self.engine.handle_consciousness(
            prompt, trigger="notification", pre_activate_groups=[self.platform],
        )
        if reply:
            await self.memory.log_consciousness(f"[通知处理] {reply[:200]}")

    async def _check_timers(self) -> None:
        """检查并处理到期的定时器。"""
        triggered = await self.memory.timer_get_triggered()
        for timer in triggered:
            label = timer.get("label", "")
            timer_id = timer.get("id")
            recurrence = timer.get("recurrence")
            logger.info(f"定时器触发: [{timer_id}] {label}")

            await self.memory.timer_delete(timer_id)

            # 周期性定时器重新调度
            if recurrence:
                await self._reschedule_timer(label, timer, recurrence)

            prompt = (
                f"你之前设置的定时器现在触发了：\n"
                f"「{label}」\n\n"
                f"请根据这个提醒决定下一步行动。"
            )
            reply = await self.engine.handle_consciousness(prompt, trigger="timer")
            if reply:
                await self.memory.log_consciousness(f"[定时器] {label}: {reply[:200]}")

    async def _reschedule_timer(
        self, label: str, timer: dict, recurrence: str
    ) -> None:
        """为周期性定时器创建下一次触发。"""
        trigger_at_str = timer.get("trigger_at", "")
        try:
            old_trigger = datetime.fromisoformat(trigger_at_str)
        except (ValueError, TypeError):
            old_trigger = datetime.now()

        if recurrence == "daily":
            delta = timedelta(days=1)
        elif recurrence == "weekly":
            delta = timedelta(weeks=1)
        else:
            logger.warning(f"未知的 recurrence 类型: {recurrence}，跳过重新调度")
            return

        next_trigger = old_trigger + delta
        # 如果下次触发已在过去（如进程曾停机），跳到未来
        now = datetime.now()
        while next_trigger <= now:
            next_trigger += delta

        new_id = await self.memory.timer_set(label, next_trigger, recurrence)
        logger.info(f"周期定时器已重新调度: [{new_id}] {label} → {next_trigger.isoformat()}")

    def _build_notification_prompt(self, count: int, lines: list[str]) -> str:
        """构建通知唤醒 prompt。"""
        return (
            f"你的电脑检测到 {count} 个窗口有新变化：\n"
            + "\n".join(lines)
            + "\n\n【处理方式】\n"
            "如果要查看或回复消息，推荐操作流程：\n"
            "1. 调用 desktop_focus_window 聚焦对应窗口\n"
            "2. 截图查看窗口内容\n"
            "3. 在窗口中操作（点击、输入回复等）\n"
            "注意：每次操作后记得截图确认结果。\n"
            "也可以选择暂时不处理。"
        )

    async def _build_wakeup_prompt(self) -> str:
        """构建心跳唤醒 prompt。"""
        now = datetime.now()
        hour = now.hour
        time_of_day = (
            "深夜" if hour < 5
            else "清晨" if hour < 8
            else "上午" if hour < 12
            else "中午" if hour < 14
            else "下午" if hour < 18
            else "傍晚" if hour < 20
            else "晚上"
        )
        time_str = now.strftime("%Y年%m月%d日 %H:%M")

        parts = [f"现在是{time_of_day}，时间 {time_str}。"]

        # 最近意识日志
        recent_logs = await self.memory.get_recent_consciousness_logs(3)
        if recent_logs:
            parts.append("你最近做过：\n" + "\n".join(f"- {l}" for l in recent_logs))

        # 待处理定时器
        timers = await self.memory.timer_list()
        if timers:
            timer_lines = [f"- {t['label']}（{t['trigger_at']}）" for t in timers[:3]]
            parts.append("待处理的定时任务：\n" + "\n".join(timer_lines))

        parts.append(
            "现在由你自由决定做什么——可以查看桌面通知、回复消息、操作电脑、上网浏览、记笔记，"
            "或者什么都不做（直接回复「暂时不做什么」也完全OK）。"
        )

        if self.persona:
            hb_guidelines = self.persona.get_guidelines("heartbeat")
            if hb_guidelines:
                parts.append(f"【自主行动准则】\n{hb_guidelines}")

        return "\n\n".join(parts)

    @staticmethod
    def _build_fingerprints(notifications: list[dict]) -> set[str]:
        """为通知列表生成指纹集合。"""
        return {
            f"{n.get('pkg', '')}:{n.get('title', '')}:{n.get('text', '')}"
            for n in notifications
        }

    def _filter_notifications(self, notifications: list[dict]) -> list[dict]:
        """根据配置过滤通知：watch_apps 应用白名单 → 内容过滤规则白名单。"""
        result = []
        watch = set(self.notif_cfg.watch_apps)
        filters = self.notif_cfg.filters

        for n in notifications:
            pkg = n.get("pkg", "")

            # 应用白名单：如果配了 watch_apps，只放行白名单中的
            if watch and pkg not in watch:
                continue

            # 内容过滤（白名单制）：如果配了规则，只放行匹配的通知
            if filters:
                title = n.get("title", "")
                text = n.get("text", "")
                matched = False
                for f in filters:
                    if not f.pattern:
                        continue
                    try:
                        pattern = re.compile(f.pattern)
                    except re.error:
                        continue
                    if f.target == "title" and pattern.search(title):
                        matched = True
                    elif f.target == "text" and pattern.search(text):
                        matched = True
                    elif f.target == "any" and (pattern.search(title) or pattern.search(text)):
                        matched = True
                    if matched:
                        break
                if not matched:
                    continue

            result.append(n)

        return result

    def _is_quiet_hours(self) -> bool:
        """判断当前是否在静默时段。"""
        quiet = self.con_cfg.quiet_hours
        if not quiet or len(quiet) < 2:
            return False

        try:
            start_h, start_m = map(int, quiet[0].split(":"))
            end_h, end_m = map(int, quiet[1].split(":"))
        except ValueError:
            return False

        now = datetime.now()
        now_minutes = now.hour * 60 + now.minute
        start_minutes = start_h * 60 + start_m
        end_minutes = end_h * 60 + end_m

        if start_minutes <= end_minutes:
            # 同一天内的静默时段
            return start_minutes <= now_minutes < end_minutes
        else:
            # 跨午夜的静默时段（如 23:00 - 07:00）
            return now_minutes >= start_minutes or now_minutes < end_minutes
