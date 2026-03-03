"""OmniParser YOLO UI 元素检测测试脚本。

用法:
    uv run python scripts/test_yolo_detection.py              # 截取整个屏幕
    uv run python scripts/test_yolo_detection.py --window 微信  # 截取指定窗口
    uv run python scripts/test_yolo_detection.py --image path/to/screenshot.png  # 使用已有图片
    uv run python scripts/test_yolo_detection.py --threshold 0.1   # 调整置信度阈值
    uv run python scripts/test_yolo_detection.py --scale 1.0       # 不缩放（原始分辨率）
    uv run python scripts/test_yolo_detection.py --no-open         # 不自动打开结果图片

输出:
    data/yolo_test_result.png  — 带编号边界框的标注截图
    终端打印所有检测到的 UI 元素列表
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

# 项目根目录加入 sys.path
ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT / "src"))


def main():
    parser = argparse.ArgumentParser(description="测试 OmniParser YOLO UI 元素检测")
    parser.add_argument("--window", type=str, default=None, help="截取指定标题的窗口（默认全屏）")
    parser.add_argument("--image", type=str, default=None, help="使用已有图片文件代替截屏")
    parser.add_argument("--threshold", type=float, default=0.05, help="检测置信度阈值（默认 0.05）")
    parser.add_argument("--scale", type=float, default=0.5, help="输出图片缩放比例（默认 0.5）")
    parser.add_argument("--no-open", action="store_true", help="不自动打开结果图片")
    parser.add_argument("--output", type=str, default=None, help="输出路径（默认 data/yolo_test_result.png）")
    parser.add_argument(
        "--model-repo", type=str, default="microsoft/OmniParser-v2.0",
        help="HuggingFace 模型仓库（默认 microsoft/OmniParser-v2.0）",
    )
    parser.add_argument(
        "--model-file", type=str, default="icon_detect/model.pt",
        help="模型文件路径（默认 icon_detect/model.pt）",
    )
    args = parser.parse_args()

    from PIL import Image

    # ── 1. 获取待检测图片 ──────────────────────────────────────────────
    if args.image:
        img_path = Path(args.image)
        if not img_path.exists():
            print(f"❌ 图片文件不存在: {img_path}")
            return
        print(f"📷 加载图片: {img_path}")
        img = Image.open(img_path).convert("RGB")
    else:
        print("📷 截取屏幕...")
        from kaguya.desktop.controller import DesktopController
        ctrl = DesktopController()

        hwnd = None
        if args.window:
            hwnd = ctrl._find_window_by_title(args.window)
            if hwnd:
                print(f"   找到窗口: 「{args.window}」(hwnd={hwnd})")
            else:
                print(f"   ⚠️ 未找到标题包含「{args.window}」的窗口，将截取全屏")

        img = ctrl.screenshot_sync(hwnd)

    print(f"   图片尺寸: {img.size[0]}×{img.size[1]}")

    # ── 2. 加载 YOLO 模型 ──────────────────────────────────────────────
    print(f"\n🔧 加载 YOLO 模型: {args.model_repo}/{args.model_file}")
    print("   （首次运行会从 HuggingFace 下载，需要几分钟...）")

    t0 = time.perf_counter()
    from kaguya.desktop.screen import _download_and_load_yolo
    model = _download_and_load_yolo(args.model_repo, args.model_file)
    t_load = time.perf_counter() - t0
    print(f"   模型加载耗时: {t_load:.2f}s")

    # ── 3. 运行检测 ────────────────────────────────────────────────────
    print(f"\n🔍 运行 YOLO 检测 (threshold={args.threshold})...")
    t0 = time.perf_counter()
    results = model.predict(source=img, conf=args.threshold, iou=0.1, verbose=False)
    t_detect = time.perf_counter() - t0

    detections = []
    if results and len(results) > 0:
        boxes = results[0].boxes
        for i in range(len(boxes)):
            x1, y1, x2, y2 = boxes.xyxy[i].tolist()
            conf = boxes.conf[i].item()
            detections.append({
                "bbox": [int(x1), int(y1), int(x2), int(y2)],
                "confidence": round(conf, 3),
            })
    # 排序：从上到下、从左到右
    detections.sort(key=lambda d: (d["bbox"][1] // 50, d["bbox"][0]))

    print(f"   检测耗时: {t_detect:.2f}s")
    print(f"   检测到 {len(detections)} 个 UI 元素\n")

    # ── 4. 打印检测结果 ────────────────────────────────────────────────
    if detections:
        print("┌─────┬────────────────────────────────┬───────────┐")
        print("│ 编号 │ 边界框 (x1, y1, x2, y2)        │ 置信度    │")
        print("├─────┼────────────────────────────────┼───────────┤")
        for i, det in enumerate(detections):
            label = i + 1
            bbox = det["bbox"]
            conf = det["confidence"]
            w = bbox[2] - bbox[0]
            h = bbox[3] - bbox[1]
            bbox_str = f"({bbox[0]:4d}, {bbox[1]:4d}, {bbox[2]:4d}, {bbox[3]:4d})"
            print(f"│ {label:3d} │ {bbox_str:30s} │ {conf:.3f}     │")
        print("└─────┴────────────────────────────────┴───────────┘")

        # 置信度分布统计
        confs = [d["confidence"] for d in detections]
        print(f"\n📊 置信度统计:")
        print(f"   最低: {min(confs):.3f}  最高: {max(confs):.3f}  平均: {sum(confs)/len(confs):.3f}")
        bins = [0, 0.1, 0.2, 0.3, 0.5, 0.7, 1.0]
        print(f"   分布:")
        for j in range(len(bins) - 1):
            count = sum(1 for c in confs if bins[j] <= c < bins[j + 1])
            bar = "█" * count
            print(f"     [{bins[j]:.1f}, {bins[j+1]:.1f}) : {count:3d} {bar}")
    else:
        print("   ⚠️ 未检测到任何 UI 元素，尝试降低 --threshold 值")

    # ── 5. 绘制标注并保存 ──────────────────────────────────────────────
    from kaguya.desktop.screen import DesktopScreenReader
    # 借用 _annotate 方法绘制标注
    mock_reader = DesktopScreenReader.__new__(DesktopScreenReader)

    # 缩放输出图
    scale = args.scale
    if scale != 1.0:
        display_img = img.resize(
            (int(img.size[0] * scale), int(img.size[1] * scale)),
            Image.LANCZOS,
        )
    else:
        display_img = img.copy()

    annotated = mock_reader._annotate(display_img, detections, scale)

    output_path = Path(args.output) if args.output else ROOT / "data" / "yolo_test_result.png"
    output_path.parent.mkdir(parents=True, exist_ok=True)
    annotated.save(str(output_path))
    print(f"\n💾 标注结果已保存: {output_path}")
    print(f"   输出尺寸: {annotated.size[0]}×{annotated.size[1]}")

    # ── 6. 自动打开 ───────────────────────────────────────────────────
    if not args.no_open:
        import os
        print(f"\n🖼️  正在打开结果图片...")
        os.startfile(str(output_path))


if __name__ == "__main__":
    main()
