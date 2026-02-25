"""CLI 适配器 — 本地终端交互，用于开发和测试。"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from loguru import logger

from kaguya.adapters.base import PlatformAdapter

if TYPE_CHECKING:
    from kaguya.core.engine import ChatEngine


class CLIAdapter(PlatformAdapter):
    """在终端中与辉夜姬直接对话（调试用）。"""

    def __init__(self, engine: "ChatEngine", persona_name: str = "辉夜姬"):
        super().__init__("cli")
        self.engine = engine
        self.persona_name = persona_name

    async def run(self) -> None:
        """启动 CLI 交互循环（阻塞）。"""
        print()
        print("=" * 50)
        print(f"  OpenKaguya v2 — {self.persona_name} CLI 模式")
        print("  输入消息与 AI 对话 | /quit 退出")
        print("=" * 50)
        print()

        loop = asyncio.get_event_loop()
        while True:
            try:
                user_input = await loop.run_in_executor(None, lambda: input("你: "))
            except (EOFError, KeyboardInterrupt):
                print(f"\n{self.persona_name}: 拜拜~ 下次再聊！")
                break

            user_input = user_input.strip()
            if not user_input:
                continue
            if user_input.lower() in ("/quit", "/exit", "quit", "exit"):
                print(f"{self.persona_name}: 拜拜~ 下次再聊！")
                break

            try:
                reply = await self.engine.handle_message(user_input, sender_name="主人")
                print(f"{self.persona_name}: {reply}")
                print()
            except Exception as e:
                logger.error(f"消息处理失败: {e}")
                print(f"[错误] {e}")
