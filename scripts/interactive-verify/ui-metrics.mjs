/**
 * 布局指标测量（主智能体维护）：给改前／改后对照表提供**可复现的数字**，而不是「看起来更好」。
 *
 * 测量项（每档尺寸 × 主题）：
 * 1. 底部工具栏高度与行数（800×600 目标 ≤96px、≤2 行）；
 * 2. 提交按钮矩形 + 命中测试（必须点得到自己）；
 * 3. 聊天面板与批量列表**同时展开**时的矩形与相交面积（目标 0）；
 * 4. 聊天消息可读区域（面板内容矩形，不能被另一面板盖住）；
 * 5. 选中卡片后：局部工具栏矩形、顶部连接点命中测试（elementFromPoint 必须命中连接点）。
 *
 * 用法：
 *   node scripts/interactive-verify/ui-metrics.mjs --label=before --app=http://127.0.0.1:5421 --backend=http://127.0.0.1:8921
 * 输出：docs/interactive-ui-screenshots/metrics-<label>.json + 控制台 markdown 表
 */
import { spawnSync } from "node:child_process";
import { mkdirSync, writeFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join, resolve } from "node:path";

const here = dirname(fileURLToPath(import.meta.url));
const root = resolve(here, "..", "..");
const arg = (name, fallback) => {
  const hit = process.argv.find((a) => a.startsWith("--" + name + "="));
  return hit ? hit.split("=").slice(1).join("=") : fallback;
};
const LABEL = arg("label", "after");
const APP = arg("app", "http://127.0.0.1:5299");
const BACKEND = arg("backend", "http://127.0.0.1:8791");
const BOARD = "board_default";
const GROUP_NAME = arg("group-name", "默认组名");
const OUT_DIR = join(root, "docs", "interactive-ui-screenshots");
mkdirSync(OUT_DIR, { recursive: true });

const SIZES = [[1440, 900], [1024, 768], [800, 600]];

function runSteps(steps, tag) {
  const file = join(process.env.TEMP || ".", "ui-metrics-" + LABEL + "-" + tag + ".json");
  writeFileSync(file, JSON.stringify(steps, null, 2), "utf8");
  const probe = spawnSync(process.execPath, [join(root, "scripts", "visual_probe_d3.mjs"), "@" + file], {
    encoding: "utf8",
    maxBuffer: 64 * 1024 * 1024,
  });
  const raw = probe.stdout || "";
  const start = raw.indexOf("{");
  if (start < 0) throw new Error("探针没有返回结果（" + tag + "）：" + (raw + (probe.stderr || "")).slice(0, 300));
  const parsed = JSON.parse(raw.slice(start));
  const values = (parsed.results || []).filter((r) => r.op === "eval").map((r) => r.value);
  for (let i = values.length - 1; i >= 0; i -= 1) {
    const text = typeof values[i] === "string" ? values[i].trim() : "";
    if (!text.startsWith("{")) continue;
    try { return JSON.parse(text); } catch { /* 继续往前找 */ }
  }
  throw new Error("没有取到 JSON 指标（" + tag + "）");
}

async function api(path, init) {
  const resp = await fetch(BACKEND + path, {
    ...(init || {}),
    headers: { "Content-Type": "application/json", Connection: "close", ...((init && init.headers) || {}) },
  });
  return resp.json();
}

/** 与 ui-screens.mjs 用同一份固定数据，保证改前／改后可比 */
async function seedBoard() {
  await api("/api/interactive/boards/" + BOARD + "/state", {
    method: "PUT",
    body: JSON.stringify({ state: { boardId: BOARD, seq: 0, updatedAt: new Date().toISOString(), cards: [], groups: [], links: [], selection: [] }, reason: "ui-metrics-reset" }),
  });
  const now = new Date().toISOString();
  const card = (id, kind, x, y, content, meta, checked) => ({
    id, kind, x, y, w: 280, h: 180, content, meta: meta || {}, deleted: false, folded: false,
    hidden: false, checked: Boolean(checked), bookmarked: false, createdAt: now, updatedAt: now,
  });
  const cards = [
    card("s_note_a", "text", 60, 60, "把材料按主题分组，先看差异再看结论。", {}, true),
    card("s_note_b", "text", 380, 60, "这一条还没有勾选，QIO 看不到它的文字。", {}, false),
    card("s_code", "code", 60, 300, "def summarize(items):\n    return [it.title for it in items]\n", { language: "python" }, false),
    card("s_long", "file", 380, 300, "2026 年第三季度跨团队协作材料汇总与后续行动项（含附录与修订记录）", { name: "2026Q3-跨团队协作材料汇总与后续行动项-最终修订版-v12.pdf" }, false),
  ];
  const groups = [{ id: "s_group", name: GROUP_NAME, defaultName: true, ordered: false, deleted: false, members: ["s_note_a", "s_code"], createdAt: now, updatedAt: now }];
  const links = [{ id: "s_link", src: "s_note_a", dst: "s_note_b", direction: false, meaning: "放在一起看", deleted: false, createdAt: now, updatedAt: now }];
  await api("/api/interactive/boards/" + BOARD + "/state", {
    method: "PUT",
    body: JSON.stringify({ state: { boardId: BOARD, seq: 1, updatedAt: now, cards, groups, links, selection: [] }, reason: "ui-metrics-seed" }),
  });
  const list = await api("/api/interactive/boards/" + BOARD + "/intents");
  for (const item of list.intents || []) {
    if (["pending", "needs_update", "waiting_dependency", "waiting_confirm"].includes(item.status)) {
      await api("/api/interactive/intents/" + item.id + "/reject", { method: "POST", body: "{}" });
    }
  }
  await api("/api/interactive/boards/" + BOARD + "/intents", { method: "POST", body: JSON.stringify({ demo: true }) });
}

