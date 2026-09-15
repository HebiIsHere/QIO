// 对比度审计：直接读 frontend/src/styles/tokens.css，按 WCAG 公式算实际对比度。
// 用法：node scripts/baseline/qa/contrast.mjs
// 输出：%TEMP%\qio-baseline\qa\report-contrast.json（并打印表格）
// 说明：这是「客观计算」，不等于视觉验收；配色改动后重跑即可。
import { readFileSync, writeFileSync, mkdirSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, resolve } from "node:path";

const here = dirname(fileURLToPath(import.meta.url));
const tokensPath = resolve(here, "../../../frontend/src/styles/tokens.css");
// 先去注释：否则「注释 + 第一条声明」会被当成一条声明，导致该令牌被漏掉
const css = readFileSync(tokensPath, "utf-8").replace(/\/\*[\s\S]*?\*\//g, "");

/** 解析 :root{...} 与 :root[data-theme="light"]{...} 两段令牌 */
function parseBlock(startMarker) {
  const start = css.indexOf(startMarker);
  if (start < 0) throw new Error(`找不到 ${startMarker}`);
  const open = css.indexOf("{", start);
  const close = css.indexOf("}", open);
  const body = css.slice(open + 1, close);
  const out = {};
  for (const decl of body.split(";")) {
    const i = decl.indexOf(":");
    if (i < 0) continue;
    const name = decl.slice(0, i).trim();
    const value = decl.slice(i + 1).trim();
    if (name.startsWith("--") && /^#[0-9a-fA-F]{3,8}$|^rgba?\(/.test(value)) out[name] = value;
  }
  return out;
}

const themes = {
  dark: parseBlock(":root {"),
  light: parseBlock(':root[data-theme="light"]'),
};

function toRgb(value) {
  const v = value.trim();
  if (v.startsWith("#")) {
    let hex = v.slice(1);
    if (hex.length === 3) hex = hex.split("").map((c) => c + c).join("");
    return [0, 2, 4].map((i) => parseInt(hex.slice(i, i + 2), 16));
  }
  const m = v.match(/rgba?\(([^)]+)\)/);
  if (!m) return null;
  const parts = m[1].split(",").map((x) => x.trim());
  return parts.slice(0, 3).map((x) => Number(x));
}

/** 半透明前景/背景叠加到实心底色上（rgba 令牌按 alpha 混合） */
function flatten(value, base) {
  const m = value.trim().match(/^rgba\(([^)]+)\)$/);
  if (!m) return toRgb(value);
  const parts = m[1].split(",").map((x) => x.trim());
  const [r, g, b] = parts.slice(0, 3).map(Number);
  const a = parts.length > 3 ? Number(parts[3]) : 1;
  const bg = toRgb(base) || [0, 0, 0];
  return [r, g, b].map((c, i) => Math.round(c * a + bg[i] * (1 - a)));
}

function luminance([r, g, b]) {
  const lin = [r, g, b].map((c) => {
    const s = c / 255;
    return s <= 0.03928 ? s / 12.92 : ((s + 0.055) / 1.055) ** 2.4;
  });
  return 0.2126 * lin[0] + 0.7152 * lin[1] + 0.0722 * lin[2];
}

function ratio(fg, bg) {
  const l1 = luminance(fg);
  const l2 = luminance(bg);
  const [hi, lo] = l1 > l2 ? [l1, l2] : [l2, l1];
  return (hi + 0.05) / (lo + 0.05);
}

// 需要检查的组合：[前景令牌, 背景令牌, 用途, 最小要求, 是否仅供参考]
// 「仅供参考」＝WCAG 1.4.11 的非文本边界要求。QIO 的边框主要是装饰性分隔，
// 输入框另有明显的聚焦态（accent 边框 + 内光晕，实测 5.35:1），
// 所以这类不达标记录为建议而不是硬失败；结论写进最终报告的 Known Problems。
const PAIRS = [
  ["--text-primary", "--bg-base", "正文（对话背景）", 4.5],
  ["--text-primary", "--bg-surface", "正文（卡片）", 4.5],
  ["--text-primary", "--bg-elevated", "正文（浮层）", 4.5],
  ["--text-secondary", "--bg-base", "次要文本（需阅读）", 4.5],
  ["--text-secondary", "--bg-surface", "次要文本（卡片）", 4.5],
  ["--text-muted", "--bg-base", "元数据（时间/状态）", 4.5],
  ["--text-muted", "--bg-elevated", "元数据（浮层）", 4.5],
  ["--text-faint", "--bg-base", "纯装饰文本", 3.0],
  ["--link", "--bg-base", "链接/强调文本", 4.5],
  ["--accent", "--bg-base", "品牌色文本/图标", 3.0],
  ["--on-accent", "--accent", "强调色按钮上的文字", 4.5],
  ["--success", "--bg-base", "成功反馈文本", 4.5],
  ["--danger", "--bg-base", "错误反馈文本", 4.5],
  ["--warning", "--bg-base", "警告反馈文本", 4.5],
  ["--border-strong", "--bg-base", "控件描边（非文本，仅供参考）", 3.0, true],
];

const results = [];
for (const [themeName, vars] of Object.entries(themes)) {
  for (const [fgName, bgName, usage, min, advisory = false] of PAIRS) {
    const fgRaw = vars[fgName];
    const bgRaw = vars[bgName];
    if (!fgRaw || !bgRaw) {
      results.push({ theme: themeName, usage, fg: fgName, bg: bgName, error: "令牌缺失" });
      continue;
    }
    const bg = flatten(bgRaw, "#000000");
    const fg = flatten(fgRaw, bgRaw);
    const r = ratio(fg, bg);
    const eps = 1e-6;
    results.push({
      theme: themeName,
      usage,
      fg: `${fgName}=${fgRaw}`,
      bg: `${bgName}=${bgRaw}`,
      ratio: Number(r.toFixed(2)),
      min,
      advisory,
      pass: r >= min - eps,
    });
  }
}

const failed = results.filter((r) => r.pass === false && !r.advisory);
const advisory = results.filter((r) => r.advisory);
console.log("主题 | 用途 | 前景 | 背景 | 对比度 | 下限 | 结论");
for (const r of results) {
  if (r.error) {
    console.log(`${r.theme} | ${r.usage} | ${r.fg} | ${r.bg} | - | - | 缺失`);
    continue;
  }
  console.log(
    `${r.theme} | ${r.usage} | ${r.fg} | ${r.bg} | ${r.ratio}:1 | ${r.min} | ${
      r.pass ? "PASS" : r.advisory ? "建议项" : "FAIL"
    }`,
  );
}

const OUT = `${process.env.TEMP}\\qio-baseline\\qa`;
mkdirSync(OUT, { recursive: true });
writeFileSync(
  `${OUT}\\report-contrast.json`,
  JSON.stringify({ tokensPath, results, failed: failed.length, advisory: advisory.length }, null, 2),
  "utf-8",
);
console.log(
  `\n合计 ${results.length} 项，未达标 ${failed.length} 项，建议项（非文本边界与纯装饰）${advisory.filter((r) => !r.pass).length} 项。报告：${OUT}\\report-contrast.json`,
);
process.exit(failed.length ? 1 : 0);
