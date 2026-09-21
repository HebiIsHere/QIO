/**
 * UI 目录汇总：把各分组各自写的 manifest-*.json 合并成一份可用的目录。
 *
 * 为什么单独一步：每个分组由不同的人/进程采集，各自只写自己的 manifest-<group>.json
 *（有的还带后缀，例如 manifest-conv-root.json —— 两路同时在采同一个分组时用来避免互相覆盖）。
 * 图册、索引、接触表都需要「一份完整的清单」，所以这里做一次合并与体检：
 *
 * - 合并：按 `group/id` 去重（同 id 多来源时取文件更新的一份，并记下来源）；
 * - 体检：清单里文件不存在的条目、以及磁盘上存在但没进任何清单的图（孤儿）都列出来 ——
 *   目录的价值在于「穷举可信」，宁可显式报出来，也不要看起来完整实际漏了；
 * - 产物：manifest-merged.json / INDEX.md / gallery-raw.html / contact-plan.json（交给 contact.py 拼图）。
 *
 * 用法：
 *   node scripts/ui-catalog/aggregate.mjs
 */
import { existsSync, readFileSync, readdirSync, statSync, writeFileSync } from "node:fs";
import path from "node:path";
import { SHOT_ROOT } from "./lib.mjs";

const ROOT = SHOT_ROOT;

/** 逐分组、逐条目的中文说明；索引表用得上 */
const GROUP_LABELS = {
  conv: "对话页",
  settings: "设置页 + 调试页",
  planet: "星球",
  atoms: "原子控件画廊",
  smoke: "冒烟",
  probe: "探针",
};

function readJson(file) {
  try {
    return JSON.parse(readFileSync(file, "utf-8"));
  } catch (e) {
    console.warn(`跳过无法解析的清单：${file}（${e.message}）`);
    return null;
  }
}

function rel(p) {
  return path.relative(ROOT, p).split(path.sep).join("/");
}

if (!existsSync(ROOT)) {
  console.error(`截图根目录不存在：${ROOT}`);
  process.exit(1);
}

const manifestFiles = readdirSync(ROOT)
  .filter((n) => /^manifest-.*\.json$/.test(n) && n !== "manifest-merged.json")
  .map((n) => path.join(ROOT, n));

/** group/id → entry */
const merged = new Map();
const sources = new Map();

for (const mf of manifestFiles) {
  const data = readJson(mf);
  if (!data || !Array.isArray(data.entries)) continue;
  const group = data.group || path.basename(mf).replace(/^manifest-/, "").replace(/\.json$/, "");
  for (const raw of data.entries) {
    if (!raw?.id) continue;
    const entry = { ...raw };
    entry.group = raw.group || group;
    // file 可能是绝对路径（lib.mjs 写的就是绝对路径）；统一算成相对 SHOT_ROOT 的 URL 路径
    if (entry.file) {
      const abs = path.isAbsolute(entry.file) ? entry.file : path.resolve(ROOT, entry.file);
      entry.absFile = abs;
      entry.relPath = rel(abs);
    }
    const key = `${entry.group}/${entry.id}`;
    const prev = merged.get(key);
    const src = path.basename(mf);
    sources.set(key, [...(sources.get(key) ?? []), src]);
    if (!prev) {
      merged.set(key, entry);
      continue;
    }
    // 同 id 多来源：取文件更新的那一份（另一路可能重拍过）
    const newer =
      entry.absFile && prev.absFile && existsSync(entry.absFile) && existsSync(prev.absFile)
        ? statSync(entry.absFile).mtimeMs > statSync(prev.absFile).mtimeMs
        : false;
    if (newer) merged.set(key, entry);
  }
}

/** 体检 1：清单里的文件不存在 */
const missing = [];
for (const e of merged.values()) {
  if (e.absFile && !existsSync(e.absFile)) missing.push(e);
}

/** 体检 2：磁盘上有图、但没有进任何清单 */
const referenced = new Set([...merged.values()].map((e) => e.absFile).filter(Boolean));
const orphans = [];
const groupDirs = readdirSync(ROOT, { withFileTypes: true })
  .filter((d) => d.isDirectory())
  .map((d) => d.name);
for (const g of groupDirs) {
  const dir = path.join(ROOT, g);
  for (const f of readdirSync(dir)) {
    if (!f.toLowerCase().endsWith(".png")) continue;
    const abs = path.join(dir, f);
    if (referenced.has(abs)) continue;
    orphans.push({ group: g, file: f, absFile: abs, relPath: rel(abs), size: statSync(abs).size });
  }
}

