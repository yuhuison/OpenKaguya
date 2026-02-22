"""微信 Adapter + 跨平台用户身份系统 — 集成测试"""
import asyncio, os, json
os.environ["PYTHONUTF8"] = "1"

async def main():
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

    # ====== 1. UserIdentityManager ======
    print("\n[1] 用户身份管理器...")
    from kaguya.core.identity import UserIdentityManager, UserIdentity

    mgr = UserIdentityManager([
        UserIdentity(
            id="alice", nickname="小爱",
            note="喜欢猫和咖啡", role="admin",
            accounts=["wechat:wxid_alice123", "qq:12345", "cli:local_user"],
        ),
        UserIdentity(
            id="bob", nickname="阿博",
            note="喜欢打游戏", role="friend",
            accounts=["wechat:wxid_bob456"],
        ),
    ])

    # 正向映射
    check("resolve wechat→alice", mgr.resolve("wechat", "wxid_alice123") == "alice")
    check("resolve qq→alice", mgr.resolve("qq", "12345") == "alice")
    check("resolve cli→alice", mgr.resolve("cli", "local_user") == "alice")
    check("resolve wechat→bob", mgr.resolve("wechat", "wxid_bob456") == "bob")

    # 退化映射（未注册的用户）
    check("resolve 未注册→退化", mgr.resolve("wechat", "wxid_unknown") == "wechat:wxid_unknown")

    # 元信息
    alice = mgr.get_identity("alice")
    check("alice nickname", alice.nickname == "小爱")
    check("alice note", alice.note == "喜欢猫和咖啡")
    check("alice role", alice.role == "admin")

    # get_nickname
    check("get_nickname alice", mgr.get_nickname("wechat", "wxid_alice123") == "小爱")
    check("get_nickname unknown+fallback", mgr.get_nickname("wechat", "wxid_xxx", fallback="路人") == "路人")

    # user context
    ctx = mgr.build_user_context("alice")
    check("user context 包含昵称", "小爱" in ctx)
    check("user context 包含备注", "喜欢猫和咖啡" in ctx)
    check("user context 包含管理员", "管理员" in ctx)
    check("user context bob 无管理员", "管理员" not in mgr.build_user_context("bob"))

    # get_platform_ids
    ids = mgr.get_platform_ids("alice")
    check("alice 3 个平台 ID", len(ids) == 3)

    # 未注册用户返回原始 ID
    check("get_platform_ids 未注册", mgr.get_platform_ids("unknown") == ["unknown"])

    # ====== 2. 配置加载 ======
    print("\n[2] 配置加载...")
    from kaguya.config import load_config
    config = load_config()
    check("wechat config 存在", hasattr(config, "wechat"))
    check("wechat enabled 类型正确", isinstance(config.wechat.enabled, bool))
    check("identity config 存在", hasattr(config, "identity"))

    # ====== 3. 微信消息解析 ======
    print("\n[3] 微信消息解析...")
    from kaguya.adapters.wechat import WeChatAdapter
    from kaguya.config import WeChatConfig

    wechat_config = WeChatConfig(
        enabled=True,
        base_url="http://127.0.0.1:8099",
        api_key="test_key",
        whitelist_users=["wxid_alice123", "wxid_bob456"],
        whitelist_groups=["12345@chatroom"],
    )
    adapter = WeChatAdapter(config=wechat_config, identity_manager=mgr)

    # _extract_str
    check("extract_str dict", adapter._extract_str({"str": "hello"}) == "hello")
    check("extract_str plain", adapter._extract_str("hello") == "hello")
    check("extract_str empty", adapter._extract_str({}) == "")

    # _extract_nickname
    check("extract nickname 英文冒号", adapter._extract_nickname("小爱: 你好") == "小爱")
    check("extract nickname 中文冒号", adapter._extract_nickname("小爱：你好") == "小爱")
    check("extract nickname 无冒号", adapter._extract_nickname("你好") == "")

    # ====== 4. 白名单过滤 ======
    print("\n[4] 白名单过滤...")
    check("用户在白名单", "wxid_alice123" in adapter._whitelist_users)
    check("用户不在白名单", "wxid_unknown" not in adapter._whitelist_users)
    check("群在白名单", "12345@chatroom" in adapter._whitelist_groups)
    check("群不在白名单", "99999@chatroom" not in adapter._whitelist_groups)

    # ====== 5. 发送消息构造 ======
    print("\n[5] 发送消息构造...")
    # 检查反向查找逻辑（alice → wxid_alice123）
    wechat_ids = [pid.removeprefix("wechat:") for pid in mgr.get_platform_ids("alice") if pid.startswith("wechat:")]
    check("alice 反向查找微信 ID", wechat_ids == ["wxid_alice123"])

    # ====== 6. 群消息解析逻辑 ======
    print("\n[6] 群消息解析...")
    # 模拟群消息的 content 格式
    content = "wxid_alice123:\n你好呀大家！"
    sender, actual = content.split(":\n", 1)
    check("群消息拆分: sender", sender == "wxid_alice123")
    check("群消息拆分: content", actual == "你好呀大家！")

    # 模拟没有 :\n 的群消息
    content2 = "纯文本消息"
    check("群消息无拆分标记", ":\n" not in content2)

    print(f"\n{'='*40}")
    print(f"  {passed} passed, {failed} failed")
    print(f"{'='*40}")
    if failed == 0:
        print("\nAll tests passed! 🎉")
    else:
        import sys; sys.exit(1)

if __name__ == "__main__":
    asyncio.run(main())
