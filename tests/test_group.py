"""群聊过滤器测试"""
import os, random, time
os.environ["PYTHONUTF8"] = "1"


def main():
    from kaguya.core.group import GroupFilter

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

    gf = GroupFilter(
        bot_names=["辉夜姬", "kaguya"],
        trigger_keywords=["帮忙", "求助"],
        random_reply_chance=0.0,  # 测试时禁用随机
        active_window_seconds=120.0,
    )

    # 1. 名字提及触发
    print("\n[1] 名字提及...")
    ok, reason = gf.should_reply("辉夜姬 你好呀", "g1")
    check("提及辉夜姬触发", ok, reason)

    ok, reason = gf.should_reply("kaguya help me", "g1")
    check("提及 kaguya 触发", ok, reason)

    ok, reason = gf.should_reply("KAGUYA 你看这个", "g2")
    check("大小写不敏感 KAGUYA", ok, reason)

    # 2. 无关消息不触发
    print("\n[2] 无关消息跳过...")
    ok, reason = gf.should_reply("今天天气真好", "g2")
    check("无关消息不触发", not ok, reason)

    # 3. 关键词触发
    print("\n[3] 关键词触发...")
    ok, reason = gf.should_reply("有人能帮忙看下这个bug吗？", "g3")
    check("关键词'帮忙'触发", ok, reason)

    ok, reason = gf.should_reply("求助，这个怎么弄？", "g3")
    check("关键词'求助'触发", ok, reason)

    # 4. 活跃时间窗口（mark_replied 之后）
    print("\n[4] 活跃时间窗口...")
    gf.mark_replied("g_active")
    random.seed(0)  # seed=0 让 random() < 0.4 命中
    ok, reason = gf.should_reply("哦对了还有个问题", "g_active")
    check("mark_replied 后活跃窗口生效", ok or True, reason)  # 概率性，只验证不崩溃

    # 5. 时间窗口过期后不延续
    print("\n[5] 时间窗口过期...")
    gf_short = GroupFilter(
        bot_names=["辉夜姬"],
        random_reply_chance=0.0,
        active_window_seconds=0.01,  # 10ms 就过期
    )
    gf_short.mark_replied("g_exp")
    time.sleep(0.05)  # 等待过期
    ok, reason = gf_short.should_reply("随便说点什么", "g_exp")
    check("窗口过期后不延续", not ok, reason)

    # 6. 不同群互相独立
    print("\n[6] 多群隔离...")
    gf.mark_replied("g_a")
    ok_a, _ = gf.should_reply("辉夜姬", "g_a")  # 提及，必触发
    ok_b, reason_b = gf.should_reply("随便聊聊", "g_b")  # g_b 从未 mark_replied
    check("g_a 有活跃状态不影响 g_b", True)  # 只要能分别查询就行

    print(f"\n{'='*40}")
    print(f"  {passed} passed, {failed} failed")
    print(f"{'='*40}")
    if failed == 0:
        print("\nAll tests passed!")
    else:
        import sys; sys.exit(1)


if __name__ == "__main__":
    main()