/** 体检 3：空的 / 疑似失败的图（小于 1KB 基本是空白） */
const tiny = [...merged.values()]
  .filter((e) => e.absFile && existsSync(e.absFile) && statSync(e.absFile).size < 1024)
  .map((e) => ({ group: e.group, id: e.id, title: e.title, size: statSync(e.absFile).size }));

const byGroup = {};
for (const e of merged.values()) {
  (byGroup[e.group] ??= []).push(e);
}

/**
 * 孤儿图（磁盘上有、清单里没有）也放进目录里，标成「未登记」。
 *
 * 为什么要显式收录而不是只在体检里列一串文件名：这些图是**真实拍出来的状态**，
 * 有些时候清单被后一轮采集覆盖过（多路并采时发生过）。丢掉标题很可惜，但更糟的是
 * 让它们在图册里彻底消失 —— 那样「穷举」就成了看起来完整。标题缺失如实标出来。
 */
for (const o of orphans) {
  const id = o.file.replace(/\.png$/i, "");
  (byGroup[o.group] ??= []).push({
    id,
    group: o.group,
    title: `（未登记）${id}`,
    note: "磁盘上有这张图，但没有任何清单登记过它：标题、主题、视口都缺失",
    theme: "",
    viewport: null,
    url: "",
    relPath: o.relPath,
    absFile: o.absFile,
    registered: false,
  });
}

for (const g of Object.keys(byGroup)) {
  byGroup[g].sort((a, b) => a.id.localeCompare(b.id, "zh-Hans-CN", { numeric: true }));
}
/** 已登记的条数（体检与图册的计数口径）：孤儿不算「已覆盖的状态」 */
const registeredCount = merged.size;

const generatedAt = new Date().toISOString();

// ── manifest-merged.json：后续脚本（contact.py / gallery）唯一的输入 ──
writeFileSync(
  path.join(ROOT, "manifest-merged.json"),
  JSON.stringify(
    {
      generatedAt,
      sourceManifests: manifestFiles.map((f) => path.basename(f)),
      groups: Object.fromEntries(
        Object.entries(byGroup).map(([g, es]) => [
          g,
          {
            label: GROUP_LABELS[g] ?? g,
            count: es.length,
            entries: es.map((e) => ({
              id: e.id,
              group: e.group,
              title: e.title ?? "",
              note: e.note ?? "",
              theme: e.theme ?? "",
              viewport: e.viewport ?? null,
              url: e.url ?? "",
              relPath: e.relPath ?? "",
              sources: sources.get(`${e.group}/${e.id}`) ?? [],
              registered: e.registered !== false,
            })),
          },
        ]),
      ),
      health: {
        missingFiles: missing.map((e) => ({ group: e.group, id: e.id, file: e.relPath })),
        orphans: orphans.map((o) => ({ group: o.group, file: o.file, relPath: o.relPath, size: o.size })),
        tinyFiles: tiny,
      },
    },
    null,
    2,
  ),
  "utf-8",
);

// ── INDEX.md：给人读的索引表 ──
const lines = [];
lines.push("# QIO UI 目录 · 索引");
lines.push("");
lines.push(`生成时间：${generatedAt}`);
lines.push("");
lines.push(
  `共 **${registeredCount}** 条已登记状态（另收录 ${orphans.length} 张未登记图，见文末体检），来自 ${manifestFiles.length} 份分组清单（${manifestFiles
    .map((f) => path.basename(f))
    .join("、")}）。`,
);
lines.push("");
const order = ["conv", "settings", "planet", "atoms", ...Object.keys(byGroup).filter((g) => !["conv", "settings", "planet", "atoms"].includes(g))];
for (const g of order) {
  const es = byGroup[g];
  if (!es) continue;
  lines.push(`## ${GROUP_LABELS[g] ?? g}（\`${g}\`） · ${es.length} 条`);
  lines.push("");
  lines.push("| # | id | 标题 | 主题 | 视口 | 图 | 来源清单 |");
  lines.push("| --- | --- | --- | --- | --- | --- | --- |");
  for (const e of es) {
    const vp = e.viewport ? `${e.viewport.width}×${e.viewport.height}` : "—";
    const link = e.relPath ? `[png](${e.relPath})` : "—";
    const src = (e.registered === false ? "未登记" : (sources.get(`${e.group}/${e.id}`) ?? []).join("+")) || "—";
    lines.push(
      `| ${es.indexOf(e) + 1} | \`${e.id}\` | ${e.title || "—"} | ${e.theme || "—"} | ${vp} | ${link} | ${src} |`,
    );
  }
  lines.push("");
}

