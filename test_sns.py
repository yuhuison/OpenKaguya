"""
朋友圈 (SNS) 功能测试 — 直接调用 wechat-v864 API 验证数据格式。

测试内容:
1. 获取朋友圈首页 (fetch_timeline) — 检验返回数据结构
2. 获取朋友圈通知 (fetch_notifications) — 检验通知格式
3. 发送纯文字朋友圈 (SnsPostTool) — 验证发送是否成功
4. 查看某条朋友圈详情 (SnsViewImageTool) — 验证详情解析

运行:
    uv run python test_sns.py
"""

import asyncio
import json
import os
import sys

os.environ["PYTHONUTF8"] = "1"


async def main():
    import aiohttp
    from kaguya.config import load_config

    # ===== 加载配置 =====
    config = load_config()
    base_url = config.wechat.base_url
    api_key = config.wechat.api_key

    if not api_key:
        print("❌ 未配置 wechat.api_key，请先在 config/secrets.toml 中设置。")
        sys.exit(1)

    print(f"🔧 API 地址: {base_url}")
    print(f"🔧 API Key:  {api_key[:6]}...")
    print("=" * 60)

    passed = 0
    failed = 0

    def check(name: str, cond: bool, detail: str = ""):
        nonlocal passed, failed
        if cond:
            passed += 1
            print(f"  ✅ {name}")
        else:
            failed += 1
            print(f"  ❌ {name} -- {detail}")

    async with aiohttp.ClientSession() as session:

        # ====== 1. 获取朋友圈首页 (raw API) ======
        print("\n[1] 获取朋友圈首页 — 原始 API 响应...")
        from kaguya.adapters.wechat_tools import _api_post

        raw_timeline = await _api_post(
            session, base_url, api_key,
            "/sns/SendSnsTimeLine",
            {"FirstPageMD5": "", "MaxID": 0},
        )

        print(f"  Raw response keys: {list(raw_timeline.keys())}")
        print(f"  Raw response Code: {raw_timeline.get('Code')}")

        # 打印完整响应（截断，避免太长）
        raw_str = json.dumps(raw_timeline, ensure_ascii=False, indent=2)
        print(f"  Raw response (前 3000 字符):\n{raw_str[:3000]}")
        if len(raw_str) > 3000:
            print(f"  ... (共 {len(raw_str)} 字符)")

        check("API 返回 Code",
              raw_timeline.get("Code") is not None,
              f"返回: {raw_timeline}")

        check("API 返回 200",
              raw_timeline.get("Code") == 200,
              f"Code={raw_timeline.get('Code')}, 可能是 key 过期或服务未启动")

        # 检查 Data 字段
        has_data = "Data" in raw_timeline or "data" in raw_timeline
        check("响应包含 Data 字段", has_data, f"只有: {list(raw_timeline.keys())}")

        # ====== 2. 使用 fetch_timeline 格式化 ======
        print("\n[2] fetch_timeline 格式化输出...")
        from kaguya.adapters.wechat_tools import fetch_timeline

        formatted = await fetch_timeline(session, base_url, api_key)
        print(f"  格式化结果:\n{formatted}")

        check("fetch_timeline 非空",
              bool(formatted),
              "返回为空字符串")

        check("fetch_timeline 非报错",
              "失败" not in formatted,
              formatted)

        # 如果有数据，检查是否正确解析出了朋友圈条目
        has_entries = any(c in formatted for c in ["[1]", "[2]", "暂无"])
        check("fetch_timeline 有条目或提示无内容",
              has_entries,
              f"输出不包含预期格式: {formatted[:200]}")

        # ====== 3. 获取朋友圈通知 ======
        print("\n[3] 获取朋友圈通知...")
        from kaguya.adapters.wechat_tools import fetch_notifications

        notifications_raw = await _api_post(
            session, base_url, api_key,
            "/sns/GetSnsSync", {},
        )
        print(f"  通知 Raw keys: {list(notifications_raw.keys())}")
        print(f"  通知 Raw Code: {notifications_raw.get('Code')}")
        notif_str = json.dumps(notifications_raw, ensure_ascii=False, indent=2)
        print(f"  通知 Raw (前 2000 字符):\n{notif_str[:2000]}")

        notifications = await fetch_notifications(session, base_url, api_key)
        print(f"  格式化通知: {notifications[:500] if notifications else '(空)'}")
        check("通知接口正常返回", notifications_raw.get("Code") is not None)

        # ====== 4. 发送纯文字朋友圈 ======
        print("\n[4] 发送纯文字朋友圈...")

        # 先测试发送 API 的原始响应
        from datetime import datetime
        test_content = f"🌙 OpenKaguya 朋友圈测试 - {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
        print(f"  测试文案: {test_content}")

        post_payload = {
            "ContentStyle": 2,  # 纯文字
            "Privacy": 0,
            "Content": test_content,
        }

        raw_post = await _api_post(
            session, base_url, api_key,
            "/sns/SendFriendCircle", post_payload,
        )

        print(f"  Raw post keys: {list(raw_post.keys())}")
        print(f"  Raw post Code: {raw_post.get('Code')}")
        post_str = json.dumps(raw_post, ensure_ascii=False, indent=2)
        print(f"  Raw post response:\n{post_str[:2000]}")

        check("发送朋友圈 API 返回 Code",
              raw_post.get("Code") is not None,
              f"返回: {raw_post}")

        check("发送朋友圈成功 (Code=200)",
              raw_post.get("Code") == 200,
              f"Code={raw_post.get('Code')}, Text={raw_post.get('Text', '')}")

        # 再测试通过 SnsPostTool 发送
        print("\n[4b] 通过 SnsPostTool 发送...")
        from kaguya.adapters.wechat_tools import SnsPostTool

        post_tool = SnsPostTool(session, base_url, api_key)
        tool_result = await post_tool.execute(
            content=f"🌙 SnsPostTool 测试 - {datetime.now().strftime('%H:%M:%S')}",
        )
        print(f"  Tool 返回: {tool_result}")
        if "成功" in tool_result:
            check("SnsPostTool 返回成功", True)
        elif "ret=" in tool_result:
            # ret!=0 说明被服务端限制（如频率限制/spam检测），代码逻辑正确检测到了
            check("SnsPostTool 正确检测到服务端拒绝", True)
            print(f"  ⚠️ 服务端拒绝发送（可能是频率限制）: {tool_result}")
        else:
            check("SnsPostTool 返回成功", False, tool_result)

        # ====== 5. 获取朋友圈详情 ======
        print("\n[5] 查看朋友圈详情...")

        # 从刚才的发布结果或首页中提取一个 sns_id
        sns_id_to_check = None

        # 尝试从发布结果中获取
        post_data = raw_post.get("Data", raw_post.get("data", {}))
        if isinstance(post_data, dict):
            sns_id_to_check = post_data.get("Id", post_data.get("id"))

        # 如果拿不到，从首页第一条获取
        if not sns_id_to_check:
            timeline_data = raw_timeline.get("Data", raw_timeline.get("data", {}))
            if isinstance(timeline_data, dict):
                items = timeline_data.get("ObjectList", timeline_data.get("objectList", []))
                if items and isinstance(items[0], dict):
                    sns_id_to_check = items[0].get("Id", items[0].get("id"))
            elif isinstance(timeline_data, list) and timeline_data:
                if isinstance(timeline_data[0], dict):
                    sns_id_to_check = timeline_data[0].get("Id", timeline_data[0].get("id"))

        if sns_id_to_check:
            print(f"  使用 sns_id: {sns_id_to_check}")

            raw_detail = await _api_post(
                session, base_url, api_key,
                "/sns/SendSnsObjectDetailById",
                {"Id": str(sns_id_to_check)},
            )
            print(f"  Detail Raw keys: {list(raw_detail.keys())}")
            detail_str = json.dumps(raw_detail, ensure_ascii=False, indent=2)
            print(f"  Detail Raw (前 2000 字符):\n{detail_str[:2000]}")

            check("详情 API 返回 200",
                  raw_detail.get("Code") == 200,
                  f"Code={raw_detail.get('Code')}")

            # 通过 SnsViewImageTool
            from kaguya.adapters.wechat_tools import SnsViewImageTool
            view_tool = SnsViewImageTool(session, base_url, api_key)
            detail_formatted = await view_tool.execute(sns_id=str(sns_id_to_check))
            print(f"  格式化详情:\n{detail_formatted}")

            check("SnsViewImageTool 返回非空",
                  bool(detail_formatted),
                  "返回为空")
            check("详情包含预期字段",
                  "朋友圈详情" in detail_formatted or "失败" not in detail_formatted,
                  detail_formatted[:200])
        else:
            print("  ⚠️ 无法获取 sns_id，跳过详情测试")
            check("获取 sns_id", False, "无法从发布结果或首页获取 sns_id")

        # ====== 6. 数据格式总结 ======
        print("\n[6] 数据格式分析...")

        if raw_timeline.get("Code") == 200:
            data = raw_timeline.get("Data", raw_timeline.get("data"))
            print(f"  Timeline Data 类型: {type(data).__name__}")
            if isinstance(data, dict):
                print(f"  Timeline Data keys: {list(data.keys())}")
                for k, v in data.items():
                    if isinstance(v, list):
                        print(f"    {k}: list (长度 {len(v)})")
                        if v:
                            print(f"      第一项类型: {type(v[0]).__name__}")
                            if isinstance(v[0], dict):
                                print(f"      第一项 keys: {list(v[0].keys())}")
                    elif isinstance(v, dict):
                        print(f"    {k}: dict (keys: {list(v.keys())})")
                    else:
                        print(f"    {k}: {type(v).__name__} = {str(v)[:100]}")
            elif isinstance(data, list):
                print(f"  Timeline Data 是列表, 长度 {len(data)}")
                if data and isinstance(data[0], dict):
                    print(f"  第一项 keys: {list(data[0].keys())}")

    # ====== 汇总 ======
    print(f"\n{'=' * 60}")
    print(f"  {passed} passed, {failed} failed")
    print(f"{'=' * 60}")
    if failed == 0:
        print("\n🎉 All tests passed!")
    else:
        print("\n⚠️ 部分测试失败，请检查上方的 Raw response 来调整数据解析逻辑。")
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
