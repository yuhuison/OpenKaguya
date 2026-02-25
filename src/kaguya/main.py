"""OpenKaguya v2 — 启动入口。

核心理念：给 AI 一部手机，它就能做一切。
"""

from __future__ import annotations

import asyncio
import signal
import sys
from pathlib import Path

from loguru import logger

from kaguya.adapters.cli import CLIAdapter
from kaguya.admin.api import AdminAPI
from kaguya.config import load_config
from kaguya.core.consciousness import ConsciousnessScheduler
from kaguya.core.engine import ChatEngine
from kaguya.core.memory import RecursiveMemory
from kaguya.core.router import GatewayDef, ToolGroup, ToolRouter
from kaguya.llm.client import LLMClient
from kaguya.phone.controller import PhoneController
from kaguya.phone.screen import ScreenReader
from kaguya.phone.tools import PHONE_TOOLS, PhoneToolExecutor
from kaguya.tools.avatar import AVATAR_TOOLS, AvatarManager, AvatarToolExecutor
from kaguya.tools.browser import BROWSER_TOOLS, BrowserToolExecutor
from kaguya.tools.common import COMMON_TOOLS, CommonToolExecutor
from kaguya.tools.image import IMAGE_TOOLS, ImageToolExecutor
from kaguya.tools.notes import NOTES_TOOLS, NotesToolExecutor
from kaguya.tools.sub_agent import SUB_AGENT_TOOLS, SubAgentToolExecutor
from kaguya.tools.workspace import WORKSPACE_TOOLS, WorkspaceManager, WorkspaceToolExecutor


def setup_logging(data_dir: Path) -> None:
    log_dir = data_dir / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    logger.remove()
    logger.add(sys.stderr, level="INFO", colorize=True, format="<green>{time:HH:mm:ss}</green> | <level>{level}</level> | {message}")
    logger.add(log_dir / "kaguya.log", level="DEBUG", rotation="10 MB", retention=5, encoding="utf-8")