if (missing.length || orphans.length || tiny.length) {
  lines.push("## 体检（不完整的地方如实列出来）");
  lines.push("");
  if (missing.length) {
    lines.push(`### 清单里有、磁盘上没有（${missing.length} 条）`);
    lines.push("");
    for (const e of missing) lines.push(`- \`${e.group}/${e.id}\` ${e.title ?? ""} → ${e.relPath}`);
    lines.push("");
  }
  if (orphans.length) {
    lines.push(`### 磁盘上有图、但没进任何清单（${orphans.length} 张）`);
    lines.push("");
    lines.push("这些图目前没有标题与状态说明（采集脚本没把它们写进 manifest），先按文件名列出：");
    lines.push("");
    for (const o of orphans) lines.push(`- \`${o.relPath}\`（${Math.round(o.size / 1024)}KB）`);
    lines.push("");
  }
  if (tiny.length) {
    lines.push(`### 疑似空图（小于 1KB，${tiny.length} 张）`);
    lines.push("");
    for (const t of tiny) lines.push(`- \`${t.group}/${t.id}\` ${t.title ?? ""}（${t.size} bytes）`);
    lines.push("");
  }
}
writeFileSync(path.join(ROOT, "INDEX.md"), lines.join("\n"), "utf-8");

// ── 机器版图册（离线、无外部依赖）：输出到 gallery-raw.html ──
const esc = (s) =>
  String(s ?? "")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");

const groupsJson = JSON.stringify(
  Object.fromEntries(
    Object.entries(byGroup).map(([g, es]) => [
      g,
      {
        label: GROUP_LABELS[g] ?? g,
        items: es.map((e) => ({
          id: e.id,
          title: e.title ?? "",
          note: e.note ?? "",
          theme: e.theme ?? "",
          viewport: e.viewport ? `${e.viewport.width}×${e.viewport.height}` : "",
          src: e.relPath,
          registered: e.registered !== false,
          size: e.absFile && existsSync(e.absFile) ? statSync(e.absFile).size : 0,
        })),
      },
    ]),
  ),
);

