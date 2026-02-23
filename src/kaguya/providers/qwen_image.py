"""
千问图像 Provider — 基于 DashScope API。

提供两个工具：
- generate_image: 文生图（Z-Image Turbo）
- edit_image: 图像编辑（Qwen-Image-Edit）

生成的图片自动下载保存到 workspace。
"""

from __future__ import annotations

import base64
import uuid
from pathlib import Path
from typing import Any

import aiohttp
from loguru import logger

from kaguya.providers import BaseProvider
from kaguya.tools.registry import Tool
from kaguya.tools.workspace import WorkspaceManager


DASHSCOPE_API_URL = "https://dashscope-intl.aliyuncs.com/api/v1/services/aigc/multimodal-generation/generation"


# ===================== HTTP 辅助 =====================


async def _dashscope_generate(
    session: aiohttp.ClientSession,
    api_key: str,
    model: str,
    messages: list[dict],
    parameters: dict | None = None,
) -> dict:
    """调用 DashScope 多模态生成 API"""
    payload: dict[str, Any] = {
        "model": model,
        "input": {"messages": messages},
    }
    if parameters:
        payload["parameters"] = parameters

    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {api_key}",
    }

    try:
        async with session.post(DASHSCOPE_API_URL, json=payload, headers=headers) as resp:
            result = await resp.json()
            if "code" in result:
                logger.error(f"DashScope API 错误: {result.get('code')} - {result.get('message')}")
            return result
    except Exception as e:
        logger.error(f"DashScope API 调用失败: {e}")
        return {"code": "NetworkError", "message": str(e)}


async def _download_image(
    session: aiohttp.ClientSession,
    url: str,
    save_path: Path,
) -> bool:
    """下载图片到本地"""
    try:
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=120)) as resp:
            if resp.status == 200:
                data = await resp.read()
                save_path.write_bytes(data)
                return True
            logger.warning(f"下载图片失败: HTTP {resp.status}")
            return False
    except Exception as e:
        logger.error(f"下载图片异常: {e}")
        return False


# ===================== 工具 =====================


class GenerateImageTool(Tool):
    """文生图工具 — Z-Image Turbo"""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        api_key: str,
        workspace: WorkspaceManager,
    ):
        self._session = session
        self._api_key = api_key
        self._workspace = workspace

    @property
    def name(self):
        return "generate_image"

    @property
    def description(self):
        return (
            "文字生成图片。输入描述文字（prompt），生成一张图片并保存到 workspace。"
            "支持中英文提示词，可指定图片尺寸。返回图片在 workspace 中的保存路径。"
        )

    @property
    def parameters(self):
        return {
            "type": "object",
            "properties": {
                "prompt": {
                    "type": "string",
                    "description": "图片描述提示词（中英文均可，越详细越好）",
                },
                "size": {
                    "type": "string",
                    "description": "可选，图片尺寸 '宽*高'，默认 1024*1024。推荐: 1024*1024, 1280*720, 720*1280",
                    "default": "1024*1024",
                },
            },
            "required": ["prompt"],
        }

    async def execute(self, prompt: str, size: str = "1024*1024", **_) -> str:
        messages = [
            {"role": "user", "content": [{"text": prompt}]}
        ]
        params = {
            "prompt_extend": False,
            "size": size,
        }

        result = await _dashscope_generate(
            self._session, self._api_key, "z-image-turbo", messages, params,
        )

        if "code" in result:
            return f"图片生成失败: {result.get('message', result.get('code'))}"

        # 提取图片 URL
        try:
            choices = result["output"]["choices"]
            image_url = None
            for item in choices[0]["message"]["content"]:
                if "image" in item:
                    image_url = item["image"]
                    break
            if not image_url:
                return "图片生成成功但未返回图片 URL"
        except (KeyError, IndexError) as e:
            return f"解析生成结果失败: {e}"

        # 下载保存到 workspace
        filename = f"gen_{uuid.uuid4().hex[:8]}.png"
        save_dir = self._workspace.kaguya_dir / "images"
        save_dir.mkdir(exist_ok=True)
        save_path = save_dir / filename

        if await _download_image(self._session, image_url, save_path):
            rel_path = f"images/{filename}"
            logger.info(f"图片已生成并保存: {save_path}")
            return f"图片已生成并保存到 workspace: {rel_path}\n完整路径: {save_path}"
        else:
            return f"图片已生成但下载失败。临时 URL（24h 有效）: {image_url}"


