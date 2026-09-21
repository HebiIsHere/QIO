/**
 * 把逐状态截图汇总成：接触表（带中文标签的拼版图）、索引 INDEX.md、可点图册 gallery.html。
 *
 * 为什么用 Playwright 渲染 HTML 而不是图像库：拼版要写中文标签，
 * 浏览器里排版（字体、换行、等比缩放）最省事，而且不引进新依赖 ——
 * 直接用已经装好的 Playwright + 系统 Edge 跑一次「截网页」。
 *
 * 用法：node scripts/ui-catalog/sheets.mjs
 * 输入：frontend/e2e-shots/ui-catalog/manifest-*.json（全部合并）
 * 产物：frontend/e2e-shots/ui-catalog/_sheets/<group>-sheet-N.png、INDEX.md、gallery.html
 */
import { mkdirSync, readdirSync, readFileSync, writeFileSync } from "node:fs";
import path from "node:path";
import { launchBrowser, SHOT_ROOT } from "./lib.mjs";

const COLS = 4;
const PER_SHEET = 24; // 4 列 × 6 行
const THUMB_W = 360;

/** 自然序：conv-2 排在 conv-10 前面 */
function naturalCompare(a, b) {
  return String(a).localeCompare(String(b), "zh-Hans-CN", { numeric: true, sensitivity: "base" });
}

function loadEntries() {
  const files = readdirSync(SHOT_ROOT).filter(
    (f) => f.startsWith("manifest-") && f.endsWith(".json"),
  );
  const byFile = new Map();
  for (const file of files) {
    let payload;
    try {
      payload = JSON.parse(readFileSync(path.join(SHOT_ROOT, file), "utf-8"));
    } catch (err) {
      console.log(`跳过无法解析的 ${file}: ${err.message}`);
      continue;
    }
    for (const entry of payload.entries ?? []) {
      if (!entry.file) continue;
      // 同一张图被多份 manifest 收录时只留一条（后者不覆盖前者的标题）
      if (!byFile.has(entry.file)) byFile.set(entry.file, { ...entry, source: file });
    }
  }
  return [...byFile.values()];
}

function groupOf(entry) {
  return entry.group ?? "未分组";
}

function sheetHtml(group, items, pageNo, totalPages, totalCount) {
  const cells = items
    .map((e) => {
      const fileUrl = `file:///${e.file.replace(/\\/g, "/")}`;
      return `<figure>
  <img src="${fileUrl}" alt="${escapeHtml(e.title)}" />
  <figcaption>
    <span class="id">${escapeHtml(e.id)}</span>
    <span class="title">${escapeHtml(e.title)}</span>
    <span class="meta">${escapeHtml(e.theme ?? "")} · ${e.viewport?.width ?? "?"}×${e.viewport?.height ?? "?"}</span>
  </figcaption>
</figure>`;
    })
    .join("\n");
  return `<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8" />
<style>
  :root { color-scheme: dark; }
  * { box-sizing: border-box; }
  body { margin: 0; padding: 28px 32px 40px; background: #171015; color: #f2e9ef;
         font-family: "Microsoft YaHei", "Noto Sans SC", system-ui, sans-serif; }
  header { display: flex; align-items: baseline; gap: 14px; margin-bottom: 20px; }
  h1 { font-size: 26px; margin: 0; font-weight: 650; letter-spacing: 0.01em; }
  .sub { font-size: 13px; color: #b9a8b3; }
  .grid { display: grid; grid-template-columns: repeat(${COLS}, ${THUMB_W}px); gap: 18px 22px; }
  figure { margin: 0; display: flex; flex-direction: column; gap: 8px; }
  img { width: ${THUMB_W}px; height: auto; display: block; border: 1px solid #43303c;
        border-radius: 10px; background: #0f0a0d; }
  figcaption { display: flex; flex-direction: column; gap: 3px; }
  .id { font-family: Consolas, "Cascadia Mono", monospace; font-size: 11px; color: #c51b7d; }
  .title { font-size: 13px; line-height: 1.35; color: #f2e9ef; }
  .meta { font-family: Consolas, monospace; font-size: 10.5px; color: #8d7d87; }
</style></head>
<body>
  <header>
    <h1>${escapeHtml(group)}</h1>
    <span class="sub">第 ${pageNo}/${totalPages} 页 · 本页 ${items.length} 张 · 该分组共 ${totalCount} 张</span>
  </header>
  <div class="grid">
${cells}
  </div>
</body></html>`;
}

