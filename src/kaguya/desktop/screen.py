"""DesktopScreenReader — YOLO UI 元素检测方案。

基于 OmniParser 的 icon_detect 模型（YOLO）：
  1. 截图
  2. YOLO 检测所有 UI 元素（按钮、图标、文本框等）
  3. 在截图上绘制编号边界框
  4. AI 通过编号直接点击目标元素
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any

from loguru import logger
from PIL import Image, ImageDraw, ImageFont

from kaguya.desktop.controller import DesktopController


# ---------------------------------------------------------------------------
# 数据模型
# ---------------------------------------------------------------------------


@dataclass
class ScreenState:
    image: Image.Image  # 带编号边界框的截图
    screen_width: int = 0
    screen_height: int = 0
    elements: list[dict[str, Any]] = field(default_factory=list)
    total_elements: int = 0

    def elements_info_text(self) -> str:
        """返回元素检测信息，供 LLM 理解屏幕内容。"""
        if not self.total_elements:
            return "未检测到 UI 元素。可使用 desktop_click_coord(x, y) 直接按坐标点击。"
        return (
            f"屏幕 {self.screen_width}×{self.screen_height}，"
            f"检测到 {self.total_elements} 个 UI 元素。\n"
            f"每个元素的编号标注在截图中的红色边界框旁。\n"
            f"使用 desktop_click(label=编号) 直接点击元素中心。\n"
            f"如需精确控制位置，可加 x_offset/y_offset 微调，"
            f"或用 desktop_click_coord(x, y) 按坐标点击。"
        )


# ---------------------------------------------------------------------------
# YOLO 模型加载
# ---------------------------------------------------------------------------


def _download_and_load_yolo(model_repo: str, model_file: str):
    """从 HuggingFace 下载并加载 YOLO 模型。"""
    import torch
    from huggingface_hub import hf_hub_download
    from ultralytics import YOLO

    logger.info(f"下载 YOLO 模型: {model_repo}/{model_file}")
    model_path = hf_hub_download(repo_id=model_repo, filename=model_file)

    model = YOLO(model_path)
    device = "cuda" if torch.cuda.is_available() else "cpu"
    model.to(device)
    logger.info(f"YOLO 模型已加载: device={device}")
    return model


# ---------------------------------------------------------------------------
# DesktopScreenReader
# ---------------------------------------------------------------------------


class DesktopScreenReader:
    """截屏并用 YOLO 检测 UI 元素，AI 通过编号点击目标。"""

    def __init__(
        self,
        controller: DesktopController,
        scale: float = 0.5,
        *,
        yolo_model_repo: str = "microsoft/OmniParser-v2.0",
        yolo_model_file: str = "icon_detect/model.pt",
        box_threshold: float = 0.05,
    ):
        self.controller = controller
        self.scale = scale
        self.box_threshold = box_threshold
        self._yolo_model_repo = yolo_model_repo
        self._yolo_model_file = yolo_model_file
        self._model = None  # 懒加载
        self._last_coord_map: dict[int, tuple[int, int]] = {}
        self._window_offset: tuple[int, int] = (0, 0)

    def _ensure_model(self):
        """懒加载 YOLO 模型（首次截图时触发下载）。"""
        if self._model is None:
            self._model = _download_and_load_yolo(
                self._yolo_model_repo, self._yolo_model_file,
            )

    async def read(self, hwnd: int | None = None) -> ScreenState:
        """截屏并检测 UI 元素，返回带编号标注的 ScreenState。"""
        self._ensure_model()

        img = await asyncio.to_thread(self.controller.screenshot_sync, hwnd)
        original_w, original_h = img.size

        # 窗口偏移
        if hwnd is not None:
            import ctypes
            import ctypes.wintypes as wt
            rect = wt.RECT()
            ctypes.windll.user32.GetWindowRect(hwnd, ctypes.byref(rect))
            self._window_offset = (rect.left, rect.top)
        else:
            self._window_offset = (0, 0)

        # YOLO 检测（在原始分辨率上）
        detections = await asyncio.to_thread(
            self._detect, img, self.box_threshold,
        )

        # 构建坐标映射（原始分辨率，编号从 1 开始）
        self._last_coord_map = {}
        elements = []
        for i, det in enumerate(detections):
            label = i + 1
            x1, y1, x2, y2 = det["bbox"]
            cx = int((x1 + x2) / 2)
            cy = int((y1 + y2) / 2)
            self._last_coord_map[label] = (cx, cy)
            elements.append({
                "id": label,
                "bbox": det["bbox"],
                "confidence": det["confidence"],
            })

        # 缩放截图
        if self.scale != 1.0:
            img = img.resize(
                (int(original_w * self.scale), int(original_h * self.scale)),
                Image.LANCZOS,
            )

        # 绘制编号边界框
        annotated = self._annotate(img, detections, self.scale)

        return ScreenState(
            image=annotated,
            screen_width=original_w,
            screen_height=original_h,
            elements=elements,
            total_elements=len(elements),
        )

    def get_coord_center(self, label: int) -> tuple[int, int]:
        """根据元素编号返回屏幕绝对坐标（含窗口偏移）。"""
        if label not in self._last_coord_map:
            raise ValueError(f"找不到元素 {label}，请先截图")
        x, y = self._last_coord_map[label]
        ox, oy = self._window_offset
        return x + ox, y + oy

    # ------------------------------------------------------------------
    # 内部方法
    # ------------------------------------------------------------------

    def _detect(self, img: Image.Image, threshold: float) -> list[dict[str, Any]]:
        """在图像上运行 YOLO 检测，返回检测结果列表。"""
        results = self._model.predict(source=img, conf=threshold, iou=0.1, verbose=False)
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
        # 按 y 坐标排序（从上到下、从左到右），使编号更直观
        detections.sort(key=lambda d: (d["bbox"][1] // 50, d["bbox"][0]))
        return detections

    def _annotate(
        self,
        img: Image.Image,
        detections: list[dict[str, Any]],
        scale: float = 1.0,
    ) -> Image.Image:
        """在截图上绘制编号边界框。"""
        annotated = img.copy().convert("RGBA")
        overlay = Image.new("RGBA", annotated.size, (0, 0, 0, 0))
        draw = ImageDraw.Draw(overlay)

        # 根据图片大小调整绘制参数
        box_overlay_ratio = max(img.size) / 3200
        thickness = max(int(3 * box_overlay_ratio), 1)

        try:
            font_size = max(10, int(14 * box_overlay_ratio))
            font = ImageFont.truetype("arial.ttf", font_size)
        except Exception:
            font = ImageFont.load_default()

        for i, det in enumerate(detections):
            label = i + 1
            x1, y1, x2, y2 = det["bbox"]

            # 缩放坐标
            sx1 = int(x1 * scale)
            sy1 = int(y1 * scale)
            sx2 = int(x2 * scale)
            sy2 = int(y2 * scale)

            # 边界框
            draw.rectangle(
                [sx1, sy1, sx2, sy2],
                outline=(255, 70, 70, 200),
                width=thickness,
            )

            # 编号标签
            text = str(label)
            try:
                bbox = draw.textbbox((0, 0), text, font=font)
                tw = bbox[2] - bbox[0]
                th = bbox[3] - bbox[1]
            except AttributeError:
                tw, th = len(text) * 7, 12

            # 标签背景（左上角）
            lx = sx1
            ly = max(sy1 - th - 4, 0)
            draw.rectangle(
                [lx, ly, lx + tw + 6, ly + th + 4],
                fill=(255, 70, 70, 220),
            )
            draw.text(
                (lx + 3, ly + 2), text,
                fill=(255, 255, 255, 255), font=font,
            )

        annotated = Image.alpha_composite(annotated, overlay)
        return annotated.convert("RGB")
