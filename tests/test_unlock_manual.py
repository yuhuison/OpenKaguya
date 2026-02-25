"""手动测试 ADB 解锁功能。

使用方法：
  uv run python tests/test_unlock_manual.py

流程：检查设备连接 → 截图查看当前状态 → 执行解锁 → 再次截图确认
"""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from kaguya.config import PhoneConfig
from kaguya.phone.controller import PhoneController

OUT_BEFORE = Path("unlock_before.png")
OUT_AFTER = Path("unlock_after.png")


async def main():
    config = PhoneConfig(adb_path="adb")
    controller = PhoneController(config)

    print("=" * 50)
    print("  解锁功能测试")
    print("=" * 50)

    # 检查设备
    connected = await controller.check_connected()
    if not connected:
        print("未检测到设备！请确认 USB 调试已开启。")
        return

    w, h = await controller.get_screen_size()
    print(f"设备已连接，分辨率 {w}x{h}")

    # 截图：解锁前
    print("\n[1/3] 截图（解锁前）...")
    img_before = await controller.screenshot()
    img_before.save(OUT_BEFORE)
    print(f"  已保存 → {OUT_BEFORE}")

    # 执行解锁
    print("\n[2/3] 执行 wake_and_unlock...")
    msg = await controller.wake_and_unlock()
    print(f"  结果: {msg}")

    # 等待动画
    print("  等待 2 秒让动画完成...")
    await asyncio.sleep(2)

    # 截图：解锁后
    print("\n[3/3] 截图（解锁后）...")
    img_after = await controller.screenshot()
    img_after.save(OUT_AFTER)
    print(f"  已保存 → {OUT_AFTER}")

    print("\n完成！请对比两张截图确认解锁是否成功：")
    print(f"  解锁前: {OUT_BEFORE}")
    print(f"  解锁后: {OUT_AFTER}")


if __name__ == "__main__":
    asyncio.run(main())
