"""image.py — 图像生成、编辑、查看工具。

使用 DashScope OpenAI-compatible images API 进行文生图和图编辑。
生成的图片保存到 workspace 中。
"""

from __future__ import annotations

import base64
import time
from pathlib import Path
from typing import Any, Optional

import aiohttp
from loguru import logger


# ---------------------------------------------------------------------------
# 工具 Schema 定义
# ---------------------------------------------------------------------------

IMAGE_TOOLS: list[dict] = [
    {
        "type": "function",
        "function": {
            "name": "generate_image",
            "description": (
                "根据文字描述生成图片。生成的图片会保存到 workspace 中。"
                "返回 workspace 中的文件路径和预览。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "prompt": {
                        "type": "string",
                        "description": "图片描述（英文效果更好）",
                    },
                    "size": {
                        "type": "string",
                        "description": "图片尺寸，默认 1024*1024",
                        "enum": ["512*512", "768*768", "1024*1024", "1024*768", "768*1024"],
                        "default": "1024*1024",
                    },
                },
                "required": ["prompt"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "edit_image",
            "description": (
                "编辑已有图片。支持修改文字、增删物体、改变风格等。"
                "输入图片必须在 workspace 中。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "image_path": {
                        "type": "string",
                        "description": "要编辑的图片的 workspace 相对路径",
                    },
                    "instruction": {
                        "type": "string",
                        "description": "编辑指令（如「把背景换成海边」）",
                    },
                },
                "required": ["image_path", "instruction"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "view_image",
            "description": "查看 workspace 中的图片。返回图片供你查看。",
            "parameters": {
                "type": "object",
                "properties": {
                    "image_path": {
                        "type": "string",
                        "description": "图片的 workspace 相对路径",
                    },
                },
                "required": ["image_path"],
            },
        },
    },
]


# ---------------------------------------------------------------------------
# 工具执行器
# ---------------------------------------------------------------------------


