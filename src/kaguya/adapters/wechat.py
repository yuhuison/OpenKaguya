"""
微信适配器 — 基于 wechat-v864 代理服务。

通过 WebSocket 接收微信消息，通过 HTTP API 发送消息。
支持文本消息和图片消息，内置 3 秒消息聚合防抖。
"""

from __future__ import annotations

import asyncio
import base64
import json
import uuid
from dataclasses import dataclass, field
from typing import Optional

import aiohttp
from loguru import logger

from kaguya.adapters.base import PlatformAdapter
from kaguya.config import WeChatConfig
from kaguya.core.identity import UserIdentityManager
from kaguya.core.types import Attachment, Platform, UnifiedMessage, UserInfo
from kaguya.tools.workspace import WorkspaceManager


# ============================================================
# 消息聚合缓冲区
# ============================================================

DEBOUNCE_SECONDS = 3.0  # 防抖等待时间


@dataclass
class PendingBuffer:
    """一个用户/群的待处理消息缓冲区"""

    texts: list[str] = field(default_factory=list)
    images: list[str] = field(default_factory=list)  # base64 JPEG
    timer: Optional[asyncio.Task] = field(default=None, repr=False)

    # 上下文（取自第一条消息）
    message_id: str = ""
    sender: Optional[UserInfo] = None
    group_id: Optional[str] = None
    platform_target: str = ""  # 微信原始目标（wxid 或 chatroom）
    user_context: Optional[str] = None

    def is_empty(self) -> bool:
        return not self.texts and not self.images

    def reset(self) -> None:
        self.texts.clear()
        self.images.clear()
        self.timer = None
        self.message_id = ""
        self.sender = None
        self.group_id = None
        self.platform_target = ""
        self.user_context = None


