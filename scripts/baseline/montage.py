"""把逐帧截图拼成带标签的对比图（演示用）。
用法：
    python scripts/baseline/montage.py out.png --cols 4 --title "..." --list items.txt
其中 items.txt 每行是「标签|图片路径」（避免命令行引号问题）。
只在本地演示时使用，不参与产品构建。
"""
from __future__ import annotations

import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont


def load_font(size: int) -> ImageFont.FreeTypeFont:
    for name in ("msyh.ttc", "msyhbd.ttc", "simhei.ttf"):
        p = Path("C:/Windows/Fonts") / name
        if p.exists():
            return ImageFont.truetype(str(p), size)
    return ImageFont.load_default()


def main() -> None:
    args = sys.argv[1:]
    out = Path(args[0])
    cols = 4
    title = ""
    items: list[tuple[str, str]] = []
    i = 1
    while i < len(args):
        a = args[i]
        if a == "--cols":
            cols = int(args[i + 1])
            i += 2
        elif a == "--title":
            title = args[i + 1]
            i += 2
        elif a == "--list":
            for line in Path(args[i + 1]).read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                label, _, path = line.partition("|")
                items.append((label, path.strip()))
            i += 2
        else:
            label, _, path = a.partition("=")
            items.append((label, path))
            i += 1

    tile_w = 460
    label_h = 34
    title_h = 42 if title else 0
    tiles = []
    for label, path in items:
        img = Image.open(path).convert("RGB")
        ratio = tile_w / img.width
        img = img.resize((tile_w, int(img.height * ratio)), Image.LANCZOS)
        tiles.append((label, img))

    rows = (len(tiles) + cols - 1) // cols
    tile_h = max(t[1].height for t in tiles)
    canvas = Image.new("RGB", (tile_w * cols, title_h + rows * (tile_h + label_h)), "#171015")
    draw = ImageDraw.Draw(canvas)
    font = load_font(20)
    title_font = load_font(26)
    if title:
        draw.text((16, 8), title, font=title_font, fill="#f5eef2")
    for idx, (label, img) in enumerate(tiles):
        r, c = divmod(idx, cols)
        x = c * tile_w
        y = title_h + r * (tile_h + label_h)
        canvas.paste(img, (x, y + label_h))
        draw.text((x + 14, y + 6), label, font=font, fill="#e878bd")
    out.parent.mkdir(parents=True, exist_ok=True)
    canvas.save(out)
    print(f"saved {out} ({canvas.width}x{canvas.height})")


if __name__ == "__main__":
    main()
