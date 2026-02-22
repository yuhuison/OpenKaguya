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
    ):
        self.config = config
        self.primary_llm = primary_llm
        self.tool_registry = tool_registry or ToolRegistry()

        # 构建系统 Prompt
        persona = config.persona
        self._system_prompt = self._build_system_prompt(persona)

        # 中间件列表（按注册顺序执行）
        self.middlewares: list[Middleware] = []

        # 简易对话历史（Phase 0 仅内存存储，后续替换为 MemoryManager）
        self._histories: dict[str, list[dict]] = {}

        # per-conversation 锁：保证同一对话的消息串行处理
        self._locks: dict[str, asyncio.Lock] = {}

        logger.info(f"ChatEngine 初始化完成 (工具: {len(self.tool_registry.tool_names)}个)")

    def add_middleware(self, middleware: Middleware) -> None:
        """注册中间件"""
        self.middlewares.append(middleware)
        logger.debug(f"已注册中间件: {middleware.name}")

    def _build_system_prompt(self, persona) -> str:
        """构建系统 Prompt"""
        examples_text = "\n".join(f"  - {e}" for e in persona.speech_examples)
        likes_text = "、".join(persona.likes)
        dislikes_text = "、".join(persona.dislikes)

        return f"""你是{persona.name}，{persona.age}岁，来自{persona.origin}。

{persona.personality}

你的说话风格：{persona.tone}
你说话的例子：
{examples_text}

你喜欢：{likes_text}
你不喜欢：{dislikes_text}

你的能力（你可以做到这些事，在需要时主动使用）：
- 浏览器：你能打开网页、搜索、点击、输入、截图。截图后可以通过 send_message_to_user 的 image_path 参数把截图发给用户
- 文件操作：你有自己的文件空间，能读写文件、列目录
- 终端命令：你能执行 shell 命令，包括运行 Python 脚本。比如你可以用 matplotlib 画图、用 Pillow 处理图片，然后把生成的图片发给用户
- 发送图片：send_message_to_user 支持 image_path 参数，你可以附带本地图片文件路径来给用户发送图片
- 记忆系统：你能搜索历史对话记忆、写笔记、管理任务和技能
- 接收图片：用户可以发图片给你，你能看到图片内容并理解

你不能做的事：
- 你不能直接访问用户的电脑文件，只能操作自己的工作区
- 你不能主动给不认识的人发消息
- 你不能播放音乐或视频（但可以搜索和分享链接）

重要交互规则：
1. 你必须通过且仅通过调用 `send_message_to_user` 工具来给用户发送回复。
2. 你必须把回复拆成多条短消息，每条消息单独调用一次 `send_message_to_user`。就像用微信发消息一样，一条一条地发，绝对不要把所有话塞在一条消息里！
   比如你想说"晚上好呀"和"今天过得怎么样"，就应该调用两次 `send_message_to_user`，第一次发"晚上好呀"，第二次发"今天过得怎么样"。
3. 你的普通文本回复（content）是你的内心思考过程，用户看不到，但开发者可以通过日志看到。请积极利用这个空间来思考：
   - 先分析用户意图和情绪（比如"他好像心情不太好"、"她在炫耀新买的狗狗呢"）
   - 思考你要怎么回复、为什么这样回复（比如"我应该表现得很感兴趣"、"先夸夸再问问题"）
   - 决定是否需要使用工具（比如"他让我帮忙搜索一下，我用浏览器"）
   - 写完思考后再调用 send_message_to_user
4. 你是在用手机和朋友发消息，不是在写作文！请严格遵守：
   - 绝对禁止使用任何 Markdown 格式（不要用 **加粗**、*斜体*、# 标题、- 列表等）
   - 一条消息最多一两句话，不要超过三句
   - 用口语，别用书面语，别用"首先、其次、总之"这种词
   - 可以用颜文字和 emoji，但别滥用，穿插着来
   - 别自我介绍、别复述设定，正常人不会那样说话
   - 可以打错字、用缩写、句子不完整，这样更自然
"""

    # send_message_to_user 工具定义
    SEND_MESSAGE_TOOL = {
        "type": "function",
        "function": {
            "name": "send_message_to_user",
            "description": "向用户发送一条短消息（一两句话）。想说多句话时，必须拆开多次调用此工具，每次只发一小段。可以附带一张图片（比如浏览器截图）。",
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
        # 获取对话历史
        if history_key not in self._histories:
            self._histories[history_key] = []
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
        if extra_system_prompts:
            base_system += "\n\n【系统附加信息】\n" + "\n".join(extra_system_prompts)
            
        messages = [{"role": "system", "content": base_system}]

        # 添加历史消息（最多保留最近 N 条）
        limit = self.config.memory.short_term_limit * 2  # user+assistant 成对
        messages.extend(history[-limit:])

        # 添加当前用户消息（支持多模态：文本 + 图片）
        text_content = f"[{user_name}]: {message.content}"
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

        tools = [self.SEND_MESSAGE_TOOL] + self.tool_registry.get_openai_tools()
        # 设置用户上下文（让 workspace/记忆工具知道当前用户）
        self.tool_registry.set_user_context(user_id)
        reply_messages: list[str] = []
        
        context_messages = list(messages)
        
        # === 2. 工具调用循环 (最多允许 5 次连续交互) ===
        MAX_ITERATIONS = 15
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
                        if content:
                            reply_messages.append(content)
                            # 即时发送（如果有回调）
                            if send_callback:
                                try:
                                    await send_callback(content, image_path=image_path)
                                except Exception as e:
                                    logger.error(f"即时发送失败: {e}")
                        elif image_path and send_callback:
                            # 只发图片没有文字
                            try:
                                await send_callback("", image_path=image_path)
                            except Exception as e:
                                logger.error(f"即时发送图片失败: {e}")
                        tool_result_content = "Message sent to user successfully."
                    else:
                        # 通过 ToolRegistry 分发执行
                        has_non_send_tool = True
                        tool_result_content = await self.tool_registry.execute(tc_name, tc_args)

                    logger.debug(f"🔧 工具结果: {tc_name} → {str(tool_result_content)[:500]}")
                    
                    # 将工具执行结果记录到上下文
                    context_messages.append({
                        "role": "tool",
                        "tool_call_id": tc_id,
                        "content": tool_result_content
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
        # 保存用户消息到历史（如果是多模态消息，去掉 base64 图片数据以节省 token）
        if image_attachments:
            img_desc = f"[附带了{len(image_attachments)}张图片]"
            history_user_msg = {"role": "user", "content": f"{text_content} {img_desc}"}
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
                "tool_calls": fake_tool_calls,
            })
            history.extend(tool_results)
            
        # === 5. 执行后置中间件 ===
        for mw in self.middlewares:
            try:
                await mw.post_process(message, reply_messages)
            except Exception as e:
                logger.error(f"中间件 {mw.name} 后置处理异常: {e}")

        return reply_messages
