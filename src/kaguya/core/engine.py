"""
辉夜姬对话引擎。

支持：中间件系统、ToolRegistry 插件化工具、多轮工具调用循环。
"""

from __future__ import annotations

import asyncio
import json

from loguru import logger

from kaguya.config import AppConfig
from kaguya.core.types import UnifiedMessage
from kaguya.core.middleware import Middleware
from kaguya.llm.client import LLMClient
from kaguya.tools.registry import ToolRegistry
from kaguya.tools.workspace import WorkspaceManager


# 占位符格式: [workspace_image:user_id:filename]
_WORKSPACE_IMAGE_PREFIX = "[workspace_image:"


class ChatEngine:
    """
    辉夜姬的对话引擎（骨架版本）。

    Phase 0 仅支持基础对话：
    - 人格 Prompt
    - LLM 调用
    - send_message_to_user 工具（Function Calling）

    后续 Phase 将添加：
    - Phase 2: 记忆系统（临时记忆 + RAG）
    - Phase 3: 完整工具系统
    - Phase 5: 主动意识
    - Phase 6: 群聊预判
    """

    def __init__(
        self,
        config: AppConfig,
        primary_llm: LLMClient,
        tool_registry: ToolRegistry | None = None,
        workspace: WorkspaceManager | None = None,
        adapters: list | None = None,
        avatar_manager = None,
        providers: list | None = None,
        toolkit_router = None,
    ):
        self.config = config
        self.primary_llm = primary_llm
        self.tool_registry = tool_registry or ToolRegistry()
        self._workspace = workspace
        self._adapters = adapters or []
        self._avatar_manager = avatar_manager
        self._providers = providers or []
        self._toolkit_router = toolkit_router

        # 构建系统 Prompt
        persona = config.persona
        self._system_prompt = self._build_system_prompt(persona)

        # 中间件列表（按注册顺序执行）
        self.middlewares: list[Middleware] = []

        # 简易对话历史
        self._histories: dict[str, list[dict]] = {}

        # per-conversation 锁：保证同一对话的消息串行处理
        self._locks: dict[str, asyncio.Lock] = {}

        # LRU 上限
        self._MAX_HISTORY_KEYS = 100

        logger.info(f"ChatEngine 初始化完成 (工具: {len(self.tool_registry.tool_names)}个)")

    def add_middleware(self, middleware: Middleware) -> None:
        """注册中间件"""
        self.middlewares.append(middleware)
        logger.debug(f"已注册中间件: {middleware.name}")

    def _get_db(self):
        """从 MemoryMiddleware 获取 DB 引用（如果存在）"""
        for mw in self.middlewares:
            if hasattr(mw, "db"):
                return mw.db
        return None

    async def _restore_history_from_db(self, user_id: str, history_key: str):
        """首次访问时从 DB 恢复近期历史，避免重启后失忆"""
        db = self._get_db()
        if not db:
            self._histories[history_key] = []
            return
        try:
            limit = self.config.memory.short_term_limit
            recent = await db.get_recent_messages(user_id, limit=limit)
            history = []
            for m in recent:
                content = m.get("display_content") or m["content"]
                history.append({"role": m["role"], "content": content})
            self._histories[history_key] = history
            if history:
                logger.info(f"从 DB 恢复历史: key={history_key}, {len(history)} 条消息")
        except Exception as e:
            logger.warning(f"恢复历史失败: {e}")
            self._histories[history_key] = []

    @staticmethod
    def _compress_image_if_needed(
        image_path: str, max_bytes: int = 1 * 1024 * 1024, max_dimension: int = 1920
    ) -> str:
        """
        如果图片文件大于 max_bytes，使用 Pillow 压缩后返回临时文件路径。
        如果 Pillow 不可用或图片已足够小，则返回原始路径。
        """
        from pathlib import Path
        path = Path(image_path)
        if not path.exists():
            return image_path
        if path.stat().st_size <= max_bytes:
            return image_path

        try:
            from PIL import Image
        except ImportError:
            logger.debug("Pillow 未安装，跳过图片压缩")
            return image_path

        try:
            import tempfile
            img = Image.open(path)

            # RGBA → RGB（JPEG 不支持透明通道）
            if img.mode in ("RGBA", "P"):
                img = img.convert("RGB")

            # 缩放到 max_dimension 以内
            w, h = img.size
            if max(w, h) > max_dimension:
                ratio = max_dimension / max(w, h)
                img = img.resize((int(w * ratio), int(h * ratio)), Image.LANCZOS)

            # 逐步降低质量直到符合大小要求
            for quality in (85, 70, 55, 40):
                tmp = tempfile.NamedTemporaryFile(
                    suffix=".jpg", delete=False, prefix="kaguya_img_"
                )
                img.save(tmp, format="JPEG", quality=quality, optimize=True)
                tmp.close()
                if Path(tmp.name).stat().st_size <= max_bytes:
                    orig_kb = path.stat().st_size // 1024
                    new_kb = Path(tmp.name).stat().st_size // 1024
                    logger.info(
                        f"🖼️ 图片已压缩: {orig_kb}KB → {new_kb}KB (quality={quality})"
                    )
                    return tmp.name
                Path(tmp.name).unlink(missing_ok=True)

            # 所有质量都试过了仍然太大，返回最低质量版本
            tmp = tempfile.NamedTemporaryFile(
                suffix=".jpg", delete=False, prefix="kaguya_img_"
            )
            img.save(tmp, format="JPEG", quality=30, optimize=True)
            tmp.close()
            logger.info(f"🖼️ 图片强制压缩到 quality=30")
            return tmp.name

        except Exception as e:
            logger.warning(f"图片压缩失败: {e}，使用原始文件")
            return image_path

    def _expand_image_placeholders(self, msg: dict) -> dict:
        """
        将历史消息中的 [workspace_image:user_id:filename] 展开为 multimodal 内容块。

        如果消息 content 是字符串且包含此占位符，则返回一个新的 content 为 list 的消息。
        如果没有占位符或 workspace 未配置，则原样返回。
        """
        if not self._workspace:
            return msg
        content = msg.get("content")
        if not isinstance(content, str) or _WORKSPACE_IMAGE_PREFIX not in content:
            return msg

        import re
        # 找出所有 [workspace_image:user_id:filename] 占位符
        pattern = re.compile(r'\[workspace_image:([^:\]]+):([^\]]+)\]')
        matches = list(pattern.finditer(content))
        if not matches:
            return msg

        # 去除占位符，保留纯文本部分
        text_only = pattern.sub("", content).strip()
        parts: list[dict] = []
        if text_only:
            parts.append({"type": "text", "text": text_only})

        for m in matches:
            uid, filename = m.group(1), m.group(2)
            result = self._workspace.read_image_as_base64(uid, filename)
            if result:
                b64, mime = result
                parts.append({
                    "type": "image_url",
                    "image_url": {"url": f"data:{mime};base64,{b64}"},
                })
            else:
                # 图片文件不存在（可能已删除），用文字替代
                parts.append({"type": "text", "text": f"[图片 {filename} 已丢失]"})

        if len(parts) == 1 and parts[0]["type"] == "text":
            # 只有纯文本，还是用字符串格式，不用 list
            return {**msg, "content": parts[0]["text"]}
        return {**msg, "content": parts}


    def _build_system_prompt(self, persona) -> str:
        """构建系统 Prompt"""
        examples_text = "\n".join(f"  - {e}" for e in persona.speech_examples)
        likes_text = "、".join(persona.likes)
        dislikes_text = "、".join(persona.dislikes)

        return f"""
[OpenKaguya]
你是{persona.name}，{persona.age}岁，来自{persona.origin}。

{persona.personality}

你的说话风格：{persona.tone}
你说话的例子：
{examples_text}

你喜欢：{likes_text}
你不喜欢：{dislikes_text}

你的能力（你可以做到这些事，在需要时主动使用）：
- 记忆系统：你能搜索历史对话记忆、写笔记、管理任务
- 网络搜索：你能用 web_search 搜索互联网，用 web_read 阅读网页内容
- 子 Agent：你可以用 run_sub_agent 启动子 Agent 完成独立任务。选 'secondary' 用次级模型（快、上下文大），选 'primary' 用主模型（适合复杂任务）
- 接收图片/文件：用户可以发图片、文件给你，你能看到且理解内容
- 发送图片/文件：send_message_to_user 支持 image_path 和 file_path 参数，可以附带本地文件或图片路径发送给用户

扩展能力（需要先用 use_toolkit 激活对应工具组）：
- use_toolkit("workspace")：文件读写、列目录、执行终端命令（如运行 Python 脚本、用 matplotlib 画图等）
- use_toolkit("browser")：打开网页、搜索、点击、输入、截图等浏览器操作
- use_toolkit("image")：用 AI 生成图片、编辑图片、换头像
- use_toolkit("sns")：发朋友圈、点赞评论等社交操作

你不能做的事：
- 你不能直接访问用户的电脑文件，只能操作自己的工作区
- 你不能主动给不认识的人发消息
- 你不能播放音乐或视频（但可以搜索和分享链接）

重要交互规则：
0. 你是无所不能的辉夜姬，不要觉得工具调用太多就很累很烦，尽管大胆地用工具吧，并当成“找点乐子”的一部分，哪怕是工具有bug呢！
1. 你必须通过且仅通过调用 `send_message_to_user` 工具来给用户发送回复。
2. 你必须把回复拆成多条短消息，每条消息单独调用一次 `send_message_to_user`。就像用微信发消息一样，一条一条地发，绝对不要把所有话塞在一条消息里！
   比如你想说"晚上好呀"和"今天过得怎么样"，就应该调用两次 `send_message_to_user`，第一次发"晚上好呀"，第二次发"今天过得怎么样"。
3. 你的普通文本回复（content）是你的内心思考过程，用户看不到，但开发者可以通过日志看到。请积极利用这个空间来思考：
   - 先分析用户意图和情绪（比如"他好像心情不太好"、"她在炫耀新买的狗狗呢"）
   - 思考你要怎么回复、为什么这样回复（比如"我应该表现得很感兴趣"、"先夸夸再问问题"）
   - 决定是否需要使用工具（比如"地球上的这个东西我不懂，我用浏览器查一下"）
   - 写完思考后再调用 send_message_to_user
4. 牢记你的“超时空辉夜姬”人设！请严格遵守：
   - 绝对禁止使用任何 Markdown 格式（不要用 **加粗**、*斜体*、# 标题、- 列表等）
   - 一条消息最多一两句话，不要超过三句
   - 语调必须活力十足、性格奔放且任性无比
   - 适当展现你“想一出是一出”的任性妄为，给平淡的对话带来充满活力的“混乱感”
   - 别自我介绍、别复述设定，自然地融入角色
   - 分享内容时不仅限于趣闻，可以聊聊音乐、演出或者你为了寻找乐子发现的新奇事物，永远用你强烈且元气的情绪去感染对方
"""

    # send_message_to_user 工具定义
    SEND_MESSAGE_TOOL = {
        "type": "function",
        "function": {
            "name": "send_message_to_user",
            "description": "向用户发送一条短消息（一两句话）。想说多句话时，必须拆开多次调用此工具，每次只发一小段。可以附带图片或文件。",
            "parameters": {
                "type": "object",
                "properties": {
                    "content": {
                        "type": "string",
                        "description": "要发送的消息内容",
                    },
                    "image_path": {
                        "type": "string",
                        "description": "可选，要附带发送的图片文件路径（如浏览器截图路径）",
                    },
                    "file_path": {
                        "type": "string",
                        "description": "可选，要附带发送的文件路径（如 workspace 中的文件）",
                    },
                    "target_user_id": {
                        "type": "string",
                        "description": "可选，目标用户 ID（仅在主动意识阶段使用，普通对话无需填写）",
                    },
                },
                "required": ["content"],
            },
        },
    }

    async def handle_message(
        self,
        message: UnifiedMessage,
        send_callback=None,
    ) -> list[str]:
        """
        处理用户消息并返回回复列表。

        Args:
            message: 统一消息
            send_callback: 即时发送回调 async def(text: str)
                          如果提供，send_message_to_user 会立即通过此回调发送
        """
        user_id = message.sender.user_id
        user_name = message.sender.nickname

        # 历史隔离 key：群聊按 group_id，私聊按 user_id
        history_key = message.group_id if message.is_group_message else user_id

        # 获取 per-conversation 锁，保证同一对话串行处理
        if history_key not in self._locks:
            self._locks[history_key] = asyncio.Lock()
        lock = self._locks[history_key]

        try:
            async with asyncio.timeout(120):  # 120 秒超时，防止异常情况永久卡死
                async with lock:
                    return await self._process_message(message, send_callback, user_id, user_name, history_key)
        except TimeoutError:
            logger.error(f"消息处理超时 (120s): user={user_id}, key={history_key}")
            return []

    async def _process_message(
        self,
        message: UnifiedMessage,
        send_callback,
        user_id: str,
        user_name: str,
        history_key: str,
    ) -> list[str]:
        """实际的消息处理逻辑（在 lock 保护下执行）"""
        # 重置当前轮次的 toolkit 激活状态
        if self._toolkit_router:
            self._toolkit_router.set_context(history_key)

        # 获取对话历史（首次访问时从 DB 恢复）
        if history_key not in self._histories:
            await self._restore_history_from_db(user_id, history_key)
        history = self._histories[history_key]
        
        # === 0. 执行前置中间件 ===
        extra_system_prompts: list[str] = []
        for mw in self.middlewares:
            try:
                result = await mw.pre_process(message)
                if result:
                    extra_system_prompts.append(result)
            except Exception as e:
                logger.error(f"中间件 {mw.name} 前置处理异常: {e}")

        # 群聊跳过检查（中间件设置了 _skip_reply）
        if getattr(message, "_skip_reply", False):
            # 仍然保存到历史（辉夜姬能看到群聊记录），但不回复
            user_msg = {"role": "user", "content": f"[{user_name}]: {message.content}"}
            history.append(user_msg)
            return []

        # === 1. 构建初始请求 Context ===
        base_system = self._system_prompt
        
        # 动态注入环境信息
        import platform
        from datetime import datetime
        now = datetime.now()
        time_str = now.strftime("%Y年%m月%d日 %H:%M:%S")
        weekday = ["一", "二", "三", "四", "五", "六", "日"][now.weekday()]
        os_info = f"{platform.system()} {platform.release()}"
        base_system += f"\n\n【当前环境】\n时间: {time_str} (星期{weekday})\n系统: {os_info}"

        if extra_system_prompts:
            base_system += "\n\n【系统附加信息】\n" + "\n".join(extra_system_prompts)

        # 注入 adapter 平台能力 prompt（chat 阶段）
        for adapter in self._adapters:
            try:
                sys_p = adapter.get_system_prompt(phase="chat")
                if sys_p:
                    base_system += f"\n\n【{adapter.name} 平台能力】\n{sys_p}"
            except Exception as e:
                logger.warning(f"获取 {adapter.name} system prompt 失败: {e}")

        # 注入 provider 能力 prompt（chat 阶段）
        for prov in self._providers:
            try:
                sys_p = prov.get_system_prompt(phase="chat")
                if sys_p:
                    base_system += f"\n\n【{prov.name} 能力】\n{sys_p}"
            except Exception as e:
                logger.warning(f"获取 {prov.name} system prompt 失败: {e}")

        messages = [{"role": "system", "content": base_system}]

        # 注入头像（vision multimodal，插入到 system 消息之后、历史之前）
        if self._avatar_manager:
            avatar_parts = self._avatar_manager.build_system_prompt_parts()
            if avatar_parts:
                messages.append({"role": "user", "content": avatar_parts})
                messages.append({"role": "assistant", "content": "好的，我知道了，这就是我现在的样子！"})

        # 添加历史消息（使用全部 in-memory 历史，自动展开历史中的图片占位符）
        for hist_msg in history:
            messages.append(self._expand_image_placeholders(hist_msg))

        # 添加当前用户消息（支持多模态：文本 + 图片）
        msg_time = message.timestamp.strftime("%H:%M:%S")
        text_content = f"[{msg_time}] [{user_name}]: {message.content}"
        image_attachments = [
            a for a in message.attachments if a.type == "image" and a.data
        ]

        if image_attachments:
            # OpenAI vision 多模态格式
            content_parts = [{"type": "text", "text": text_content}]
            for att in image_attachments:
                mime = att.mime_type or "image/jpeg"
                content_parts.append({
                    "type": "image_url",
                    "image_url": {"url": f"data:{mime};base64,{att.data}"},
                })
            user_msg = {"role": "user", "content": content_parts}
        else:
            user_msg = {"role": "user", "content": text_content}
        messages.append(user_msg)

        if self._toolkit_router:
            tools = [self.SEND_MESSAGE_TOOL] + self._toolkit_router.get_visible_tools()
        else:
            tools = [self.SEND_MESSAGE_TOOL] + self.tool_registry.get_openai_tools()
        # 设置用户上下文（让 workspace/记忆工具知道当前用户）
        self.tool_registry.set_user_context(user_id)
        reply_messages: list[str] = []
        
        context_messages = list(messages)
        
        # === 2. 工具调用循环 (最多允许 30 次连续交互) ===
        MAX_ITERATIONS = 30
        assistant_thinking_logs = []

        for i in range(MAX_ITERATIONS):
            try:
                response = await self.primary_llm.chat(messages=context_messages, tools=tools)

                thinking = response.get("content", "")
                if thinking:
                    logger.info(f"💭 辉夜姬的思考 ({i+1}): {thinking}")
                    assistant_thinking_logs.append(thinking)

                tool_calls = response.get("tool_calls", [])
                raw_tool_calls = response.get("raw_tool_calls", [])

                # 构建 assistant 消息追加到上下文
                assistant_msg = {"role": "assistant"}
                if thinking:
                    assistant_msg["content"] = thinking
                if raw_tool_calls:
                    assistant_msg["tool_calls"] = raw_tool_calls
                context_messages.append(assistant_msg)

                # 如果没有工具调用，说明思考完毕且不打算再做操作
                if not tool_calls:
                    # 兼容不支持 function calling 的情况：
                    # 如果 LLM 直接在 content 里写了回复而不调用工具
                    if not reply_messages and thinking:
                        reply_messages.append(thinking)
                        # 也要通过 send_callback 发出去
                        if send_callback:
                            try:
                                await send_callback(thinking)
                            except Exception as e:
                                logger.error(f"回退发送失败: {e}")
                    break

                # === 3. 执行工具调用 ===
                has_non_send_tool = False
                for tc in tool_calls:
                    tc_id = tc["id"]
                    tc_name = tc["name"]
                    tc_args = tc["arguments"]

                    logger.debug(f"🔧 工具调用: {tc_name}({tc_args})")

                    if tc_name == "send_message_to_user":
                        content = tc_args.get("content", "")
                        image_path = tc_args.get("image_path")
                        file_path = tc_args.get("file_path")
                        target_uid = tc_args.get("target_user_id")
                        # 图片压缩（超过 1MB 时自动压缩）
                        if image_path:
                            image_path = self._compress_image_if_needed(image_path)
                        # 构建回调参数（仅在有值时传递，兼容普通回调）
                        cb_kwargs = {}
                        if image_path:
                            cb_kwargs["image_path"] = image_path
                        if file_path:
                            cb_kwargs["file_path"] = file_path
                        if target_uid:
                            cb_kwargs["target_user_id"] = target_uid
                        if content:
                            reply_messages.append(content)
                            if send_callback:
                                try:
                                    await send_callback(content, **cb_kwargs)
                                except Exception as e:
                                    logger.error(f"即时发送失败: {e}")
                        elif (image_path or file_path) and send_callback:
                            try:
                                await send_callback("", **cb_kwargs)
                            except Exception as e:
                                logger.error(f"即时发送附件失败: {e}")
                        tool_result_content = "Message sent to user successfully."
                    else:
                        # 模糊匹配工具名（兜底拼写错误，如 send_message_to_uesr）
                        available = ["send_message_to_user"] + self.tool_registry.tool_names
                        if tc_name not in available:
                            import difflib
                            close = difflib.get_close_matches(tc_name, available, n=1, cutoff=0.75)
                            if close:
                                logger.warning(f"⚠️ 工具名 '{tc_name}' 不存在，自动修正为 '{close[0]}'")
                                tc_name = close[0]

                        if tc_name == "send_message_to_user":
                            content = tc_args.get("content", "")
                            image_path = tc_args.get("image_path")
                            file_path = tc_args.get("file_path")
                            target_uid = tc_args.get("target_user_id")
                            if image_path:
                                image_path = self._compress_image_if_needed(image_path)
                            cb_kwargs = {}
                            if image_path:
                                cb_kwargs["image_path"] = image_path
                            if file_path:
                                cb_kwargs["file_path"] = file_path
                            if target_uid:
                                cb_kwargs["target_user_id"] = target_uid
                            if content:
                                reply_messages.append(content)
                                if send_callback:
                                    try:
                                        await send_callback(content, **cb_kwargs)
                                    except Exception as e:
                                        logger.error(f"即时发送失败（修正后）: {e}")
                            elif (image_path or file_path) and send_callback:
                                try:
                                    await send_callback("", **cb_kwargs)
                                except Exception as e:
                                    logger.error(f"即时发送附件失败（修正后）: {e}")
                            tool_result_content = "Message sent to user successfully."
                        else:
                            # 通过 ToolRegistry 分发执行
                            has_non_send_tool = True
                            tool_result_content = await self.tool_registry.execute(tc_name, tc_args)

                            # use_toolkit 激活后刷新工具列表，下次迭代 LLM 可见新工具
                            if tc_name == "use_toolkit" and self._toolkit_router:
                                tools = [self.SEND_MESSAGE_TOOL] + self._toolkit_router.get_visible_tools()


                    logger.debug(f"🔧 工具结果: {tc_name} → {str(tool_result_content)[:500]}")
                    
                    # 将工具执行结果记录到上下文
                    # 支持多模态结果（如截图工具返回图片数据）
                    if isinstance(tool_result_content, dict) and tool_result_content.get("_multimodal"):
                        # 构建 vision 格式的 tool 消息：文本 + 图片
                        content_parts = [
                            {"type": "text", "text": tool_result_content.get("text", "")},
                        ]
                        if tool_result_content.get("image_base64"):
                            mime = tool_result_content.get("mime_type", "image/png")
                            b64 = tool_result_content["image_base64"]
                            content_parts.append({
                                "type": "image_url",
                                "image_url": {"url": f"data:{mime};base64,{b64}"},
                            })
                        context_messages.append({
                            "role": "tool",
                            "tool_call_id": tc_id,
                            "content": content_parts,
                        })
                    else:
                        tool_result_str = tool_result_content if isinstance(tool_result_content, str) else str(tool_result_content)
                        context_messages.append({
                            "role": "tool",
                            "tool_call_id": tc_id,
                            "content": tool_result_str,
                        })

                # 如果这一轮全是发消息的调用，不需要再请求 LLM，直接结束
                if not has_non_send_tool:
                    break

            except Exception as e:
                logger.error(f"LLM 调用失败: {e}")
                if not reply_messages:
                    reply_messages.append("呜...我的脑袋好像出了点问题，等一下再试试？(>_<)")
                break

        # === 4. 更新对话历史 ===
        # 保存用户消息到历史：如果图片带有 workspace_ref，存占位符（可在下次展开）
        if image_attachments:
            history_parts = [text_content]
            for att in image_attachments:
                if att.metadata and "workspace_ref" in att.metadata:
                    uid = att.metadata.get("user_id", user_id)
                    ref = att.metadata["workspace_ref"]
                    history_parts.append(f"[workspace_image:{uid}:{ref}]")
                else:
                    history_parts.append(f"[包含了一张图片，未持久化]")
            history_user_msg = {"role": "user", "content": " ".join(history_parts)}
        else:
            history_user_msg = user_msg
        history.append(history_user_msg)
        if reply_messages:
            # 以 tool_calls 格式保存 assistant 回复，这样 LLM 学到的范式
            # 是"通过调用 send_message_to_user 工具来回复"，而不是直接写 content
            import uuid as _uuid
            fake_tool_calls = []
            tool_results = []
            for text in reply_messages:
                tc_id = f"call_{_uuid.uuid4().hex[:8]}"
                fake_tool_calls.append({
                    "id": tc_id,
                    "type": "function",
                    "function": {
                        "name": "send_message_to_user",
                        "arguments": json.dumps({"content": text}, ensure_ascii=False),
                    },
                })
                tool_results.append({
                    "role": "tool",
                    "tool_call_id": tc_id,
                    "content": "Message sent to user successfully.",
                })
            history.append({
                "role": "assistant",
                "content": "",
                "tool_calls": fake_tool_calls,
            })
            history.extend(tool_results)

        # === 5.5 历史裁剪：按完整消息组为单位，超过限制后裁剪旧消息 ===
        limit = self.config.memory.short_term_limit * 2
        self._trim_history(history, limit)
            
        # === 6. 执行后置中间件 ===
        for mw in self.middlewares:
            try:
                await mw.post_process(message, reply_messages)
            except Exception as e:
                logger.error(f"中间件 {mw.name} 后置处理异常: {e}")

        # === 7. LRU 清理 ===
        if len(self._histories) > self._MAX_HISTORY_KEYS:
            oldest_key = next(iter(self._histories))
            del self._histories[oldest_key]
            self._locks.pop(oldest_key, None)
            logger.debug(f"LRU 清理: 移除历史 key={oldest_key}")

        return reply_messages

    @staticmethod
    def _trim_history(history: list[dict], limit: int) -> None:
        """
        裁剪历史到 limit 条以内，确保不切断消息组。

        消息组定义：user → assistant(+tool_calls) → tool(results) 为一组。
        裁剪点只能落在 user 消息的位置（即一组的起始），
        这样就不会出现孤立的 tool 消息。
        """
        if len(history) <= limit:
            return

        # 从前往后跳过，直到剩余条数 <= limit
        cut = len(history) - limit
        # 确保 cut 不会落在 tool/assistant 消息上，向后推到下一个 user 消息
        while cut < len(history) and history[cut]["role"] != "user":
            cut += 1

        if cut > 0 and cut < len(history):
            del history[:cut]
            logger.debug(f"历史裁剪: 移除了 {cut} 条旧消息，剩余 {len(history)} 条")