const MEASURE_JS = `JSON.stringify((function(){
  const rect=(sel)=>{const e=document.querySelector(sel);if(!e)return null;const r=e.getBoundingClientRect();return {l:Math.round(r.left),t:Math.round(r.top),r:Math.round(r.right),b:Math.round(r.bottom),w:Math.round(r.width),h:Math.round(r.height)};};
  const hit=(sel)=>{const e=document.querySelector(sel);if(!e)return null;const r=e.getBoundingClientRect();const el=document.elementFromPoint(Math.round(r.left+r.width/2),Math.round(r.top+r.height/2));return !!(el&&(el===e||e.contains(el)));};
  const area=(a,b)=>{if(!a||!b)return 0;const w=Math.max(0,Math.min(a.r,b.r)-Math.max(a.l,b.l));const h=Math.max(0,Math.min(a.b,b.b)-Math.max(a.t,b.t));return w*h;};
  const toolbar=rect('[data-im="board-toolbar"]');
  const submit=rect('[data-im="submit"]');
  const chat=rect('[data-im="chat-panel"]');
  const batch=rect('[data-im="batch-list"]');
  // 工具栏行数：按子元素顶边聚类（同一行算一行）
  let rows=0;
  const bar=document.querySelector('[data-im="board-toolbar"]');
  if(bar){const tops=new Set();[...bar.querySelectorAll('button, input, select')].forEach((el)=>{const r=el.getBoundingClientRect();if(r.height>0)tops.add(Math.round(r.top/8));});rows=tops.size;}
  const point=(function(){const p=document.querySelector('[data-im="connect-point"]');if(!p)return null;const r=p.getBoundingClientRect();const x=Math.round(r.left+r.width/2),y=Math.round(r.top+r.height/2);const el=document.elementFromPoint(x,y);return {x,y,hitIm:(el&&el.getAttribute&&el.getAttribute('data-im'))||(el?el.className:null),isPoint:!!(el&&el.getAttribute&&el.getAttribute('data-im')==='connect-point')};})();
  return {toolbar, toolbarRows:rows, submit, submitHit:hit('[data-im="submit"]'), chat, batch, overlapChatBatch:area(chat,batch), cardToolbar:rect('[data-im="card-toolbar"]'), connectPoint:point, overflowX: document.documentElement.scrollWidth>window.innerWidth};
})())`;

async function measure(w, h) {
  const steps = [
    { op: "navigate", url: APP + "/#/interactive", ms: 7000 },
    { op: "viewport", width: w, height: h },
    { op: "wait", ms: 1300 },
    { op: "eval", js: `document.documentElement.setAttribute('data-theme','dark'); 'dark'` },
    { op: "eval", js: MEASURE_JS },
    // 选中第一张卡片后再量局部工具栏与连接点
    { op: "eval", js: `(function(){const c=document.querySelector('[data-im="card"]');if(!c)return 'no-card';const r=c.getBoundingClientRect();const o={bubbles:true,cancelable:true,composed:true,clientX:Math.round(r.left+r.width/2),clientY:Math.round(r.top+18),button:0,buttons:1,pointerId:1,pointerType:'mouse',isPrimary:true};c.dispatchEvent(new PointerEvent('pointerdown',o));document.dispatchEvent(new PointerEvent('pointerup',Object.assign({},o,{buttons:0})));return 'selected';})()` },
    { op: "wait", ms: 600 },
    { op: "eval", js: MEASURE_JS },
    // 同时展开聊天与批量列表再量一次
    { op: "eval", js: `(function(){const p=document.querySelector('[data-im="chat-panel"]');if(!p){const b=document.querySelector('[data-im="chat-toggle"]');if(b)b.click();}return 'chat';})()` },
    { op: "wait", ms: 700 },
    { op: "eval", js: `(function(){const l=document.querySelector('[data-im="batch-list"]');if(!l){const b=document.querySelector('[data-im="batch-entry"]');if(b)b.click();}return 'batch';})()` },
    { op: "wait", ms: 900 },
    { op: "eval", js: MEASURE_JS },
  ];
  return runSteps(steps, w + "x" + h);
}

const main = async () => {
  await seedBoard();
  const results = {};
  for (const [w, h] of SIZES) {
    results[w + "x" + h] = await measure(w, h);
    console.log("已测量 " + w + "x" + h);
  }
  const out = { label: LABEL, app: APP, backend: BACKEND, measuredAt: new Date().toISOString(), results };
  writeFileSync(join(OUT_DIR, "metrics-" + LABEL + ".json"), JSON.stringify(out, null, 2), "utf8");
  console.log("\n| 档位 | 工具栏高度 | 行数 | 提交可点 | 聊天×批量相交 | 局部工具栏 | 连接点命中 | 横向溢出 |");
  console.log("| --- | --- | --- | --- | --- | --- | --- | --- |");
  for (const [size, r] of Object.entries(results)) {
    console.log("| " + size + " | " + (r.toolbar ? r.toolbar.h + "px" : "-") + " | " + r.toolbarRows + " | " + (r.submitHit ? "是" : "否") + " | " + r.overlapChatBatch + "px² | " + (r.cardToolbar ? r.cardToolbar.w + "×" + r.cardToolbar.h : "-") + " | " + (r.connectPoint ? (r.connectPoint.isPoint ? "是" : "否（命中 " + r.connectPoint.hitIm + "）") : "-") + " | " + (r.overflowX ? "有" : "无") + " |");
  }
  console.log("\n已写入 " + join(OUT_DIR, "metrics-" + LABEL + ".json"));
};

main().catch((error) => {
  console.error("测量失败：" + (error && error.message ? error.message : error));
  process.exit(1);
});