class ImageToolExecutor:
    """图像生成/编辑/查看执行器。"""

    def __init__(
        self,
        config,  # ImageConfig
        workspace_manager,  # WorkspaceManager
    ):
        self.config = config
        self.workspace = workspace_manager
        self._session: Optional[aiohttp.ClientSession] = None

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()
        return self._session

    async def close(self):
        if self._session and not self._session.closed:
            await self._session.close()

    async def execute(self, tool_name: str, args: dict[str, Any]) -> dict[str, Any]:
        handler = getattr(self, f"_tool_{tool_name}", None)
        if handler is None:
            return {"error": f"未知工具: {tool_name}"}
        if not self.config.enabled and tool_name != "view_image":
            return {"error": "图像生成功能未启用，请在配置中设置 [image] enabled = true"}
        try:
            return await handler(**args)
        except Exception as e:
            logger.error(f"图像工具 [{tool_name}] 执行失败: {e}")
            return {"error": str(e)}

    async def _tool_generate_image(
        self, prompt: str, size: str = "1024*1024"
    ) -> dict[str, Any]:
        """调用 DashScope images API 生成图片。"""
        session = await self._get_session()
        url = f"{self.config.base_url}/images/generations"
        headers = {
            "Authorization": f"Bearer {self.config.api_key}",
            "Content-Type": "application/json",
        }
        payload = {
            "model": self.config.model_generate,
            "input": {"prompt": prompt},
            "parameters": {"size": size, "n": 1},
        }

        logger.info(f"生成图片: {prompt[:50]}...")

        # DashScope images API 可能是异步任务
        async with session.post(url, json=payload, headers=headers) as resp:
            if resp.status != 200:
                text = await resp.text()
                return {"error": f"API 请求失败 ({resp.status}): {text}"}
            data = await resp.json()

        # 处理异步任务（DashScope 特有）
        image_url = await self._extract_image_url(data, session, headers)
        if not image_url:
            return {"error": f"未能获取生成结果: {data}"}

        # 下载图片并保存到 workspace
        save_path = await self._download_and_save(session, image_url, "generated")
        if not save_path:
            return {"error": "图片下载失败"}

        # 读取图片为 base64 返回预览
        resolved = self.workspace.resolve_path(save_path)
        img_b64 = base64.b64encode(resolved.read_bytes()).decode()

        return {
            "success": True,
            "path": save_path,
            "image_base64": img_b64,
            "image_media_type": "image/png",
            "text": f"图片已生成并保存到: {save_path}",
        }

    async def _tool_edit_image(
        self, image_path: str, instruction: str
    ) -> dict[str, Any]:
        """调用 DashScope 图像编辑 API。"""
        resolved = self.workspace.resolve_path(image_path)
        if not resolved.exists():
            return {"error": f"图片不存在: {image_path}"}

        img_data = resolved.read_bytes()
        img_b64 = base64.b64encode(img_data).decode()

        session = await self._get_session()
        url = f"{self.config.base_url}/images/generations"
        headers = {
            "Authorization": f"Bearer {self.config.api_key}",
            "Content-Type": "application/json",
        }
        payload = {
            "model": self.config.model_edit,
            "input": {
                "prompt": instruction,
                "base_image_url": f"data:image/png;base64,{img_b64}",
            },
            "parameters": {"n": 1},
        }

        logger.info(f"编辑图片: {image_path} — {instruction[:50]}...")

        async with session.post(url, json=payload, headers=headers) as resp:
            if resp.status != 200:
                text = await resp.text()
                return {"error": f"API 请求失败 ({resp.status}): {text}"}
            data = await resp.json()

        image_url = await self._extract_image_url(data, session, headers)
        if not image_url:
            return {"error": f"未能获取编辑结果: {data}"}

        save_path = await self._download_and_save(session, image_url, "edited")
        if not save_path:
            return {"error": "图片下载失败"}

        resolved_new = self.workspace.resolve_path(save_path)
        new_b64 = base64.b64encode(resolved_new.read_bytes()).decode()

        return {
            "success": True,
            "path": save_path,
            "image_base64": new_b64,
            "image_media_type": "image/png",
            "text": f"图片编辑完成，保存到: {save_path}",
        }

    async def _tool_view_image(self, image_path: str) -> dict[str, Any]:
        """查看 workspace 中的图片。"""
        resolved = self.workspace.resolve_path(image_path)
        if not resolved.exists():
            return {"error": f"图片不存在: {image_path}"}

        img_data = resolved.read_bytes()
        img_b64 = base64.b64encode(img_data).decode()

        # 猜测 MIME 类型
        suffix = resolved.suffix.lower()
        mime_map = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
                    ".gif": "image/gif", ".webp": "image/webp"}
        mime = mime_map.get(suffix, "image/png")

        return {
            "image_base64": img_b64,
            "image_media_type": mime,
            "text": f"图片: {image_path} ({len(img_data)} bytes)",
        }

    # -----------------------------------------------------------------------
    # DashScope 辅助方法
    # -----------------------------------------------------------------------

    async def _extract_image_url(
        self, data: dict, session: aiohttp.ClientSession, headers: dict
    ) -> Optional[str]:
        """从 DashScope 响应中提取图片 URL（处理异步任务轮询）。"""
        # 直接返回结果的情况
        output = data.get("output", {})
        results = output.get("results", [])
        if results:
            return results[0].get("url") or results[0].get("b64_image")

        # 异步任务：需要轮询
        task_id = output.get("task_id")
        if not task_id:
            return None

        task_url = f"{self.config.base_url.rstrip('/v1')}/tasks/{task_id}"
        for _ in range(60):  # 最多等 60 秒
            await __import__("asyncio").sleep(1)
            async with session.get(task_url, headers=headers) as resp:
                if resp.status != 200:
                    continue
                task_data = await resp.json()
                status = task_data.get("output", {}).get("task_status", "")
                if status == "SUCCEEDED":
                    results = task_data.get("output", {}).get("results", [])
                    if results:
                        return results[0].get("url") or results[0].get("b64_image")
                    return None
                elif status in ("FAILED", "UNKNOWN"):
                    return None

        return None

    async def _download_and_save(
        self, session: aiohttp.ClientSession, url_or_b64: str, prefix: str
    ) -> Optional[str]:
        """下载图片并保存到 workspace/images/。"""
        images_dir = self.workspace.kaguya_dir / "images"
        images_dir.mkdir(parents=True, exist_ok=True)

        filename = f"{prefix}_{int(time.time())}.png"
        save_path = images_dir / filename

        if url_or_b64.startswith("http"):
            try:
                async with session.get(url_or_b64) as resp:
                    if resp.status != 200:
                        return None
                    data = await resp.read()
                    save_path.write_bytes(data)
            except Exception as e:
                logger.error(f"图片下载失败: {e}")
                return None
        else:
            # base64 数据
            try:
                data = base64.b64decode(url_or_b64)
                save_path.write_bytes(data)
            except Exception as e:
                logger.error(f"base64 解码失败: {e}")
                return None

        return f"images/{filename}"
