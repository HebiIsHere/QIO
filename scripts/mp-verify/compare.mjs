/**
 * 「基线 vs 改版」对照：把两次 shots.mjs 的报告读成一张表 + 差异热图。
 *
 * 判读口径（写进文档，避免用眼睛下结论）：
 * 1. **美术有没有变** → 看「球体裁剪图」的逐像素差异。裁剪框是以画布中心为中心、
 *    边长 = 画布短边 × 1.02，所以它天然跟着球体走：布局把球挪到左边**不会**造成假差异。
 * 2. **球体直径** → 玫红结构包围盒的高度（竖直方向不被布局裁切），plus 未展开态的径向拟合。
 * 3. **环宽** → 横向扫描线上玫红笔画的宽度直方图峰（SDF 环是恒定像素宽描边）。
 * 4. **位置/布局** → 画布矩形中心（含 CSS 变换）与内容区左边缘的关系，属于「本轮要改对的行为」，
 *    单独列一栏，不混进美术结论。
 *
 * 用法：
 *   node scripts/mp-verify/compare.mjs --base docs/mp-verify/c-planet/shots/base \
 *        --rev docs/mp-verify/c-planet/shots/rev --out docs/mp-verify/c-planet/shots/compare
 */
import { mkdirSync, readFileSync, writeFileSync } from "node:fs";
import { existsSync } from "node:fs";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import {
  bestRotationAlignment,
  contourRingColor,
  decodePng,
  diffImage,
  diffStats,
  encodePng,
  probeMaterials,
  rotateAbout,
} from "./lib/png.mjs";

const here = dirname(fileURLToPath(import.meta.url));
const args = process.argv.slice(2);
const flag = (name, fallback) => {
  const index = args.indexOf("--" + name);
  if (index < 0) return fallback;
  return args[index + 1] ?? fallback;
};

const baseDir = resolve(String(flag("base", "")));
const revDir = resolve(String(flag("rev", "")));
const outDir = resolve(String(flag("out", join(here, "..", "..", "docs", "mp-verify", "c-planet", "shots", "compare"))));
const tolerance = Number(flag("tolerance", 2));

function loadReport(dir) {
  const file = join(dir, "report.json");
  if (!existsSync(file)) throw new Error("缺少报告：" + file);
  return JSON.parse(readFileSync(file, "utf8"));
}

const base = loadReport(baseDir);
const rev = loadReport(revDir);
mkdirSync(outDir, { recursive: true });

const key = (run) => run.viewport + "/" + run.scenario;
const indexOf = (report) => new Map(report.runs.filter((r) => !r.skipped).map((run) => [key(run), run]));
const baseRuns = indexOf(base);
const revRuns = indexOf(rev);

const rows = [];
const notes = [];
const fmt = (value, digits = 2) =>
  value === null || value === undefined ? "-" : typeof value === "number" ? String(Number(value.toFixed(digits))) : String(value);

