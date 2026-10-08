/**
 * 本轮（草稿竞态与界面收敛）的改前／改后对照截图采集（主智能体维护）。
 *
 * 提示词要求至少覆盖四个场景：工具栏常态、聊天有消息且后方板面文字密集、
 * 聊天与批量列表同时展开、保存失败及恢复入口；三档 1440×900 / 1024×768 / 800×600 × 暗/亮。
 *
 * 用法：
 *   node scripts/interactive-verify/ui-screens-races.mjs --label=before --app=http://127.0.0.1:5421 --backend=http://127.0.0.1:8921
 *   node scripts/interactive-verify/ui-screens-races.mjs --label=after  --app=http://127.0.0.1:5299 --backend=http://127.0.0.1:8791
 *
 * 说明（诚实标注）：场景布置用接口铺数据 + 页面内合成事件；交互正确性由各子智能体的探针与
 * `fe-scenarios.mjs` 的真实输入覆盖，不靠这里的截图。驱动用 Edge（本机 Chrome 会卡死）。
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
const SHOTS_SRC = join(process.env.TEMP || "", "qio-visual", "shots");
const OUT_DIR = join(root, "docs", "interactive-ui-screenshots");
mkdirSync(OUT_DIR, { recursive: true });
const SIZES = [[1440, 900], [1024, 768], [800, 600]];
const SCENES = ["toolbar", "chat-dense", "overlays", "save-failure"];

/**
 * 驱动：默认用仓库的 Chrome 探针（`scripts/visual_probe.mjs`），
 * 需要时用 `QIO_PROBE=edge` 切到 Edge 探针（`visual_probe_d3.mjs`）。
 * 同一批对照图必须用同一个驱动，否则几何口径不可比。
 */
const PROBE_SCRIPT = process.env.QIO_PROBE === "edge" ? "visual_probe_d3.mjs" : "visual_probe.mjs";

