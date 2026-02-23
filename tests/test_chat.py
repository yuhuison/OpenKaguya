"""
轻量级辉夜姬聊天测试脚本。

跳过记忆系统（向量化、FTS5等），仅启用 ChatEngine + LLM + 基础工具，
在终端中直接和辉夜姬对话。
"""

import asyncio
import uuid

from kaguya.config import load_config
from kaguya.core.engine import ChatEngine
from kaguya.core.types import Platform, UnifiedMessage, UserInfo
from kaguya.llm.client import LLMClient
from kaguya.tools.registry import ToolRegistry


async def main():
    # 1. 加载配置
    config = load_config()
    primary_llm = LLMClient(config.llm.primary, name="primary")

    # 2. 初始化工具（仅注册基础工具，不需要数据库）
    tool_registry = ToolRegistry()

    # 可选：注册浏览器工具
    if hasattr(config, "browser") and config.browser.mode:
        try:
            from kaguya.tools.browser import BrowserToolkit
            browser_toolkit = BrowserToolkit(
                mode=config.browser.mode,
                chrome_path=config.browser.chrome_path,
                cdp_url=config.browser.cdp_url,
                headless=config.browser.headless,
                cloud_proxy_country=config.browser.cloud_proxy_country,
                api_key=config.browser.api_key,
            )
            tool_registry.register_all(browser_toolkit.get_tools())
            print(f"  浏览器工具已加载 (模式: {config.browser.mode})")
        except Exception as e:
            print(f"  浏览器工具加载失败: {e}")

    # 3. 初始化 ChatEngine（无中间件 = 无记忆系统）
    engine = ChatEngine(
        config=config,
        primary_llm=primary_llm,
        tool_registry=tool_registry,
    )

    user = UserInfo(
        user_id="test:local",
        nickname="主人",
        platform=Platform.CLI,
    )

    print()
    print("=" * 50)
    print("  🌙 辉夜姬轻量测试模式")
    print("  无记忆系统，纯 ChatEngine 对话")
    print("  输入 /quit 退出")
    print("=" * 50)
    print()

    while True:
        try:
            user_input = input("你: ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\n拜拜~")
            break

        if not user_input:
            continue
        if user_input.lower() == "/quit":
            print("辉夜姬: 拜拜~ (◡‿◡✿)")
            break

        message = UnifiedMessage(
            message_id=str(uuid.uuid4()),
            platform=Platform.CLI,
            sender=user,
            content=user_input,
        )

        replies = await engine.handle_message(message)
        for reply in replies:
            print(f"辉夜姬: {reply}")
        print()


if __name__ == "__main__":
    from kaguya.main import setup_logging
    setup_logging()
    asyncio.run(main())