class EditImageTool(Tool):
    """图像编辑工具 — Qwen-Image-Edit"""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        api_key: str,
        workspace: WorkspaceManager,
        model: str = "qwen-image-edit-max",
    ):
        self._session = session
        self._api_key = api_key
        self._workspace = workspace
        self._model = model

    @property
    def name(self):
        return "edit_image"

    @property
    def description(self):
        return (
            "编辑图片。输入 1-3 张图片路径和编辑指令，生成编辑后的图片。"
            "支持修改文字、增删物体、改变动作、迁移风格、多图融合等。"
            "返回编辑后图片在 workspace 中的保存路径。"
        )

    @property
    def parameters(self):
        return {
            "type": "object",
            "properties": {
                "image_paths": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "输入图片路径列表（1-3 张），支持本地路径或 workspace 相对路径",
                },
                "instruction": {
                    "type": "string",
                    "description": "编辑指令（如 '把背景换成海边' 或 '图1中的人穿上图2的衣服'）",
                },
                "size": {
                    "type": "string",
                    "description": "可选，输出尺寸 '宽*高'，默认自动匹配输入图片比例",
                },
            },
            "required": ["image_paths", "instruction"],
        }

    async def execute(
        self,
        image_paths: list[str],
        instruction: str,
        size: str = "",
        **_,
    ) -> str:
        if not image_paths or len(image_paths) > 3:
            return "需要 1-3 张输入图片"

        # 构建 content: images + text
        content: list[dict] = []
        for img_path in image_paths:
            b64_data = self._load_image_as_base64(img_path)
            if b64_data is None:
                return f"无法读取图片: {img_path}"
            content.append({"image": b64_data})
        content.append({"text": instruction})

        messages = [{"role": "user", "content": content}]
        params: dict[str, Any] = {
            "n": 1,
            "prompt_extend": True,
            "watermark": False,
        }
        if size:
            params["size"] = size

        result = await _dashscope_generate(
            self._session, self._api_key, self._model, messages, params,
        )

        if "code" in result:
            return f"图片编辑失败: {result.get('message', result.get('code'))}"

        # 提取图片 URL
        try:
            choices = result["output"]["choices"]
            image_url = None
            for item in choices[0]["message"]["content"]:
                if "image" in item:
                    image_url = item["image"]
                    break
            if not image_url:
                return "图片编辑成功但未返回图片 URL"
        except (KeyError, IndexError) as e:
            return f"解析编辑结果失败: {e}"

        # 下载保存
        filename = f"edit_{uuid.uuid4().hex[:8]}.png"
        save_dir = self._workspace.kaguya_dir / "images"
        save_dir.mkdir(exist_ok=True)
        save_path = save_dir / filename

        if await _download_image(self._session, image_url, save_path):
            rel_path = f"images/{filename}"
            logger.info(f"编辑后图片已保存: {save_path}")
            return f"图片已编辑并保存到 workspace: {rel_path}\n完整路径: {save_path}"
        else:
            return f"图片已编辑但下载失败。临时 URL（24h 有效）: {image_url}"

    def _load_image_as_base64(self, path_str: str) -> str | None:
        """加载图片为 base64 data URI"""
        path = Path(path_str)
        if not path.is_absolute():
            # 尝试从 kaguya workspace 解析
            path = self._workspace.kaguya_dir / path_str
        if not path.exists():
            return None

        ext = path.suffix.lower().lstrip(".")
        mime = {
            "png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg",
            "webp": "image/webp", "bmp": "image/bmp", "gif": "image/gif",
        }.get(ext, "image/png")

        b64 = base64.b64encode(path.read_bytes()).decode("utf-8")
        return f"data:{mime};base64,{b64}"


# ===================== Provider =====================


class QwenImageProvider(BaseProvider):
    """千问图像 Provider — 文生图 + 图像编辑"""

    def __init__(
        self,
        api_key: str,
        workspace: WorkspaceManager,
        edit_model: str = "qwen-image-edit-max",
    ):
        self._api_key = api_key
        self._workspace = workspace
        self._edit_model = edit_model
        self._session: aiohttp.ClientSession | None = None

    def _ensure_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()
        return self._session

    @property
    def name(self) -> str:
        return "qwen_image"

    def get_tools(self, phase: str = "chat") -> list[Tool]:
        session = self._ensure_session()
        return [
            GenerateImageTool(session, self._api_key, self._workspace),
            EditImageTool(session, self._api_key, self._workspace, self._edit_model),
        ]

    def get_system_prompt(self, phase: str = "chat") -> str:
        if phase == "consciousness":
            return (
                "你拥有图像创作能力：\n"
                "- generate_image: 用文字描述生成图片（越详细越好）\n"
                "- edit_image: 编辑已有图片（修改文字、换背景、换衣服、风格迁移等）\n"
                "生成的图片会保存在你的 workspace/images/ 里。"
            )
        return (
            "你可以用 generate_image 生成图片，用 edit_image 编辑图片。"
            "生成结果保存在 workspace 中，可用 send_message_to_user 的 image_path 发送给好朋友。"
        )
