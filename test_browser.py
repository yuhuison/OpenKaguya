"""Browser-Use Cloud API 直接测试"""
import asyncio, os
os.environ["PYTHONUTF8"] = "1"

import sys
if sys.version_info >= (3, 11):
    import tomllib
else:
    import tomli as tomllib

# 读配置
with open("config/secrets.toml", "rb") as f:
    secrets = tomllib.load(f)
API_KEY = secrets.get("browser", {}).get("browser_use_api_key", "")

if not API_KEY:
    print("❌ 未找到 browser_use_api_key")
    sys.exit(1)

print(f"🔑 API Key: {API_KEY[:10]}...")

async def main():
    import aiohttp

    base = "https://api.browser-use.com/api/v2"
    headers = {
        "X-Browser-Use-API-Key": API_KEY,
        "Content-Type": "application/json",
    }

    async with aiohttp.ClientSession() as session:
        # 1. 创建任务
        print("\n[1] 创建搜索任务...")
        payload = {"task": "Search Google for '辉夜姬 传说' and return the first 3 search result titles"}
        async with session.post(f"{base}/tasks", json=payload, headers=headers) as resp:
            data = await resp.json()
            print(f"  状态码: {resp.status}")
            print(f"  响应: {data}")

            task_id = data.get("id", data.get("task_id", ""))
            if not task_id:
                print("❌ 创建任务失败")
                return
            print(f"  任务 ID: {task_id}")

        # 2. 轮询任务状态
        print("\n[2] 等待任务完成...")
        for i in range(60):
            await asyncio.sleep(3)
            async with session.get(f"{base}/tasks/{task_id}", headers=headers) as resp:
                data = await resp.json()
                status = data.get("status", "unknown")
                print(f"  [{i*3}s] 状态: {status}")
                if status in ("completed", "finished", "done", "failed", "error"):
                    print(f"\n  最终结果:")
                    print(f"  {data}")
                    break
        else:
            print("⏰ 超时")

asyncio.run(main())
