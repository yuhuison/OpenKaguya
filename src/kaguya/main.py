"""
OpenKaguya 启动入口。
"""

from __future__ import annotations

import asyncio
import sys

from loguru import logger


def setup_logging():
    """配置日志"""
    logger.remove()
    logger.add(
        sys.stderr,
        format="<green>{time:HH:mm:ss}</green> | <level>{level: <7}</level> | <cyan>{name}</cyan> - <level>{message}</level>",
        level="INFO",
    )
    logger.add(
        "data/logs/kaguya_{time:YYYY-MM-DD}.log",
        rotation="1 day",
        retention="30 days",
        level="DEBUG",
    )


async def run_cli():
    """以 CLI 模式运行辉夜姬"""
    from kaguya.adapters.cli import CLIAdapter
    from kaguya.config import load_config
    from kaguya.core.engine import ChatEngine
    from kaguya.llm.client import LLMClient
    from kaguya.llm.embedding import EmbeddingClient
    from kaguya.memory.database import Database
    from kaguya.memory.middleware import MemoryMiddleware
    from kaguya.memory.retriever import MemoryRetriever

    # 1. 加载配置
    config = load_config()

    # 2. 检查 API Key
    if not config.llm.primary.api_key:
        logger.error("未配置主模型 API Key！")
        print()
        print("⚠️  请先配置 API Key：")
        print("   1. 复制 config/secrets.example.toml 为 config/secrets.toml")
        print("   2. 在 secrets.toml 中填入你的 API Key")
        print()
        return

    # 3. 初始化 LLM 客户端
    primary_llm = LLMClient(config.llm.primary, name="primary")
    secondary_llm = LLMClient(config.llm.secondary, name="secondary")

    # 4. 初始化数据库
    db = Database(embedding_dim=config.llm.embedding.dimensions or 4096)
    await db.connect()

    # 5. 初始化记忆系统
    embed_client = EmbeddingClient(config.llm.embedding)
    retriever = MemoryRetriever(
        db=db,
        embed_client=embed_client,
        secondary_llm=secondary_llm,
        vectorize_threshold=config.memory.vectorize_threshold,
    )
    memory_mw = MemoryMiddleware(
        db=db,
        retriever=retriever,
        top_k=config.memory.retrieval_top_k,
    )

    # 6. 初始化工具系统
    from kaguya.tools.registry import ToolRegistry
    from kaguya.tools.workspace import WorkspaceManager
    from kaguya.tools.builtin import create_builtin_tools

    workspace = WorkspaceManager()
    tool_registry = ToolRegistry()
    builtin_tools = create_builtin_tools(
        workspace=workspace,
        db=db,
        retriever=retriever,
    )
    tool_registry.register_all(builtin_tools)

    # 6b. 初始化浏览器工具（可选，按配置启用）
    browser_toolkit = None
    if hasattr(config, "browser") and config.browser.mode:
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
        logger.info(f"浏览器工具已注册 (模式: {config.browser.mode})")

    # 7. 初始化用户身份管理器
    from kaguya.core.identity import UserIdentityManager, UserIdentity

    identities = [
        UserIdentity(
            id=u.id,
            nickname=u.nickname,
            note=u.note,
            role=u.role,
            accounts=u.accounts,
        )
        for u in config.identity.users
    ]
    identity_mgr = UserIdentityManager(identities)

    # 8. 初始化对话引擎 & 注册中间件
    engine = ChatEngine(
        config=config,
        primary_llm=primary_llm,
        tool_registry=tool_registry,
    )
    # 注册中间件（顺序重要：群聊过滤 → 记忆系统）
    from kaguya.core.group import GroupFilterMiddleware
    group_filter = GroupFilterMiddleware(
        bot_names=[config.persona.name, "kaguya", "Kaguya"],
    )
    engine.add_middleware(group_filter)
    engine.add_middleware(memory_mw)

    # 条件启动微信适配器（先初始化，后面给 consciousness 用）
    wechat_adapter = None
    if config.wechat.enabled:
        from kaguya.adapters.wechat import WeChatAdapter
        wechat_adapter = WeChatAdapter(
            config=config.wechat,
            identity_manager=identity_mgr,
        )
        wechat_adapter.set_handler(engine.handle_message)

    # 9. 初始化主动意识系统
    from kaguya.core.consciousness import ConsciousnessScheduler

    # 构建主动意识的发送回调（通过微信发给第一个白名单用户）
    consciousness_send_cb = None
    if wechat_adapter and config.wechat.whitelist_users:
        default_target = config.wechat.whitelist_users[0]

        async def _consciousness_send(text: str, image_path: str | None = None):
            """主动意识发送回调：发消息给默认用户"""
            if text:
                await wechat_adapter._send_single(default_target, text)
            if image_path:
                await wechat_adapter._send_image(default_target, image_path)

        consciousness_send_cb = _consciousness_send

    consciousness = ConsciousnessScheduler(
        config=config,
        chat_engine=engine,
        send_callback=consciousness_send_cb,
        db=db,
    )

    # 10. 初始化 CLI 适配器
    adapter = CLIAdapter()
    adapter.set_handler(engine.handle_message)

    # 11. 启动
    logger.info("🌙 OpenKaguya 启动中...")
    await consciousness.start()
    if wechat_adapter:
        await wechat_adapter.start()
        logger.info("📱 微信适配器已启动")
    try:
        await adapter.start()
    except KeyboardInterrupt:
        pass
    finally:
        await consciousness.stop()
        await adapter.stop()
        if wechat_adapter:
            await wechat_adapter.stop()
        if browser_toolkit:
            await browser_toolkit.close()
        await db.close()
        logger.info("🌙 OpenKaguya 已关闭")


def main():
    """程序入口"""
    setup_logging()
    asyncio.run(run_cli())


if __name__ == "__main__":
    main()
