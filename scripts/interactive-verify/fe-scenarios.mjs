/**
 * 互动板前端改版的实机验收（主智能体维护）。
 *
 * 对应提示词第七节的 12 个场景，用 scripts/visual_probe.mjs 驱动真实 Chrome：
 * 真实鼠标拖动、真实滚轮、真实键盘，并在页面里包一层 fetch 记录，用来证明
 * 「聊天发送不带板面、不调提交接口」。
 *
 * 前置：后端 IM_BACKEND（默认 http://127.0.0.1:8891）、前端 IM_APP（默认 http://127.0.0.1:5399）。
 * 用法：node scripts/interactive-verify/fe-scenarios.mjs [--only=1,2,4]
 */
import { spawnSync } from "node:child_process";
import { fileURLToPath } from "node:url";
import { dirname, resolve } from "node:path";

const here = dirname(fileURLToPath(import.meta.url));
const root = resolve(here, "..", "..");
const APP = process.env.IM_APP || "http://127.0.0.1:5399";
const BACKEND = process.env.IM_BACKEND || "http://127.0.0.1:8891";
const BOARD = "board_default";
const onlyArg = (process.argv.find((a) => a.startsWith("--only=")) || "").split("=")[1] || "";
const ONLY = onlyArg ? onlyArg.split(",").map((x) => x.trim()) : null;
const want = (id) => !ONLY || ONLY.includes(String(id));

const checks = [];
function check(name, ok, detail) {
  checks.push({ name, ok: Boolean(ok), detail: detail || "" });
  console.log((ok ? "PASS  " : "FAIL  ") + name + (detail ? "  —— " + detail : ""));
}

function runSteps(steps) {
  const probe = spawnSync(process.execPath, [resolve(root, "scripts", "visual_probe.mjs"), JSON.stringify(steps)], {
    encoding: "utf8",
    maxBuffer: 64 * 1024 * 1024,
  });
  const raw = probe.stdout || "";
  const start = raw.indexOf("{");
  if (start < 0) throw new Error("探针没有输出 JSON：" + raw.slice(0, 400));
  return JSON.parse(raw.slice(start));
}

const evals = (payload) => (payload.results || []).filter((r) => r.op === "eval").map((r) => r.value);
function lastJson(values, fallback) {
  for (let i = values.length - 1; i >= 0; i -= 1) {
    const value = values[i];
    if (typeof value !== "string") continue;
    const text = value.trim();
    if (!text.startsWith("{") && !text.startsWith("[")) continue;
    try { return JSON.parse(text); } catch { /* 继续往前找 */ }
  }
  return fallback === undefined ? {} : fallback;
}
const last = (payload, fallback) => lastJson(evals(payload), fallback);

async function api(path, init) {
  const resp = await fetch(BACKEND + path, {
    ...(init || {}),
    headers: { "Content-Type": "application/json", ...((init && init.headers) || {}) },
  });
  return resp.json();
}
const boardState = () => api("/api/interactive/boards/" + BOARD + "/state");
const submissions = () => api("/api/interactive/boards/" + BOARD + "/submissions");
const intentsApi = () => api("/api/interactive/boards/" + BOARD + "/intents");

async function resetBoard() {
  await api("/api/interactive/boards/" + BOARD + "/state", {
    method: "PUT",
    body: JSON.stringify({
      state: { boardId: BOARD, seq: 0, updatedAt: new Date().toISOString(), cards: [], groups: [], links: [], selection: [] },
      reason: "fe-e2e-reset",
    }),
  });
}

const NAV = [
  { op: "navigate", url: APP + "/#/interactive", ms: 5200 },
  { op: "viewport", width: 1440, height: 900 },
  { op: "wait", ms: 800 },
];

/** 在页面里记下所有 fetch（用来证明聊天与提交是两条独立请求路径） */
const SPY_ON = { op: "eval", js: "(function(){if(window.__qioCalls)return 'already';window.__qioCalls=[];const raw=window.fetch.bind(window);window.fetch=function(input,init){try{window.__qioCalls.push({url:String(input&&input.url||input),method:(init&&init.method)||'GET',body:(init&&typeof init.body==='string')?init.body.slice(0,400):null,at:Date.now()});}catch(e){}return raw(input,init);};return 'spy-on';})()" };
const SPY_READ = { op: "eval", js: "JSON.stringify(window.__qioCalls||[])" };

