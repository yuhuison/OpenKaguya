"""OpenKaguya v2 — 启动入口。

核心理念：给 AI 一台电脑，它就能做一切。
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
from kaguya.core.router import ToolGroup, ToolRouter
from kaguya.extensions import ExtensionContext, ExtensionManager
from kaguya.llm.client import LLMClient
from kaguya.desktop.controller import DesktopController
from kaguya.desktop.screen import DesktopScreenReader
from kaguya.desktop.tools import DESKTOP_TOOLS, DesktopToolExecutor
from kaguya.tools.avatar import AVATAR_TOOLS, AvatarManager, AvatarToolExecutor
from kaguya.tools.browser import BROWSER_TOOLS, BrowserToolExecutor
from kaguya.tools.common import COMMON_TOOLS, CommonToolExecutor
from kaguya.tools.image import IMAGE_TOOLS, ImageToolExecutor
from kaguya.tools.notes import NOTES_TOOLS, NotesToolExecutor
from kaguya.tools.sub_agent import (
    AGENT_MANAGEMENT_TOOLS,
    AgentManagementExecutor,
    SubAgentManager,
)
from kaguya.tools.task import TASK_TOOLS, TaskToolExecutor, TaskTracker
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
    agent_llm = LLMClient(config.llm.agent or config.llm.summarizer, name="agent")

    # ── 记忆系统 ──────────────────────────────────────────────────────
    db_path = data_dir / "kaguya.db"
    memory = RecursiveMemory(db_path, summarizer, config.memory)

    # ── Workspace ────────────────────────────────────────────────────
    workspace_mgr = WorkspaceManager(data_dir / "workspaces")

    # ── 桌面控制 ──────────────────────────────────────────────────────
    desktop_controller = DesktopController()
    desktop_screen_reader = DesktopScreenReader(
        desktop_controller, config.desktop.screenshot_scale,
        yolo_model_repo=config.desktop.yolo_model_repo,
        yolo_model_file=config.desktop.yolo_model_file,
        box_threshold=config.desktop.box_threshold,
    )
    desktop_executor = DesktopToolExecutor(desktop_controller, desktop_screen_reader)

    # ── Avatar ────────────────────────────────────────────────────────
    avatar_mgr = AvatarManager(workspace_mgr.kaguya_dir, config_dir)

    # ── 工具路由器 ────────────────────────────────────────────────────
    router = ToolRouter()

    notes_executor = NotesToolExecutor(memory)
    common_executor = CommonToolExecutor(memory)
    workspace_executor = WorkspaceToolExecutor(workspace_mgr)
    avatar_executor = AvatarToolExecutor(avatar_mgr, workspace_mgr)
    image_executor = ImageToolExecutor(config.image, workspace_mgr)
    task_tracker = TaskTracker()
    task_executor = TaskToolExecutor(task_tracker)

    # 基础组（始终可见）
    router.register_group(ToolGroup("notes", NOTES_TOOLS, notes_executor, is_base=True))
    router.register_group(ToolGroup("common", COMMON_TOOLS, common_executor, is_base=True))
    router.register_group(ToolGroup("task", TASK_TOOLS, task_executor, is_base=True))
    router.register_group(ToolGroup("workspace", WORKSPACE_TOOLS, workspace_executor, is_base=True))
    router.register_group(ToolGroup("avatar", AVATAR_TOOLS, avatar_executor, is_base=True))
    router.register_group(ToolGroup("image", IMAGE_TOOLS, image_executor, is_base=True))

    # 浏览器执行器（可选）
    browser_executor = None
    if config.browser.enabled:
        browser_executor = BrowserToolExecutor(
            config.browser, screenshot_dir=workspace_mgr.kaguya_dir / "screenshots",
        )

    # ── 子代理管理器 ──────────────────────────────────────────────────
    sub_agent_manager = SubAgentManager(
        agent_llm=agent_llm,
        desktop_tools=DESKTOP_TOOLS if config.desktop.enabled else None,
        desktop_executor=desktop_executor if config.desktop.enabled else None,
        browser_tools=BROWSER_TOOLS if config.browser.enabled else None,
        browser_executor=browser_executor,
    )
    agent_mgmt_executor = AgentManagementExecutor(sub_agent_manager)
    router.register_group(ToolGroup(
        "agent", AGENT_MANAGEMENT_TOOLS, agent_mgmt_executor, is_base=True,
    ))
    router.register_reset_callback(sub_agent_manager.close_all)

    # ── 扩展系统 ──────────────────────────────────────────────────────
    ext_manager = ExtensionManager()
    ext_dir = root / "extensions"
    if ext_dir.is_dir():
        ext_manager.load_from_directory(ext_dir)

    router.set_extension_manager(ext_manager)

    # ── ChatEngine ────────────────────────────────────────────────────
    engine = ChatEngine(
        llm=llm,
        memory=memory,
        router=router,
        persona=config.persona,
        avatar_manager=avatar_mgr,
        task_tracker=task_tracker,
    )

    # 创建扩展上下文并初始化所有扩展
    ext_ctx = ExtensionContext(engine=engine, memory=memory, app_config=config)
    await ext_manager.setup_all(ext_ctx)

    # ── 通知源 ─────────────────────────────────────────────────────────
    notification_source = None
    if config.desktop.enabled:
        from kaguya.desktop.notifications import WinRTNotificationSource
        notification_source = WinRTNotificationSource()

    # ── 意识调度器 ────────────────────────────────────────────────────
    consciousness = ConsciousnessScheduler(
        engine=engine,
        memory=memory,
        notification_source=notification_source,
        extension_manager=ext_manager,
        consciousness_config=config.consciousness,
        notifications_config=config.notifications,
        persona=config.persona,
        platform="desktop",
    )

    # ── 后台任务 ──────────────────────────────────────────────────────
    tasks: list[asyncio.Task] = []

    if config.admin.enabled:
        admin = AdminAPI(
            engine, memory, config.admin,
            app_config=config,
            persona_name=config.persona.name,
        )
        tasks.append(asyncio.create_task(admin.start()))

    tasks.append(asyncio.create_task(consciousness.heartbeat_loop()))
    tasks.append(asyncio.create_task(consciousness.notification_loop()))
    tasks.append(asyncio.create_task(consciousness.timer_loop()))

    # 扩展后台任务
    for coro in ext_manager.get_background_coroutines():
        tasks.append(asyncio.create_task(coro))

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
    await ext_manager.teardown_all()
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
