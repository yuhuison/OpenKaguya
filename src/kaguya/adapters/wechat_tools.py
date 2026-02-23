"""
微信朋友圈工具集 — 基于 wechat-v864 API。

提供 3 个工具 + 1 个数据获取函数：
- 工具: sns_post, sns_interact, sns_view_detail
- 数据获取: fetch_timeline()
"""

from __future__ import annotations

import base64
import json
from pathlib import Path
from typing import Any, Optional

import aiohttp
from loguru import logger

from kaguya.core.identity import UserIdentityManager
from kaguya.tools.registry import Tool


# ===================== HTTP 请求辅助 =====================


async def _api_post(
    session: aiohttp.ClientSession,
    base_url: str,
    api_key: str,
    path: str,
    payload: dict,
) -> dict:
    """调用 wechat-v864 API 并返回 JSON 响应"""
    url = f"{base_url}{path}?key={api_key}"
    try:
        async with session.post(url, json=payload) as resp:
            return await resp.json()
    except Exception as e:
        logger.error(f"朋友圈 API 调用失败 ({path}): {e}")
        return {"Code": -1, "error": str(e)}


# ===================== 数据获取（注入 prompt，不暴露为工具） =====================


async def fetch_timeline(
    session: aiohttp.ClientSession,
    base_url: str,
    api_key: str,
) -> str:
    """获取朋友圈首页，格式化为可读文本供 prompt 注入"""
    result = await _api_post(session, base_url, api_key, "/sns/SendSnsTimeLine", {
        "FirstPageMD5": "",
        "MaxID": 0,
    })

    if result.get("Code") != 200:
        logger.warning(f"获取朋友圈首页失败: {result}")
        return ""

    # 实际 API 返回: Data.objectList (camelCase)
    data = result.get("Data", result.get("data", {}))
    if not data:
        return "朋友圈暂无新内容。"

    items = []
    if isinstance(data, dict):
        items = data.get("objectList", data.get("ObjectList", []))
    elif isinstance(data, list):
        items = data

    if not items:
        return "朋友圈暂无新内容。"

    lines = ["📱 朋友圈最新动态：\n"]
    for i, item in enumerate(items[:10]):  # 最多 10 条
        if isinstance(item, dict):
            sns_id = item.get("id", item.get("Id", "?"))
            nickname = item.get("nickname", item.get("NickName", "好友"))
            # 实际返回中 content 在 objectDescStr 字段（非 Content）
            content = (
                item.get("objectDescStr", "")
                or item.get("ContentDesc", "")
                or item.get("contentDesc", "")
                or item.get("Content", "")
                or item.get("content", "")
            )
            create_time = item.get("createTime", item.get("CreateTime", ""))
            like_count = item.get("likeCount", item.get("LikeCount", 0))
            comment_count = item.get("commentCount", item.get("CommentCount", 0))
            # 媒体信息在 ContentObject.MediaList.Media 或顶层 mediaList
            content_obj = item.get("ContentObject", item.get("contentObject", {}))
            media_list = content_obj.get("MediaList", content_obj.get("mediaList", {}))
            if isinstance(media_list, dict):
                media_items = media_list.get("Media", media_list.get("media", []))
            elif isinstance(media_list, list):
                media_items = media_list
            else:
                media_items = []
            has_media = bool(media_items)

            lines.append(f"[{i+1}] {nickname}:")
            if content:
                lines.append(f"    {content[:200]}")
            media_tag = " 📷" if has_media else ""
            lines.append(f"    ❤️{like_count} 💬{comment_count}{media_tag}  (ID: {sns_id})")
            lines.append("")

    return "\n".join(lines) if len(lines) > 1 else "朋友圈暂无新内容。"





# ===================== 朋友圈工具 =====================


