"""平台适配器基类（V2 简化版）。"""

from __future__ import annotations

import abc


class PlatformAdapter(abc.ABC):
    """平台适配器基类。"""

    def __init__(self, name: str):
        self.name = name

    @abc.abstractmethod
    async def run(self) -> None:
        """启动适配器的主循环（阻塞直到退出）。"""
        ...
