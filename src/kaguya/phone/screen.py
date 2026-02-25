"""ScreenReader — 屏幕理解模块（V2 纯视觉版）。

将手机屏幕转化为 AI 能理解的信息：
  1. 截图并缩放
  2. 覆盖全屏编号圆圈标记点（行优先，从左到右、从上到下）
  3. AI 通过视觉理解截图内容，用编号 + 偏移量定位点击

截图工具会返回网格间距，AI 可据此估算偏移量。
"""

from __future__ import annotations

from dataclasses import dataclass

from PIL import Image, ImageDraw, ImageFont

from kaguya.phone.controller import PhoneController


# ---------------------------------------------------------------------------
# 数据模型
# ---------------------------------------------------------------------------

GRID_SIZE = 120  # 网格间距（原始分辨率像素）


@dataclass
class ScreenState:
    image: Image.Image  # 带圆圈标注的截图
    screen_width: int = 0
    screen_height: int = 0
    grid_cols: int = 0
    grid_rows: int = 0
    grid_spacing: int = GRID_SIZE
    total_points: int = 0

    def grid_info_text(self) -> str:
        """返回网格信息，供 LLM 理解屏幕坐标系。"""
        if not self.total_points:
            return ""
        gs = self.grid_spacing
        return (
            f"屏幕 {self.screen_width}×{self.screen_height}，"
            f"网格 {self.grid_cols}列×{self.grid_rows}行，"
            f"共 {self.total_points} 个标记点，"
            f"相邻点间距 {gs}px。\n"
            f"编号从左到右、从上到下递增"
            f"（第一行 1~{self.grid_cols}，"
            f"第二行 {self.grid_cols + 1}~{self.grid_cols * 2}，以此类推）。\n"
            f"重要：大部分按钮/文字不会正好在标记点上。请用最近的标记点 + 偏移量点击，"
            f"或直接用 phone_tap_coord 指定估算坐标。"
            f"偏移量参考：半格={gs // 2}px，1/3格≈{gs // 3}px。"
        )


# ---------------------------------------------------------------------------
# ScreenReader
# ---------------------------------------------------------------------------


class ScreenReader:
    """截屏并覆盖编号圆圈标记点，AI 通过视觉理解屏幕内容。"""

    def __init__(self, controller: PhoneController, scale: float = 0.5):
        self.controller = controller
        self.scale = scale
        self._last_coord_map: dict[int, tuple[int, int]] = {}

    async def read(self) -> ScreenState:
        """截屏并生成带编号圆圈的 ScreenState。"""
        img = await self.controller.screenshot()
        original_w, original_h = img.size

        # 缩放截图
        if self.scale != 1.0:
            img = img.resize(
                (int(original_w * self.scale), int(original_h * self.scale)),
                Image.LANCZOS,
            )

        # 生成网格坐标点（行优先）
        coord_points, n_cols, n_rows = self._generate_grid(original_w, original_h)
        self._last_coord_map = {label: (x, y) for label, x, y in coord_points}

        # 绘制圆圈标注
        annotated = self._annotate(img, coord_points, self.scale)

        return ScreenState(
            image=annotated,
            screen_width=original_w,
            screen_height=original_h,
            grid_cols=n_cols,
            grid_rows=n_rows,
            grid_spacing=GRID_SIZE,
            total_points=len(coord_points),
        )

    def get_coord_center(self, label: int) -> tuple[int, int]:
        """根据编号返回坐标（原始分辨率）。"""
        if label not in self._last_coord_map:
            raise ValueError(f"找不到标签 {label}，请先截图")
        return self._last_coord_map[label]

    # ------------------------------------------------------------------
    # 内部方法
    # ------------------------------------------------------------------

    def _generate_grid(
        self,
        width: int,
        height: int,
    ) -> tuple[list[tuple[int, int, int]], int, int]:
        """生成全屏网格坐标点（行优先顺序，编号从 1 开始）。"""
        gs = GRID_SIZE

        cols: list[int] = []
        x = gs // 2
        while x < width:
            cols.append(x)
            x += gs

        rows: list[int] = []
        y = gs // 2
        while y < height:
            rows.append(y)
            y += gs

        # 行优先编号：第一行 1~n_cols, 第二行 n_cols+1~2*n_cols ...
        points: list[tuple[int, int, int]] = []
        label = 1
        for cy in rows:
            for cx in cols:
                points.append((label, cx, cy))
                label += 1

        return points, len(cols), len(rows)

    def _annotate(
        self,
        img: Image.Image,
        coord_points: list[tuple[int, int, int]],
        scale: float = 1.0,
    ) -> Image.Image:
        """在截图上绘制编号圆圈标记点。"""
        annotated = img.copy().convert("RGBA")
        overlay = Image.new("RGBA", annotated.size, (0, 0, 0, 0))
        draw = ImageDraw.Draw(overlay)

        try:
            font = ImageFont.truetype("arial.ttf", max(9, int(11 * scale)))
        except Exception:
            font = ImageFont.load_default()

        circle_r = max(3, int(5 * scale))

        for label, cx, cy in coord_points:
            sx = int(cx * scale)
            sy = int(cy * scale)

            # 圆圈标记
            draw.ellipse(
                [sx - circle_r, sy - circle_r, sx + circle_r, sy + circle_r],
                fill=(255, 70, 70, 130),
                outline=(255, 255, 255, 160),
            )

            # 编号文字
            text = str(label)
            try:
                bbox = draw.textbbox((0, 0), text, font=font)
                tw = bbox[2] - bbox[0]
                th = bbox[3] - bbox[1]
            except AttributeError:
                tw, th = len(text) * 6, 10

            # 文字位于圆圈右侧偏上
            tx = sx + circle_r + 2
            ty = sy - th // 2

            # 半透明底色提高可读性
            draw.rectangle(
                [tx - 1, ty - 1, tx + tw + 2, ty + th + 1],
                fill=(0, 0, 0, 110),
            )
            draw.text((tx, ty), text, fill=(255, 255, 255, 210), font=font)

        annotated = Image.alpha_composite(annotated, overlay)
        return annotated.convert("RGB")
