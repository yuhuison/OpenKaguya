"""
微信 WebSocket 消息接收测试 — 独立脚本，不依赖 OpenKaguya。
直接连接 wechat-v864 代理的 WebSocket，打印所有收到的消息。

用法: uv run python test_ws_recv.py
"""

import asyncio
import json
import sys
import os

os.environ["PYTHONUTF8"] = "1"

# ===== 配置 =====
# 从 secrets.toml 读取，或直接在这里改
BASE_URL = "http://open-kaguya.tech:8099"
API_KEY = ""  # 留空则从 secrets.toml 读

def load_key():
    """从 secrets.toml 读取 API Key"""
    global API_KEY, BASE_URL
    if API_KEY:
        return
    try:
        if sys.version_info >= (3, 11):
            import tomllib
        else:
            import tomli as tomllib
        with open("config/secrets.toml", "rb") as f:
            secrets = tomllib.load(f)
        API_KEY = secrets.get("wechat", {}).get("api_key", "")
        with open("config/default.toml", "rb") as f:
            defaults = tomllib.load(f)
        BASE_URL = defaults.get("wechat", {}).get("base_url", BASE_URL)
    except Exception as e:
        print(f"⚠️  无法读取配置: {e}")

def extract_str(value) -> str:
    if isinstance(value, dict):
        return value.get("str", value.get("string", ""))
    return str(value) if value else ""

async def main():
    load_key()
    if not API_KEY:
        print("❌ 没有找到 API Key！请在脚本顶部设置 API_KEY 或在 config/secrets.toml 中配置")
        return

    try:
        import aiohttp
    except ImportError:
        print("❌ 需要 aiohttp: uv add aiohttp")
        return

    ws_url = BASE_URL.replace("http://", "ws://").replace("https://", "wss://")
    ws_url = f"{ws_url}/ws/GetSyncMsg?key={API_KEY}"

    print(f"🔌 正在连接: {ws_url[:60]}...")
    print(f"   按 Ctrl+C 退出\n")

    async with aiohttp.ClientSession() as session:
        try:
            async with session.ws_connect(ws_url) as ws:
                print("✅ WebSocket 已连接！等待消息...\n")
                msg_count = 0
                async for msg in ws:
                    if msg.type == aiohttp.WSMsgType.TEXT:
                        msg_count += 1
                        try:
                            data = json.loads(msg.data)

                            # 先打印原始 JSON（缩进格式）
                            print(f"{'='*60}")
                            print(f"  📩 #{msg_count} 原始 JSON:")
                            print(json.dumps(data, ensure_ascii=False, indent=2)[:2000])
                            print()

                            msg_type = data.get("msgType", data.get("MsgType", data.get("msg_type", "?")))
                            from_user = extract_str(data.get("fromUserName", data.get("FromUserName", {})))
                            to_user = extract_str(data.get("toUserName", data.get("ToUserName", {})))
                            content = extract_str(data.get("content", data.get("msgContent", data.get("Content", {}))))
                            push = data.get("pushContent", "")
                            new_id = data.get("newMsgId", "")

                            # 群消息特殊处理
                            is_group = from_user.endswith("@chatroom")
                            actual_sender = from_user
                            actual_content = content
                            if is_group and ":\n" in content:
                                actual_sender, actual_content = content.split(":\n", 1)

                            type_names = {
                                1: "文本", 3: "图片", 34: "语音", 37: "好友请求",
                                43: "视频", 47: "表情", 49: "链接/文件",
                                10000: "系统", 10002: "撤回", 51: "状态",
                            }
                            type_str = type_names.get(msg_type, f"未知({msg_type})")

                            print(f"{'='*60}")
                            print(f"  📩 #{msg_count} [{type_str}]")
                            print(f"  来自: {actual_sender}")
                            if is_group:
                                print(f"  群组: {from_user}")
                            print(f"  目标: {to_user}")
                            print(f"  推送: {push}")
                            if msg_type == 1:
                                print(f"  内容: {actual_content[:200]}")
                            else:
                                print(f"  原始: {content[:100]}...")
                            print(f"  ID:   {new_id}")
                            print()

                        except Exception as e:
                            print(f"  ⚠️  解析失败: {e}")
                            print(f"  原始数据: {msg.data[:200]}\n")

                    elif msg.type == aiohttp.WSMsgType.ERROR:
                        print(f"❌ WebSocket 错误: {ws.exception()}")
                        break
                    elif msg.type == aiohttp.WSMsgType.CLOSED:
                        print("🔌 WebSocket 已关闭")
                        break

        except aiohttp.ClientError as e:
            print(f"❌ 连接失败: {e}")
        except KeyboardInterrupt:
            print(f"\n\n📊 共收到 {msg_count} 条消息")

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n👋 已退出")
