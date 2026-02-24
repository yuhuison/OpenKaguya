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
    from kaguya.memory.topic_manager import TopicManager

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
    topic_manager = TopicManager(
        db=db,
        embed_client=embed_client,
        secondary_llm=secondary_llm,
    )
    memory_mw = MemoryMiddleware(
        db=db,
        topic_manager=topic_manager,
        top_k=config.memory.vectorize_threshold,
        embed_client=embed_client,
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
    )
    tool_registry.register_all(builtin_tools)

    # 6b. 注册记忆话题工具
    from kaguya.tools.memory_tools import MemoryTools
    from kaguya.tools.registry import Tool

    memory_tools_instance = MemoryTools(db=db, embed_client=embed_client)

    def _make_mem_tool(td: dict, bound_fn) -> Tool:
        """工厂函数：为每个记忆工具创建一个独立的 Tool 实例，避免闭包变量捕获问题"""
        class _MemTool(Tool):
            @property
            def name(self): return td["function"]["name"]
            @property
            def description(self): return td["function"]["description"]
            @property
            def parameters(self): return td["function"]["parameters"]
            async def execute(self, **kwargs): return await bound_fn(**kwargs)
        return _MemTool()

    for _td in MemoryTools.TOOL_DEFINITIONS:
        _fn = getattr(memory_tools_instance, _td["function"]["name"])
        tool_registry.register(_make_mem_tool(_td, _fn))

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
            # 主模型配置，供 browser_task 默认复用
            primary_model=config.llm.primary.model,
            primary_base_url=config.llm.primary.base_url,
            primary_api_key=config.llm.primary.api_key,
        )
        tool_registry.register_all(browser_toolkit.get_tools())
        logger.info(f"浏览器工具已注册 (模式: {config.browser.mode})")

    # 6c. 初始化网络搜索工具（Exa / Tavily，二选一）
    from kaguya.config import CONFIG_DIR, _load_toml
    _secrets = _load_toml(CONFIG_DIR / "secrets.toml")
    _exa_key = (_secrets.get("exa") or {}).get("api_key", "")
    _tavily_key = (_secrets.get("tavily") or {}).get("api_key", "")
    if _exa_key or _tavily_key:
        from kaguya.tools.web_search import create_web_search_tools
        web_tools = create_web_search_tools(exa_api_key=_exa_key, tavily_api_key=_tavily_key)
        if web_tools:
            tool_registry.register_all(web_tools)
            logger.info(f"🔍 网络搜索工具已注册 ({web_tools[0]._backend.provider_name})")

    # 6d. 初始化子 Agent 工具
    from kaguya.tools.sub_agent import SubAgentTool
    sub_agent_tool = SubAgentTool(
        primary_llm=primary_llm,
        secondary_llm=secondary_llm,
        tool_registry=tool_registry,
    )
    tool_registry.register(sub_agent_tool)

    # 6e. 初始化头像管理器
    from kaguya.tools.avatar import AvatarManager, SetAvatarTool
    avatar_manager = AvatarManager(workspace.kaguya_dir)
    avatar_manager.init_from_config()
    tool_registry.register(SetAvatarTool(avatar_manager))

    # 6f. 初始化 AI 能力 Providers
    all_providers = []
    if config.providers.enabled:
        for pname in config.providers.enabled:
            try:
                if pname == "qwen_image":
                    from kaguya.providers.qwen_image import QwenImageProvider
                    pentry = config.providers.entries.get(pname, {})
                    edit_model = getattr(pentry, 'extra', {}).get('edit_model', 'qwen-image-edit-max') if pentry else 'qwen-image-edit-max'
                    _dashscope_key = _secrets.get("dashscope", {}).get("api_key", "")
                    if _dashscope_key:
                        provider = QwenImageProvider(
                            api_key=_dashscope_key,
                            workspace=workspace,
                            edit_model=edit_model,
                        )
                        all_providers.append(provider)
                        logger.info(f"🎨 已初始化 Provider: {pname}")
                    else:
                        logger.warning(f"Provider {pname} 缺少 DashScope API Key，跳过")
                else:
                    logger.warning(f"未知的 Provider: {pname}，跳过")
            except Exception as e:
                logger.error(f"初始化 Provider {pname} 失败: {e}")

    # 注册 provider 的 chat 阶段工具
    for p in all_providers:
        p_tools = p.get_tools(phase="chat")
        if p_tools:
            tool_registry.register_all(p_tools)
            logger.info(f"🔧 已注册 {p.name} 工具 ({len(p_tools)} 个)")

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

    # 条件初始化微信适配器
    wechat_adapter = None
    if config.wechat.enabled:
        from kaguya.adapters.wechat import WeChatAdapter
        wechat_adapter = WeChatAdapter(
            config=config.wechat,
            identity_manager=identity_mgr,
            workspace=workspace,
        )

    # 8. 初始化对话引擎 & 注册中间件
    # 收集所有活跃的 adapter（用于工具和 prompt 注入）
    all_adapters = [a for a in [wechat_adapter] if a is not None]

    # 注册 chat 阶段的 adapter 工具
    for ada in all_adapters:
        chat_tools = ada.get_tools(phase="chat")
        if chat_tools:
            tool_registry.register_all(chat_tools)
            logger.info(f"🔧 已注册 {ada.name} 平台工具 ({len(chat_tools)} 个)")

    # 8a. 初始化 Toolkit 路由器（所有工具注册完毕后）
    from kaguya.tools.toolkit_router import ToolkitRouter, UseToolkitTool
    toolkit_router = ToolkitRouter(tool_registry)
    tool_registry.register(UseToolkitTool(toolkit_router))
    logger.info(
        f"🔧 Toolkit 路由器已初始化 "
        f"(核心工具: {len(toolkit_router.get_visible_tools())} 个, "
        f"总工具: {len(tool_registry.tool_names)} 个)"
    )

    engine = ChatEngine(
        config=config,
        primary_llm=primary_llm,
        tool_registry=tool_registry,
        workspace=workspace,
        adapters=all_adapters,
        avatar_manager=avatar_manager,
        providers=all_providers,
        toolkit_router=toolkit_router,
    )
    # 注册中间件（顺序重要：群聊过滤 → 记忆系统）
    from kaguya.core.group import GroupFilterMiddleware
    group_filter = GroupFilterMiddleware(
        bot_names=[config.persona.name, "kaguya", "Kaguya"],
    )
    engine.add_middleware(group_filter)
    engine.add_middleware(memory_mw)

    # 设置 adapter handler（engine 已创建）
    if wechat_adapter:
        wechat_adapter.set_handler(engine.handle_message)

    # 9. 初始化主动意识系统
    from kaguya.core.consciousness import ConsciousnessScheduler

    # 构建主动意识的发送回调（支持 target_user_id 路由到正确用户）
    consciousness_send_cb = None
    if wechat_adapter and config.wechat.whitelist_users:
        default_target = config.wechat.whitelist_users[0]

        async def _consciousness_send(
            text: str, image_path: str | None = None,
            target_user_id: str | None = None, **_
        ):
            """主动意识发送回调：按 target_user_id 路由，默认发给第一个白名单用户"""
            target = target_user_id or default_target
            if text:
                await wechat_adapter._send_single(target, text)
            if image_path:
                await wechat_adapter._send_image(target, image_path)

        consciousness_send_cb = _consciousness_send

    consciousness = ConsciousnessScheduler(
        config=config,
        chat_engine=engine,
        send_callback=consciousness_send_cb,
        db=db,
        secondary_llm=secondary_llm,
        adapters=all_adapters,
        providers=all_providers,
    )

    # 10. 初始化 CLI 适配器（仅交互模式）
    import sys
    is_interactive = sys.stdin.isatty()

    adapter = None
    if is_interactive:
        adapter = CLIAdapter()
        adapter.set_handler(engine.handle_message)

    # 11. 启动管理面板
    admin_runner = None
    if config.admin.enabled:
        from kaguya.admin import start_admin_server
        admin_runner = await start_admin_server(
            db=db,
            host=config.admin.host,
            port=config.admin.port,
            password=config.admin.password,
            consciousness=consciousness,
            engine=engine,
        )

    # 12. 启动
    logger.info("🌙 OpenKaguya 启动中...")
    await consciousness.start()
    if wechat_adapter:
        await wechat_adapter.start()
        logger.info("📱 微信适配器已启动")

    try:
        if is_interactive and adapter:
            # 交互模式：CLI 适配器运行直到用户输入 /quit 或 Ctrl+C
            await adapter.start()
        else:
            # 守护进程模式（systemd）：挂起等待 SIGTERM / SIGINT
            import signal
            stop_event = asyncio.Event()
            loop = asyncio.get_running_loop()
            for sig in (signal.SIGTERM, signal.SIGINT):
                loop.add_signal_handler(sig, stop_event.set)
            logger.info("🌙 守护进程模式运行中，等待 SIGTERM 信号退出")
            await stop_event.wait()
    except KeyboardInterrupt:
        pass
    finally:
        await consciousness.stop()
        if adapter:
            await adapter.stop()
        if wechat_adapter:
            await wechat_adapter.stop()
        if admin_runner:
            await admin_runner.cleanup()
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
