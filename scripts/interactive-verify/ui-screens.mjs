/**
 * 改前／改后对照截图采集（主智能体维护）。
 *
 * 要求（提示词第四节）：同一尺寸、同一主题、同一数据；覆盖常态布局、选中注释、重叠成组、
 * 单项审批、聊天与批量列表同时展开、长消息／长代码、错误状态；三档 × 暗色与亮色；
 * 截图落到 `docs/interactive-ui-screenshots/` 并随分支提交。
 *
 * 用法：
 *   node scripts/interactive-verify/ui-screens.mjs --label=before --app=http://127.0.0.1:5421 --backend=http://127.0.0.1:8921
 *   node scripts/interactive-verify/ui-screens.mjs --label=after  --app=http://127.0.0.1:5299 --backend=http://127.0.0.1:8791
 *
 * 说明（诚实标注）：场景**布置**用页面内合成指针事件与接口铺数据（为了同一数据可复现、跑得快）；
 * 交互本身的正确性由 `fe-scenarios.mjs` 的真实鼠标/键盘/滚轮覆盖，不靠这里的截图。
 * 驱动用 Edge（scripts/visual_probe_d3.mjs）：本机 Chrome 在大量 headless 实例后不可靠。
 */
import { spawnSync } from "node:child_process";
import { copyFileSync, existsSync, mkdirSync, writeFileSync } from "node:fs";
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
/** 组名按被测版本的规则给（改前是「组 1」，改后是「默认组名」），这样对照图能直接看出命名差异 */
const GROUP_NAME = arg("group-name", "默认组名");
const SHOTS_SRC = join(process.env.TEMP || "", "qio-visual", "shots");
const OUT_DIR = join(root, "docs", "interactive-ui-screenshots");
mkdirSync(OUT_DIR, { recursive: true });
const SIZES = [[1440, 900], [1024, 768], [800, 600]];
const SCENES = ["normal", "selected", "grouped", "approval", "overlays", "error"];

function runSteps(steps, tag) {
  const file = join(process.env.TEMP || ".", "ui-screens-" + LABEL + "-" + tag + ".json");
  writeFileSync(file, JSON.stringify(steps, null, 2), "utf8");
  const probe = spawnSync(process.execPath, [join(root, "scripts", "visual_probe_d3.mjs"), "@" + file], {
    encoding: "utf8",
    maxBuffer: 64 * 1024 * 1024,
  });
  const raw = (probe.stdout || "") + (probe.stderr || "");
  if (!raw.includes('"results"')) throw new Error("探针没有返回结果（" + tag + "）：" + raw.slice(0, 300));
  return raw;
}

async function api(path, init) {
  const resp = await fetch(BACKEND + path, {
    ...(init || {}),
    headers: { "Content-Type": "application/json", Connection: "close", ...((init && init.headers) || {}) },
  });
  return resp.json();
}

/** 固定数据：两张注释（一条勾选、一条未勾选）、一个组、一条长代码卡、一条超长名称卡、一条关系 */
async function seedBoard() {
  await api("/api/interactive/boards/" + BOARD + "/state", {
    method: "PUT",
    body: JSON.stringify({
      state: { boardId: BOARD, seq: 0, updatedAt: new Date().toISOString(), cards: [], groups: [], links: [], selection: [] },
      reason: "ui-screens-reset",
    }),
  });
  const now = new Date().toISOString();
  const card = (id, kind, x, y, content, meta, checked) => ({
    id, kind, x, y, w: 280, h: 180, content, meta: meta || {}, deleted: false, folded: false,
    hidden: false, checked: Boolean(checked), bookmarked: false, createdAt: now, updatedAt: now,
  });
  const cards = [
    card("s_note_a", "text", 60, 60, "把材料按主题分组，先看差异再看结论。", {}, true),
    card("s_note_b", "text", 380, 60, "这一条还没有勾选，QIO 看不到它的文字。", {}, false),
    card("s_code", "code", 60, 300,
      "def summarize(items):\n    # 很长的一行，用来验证换行与横向滚动是否正常：\n    return [{'title': it.title, 'why': it.reason, 'next': it.next_step} for it in items if it.enabled]\n",
      { language: "python" }, false),
    card("s_long", "file", 380, 300,
      "2026 年第三季度跨团队协作材料汇总与后续行动项（含附录与修订记录）",
      { name: "2026Q3-跨团队协作材料汇总与后续行动项-最终修订版-v12.pdf" }, false),
  ];
  const groups = [{
    id: "s_group", name: GROUP_NAME, defaultName: true, ordered: false, deleted: false,
    members: ["s_note_a", "s_code"], createdAt: now, updatedAt: now,
  }];
  const links = [{
    id: "s_link", src: "s_note_a", dst: "s_note_b", direction: false, meaning: "放在一起看",
    deleted: false, createdAt: now, updatedAt: now,
  }];
  await api("/api/interactive/boards/" + BOARD + "/state", {
    method: "PUT",
    body: JSON.stringify({ state: { boardId: BOARD, seq: 1, updatedAt: now, cards, groups, links, selection: [] }, reason: "ui-screens-seed" }),
  });
  return cards.length;
}

