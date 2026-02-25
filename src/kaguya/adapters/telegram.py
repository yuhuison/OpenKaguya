"""
Telegram 适配器 — 基于 Bot API 长轮询模式。

通过 Telegram Bot API 接收和发送消息，无需额外依赖（复用 aiohttp）。
支持文本、图片（photo）、文件（document）消息，内置 3 秒消息聚合防抖。

使用前提：
  1. 通过 @BotFather 创建 Bot，获取 Bot Token
  2. 在超级群中需将 Bot 设为管理员，或关闭隐私模式才能读取全部消息
"""

from __future__ import annotations

import asyncio
import base64
import random
from dataclasses import dataclass, field
from typing import Optional

import aiohttp
from loguru import logger

from kaguya.adapters.base import PlatformAdapter
from kaguya.config import TelegramConfig
from kaguya.core.group import GroupFilter
from kaguya.core.identity import UserIdentityManager
from kaguya.core.types import Attachment, Platform, UnifiedMessage, UserInfo
from kaguya.tools.workspace import WorkspaceManager


DEBOUNCE_SECONDS = 3.0


@dataclass
class TelegramBuffer:
    """一个用户/群的待处理消息缓冲区"""

    texts: list[str] = field(default_factory=list)
    images: list[str] = field(default_factory=list)    # base64
    files: list[dict] = field(default_factory=list)    # [{"filename", "data", "size"}]
    timer: Optional[asyncio.Task] = field(default=None, repr=False)

    message_id: str = ""
    sender: Optional[UserInfo] = None
    group_id: Optional[str] = None
    platform_chat_id: str = ""    # Telegram chat_id（字符串）
    user_context: Optional[str] = None

    def is_empty(self) -> bool:
        return not self.texts and not self.images and not self.files