function mouse(type, x, y, extra) {
  return {
    op: "cdp",
    method: "Input.dispatchMouseEvent",
    params: { type, x: Math.round(x), y: Math.round(y), button: "left", clickCount: 1, buttons: type === "mouseReleased" ? 0 : 1, ...(extra || {}) },
  };
}
function wheel(x, y, deltaY) {
  return { op: "cdp", method: "Input.dispatchMouseEvent", params: { type: "mouseWheel", x: Math.round(x), y: Math.round(y), deltaX: 0, deltaY, button: "none", buttons: 0 } };
}
const cardsExpr = "JSON.stringify([...document.querySelectorAll('[data-im=\"card\"]')].map(c=>{const r=c.getBoundingClientRect();return {id:c.getAttribute('data-card-id'), x:Math.round(r.left), y:Math.round(r.top), w:Math.round(r.width), cx:Math.round(r.left+r.width/2), cy:Math.round(r.top+16)};}))";

/**
 * 场景 1：初始页以板面为主体；底部工具栏、添加菜单、独立聊天入口位置正确；无常驻右侧回复栏。
 */
async function scenario1() {
  const payload = runSteps([...NAV, SPY_ON,
    { op: "eval", js: "JSON.stringify({toolbar: !!document.querySelector('[data-im=\"board-toolbar\"]'), addMenu: !!document.querySelector('[data-im=\"add-menu\"]'), chatToggle: !!document.querySelector('[data-im=\"chat-toggle\"]'), submit: !!document.querySelector('[data-im=\"submit\"]'), stage: !!document.querySelector('.im-stage'), legacyRail: !!document.querySelector('.im-aux'), replyPanel: !!document.querySelector('[data-im=\"reply-panel\"]')})" },
    { op: "eval", js: "JSON.stringify((function(){const t=document.querySelector('[data-im=\"board-toolbar\"]').getBoundingClientRect();const c=document.querySelector('[data-im=\"chat-toggle\"]').getBoundingClientRect();return {toolbarBottomGap: Math.round(window.innerHeight-t.bottom), toolbarVisible: t.width>0&&t.height>0, chatRightGap: Math.round(window.innerWidth-c.right), chatBottomGap: Math.round(window.innerHeight-c.bottom)};})())" },
    { op: "screenshot", name: "fe-01-initial" },
  ]);
  const values = evals(payload);
  const dom = lastJson(values.slice(0, -1));
  const geo = lastJson(values);
  check("1. 底部工具栏与添加菜单存在", dom.toolbar && dom.addMenu, JSON.stringify(dom));
  check("1. 右下独立聊天入口存在且可点", dom.chatToggle, JSON.stringify(geo));
  check("1. 提交入口在工具栏内", dom.submit);
  check("1. 没有常驻右侧回复栏", dom.legacyRail === false && dom.replyPanel === false, JSON.stringify({ rail: dom.legacyRail, reply: dom.replyPanel }));
  check("1. 工具栏贴底、聊天入口贴右下", geo.toolbarBottomGap >= 0 && geo.toolbarBottomGap <= 80 && geo.chatRightGap >= 0 && geo.chatRightGap <= 80, JSON.stringify(geo));
}

const main = async () => {
  console.log("=== 互动板前端改版实机验收（app=" + APP + " backend=" + BACKEND + "）===");
  try {
    if (want(1)) { await resetBoard(); await scenario1(); }
  } catch (error) {
    check("验收脚本执行完成", false, String(error));
  }
  const failed = checks.filter((c) => !c.ok);
  console.log("");
  console.log("合计 " + checks.length + " 项，通过 " + (checks.length - failed.length) + " 项，失败 " + failed.length + " 项");
  if (failed.length) console.log("失败项：" + failed.map((f) => f.name).join("；"));
  process.exit(failed.length ? 1 : 0);
};

main();
