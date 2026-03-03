"""extensions/manager.py — 扩展管理器：加载、聚合、生命周期管理。"""

from __future__ import annotations

import importlib.util
import inspect
from pathlib import Path
from typing import Any

from loguru import logger

from kaguya.extensions.base import Extension, ExtensionContext, Stage


class ExtensionManager:
    """管理所有已注册扩展的生命周期与聚合调度。"""

    def __init__(self) -> None:
        self._extensions: list[Extension] = []

    # ------------------------------------------------------------------
    # 注册 & 加载
    # ------------------------------------------------------------------

    def register(self, ext: Extension) -> None:
        """手动注册一个扩展实例。"""
        self._extensions.append(ext)
        logger.info(f"扩展注册: {ext.name}")

    def load_from_directory(self, ext_dir: Path) -> None:
        """扫描目录中的 ``.py`` 文件，自动发现 Extension 子类并注册。

        - 跳过 ``_`` 开头的文件
        - 单个文件加载失败不影响其他文件
        """
        ext_dir = Path(ext_dir)
        if not ext_dir.is_dir():
            return

        for py_file in sorted(ext_dir.glob("*.py")):
            if py_file.name.startswith("_"):
                continue
            try:
                self._load_module(py_file)
            except Exception as e:
                logger.warning(f"加载扩展文件 {py_file.name} 失败: {e}")

    def _load_module(self, py_file: Path) -> None:
        """从 .py 文件加载模块，发现并注册 Extension 子类。"""
        module_name = f"kaguya_ext_{py_file.stem}"
        spec = importlib.util.spec_from_file_location(module_name, py_file)
        if not spec or not spec.loader:
            return
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)

        for attr_name in dir(mod):
            obj = getattr(mod, attr_name)
            if (
                isinstance(obj, type)
                and issubclass(obj, Extension)
                and obj is not Extension
                and not inspect.isabstract(obj)
            ):
                ext = obj()
                self.register(ext)

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------

    async def setup_all(self, ctx: ExtensionContext) -> None:
        """初始化所有已注册扩展。单个扩展异常不中断其他。"""
        for ext in self._extensions:
            try:
                await ext.setup(ctx)
                logger.debug(f"扩展 {ext.name} 初始化完成")
            except Exception as e:
                logger.warning(f"扩展 {ext.name} 初始化失败: {e}")

    async def teardown_all(self) -> None:
        """清理所有扩展。"""
        for ext in self._extensions:
            try:
                await ext.teardown()
            except Exception as e:
                logger.debug(f"扩展 {ext.name} 清理异常: {e}")

    # ------------------------------------------------------------------
    # 聚合：通知
    # ------------------------------------------------------------------

    async def get_all_notifications(self) -> list[dict[str, Any]]:
        """聚合所有扩展的通知。单个扩展异常不影响其他。"""
        result: list[dict[str, Any]] = []
        for ext in self._extensions:
            try:
                notifs = await ext.get_notifications()
                if notifs:
                    result.extend(notifs)
            except Exception as e:
                logger.debug(f"扩展 {ext.name} 通知拉取失败: {e}")
        return result

    def has_notification_extensions(self) -> bool:
        """是否有任何扩展可能提供通知。

        简单检查：只要有扩展注册就返回 True（无法提前判断扩展是否实现了通知）。
        """
        return len(self._extensions) > 0

    # ------------------------------------------------------------------
    # 聚合：工具
    # ------------------------------------------------------------------

    def get_all_tools(self, stage: Stage) -> list[dict]:
        """聚合所有扩展在指定阶段的工具定义。"""
        tools: list[dict] = []
        for ext in self._extensions:
            try:
                ext_tools = ext.get_tools(stage)
                if ext_tools:
                    tools.extend(ext_tools)
            except Exception as e:
                logger.debug(f"扩展 {ext.name} 获取工具失败: {e}")
        return tools

    async def execute_tool(self, tool_name: str, args: dict[str, Any]) -> dict[str, Any] | None:
        """遍历扩展尝试执行工具。

        返回 ``None`` 表示没有扩展能处理该工具（区别于 ``{"error": ...}``）。
        """
        for ext in self._extensions:
            try:
                result = await ext.execute_tool(tool_name, args)
                if result.get("error") != f"未知工具: {tool_name}":
                    return result
            except Exception as e:
                logger.error(f"扩展 {ext.name} 执行工具 {tool_name} 异常: {e}")
                return {"error": str(e)}
        return None

    # ------------------------------------------------------------------
    # 聚合：Prompt
    # ------------------------------------------------------------------

    async def get_all_prompts(self, stage: Stage) -> list[str]:
        """聚合所有扩展在指定阶段的 prompt 片段。"""
        prompts: list[str] = []
        for ext in self._extensions:
            try:
                p = await ext.get_prompt(stage)
                if p and p.strip():
                    prompts.append(p.strip())
            except Exception as e:
                logger.debug(f"扩展 {ext.name} 获取 prompt 失败: {e}")
        return prompts

    # ------------------------------------------------------------------
    # 后台任务
    # ------------------------------------------------------------------

    def get_background_coroutines(self) -> list:
        """收集所有覆盖了 ``run_background`` 的扩展协程。"""
        coros = []
        for ext in self._extensions:
            # 只有子类显式覆盖了 run_background 才启动
            if type(ext).run_background is not Extension.run_background:
                coros.append(ext.run_background())
        return coros
