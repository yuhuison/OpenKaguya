"""手动测试 ADB 截图 + 编号圆圈标注 + 交互式点击调试。

使用方法：
  uv run python tests/test_phone_manual.py

交互流程：
  截图 → 显示网格信息 → 输入编号或坐标 → 点击 → 再次截图确认
  支持命令：<编号> 点击标记点 | x,y 点击坐标 | s 重新截图 | q 退出
"""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from kaguya.config import PhoneConfig
from kaguya.phone.controller import PhoneController
from kaguya.phone.screen import ScreenReader, ScreenState

OUT_ANNOTATED = Path("screenshot_annotated.png")
OUT_BEFORE = Path("screenshot_before.png")
OUT_AFTER = Path("screenshot_after.png")


async def take_screenshot(reader: ScreenReader, label: str = "截图") -> ScreenState:
    print(f"\n[{label}] 正在截图...")
    state = await reader.read()
    state.image.save(OUT_ANNOTATED)
    print(f"  已保存标注截图 → {OUT_ANNOTATED}")
    print(f"  {state.grid_info_text()}")
    return state


async def main():
    config = PhoneConfig(adb_path="adb", screenshot_scale=0.5)
    controller = PhoneController(config)

    print("=" * 60)
    print("  手机调试工具（V2 编号圆圈版）")
    print("=" * 60)

    # 检查设备
    connected = await controller.check_connected()
    if not connected:
        print("未检测到设备！请确认 USB 调试已开启。")
        return
    w, h = await controller.get_screen_size()
    print(f"设备已连接，分辨率 {w}x{h}")

    reader = ScreenReader(controller, scale=config.screenshot_scale)
    state = await take_screenshot(reader, "初始截图")

    print()
    print("命令说明：")
    print("  <编号>       点击对应标记点（如 42）")
    print("  <编号>+dx,dy 点击标记点+偏移（如 42+30,-20）")
    print("  x,y          直接点击坐标（如 540,960）")
    print("  s            重新截图刷新")
    print("  q            退出")
    print()

    while True:
        try:
            cmd = input(">>> ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\n退出。")
            break

        if not cmd:
            continue

        if cmd.lower() == "q":
            print("退出。")
            break

        if cmd.lower() == "s":
            state = await take_screenshot(reader, "重新截图")
            continue

        # 尝试解析为 "编号+dx,dy" 格式
        if "+" in cmd:
            try:
                label_str, offset_str = cmd.split("+", 1)
                label_num = int(label_str.strip())
                parts = offset_str.split(",")
                dx, dy = int(parts[0].strip()), int(parts[1].strip())
                x, y = reader.get_coord_center(label_num)
                x += dx
                y += dy
                print(f"  点击标记点 {label_num} +偏移({dx},{dy}) → ({x}, {y})...")
                state.image.save(OUT_BEFORE)
                await controller.tap(x, y)
                await asyncio.sleep(0.8)
                state = await take_screenshot(reader, "点击后截图")
                state.image.save(OUT_AFTER)
                print(f"  截图已保存: {OUT_BEFORE} / {OUT_AFTER}")
            except (ValueError, IndexError) as e:
                print(f"  解析失败: {e}，格式应为 编号+dx,dy（如 42+30,-20）")
            continue

        # 尝试解析为 "x,y" 坐标
        if "," in cmd:
            try:
                parts = cmd.split(",")
                tx, ty = int(parts[0].strip()), int(parts[1].strip())
                print(f"  点击坐标 ({tx}, {ty})...")
                state.image.save(OUT_BEFORE)
                await controller.tap(tx, ty)
                await asyncio.sleep(0.8)
                state = await take_screenshot(reader, "点击后截图")
                state.image.save(OUT_AFTER)
                print(f"  截图已保存: {OUT_BEFORE} / {OUT_AFTER}")
            except (ValueError, IndexError):
                print("  无法解析坐标，格式应为 x,y（如 540,960）")
            continue

        # 尝试解析为编号
        try:
            label_num = int(cmd)
        except ValueError:
            print(f"  未知命令：{cmd!r}，输入 s 重新截图，q 退出")
            continue

        try:
            x, y = reader.get_coord_center(label_num)
        except ValueError as e:
            print(f"  {e}")
            print("  提示：先截图(s)再使用编号")
            continue

        print(f"  点击标记点 {label_num} → 坐标 ({x}, {y})")
        state.image.save(OUT_BEFORE)

        await controller.tap(x, y)
        await asyncio.sleep(0.8)

        state = await take_screenshot(reader, f"点击 {label_num} 后截图")
        state.image.save(OUT_AFTER)
        print(f"  截图已保存: {OUT_BEFORE} / {OUT_AFTER}")


if __name__ == "__main__":
    asyncio.run(main())
