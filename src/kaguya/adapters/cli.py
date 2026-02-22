"""
CLI 适配器 — 本地终端交互，用于开发和测试。
"""

from __future__ import annotations

import asyncio
import uuid

from loguru import logger

from kaguya.adapters.base import PlatformAdapter
from kaguya.core.types import Platform, UnifiedMessage, UserInfo


class CLIAdapter(PlatformAdapter):
    """
    CLI 适配器：在终端中与辉夜姬对话。
    用于开发和测试，不需要任何外部服务。
    """

    def __init__(self):
        super().__init__("cli")
        self._running = False
        self._user = UserInfo(
            user_id="cli:local_user",
            nickname="主人",
            platform=Platform.CLI,
        )

    async def start(self) -> None:
        """启动 CLI 交互循环"""
        self._running = True
        logger.info("CLI 适配器启动 — 在终端中与辉夜姬对话")
        print()
        print("=" * 50)
        print("  🌙 OpenKaguya — 辉夜姬 CLI 模式")
        print("  输入消息与辉夜姬对话")
        print("  输入 /quit 退出")
        print("=" * 50)
        print()

        while self._running:
            try:
                # 在线程池中运行 input() 以避免阻塞事件循环
                user_input = await asyncio.get_event_loop().run_in_executor(
                    None, lambda: input("你: ")
                )
            except (EOFError, KeyboardInterrupt):
                print("\n再见！")
                break

            user_input = user_input.strip()
            if not user_input:
                continue

            if user_input.lower() == "/quit":
                print("辉夜姬: 拜拜~ 下次再聊！(◡‿◡✿)")
                break

            # 构造 UnifiedMessage
            message = UnifiedMessage(
                message_id=str(uuid.uuid4()),
                platform=Platform.CLI,
                sender=self._user,
                content=user_input,
            )

            # 调用处理器
            if self._handler:
                try:
                    replies = await self._handler(message)
                    for reply in replies:
                        print(f"辉夜姬: {reply}")
                        print()
                except Exception as e:
                    logger.error(f"消息处理失败: {e}")
                    print(f"[系统错误] {e}")
            else:
                print("[系统] 消息处理器未设置")

    async def stop(self) -> None:
        """停止 CLI 适配器"""
        self._running = False
        logger.info("CLI 适配器已停止")

    async def send_messages(
        self,
        user_id: str,
        messages: list[str],
        group_id: str | None = None,
    ) -> None:
        """在终端中打印消息"""
        for msg in messages:
            print(f"辉夜姬: {msg}")
            print()