class SnsPostTool(Tool):
    """发布朋友圈（纯文字或图文）"""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        base_url: str,
        api_key: str,
    ):
        self._session = session
        self._base_url = base_url
        self._api_key = api_key

    @property
    def name(self):
        return "sns_post"

    @property
    def description(self):
        return (
            "发一条朋友圈。支持纯文字和图文。"
            "如果要发图片，传入图片文件路径列表（本地路径），系统会自动上传。"
        )

    @property
    def parameters(self):
        return {
            "type": "object",
            "properties": {
                "content": {
                    "type": "string",
                    "description": "朋友圈文案内容",
                },
                "image_paths": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "可选，图片文件路径列表（本地路径），最多 9 张",
                },
            },
            "required": ["content"],
        }

    async def execute(self, content: str, image_paths: list[str] | None = None, **_) -> str:
        media_list = []

        # 如果有图片，先上传
        if image_paths:
            image_data_list = []
            for path_str in image_paths[:9]:
                path = Path(path_str)
                if not path.exists():
                    return f"图片文件不存在: {path_str}"
                try:
                    img_bytes = path.read_bytes()
                    img_b64 = base64.b64encode(img_bytes).decode("ascii")
                    image_data_list.append(img_b64)
                except Exception as e:
                    return f"读取图片失败 ({path_str}): {e}"

            # 上传图片
            upload_result = await _api_post(
                self._session, self._base_url, self._api_key,
                "/sns/UploadFriendCircleImage",
                {"ImageDataList": image_data_list},
            )

            if upload_result.get("Code") != 200:
                return f"图片上传失败: {upload_result}"

            # 提取上传后的 URL
            uploaded = upload_result.get("Data", upload_result.get("data", []))
            if isinstance(uploaded, dict):
                uploaded = uploaded.get("ImageList", uploaded.get("imageList", []))

            for idx, img_info in enumerate(uploaded):
                if isinstance(img_info, dict):
                    media_list.append({
                        "ID": idx + 1,
                        "Type": 2,
                        "URL": img_info.get("URL", img_info.get("url", "")),
                        "URLType": "1",
                        "Thumb": img_info.get("Thumb", img_info.get("thumb", "")),
                        "ThumType": "1",
                        "MD5": img_info.get("MD5", img_info.get("md5", "")),
                    })

        # 发送朋友圈
        payload: dict[str, Any] = {
            "ContentStyle": 1 if media_list else 2,  # 1=图文, 2=纯文字
            "Privacy": 0,  # 公开
            "Content": content,
        }
        if media_list:
            payload["MediaList"] = media_list

        result = await _api_post(
            self._session, self._base_url, self._api_key,
            "/sns/SendFriendCircle", payload,
        )

        if result.get("Code") != 200:
            return f"朋友圈发布失败: {result}"

        # 即使 Code=200，需要检查 Data.baseResponse.ret 来判断真正是否成功
        resp_data = result.get("Data", {})
        base_resp = resp_data.get("baseResponse", {}) if isinstance(resp_data, dict) else {}
        ret_code = base_resp.get("ret", 0)
        spam_tips = resp_data.get("spamTips", "") if isinstance(resp_data, dict) else ""

        if ret_code != 0:
            return f"朋友圈发布失败（ret={ret_code}）: {spam_tips or result.get('Text', '未知错误')}"

        return f"朋友圈发布成功！{'（含 ' + str(len(media_list)) + ' 张图片）' if media_list else ''}"



class SnsInteractTool(Tool):
    """点赞/评论朋友圈"""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        base_url: str,
        api_key: str,
        identity_manager: UserIdentityManager,
    ):
        self._session = session
        self._base_url = base_url
        self._api_key = api_key
        self._identity = identity_manager

    @property
    def name(self):
        return "sns_interact"

    @property
    def description(self):
        return (
            "对朋友圈进行互动：点赞或评论。"
            "sns_id 从朋友圈列表中获取，to_user 是发布者的用户ID（不是wxid）。"
        )

    @property
    def parameters(self):
        return {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["like", "comment"],
                    "description": "操作类型：like=点赞, comment=评论",
                },
                "sns_id": {
                    "type": "string",
                    "description": "朋友圈 ID（从朋友圈列表中获取）",
                },
                "to_user": {
                    "type": "string",
                    "description": "发布者的用户 ID",
                },
                "content": {
                    "type": "string",
                    "description": "评论内容（action=comment 时必填）",
                },
            },
            "required": ["action", "sns_id", "to_user"],
        }

    async def execute(
        self,
        action: str,
        sns_id: str,
        to_user: str,
        content: str = "",
        **_,
    ) -> str:
        # 将用户 ID 转换为 wxid
        wxid = self._resolve_wxid(to_user)

        op_type = 1 if action == "like" else 2
        comment_item: dict[str, Any] = {
            "OpType": op_type,
            "ItemID": sns_id,
            "ToUserName": wxid,
        }
        if action == "comment":
            if not content:
                return "评论操作需要 content 参数。"
            comment_item["Content"] = content

        result = await _api_post(
            self._session, self._base_url, self._api_key,
            "/sns/SendSnsComment",
            {"SnsCommentList": [comment_item]},
        )

        if result.get("Code") == 200:
            action_text = "点赞" if action == "like" else "评论"
            return f"已{action_text}成功！"
        return f"操作失败: {result}"

    def _resolve_wxid(self, user_id: str) -> str:
        """将统一用户 ID 转换为 wxid"""
        if user_id.startswith("wxid_"):
            return user_id  # 已经是 wxid

        platform_ids = self._identity.get_platform_ids(user_id)
        for pid in platform_ids:
            if pid.startswith("wechat:"):
                return pid.removeprefix("wechat:")

        # 找不到映射就原样返回
        return user_id