class WeChatAdapter(PlatformAdapter):
    """
    微信适配器：通过 wechat-v864 代理收发微信消息。

    接收: WebSocket (ws://BASE_URL/ws/GetSyncMsg?key=API_KEY)
    发送: HTTP POST (BASE_URL/message/SendTextMessage?key=API_KEY)

    特性:
    - 3 秒消息聚合防抖（等用户发完再处理）
    - 支持文本 (msg_type=1) 和图片 (msg_type=3)
    """

    def __init__(
        self,
        config: WeChatConfig,
        identity_manager: UserIdentityManager,
        workspace: WorkspaceManager | None = None,
    ):
        super().__init__("wechat")
        self.config = config
        self.identity = identity_manager
        self._workspace = workspace
        self._running = False
        self._ws_task: Optional[asyncio.Task] = None
        self._session: Optional[aiohttp.ClientSession] = None

        # 白名单集合（查询更快）
        self._whitelist_users = set(config.whitelist_users)
        self._whitelist_groups = set(config.whitelist_groups)

        # 消息聚合缓冲区：key = history_key（group_id 或 user wxid）
        self._pending: dict[str, PendingBuffer] = {}

        logger.info(
            f"微信适配器初始化: base_url={config.base_url}, "
            f"白名单用户={len(self._whitelist_users)}, "
            f"白名单群组={len(self._whitelist_groups)}"
        )

    async def start(self) -> None:
        """启动 WebSocket 接收循环"""
        self._running = True
        self._session = aiohttp.ClientSession()
        self._ws_task = asyncio.create_task(self._ws_loop())
        logger.info("📱 微信适配器已启动")

    async def stop(self) -> None:
        """停止适配器"""
        self._running = False
        # 取消所有挂起的防抖定时器
        for buf in self._pending.values():
            if buf.timer and not buf.timer.done():
                buf.timer.cancel()
        self._pending.clear()
        if self._ws_task:
            self._ws_task.cancel()
            try:
                await self._ws_task
            except asyncio.CancelledError:
                pass
        if self._session:
            await self._session.close()
        logger.info("📱 微信适配器已停止")

    # ==================== WebSocket 消息接收 ====================

    async def _ws_loop(self) -> None:
        """WebSocket 主循环，自动重连"""
        ws_url = (
            self.config.base_url
            .replace("http://", "ws://")
            .replace("https://", "wss://")
        )
        ws_url = f"{ws_url}/ws/GetSyncMsg?key={self.config.api_key}"

        while self._running:
            try:
                logger.info(f"正在连接微信 WebSocket: {ws_url[:50]}...")
                async with self._session.ws_connect(ws_url) as ws:
                    logger.info("✅ 微信 WebSocket 已连接")
                    async for msg in ws:
                        if not self._running:
                            break
                        if msg.type == aiohttp.WSMsgType.TEXT:
                            await self._handle_ws_message(msg.data)
                        elif msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                            logger.warning(f"WebSocket 异常: {msg.type}")
                            break
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"WebSocket 连接失败: {e}")

            if self._running:
                logger.info("5 秒后重新连接...")
                await asyncio.sleep(5)

    async def _handle_ws_message(self, raw: str) -> None:
        """处理一条 WebSocket 消息"""
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            logger.warning(f"收到非 JSON 消息: {raw[:100]}")
            return

        # 提取字段
        msg_type = data.get("msg_type", 0)
        from_user = self._extract_str(data.get("from_user_name", {}))
        to_user = self._extract_str(data.get("to_user_name", {}))
        content = self._extract_str(data.get("content", {}))
        push_content = data.get("push_content", "")
        new_msg_id = str(data.get("new_msg_id", data.get("msg_id", "")))

        # 支持文本消息(1) 和图片消息(3)
        if msg_type not in (1, 3):
            logger.debug(f"跳过不支持的消息类型: type={msg_type} from={from_user}")
            return

        if not from_user:
            return

        # 判断群消息 vs 私聊
        is_group = from_user.endswith("@chatroom") or to_user.endswith("@chatroom")
        group_id: Optional[str] = None
        actual_sender = from_user
        actual_content = content

        if is_group:
            if from_user.endswith("@chatroom"):
                group_id = from_user
                if ":\n" in content:
                    actual_sender, actual_content = content.split(":\n", 1)
                else:
                    actual_sender = from_user
                    actual_content = content
            else:
                group_id = to_user
                actual_sender = from_user

        # 白名单检查
        if is_group:
            if group_id not in self._whitelist_groups:
                return
        else:
            if actual_sender not in self._whitelist_users:
                return

        # ID 映射
        unified_id = self.identity.resolve("wechat", actual_sender)
        nickname = self.identity.get_nickname(
            "wechat", actual_sender,
            fallback=self._extract_nickname(push_content),
        )

        # 确定聚合 key（群聊用 group_id，私聊用发送者 wxid）
        buffer_key = group_id if is_group else actual_sender
        platform_target = group_id if is_group else actual_sender

        # 提取图片 base64（msg_type=3）
        image_b64: Optional[str] = None
        if msg_type == 3:
            image_b64 = self._extract_image_base64(data)
            if not image_b64:
                logger.warning("收到图片消息但无法提取图片数据")
                return
            logger.info(
                f"📩 微信{'群' if is_group else '私聊'}图片: "
                f"{nickname}({unified_id}): [图片 {len(image_b64) // 1024}KB]"
            )
        else:
            if not actual_content:
                return
            logger.info(
                f"📩 微信{'群' if is_group else '私聊'}消息: "
                f"{nickname}({unified_id}): {actual_content[:50]}"
            )

        # === 消息聚合防抖 ===
        sender_info = UserInfo(
            user_id=unified_id,
            nickname=nickname,
            platform=Platform.WECHAT,
        )
        user_context = self.identity.build_user_context(unified_id)

        buf = self._pending.get(buffer_key)
        if buf is None:
            buf = PendingBuffer()
            self._pending[buffer_key] = buf

        # 追加内容
        if msg_type == 1:
            buf.texts.append(actual_content)
        elif msg_type == 3 and image_b64:
            buf.images.append(image_b64)

        # 更新上下文（取第一条消息的信息，或持续更新）
        if not buf.sender:
            buf.message_id = new_msg_id or str(uuid.uuid4())
            buf.sender = sender_info
            buf.group_id = group_id
            buf.platform_target = platform_target
            buf.user_context = user_context

        # 只有文本消息才启动/重置防抖定时器
        # 图片消息只是静默加入缓冲区，等待后续文本消息触发处理
        if msg_type == 1:
            if buf.timer and not buf.timer.done():
                buf.timer.cancel()
            buf.timer = asyncio.create_task(self._flush_after_delay(buffer_key))

    async def _flush_after_delay(self, buffer_key: str) -> None:
        """防抖定时器：等待 N 秒后刷新缓冲区"""
        try:
            await asyncio.sleep(DEBOUNCE_SECONDS)
            await self._flush_buffer(buffer_key)
        except asyncio.CancelledError:
            pass  # 被新消息取消，正常行为

    async def _flush_buffer(self, buffer_key: str) -> None:
        """将缓冲区中的消息合并为一个 UnifiedMessage 并提交处理"""
        buf = self._pending.pop(buffer_key, None)
        if buf is None or buf.is_empty():
            return

        # 构建合并后的文本内容
        merged_content = "\n".join(buf.texts) if buf.texts else ""

        # 构建附件列表：将图片持久化到 workspace/.images/，使用占位符
        attachments: list[Attachment] = []
        image_placeholders: list[str] = []

        for i, img_b64 in enumerate(buf.images):
            if self._workspace and buf.sender:
                try:
                    filename = self._workspace.save_image(
                        user_id=buf.sender.user_id,
                        data=img_b64,
                        mime_type="image/jpeg",
                    )
                    placeholder = f"[workspace_image:{buf.sender.user_id}:{filename}]"
                    image_placeholders.append(placeholder)
                    attachments.append(Attachment(
                        type="image",
                        mime_type="image/jpeg",
                        data=img_b64,   # 当前轮 LLM 调用仍使用 base64（直接给 vision）
                        filename=filename,
                        metadata={"workspace_ref": filename, "user_id": buf.sender.user_id},
                    ))
                except Exception as e:
                    logger.error(f"图片保存失败: {e}，回退到内存 base64")
                    attachments.append(Attachment(
                        type="image", mime_type="image/jpeg",
                        data=img_b64, filename=f"wechat_image_{i}.jpg",
                    ))
            else:
                attachments.append(Attachment(
                    type="image", mime_type="image/jpeg",
                    data=img_b64, filename=f"wechat_image_{i}.jpg",
                ))

        # 如果只有图片没有文字，用占位符作为内容（以便后续历史中能展开）
        if not merged_content:
            if image_placeholders:
                merged_content = " ".join(image_placeholders)
            elif attachments:
                merged_content = "[用户发送了图片]"


        message = UnifiedMessage(
            message_id=buf.message_id,
            platform=Platform.WECHAT,
            sender=buf.sender,
            content=merged_content,
            group_id=buf.group_id,
            attachments=attachments,
        )

        # 注入用户上下文
        if buf.user_context:
            message._user_context = buf.user_context

        logger.info(
            f"📦 消息聚合完毕: {len(buf.texts)}条文字 + {len(buf.images)}张图片 → 提交处理"
        )

        # 调用处理器
        if self._handler:
            try:
                target = buf.platform_target
                send_count = 0

                async def _send_now(text: str, image_path: str | None = None):
                    nonlocal send_count
                    if send_count > 0:
                        import random
                        delay = random.uniform(0.5, 1.5) + len(text) * 0.05
                        delay = min(delay, 4.0)
                        await asyncio.sleep(delay)
                    send_count += 1
                    if text:
                        await self._send_single(target, text)
                    if image_path:
                        await self._send_image(target, image_path)

                await self._handler(message, send_callback=_send_now)
            except Exception as e:
                logger.error(f"消息处理失败: {e}")

    # ==================== 图片提取 ====================

    @staticmethod
    def _extract_image_base64(data: dict) -> Optional[str]:
        """从微信消息 JSON 中提取图片的 base64 数据"""
        img_buf = data.get("img_buf")
        if not img_buf:
            return None

        buffer = img_buf.get("buffer")
        if not buffer or not isinstance(buffer, str):
            return None

        # 验证 base64 有效性：尝试解码前几字节
        try:
            sample = base64.b64decode(buffer[:100] + "==")
            if len(sample) < 2:
                return None
        except Exception:
            return None

        return buffer

    # ==================== 发送消息 ====================

    async def _send_single(self, target: str, text: str) -> None:
        """发送单条文本消息"""
        url = f"{self.config.base_url}/message/SendTextMessage?key={self.config.api_key}"
        payload = {
            "MsgItem": [{
                "ToUserName": target,
                "TextContent": text,
                "MsgType": 1,
                "AtWxIDList": [],
            }]
        }
        try:
            async with self._session.post(url, json=payload) as resp:
                result = await resp.json()
                if result.get("Code") != 200:
                    logger.warning(f"发送消息失败: {result}")
                else:
                    logger.debug(f"📤 微信消息已发送到 {target}: {text[:50]}")
        except Exception as e:
            logger.error(f"发送消息异常: {e}")

    async def _send_image(self, target: str, image_path: str) -> None:
        """发送图片消息（读取本地文件 → base64 → SendImageMessage API）"""
        from pathlib import Path
        path = Path(image_path)
        if not path.exists():
            logger.warning(f"图片文件不存在: {image_path}")
            return

        try:
            image_data = path.read_bytes()
            image_b64 = base64.b64encode(image_data).decode("ascii")
        except Exception as e:
            logger.error(f"读取图片文件失败: {e}")
            return

        url = f"{self.config.base_url}/message/SendImageMessage?key={self.config.api_key}"
        payload = {
            "MsgItem": [{
                "ToUserName": target,
                "ImageContent": image_b64,
                "MsgType": 2,
                "AtWxIDList": [],
            }]
        }
        try:
            async with self._session.post(url, json=payload) as resp:
                result = await resp.json()
                if result.get("Code") != 200:
                    logger.warning(f"发送图片失败: {result}")
                else:
                    logger.debug(f"📤 微信图片已发送到 {target}: {path.name}")
        except Exception as e:
            logger.error(f"发送图片异常: {e}")

    async def send_messages(
        self,
        user_id: str,
        messages: list[str],
        group_id: str | None = None,
    ) -> None:
        """发送文本消息到微信"""
        target = group_id or user_id

        # 反向查找：如果 user_id 是统一 ID（如 "alice"），需要找到微信原始 ID
        if not target.startswith("wxid_") and not target.endswith("@chatroom"):
            platform_ids = self.identity.get_platform_ids(target)
            wechat_ids = [pid.removeprefix("wechat:") for pid in platform_ids if pid.startswith("wechat:")]
            if wechat_ids:
                target = wechat_ids[0]
            else:
                logger.warning(f"无法找到 {target} 的微信 ID，跳过发送")
                return

        url = f"{self.config.base_url}/message/SendTextMessage?key={self.config.api_key}"

        import random

        for i, text in enumerate(messages):
            if i > 0:
                delay = random.uniform(0.5, 1.5) + len(text) * 0.05
                delay = min(delay, 4.0)
                await asyncio.sleep(delay)

            payload = {
                "MsgItem": [{
                    "ToUserName": target,
                    "TextContent": text,
                    "MsgType": 1,
                    "AtWxIDList": [],
                }]
            }
            try:
                async with self._session.post(url, json=payload) as resp:
                    result = await resp.json()
                    if result.get("Code") != 200:
                        logger.warning(f"发送消息失败: {result}")
                    else:
                        logger.debug(f"📤 微信消息已发送到 {target}: {text[:50]}")
            except Exception as e:
                logger.error(f"发送消息异常: {e}")

    # ==================== 工具方法 ====================

    @staticmethod
    def _extract_str(value) -> str:
        """提取 protobuf JSON 的 {str: "xxx"} 格式"""
        if isinstance(value, dict):
            return value.get("str", value.get("string", ""))
        if isinstance(value, str):
            return value
        return str(value) if value else ""

    @staticmethod
    def _extract_nickname(push_content: str) -> str:
        """从 pushContent（如 '昵称: 消息摘要'）中提取昵称"""
        if ":" in push_content:
            return push_content.split(":", 1)[0].strip()
        if "：" in push_content:
            return push_content.split("：", 1)[0].strip()
        return ""
