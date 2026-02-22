"""
辉夜姬对话引擎。

支持：中间件系统、ToolRegistry 插件化工具、多轮工具调用循环。
"""

from __future__ import annotations

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

重要交互规则：
1. 你必须通过且仅通过调用 `send_message_to_user` 工具来给用户发送回复。
2. 你可以在一个回合内连续多次调用 `send_message_to_user`，用来发送多条消息（比如分段发送，避免长篇大论）。
3. 你的普通文本回复（content）只是你的内心思考过程，用户是绝对看不到的！
4. 保持自然、有个性地说和思考，不要像个官方的AI助手。
"""

    # send_message_to_user 工具定义
    SEND_MESSAGE_TOOL = {
        "type": "function",
        "function": {
            "name": "send_message_to_user",
            "description": "向当前对话的用户发送一条消息。可多次调用发送多条消息。",
            "parameters": {
                "type": "object",
                "properties": {
                    "content": {
                        "type": "string",
                        "description": "要发送的消息内容",
                    }
                },
                "required": ["content"],
            },
        },
    }

    async def handle_message(self, message: UnifiedMessage) -> list[str]:
        """
        处理用户消息并返回回复列表。
        群聊消息会检查 _skip_reply 标志（由 GroupFilterMiddleware 设置）。
        """
        user_id = message.sender.user_id
        user_name = message.sender.nickname

        # 历史隔离 key：群聊按 group_id，私聊按 user_id
        history_key = message.group_id if message.is_group_message else user_id

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

        # 添加当前用户消息（群聊标注发言者，私聊也标注）
        user_msg = {"role": "user", "content": f"[{user_name}]: {message.content}"}
        messages.append(user_msg)

        tools = [self.SEND_MESSAGE_TOOL] + self.tool_registry.get_openai_tools()
        # 设置用户上下文（让 workspace/记忆工具知道当前用户）
        self.tool_registry.set_user_context(user_id)
        reply_messages: list[str] = []
        
        context_messages = list(messages)
        
        # === 2. 工具调用循环 (最多允许 5 次连续交互) ===
        MAX_ITERATIONS = 5
        assistant_thinking_logs = []

        for i in range(MAX_ITERATIONS):
            try:
                response = await self.primary_llm.chat(messages=context_messages, tools=tools)

                thinking = response.get("content", "")
                if thinking:
                    logger.debug(f"辉夜姬的思考 ({i+1}): {thinking[:200]}...")
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
                    # 兼容不支持 function calling 的情况
                    if not reply_messages and thinking:
                        reply_messages.append(thinking)
                    break

                # === 3. 执行工具调用 ===
                for tc in tool_calls:
                    tc_id = tc["id"]
                    tc_name = tc["name"]
                    tc_args = tc["arguments"]

                    if tc_name == "send_message_to_user":
                        # 特殊处理：收集回复
                        content = tc_args.get("content", "")
                        if content:
                            reply_messages.append(content)
                        tool_result_content = "Message sent to user successfully."
                    else:
                        # 通过 ToolRegistry 分发执行
                        tool_result_content = await self.tool_registry.execute(tc_name, tc_args)
                    
                    # 将工具执行结果记录到上下文
                    context_messages.append({
                        "role": "tool",
                        "tool_call_id": tc_id,
                        "content": tool_result_content
                    })

            except Exception as e:
                logger.error(f"LLM 调用失败: {e}")
                if not reply_messages:
                    reply_messages.append("呜...我的脑袋好像出了点问题，等一下再试试？(>_<)")
                break

        # === 4. 更新对话历史 ===
        history.append(user_msg)
        if reply_messages:
            history.append({
                "role": "assistant",
                "content": "\n\n".join(assistant_thinking_logs) or ""
            })
            
        # === 5. 执行后置中间件 ===
        for mw in self.middlewares:
            try:
                await mw.post_process(message, reply_messages)
            except Exception as e:
                logger.error(f"中间件 {mw.name} 后置处理异常: {e}")

        return reply_messages