function escapeHtml(value) {
  return String(value ?? "")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

function indexMarkdown(groups) {
  const lines = [
    "# QIO UI 目录索引",
    "",
    `生成时间：${new Date().toISOString()}`,
    "",
    "| 分组 | 张数 |",
    "| --- | --- |",
    ...groups.map(([group, items]) => `| ${group} | ${items.length} |`),
    "",
  ];
  for (const [group, items] of groups) {
    lines.push(`## ${group}（${items.length} 张）`, "");
    lines.push("| id | 标题 | 主题 | 视口 | 文件 |");
    lines.push("| --- | --- | --- | --- | --- |");
    for (const e of items) {
      const rel = path.relative(SHOT_ROOT, e.file).split("\\").join("/");
      lines.push(
        `| ${e.id} | ${e.title} | ${e.theme ?? ""} | ${e.viewport?.width ?? "?"}×${e.viewport?.height ?? "?"} | [${rel}](${rel}) |`,
      );
    }
    lines.push("");
  }
  return lines.join("\n");
}

function galleryHtml(groups) {
  const nav = groups
    .map(([group, items]) => `<a href="#g-${encodeURIComponent(group)}">${escapeHtml(group)} ${items.length}</a>`)
    .join(" · ");
  const sections = groups
    .map(([group, items]) => {
      const cards = items
        .map((e) => {
          const rel = path.relative(SHOT_ROOT, e.file).split("\\").join("/");
          return `<figure id="${escapeHtml(e.id)}">
  <img loading="lazy" src="${rel}" alt="${escapeHtml(e.title)}" />
  <figcaption><b>${escapeHtml(e.id)}</b><span>${escapeHtml(e.title)}</span>
  <i>${escapeHtml(e.theme ?? "")} · ${e.viewport?.width ?? "?"}×${e.viewport?.height ?? "?"}</i></figcaption>
</figure>`;
        })
        .join("\n");
      return `<section id="g-${encodeURIComponent(group)}"><h2>${escapeHtml(group)} <small>${items.length} 张</small></h2>
<div class="grid">
${cards}
</div></section>`;
    })
    .join("\n");
  return `<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8" /><title>QIO UI 目录</title>
<style>
  :root { color-scheme: dark; }
  body { margin: 0; padding: 26px 30px 60px; background: #171015; color: #f2e9ef;
         font-family: "Microsoft YaHei", system-ui, sans-serif; }
  h1 { font-size: 24px; margin: 0 0 6px; }
  nav { font-size: 13px; color: #b9a8b3; margin-bottom: 22px; }
  nav a { color: #e46bb0; text-decoration: none; }
  h2 { font-size: 18px; margin: 34px 0 14px; border-top: 1px solid #3a2a34; padding-top: 16px; }
  h2 small { color: #8d7d87; font-weight: 400; font-size: 12px; }
  .grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(300px, 1fr)); gap: 18px; }
  figure { margin: 0; display: flex; flex-direction: column; gap: 6px;
           border: 1px solid #33242c; border-radius: 10px; padding: 10px; background: #1d1419; }
  img { width: 100%; height: auto; border-radius: 6px; }
  figcaption { display: flex; flex-direction: column; gap: 2px; font-size: 12px; }
  figcaption b { font-family: Consolas, monospace; color: #c51b7d; font-size: 11px; }
  figcaption i { color: #8d7d87; font-style: normal; font-size: 10.5px; }
</style></head>
<body>
  <h1>QIO UI 目录</h1>
  <nav>${nav}</nav>
${sections}
</body></html>`;
}

async function main() {
  const entries = loadEntries();
  if (!entries.length) throw new Error(`没有找到任何 manifest 条目（${SHOT_ROOT}）`);

  const byGroup = new Map();
  for (const e of entries) {
    const g = groupOf(e);
    if (!byGroup.has(g)) byGroup.set(g, []);
    byGroup.get(g).push(e);
  }
  const groups = [...byGroup.entries()].map(([g, items]) => [
    g,
    items.slice().sort((a, b) => naturalCompare(a.id, b.id)),
  ]);
  groups.sort((a, b) => naturalCompare(a[0], b[0]));

  const sheetDir = path.join(SHOT_ROOT, "_sheets");
  mkdirSync(sheetDir, { recursive: true });

  const browser = await launchBrowser();
  const page = await browser.newPage({ viewport: { width: COLS * (THUMB_W + 22) + 70, height: 1200 } });

  const written = [];
  for (const [group, items] of groups) {
    const pages = Math.max(1, Math.ceil(items.length / PER_SHEET));
    for (let i = 0; i < pages; i += 1) {
      const chunk = items.slice(i * PER_SHEET, (i + 1) * PER_SHEET);
      const htmlPath = path.join(sheetDir, `${group}-sheet-${i + 1}.html`);
      writeFileSync(htmlPath, sheetHtml(group, chunk, i + 1, pages, items.length), "utf-8");
      await page.goto(`file:///${htmlPath.replace(/\\/g, "/")}`);
      // 等图片真的解码完，否则会拍出空框
      await page.waitForFunction(
        () => [...document.images].every((img) => img.complete && img.naturalWidth > 0),
        null,
        { timeout: 60000 },
      );
      const out = path.join(sheetDir, `${group}-sheet-${i + 1}.png`);
      await page.screenshot({ path: out, fullPage: true });
      written.push(out);
      console.log(`[sheet] ${out}`);
    }
  }
  await browser.close();

  writeFileSync(path.join(SHOT_ROOT, "INDEX.md"), indexMarkdown(groups), "utf-8");
  writeFileSync(path.join(SHOT_ROOT, "gallery.html"), galleryHtml(groups), "utf-8");
  writeFileSync(
    path.join(SHOT_ROOT, "manifest-all.json"),
    JSON.stringify({ generatedAt: new Date().toISOString(), count: entries.length, entries }, null, 2),
    "utf-8",
  );
  console.log(`\n合计 ${entries.length} 张，${groups.length} 个分组；拼版 ${written.length} 张。`);
  for (const [group, items] of groups) console.log(`  - ${group}: ${items.length}`);
  console.log(`索引：${path.join(SHOT_ROOT, "INDEX.md")}`);
  console.log(`图册：${path.join(SHOT_ROOT, "gallery.html")}`);
}

await main();