/** 造一批待审批意图（走产品接口），用于「单项审批」场景 */
async function seedDemoIntents() {
  const list = await api("/api/interactive/boards/" + BOARD + "/intents");
  for (const item of list.intents || []) {
    if (["pending", "needs_update", "waiting_dependency", "waiting_confirm"].includes(item.status)) {
      await api("/api/interactive/intents/" + item.id + "/reject", { method: "POST", body: "{}" });
    }
  }
  await api("/api/interactive/boards/" + BOARD + "/intents", { method: "POST", body: JSON.stringify({ demo: true }) });
}

const themeStep = (theme) => ({ op: "eval", js: `document.documentElement.setAttribute('data-theme','${theme}'); '${theme}'` });
const shot = (name) => ({ op: "screenshot", name });

/** 选中第一张卡片：合成指针事件（见文件头说明） */
const selectFirstCard = {
  op: "eval",
  js: `(function(){const c=document.querySelector('[data-im="card"]');if(!c)return 'no-card';const r=c.getBoundingClientRect();const opts={bubbles:true,cancelable:true,composed:true,clientX:Math.round(r.left+r.width/2),clientY:Math.round(r.top+18),button:0,buttons:1,pointerId:1,pointerType:'mouse',isPrimary:true};c.dispatchEvent(new PointerEvent('pointerdown',opts));document.dispatchEvent(new PointerEvent('pointerup',Object.assign({},opts,{buttons:0})));return 'selected';})()`,
};
const openChat = { op: "eval", js: `(function(){const p=document.querySelector('[data-im="chat-panel"]');if(p)return 'already';const b=document.querySelector('[data-im="chat-toggle"]');if(b)b.click();return b?'clicked':'missing';})()` };
const openBatch = { op: "eval", js: `(function(){const l=document.querySelector('[data-im="batch-list"]');if(l)return 'already';const b=document.querySelector('[data-im="batch-entry"]');if(b)b.click();return b?'clicked':'missing';})()` };
/** 制造一次保存失败：只拦板面状态写入，其他请求照常 */
const breakSave = {
  op: "eval",
  js: `(function(){if(!window.__origFetch){window.__origFetch=window.fetch.bind(window);}window.fetch=function(input,init){const url=String((input&&input.url)||input);if(/\\/api\\/interactive\\/boards\\/[^/]+\\/state$/.test(url)&&(init&&String(init.method||'').toUpperCase()==='PUT')){return Promise.reject(new TypeError('Failed to fetch'));}return window.__origFetch(input,init);};return 'stubbed';})()`,
};
/** 触发一次真实改动（添加文字注释），让保存失败显现 */
const addCard = { op: "eval", js: `(function(){const m=document.querySelector('[data-im="add-menu"]');if(m)m.click();return m?'opened':'missing';})()` };
const addText = { op: "eval", js: `(function(){const b=document.querySelector('[data-im="add-text"]');if(!b)return 'missing';b.click();return 'clicked';})()` };

async function captureScene(w, h, theme, scene) {
  const tag = LABEL + "-" + w + "x" + h + "-" + theme + "-" + scene;
  const steps = [
    { op: "navigate", url: APP + "/#/interactive", ms: 7000 },
    { op: "viewport", width: w, height: h },
    { op: "wait", ms: 1200 },
    themeStep(theme),
    { op: "wait", ms: 400 },
  ];
  if (scene === "selected") steps.push(selectFirstCard, { op: "wait", ms: 600 });
  if (scene === "approval") steps.push({ op: "wait", ms: 1200 });
  if (scene === "overlays") steps.push(openChat, { op: "wait", ms: 700 }, openBatch, { op: "wait", ms: 900 });
  if (scene === "error") steps.push(breakSave, { op: "wait", ms: 300 }, addCard, { op: "wait", ms: 500 }, addText, { op: "wait", ms: 1800 });
  steps.push(shot(tag));
  runSteps(steps, tag);
  return tag;
}

const main = async () => {
  const count = await seedBoard();
  console.log("已铺固定数据：卡片 " + count + " 张（含长代码与超长名称）、1 个组、1 条关系");
  await seedDemoIntents();
  console.log("已生成一批演示意图（用于单项审批与批量列表场景）");
  let done = 0;
  for (const [w, h] of SIZES) {
    for (const theme of ["dark", "light"]) {
      for (const scene of SCENES) {
        const tag = await captureScene(w, h, theme, scene);
        console.log("已截图 " + tag);
        done += 1;
      }
    }
  }
  let moved = 0;
  for (const [w, h] of SIZES) {
    for (const theme of ["dark", "light"]) {
      for (const scene of SCENES) {
        const name = LABEL + "-" + w + "x" + h + "-" + theme + "-" + scene + ".png";
        const src = join(SHOTS_SRC, name);
        if (existsSync(src)) { copyFileSync(src, join(OUT_DIR, name)); moved += 1; }
      }
    }
  }
  console.log("共 " + done + " 个场景，已复制 " + moved + " 张到 " + OUT_DIR);
};

main().catch((error) => {
  console.error("采集失败：" + (error && error.message ? error.message : error));
  process.exit(1);
});