for (const [runKey, baseRun] of baseRuns) {
  const revRun = revRuns.get(runKey);
  if (!revRun) {
    notes.push("改版缺少场景 " + runKey + "（未验证）");
    continue;
  }
  const row = { scenario: runKey, diffs: {}, metrics: {}, verdicts: {} };

  // 球体裁剪图（球心对齐）逐像素差异 = 美术是否变化
  const sphereBase = baseRun.files.sphere;
  const sphereRev = revRun.files.sphere;
  if (sphereBase && sphereRev && existsSync(sphereBase) && existsSync(sphereRev)) {
    const a = decodePng(readFileSync(sphereBase));
    const b = decodePng(readFileSync(sphereRev));
    if (a.width === b.width && a.height === b.height) {
      /**
       * 先做**单轴对齐**再比：基线球体的最终滚转角不确定（入口球空闲自转 + 聚焦 slerp 残留），
       * 不对齐就会得到 10%+ 的假差异。对齐角写进报告，热图也是对齐后的 —— 这样
       * 「相同尺寸与朝向」是可复核的，不是嘴上说说。
       */
      const center = baseRun.measurement?.centerInImage ?? { x: a.width / 2, y: a.height / 2 };
      const alignment = bestRotationAlignment(a, b, { cx: center.x, cy: center.y });
      row.diffs.alignment = alignment;
      const alignedB = alignment.ok ? rotateAbout(b, alignment.angle, { cx: center.x, cy: center.y }) : b;
      const stats = diffStats(a, alignedB, { tolerance });
      row.diffs.sphere = stats;
      row.diffs.sphereUnaligned = diffStats(a, b, { tolerance });
      const heat = encodePng(diffImage(a, alignedB, { tolerance }));
      const file = join(outDir, runKey.replace("/", "-") + "-sphere-diff.png");
      writeFileSync(file, heat);
      row.diffs.sphereHeatmap = file;
      row.verdicts.artIdentical = stats.changedPixels === 0;
    } else {
      notes.push(runKey + " 球体裁剪尺寸不同（" + a.width + "x" + a.height + " vs " + b.width + "x" + b.height + "），无法逐像素比对");
    }
  }

  // 舞台裁剪（含布局）：变化是预期的（球体要移位），只作记录
  const stageBase = baseRun.files.stage;
  const stageRev = revRun.files.stage;
  if (stageBase && stageRev && existsSync(stageBase) && existsSync(stageRev)) {
    const a = decodePng(readFileSync(stageBase));
    const b = decodePng(readFileSync(stageRev));
    if (a.width === b.width && a.height === b.height) {
      row.diffs.stage = diffStats(a, b, { tolerance });
      writeFileSync(join(outDir, runKey.replace("/", "-") + "-stage-diff.png"), encodePng(diffImage(a, b, { tolerance })));
    }
  }

  // 直径（包围盒高度：竖直方向不被布局裁切）
  // 直径：优先用「球心所在行的左右扫描」（像素证据），钩子半径作为同量级的交叉验证
  const rowScanOf = (run) =>
    run.measurement && run.measurement.rowScan && run.measurement.rowScan.ok ? run.measurement.rowScan : null;
  const sd = (run) => (run.measurement && run.measurement.silhouette && run.measurement.silhouette.ok ? run.measurement.silhouette : null);
  const baseSil = sd(baseRun);
  const revSil = sd(revRun);
  const baseScan = rowScanOf(baseRun);
  const revScan = rowScanOf(revRun);
  row.metrics.diameter = {
    base: baseScan ? baseScan.width : baseSil ? baseSil.height : null,
    rev: revScan ? revScan.width : revSil ? revSil.height : null,
    delta:
      baseScan && revScan ? revScan.width - baseScan.width : baseSil && revSil ? revSil.height - baseSil.height : null,
    method: baseScan ? "球心行左右扫描（像素）" : "玫红包围盒高度",
    leftRadius: { base: baseScan?.leftRadius ?? null, rev: revScan?.leftRadius ?? null },
    rightRadius: { base: baseScan?.rightRadius ?? null, rev: revScan?.rightRadius ?? null },
  };
  const baseRadial = baseRun.measurement?.radial;
  const revRadial = revRun.measurement?.radial;
  row.metrics.radial = {
    base: baseRadial?.ok ? baseRadial.diameter : null,
    rev: revRadial?.ok ? revRadial.diameter : null,
  };

  // 环宽（像素笔画宽度直方图）
  row.metrics.strokes = {
    base: baseRun.measurement?.strokeRuns?.top ?? [],
    rev: revRun.measurement?.strokeRuns?.top ?? [],
  };

  // 调色板（旋转无关）：颜色/透明度有没有变，看这份集合
  const palOf = (run) => run.measurement?.palette ?? null;
  const basePal = palOf(baseRun);
  const revPal = palOf(revRun);
  if (basePal && revPal) {
    const baseSet = new Set(basePal.significant);
    const revSet = new Set(revPal.significant);
    const onlyBase = [...baseSet].filter((rgb) => !revSet.has(rgb));
    const onlyRev = [...revSet].filter((rgb) => !baseSet.has(rgb));
    row.metrics.palette = {
      baseDistinct: basePal.distinct,
      revDistinct: revPal.distinct,
      baseSignificant: baseSet.size,
      revSignificant: revSet.size,
      onlyBase,
      onlyRev,
      identical: onlyBase.length === 0 && onlyRev.length === 0,
      baseTop: basePal.top.slice(0, 6),
      revTop: revPal.top.slice(0, 6),
    };
  }

  // 材质探针直接从球体裁剪图算（图像 + 报告里的球心位置），不依赖当初截图的中间结果，
  // 这样改了探针算法只需要重跑 compare，不用重跑浏览器。
  const matOf = (run) => {
    const file = run.files?.sphere;
    if (!file || !existsSync(file)) return null;
    const image = decodePng(readFileSync(file));
    const center = run.measurement?.centerInImage ?? { x: image.width / 2, y: image.height / 2 };
    const materials = probeMaterials(image, center);
    const scanRadius = run.measurement?.rowScan?.leftRadius;
    materials.contourRing = contourRingColor(image, {
      centerX: center.x,
      centerY: center.y,
      radius: scanRadius,
    });
    return materials;
  };
  const baseMat = matOf(baseRun);
  const revMat = matOf(revRun);
  if (baseMat && revMat) {
    const strongSame =
      JSON.stringify((baseMat.strongPinkTop ?? []).slice(0, 3).map((i) => i.rgb)) ===
      JSON.stringify((revMat.strongPinkTop ?? []).slice(0, 3).map((i) => i.rgb));
    row.metrics.materials = {
      base: baseMat,
      rev: revMat,
      same:
        (baseMat.contourRing?.mode?.rgb ?? "") === (revMat.contourRing?.mode?.rgb ?? "") &&
        (baseMat.interiorTop?.[0]?.rgb ?? "") === (revMat.interiorTop?.[0]?.rgb ?? "") &&
        strongSame,
    };
  }

  // 位置：球心（画布矩形中心，含 CSS 变换）相对内容区左边缘
  const centerOf = (run) => {
    const canvas = run.metrics.canvas;
    const content = run.metrics.content;
    if (!canvas || !content) return null;
    return {
      x: Number((canvas.left + canvas.width / 2 - content.left).toFixed(2)),
      y: Number((canvas.top + canvas.height / 2 - content.top).toFixed(2)),
      canvasWidth: canvas.width,
      contentWidth: content.width,
      contentHeight: content.height,
    };
  };
  row.metrics.center = { base: centerOf(baseRun), rev: centerOf(revRun) };
  row.metrics.mp = { base: baseRun.metrics.mp, rev: revRun.metrics.mp };
  // 半径：DEV 钩子给的布局值半径（不含 CSS 变换）—— 与画布高度成正比，是最硬的「直径不变」读数
  row.metrics.radiusHook = { base: baseRun.radiusHook ?? null, rev: revRun.radiusHook ?? null };
  // 管理态几何：球心 x（画布中心 - 内容区左边）、可见范围左右边界、右半球到达比例、选中标签 x
  row.metrics.manageGeometry = {
    base: baseRun.stageMeasurement
      ? {
          centerX: baseRun.stageMeasurement.canvasCenterX,
          left: baseRun.stageMeasurement.visibleLeftEdge,
          right: baseRun.stageMeasurement.visibleRightEdge,
          rightReachRatio: baseRun.stageMeasurement.rightReachRatio,
        }
      : null,
    rev: revRun.stageMeasurement
      ? {
          centerX: revRun.stageMeasurement.canvasCenterX,
          left: revRun.stageMeasurement.visibleLeftEdge,
          right: revRun.stageMeasurement.visibleRightEdge,
          rightReachRatio: revRun.stageMeasurement.rightReachRatio,
        }
      : null,
  };
  row.metrics.labelX = { base: baseRun.labelX ?? null, rev: revRun.labelX ?? null };
  row.metrics.horizontalOverflow = {
    base: baseRun.metrics.docScroll.horizontalOverflow,
    rev: revRun.metrics.docScroll.horizontalOverflow,
  };
  row.metrics.overflowScrollers = {
    base: baseRun.metrics.overflowScrollers?.length ?? 0,
    rev: revRun.metrics.overflowScrollers?.length ?? 0,
  };
  row.metrics.titlebar = { base: baseRun.metrics.titlebar, rev: revRun.metrics.titlebar };
  row.metrics.radiusToken = { base: baseRun.metrics.radiusToken, rev: revRun.metrics.radiusToken };
  rows.push(row);
}

