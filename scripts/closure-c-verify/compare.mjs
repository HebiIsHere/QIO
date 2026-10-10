/**
 * 前/后对照汇总（只给验收用）：把 shots/before-report.json 与 shots/after-report.json
 * 读成一张表，并把结论写成 shots/closure-c-compare.md（提交进仓库，便于复核）。
 *
 * 用法：node scripts/closure-c-verify/compare.mjs
 */
import { existsSync, readFileSync, writeFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const here = dirname(fileURLToPath(import.meta.url));
const load = (label) => {
  const file = join(here, "shots", label + "-report.json");
  if (!existsSync(file)) return null;
  return JSON.parse(readFileSync(file, "utf8"));
};

const before = load("before");
const after = load("after");
if (!before || !after) {
  console.error("缺少报告：先跑 capture.mjs --label=before / --label=after");
  process.exit(2);
}

const byTag = (report) => new Map(report.runs.map((run) => [run.tag.replace(report.label + "-", ""), run]));
const b = byTag(before);
const a = byTag(after);

const fmt = (value, digits = 2) => (value === null || value === undefined ? "-" : typeof value === "number" ? String(Number(value.toFixed(digits))) : String(value));
const ratio = (run, key) => (run && run.metrics.contrast && run.metrics.contrast[key] ? run.metrics.contrast[key].ratio : null);
const tb = (run) => (run && run.metrics.toolbar ? run.metrics.toolbar.height + "px/" + run.metrics.toolbar.rows + "行" : "-");
const overlap = (run) => {
  if (!run || !run.metrics.overlap) return "-";
  const o = run.metrics.overlap;
  return "聊×批 " + fmt(o.chatBatch === null ? "n/a" : o.chatBatch) + " / 聊×栏 " + fmt(o.chatToolbar === null ? "n/a" : o.chatToolbar);
};
const lines = [];
lines.push("# 互动界面（C）前/后对照（自动生成，别手改）");
lines.push("");
lines.push("- 前置：后端 8921（独立临时 QIO_DATA_DIR）+ 前端 5421（vite.e2e.config.ts，修复 webfont 403）");
lines.push("- 同一份铺底数据与同一套步骤：" + JSON.stringify(after.seeded));
lines.push("- 生成时间：" + new Date().toISOString());
lines.push("");
lines.push("| 场景 | 工具栏 前 → 后 | 快捷键说明对比度 | 草稿状态 | 占位说明 | 失败原因 | 裁切 前/后 | 死按钮 前/后 | 可见文本开发术语 前/后 |");
lines.push("| --- | --- | --- | --- | --- | --- | --- | --- | --- |");
for (const key of [...a.keys()].sort()) {
  const rb = b.get(key);
  const ra = a.get(key);
  lines.push(
    "| " + key +
    " | " + tb(rb) + " → " + tb(ra) +
    " | " + fmt(ratio(rb, "panelHint")) + " → " + fmt(ratio(ra, "panelHint")) +
    " | " + fmt(ratio(rb, "draftStatus")) + " → " + fmt(ratio(ra, "draftStatus")) +
    " | " + fmt(ratio(rb, "placeholder")) + " → " + fmt(ratio(ra, "placeholder")) +
    " | " + fmt(ratio(rb, "submitFailureReason")) + " → " + fmt(ratio(ra, "submitFailureReason")) +
    " | " + (rb ? rb.metrics.clipped.length : "-") + " / " + (ra ? ra.metrics.clipped.length : "-") +
    " | " + (rb ? rb.metrics.deadButtons.length : "-") + " / " + (ra ? ra.metrics.deadButtons.length : "-") +
    " | " + (rb ? (rb.metrics.devTermsInVisibleText.join("、") || "无") : "-") + " / " + (ra ? (ra.metrics.devTermsInVisibleText.join("、") || "无") : "-") +
    " |",
  );
}
lines.push("");
lines.push("## 聊天与批量列表共存（同时展开）");
lines.push("");
lines.push("| 场景 | 重叠（聊×批 / 聊×工具栏） | 切换条 |");
lines.push("| --- | --- | --- |");
for (const key of [...a.keys()].filter((k) => k.endsWith("-coexist")).sort()) {
  const rb = b.get(key);
  const ra = a.get(key);
  lines.push("| " + key + " | 前 " + overlap(rb) + " → 后 " + overlap(ra) + " | " + (ra && ra.metrics.switchBar ? ra.metrics.switchBar : "未出现（空间够，两个面板都展开）") + " |");
}
lines.push("");
lines.push("## 开发术语扫描（页面可见文本）");
lines.push("");
lines.push("- 词表：" + JSON.stringify(["stale_check", "stale_state", "checkId", "impact_confirmation_required", "/api/", "draft_too_long", "undefined", "NaN", " seq"]));
const beforeTerms = [...new Set(before.runs.flatMap((run) => run.metrics.devTermsInVisibleText))];
const afterTerms = [...new Set(after.runs.flatMap((run) => run.metrics.devTermsInVisibleText))];
lines.push("- 前：" + (beforeTerms.join("、") || "无"));
lines.push("- 后：" + (afterTerms.join("、") || "无"));
const out = join(here, "shots", "closure-c-compare.md");
writeFileSync(out, lines.join("\n") + "\n", "utf8");
console.log("已写出 " + out);
console.log("前 terminology：" + (beforeTerms.join("、") || "无") + "  后：" + (afterTerms.join("、") || "无"));
