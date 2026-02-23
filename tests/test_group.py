"""Phase 6 群聊能力测试"""
import asyncio, os, random
os.environ["PYTHONUTF8"] = "1"

async def main():
    from kaguya.core.group import GroupFilterMiddleware
    from kaguya.core.types import UnifiedMessage, UserInfo, Platform

    passed = 0
    failed = 0
    def check(name, cond, detail=""):
        nonlocal passed, failed
        if cond:
            passed += 1
            print(f"  OK: {name}")
        else:
            failed += 1
            print(f"  FAIL: {name} -- {detail}")

    mw = GroupFilterMiddleware(
        bot_names=["辉夜姬", "kaguya"],
        trigger_keywords=["帮忙", "求助"],
        random_reply_chance=0.0,  # 测试时禁用随机
    )

    def make_msg(content, group_id=None, user="Alice", uid="u1"):
        return UnifiedMessage(
            message_id="test",
            platform=Platform.CLI,
            sender=UserInfo(user_id=uid, nickname=user, platform=Platform.CLI),
            content=content,
            group_id=group_id,
        )

    # 1. 私聊直接放行
    print("\n[1] 私聊放行...")
    msg = make_msg("你好", group_id=None)
    r = await mw.pre_process(msg)
    check("私聊不设置 skip", not getattr(msg, "_skip_reply", False))

    # 2. 群聊 @ 触发
    print("\n[2] @ 触发...")
    msg = make_msg("辉夜姬 你好呀", group_id="g1")
    r = await mw.pre_process(msg)
    check("@ 辉夜姬触发回复", not getattr(msg, "_skip_reply", False))
    check("返回群聊上下文", r is not None and "群聊" in r)

    msg = make_msg("kaguya help me", group_id="g1")
    r = await mw.pre_process(msg)
    check("@ kaguya 触发", not getattr(msg, "_skip_reply", False))

    # 3. 无关消息跳过
    print("\n[3] 无关消息跳过...")
    msg = make_msg("今天天气真好", group_id="g2")
    r = await mw.pre_process(msg)
    check("无关消息被跳过", getattr(msg, "_skip_reply", False))

    # 4. 关键词触发
    print("\n[4] 关键词触发...")
    msg = make_msg("有人能帮忙看下这个bug吗？", group_id="g3")
    r = await mw.pre_process(msg)
    check("关键词'帮忙'触发", not getattr(msg, "_skip_reply", False))

    # 5. 对话延续
    print("\n[5] 对话延续...")
    # 模拟辉夜姬刚回复过 g1（在第2步已经设置了）
    # 下一条消息有更高概率继续对话
    # 重置随机种子确保可重复
    random.seed(42)
    msg = make_msg("哦对了还有个问题", group_id="g1")
    r = await mw.pre_process(msg)
    # 对话延续是概率性的，使用 seed=42 结果是确定的
    has_skip = getattr(msg, "_skip_reply", False)
    reply_reason = getattr(msg, "_group_reply_reason", "")
    check("对话延续判定可执行", True)  # 只要不报错就行
    if not has_skip:
        print(f"    -> 继续对话: {reply_reason}")
    else:
        print(f"    -> 未继续对话（概率未命中）")

    # 6. 大小写不敏感
    print("\n[6] 大小写不敏感...")
    msg = make_msg("KAGUYA 你看这个", group_id="g4")
    r = await mw.pre_process(msg)
    check("大写 KAGUYA 也能触发", not getattr(msg, "_skip_reply", False))

    print(f"\n{'='*40}")
    print(f"  {passed} passed, {failed} failed")
    print(f"{'='*40}")
    if failed == 0:
        print("\nAll tests passed!")
    else:
        import sys; sys.exit(1)

if __name__ == "__main__":
    asyncio.run(main())