// ---- 输出 markdown ----
const lines = [];
lines.push("# 记忆星球页 · 基线 vs 改版 对照（自动生成，别手改）");
lines.push("");
lines.push("- 基线截图标签：" + base.label + "；改版截图标签：" + rev.label);
lines.push("- 基线 URL：" + base.url + "；改版 URL：" + rev.url);
lines.push("- 生成时间：" + new Date().toISOString());
lines.push("- 比对方法：球体裁剪图以画布中心为中心裁剪（边长 = 画布短边 × 1.02），所以「球被布局挪到左边」不会造成假差异；逐像素容差 ≤ " + tolerance + "/255 视为未变化。");
lines.push("- 对齐：以球心为圆心、按最佳对齐角旋转改版裁剪图后再比（基线自身滚转角不可复现，见下文噪声地板）。");
lines.push("");
lines.push("**比较前先做单轴（视线轴）对齐**：以球心为圆心在 -180°..+180° 内搜最佳对齐角（2° 粗搜 + 0.25° 细化），");
lines.push("对齐后再逐像素比。不对齐的数字同时列出，用来显示「朝向不确定」会造成多大的假差异。");
lines.push("");
lines.push("| 场景 | 对齐角 | 对齐后变化像素 | 对齐前变化像素 | 对齐后最大通道差 | 球轮廓直径 基→改（像素扫描） | 半径（DEV 钩子）基→改 | 环宽笔画峰（改） |");
lines.push("| --- | --- | --- | --- | --- | --- | --- | --- |");
for (const row of rows) {
  const sphere = row.diffs.sphere;
  lines.push(
    "| " + row.scenario +
    " | " + (row.diffs.alignment?.ok ? row.diffs.alignment.angle + "°" : "-") +
    " | " + (sphere ? sphere.changedPixels + " / " + sphere.totalPixels + "（" + (sphere.changedRatio * 100).toFixed(3) + "%）" : "-") +
    " | " + (row.diffs.sphereUnaligned ? (row.diffs.sphereUnaligned.changedRatio * 100).toFixed(3) + "%" : "-") +
    " | " + (sphere ? sphere.maxDelta : "-") +
    " | " + fmt(row.metrics.diameter.base) + " → " + fmt(row.metrics.diameter.rev) + "（Δ " + fmt(row.metrics.diameter.delta) + "，" + row.metrics.diameter.method + "）" +
    " | " + fmt(row.metrics.radiusHook.base) + " → " + fmt(row.metrics.radiusHook.rev) +
    " | " + (row.metrics.strokes.rev.map((item) => item.width + "px×" + item.count).join("、") || "-") +
    " |",
  );
}
lines.push("");
lines.push("## 位置与布局（本轮要改对的行为，不算美术差异）");
lines.push("");
lines.push("判据（Lead 口径）：管理态稳定后球心 x ≈ 0（±2px）；球体可见范围左边界 = 0、右边界 ≈ 球心 + 半径（右半球完整）；半径与管理前一致（±1px）。");
lines.push("");
lines.push("| 场景 | 球心 x（相对内容区左边缘）基→改 | 可见左边界 基→改 | 可见右边界 基→改 | 右半球到达比 基→改 | 选中标签 x 基→改 | --mp 基→改 | 横向滚动 基→改 |");
lines.push("| --- | --- | --- | --- | --- | --- | --- | --- |");
for (const row of rows) {
  const gb = row.metrics.manageGeometry.base;
  const gr = row.metrics.manageGeometry.rev;
  lines.push(
    "| " + row.scenario +
    " | " + fmt(row.metrics.center.base?.x) + " → " + fmt(row.metrics.center.rev?.x) +
    " | " + fmt(gb?.left) + " → " + fmt(gr?.left) +
    " | " + fmt(gb?.right) + " → " + fmt(gr?.right) +
    " | " + fmt(gb?.rightReachRatio, 3) + " → " + fmt(gr?.rightReachRatio, 3) +
    " | " + fmt(row.metrics.labelX.base) + " → " + fmt(row.metrics.labelX.rev) +
    " | " + fmt(row.metrics.mp.base) + " → " + fmt(row.metrics.mp.rev) +
    " | " + String(row.metrics.horizontalOverflow.base) + " → " + String(row.metrics.horizontalOverflow.rev) +
    " |",
  );
}
lines.push("");
lines.push("## 材质探针（旋转无关：颜色/透明度/材质是否变化）");
lines.push("");
lines.push("基线自身的最终朝向不可复现（聚焦 slerp 在相机补间结束时被截断，帧时序一变角度就变）——");
lines.push("因此**逐像素比较要对着「噪声地板」看**（同一份代码跑两次的差异），而材质对比用结构位置上的颜色：");
lines.push("轮廓环在球心行的颜色、玫红结构最强那批像素的颜色、球体内部众数色。这三组由令牌决定，与朝向无关。");
lines.push("");
lines.push("| 场景 | 轮廓环颜色 基→改 | 玫红核心色（前 3）基 → 改 | 内部众数色 基 → 改 | 判定 |");
lines.push("| --- | --- | --- | --- | --- |");
for (const row of rows) {
  const m = row.metrics.materials;
  if (!m) {
    lines.push("| " + row.scenario + " | - | - | - | 无探针数据 |");
    continue;
  }
  const top = (list, n) => (list ?? []).slice(0, n).map((item) => item.rgb).join(" / ");
  lines.push(
    "| " + row.scenario +
    " | " + (m.base?.contourRing?.mode?.rgb ?? "-") + " → " + (m.rev?.contourRing?.mode?.rgb ?? "-") +
    " | " + top(m.base?.strongPinkTop, 3) + " → " + top(m.rev?.strongPinkTop, 3) +
    " | " + (m.base?.interiorTop?.[0]?.rgb ?? "-") + " → " + (m.rev?.interiorTop?.[0]?.rgb ?? "-") +
    " | " + (m.same ? "一致" : "有差异") +
    " |",
  );
}
lines.push("");
lines.push("## 调色板信息（仅供参考：抗锯齿中间色随朝向变化，**不作为判据**）");
lines.push("");
for (const row of rows) {
  const pal = row.metrics.palette;
  if (!pal) continue;
  lines.push("- " + row.scenario + "：distinct " + pal.baseDistinct + " → " + pal.revDistinct +
    "；top1 " + (pal.baseTop[0]?.rgb ?? "-") + " → " + (pal.revTop[0]?.rgb ?? "-"));
}
lines.push("");
lines.push("## 全局栏与圆角令牌");
lines.push("");
for (const row of rows) {
  lines.push("- " + row.scenario + "：--titlebar-height " + fmt(row.metrics.titlebar.base) + " → " + fmt(row.metrics.titlebar.rev) +
    "；--qio-ui-radius " + row.metrics.radiusToken.base + " → " + row.metrics.radiusToken.rev);
}
if (notes.length) {
  lines.push("");
  lines.push("## 未验证 / 说明");
  for (const note of notes) lines.push("- " + note);
}
const md = lines.join("\n") + "\n";
writeFileSync(join(outDir, "compare.md"), md, "utf8");
console.log(md);
console.log("写出：" + join(outDir, "compare.md"));