"""UI 目录的机器体检：把 README 里「每张图的验收标准」中能用像素判定
的那几条跑一遍。
为什么值得单独做：几百张图靠人眼看不过来，而下面这几件事恰恰是肉眼最容易漏、
机器一查就出来的：
- 空图 / 近似纯色：采集脚本抓早了（动画中途、内容还没渲染），看着「有」其实是空的；
- 主题不符：暗色图拍成浅色（无头浏览器默认浅色，忘了预置 localStorage 就是这个结果）；
- 视口不符：声明 1440×900 的图实际是 820 宽（会话开错视口）；
- 标题缺失 / 不是中文：目录是给人看的，英文 id 当标题等于没标题；
- 未登记：磁盘上有图但清单里没有（说明清单被后一轮采集覆盖过）。
输出 AUDIT.md 与 audit.json（都在截图根目录），并打印摘要。
用法：
    python scripts/ui-catalog/audit.py
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from PIL import Image, ImageStat

SHOT_ROOT = Path(
    os.environ.get("QIO_SHOT_ROOT")
    or Path(__file__).resolve().parents[2] / "frontend" / "e2e-shots" / "ui-catalog"
)

KNOWN_WIDTHS = {620, 820, 1440, 1680}
# 判「空图」不能只看 stddev：整体压暗但确实有内容的图（例如停在星球预热遮罩下的
# 空对话页）stddev 只有 2.9，可问候语清晰可见；也不能只看分位数差：浅色主题下
# 背景占了绝大多数像素，98 分位与中位数会相等，于是一堆正常的浅色图会被误判。
# 用「与背景色（中位数）明显不同的像素占比」才稳：有文字/控件就一定超过千分之几，
# 纯空白则接近 0。
CONTENT_MIN_RATIO = 0.004
CONTENT_DELTA = 30
DARK_MAX_MEAN = 110.0
LIGHT_MIN_MEAN = 90.0


def has_cjk(text: str) -> bool:
    return any("\u4e00" <= ch <= "\u9fff" for ch in text or "")


def percentile(hist: list[int], frac: float) -> int:
    """从 256 桶的亮度直方图里取分位数（不排序全图，快）。"""
    total = sum(hist)
    if total == 0:
        return 0
    target = total * frac
    acc = 0
    for value, count in enumerate(hist):
        acc += count
        if acc >= target:
            return value
    return 255


def content_ratio(hist: list[int], reference: int) -> float:
    """与背景色（中位数）差异明显的像素占比。"""
    total = sum(hist)
    if total == 0:
        return 0.0
    far = sum(count for value, count in enumerate(hist) if abs(value - reference) > CONTENT_DELTA)
    return far / total


def main() -> int:
    merged_path = SHOT_ROOT / "manifest-merged.json"
    if not merged_path.exists():
        print(f"找不到 {merged_path}：先跑 node scripts/ui-catalog/aggregate.mjs")
        return 1
    merged = json.loads(merged_path.read_text(encoding="utf-8"))

    flat: list[dict] = []
    theme_wrong: list[dict] = []
    viewport_wrong: list[dict] = []
    title_bad: list[dict] = []
    unregistered: list[dict] = []
    total = 0
    checked = 0

    for group, data in merged.get("groups", {}).items():
        for e in data.get("entries", []):
            total += 1
            if e.get("registered") is False:
                unregistered.append({"group": group, "id": e["id"]})
                continue
            rel = e.get("relPath") or ""
            path = SHOT_ROOT / rel
            if not rel or not path.exists():
                continue
            title = e.get("title") or ""
            if not has_cjk(title):
                title_bad.append({"group": group, "id": e["id"], "title": title})
            try:
                with Image.open(path) as img:
                    gray = img.convert("L")
                    stat = ImageStat.Stat(gray)
                    mean = stat.mean[0]
                    stddev = stat.stddev[0]
                    hist = gray.histogram()
                    median = percentile(hist, 0.5)
                    ratio = content_ratio(hist, median)
                    width, height = img.size
            except Exception as exc:
                flat.append({"group": group, "id": e["id"], "reason": f"读图失败：{exc}"})
                continue
            checked += 1

            if ratio < CONTENT_MIN_RATIO:
                flat.append(
                    {
                        "group": group,
                        "id": e["id"],
                        "title": title,
                        "reason": (
                            f"几乎没有可读内容（非背景像素占比 {ratio * 100:.2f}%，"
                            f"stddev={stddev:.1f}，mean={mean:.0f}，{width}×{height}）"
                        ),
                    }
                )
            theme = e.get("theme") or ""
            if theme == "dark" and mean > DARK_MAX_MEAN:
                theme_wrong.append(
                    {
                        "group": group,
                        "id": e["id"],
                        "title": title,
                        "reason": f"声明 dark，实测偏亮（mean={mean:.0f}）",
                    }
                )
            if theme == "light" and mean < LIGHT_MIN_MEAN:
                theme_wrong.append(
                    {
                        "group": group,
                        "id": e["id"],
                        "title": title,
                        "reason": f"声明 light，实测偏暗（mean={mean:.0f}）",
                    }
                )
            vp = e.get("viewport") or None
            # 只对「整页截图」做视口核对：局部元素截图（卡片、状态行、悬浮球）本来就是裁下来的，
            # 它的宽度和会话视口无关，拿它比会把正常的元素图全判成错。
            page_like = height >= 600 and width in KNOWN_WIDTHS
            if vp and page_like and width != vp.get("width"):
                viewport_wrong.append(
                    {
                        "group": group,
                        "id": e["id"],
                        "title": title,
                        "reason": f"声明 {vp.get('width')}×{vp.get('height')}，实际 {width}×{height}",
                    }
                )

    lines: list[str] = []
    lines.append("# UI 目录机器体检")
    lines.append("")
    lines.append(
        f"检查 {checked} 张已登记截图（目录共 {total} 条，其中未登记 {len(unregistered)} 张）。"
    )
    lines.append("")
    lines.append(
        f"空图判据：与背景色差异超过 {CONTENT_DELTA} 级的像素占比 < {CONTENT_MIN_RATIO * 100:.1f}%。"
        "这是启发式 —— **细长的裁切图**（状态行、单条通知）文字占比天然偏低，可能被列进来，"
        "判读时结合标题与图册一起看；真正要修的是「整页偏暗、内容看不清」那一类。"
    )
    lines.append("")

    def section(title: str, rows: list[dict], fmt) -> None:
        lines.append(f"## {title}（{len(rows)}）")
        lines.append("")
        if not rows:
            lines.append("无。")
            lines.append("")
            return
        lines.append("| 分组 | id | 标题 | 说明 |")
        lines.append("| --- | --- | --- | --- |")
        for r in rows[:200]:
            lines.append(
                f"| {r.get('group','')} | `{r.get('id','')}` | {r.get('title','')} | {fmt(r)} |"
            )
        if len(rows) > 200:
            lines.append(f"| … | … | … | 还有 {len(rows) - 200} 条，见 audit.json |")
        lines.append("")

    section("疑似空图 / 近似纯色", flat, lambda r: r.get("reason", ""))
    section("主题不符（暗色拍成浅色 / 浅色拍成暗色）", theme_wrong, lambda r: r.get("reason", ""))
    section("视口不符", viewport_wrong, lambda r: r.get("reason", ""))
    section("标题缺失或不是中文", title_bad, lambda r: f"标题：{r.get('title','') or '（空）'}")
    lines.append(f"## 未登记（{len(unregistered)}）")
    lines.append("")
    lines.append(
        "磁盘上有这些图，但没有任何清单登记它们：标题 / 主题 / 视口都缺失。"
        "常见原因是多路并采时先跑完那一路的清单被后写的一份覆盖掉了。"
        "它们仍被收进图册（标成「未登记」），不假装完整。"
    )
    lines.append("")
    for r in unregistered[:80]:
        lines.append(f"- `{r['group']}/{r['id']}`")
    if len(unregistered) > 80:
        lines.append(f"- …还有 {len(unregistered) - 80} 条，见 audit.json")
    lines.append("")

    (SHOT_ROOT / "AUDIT.md").write_text("\n".join(lines), encoding="utf-8")
    (SHOT_ROOT / "audit.json").write_text(
        json.dumps(
            {
                "checked": checked,
                "total": total,
                "flat": flat,
                "themeWrong": theme_wrong,
                "viewportWrong": viewport_wrong,
                "titleBad": title_bad,
                "unregistered": unregistered,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    print(f"体检 {checked} 张（未登记 {len(unregistered)}）")
    print(
        f"  疑似空图 {len(flat)} · 主题不符 {len(theme_wrong)} · "
        f"视口不符 {len(viewport_wrong)} · 标题问题 {len(title_bad)}"
    )
    print(f"产物：{SHOT_ROOT / 'AUDIT.md'} / audit.json")
    return 0


if __name__ == "__main__":
    sys.exit(main())
