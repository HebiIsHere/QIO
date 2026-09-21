"""把 UI 目录的截图拼成带标签的接触表（contact sheet）。

为什么需要：几十上百张图逐张点开太慢，接触表用来一眼扫一遍 ——
有没有空图、有没有把暗色图拍成浅色、状态对不对，扫一遍就能发现。

输入是 aggregate.mjs 产出的 contact-plan.json（每个分组一份清单），输出
contact-<group>-NN.png。图片分页切分，超高元素截图裁到顶部一段并标注「已裁切」，
避免一张竖图把整页撑得没法看。

用法：
    python scripts/ui-catalog/contact.py
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

SHOT_ROOT = Path(
    os.environ.get("QIO_SHOT_ROOT")
    or Path(__file__).resolve().parents[2] / "frontend" / "e2e-shots" / "ui-catalog"
)

TILE_W = 420
MAX_TILE_H = 360
COLS = 4
ROWS = 4
LABEL_H = 46
TITLE_H = 44
BG = "#100d12"
LABEL_FG = "#d9a6d2"
TITLE_FG = "#f2e9f1"
CUT_FG = "#e8a13a"


def load_font(size: int) -> ImageFont.FreeTypeFont:
    for name in ("msyh.ttc", "msyhbd.ttc", "simhei.ttf", "segoeui.ttf"):
        p = Path("C:/Windows/Fonts") / name
        if p.exists():
            return ImageFont.truetype(str(p), size)
    return ImageFont.load_default()


def make_tile(path: Path) -> Image.Image:
    img = Image.open(path).convert("RGB")
    ratio = TILE_W / img.width
    img = img.resize((TILE_W, max(1, int(img.height * ratio))), Image.LANCZOS)
    if img.height > MAX_TILE_H:
        img = img.crop((0, 0, TILE_W, MAX_TILE_H))
    return img


def main() -> int:
    plan_path = SHOT_ROOT / "contact-plan.json"
    if not plan_path.exists():
        print(f"找不到 {plan_path}：先跑 node scripts/ui-catalog/aggregate.mjs")
        return 1
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    id_font = load_font(13)
    font = load_font(16)
    title_font = load_font(24)
    tiny_font = load_font(14)

    total_pages = 0
    for sheet in plan.get("sheets", []):
        items = [it for it in sheet.get("items", []) if Path(it["path"]).exists()]
        if not items:
            print(f"{sheet['group']}: 没有可用截图，跳过")
            continue
        per_page = COLS * ROWS
        pages = (len(items) + per_page - 1) // per_page
        for page in range(pages):
            chunk = items[page * per_page : (page + 1) * per_page]
            # 先做好每一块，再按「本行最高的一块」定行高：整页高度平均分配会留下大片空白，
            # 让人误以为有些状态没拍到。
            tiles: list[tuple[str, str, Image.Image]] = []
            for it in chunk:
                try:
                    tiles.append((it.get("id", ""), it.get("title", ""), make_tile(Path(it["path"]))))
                except Exception as e:
                    print(f"  跳过 {it['path']}：{e}")
            if not tiles:
                continue
            rows = [tiles[i : i + COLS] for i in range(0, len(tiles), COLS)]
            row_heights = [max(t[2].height for t in row) + LABEL_H for row in rows]
            canvas = Image.new(
                "RGB", (TILE_W * COLS, TITLE_H + sum(row_heights)), BG
            )
            draw = ImageDraw.Draw(canvas)
            draw.text(
                (16, 10),
                f"{sheet['label']}（{sheet['group']}）· {len(items)} 张 · 第 {page + 1}/{pages} 页",
                font=title_font,
                fill=TITLE_FG,
            )
            y = TITLE_H
            for row, height in zip(rows, row_heights):
                for c, (item_id, item_title, tile) in enumerate(row):
                    x = c * TILE_W
                    canvas.paste(tile, (x, y + LABEL_H))
                    draw.text((x + 8, y + 4), item_id, font=id_font, fill="#8e7f8c")
                    text = item_title if len(item_title) <= 24 else item_title[:23] + "…"
                    draw.text((x + 8, y + 20), text, font=font, fill=LABEL_FG)
                    if tile.height >= MAX_TILE_H:
                        draw.text(
                            (x + 8, y + LABEL_H + MAX_TILE_H - 18),
                            "已裁切",
                            font=tiny_font,
                            fill=CUT_FG,
                        )
                y += height
            out = SHOT_ROOT / f"contact-{sheet['group']}-{page + 1:02d}.png"
            canvas.save(out)
            total_pages += 1
            print(f"saved {out.name} ({canvas.width}x{canvas.height})")
    print(f"\n共 {total_pages} 页接触表 → {SHOT_ROOT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