function runSteps(steps, tag) {
  const file = join(process.env.TEMP || ".", "ui-races-" + LABEL + "-" + tag + ".json");
  writeFileSync(file, JSON.stringify(steps, null, 2), "utf8");
  const probe = spawnSync(process.execPath, [join(root, "scripts", PROBE_SCRIPT), "@" + file], {
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

/** 铺一份**文字密集**的板面：长注释、长代码、网址、超长名称，用来检验聊天文字是否被后方文字干扰 */
async function seedDenseBoard() {
  await api("/api/interactive/boards/" + BOARD + "/state", {
    method: "PUT",
    body: JSON.stringify({ state: { boardId: BOARD, seq: 0, updatedAt: new Date().toISOString(), cards: [], groups: [], links: [], selection: [] }, reason: "races-reset" }),
  });
  const now = new Date().toISOString();
  const card = (id, kind, x, y, content, meta, checked) => ({
    id, kind, x, y, w: 320, h: 200, content, meta: meta || {}, deleted: false, folded: false,
    hidden: false, checked: Boolean(checked), bookmarked: false, createdAt: now, updatedAt: now,
  });
  const long = "把材料按主题分组之后，先比较差异，再决定哪些结论可以合并；这一段刻意写得很长，用来制造密集的文字背景，检验聊天面板里的消息是否仍然清楚可读。";
  const cards = [
    card("d_note_a", "text", 40, 40, long, {}, true),
    card("d_note_b", "text", 40, 260, long + long, {}, false),
    card("d_code", "code", 380, 40,
      "def summarize(items):\n    # 很长的一行注释，用来验证换行与横向滚动是否正常：\n    return [{'title': it.title, 'why': it.reason, 'next': it.next_step, 'owner': it.owner} for it in items if it.enabled]\n",
      { language: "python" }, false),
    card("d_url", "url", 380, 260, "https://example.invalid/2026/q3-cross-team-collaboration-material-summary-and-next-actions",
      { href: "https://example.invalid/a-very-long-url-that-should-wrap", title: "跨团队协作材料汇总" }, false),
    card("d_long", "file", 720, 40, "2026 年第三季度跨团队协作材料汇总与后续行动项（含附录与修订记录）",
      { name: "2026Q3-跨团队协作材料汇总与后续行动项-最终修订版-v12.pdf" }, false),
  ];
  await api("/api/interactive/boards/" + BOARD + "/state", {
    method: "PUT",
    body: JSON.stringify({ state: { boardId: BOARD, seq: 1, updatedAt: now, cards, groups: [], links: [], selection: [] }, reason: "races-seed" }),
  });
  const list = await api("/api/interactive/boards/" + BOARD + "/intents");
  for (const item of list.intents || []) {
    if (["pending", "needs_update", "waiting_dependency", "waiting_confirm"].includes(item.status)) {
      await api("/api/interactive/intents/" + item.id + "/reject", { method: "POST", body: "{}" });
    }
  }
  await api("/api/interactive/boards/" + BOARD + "/intents", { method: "POST", body: JSON.stringify({ demo: true }) });
  return cards.length;
}

const themeStep = (theme) => ({ op: "eval", js: `document.documentElement.setAttribute('data-theme','${theme}'); '${theme}'` });
const shot = (name) => ({ op: "screenshot", name });
const openChat = { op: "eval", js: `(function(){const p=document.querySelector('[data-im="chat-panel"]');if(p)return 'already';const b=document.querySelector('[data-im="chat-toggle"]');if(b)b.click();return b?'clicked':'missing';})()` };
const openBatch = { op: "eval", js: `(function(){const l=document.querySelector('[data-im="batch-list"]');if(l)return 'already';const b=document.querySelector('[data-im="batch-entry"]');if(b)b.click();return b?'clicked':'missing';})()` };
const waitNoOverlap = {
  op: "eval",
  await: true,
  js: `(async () => {
    const rect = (sel) => { const el = document.querySelector(sel); if (!el) return null; const r = el.getBoundingClientRect(); return { l: r.left, t: r.top, r: r.right, b: r.bottom }; };
    const t0 = Date.now();
    for (;;) {
      const a = rect('[data-im="chat-panel"]');
      const b = rect('[data-im="batch-list"]');
      if (a && b) {
        const w = Math.max(0, Math.min(a.r, b.r) - Math.max(a.l, b.l));
        const h = Math.max(0, Math.min(a.b, b.b) - Math.max(a.t, b.t));
        if (w * h === 0) return JSON.stringify({ ok: true, ms: Date.now() - t0 });
      }
      if (Date.now() - t0 > 8000) return JSON.stringify({ ok: false, ms: Date.now() - t0 });
      await new Promise((r) => setTimeout(r, 150));
    }
  })()`,
};
/**
 * 在聊天里发一条真实消息。
 *
 * 必须**分两步**：同一个 tick 里设完 value 立刻点发送，按钮还是上一次渲染的禁用态（Vue 还没重绘），
 * 点击会被丢掉 —— 实测那样截出来是「输入框里有字、消息流空着」，不能当作「有消息」的证据。
 */
const typeChatMessage = (text) => ({
  op: "eval",
  js: `(function(){const t=document.querySelector('[data-im="chat-input"] textarea, textarea[data-im="chat-input"], [data-im="chat-input"]');if(!t)return 'no-input';t.focus();t.value=${JSON.stringify(text)};t.dispatchEvent(new Event('input',{bubbles:true}));return 'typed';})()`,
});
const clickChatSend = {
  op: "eval",
  js: `(function(){const b=document.querySelector('[data-im="chat-send"]');if(!b)return 'no-send';if(b.disabled)return 'disabled';b.click();return 'sent';})()`,
};
const sendChatMessage = (text) => [typeChatMessage(text), { op: "wait", ms: 400 }, clickChatSend, { op: "wait", ms: 2400 }];
/** 只拦「发送」这一条请求：失败是模拟的，但应用自己的失败处理是真的 */
const breakSend = {
  op: "eval",
  js: `(function(){if(!window.__origFetch){window.__origFetch=window.fetch.bind(window);}window.fetch=function(input,init){const url=String((input&&input.url)||input);if(/\\/api\\/turns$/.test(url)){return Promise.reject(new TypeError('Failed to fetch'));}return window.__origFetch(input,init);};return 'stubbed-send';})()`,
};
/** 制造保存失败（只拦板面状态写入），并触发一次真实改动，让失败与恢复入口显现 */
const breakSave = {
  op: "eval",
  js: `(function(){if(!window.__origFetch){window.__origFetch=window.fetch.bind(window);}window.fetch=function(input,init){const url=String((input&&input.url)||input);if(/\\/api\\/interactive\\/boards\\/[^/]+\\/state$/.test(url)&&(init&&String(init.method||'').toUpperCase()==='PUT')){return Promise.reject(new TypeError('Failed to fetch'));}return window.__origFetch(input,init);};return 'stubbed';})()`,
};

async function captureScene(w, h, theme, scene) {
  const tag = "p4-" + LABEL + "-" + w + "x" + h + "-" + theme + "-" + scene;
  const steps = [
    { op: "navigate", url: APP + "/#/interactive", ms: 7000 },
    { op: "viewport", width: w, height: h },
    { op: "wait", ms: 1300 },
    themeStep(theme),
    { op: "wait", ms: 400 },
  ];
  if (scene === "chat-dense") {
    steps.push(openChat, { op: "wait", ms: 700 }, ...sendChatMessage("这一条消息用来检验：聊天文字压在密集板面文字上还清楚吗？"));
  }
  if (scene === "overlays") {
    steps.push(openChat, { op: "wait", ms: 700 }, openBatch, waitNoOverlap, { op: "wait", ms: 300 });
  }
  if (scene === "save-failure") {
    steps.push(
      openChat,
      { op: "wait", ms: 600 },
      breakSend,
      { op: "wait", ms: 300 },
      ...sendChatMessage("这一条会失败：失败之后原文要能取回（恢复入口）"),
    );
  }
  steps.push(shot(tag));
  runSteps(steps, tag);
  return tag;
}

const main = async () => {
  const count = await seedDenseBoard();
  console.log("已铺文字密集板面：卡片 " + count + " 张（长注释 / 长代码 / 长网址 / 超长名称）+ 一批演示意图");
  let done = 0;
  for (const [w, h] of SIZES) {
    for (const theme of ["dark", "light"]) {
      for (const scene of SCENES) {
        await captureScene(w, h, theme, scene);
        done += 1;
      }
    }
  }
  let moved = 0;
  for (const [w, h] of SIZES) {
    for (const theme of ["dark", "light"]) {
      for (const scene of SCENES) {
        const name = "p4-" + LABEL + "-" + w + "x" + h + "-" + theme + "-" + scene + ".png";
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