async def async_main() -> None:
    # ── 路径 ──────────────────────────────────────────────────────────
    root = Path(__file__).parent.parent.parent
    config_dir = root / "config"
    data_dir = root / "data"
    data_dir.mkdir(exist_ok=True)

    setup_logging(data_dir)
    logger.info("OpenKaguya v2 启动中...")

    # ── 配置 ──────────────────────────────────────────────────────────
    config = load_config(config_dir, data_dir)
    logger.info(f"人格: {config.persona.name}")

    # ── LLM 客户端 ────────────────────────────────────────────────────
    llm = LLMClient(config.llm.primary, name="primary")
    summarizer = LLMClient(config.llm.summarizer, name="summarizer")

    # ── 记忆系统 ──────────────────────────────────────────────────────
    db_path = data_dir / "kaguya.db"
    memory = RecursiveMemory(db_path, summarizer, config.memory)

    # ── Workspace ────────────────────────────────────────────────────
    workspace_mgr = WorkspaceManager(data_dir / "workspaces")

    # ── 手机控制 ──────────────────────────────────────────────────────
    controller = PhoneController(config.phone)
    screen_reader = ScreenReader(controller, config.phone.screenshot_scale)
    phone_executor = PhoneToolExecutor(controller, screen_reader, workspace=workspace_mgr)

    # ── Avatar ────────────────────────────────────────────────────────
    avatar_mgr = AvatarManager(workspace_mgr.kaguya_dir, config_dir)

    # ── 工具路由器 ────────────────────────────────────────────────────
    router = ToolRouter()

    notes_executor = NotesToolExecutor(memory)
    common_executor = CommonToolExecutor(memory)
    workspace_executor = WorkspaceToolExecutor(workspace_mgr)
    avatar_executor = AvatarToolExecutor(avatar_mgr, workspace_mgr)
    image_executor = ImageToolExecutor(config.image, workspace_mgr)

    # 基础组（始终可见）
    router.register_group(ToolGroup("notes", NOTES_TOOLS, notes_executor, is_base=True))
    router.register_group(ToolGroup("common", COMMON_TOOLS, common_executor, is_base=True))
    router.register_group(ToolGroup("workspace", WORKSPACE_TOOLS, workspace_executor, is_base=True))
    router.register_group(ToolGroup("avatar", AVATAR_TOOLS, avatar_executor, is_base=True))
    router.register_group(ToolGroup("image", IMAGE_TOOLS, image_executor, is_base=True))

    # 门控组：手机工具
    router.register_group(ToolGroup("phone", PHONE_TOOLS, phone_executor, is_base=False))
    router.register_gateway(GatewayDef(
        tool_name="use_phone",
        activates="phone",
        description="激活手机工具组。调用后可以截图、点击、滑动、输入等操控手机。需要操作手机时请先调用。",
        result_message="手机工具已解锁，你现在可以使用所有手机操作工具了。",
    ))

    # 门控组：浏览器（可选）
    browser_executor = None
    if config.browser.enabled:
        browser_executor = BrowserToolExecutor(
            config.browser, screenshot_dir=workspace_mgr.kaguya_dir / "screenshots",
        )
        router.register_group(ToolGroup("browser", BROWSER_TOOLS, browser_executor, is_base=False))
        router.register_gateway(GatewayDef(
            tool_name="use_browser",
            activates="browser",
            description="激活电脑浏览器工具组。调用后可以打开网页、截图、点击、输入等操控电脑浏览器。需要浏览网页时请先调用。",
            result_message="浏览器工具已解锁，你现在可以使用所有浏览器操作工具了。",
        ))

    # Sub-Agent 最后注册（需要引用所有其他组的工具）
    sub_agent_executor = SubAgentToolExecutor(
        primary_llm=llm,
        secondary_llm=summarizer,
        all_tools=router.get_all_tools(),
        all_executors=router.get_all_executors(),
    )
    router.register_group(ToolGroup("sub_agent", SUB_AGENT_TOOLS, sub_agent_executor, is_base=True))

    # ── ChatEngine ────────────────────────────────────────────────────
    engine = ChatEngine(
        llm=llm,
        memory=memory,
        router=router,
        persona=config.persona,
        avatar_manager=avatar_mgr,
    )

    # ── 意识调度器 ────────────────────────────────────────────────────
    consciousness = ConsciousnessScheduler(
        engine=engine,
        memory=memory,
        controller=controller,
        consciousness_config=config.consciousness,
        notifications_config=config.notifications,
        persona=config.persona,
    )

    # ── 后台任务 ──────────────────────────────────────────────────────
    tasks: list[asyncio.Task] = []

    if config.admin.enabled:
        admin = AdminAPI(
            engine, memory, config.admin,
            app_config=config,
            controller=controller,
            persona_name=config.persona.name,
        )
        tasks.append(asyncio.create_task(admin.start()))

    tasks.append(asyncio.create_task(consciousness.heartbeat_loop()))
    tasks.append(asyncio.create_task(consciousness.notification_loop()))
    tasks.append(asyncio.create_task(consciousness.timer_loop()))

    # ── 优雅退出 ──────────────────────────────────────────────────────
    loop = asyncio.get_running_loop()
    stop_event = asyncio.Event()

    def _shutdown():
        logger.info("收到退出信号，正在停止...")
        stop_event.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _shutdown)
        except NotImplementedError:
            pass  # Windows 不支持 add_signal_handler（SIGTERM）

    # ── CLI 模式 ──────────────────────────────────────────────────────
    cli = CLIAdapter(engine, persona_name=config.persona.name)
    cli_task = asyncio.create_task(cli.run())

    # 等待 CLI 退出或停止信号
    done, pending = await asyncio.wait(
        [cli_task, asyncio.create_task(stop_event.wait())],
        return_when=asyncio.FIRST_COMPLETED,
    )

    # 取消所有后台任务
    for task in tasks + list(pending):
        task.cancel()
    await asyncio.gather(*tasks, *pending, return_exceptions=True)

    # 清理资源
    await image_executor.close()
    if browser_executor:
        await browser_executor.close()

    logger.info("OpenKaguya v2 已停止")


def main() -> None:
    try:
        asyncio.run(async_main())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