const gallery = `<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<title>QIO UI 目录</title>
<style>
  :root { color-scheme: dark; }
  * { box-sizing: border-box; }
  body { margin: 0; background: #100d12; color: #efe7ee; font: 14px/1.5 -apple-system, "Segoe UI", "Microsoft YaHei", sans-serif; }
  header { position: sticky; top: 0; z-index: 10; padding: 14px 20px; background: rgba(16,13,18,.92); backdrop-filter: blur(8px); border-bottom: 1px solid #2c2430; display: flex; gap: 14px; align-items: center; flex-wrap: wrap; }
  h1 { font-size: 15px; margin: 0 12px 0 0; font-weight: 600; letter-spacing: .02em; }
  input[type=search] { flex: 1 1 260px; min-width: 200px; padding: 7px 12px; border-radius: 999px; border: 1px solid #3a2f3d; background: #191320; color: inherit; }
  .filters { display: flex; gap: 6px; flex-wrap: wrap; }
  .filters button { padding: 5px 12px; border-radius: 999px; border: 1px solid #3a2f3d; background: #191320; color: #cbbccb; cursor: pointer; font: inherit; }
  .filters button[aria-pressed=true] { background: #3b2440; color: #fff; border-color: #7c4a86; }
  .meta { color: #94868f; font-size: 12px; }
  main { padding: 18px 20px 60px; }
  section { margin-bottom: 34px; }
  h2 { font-size: 13px; text-transform: none; letter-spacing: .06em; color: #d9a6d2; margin: 0 0 12px; font-weight: 600; }
  .grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(300px, 1fr)); gap: 16px; }
  figure { margin: 0; background: #171320; border: 1px solid #2c2430; border-radius: 10px; overflow: hidden; display: flex; flex-direction: column; }
  figure img { width: 100%; height: 220px; object-fit: cover; object-position: top center; background: #0c0a0f; cursor: zoom-in; display: block; }
  figcaption { padding: 10px 12px; display: flex; flex-direction: column; gap: 4px; }
  .t { font-weight: 600; }
  .i { font: 11.5px/1.4 ui-monospace, Consolas, monospace; color: #8e7f8c; }
  .badge { display: inline-block; padding: 1px 7px; border-radius: 999px; border: 1px solid #3a2f3d; font-size: 11px; color: #b9a7b7; margin-right: 6px; }
  dialog { border: 0; padding: 0; background: transparent; max-width: 96vw; max-height: 96vh; }
  dialog img { max-width: 96vw; max-height: 92vh; border-radius: 8px; }
  dialog::backdrop { background: rgba(0,0,0,.82); }
</style>
</head>
<body>
<header>
  <h1>QIO UI 目录</h1>
  <input type="search" id="q" placeholder="搜索标题 / id / 备注…" />
  <div class="filters" id="filters"></div>
  <span class="meta" id="count"></span>
</header>
<main id="main"></main>
<dialog id="view"><img alt="" /></dialog>
<script>
const GROUPS = ${groupsJson};
const filters = new Set();
const main = document.getElementById("main");
const q = document.getElementById("q");
const countEl = document.getElementById("count");
const dialog = document.getElementById("view");
const dialogImg = dialog.querySelector("img");

const chips = document.getElementById("filters");
for (const [key, g] of Object.entries(GROUPS)) {
  const b = document.createElement("button");
  b.textContent = g.label + " (" + g.items.length + ")";
  b.setAttribute("aria-pressed", "false");
  b.onclick = () => { filters.has(key) ? filters.delete(key) : filters.add(key); render(); };
  chips.appendChild(b);
}
q.oninput = render;
dialog.onclick = () => dialog.close();

function human(n) { return n > 1024*1024 ? (n/1048576).toFixed(1)+"MB" : Math.round(n/1024)+"KB"; }

function render() {
  const needle = q.value.trim().toLowerCase();
  main.innerHTML = "";
  let shown = 0, total = 0;
  for (const [key, g] of Object.entries(GROUPS)) {
    if (filters.size && !filters.has(key)) continue;
    const items = g.items.filter(it =>
      !needle || (it.id + " " + it.title + " " + it.note).toLowerCase().includes(needle));
    total += g.items.length;
    if (!items.length) continue;
    shown += items.length;
    const sec = document.createElement("section");
    const h = document.createElement("h2");
    h.textContent = g.label + " · " + key + " — " + items.length + " 条";
    sec.appendChild(h);
    const grid = document.createElement("div");
    grid.className = "grid";
    for (const it of items) {
      const fig = document.createElement("figure");
      const img = document.createElement("img");
      img.src = it.src; img.alt = it.title; img.loading = "lazy";
      img.onclick = () => { dialogImg.src = it.src; dialogImg.alt = it.title; dialog.showModal(); };
      const cap = document.createElement("figcaption");
      const t = document.createElement("div"); t.className = "t"; t.textContent = it.title || it.id;
      const i = document.createElement("div"); i.className = "i"; i.textContent = it.id;
      const b = document.createElement("div");
      b.innerHTML = (it.registered ? "" : '<span class="badge" style="border-color:#7a5a2a;color:#e8a13a">未登记</span>') +
                    '<span class="badge">' + (it.theme || "—") + '</span>' +
                    '<span class="badge">' + (it.viewport || "—") + '</span>' +
                    '<span class="badge">' + human(it.size) + '</span>';
      cap.append(t, i);
      if (it.note) { const n = document.createElement("div"); n.className = "i"; n.textContent = it.note; cap.appendChild(n); }
      cap.appendChild(b);
      fig.append(img, cap);
      grid.appendChild(fig);
    }
    sec.appendChild(grid);
    main.appendChild(sec);
  }
  countEl.textContent = shown + " / " + total + " 条";
  for (const btn of chips.children) {
    const key = Object.keys(GROUPS)[[...chips.children].indexOf(btn)];
    btn.setAttribute("aria-pressed", String(filters.has(key)));
  }
}
render();
</script>
</body>
</html>
`;
// 注意：gallery.html 现在是给非技术读者看的「通俗版」（scripts/ui-catalog/human-page.mjs 生成）。
// 这里生成的是机器版（按清单直出），写到 gallery-raw.html，避免覆盖通俗版。
writeFileSync(path.join(ROOT, "gallery-raw.html"), gallery, "utf-8");

// ── contact-plan.json：交给 contact.py 拼接触表 ──
writeFileSync(
  path.join(ROOT, "contact-plan.json"),
  JSON.stringify(
    {
      generatedAt,
      sheets: Object.entries(byGroup).map(([g, es]) => ({
        group: g,
        label: GROUP_LABELS[g] ?? g,
        items: es
          .filter((e) => e.relPath && e.absFile && existsSync(e.absFile))
          .map((e) => ({
            id: e.id,
            title: e.title ?? "",
            label: `${e.id} · ${e.title ?? ""}`.trim(),
            path: e.absFile,
          })),
      })),
    },
    null,
    2,
  ),
  "utf-8",
);

console.log(`合并 ${manifestFiles.length} 份清单 → ${merged.size} 条`);
for (const [g, es] of Object.entries(byGroup)) console.log(`  ${g}: ${es.length} 条`);
console.log(`体检：清单缺文件 ${missing.length} · 孤儿图 ${orphans.length} · 疑似空图 ${tiny.length}`);
console.log(`产物：manifest-merged.json / INDEX.md / gallery.html / contact-plan.json（${ROOT}）`);