class TelegramAdapter(PlatformAdapter):
    """
    Telegram 适配器：通过 Bot API 长轮询收发消息。

    接收: GET https://api.telegram.org/bot{token}/getUpdates (long polling, timeout=30s)
    发送: POST .../sendMessage / sendPhoto / sendDocument

    特性:
    - 3 秒消息聚合防抖
    - 支持文本、图片（photo）、文件（document）
    - 群聊过滤（GroupFilter）
    """

    def __init__(
        self,
        config: TelegramConfig,
        identity_manager: UserIdentityManager,
        workspace: WorkspaceManager | None = None,
        group_filter: GroupFilter | None = None,
    ):
        super().__init__("telegram")
        self.config = config
        self.identity = identity_manager
        self._workspace = workspace
        self._group_filter = group_filter
        self._running = False
        self._poll_task: Optional[asyncio.Task] = None
        self._session: Optional[aiohttp.ClientSession] = None
        self._offset = 0

        # 白名单（统一转字符串方便比较）
        self._whitelist_users: set[str] = {str(uid) for uid in config.whitelist_users}
        self._whitelist_groups: set[str] = {str(gid) for gid in config.whitelist_groups}

        self._pending: dict[str, TelegramBuffer] = {}

        logger.info(
            f"Telegram 适配器初始化: "
            f"白名单用户={len(self._whitelist_users)}, "
            f"白名单群组={len(self._whitelist_groups)}"
        )

    @property
    def _api(self) -> str:
        return f"https://api.telegram.org/bot{self.config.bot_token}"

    # ==================== 生命周期 ====================

    async def start(self) -> None:
        self._running = True
        self._session = aiohttp.ClientSession()
        self._poll_task = asyncio.create_task(self._poll_loop())
        logger.info("📲 Telegram 适配器已启动")

    async def stop(self) -> None:
        self._running = False
        for buf in self._pending.values():
            if buf.timer and not buf.timer.done():
                buf.timer.cancel()
        self._pending.clear()
        if self._poll_task:
            self._poll_task.cancel()
            try:
                await self._poll_task
            except asyncio.CancelledError:
                pass
        if self._session:
            await self._session.close()
        logger.info("📲 Telegram 适配器已停止")

    # ==================== 长轮询接收 ====================

    async def _poll_loop(self) -> None:
        """getUpdates 长轮询主循环，自动重试"""
        while self._running:
            try:
                updates = await self._get_updates()
                for update in updates:
                    asyncio.create_task(self._handle_update(update))
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Telegram 轮询异常: {e}")
                await asyncio.sleep(5)

    async def _get_updates(self) -> list[dict]:
        """发起一次 long polling 请求，返回新消息列表"""
        params = {
            "offset": self._offset,
            "timeout": 30,
            "allowed_updates": ["message"],
        }
        try:
            async with self._session.get(
                f"{self._api}/getUpdates",
                params=params,
                timeout=aiohttp.ClientTimeout(total=40),
            ) as resp:
                data = await resp.json()
                if not data.get("ok"):
                    logger.warning(f"getUpdates 失败: {data}")
                    await asyncio.sleep(5)
                    return []
                updates = data.get("result", [])
                if updates:
                    self._offset = updates[-1]["update_id"] + 1
                return updates
        except asyncio.TimeoutError:
            return []  # 正常的轮询超时，立即重试
        except Exception as e:
            logger.error(f"getUpdates 异常: {e}")
            await asyncio.sleep(5)
            return []

    async def _handle_update(self, update: dict) -> None:
        """处理一条 Telegram Update"""
        message = update.get("message")
        if not message:
            return

        chat = message.get("chat", {})
        chat_id = str(chat.get("id", ""))
        chat_type = chat.get("type", "private")  # private / group / supergroup / channel
        from_user = message.get("from", {})
        raw_user_id = str(from_user.get("id", ""))

        if not raw_user_id or not chat_id:
            return

        is_group = chat_type in ("group", "supergroup")
        group_id: Optional[str] = chat_id if is_group else None

        # 白名单检查
        if is_group:
            if chat_id not in self._whitelist_groups:
                return
        else:
            if raw_user_id not in self._whitelist_users:
                return

        # 提取消息内容
        text = (message.get("text") or message.get("caption") or "").strip()
        photo_list = message.get("photo")   # list[PhotoSize]，最后一个是最大分辨率
        document = message.get("document")

        # 昵称（从 Telegram 字段拼接）
        name_parts = [from_user.get("first_name", ""), from_user.get("last_name", "")]
        raw_nickname = (
            " ".join(p for p in name_parts if p).strip()
            or from_user.get("username", raw_user_id)
        )

        # 身份解析
        unified_id = self.identity.resolve("telegram", raw_user_id)
        nickname = self.identity.get_nickname("telegram", raw_user_id, fallback=raw_nickname)
        user_context = self.identity.build_user_context(unified_id)

        # 聚合 key
        buffer_key = chat_id if is_group else raw_user_id

        buf = self._pending.get(buffer_key)
        if buf is None:
            buf = TelegramBuffer()
            self._pending[buffer_key] = buf

        if not buf.sender:
            buf.message_id = str(message.get("message_id", ""))
            buf.sender = UserInfo(user_id=unified_id, nickname=nickname, platform=Platform.TELEGRAM)
            buf.group_id = group_id
            buf.platform_chat_id = chat_id
            buf.user_context = user_context

        if text:
            buf.texts.append(text)
            logger.info(
                f"📩 Telegram {'群' if is_group else '私聊'}消息: "
                f"{nickname}({unified_id}): {text[:50]}"
            )

        if photo_list:
            largest = photo_list[-1]
            img_b64 = await self._download_as_base64(largest["file_id"])
            if img_b64:
                buf.images.append(img_b64)
                logger.info(f"📩 Telegram 图片: {nickname}({unified_id}): [{len(img_b64)//1024}KB]")

        if document:
            file_size = document.get("file_size", 0)
            if file_size < 20 * 1024 * 1024:  # Telegram 文件限制 20MB
                file_b64 = await self._download_as_base64(document["file_id"])
                if file_b64:
                    filename = document.get("file_name", "file")
                    buf.files.append({"filename": filename, "data": file_b64, "size": file_size})
                    logger.info(
                        f"📩 Telegram 文件: {nickname}({unified_id}): "
                        f"[{filename} {file_size//1024}KB]"
                    )

        # 文本和文件触发/重置防抖定时器；图片静默加入缓冲区
        if text or document:
            if buf.timer and not buf.timer.done():
                buf.timer.cancel()
            buf.timer = asyncio.create_task(self._flush_after_delay(buffer_key))

    async def _flush_after_delay(self, buffer_key: str) -> None:
        try:
            await asyncio.sleep(DEBOUNCE_SECONDS)
            await self._flush_buffer(buffer_key)
        except asyncio.CancelledError:
            pass

    async def _flush_buffer(self, buffer_key: str) -> None:
        """将缓冲区消息合并为 UnifiedMessage 并提交 ChatEngine 处理"""
        buf = self._pending.pop(buffer_key, None)
        if buf is None or buf.is_empty():
            return

        merged_content = "\n".join(buf.texts) if buf.texts else ""

        # 处理图片附件
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
                    image_placeholders.append(f"[workspace_image:{buf.sender.user_id}:{filename}]")
                    attachments.append(Attachment(
                        type="image", mime_type="image/jpeg",
                        data=img_b64, filename=filename,
                        metadata={"workspace_ref": filename, "user_id": buf.sender.user_id},
                    ))
                except Exception as e:
                    logger.error(f"图片保存失败: {e}")
                    attachments.append(Attachment(
                        type="image", mime_type="image/jpeg",
                        data=img_b64, filename=f"tg_image_{i}.jpg",
                    ))
            else:
                attachments.append(Attachment(
                    type="image", mime_type="image/jpeg",
                    data=img_b64, filename=f"tg_image_{i}.jpg",
                ))

        # 处理文件附件
        file_placeholders: list[str] = []
        for f_info in buf.files:
            if self._workspace and buf.sender:
                try:
                    saved_name = self._workspace.save_file(
                        user_id=buf.sender.user_id,
                        filename=f_info["filename"],
                        data=f_info["data"],
                    )
                    file_placeholders.append(f"[workspace_file:{buf.sender.user_id}:{saved_name}]")
                    attachments.append(Attachment(
                        type="file", filename=saved_name,
                        metadata={
                            "original_filename": f_info["filename"],
                            "workspace_ref": saved_name,
                            "user_id": buf.sender.user_id,
                            "size": f_info["size"],
                        },
                    ))
                except Exception as e:
                    logger.error(f"文件保存失败: {e}")
                    file_placeholders.append(f"[用户发送了文件: {f_info['filename']}]")
            else:
                file_placeholders.append(f"[用户发送了文件: {f_info['filename']}]")

        if not merged_content:
            all_placeholders = image_placeholders + file_placeholders
            merged_content = " ".join(all_placeholders) if all_placeholders else "[用户发送了附件]"

        # 群聊过滤（在进入 ChatEngine 之前，零开销）
        group_id = buf.group_id
        if group_id and self._group_filter:
            should, reason = self._group_filter.should_reply(merged_content, group_id)
            if not should:
                logger.debug(f"群聊过滤: 跳过 [{group_id}] ({reason})")
                return
            logger.debug(f"群聊过滤: 回复 [{group_id}] (原因: {reason})")

        message = UnifiedMessage(
            message_id=buf.message_id,
            platform=Platform.TELEGRAM,
            sender=buf.sender,
            content=merged_content,
            group_id=group_id,
            attachments=attachments,
        )
        if buf.user_context:
            message._user_context = buf.user_context

        logger.info(
            f"📦 消息聚合完毕: {len(buf.texts)}条文字 + "
            f"{len(buf.images)}张图片 + {len(buf.files)}个文件 → 提交处理"
        )

        if self._handler:
            try:
                chat_id = buf.platform_chat_id
                send_count = 0

                async def _send_now(
                    text: str,
                    image_path: str | None = None,
                    file_path: str | None = None,
                    **_,
                ):
                    nonlocal send_count
                    if send_count > 0:
                        delay = random.uniform(0.5, 1.5) + len(text) * 0.05
                        delay = min(delay, 4.0)
                        await asyncio.sleep(delay)
                    send_count += 1
                    if send_count == 1 and group_id and self._group_filter:
                        self._group_filter.mark_replied(group_id)
                    if text:
                        await self._send_text(chat_id, text)
                    if image_path:
                        await self._send_photo(chat_id, image_path)
                    if file_path:
                        await self._send_document(chat_id, file_path)

                await self._handler(message, send_callback=_send_now)
            except Exception as e:
                logger.error(f"消息处理失败: {e}")

    # ==================== 文件下载 ====================

    async def _download_as_base64(self, file_id: str) -> Optional[str]:
        """通过 file_id 下载 Telegram 文件，返回 base64 字符串"""
        try:
            async with self._session.get(
                f"{self._api}/getFile",
                params={"file_id": file_id},
            ) as resp:
                data = await resp.json()
                if not data.get("ok"):
                    logger.warning(f"getFile 失败: {data}")
                    return None
                file_path = data["result"].get("file_path")
                if not file_path:
                    return None

            download_url = (
                f"https://api.telegram.org/file/bot{self.config.bot_token}/{file_path}"
            )
            async with self._session.get(
                download_url, timeout=aiohttp.ClientTimeout(total=60)
            ) as resp:
                if resp.status != 200:
                    logger.warning(f"文件下载失败: HTTP {resp.status}")
                    return None
                content = await resp.read()
                return base64.b64encode(content).decode("ascii")
        except Exception as e:
            logger.error(f"文件下载异常: {e}")
            return None

    # ==================== 发送消息 ====================

    async def _send_text(self, chat_id: str, text: str) -> None:
        try:
            async with self._session.post(
                f"{self._api}/sendMessage",
                json={"chat_id": chat_id, "text": text},
            ) as resp:
                result = await resp.json()
                if not result.get("ok"):
                    logger.warning(f"sendMessage 失败: {result}")
                else:
                    logger.debug(f"📤 Telegram 消息已发送到 {chat_id}: {text[:50]}")
        except Exception as e:
            logger.error(f"sendMessage 异常: {e}")

    async def _send_photo(self, chat_id: str, image_path: str) -> None:
        from pathlib import Path
        path = Path(image_path)
        if not path.exists():
            logger.warning(f"图片文件不存在: {image_path}")
            return
        try:
            with path.open("rb") as f:
                form = aiohttp.FormData()
                form.add_field("chat_id", chat_id)
                form.add_field("photo", f, filename=path.name, content_type="image/jpeg")
                async with self._session.post(f"{self._api}/sendPhoto", data=form) as resp:
                    result = await resp.json()
                    if not result.get("ok"):
                        logger.warning(f"sendPhoto 失败: {result}")
                    else:
                        logger.debug(f"📤 Telegram 图片已发送到 {chat_id}: {path.name}")
        except Exception as e:
            logger.error(f"sendPhoto 异常: {e}")

    async def _send_document(self, chat_id: str, file_path: str) -> None:
        from pathlib import Path
        path = Path(file_path)
        if not path.exists():
            logger.warning(f"文件不存在: {file_path}")
            return
        try:
            with path.open("rb") as f:
                form = aiohttp.FormData()
                form.add_field("chat_id", chat_id)
                form.add_field("document", f, filename=path.name)
                async with self._session.post(f"{self._api}/sendDocument", data=form) as resp:
                    result = await resp.json()
                    if not result.get("ok"):
                        logger.warning(f"sendDocument 失败: {result}")
                    else:
                        logger.debug(f"📤 Telegram 文件已发送到 {chat_id}: {path.name}")
        except Exception as e:
            logger.error(f"sendDocument 异常: {e}")

    async def send_messages(
        self,
        user_id: str,
        messages: list[str],
        group_id: str | None = None,
    ) -> None:
        """发送文本消息（主动意识等外部调用入口）"""
        if group_id:
            chat_id = group_id
        else:
            platform_ids = self.identity.get_platform_ids(user_id)
            tg_ids = [
                pid.removeprefix("telegram:")
                for pid in platform_ids
                if pid.startswith("telegram:")
            ]
            if not tg_ids:
                logger.warning(f"无法找到 {user_id} 的 Telegram ID，跳过发送")
                return
            chat_id = tg_ids[0]

        for i, text in enumerate(messages):
            if i > 0:
                delay = random.uniform(0.5, 1.5) + len(text) * 0.05
                delay = min(delay, 4.0)
                await asyncio.sleep(delay)
            await self._send_text(chat_id, text)

    # ==================== 平台专属能力 ====================

    def get_system_prompt(self, phase: str = "chat") -> str:
        return "你当前通过 Telegram 与用户聊天。"