class SnsViewImageTool(Tool):
    """查看某条朋友圈的详情和图片"""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        base_url: str,
        api_key: str,
    ):
        self._session = session
        self._base_url = base_url
        self._api_key = api_key

    @property
    def name(self):
        return "sns_view_detail"

    @property
    def description(self):
        return "查看某条朋友圈的完整内容（含图片、评论、点赞详情）。"

    @property
    def parameters(self):
        return {
            "type": "object",
            "properties": {
                "sns_id": {
                    "type": "string",
                    "description": "朋友圈 ID",
                },
            },
            "required": ["sns_id"],
        }

    async def execute(self, sns_id: str, **_) -> str:
        result = await _api_post(
            self._session, self._base_url, self._api_key,
            "/sns/SendSnsObjectDetailById",
            {"Id": sns_id},
        )

        if result.get("Code") != 200:
            return f"获取朋友圈详情失败: {result}"

        data = result.get("Data", result.get("data", {}))
        if not data:
            return "未获取到详情数据。"

        # 格式化输出
        lines = ["📋 朋友圈详情：\n"]

        if isinstance(data, dict):
            nickname = data.get("nickname", data.get("NickName", ""))
            # 内容在 objectDescStr 字段（不是 Content）
            content = (
                data.get("objectDescStr", "")
                or data.get("ContentDesc", "")
                or data.get("contentDesc", "")
                or data.get("Content", "")
                or data.get("content", "")
            )
            create_time = data.get("createTime", data.get("CreateTime", ""))

            if nickname:
                lines.append(f"发布者: {nickname}")
            if content:
                lines.append(f"内容: {content}")
            if create_time:
                lines.append(f"时间: {create_time}")

            # 图片 — 实际在 ContentObject.MediaList.Media 嵌套结构
            content_obj = data.get("ContentObject", data.get("contentObject", {}))
            media_container = content_obj.get("MediaList", content_obj.get("mediaList", {}))
            if isinstance(media_container, dict):
                media = media_container.get("Media", media_container.get("media", []))
            elif isinstance(media_container, list):
                media = media_container
            else:
                media = data.get("MediaList", data.get("mediaList", []))

            if media:
                lines.append(f"\n图片 ({len(media)} 张):")
                for i, m in enumerate(media):
                    url_obj = m.get("URL", m.get("url", {}))
                    if isinstance(url_obj, dict):
                        url = url_obj.get("Value", url_obj.get("value", ""))
                    elif isinstance(url_obj, str):
                        url = url_obj
                    else:
                        url = ""
                    if url:
                        lines.append(f"  [{i+1}] {url}")

            # 评论
            comments = data.get("CommentList", data.get("commentList", []))
            if comments:
                lines.append(f"\n评论 ({len(comments)} 条):")
                for c in comments:
                    cn = c.get("NickName", c.get("nickname", "?"))
                    cc = c.get("Content", c.get("content", ""))
                    ct = c.get("OpType", 0)
                    if ct == 1:
                        lines.append(f"  ❤️ {cn} 点赞")
                    else:
                        lines.append(f"  💬 {cn}: {cc}")

        return "\n".join(lines)


# ===================== 工厂函数 =====================


def create_sns_tools(
    session: aiohttp.ClientSession,
    base_url: str,
    api_key: str,
    identity_manager: UserIdentityManager,
    phase: str = "consciousness",
) -> list[Tool]:
    """
    创建朋友圈工具集。

    phase='consciousness': sns_post + sns_interact + sns_view_detail
    phase='chat': sns_interact only
    """
    interact = SnsInteractTool(session, base_url, api_key, identity_manager)
    view_detail = SnsViewImageTool(session, base_url, api_key)

    if phase == "consciousness":
        post = SnsPostTool(session, base_url, api_key)
        return [post, interact, view_detail]
    else:
        return [interact]
