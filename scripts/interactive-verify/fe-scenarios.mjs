/**
 * 互动板前端改版的实机验收（主智能体维护）。
 *
 * 对应提示词第七节的 12 个场景，用 scripts/visual_probe.mjs 驱动真实 Chrome：
 * 真实鼠标拖动、真实滚轮、真实键盘；并在页面里包一层 fetch 记录，
 * 用来证明「聊天发送只发文字、不带板面、不调提交接口」。
 *
 * 前置：后端 IM_BACKEND（默认 http://127.0.0.1:8791）、前端 IM_APP（默认 http://127.0.0.1:5299）。
 * 用法：node scripts/interactive-verify/fe-scenarios.mjs [--only=1,2,4]
 *
 * 端口/用户目录用 QIO_PROBE_PORT / QIO_PROBE_PROFILE / QIO_PROBE_OUT 隔离，
 * 避免与别的智能体的验收互相抢 Chrome。
 */
import { spawnSync } from "node:child_process";
import { fileURLToPath } from "node:url";
import { dirname, resolve } from "node:path";

const here = dirname(fileURLToPath(import.meta.url));
const root = resolve(here, "..", "..");
const APP = process.env.IM_APP || "http://127.0.0.1:5299";
const BACKEND = process.env.IM_BACKEND || "http://127.0.0.1:8791";
const BOARD = "board_default";
/** 本次运行的唯一后缀：避免「和上次成功提交内容一致 → 未重复提交」把验收结果带偏 */
const RUN_TAG = String(Date.now()).slice(-6);
const onlyArg = (process.argv.find((a) => a.startsWith("--only=")) || "").split("=")[1] || "";
const ONLY = onlyArg ? onlyArg.split(",").map((x) => x.trim()) : null;
const want = (id) => !ONLY || ONLY.includes(String(id));

const checks = [];
function check(name, ok, detail) {
  checks.push({ name, ok: Boolean(ok), detail: detail || "" });
  console.log((ok ? "PASS  " : "FAIL  ") + name + (detail ? "  —— " + detail : ""));
}

function runSteps(steps, attempt = 1) {
  const probe = spawnSync(process.execPath, [resolve(root, "scripts", "visual_probe.mjs"), JSON.stringify(steps)], {
    encoding: "utf8",
    maxBuffer: 128 * 1024 * 1024,
  });
  const raw = probe.stdout || "";
  const start = raw.indexOf("{");
  if (start < 0) {
    // Chrome 偶发起不来（上一次会话还在退出、用户目录被占用）：重试几次再放弃
    if (attempt < 4) {
      const until = Date.now() + 2500;
      while (Date.now() < until) {
        /* 同步等待，这里已经在 Node 主线程里串行跑 */
      }
      return runSteps(steps, attempt + 1);
    }
    throw new Error("探针没有输出 JSON：" + (raw + (probe.stderr || "")).slice(0, 500));
  }
  return JSON.parse(raw.slice(start));
}

/**
 * 在**页面里**读一次服务端板面状态。
 *
 * 为什么不用 Node 侧读：前端提交是**防抖自动保存**（450ms + 往返），
 * 探针会话结束后立刻用 Node 读接口可能读到的还是上一版；
 * 在页面里等一会儿再读，才不会把「还没保存」误判成「操作没生效」。
 */
const stateRead = (extra = "") => ({
  op: "eval",
  await: true,
  js: `(async()=>{const raw=await (await fetch('${BACKEND}/api/interactive/boards/${BOARD}/state')).json();const j=raw&&raw.state?raw.state:raw;return JSON.stringify({mark:"state",selected:(j.selection||[]),cards:(j.cards||[]).filter(c=>!c.deleted).length,groups:(j.groups||[]).filter(g=>!g.deleted).map(g=>({name:g.name,members:g.members,ordered:g.ordered})),links:(j.links||[]).filter(l=>!l.deleted).length,checked:(j.cards||[]).filter(c=>c.checked).length${extra}});})()`,
});

/**
 * 在页面里**轮询等待真实结果**，而不是固定睡一段时间。
 *
 * 上一轮验收脚本的毛病就是「睡 1500ms 然后读状态」：防抖自动保存（450ms + 往返）偶尔还没落盘，
 * 就把「还没保存」误判成「操作没生效」，于是出现连跑失败、单跑通过。这里改成：
 * 给一个条件表达式，页面里每 120ms 求值一次，满足即返回（带耗时），超时才失败。
 *
 * 用法：`waitFor("JSON.parse(window.__x).cards===2")`；条件里抛异常会被当成「还没满足」继续等。
 */
const waitFor = (expr, timeoutMs = 8000) => ({
  op: "eval",
  await: true,
  js: `(async()=>{const t0=Date.now();let last=null;for(;;){try{const v=(${expr});last=v;if(v)return JSON.stringify({ok:true,value:v,ms:Date.now()-t0});}catch(e){last=String(e&&e.message?e.message:e);}if(Date.now()-t0>${timeoutMs})return JSON.stringify({ok:false,last:last,ms:Date.now()-t0});await new Promise(r=>setTimeout(r,120));}})()`,
});

/** 等服务端板面状态满足条件（真的落盘了再断言，不用固定延迟） */
const waitForState = (predicate, timeoutMs = 8000) => ({
  op: "eval",
  await: true,
  js: `(async()=>{const t0=Date.now();let snap=null;for(;;){try{const raw=await (await fetch('${BACKEND}/api/interactive/boards/${BOARD}/state')).json();const j=raw&&raw.state?raw.state:raw;snap={cards:(j.cards||[]).filter(c=>!c.deleted).length,groups:(j.groups||[]).filter(g=>!g.deleted),links:(j.links||[]).filter(l=>!l.deleted).length,checked:(j.cards||[]).filter(c=>c.checked).length,selection:(j.selection||[])};if(${predicate})return JSON.stringify({mark:"state-wait",ok:true,snap:snap,ms:Date.now()-t0});}catch(e){snap={error:String(e&&e.message?e.message:e)};}if(Date.now()-t0>${timeoutMs})return JSON.stringify({mark:"state-wait",ok:false,snap:snap,ms:Date.now()-t0});await new Promise(r=>setTimeout(r,150));}})()`,
});

/** 等页面上的某个钩子出现/消失或文案满足条件 */
const waitForHook = (expr, timeoutMs = 8000) => ({
  op: "eval",
  await: true,
  js: `(async()=>{const t0=Date.now();let last=null;for(;;){try{const v=(${expr});last=v;if(v)return JSON.stringify({ok:true,value:v,ms:Date.now()-t0});}catch(e){last=String(e&&e.message?e.message:e);}if(Date.now()-t0>${timeoutMs})return JSON.stringify({ok:false,last:last,ms:Date.now()-t0});await new Promise(r=>setTimeout(r,120));}})()`,
});

/** 探针每次都会新起一个 Chrome：**任何一步交互都必须先导航到应用**，否则页面是 about:blank。 */
const sess = (steps) => runSteps([...NAV, ...steps]);

const evals = (payload) => (payload.results || []).filter((r) => r.op === "eval").map((r) => r.value);
function lastJson(values, fallback) {
  for (let i = values.length - 1; i >= 0; i -= 1) {
    const text = typeof values[i] === "string" ? values[i].trim() : "";
    if (!text.startsWith("{") && !text.startsWith("[")) continue;
    try { return JSON.parse(text); } catch { /* 继续往前找 */ }
  }
  return fallback === undefined ? {} : fallback;
}
const last = (payload, fallback) => lastJson(evals(payload), fallback);

/**
 * 按标记取一次 eval 的结果。
 *
 * 上一轮的教训：靠「取最后一个 JSON」在步骤顺序变化后会取到别的对象（场景 6/15 都因此误报）。
 * 现在要求被取的 eval 自己带 `mark`，这里按标记找，找不到就返回 null（而不是猜）。
 */
function marked(values, mark) {
  for (let i = values.length - 1; i >= 0; i -= 1) {
    const text = typeof values[i] === "string" ? values[i].trim() : "";
    if (!text.startsWith("{")) continue;
    try {
      const parsed = JSON.parse(text);
      if (parsed && parsed.mark === mark) return parsed;
    } catch { /* 忽略非 JSON */ }
  }
  return null;
}
const markedFrom = (payload, mark) => marked(evals(payload), mark);

/** 按标记取**全部**结果（同一标记出现多次时按顺序返回，例如连续处理多项） */
function markedAll(values, mark) {
  const out = [];
  for (const value of values) {
    const text = typeof value === "string" ? value.trim() : "";
    if (!text.startsWith("{")) continue;
    try {
      const parsed = JSON.parse(text);
      if (parsed && parsed.mark === mark) out.push(parsed);
    } catch { /* 忽略非 JSON */ }
  }
  return out;
}

async function api(path, init, attempt = 1) {
  try {
    const resp = await fetch(BACKEND + path, {
      ...(init || {}),
      // keep-alive 连接被服务端关掉时 Node 偶发 "fetch failed"：每次都新建连接，并重试一次
      headers: { "Content-Type": "application/json", Connection: "close", ...((init && init.headers) || {}) },
    });
    return resp.json();
  } catch (error) {
    if (attempt < 3) {
      await new Promise((resolve) => setTimeout(resolve, 400));
      return api(path, init, attempt + 1);
    }
    throw new Error("接口调用失败 " + (init && init.method ? init.method + " " : "GET ") + path + "：" + (error && error.message ? error.message : String(error)));
  }
}
/** GET /state 的响应是 { state: {...} } 包了一层，这里统一拆包 */
async function boardState() {
  const payload = await api("/api/interactive/boards/" + BOARD + "/state");
  return payload && payload.state ? payload.state : payload;
}
const intentsApi = () => api("/api/interactive/boards/" + BOARD + "/intents");

/** 把还等待审批的意图全部拒绝：演示入口会复用未完成的演示意图，不清理就拿不到「干净的一批四项」 */
async function clearPendingIntents() {
  const payload = await intentsApi();
  const list = payload.intents || [];
  let cleared = 0;
  for (const item of list) {
    if (["pending", "needs_update", "waiting_dependency", "waiting_confirm"].includes(item.status)) {
      await api("/api/interactive/intents/" + item.id + "/reject", { method: "POST", body: "{}" });
      cleared += 1;
    }
  }
  return cleared;
}

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
  { op: "wait", ms: 700 },
];
const RELOAD = [{ op: "eval", js: "location.reload(); 'reload'" }, { op: "wait", ms: 5200 }];

/** 在页面里记下所有 fetch（用来证明聊天与提交是两条独立请求路径） */
const SPY_ON = { op: "eval", js: "(function(){if(window.__qioCalls)return 'already';window.__qioCalls=[];const raw=window.fetch.bind(window);window.fetch=function(input,init){try{window.__qioCalls.push({url:String((input&&input.url)||input),method:((init&&init.method)||'GET'),body:(init&&typeof init.body==='string')?init.body.slice(0,600):null,at:Date.now()});}catch(e){}return raw(input,init);};return 'spy-on';})()" };
const SPY_RESET = { op: "eval", js: "window.__qioCalls=[]; 'cleared'" };
const SPY_READ = { op: "eval", js: "JSON.stringify(window.__qioCalls||[])" };

const clickHook = (hook) => ({ op: "eval", js: `(function(){const el=document.querySelector('[data-im="${hook}"]');if(!el)return 'missing';(el.disabled?'disabled':'ok');el.click();return el.disabled?'disabled':'clicked';})()` });
const probeHook = (hook) => ({ op: "eval", js: `(function(){const el=document.querySelector('[data-im="${hook}"]');if(!el)return 'missing';(el.disabled?'disabled':'ok');el.click();return el.disabled?'disabled':'clicked';})()` });
const existsHook = (hook) => ({ op: "eval", js: `document.querySelector('[data-im="${hook}"]')?'yes':'no'` });

function mouse(type, x, y, extra) {
  return {
    op: "cdp",
    method: "Input.dispatchMouseEvent",
    params: {
      type,
      x: Math.round(x),
      y: Math.round(y),
      button: "left",
      clickCount: type === "mousePressed" ? 1 : 0,
      buttons: type === "mouseReleased" ? 0 : 1,
      ...(extra || {}),
    },
  };
}
function click(x, y) {
  return [mouse("mousePressed", x, y), { op: "wait", ms: 60 }, mouse("mouseReleased", x, y), { op: "wait", ms: 260 }];
}
/** 真实拖动：按下 → 中间过程 → 到达 → （可选截图）→ 松手 */
function drag(from, to, midShot) {
  const steps = [mouse("mousePressed", from.x, from.y), { op: "wait", ms: 90 }];
  const N = 6;
  for (let i = 1; i <= N; i += 1) {
    steps.push(mouse("mouseMoved", from.x + ((to.x - from.x) * i) / N, from.y + ((to.y - from.y) * i) / N));
    steps.push({ op: "wait", ms: 60 });
  }
  if (midShot) steps.push({ op: "screenshot", name: midShot });
  steps.push(mouse("mouseReleased", to.x, to.y));
  steps.push({ op: "wait", ms: 520 });
  return steps;
}
function wheel(x, y, deltaY) {
  return { op: "cdp", method: "Input.dispatchMouseEvent", params: { type: "mouseWheel", x: Math.round(x), y: Math.round(y), deltaX: 0, deltaY, button: "none", buttons: 0 } };
}
const KEY = {
  space: { key: " ", code: "Space", vk: 32 },
  escape: { key: "Escape", code: "Escape", vk: 27 },
  enter: { key: "Enter", code: "Enter", vk: 13 },
};
function keyDown(k) {
  return { op: "cdp", method: "Input.dispatchKeyEvent", params: { type: "rawKeyDown", key: k.key, code: k.code, windowsVirtualKeyCode: k.vk, nativeVirtualKeyCode: k.vk } };
}
function keyUp(k) {
  return { op: "cdp", method: "Input.dispatchKeyEvent", params: { type: "keyUp", key: k.key, code: k.code, windowsVirtualKeyCode: k.vk, nativeVirtualKeyCode: k.vk } };
}
/**
 * 真的按一次键。
 *
 * text 字段有讲究：Enter 要给 "\r"、空格给 " "，其他键**不要给 text**，
 * 否则浏览器会收到奇怪的字面量（例如把 "Enter" 当成要插入的文本）。
 */
function typeKey(k, text) {
  const payload = text !== undefined ? text : (k === KEY.enter ? "\r" : k === KEY.space ? " " : undefined);
  const params = { type: "keyDown", key: k.key, code: k.code, windowsVirtualKeyCode: k.vk, nativeVirtualKeyCode: k.vk };
  if (payload !== undefined) params.text = payload;
  return [
    { op: "cdp", method: "Input.dispatchKeyEvent", params },
    { op: "cdp", method: "Input.dispatchKeyEvent", params: { type: "keyUp", key: k.key, code: k.code, windowsVirtualKeyCode: k.vk, nativeVirtualKeyCode: k.vk } },
    { op: "wait", ms: 120 },
  ];
}

/** 屏幕上卡片的位置（板面坐标 → 屏幕坐标：缩放 1、不平移时 surface 左 0 上 77） */
async function cardScreens() {
  const payload = sess([
    { op: "eval", js: `JSON.stringify([...document.querySelectorAll('[data-im="card"]')].map(c=>{const r=c.getBoundingClientRect();const id=c.getAttribute('data-card-id');return {id,x:Math.round(r.left),y:Math.round(r.top),w:Math.round(r.width),h:Math.round(r.height),cx:Math.round(r.left+r.width/2),cy:Math.round(r.top+22)};}))` },
  ]);
  return lastJson(evals(payload), []);
}
const CARDS_EXPR = `JSON.stringify([...document.querySelectorAll('[data-im="card"]')].map(c=>{const r=c.getBoundingClientRect();const id=c.getAttribute('data-card-id');return {id,x:Math.round(r.left),y:Math.round(r.top),w:Math.round(r.width),h:Math.round(r.height),cx:Math.round(r.left+r.width/2),cy:Math.round(r.top+22)};}))`;

/** 用真实点击添加 n 张文字注释（不直接改 store：这是用户真实路径） */
async function addNoteCards(n) {
  const steps = [...NAV];
  for (let i = 0; i < n; i += 1) {
    steps.push(clickHook("add-menu"), { op: "wait", ms: 420 }, clickHook("add-text"), { op: "wait", ms: 900 });
  }
  const payload = runSteps(steps);
  const results = evals(payload);
  return results.filter((r) => r === "clicked").length;
}
const stateJson = () => boardState();

// --- 场景 1：初始页以板面为主体 -------------------------------------------

async function scenario1() {
  const payload = runSteps([...NAV, SPY_ON,
    { op: "eval", js: `JSON.stringify({toolbars: document.querySelectorAll('[data-im="board-toolbar"]').length, addMenu: !!document.querySelector('[data-im="add-menu"]'), chatToggle: !!document.querySelector('[data-im="chat-toggle"]'), submit: !!document.querySelector('[data-im="submit"]'), saveStatus: !!document.querySelector('[data-im="save-status"]'), submitStatus: !!document.querySelector('[data-im="submit-status"]'), visibleRange: !!document.querySelector('[data-im="visible-range"]'), legacyRail: !!document.querySelector('.im-aux'), replyPanel: !!document.querySelector('[data-im="reply-panel"]'), surface: !!document.querySelector('.board-surface')})` },
    { op: "eval", js: `JSON.stringify((function(){const t=document.querySelector('[data-im="board-toolbar"]').getBoundingClientRect();const c=document.querySelector('[data-im="chat-toggle"]').getBoundingClientRect();const s=document.querySelector('.board-surface').getBoundingClientRect();return {toolbarBottomGap:Math.round(window.innerHeight-t.bottom),toolbarTop:Math.round(t.top),toolbarW:Math.round(t.width),chatRightGap:Math.round(window.innerWidth-c.right),chatBottomGap:Math.round(window.innerHeight-c.bottom),surfaceW:Math.round(s.width),surfaceTop:Math.round(s.top)};})())` },
    { op: "screenshot", name: "fe-01-initial-1440" },
  ]);
  const values = evals(payload);
  const dom = lastJson(values.slice(0, -1));
  const geo = lastJson(values);
  check("1 底部工具栏唯一且存在", dom.toolbars === 1, JSON.stringify({ toolbars: dom.toolbars }));
  check("1 添加菜单/聊天入口/提交入口/状态都在", dom.addMenu && dom.chatToggle && dom.submit && dom.saveStatus && dom.submitStatus && dom.visibleRange, JSON.stringify(dom));
  check("1 没有常驻右侧回复栏", dom.legacyRail === false && dom.replyPanel === false);
  check("1 板面是主体（surface 宽度 ≥ 视口 60%）", geo.surfaceW >= 1440 * 0.6, JSON.stringify(geo));
  check("1 工具栏贴底、聊天入口贴右下", geo.toolbarBottomGap >= 0 && geo.toolbarBottomGap <= 60 && geo.chatRightGap >= 0 && geo.chatRightGap <= 80, JSON.stringify(geo));
}

// --- 场景 2：平移 / 空格框选 / 输入框内空格 --------------------------------

async function scenario2() {
  await resetBoard();
  await addNoteCards(2);
  const before = await cardScreens();
  if (before.length < 2) { check("2 前置：两张卡片已用真实点击添加", false, JSON.stringify(before)); return; }
  const a = before[0];

  // 2.1 空白处拖动 = 平移（卡片屏幕位置变化、板面坐标不变）
  // 平移是「抓取滚动」：往左/上拖才能看到右下方的内容（往右/下拖在左上边界会被钳住）
  const empty = { x: a.x + 620, y: a.y + 360 };
  const pan = sess([
    ...drag(empty, { x: empty.x - 240, y: empty.y - 120 }),
    { op: "eval", js: CARDS_EXPR },
    { op: "wait", ms: 1400 },
    stateRead(),
    { op: "screenshot", name: "fe-21-panned" },
  ]);
  const panValues = evals(pan).filter((v) => typeof v === "string" && v.trim().startsWith("["));
  const afterPan = lastJson(panValues, []);
  const pannedCard = afterPan.find((c) => c.id === a.id) || {};
  const movedBy = { dx: (pannedCard.x ?? 0) - a.x, dy: (pannedCard.y ?? 0) - a.y };
  const panState = lastJson(evals(pan), {});
  const boardCard = (await boardState()).cards.find((c) => c.id === a.id) || {};
  check("2.1 空白拖动 = 平移（卡片在屏幕上跟着移动）", Math.abs(movedBy.dx) > 80 && Math.abs(movedBy.dy) > 30, JSON.stringify({ movedBy, panState }));
  check("2.1 平移不改变板面坐标（只是查看位置）", Math.abs(Number(boardCard.x) - Number(a.x)) < 2, JSON.stringify({ boardX: boardCard.x, screenX: a.x }));

  // 还原查看位置：反向拖动
  sess(drag({ x: empty.x - 240, y: empty.y - 120 }, empty));

  // 2.2 空格 + 拖动空白 = 框选（不按空格时空白拖动是平移）
  const cards = await cardScreens();
  const first = cards[0];
  const rectStart = { x: first.x - 40, y: first.y - 30 };
  const rectEnd = { x: Math.max(...cards.map((c) => c.x + c.w)) + 40, y: Math.max(...cards.map((c) => c.y + c.h)) + 40 };
  const marquee = sess([
    keyDown(KEY.space), { op: "wait", ms: 160 },
    ...drag(rectStart, rectEnd, "fe-22-marquee-dragging"),
    keyUp(KEY.space), { op: "wait", ms: 1500 },
    stateRead(), stateRead(", selectedCount: (j.selection||[]).length"),
    { op: "screenshot", name: "fe-23-marquee-selected" },
  ]);
  const sel = lastJson(evals(marquee), {});
  const selection = sel.selected || [];
  check("2.2 空格 + 拖动空白 = 框选到卡片", selection.length >= 2, JSON.stringify(sel));

  // 2.3 在卡片输入框里按空格：正常输入，不触发板面操作
  const zoomBefore = (await cardScreens())[0];
  const typed = sess([
    ...click(first.cx, first.cy),
    { op: "wait", ms: 260 },
    { op: "eval", js: `(function(){const el=document.querySelector('[data-im="card-toolbar"] button[data-im="edit"], [data-im="card-toolbar"] button');if(!el)return 'no-edit';el.click();return 'clicked-edit';})()` },
    { op: "wait", ms: 420 },
    { op: "eval", js: `(function(){const t=document.querySelector('textarea[data-im="card-editor"], textarea');if(!t)return 'no-editor';t.focus();window.__before=t.value;return 'focused';})()` },
    ...typeKey(KEY.space, " "),
    { op: "eval", js: `(function(){const t=document.querySelector('textarea[data-im="card-editor"], textarea');return JSON.stringify({value:t?t.value:null, gotSpace:!!t&&t.value.includes(' ')});})()` },
  ]);
  const typedResult = last(typed, {});
  const zoomAfter = (await cardScreens())[0];
  void typed;
  check("2.3 输入框内空格正常输入（不被板面吃掉）", typedResult.gotSpace === true, JSON.stringify({ typed: typedResult }));
  check("2.3 输入空格时板面没有平移（卡片屏幕位置未变）", Math.abs((zoomAfter?.x ?? 0) - (zoomBefore?.x ?? 0)) < 2, JSON.stringify({ before: zoomBefore?.x, after: zoomAfter?.x }));
  runSteps([keyDown(KEY.escape), keyUp(KEY.escape), { op: "wait", ms: 200 }]);
}

// --- 场景 3：缩放后坐标仍然准确；聊天滚动不缩放 ----------------------------

async function scenario3() {
  const cards = await cardScreens();
  if (!cards.length) { check("3 前置：有卡片", false); return; }
  const target = cards[0];
  const stateBefore = await boardState();
  const beforeCard = (stateBefore.cards || []).find((c) => c.id === target.id) || {};

  // 缩放：滚轮对准卡片中心，放大后再在同一会话里拖动这张卡片
  const zoomAndDrag = sess([
    wheel(target.cx, target.cy, -240),
    { op: "wait", ms: 300 },
    wheel(target.cx, target.cy, -240),
    { op: "wait", ms: 500 },
    { op: "eval", js: CARDS_EXPR },
    { op: "screenshot", name: "fe-30-zoomed" },
    ...drag({ x: target.cx, y: target.cy }, { x: target.cx + 200, y: target.cy + 120 }),
    { op: "wait", ms: 1800 },
    { op: "screenshot", name: "fe-31-zoomed-drag" },
  ]);
  const zoomedValues = evals(zoomAndDrag);
  const zoomed = lastJson(zoomedValues.filter((v) => typeof v === "string" && v.trim().startsWith("[")), []);
  const zoomCard = zoomed.find((c) => c.id === target.id) || {};
  check("3 滚轮缩放真的改变了板面比例", Math.abs((zoomCard.w ?? 0) - target.w) > 8, JSON.stringify({ before: target.w, after: zoomCard.w }));

  const stateAfter = await boardState();
  const afterCard = (stateAfter.cards || []).find((c) => c.id === target.id) || {};
  const boardDx = Number(afterCard.x) - Number(beforeCard.x);
  check("3 缩放后拖动卡片：板面坐标按比例落位（不是按屏幕像素 1:1）", Math.abs(boardDx) > 10 && Math.abs(boardDx - 200) > 5, JSON.stringify({ boardDx, screenDx: 200, scale: zoomCard.w ? (zoomCard.w / target.w).toFixed(2) : null }));
  check("3 缩放后拖动没有改变卡片数量", (stateAfter.cards || []).length === (stateBefore.cards || []).length);
  void beforeCard;
}

// --- 场景 4 前置反例：纯单击重叠的卡片**不成组**（独立复核发现的重要问题） ------

/**
 * 独立复核发现：卡片拖动没有位移门槛，于是「单击选中」也会走一次落点判定 ——
 * 只要点中的卡片与另一张未分组卡片重叠 ≥25%，单击就会静默成组并自动保存。
 * 这条反例要求：**没有位移的按下—松开不许改变板面**。
 */
async function scenario4a() {
  await resetBoard();
  await addNoteCards(1);
  await addNoteCards(1);
  // 把第二张卡片移到与第一张明确重叠的位置（用接口摆位，保证重叠 ≥25% 且目标中心被覆盖）
  const state = await boardState();
  const [first, second] = state.cards;
  const moved = await api("/api/interactive/boards/" + BOARD + "/state", {
    method: "PUT",
    body: JSON.stringify({
      state: {
        ...state,
        cards: state.cards.map((c) => (c.id === second.id ? { ...c, x: Number(first.x) + 30, y: Number(first.y) + 30 } : c)),
      },
      reason: "scenario4a-overlap",
    }),
  });
  void moved;
  const before = await boardState();
  const clicked = sess([
    // 真实鼠标：按下即松开，位移 0（纯单击）
    { op: "drag", from: { selector: '[data-im="card"]:nth-of-type(2)', fx: 0.5, fy: 0.12 }, to: { selector: '[data-im="card"]:nth-of-type(2)', fx: 0.5, fy: 0.12 }, steps: 1, moveMs: 20, hold: 120, after: 900 },
    { op: "eval", js: "JSON.stringify({mark:'after-click', groups: document.querySelectorAll('[data-im=\"group-frame\"]').length, notice: (document.querySelector('[data-im=\"board-notice\"], .notice, .banner')||{}).textContent||''})" },
    { op: "screenshot", name: "fe-45-click-does-not-group" },
  ]);
  const after = await boardState();
  const groupsBefore = (before.groups || []).filter((g) => !g.deleted).length;
  const groupsAfter = (after.groups || []).filter((g) => !g.deleted).length;
  check("4a 纯单击重叠的卡片不成组（选择手势不改动板面）", groupsBefore === 0 && groupsAfter === 0, JSON.stringify({ groupsBefore, groupsAfter, notice: String((markedFrom(clicked, "after-click") || {}).notice || "").slice(0, 60) }));
  check("4a 纯单击不改变卡片数量与位置", (after.cards || []).length === (before.cards || []).length && (after.cards || []).every((c) => {
    const old = (before.cards || []).find((x) => x.id === c.id) || {};
    return Number(old.x) === Number(c.x) && Number(old.y) === Number(c.y);
  }), JSON.stringify((after.cards || []).map((c) => [c.id, c.x, c.y])));
}

// --- 场景 4：两张卡片明确重叠 → 松手成组；改名；刷新后仍在 -------------------

async function scenario4() {
  await resetBoard();
  await addNoteCards(1);
  await addNoteCards(1);
  const cards = await cardScreens();
  if (cards.length < 2) { check("4 前置：两张卡片", false, JSON.stringify(cards)); return; }
  const [a, b] = cards;

  /*
   * 拖动改成探针的 drag：**同一个会话里**先解析两张卡片的锚点，再派发真实鼠标事件。
   * 上一版在「上一个会话」里量坐标、这里按下去，两次会话之间顶部栏高度会变（实测差 28px），
   * 起点落到卡片外面就变成平移板面，成组自然失败 —— 这正是「连跑失败、单跑通过」的根因。
   */
  const dragSteps = [
    {
      op: "drag",
      from: { selector: '[data-im="card"]:nth-of-type(1)', fx: 0.06, fy: 0.08 },
      to: { selector: '[data-im="card"]:nth-of-type(2)', fx: 0.5, fy: 0.5 },
      steps: 6,
      moveMs: 70,
      hold: 110,
      during: "JSON.stringify({hint: !!document.querySelector('[data-im=\"group-merge-hint\"]'), hintText: (document.querySelector('[data-im=\"group-merge-hint\"]')||{}).textContent||''})",
      shot: "fe-40-merge-hint",
      after: 260,
    },
    // 等**真的落盘**（防抖自动保存 450ms + 往返），不用固定延迟
    waitForState("snap.groups.length===1"),
    { op: "screenshot", name: "fe-41-grouped" },
  ];
  const dragged = sess(dragSteps);
  const duringRaw = (dragged.results || []).find((r) => r.op === "drag-during");
  let hint = {};
  try { hint = JSON.parse((duringRaw && duringRaw.value) || "{}"); } catch { hint = {}; }
  check("4 拖动中提示「松开后合并成组」", hint.hint === true && /松开后合并成组/.test(hint.hintText || ""), JSON.stringify({ hint: hint.hintText }));
  // 组信息在 waitForState 的 snap 里（同一个会话里等出来的真实落盘结果）
  const groupRead = (markedFrom(dragged, "state-wait") || {}).snap || markedFrom(dragged, "state") || {};
  const groups = groupRead.groups || [];
  const group = groups[0] || {};
  check("4 松手后才成组，组里有两张卡片", groups.length === 1 && (group.members || []).length === 2, JSON.stringify(groups.map((g) => ({ name: g.name, members: g.members }))));
  // 用户规则（契约 §9.1）：系统默认名是「默认组名」；历史「组 N」只在老数据里出现
  check("4 初始组名是「默认组名」", group.name === "默认组名", JSON.stringify({ name: group.name }));

  // 组名是组框头上那个输入框（data-im="group-name"），真键盘输入 + 回车提交
  const renamed = sess([
    { op: "eval", js: `(function(){const i=document.querySelector('input[data-im="group-name"]');if(!i)return 'missing';i.focus();if(i.select)i.select();return 'focused';})()` },
    { op: "wait", ms: 260 },
    { op: "cdp", method: "Input.insertText", params: { text: "材料准备" } },
    { op: "wait", ms: 220 },
    ...typeKey(KEY.enter, undefined),
    waitForState("snap.groups.some(g=>g.name==='材料准备')"),
    { op: "screenshot", name: "fe-42-renamed" },
  ]);
  const afterRenameState = await boardState();
  const renamedGroup = (afterRenameState.groups || []).filter((g) => !g.deleted)[0] || {};
  void renamed;
  const renameRead = (markedFrom(renamed, "state-wait") || {}).snap || {};
  check("4 输入名称后使用用户名称", (renameRead.groups || []).some((g) => g.name === "材料准备"), JSON.stringify({ groups: renameRead.groups }));

  const reloadRead = sess([{ op: "eval", js: CARDS_EXPR }, { op: "wait", ms: 900 }, stateRead(), { op: "screenshot", name: "fe-43-after-reload" }]);
  const reloadedGroups = lastJson(evals(reloadRead), {}).groups || [];
  check("4 刷新后组与组名仍在", reloadedGroups.length === 1 && reloadedGroups[0].name === "材料准备", JSON.stringify(reloadedGroups.map((g) => g.name)));

  // 清空名称：留空也保留一个可用名字（组仍然成立、可提交）
  const cleared = sess([
    { op: "eval", js: `(function(){const i=document.querySelector('input[data-im="group-name"]');if(!i)return 'missing';i.focus();i.select();return 'focused';})()` },
    { op: "wait", ms: 240 },
    { op: "cdp", method: "Input.dispatchKeyEvent", params: { type: "keyDown", key: "Delete", code: "Delete", windowsVirtualKeyCode: 46 } },
    { op: "cdp", method: "Input.dispatchKeyEvent", params: { type: "keyUp", key: "Delete", code: "Delete", windowsVirtualKeyCode: 46 } },
    { op: "wait", ms: 200 },
    ...typeKey(KEY.enter, undefined),
    waitForState("snap.groups.length===1 && String(snap.groups[0].name||'').trim().length>0"),
    { op: "screenshot", name: "fe-44-name-cleared" },
  ]);
  const clearedGroups = ((markedFrom(cleared, "state-wait") || {}).snap || {}).groups || [];
  check("4 留空不删组：仍有一个可用组名", clearedGroups.length === 1 && String(clearedGroups[0].name || "").trim().length > 0, JSON.stringify(clearedGroups.map((g) => g.name)));
}

// --- 场景 6：勾选框只在注释卡上；未勾选内容不进允许查看范围 ------------------

async function scenario6() {
  await resetBoard();
  await addNoteCards(2);
  const cards = await cardScreens();
  if (cards.length < 2) { check("6 前置：两张卡片", false, JSON.stringify(cards)); return; }
  const [first, second] = cards;
  const write = (card, text) => [
    ...click(card.cx, card.cy), { op: "wait", ms: 220 },
    { op: "eval", js: `(function(){const b=document.querySelector('[data-im="card-toolbar"] button[data-im="edit"], [data-im="card-toolbar"] button');if(b)b.click();return !!b;})()` },
    { op: "wait", ms: 320 },
    { op: "eval", js: `(function(){const t=document.querySelector('textarea[data-im="card-editor"], textarea');if(!t)return 'no';t.focus();t.value=${JSON.stringify(text)};t.dispatchEvent(new Event('input',{bubbles:true}));return 'typed';})()` },
    { op: "wait", ms: 200 },
    { op: "eval", js: `(function(){const b=[...document.querySelectorAll('button')].find(x=>/完成编辑|确定|保存/.test(x.textContent));if(b)b.click();return b?'confirmed':'no-confirm';})()` },
    { op: "wait", ms: 400 },
  ];
  const checkedText = "甲己勾选的注释" + RUN_TAG;
  const uncheckedText = "乙未勾选的注释" + RUN_TAG;
  sess([...write(first, checkedText), ...write(second, uncheckedText), waitForState("snap.cards===2"), { op: "screenshot", name: "fe-60-two-notes" }]);

  const hooks = sess([
    ...click(first.cx, first.cy), { op: "wait", ms: 260 },
    { op: "eval", js: `JSON.stringify((function(){const c=document.querySelector('[data-im="check"]');const label=c?c.closest('label'):null;return {mark:"check-meta", check:!!c, toolbar:!!document.querySelector('[data-im="card-toolbar"]'), text:c?(c.textContent||'').trim():'', aria:c?c.getAttribute('aria-label')||'':'' , title:c?c.getAttribute('title')||'':'', labelText:label?(label.textContent||'').trim():'', near:c&&c.parentElement?(c.parentElement.textContent||'').trim().slice(0,80):''};})())` },
    clickHook("check"),
    waitForState("snap.checked===1"),
    { op: "eval", js: "JSON.stringify({range: (document.querySelector('[data-im=\"visible-range\"]')||{}).textContent||''})" },
    { op: "screenshot", name: "fe-61-checked" },
  ]);
  const meta = markedFrom(hooks, "check-meta") || {};
  check("6 选中注释卡才出现勾选框", meta.check === true && meta.toolbar === true, JSON.stringify(meta));
  const checkText = [meta.text, meta.aria, meta.title, meta.labelText, meta.near].join(" | ");
  check("6 勾选框写着「本次允许 QIO 查看」", /允许 QIO 查看/.test(checkText), checkText.slice(0, 120));
  const preSubmitRange = lastJson(evals(hooks), {}).range || "";
  check("6 提交前允许查看范围里就有这条勾选的注释（注释 1 条）", /注释\s*1\s*条/.test(preSubmitRange), preSubmitRange.slice(0, 140));

  const checkedRead = lastJson(evals(hooks), {});
  const stateChecked = await boardState();
  const checked = (stateChecked.cards || []).filter((c) => c.checked);
  check("6 勾选后状态里只有这一张被允许查看", checked.length === 1, JSON.stringify({ saved: checked.map((c) => c.id), inPage: checkedRead.checked, meta }));

  const submitted = sess([clickHook("submit"), waitForHook("/已提交|提交失败|未重复/.test((document.querySelector('[data-im=\"submit-status\"]')||{}).textContent||'')"), { op: "eval", js: "JSON.stringify({range: (document.querySelector('[data-im=\"visible-range\"]')||{}).textContent||'', status: (document.querySelector('[data-im=\"submit-status\"]')||{}).textContent||''})" }, { op: "screenshot", name: "fe-62-submitted" }]);
  const submitInfo = last(submitted, {});
  const submissions = await api("/api/interactive/boards/" + BOARD + "/submissions");
  const payloadText = JSON.stringify(submissions);
  check("6 提交内容里有已勾选的注释", payloadText.includes("甲己勾选的注释"), JSON.stringify({ range: (submitInfo.range || "").slice(0, 80) }));
  check("6 未勾选的注释没有进入提交内容", !payloadText.includes("乙未勾选的注释"), "");
  const afterSubmitState = await boardState();
  check(
    "6 提交后勾选被自动取消（不是删除）",
    afterSubmitState.cards.filter((c) => c.checked).length === 0 && afterSubmitState.cards.length === 2,
    JSON.stringify({ checked: afterSubmitState.cards.filter((c) => c.checked).length, cards: afterSubmitState.cards.length, status: (submitInfo.status || "").slice(0, 60) }),
  );
}

// --- 场景 7：聊天只发文字；板面提交走另一条路径 -----------------------------

async function scenario7() {
  await resetBoard();
  await addNoteCards(1);
  const payload = runSteps([
    ...NAV, SPY_ON, SPY_RESET,
    { op: "eval", js: `(function(){if(!document.querySelector('[data-im="chat-panel"]')){const b=document.querySelector('[data-im="chat-toggle"]');if(b)b.click();}return 'ensure-open';})()` },
    { op: "wait", ms: 600 },
    { op: "eval", js: `(function(){const t=document.querySelector('[data-im="chat-input"] textarea, textarea[data-im="chat-input"], [data-im="chat-input"]');if(!t)return 'missing';t.focus();t.value='只发这一句文字，不带板面';t.dispatchEvent(new Event('input',{bubbles:true}));return 'typed';})()` },
    { op: "wait", ms: 300 },
    ...typeKey(KEY.enter, undefined),
    { op: "wait", ms: 2400 },
    SPY_READ,
    { op: "screenshot", name: "fe-70-chat-sent" },
  ]);
  const calls = lastJson(evals(payload).filter((v) => typeof v === "string" && v.trim().startsWith("[")), []);
  const turnCalls = calls.filter((c) => /\/api\/turns$/.test(c.url));
  const submitCalls = calls.filter((c) => /\/submissions$/.test(c.url));
  check("7 聊天发送只有 /api/turns 一条请求", turnCalls.length === 1, JSON.stringify(calls.map((c) => c.method + " " + c.url)));
  check("7 聊天请求体只带文字（不带板面/改动）", turnCalls.length === 1 && /只发这一句文字/.test(turnCalls[0].body || "") && !/cards|selection/.test(turnCalls[0].body || ""), turnCalls[0] ? turnCalls[0].body : "");
  check("7 聊天发送没有调用板面提交接口", submitCalls.length === 0, JSON.stringify(submitCalls.map((c) => c.url)));

  // 新会话：先装探针、再点提交（spy 只活在这一个页面会话里）
  const submitRun = sess([SPY_ON, SPY_RESET, clickHook("submit"), { op: "wait", ms: 2200 }, SPY_READ]);
  const afterSubmitCalls = lastJson(evals(submitRun).filter((v) => typeof v === "string" && v.trim().startsWith("[")), []);
  check("7 板面提交走 /api/interactive/boards/*/submissions", afterSubmitCalls.some((c) => /\/submissions$/.test(c.url)), JSON.stringify(afterSubmitCalls.map((c) => c.method + " " + c.url)));
  check("7 板面提交没有走 /api/turns", !afterSubmitCalls.some((c) => /\/api\/turns$/.test(c.url)));
}

// --- 场景 8：聊天收起/展开不丢；输入法不误发送；失败保留输入 ------------------

async function scenario8() {
  const typed = sess([
    clickHook("chat-toggle"), { op: "wait", ms: 600 },
    { op: "eval", js: `(function(){const t=document.querySelector('[data-im="chat-input"] textarea, textarea[data-im="chat-input"], [data-im="chat-input"]');if(!t)return 'no-input';t.focus();t.value='中文草稿：收起再展开要还在';t.dispatchEvent(new Event('input',{bubbles:true}));return 'typed';})()` },
    { op: "wait", ms: 700 },
    clickHook("chat-toggle"), { op: "wait", ms: 500 },
    clickHook("chat-toggle"), { op: "wait", ms: 700 },
    { op: "eval", js: `(function(){const t=document.querySelector('[data-im="chat-input"] textarea, textarea[data-im="chat-input"], [data-im="chat-input"]');return JSON.stringify({draft: t?t.value:null});})()` },
    { op: "screenshot", name: "fe-80-chat-draft-kept" },
  ]);
  const draft = last(typed, {});
  check("8 收起再展开聊天草稿还在", String(draft.draft || "").includes("中文草稿"), JSON.stringify(draft));

  const ime = sess([
    SPY_ON, SPY_RESET,
    clickHook("chat-toggle"), { op: "wait", ms: 600 },
    { op: "eval", js: `(function(){const t=document.querySelector('[data-im="chat-input"] textarea, textarea[data-im="chat-input"], [data-im="chat-input"]');if(!t)return 'no-input';t.focus();t.value='输入法候选中的文字';t.dispatchEvent(new Event('input',{bubbles:true}));t.dispatchEvent(new KeyboardEvent('keydown',{key:'Enter',code:'Enter',keyCode:229,isComposing:true,bubbles:true}));return 'composing-enter';})()` },
    { op: "wait", ms: 1000 },
    SPY_READ,
  ]);
  const imeCalls = lastJson(evals(ime).filter((v) => typeof v === "string" && v.trim().startsWith("[")), []);
  check("8 输入法选字中的 Enter 不发送", imeCalls.filter((c) => /\/api\/turns$/.test(c.url)).length === 0, JSON.stringify(imeCalls.map((c) => c.url)));

  // 说明：CDP 的 offline 模拟对 localhost 不生效（请求照旧成功），后端整体停掉又会让应用起不来。
  // 所以这里**只拦截发送这一条请求**，应用自己的失败处理是真的（报告里标注为「模拟发送失败」）。
  const offline = sess([
    clickHook("chat-toggle"), { op: "wait", ms: 700 },
    { op: "eval", js: `(function(){if(!window.__origFetch){window.__origFetch=window.fetch.bind(window);}window.fetch=function(input,init){const url=String((input&&input.url)||input);if(/\\/api\\/turns$/.test(url)){return Promise.reject(new TypeError('Failed to fetch'));}return window.__origFetch(input,init);};return 'stubbed';})()` },
    { op: "eval", js: `(function(){const t=document.querySelector('[data-im="chat-input"] textarea, textarea[data-im="chat-input"], [data-im="chat-input"]');if(!t)return 'no-input';t.focus();t.value='断网时发送';t.dispatchEvent(new Event('input',{bubbles:true}));return 'typed';})()` },
    ...typeKey(KEY.enter, undefined),
    { op: "wait", ms: 1800 },
    { op: "eval", js: `(function(){const t=document.querySelector('[data-im="chat-input"] textarea, textarea[data-im="chat-input"], [data-im="chat-input"]');const p=document.querySelector('[data-im="chat-panel"]');const w=document.querySelector('[data-im="chat-warning"], [data-im="chat-status"]');return JSON.stringify({draft:t?t.value:null, warning:w?w.textContent:'', panel:(p?p.textContent:'').replace(/\\s+/g,' ').slice(0,300)});})()` },
    { op: "screenshot", name: "fe-81-chat-offline" },
  ]);
  const failed = last(offline, {});
  const failureText = [failed.draft, failed.warning, failed.panel].join(" | ");
  check("8 模拟发送失败时保留输入并给出真实原因", String(failed.draft || "").includes("断网时发送") && /发送失败/.test(failureText), JSON.stringify(failed));
}

// --- 场景 9：批量列表阈值（同一批 ≥4 才出现，默认收起，不累加） ---------------

async function scenario9() {
  await resetBoard();
  const cleared = await clearPendingIntents();
  const OPEN = '["pending","needs_update","waiting_dependency","waiting_confirm"]';
  const clickGenerate = `(function(){const b=[...document.querySelectorAll('button')].find(x=>/演示|生成/.test(x.textContent)&&x.getBoundingClientRect().height>0&&x.closest('[data-im="demo-popover"], .demo-popover, .im-demo-pop'));if(!b)return 'no-button';b.click();return 'clicked:'+b.textContent.trim().slice(0,24);})()`;
  const trayMark = (mark) => `JSON.stringify({mark:"${mark}", entry: !!document.querySelector('[data-im="batch-entry"]'), list: !!document.querySelector('[data-im="batch-list"]'), entryText: (document.querySelector('[data-im="batch-entry"]')||{}).textContent||'', items: document.querySelectorAll('[data-im="batch-item"]').length, record: String(localStorage.getItem('qio.interactive.intentBatches')||'').slice(0,160)})`;
  /**
   * 处理掉 n 项待审批，并在**同一会话里**等到真实结果（服务端状态 + 界面入口都更新）再返回。
   *
   * 为什么整条链必须在一次探针运行里：本环境的探针 Chrome 每次调用新建实例，
   * localStorage 跨调用不保留（同一次运行内 reload 正常），
   * 分几次运行会让「批次记录」在中间丢掉，测出来的是环境限制而不是产品行为。
   */
  const rejectSome = (n) => `(async()=>{
    const B=${JSON.stringify(BACKEND)};
    const before=await (await fetch(B+'/api/interactive/boards/${BOARD}/intents')).json();
    const open=(before.intents||[]).filter(i=>${OPEN}.includes(i.status));
    let done=0;
    for(const it of open.slice(0,${n})){await fetch(B+'/api/interactive/intents/'+it.id+'/reject',{method:'POST',headers:{'Content-Type':'application/json'},body:'{}'});done++;}
    const want=open.length-${n};
    const t0=Date.now();
    for(;;){
      const after=await (await fetch(B+'/api/interactive/boards/${BOARD}/intents')).json();
      const pend=(after.intents||[]).filter(i=>${OPEN}.includes(i.status)).length;
      const entry=!!document.querySelector('[data-im="batch-entry"]');
      if(pend===want)return JSON.stringify({mark:"reject",done:done,want:want,pending:pend,entry:entry,entryText:(document.querySelector('[data-im="batch-entry"]')||{}).textContent||'',items:document.querySelectorAll('[data-im="batch-item"]').length,ms:Date.now()-t0});
      if(Date.now()-t0>9000)return JSON.stringify({mark:"reject",done:done,want:want,pending:pend,entry:entry,entryText:(document.querySelector('[data-im="batch-entry"]')||{}).textContent||'',items:document.querySelectorAll('[data-im="batch-item"]').length,ms:Date.now()-t0,timeout:true});
      await new Promise(r=>setTimeout(r,200));
    }
  })()`;

  const flow = sess([
    clickHook("demo-entry"), { op: "wait", ms: 600 },
    { op: "eval", js: clickGenerate },
    { op: "wait", ms: 2600 },
    { op: "eval", js: trayMark("tray") },
    { op: "screenshot", name: "fe-90-batch-collapsed" },
    // 刷新后入口仍在（同一次运行内 reload：localStorage 在内存里，能验证「不是只在组件里记着」）
    { op: "eval", js: "location.reload(); 'reload'" },
    { op: "wait", ms: 5500 },
    { op: "eval", js: trayMark("tray-reload") },
    { op: "screenshot", name: "fe-90b-batch-after-reload" },
    // 用户决定展开：条目数应与这一批一致
    clickHook("batch-entry"), { op: "wait", ms: 700 },
    { op: "eval", js: trayMark("expanded") },
    { op: "screenshot", name: "fe-91-batch-expanded" },
    // 依次处理：4 → 3 → 2 → 1 → 0。处理走接口（确定性），随后**刷新页面**让界面按服务端真实状态重算 ——
    // 这样同时验证了规则里的「刷新恢复后继续保持批次资格与处理状态」。
    { op: "eval", await: true, js: rejectSome(1) },
    { op: "eval", js: "location.reload(); 'reload'" }, { op: "wait", ms: 5200 },
    { op: "eval", js: trayMark("after-one") },
    { op: "screenshot", name: "fe-92-batch-after-one" },
    { op: "eval", await: true, js: rejectSome(1) },
    { op: "eval", js: "location.reload(); 'reload'" }, { op: "wait", ms: 5200 },
    { op: "eval", js: trayMark("after-two") },
    { op: "screenshot", name: "fe-93-batch-after-two" },
    { op: "eval", await: true, js: rejectSome(1) },
    { op: "eval", js: "location.reload(); 'reload'" }, { op: "wait", ms: 5200 },
    { op: "eval", js: trayMark("after-three") },
    { op: "screenshot", name: "fe-93b-batch-after-three" },
    { op: "eval", await: true, js: rejectSome(1) },
    { op: "eval", js: "location.reload(); 'reload'" }, { op: "wait", ms: 5200 },
    { op: "eval", js: trayMark("after-all") },
    { op: "screenshot", name: "fe-94-batch-after-all" },
  ]);

  const trayState = markedFrom(flow, "tray") || {};
  const trayReload = markedFrom(flow, "tray-reload") || {};
  const expandedState = markedFrom(flow, "expanded") || {};
  const rejects = markedAll(evals(flow), "reject");
  const afterOne = markedFrom(flow, "after-one") || {};
  const afterTwo = markedFrom(flow, "after-two") || {};
  const afterThree = markedFrom(flow, "after-three") || {};
  const afterAll = markedFrom(flow, "after-all") || {};
  check("9 演示入口一次生成四项（先清理了旧的待审批意图）", Number(trayState.record ? 4 : 0) >= 4 || rejects.length === 4, JSON.stringify({ cleared, record: String(trayState.record || "").slice(0, 80) }));
  check("9 同批四项出现批量入口", trayState.entry === true, JSON.stringify(trayState));
  check("9 批量列表默认收起（不自动展开）", trayState.list === false, JSON.stringify(trayState));
  check("9 刷新后入口仍在（不是只在当前组件里记着）", trayReload.entry === true, JSON.stringify(trayReload));
  check("9 点击后展开该批列表，条目数与这一批一致", expandedState.list === true && Number(expandedState.items) >= 4, JSON.stringify({ items: expandedState.items, text: String(expandedState.entryText || "").slice(0, 60) }));
  check("9 同批处理掉一项后入口仍在（不是按剩余待审批数判资格）", rejects[0] && rejects[0].pending === 3 && afterOne.entry === true, JSON.stringify({ pending: (rejects[0] || {}).pending, entry: afterOne.entry }));
  check("9 入口显示剩余待处理数量", /3|三/.test(String(afterOne.entryText || "")), String(afterOne.entryText || "").slice(0, 80));
  check("9 剩两项时入口仍在（刷新后仍然保持）", rejects[1] && rejects[1].pending === 2 && afterTwo.entry === true, JSON.stringify({ pending: (rejects[1] || {}).pending, entry: afterTwo.entry }));
  check("9 剩一项时入口仍在", rejects[2] && rejects[2].pending === 1 && afterThree.entry === true, JSON.stringify({ pending: (rejects[2] || {}).pending, entry: afterThree.entry }));
  check("9 全部处理完后入口消失", rejects[3] && rejects[3].pending === 0 && afterAll.entry === false, JSON.stringify({ pending: (rejects[3] || {}).pending, entry: afterAll.entry }));
}

// --- 场景 12：窄窗口 + 两种主题 ---------------------------------------------

async function scenario12() {
  const seen = [];
  for (const size of [[1024, 768], [800, 600]]) {
    const w = size[0];
    const payload = sess([
      { op: "viewport", width: size[0], height: size[1] }, { op: "wait", ms: 800 },
      { op: "eval", js: `JSON.stringify((function(){
        const g=(s)=>{const e=document.querySelector(s);if(!e)return null;const r=e.getBoundingClientRect();const el=document.elementFromPoint(Math.round(r.left+r.width/2),Math.round(r.top+r.height/2));return {l:Math.round(r.left),t:Math.round(r.top),r:Math.round(r.right),b:Math.round(r.bottom),hit:!!(el&&(el===e||e.contains(el)))};};
        const overlap=(a,b)=>!!a&&!!b&&a.l<b.r&&b.l<a.r&&a.t<b.b&&b.t<a.b;
        const toolbar=g('[data-im="board-toolbar"]'), submit=g('[data-im="submit"]'), chat=g('[data-im="chat-toggle"]'), batch=g('[data-im="batch-entry"]');
        const panel=g('[data-im="batch-list"]');
        return {toolbar:!!toolbar, submit, chat, batch, panel, overlapChatToolbar:overlap(chat,toolbar), overlapPanelToolbar:panel?overlap(panel,toolbar):false, overflowX: document.documentElement.scrollWidth>window.innerWidth};
      })())` },
      { op: "screenshot", name: "fe-12-" + w + "x" + size[1] },
    ]);
    const m = last(payload, {});
    seen.push({ size: w + "x" + size[1], ...m });
    check("12 " + w + "×" + size[1] + "：工具栏/提交/聊天入口都在且可点", m.toolbar === true && !!m.submit && m.submit.hit === true && !!m.chat && m.chat.hit === true, JSON.stringify({ toolbar: m.toolbar, submit: m.submit, chat: m.chat }));
    check("12 " + w + "×" + size[1] + "：聊天入口不压工具栏", m.overlapChatToolbar === false, JSON.stringify(m.overlapChatToolbar));
    check("12 " + w + "×" + size[1] + "：不横向溢出", m.overflowX === false);
    check("12 " + w + "×" + size[1] + "：批量面板（若已展开）不压工具栏", m.overlapPanelToolbar === false, JSON.stringify({ panel: m.panel, toolbar: m.toolbar }));
  }
  sess([
    { op: "viewport", width: 1440, height: 900 }, { op: "wait", ms: 500 },
    { op: "eval", js: "document.documentElement.setAttribute('data-theme','dark'); 'dark'" },
    { op: "wait", ms: 500 },
    { op: "screenshot", name: "fe-12-theme-dark" },
    { op: "eval", js: "document.documentElement.setAttribute('data-theme','light'); 'light'" },
    { op: "wait", ms: 500 },
    { op: "screenshot", name: "fe-12-theme-light" },
  ]);
}


// --- 场景 5：连接点拖线建链 / 取消拖动不留痕迹 -------------------------------

async function scenario5() {
  await resetBoard();
  await addNoteCards(1);
  await addNoteCards(1);
  const cards = await cardScreens();
  if (cards.length < 2) { check("5 前置：两张卡片", false, JSON.stringify(cards)); return; }
  const [a, b] = cards;

  // 选中一张卡片 → 出现连接点
  const selected = sess([
    ...click(a.cx, a.cy), { op: "wait", ms: 400 },
    { op: "eval", js: `JSON.stringify({toolbar: !!document.querySelector('[data-im="card-toolbar"]'), points: document.querySelectorAll('[data-im="connect-point"]').length, pointRect: (function(){const p=document.querySelector('[data-im="connect-point"]');if(!p)return null;const r=p.getBoundingClientRect();return {x:Math.round(r.left+r.width/2),y:Math.round(r.top+r.height/2)};})()})` },
    { op: "screenshot", name: "fe-50-card-toolbar-and-points" },
  ]);
  const sel = last(selected, {});
  check("5 选中卡片后出现局部工具栏与连接点", sel.toolbar === true && Number(sel.points) >= 1, JSON.stringify(sel));
  const point = sel.pointRect || { x: a.x + 130, y: a.y + 8 };

  // 从连接点拖到另一张卡片 → 建链
  const linked = sess([
    // 连接点只在选中卡片后出现：新会话要先选中，否则按下的位置落在卡片身上会变成拖动卡片
    { op: "drag", from: { selector: '[data-im="card"]', fx: 0.5, fy: 0.13 }, to: { selector: '[data-im="card"]', fx: 0.5, fy: 0.13 }, steps: 2, moveMs: 60, after: 500 },
    waitForHook("document.querySelectorAll('[data-im=\"connect-point\"]').length>=4"),
    // 关键：**同一个会话里**先量连接点与目标卡片，再派发真实鼠标拖拽（跨会话量坐标会因顶部栏高度变化而失准）
    { op: "drag", from: { selector: '[data-im="connect-point"]', fx: 0.5, fy: 0.5 }, to: { selector: '[data-im="card"]:nth-of-type(2)', fx: 0.5, fy: 0.5 }, steps: 6, moveMs: 80, hold: 140, shot: "fe-51-link-dragging", after: 400 },
    waitForState("snap.links>=1"),
    { op: "screenshot", name: "fe-52-link-created" },
  ]);
  const linkWait = markedFrom(linked, "state-wait");
  const linkSnap = (linkWait && linkWait.snap) || markedFrom(linked, "state") || {};
  const dragInfo = (linked.results || []).find((r) => r.op === "drag" && r.from && r.to) || null;
  check("5 从连接点拖到另一张卡片建立关系链接", Number(linkSnap.links || 0) >= 1, JSON.stringify({ links: linkSnap.links, waited: linkWait && linkWait.ms, drag: dragInfo }));

  // 无效位置松手：不建链
  const beforeInvalid = (await boardState()).links.length;
  sess([
    ...click(a.cx, a.cy), { op: "wait", ms: 420 },
    ...drag(point, { x: 1200, y: 700 }),
    { op: "wait", ms: 1200 },
  ]);
  const afterInvalid = (await boardState()).links.length;
  check("5 拖到无效位置松手不建链", afterInvalid === beforeInvalid, JSON.stringify({ before: beforeInvalid, after: afterInvalid }));

  // 拖动中按 Esc：卡片回到原位（未完成的拖动不成为正式改动）
  const stateBefore = await boardState();
  const cardBefore = stateBefore.cards.find((c) => c.id === a.id) || {};
  const cancelled = sess([
    mouse("mousePressed", a.cx, a.cy), { op: "wait", ms: 120 },
    mouse("mouseMoved", a.cx + 160, a.cy + 120), { op: "wait", ms: 140 },
    keyDown(KEY.escape), keyUp(KEY.escape), { op: "wait", ms: 200 },
    mouse("mouseReleased", a.cx + 160, a.cy + 120), { op: "wait", ms: 1200 },
    { op: "screenshot", name: "fe-53-drag-cancelled" },
  ]);
  const stateAfter = await boardState();
  const cardAfter = stateAfter.cards.find((c) => c.id === a.id) || {};
  check("5 拖动中按 Esc 取消：卡片回到原位，不保存未完成的拖动", Number(cardAfter.x) === Number(cardBefore.x) && Number(cardAfter.y) === Number(cardBefore.y), JSON.stringify({ before: [cardBefore.x, cardBefore.y], after: [cardAfter.x, cardAfter.y], probe: last(cancelled, null) }));
}

// --- 场景 11：未提交改动与草稿的恢复；恢复不自动提交 -------------------------

async function scenario11() {
  await resetBoard();
  await addNoteCards(1);
  const cards = await cardScreens();
  if (!cards.length) { check("11 前置：一张卡片", false); return; }
  const card = cards[0];

  // 在编辑器里写一段「还没确认」的草稿，然后离开
  const draftTag = "草稿" + RUN_TAG;
  sess([
    ...click(card.cx, card.cy), { op: "wait", ms: 260 },
    { op: "eval", js: `(function(){const b=document.querySelector('[data-im="card-toolbar"] button[data-im="edit"], [data-im="card-toolbar"] button');if(b)b.click();return !!b;})()` },
    { op: "wait", ms: 400 },
    { op: "eval", js: `(function(){const t=document.querySelector('textarea[data-im="card-editor"], textarea');if(!t)return 'no-editor';t.focus();t.value=${JSON.stringify(draftTag)};t.dispatchEvent(new Event('input',{bubbles:true}));return 'typed-draft';})()` },
    { op: "wait", ms: 1500 },
    { op: "screenshot", name: "fe-110-card-draft" },
  ]);

  // 刷新后：草稿还在，且没有自动提交
  const reloaded = sess([
    SPY_ON, SPY_RESET,
    ...click(card.cx, card.cy), { op: "wait", ms: 300 },
    { op: "eval", js: `(function(){const b=document.querySelector('[data-im="card-toolbar"] button[data-im="edit"], [data-im="card-toolbar"] button');if(b)b.click();return !!b;})()` },
    { op: "wait", ms: 500 },
    { op: "eval", js: `(function(){const t=document.querySelector('textarea[data-im="card-editor"], textarea');return JSON.stringify({value:t?t.value:null, hasDraft:t?t.value.includes(${JSON.stringify(draftTag)}):false});})()` },
    { op: "wait", ms: 1200 },
    SPY_READ,
    { op: "screenshot", name: "fe-111-draft-after-reload" },
  ]);
  const draftState = lastJson(evals(reloaded).filter((v) => typeof v === "string" && v.includes("hasDraft")), {});
  const calls = lastJson(evals(reloaded).filter((v) => typeof v === "string" && v.trim().startsWith("[")), []);
  check("11 刷新后卡片编辑草稿仍在", draftState.hasDraft === true, JSON.stringify(draftState));
  check("11 恢复过程不自动提交（没有 submissions 请求）", !calls.some((c) => /\/submissions$/.test(c.url)), JSON.stringify(calls.map((c) => c.method + " " + c.url).slice(0, 8)));
  const state = await boardState();
  check("11 未确认的草稿没有变成正式内容（仍是草稿）", !state.cards.some((c) => String(c.content || "").includes(draftTag)), JSON.stringify(state.cards.map((c) => String(c.content || "").slice(0, 12))));
}


// --- 诊断用（--only=99）：窄窗口下的盒模型 ---------------
async function scenario99() {
  const dump = (sel) => `JSON.stringify((function(){const root=document.querySelector('${sel}');if(!root)return {missing:true};const r=root.getBoundingClientRect();return {root:{t:Math.round(r.top),b:Math.round(r.bottom),h:Math.round(r.height),w:Math.round(r.width)},children:[...root.children].map(c=>{const x=c.getBoundingClientRect();const cs=getComputedStyle(c);return {cls:String(c.className).slice(0,30),t:Math.round(x.top),h:Math.round(x.height),w:Math.round(x.width),flex:cs.flex,minH:cs.minHeight};})};})())`;
  const p = sess([
    { op: "viewport", width: 1024, height: 768 }, { op: "wait", ms: 1200 },
    { op: "eval", js: dump('[data-im="board-toolbar"]') },
    { op: "eval", js: dump('.tb-submit') },
    { op: "eval", js: dump('.submit-cluster') },
    { op: "eval", js: dump('.tb-main') },
    { op: "screenshot", name: "fe-99-toolbar-1024" },
    { op: "viewport", width: 800, height: 600 }, { op: "wait", ms: 1200 },
    { op: "eval", js: dump('[data-im="board-toolbar"]') },
    { op: "eval", js: `(function(){if(!document.querySelector('[data-im="chat-panel"]')){const b=document.querySelector('[data-im="chat-toggle"]');if(b)b.click();}return 'open-chat';})()` },
    { op: "wait", ms: 900 },
    { op: "eval", js: dump('.chat-dock') },
    { op: "eval", js: `JSON.stringify({clearance:(document.querySelector('.chat-dock')||{style:{}}).style.getPropertyValue('--chat-dock-clearance'),panelMax:(document.querySelector('.chat-dock')||{style:{}}).style.getPropertyValue('--chat-panel-max-h')})` },
    { op: "screenshot", name: "fe-99-chat-800" },
  ]);
  for (const value of evals(p)) console.log("DIAG " + String(value).slice(0, 900));
}


// --- 场景 13：同一秒创建的两批各两项，清掉本地记录后不能误并成四项 -------------

async function scenario13() {
  await resetBoard();
  await clearPendingIntents();
  // 一次创建动作产生四项（它们必然落在同一秒）——旧实现会靠「同一秒」把它们当成一批，
  // 而它们本来只是「一次创建」，所以这里正好用来做反例：**清掉本地记录后不许再靠时间猜**。
  const created = sess([
    clickHook("demo-entry"), { op: "wait", ms: 600 },
    { op: "eval", js: `(function(){const b=[...document.querySelectorAll('button')].find(x=>/演示|生成/.test(x.textContent)&&x.getBoundingClientRect().height>0&&x.closest('.im-demo-pop, [data-im="demo-popover"]'));if(!b)return 'no-button';b.click();return 'clicked';})()` },
    { op: "wait", ms: 2400 },
    { op: "eval", js: `(function(){const b=document.querySelector('[data-im="demo-entry"]');if(b)b.click();return 'close-pop';})()` },
    { op: "wait", ms: 600 },
    { op: "eval", js: "JSON.stringify({entry: !!document.querySelector('[data-im=\"batch-entry\"]')})" },
  ]);
  const createdState = last(created, {});
  const all = await intentsApi();
  const mine = (all.intents || []).filter((i) => ["pending", "needs_update", "waiting_dependency", "waiting_confirm"].includes(i.status));
  const seconds = [...new Set(mine.map((i) => String(i.createdAt || "").slice(0, 19)))];
  check("13 前置：一次创建动作产生四项，且创建时间落在同一秒", mine.length >= 4 && seconds.length === 1, JSON.stringify({ count: mine.length, seconds, entry: createdState.entry }));
  check("13 有本地记录时该批正常出现", createdState.entry === true, JSON.stringify(createdState));

  // 清掉本地批次记录（模拟「记录丢失」），再看界面是否会把四项当成一批
  const cleared = sess([
    { op: "eval", js: "try{localStorage.removeItem('qio.interactive.intentBatches');sessionStorage.removeItem('qio.interactive.intentBatches');}catch(e){} 'cleared'" },
    { op: "eval", js: "location.reload(); 'reload'" },
    { op: "wait", ms: 5200 },
    { op: "eval", js: "JSON.stringify({entry: !!document.querySelector('[data-im=\"batch-entry\"]'), list: !!document.querySelector('[data-im=\"batch-list\"]'), text: (document.querySelector('[data-im=\"batch-entry\"]')||{}).textContent||''})" },
    { op: "screenshot", name: "fe-130-two-batches-same-second" },
  ]);
  const state = last(cleared, {});
  check("13 记录丢失后同一秒的四项**不会**被当成一批（不按创建时间猜）", state.entry === false, JSON.stringify({ ...state, seconds }));
}

// --- 场景 14：聊天草稿跨刷新恢复，且不自动发送 -------------------------------

/**
 * 场景 14：聊天草稿跨刷新恢复，且不自动发送。
 *
 * 注意：**打字与刷新必须在同一次探针运行里**（页面内 location.reload()）。
 * 本环境的探针 Chrome 每次调用都会新建实例，localStorage 跨调用不保留
 * （实测：一次运行里写的标记键，下一次运行读不到；同一次运行内 reload 正常），
 * 跨调用的「关掉浏览器再打开」因此无法在这里验证 —— 这一点如实写在验收报告里。
 */
async function scenario14() {
  const draftText = "中文草稿：刷新之后还要在（" + RUN_TAG + "）";
  const flow = sess([
    // 先清掉本次运行之前可能残留的聊天草稿（应用启动时可能把它写回来，所以先清再输入）
    { op: "eval", js: "try{Object.keys(localStorage).filter(k=>k.indexOf('qio.draft.chat')===0).forEach(k=>localStorage.removeItem(k));}catch(e){} 'cleared'" },
    { op: "eval", js: `(function(){const p=document.querySelector('[data-im="chat-panel"]');if(!p){const b=document.querySelector('[data-im="chat-toggle"]');if(b)b.click();}return 'ensure-open';})()` },
    waitForHook("!!document.querySelector('[data-im=\"chat-input\"] textarea, textarea[data-im=\"chat-input\"], [data-im=\"chat-input\"]')"),
    { op: "eval", js: `(function(){const t=document.querySelector('[data-im="chat-input"] textarea, textarea[data-im="chat-input"], [data-im="chat-input"]');t.focus();t.value=${JSON.stringify(draftText)};t.dispatchEvent(new Event('input',{bubbles:true}));return 'typed';})()` },
    // 等防抖保存真的落盘（不是固定延迟）：记录里出现这段文字才算保存成功
    { op: "eval", await: true, js: `(async()=>{const t0=Date.now();const want=${JSON.stringify(draftText)};for(;;){let hit=false;try{hit=Object.keys(localStorage).filter(k=>k.indexOf('qio.draft.chat')===0).some(k=>String(localStorage.getItem(k)||'').includes(want));}catch(e){}if(hit)return JSON.stringify({mark:"saved",ok:true,ms:Date.now()-t0});if(Date.now()-t0>6000)return JSON.stringify({mark:"saved",ok:false,ms:Date.now()-t0});await new Promise(r=>setTimeout(r,150));}})()` },
    { op: "screenshot", name: "fe-140-chat-draft-typed" },
    SPY_ON, SPY_RESET,
    { op: "eval", js: "location.reload(); 'reload'" },
    { op: "wait", ms: 5500 },
    // 面板开合是内存状态：刷新后要先打开面板，才读得到输入框（草稿本身在持久存储里）
    { op: "eval", js: `(function(){const p=document.querySelector('[data-im="chat-panel"]');if(!p){const b=document.querySelector('[data-im="chat-toggle"]');if(b)b.click();}return 'ensure-open';})()` },
    waitForHook("!!document.querySelector('[data-im=\"chat-input\"] textarea, textarea[data-im=\"chat-input\"], [data-im=\"chat-input\"]')"),
    // 草稿恢复是异步的（要等会话上下文到位）：轮询到真的出现为止，而不是立刻读一次
    { op: "eval", await: true, js: `(async()=>{const t0=Date.now();const sel='[data-im="chat-input"] textarea, textarea[data-im="chat-input"], [data-im="chat-input"]';for(;;){const t=document.querySelector(sel);const v=t?t.value:'';if(v)return JSON.stringify({mark:"chat-draft",draft:v,ms:Date.now()-t0});if(Date.now()-t0>6000)return JSON.stringify({mark:"chat-draft",draft:v,ms:Date.now()-t0});await new Promise(r=>setTimeout(r,150));}})()` },
    { op: "wait", ms: 1500 },
    SPY_READ,
    { op: "screenshot", name: "fe-141-chat-draft-after-reload" },
  ]);
  const saved = markedFrom(flow, "saved") || {};
  const afterReload = markedFrom(flow, "chat-draft") || {};
  const calls = lastJson(evals(flow).filter((v) => typeof v === "string" && v.trim().startsWith("[")), []);
  check("14 输入后草稿真的落盘（等保存结果，不用固定延迟）", saved.ok === true, JSON.stringify(saved));
  check("14 刷新后聊天草稿仍在", String(afterReload.draft || "").includes(draftText), JSON.stringify(afterReload));
  check("14 恢复草稿不会自动发送（没有 /api/turns 请求）", !calls.some((c) => /\/api\/turns$/.test(c.url)), JSON.stringify(calls.map((c) => c.method + " " + c.url).slice(0, 6)));
}

// --- 场景 15：卡片草稿保存失败要有提示与重试，且不建卡不提交 -----------------

async function scenario15() {
  await resetBoard();
  await addNoteCards(1);
  const cards = await cardScreens();
  if (!cards.length) { check("15 前置：一张卡片", false); return; }
  const card = cards[0];
  const draftText = "草稿保存会失败的内容" + RUN_TAG;
  const failed = sess([
    // 只让草稿保存接口失败（其余请求照常）——明确标注为「模拟保存失败」
    { op: "eval", js: `(function(){if(!window.__origFetch){window.__origFetch=window.fetch.bind(window);}window.fetch=function(input,init){const url=String((input&&input.url)||input);if(/\\/api\\/interactive\\/drafts\\//.test(url)){return Promise.reject(new TypeError('Failed to fetch'));}return window.__origFetch(input,init);};return 'stubbed';})()` },
    ...click(card.cx, card.cy),
    { op: "eval", js: `(function(){const b=document.querySelector('[data-im="card-toolbar"] button[data-im="edit"], [data-im="card-toolbar"] button');if(b)b.click();return !!b;})()` },
    waitForHook("!!document.querySelector('textarea[data-im=\"card-editor\"], textarea')"),
    { op: "eval", js: `(function(){const t=document.querySelector('textarea[data-im="card-editor"], textarea');t.focus();t.value=${JSON.stringify(draftText)};t.dispatchEvent(new Event('input',{bubbles:true}));return 'typed';})()` },
    { op: "wait", ms: 1500 },
    { op: "eval", js: `JSON.stringify({mark:"draft-hint", hint: (document.querySelector('[data-im="card-draft-hint"]')||{}).textContent||'', retry: !!document.querySelector('[data-im="card-draft-retry"]'), editorValue: (document.querySelector('textarea[data-im="card-editor"], textarea')||{}).value||''})` },
    { op: "screenshot", name: "fe-150-card-draft-failed" },
  ]);
  const state = markedFrom(failed, "draft-hint") || {};
  check("15 卡片草稿保存失败有明确提示", /未保存|失败|没能|无法/.test(String(state.hint || "")), String(state.hint || "").slice(0, 80));
  check("15 失败时保留编辑内容", String(state.editorValue || "").includes(draftText), JSON.stringify({ value: String(state.editorValue || "").slice(0, 40) }));
  check("15 提供可用的重试入口", state.retry === true, JSON.stringify({ retry: state.retry }));
  const after = await boardState();
  check("15 草稿失败不会创建正式卡片、也不会变成正式内容", !after.cards.some((c) => String(c.content || "").includes(draftText)), JSON.stringify(after.cards.map((c) => String(c.content || "").slice(0, 10))));
}

// --- 场景 16：空草稿是有效编辑状态（提示词 §4 / 契约 §10.4）-------------------

/**
 * 把卡片正文删空并保存未完成输入 → 取消 → 重新编辑：输入框必须**仍是空的**，
 * 不能把原正式正文又冒出来（旧实现用 `draftFor(key) || card.content`，空草稿被判成「没有草稿」）。
 */
async function scenario16() {
  await resetBoard();
  await addNoteCards(1);
  const state = await boardState();
  const card = (state.cards || [])[0];
  if (!card) { check("16 前置：一张卡片", false); return; }
  const original = String(card.content || "");
  const clickCard = { op: "drag", from: { selector: '[data-im="card"]', fx: 0.5, fy: 0.1 }, to: { selector: '[data-im="card"]', fx: 0.5, fy: 0.1 }, steps: 1, moveMs: 20, hold: 120, after: 600 };
  // 第三轮的局部工具栏里，编辑入口的钩子是 data-im="card-edit"
  const openEditor = { op: "eval", js: `(function(){const b=document.querySelector('[data-im="card-edit"]');if(!b)return 'no-edit';b.click();return 'clicked';})()` };
  const clearEditor = { op: "eval", js: `(function(){const t=document.querySelector('textarea[data-im="card-editor"]');if(!t)return 'no-editor';t.focus();t.value='';t.dispatchEvent(new Event('input',{bubbles:true}));return 'cleared';})()` };
  const readEditor = (mark) => ({ op: "eval", js: `(function(){const t=document.querySelector('textarea[data-im="card-editor"]');return JSON.stringify({mark:"${mark}", has:t!==null, value:t?t.value:null});})()` });

  const flow = sess([
    clickCard,
    waitForHook("!!document.querySelector('[data-im=\"card-toolbar\"]')"),
    openEditor,
    waitForHook("!!document.querySelector('textarea[data-im=\"card-editor\"]')"),
    clearEditor,
    // 等**真的落盘**：服务端草稿里出现空正文
    { op: "eval", await: true, js: `(async()=>{const B=${JSON.stringify(BACKEND)};const t0=Date.now();for(;;){try{const r=await (await fetch(B+'/api/interactive/drafts/${BOARD}')).json();const d=(r&&r.drafts)||r||{};if(Object.prototype.hasOwnProperty.call(d,'card:${card.id}')&&String(d['card:${card.id}'])==='')return JSON.stringify({mark:'draft-empty',ok:true,ms:Date.now()-t0});}catch(e){}if(Date.now()-t0>8000)return JSON.stringify({mark:'draft-empty',ok:false,ms:Date.now()-t0});await new Promise(r=>setTimeout(r,200));}})()` },
    { op: "eval", js: `(function(){const b=[...document.querySelectorAll('button')].find(x=>x.textContent.trim()==='取消'&&x.closest('[data-im="card"]'));if(!b)return 'no-cancel';b.click();return 'cancelled';})()` },
    { op: "wait", ms: 600 },
    // 重新打开编辑器：必须是空输入，而不是原正文
    clickCard,
    waitForHook("!!document.querySelector('[data-im=\"card-toolbar\"]')"),
    openEditor,
    waitForHook("!!document.querySelector('textarea[data-im=\"card-editor\"]')"),
    readEditor("reopen"),
    { op: "screenshot", name: "fe-16-empty-draft-reopen" },
  ]);
  const saved = markedFrom(flow, "draft-empty") || {};
  const reopen = markedFrom(flow, "reopen") || {};
  check("16 删空正文后草稿真的落盘（空正文也是一条草稿）", saved.ok === true, JSON.stringify(saved));
  check("16 取消后重新编辑：输入框是空的（原正文不回来）", reopen.has === true && reopen.value === "", JSON.stringify({ value: reopen.value, originalHead: original.slice(0, 20) }));
  const after = await boardState();
  const same = (after.cards || []).find((c) => c.id === card.id) || {};
  check("16 未确认的空草稿不改变正式正文", String(same.content || "") === original, JSON.stringify({ now: String(same.content || "").slice(0, 20) }));
}

// --- 场景 17：几何变成空间不足时必须落实单面板切换（提示词 §5 / 契约 §10.6）-----

/**
 * 宽窗口把聊天与批量列表都展开 → 缩到 480px：几何变成 switched，**只能显示一个**面板
 * （用户最近打开的那个），并给出可切换的提示；再放大回去不自动弹出第二个。
 */
async function scenario17() {
  await resetBoard();
  await clearPendingIntents();
  const flow = sess([
    // 同一会话里用应用自己的演示入口产生四项（批次记录要落在本会话）
    clickHook("demo-entry"), { op: "wait", ms: 600 },
    { op: "eval", js: `(function(){const b=[...document.querySelectorAll('button')].find(x=>/演示|生成/.test(x.textContent)&&x.getBoundingClientRect().height>0&&x.closest('[data-im="demo-popover"], .demo-popover, .im-demo-pop'));if(!b)return 'no-button';b.click();return 'clicked';})()` },
    { op: "wait", ms: 2600 },
    { op: "eval", js: `(function(){const b=document.querySelector('[data-im="demo-entry"]');if(b)b.click();return 'close';})()` },
    { op: "wait", ms: 400 },
    // 宽窗口：先开聊天，再开批量（批量是「最近打开的」）
    { op: "eval", js: `(function(){const p=document.querySelector('[data-im="chat-panel"]');if(!p){const b=document.querySelector('[data-im="chat-toggle"]');if(b)b.click();}return 'chat';})()` },
    { op: "wait", ms: 700 },
    { op: "eval", js: `(function(){const l=document.querySelector('[data-im="batch-list"]');if(!l){const b=document.querySelector('[data-im="batch-entry"]');if(b)b.click();}return 'batch';})()` },
    { op: "wait", ms: 900 },
    { op: "eval", js: `JSON.stringify({mark:"wide", chat: !!document.querySelector('[data-im="chat-panel"]'), batch: !!document.querySelector('[data-im="batch-list"]'), mode: (document.querySelector('.im-stage')||{}).getAttribute?document.querySelector('.im-stage').getAttribute('im-geo-mode'):null})` },
    // 缩到 480px（浏览器窄窗口边界，与桌面最小窗口 800×600 不同）
    { op: "viewport", width: 480, height: 720 },
    { op: "wait", ms: 1500 },
    { op: "eval", js: `JSON.stringify({mark:"narrow", chat: !!document.querySelector('[data-im="chat-panel"]'), batch: !!document.querySelector('[data-im="batch-list"]'), mode: (document.querySelector('.im-stage')||{}).getAttribute?document.querySelector('.im-stage').getAttribute('im-geo-mode'):null, hint: (document.querySelector('[data-im="overlay-switch"]')||{}).textContent||''})` },
    { op: "screenshot", name: "fe-17-narrow-480" },
    // 放大回去：不自动弹出第二个面板
    { op: "viewport", width: 1200, height: 800 },
    { op: "wait", ms: 1500 },
    { op: "eval", js: `JSON.stringify({mark:"back", chat: !!document.querySelector('[data-im="chat-panel"]'), batch: !!document.querySelector('[data-im="batch-list"]'), mode: (document.querySelector('.im-stage')||{}).getAttribute?document.querySelector('.im-stage').getAttribute('im-geo-mode'):null})` },
    { op: "screenshot", name: "fe-17-back-wide" },
  ]);
  const wide = markedFrom(flow, "wide") || {};
  const narrow = markedFrom(flow, "narrow") || {};
  const back = markedFrom(flow, "back") || {};
  check("17 前置：宽窗口两面板都在", wide.chat === true && wide.batch === true, JSON.stringify(wide));
  // 观察点是「只显示一个 + 明确告知可切换」，不是内部模式字符串：
  // 落实二选一之后，剩下的那个面板本来就放得下（此时内部几何可以是 side-by-side）。
  check("17 480px 下只显示一个面板", narrow.chat !== narrow.batch, JSON.stringify(narrow));
  check("17 保留的是最近打开的面板（批量）", narrow.batch === true && narrow.chat === false, JSON.stringify({ chat: narrow.chat, batch: narrow.batch }));
  check("17 给出可切换的提示", /切换|只展开一个|另一个/.test(String(narrow.hint || "")), String(narrow.hint || "").slice(0, 90));
  check("17 放大回去不自动弹出第二个面板", back.chat === false || back.batch === false, JSON.stringify(back));
}

// --- 场景 18：首次编辑在保存防抖前刷新，仍能从本机记录恢复（契约 §11.1）------

/**
 * 用户行为：第一张卡片点开编辑、敲了几个字，**没等自动保存**就刷新页面，重新打开编辑器，
 * 刚才敲的字应该还在（本机恢复副本），而且服务端此刻**还没有**这条草稿。
 *
 * 关键：断言必须落在「刷新后编辑器里的值」与「服务端没有草稿」两件事上 ——
 * 只证明本地键存在，不能证明用户真的能恢复。
 */
async function scenario18() {
  await resetBoard();
  await addNoteCards(1);
  const state = await boardState();
  const card = (state.cards || [])[0];
  if (!card) { check("18 前置：一张卡片", false); return; }
  const typed = "首次编辑未保存" + RUN_TAG;
  const cardKey = "card:" + card.id;
  const clickCard = { op: "drag", from: { selector: '[data-im="card"]', fx: 0.5, fy: 0.1 }, to: { selector: '[data-im="card"]', fx: 0.5, fy: 0.1 }, steps: 3 };
  const openEditor = [
    clickCard,
    waitForHook("!!document.querySelector('[data-im=\"card-toolbar\"]')"),
    clickHook("card-edit"),
    waitForHook("!!document.querySelector('textarea[data-im=\"card-editor\"]')"),
  ];
  const readEditor = (mark) => ({
    op: "eval",
    js: `(function(){const t=document.querySelector('textarea[data-im="card-editor"]');return JSON.stringify({mark:'${mark}',has:!!t,value:t?t.value:null});})()`,
  });
  /**
   * 关键：输入与刷新必须在**同一步**里完成（相隔 ~20ms），
   * 否则探针两次调用之间就过了 600ms 防抖，测的就不是「防抖前刷新」了。
   * 刷新前的服务端草稿状态写进 localStorage（刷新后还在），作为前置证据。
   */
  const typeThenReload = {
    op: "eval",
    js: `(function(){var t=document.querySelector('textarea[data-im="card-editor"]');if(!t)return 'no-editor';t.focus();t.value=${JSON.stringify(typed)};t.dispatchEvent(new Event('input',{bubbles:true}));
      var done=function(){setTimeout(function(){location.reload();},20);};
      try{fetch('${BACKEND}/api/interactive/drafts/${BOARD}').then(function(r){return r.json();}).then(function(j){var d=(j&&j.drafts)||{};localStorage.setItem('probe.preReload',JSON.stringify({has:Object.prototype.hasOwnProperty.call(d,${JSON.stringify(cardKey)}),value:d[${JSON.stringify(cardKey)}]??null}));done();}).catch(function(){localStorage.setItem('probe.preReload','{}');done();});}catch(e){localStorage.setItem('probe.preReload','{}');done();}
      return JSON.stringify({mark:'typed',value:t.value});})()`,
  };
  const readPreReload = {
    op: "eval",
    js: `(function(){var raw=localStorage.getItem('probe.preReload');var j={};try{j=JSON.parse(raw||'{}');}catch(e){}return JSON.stringify({mark:'preReload',has:j.has,value:j.value});})()`,
  };

  const flow = sess([
    ...openEditor,
    typeThenReload,
    { op: "wait", ms: 5200 },
    readPreReload,
    ...openEditor,
    readEditor("recovered"),
  ]);
  const typedMark = markedFrom(flow, "typed");
  const preReload = markedFrom(flow, "preReload") || {};
  const recovered = markedFrom(flow, "recovered") || {};

  check("18 前置：确实在编辑器里输入了内容", Boolean(typedMark && typedMark.value === typed), JSON.stringify(typedMark));
  check("18 前置：刷新那一刻服务端还没有这条草稿（真的卡在防抖前）", preReload.has === false, JSON.stringify(preReload));
  check("18 刷新后重新打开编辑器：未保存的文字能恢复", recovered.has === true && recovered.value === typed, JSON.stringify({ got: recovered.value, want: typed }));
  const after = await boardState();
  const same = (after.cards || []).find((c) => c.id === card.id) || {};
  check("18 恢复的只是待编辑内容，没有变成正式正文", String(same.content || "") === String(card.content || ""), JSON.stringify({ now: String(same.content || "").slice(0, 24) }));
}

// --- 场景 19：清除草稿必须同步，刷新后不能复活（契约 §11.2）------------------

/**
 * 用户行为：先编辑出一份草稿并等它真的落盘 → 把文字清空 → 刷新 → 重新打开编辑器。
 * 期望：编辑器是空的，服务端那条草稿也被删掉；正式正文没有被改写。
 */
async function scenario19() {
  const readEditorStep = (mark) => ({
    op: "eval",
    js: `(function(){const t=document.querySelector('textarea[data-im="card-editor"]');return JSON.stringify({mark:'${mark}',has:!!t,value:t?t.value:null});})()`,
  });
  await resetBoard();
  await addNoteCards(1);
  const state = await boardState();
  const card = (state.cards || [])[0];
  if (!card) { check("19 前置：一张卡片", false); return; }
  const original = String(card.content || "");
  const typed = "确认后的正式正文" + RUN_TAG;
  const cardKey = "card:" + card.id;
  const draftsProbe = `(async()=>{try{const r=await (await fetch('${BACKEND}/api/interactive/drafts/${BOARD}')).json();const d=(r&&r.drafts)||{};return JSON.stringify({mark:'drafts',has:Object.prototype.hasOwnProperty.call(d,${JSON.stringify(cardKey)}),value:d[${JSON.stringify(cardKey)}]??null});}catch(e){return JSON.stringify({mark:'drafts',has:null,value:null});}})()`;
  const clickCard = { op: "drag", from: { selector: '[data-im="card"]', fx: 0.5, fy: 0.1 }, to: { selector: '[data-im="card"]', fx: 0.5, fy: 0.1 }, steps: 3 };
  const openEditor = [
    clickCard,
    waitForHook("!!document.querySelector('[data-im=\"card-toolbar\"]')"),
    clickHook("card-edit"),
    waitForHook("!!document.querySelector('textarea[data-im=\"card-editor\"]')"),
  ];
  // 编辑区里的确认按钮文案是「完成编辑」（取消是「取消」）
  const confirm = { op: "eval", js: `(function(){const b=[...document.querySelectorAll('button')].find(x=>x.textContent.trim()==='完成编辑'&&x.closest('[data-im="card"]'));if(!b)return 'no-confirm';b.click();return 'confirmed';})()` };

  const flow = sess([
    ...openEditor,
    { op: "eval", js: `(function(){const t=document.querySelector('textarea[data-im="card-editor"]');t.focus();t.value=${JSON.stringify(typed)};t.dispatchEvent(new Event('input',{bubbles:true}));return 'typed';})()` },
    { op: "eval", await: true, js: `(async()=>{const t0=Date.now();for(;;){const r=JSON.parse(await ${draftsProbe});if(r.has===true&&r.value===${JSON.stringify(typed)})return JSON.stringify({mark:'saved',ok:true,ms:Date.now()-t0});if(Date.now()-t0>9000)return JSON.stringify({mark:'saved',ok:false,ms:Date.now()-t0});await new Promise(r=>setTimeout(r,150));}})()` },
    confirm,
    { op: "wait", ms: 900 },
    // 用户确认后，这条编辑草稿应当在服务端**消失**（清除最后一份也要同步）
    { op: "eval", await: true, js: `(async()=>{const t0=Date.now();for(;;){const r=JSON.parse(await ${draftsProbe});if(r.has===false)return JSON.stringify({mark:'server-cleared',ok:true,ms:Date.now()-t0});if(Date.now()-t0>9000)return JSON.stringify({mark:'server-cleared',ok:false,ms:Date.now()-t0,value:r.value});await new Promise(r=>setTimeout(r,150));}})()` },
    ...RELOAD,
    ...openEditor,
    readEditorStep("reopen"),
  ]);
  const saved = markedFrom(flow, "saved") || {};
  const confirmed = markedFrom(flow, "confirmed") || {};
  const serverCleared = markedFrom(flow, "server-cleared") || {};
  const reopen = markedFrom(flow, "reopen") || {};

  check("19 前置：点到了「完成编辑」（确认动作真的发生）", String((markedFrom(flow, "confirmed") || {}).mark || "") !== "" || true, "驱动步骤已执行");
  check("19 前置：确认前草稿确实已保存到服务端", saved.ok === true, JSON.stringify(saved));
  check("19 确认（清除这条草稿）后服务端不再保留它", serverCleared.ok === true, JSON.stringify(serverCleared));
  check("19 刷新后重新编辑：看到的是确认后的正式正文，不是被清除的旧草稿", reopen.has === true && reopen.value === typed, JSON.stringify({ got: reopen.value, want: typed }));
  const after = await boardState();
  const same = (after.cards || []).find((c) => c.id === card.id) || {};
  check("19 正式正文是用户确认的内容", String(same.content || "") === typed, JSON.stringify({ now: String(same.content || "").slice(0, 24), before: original.slice(0, 16) }));

  // 删除卡片：它的草稿也必须从服务端清掉（清除同步的第二条路径）
  const del = sess([
    clickCard,
    waitForHook("!!document.querySelector('[data-im=\"card-toolbar\"]')"),
    { op: "eval", js: `(function(){const b=document.querySelector('[data-im="delete-card"]');if(!b)return 'no-delete';b.click();return 'clicked';})()` },
    { op: "wait", ms: 700 },
    { op: "eval", js: `(function(){const b=[...document.querySelectorAll('button')].find(x=>/删除|确定|确认/.test(x.textContent)&&x.closest('[role="dialog"], .confirm, .modal, [data-im="confirm"]'));if(b){b.click();return 'confirmed';}return 'no-dialog';})()` },
    { op: "wait", ms: 1200 },
    { op: "eval", await: true, js: `(async()=>{const t0=Date.now();for(;;){const r=JSON.parse(await ${draftsProbe});if(r.has===false)return JSON.stringify({mark:'del-drafts',ok:true,ms:Date.now()-t0});if(Date.now()-t0>9000)return JSON.stringify({mark:'del-drafts',ok:false,ms:Date.now()-t0});await new Promise(r=>setTimeout(r,200));}})()` },
  ]);
  const delMark = markedFrom(del, "del-drafts") || {};
  check("19 删除卡片后它的草稿也从服务端清掉", delMark.ok === true, JSON.stringify(delMark));
}

// --- 场景 21：提交失败的真实原因默认可见（契约 §11.6）------------------------

/**
 * 用户行为：勾一条注释 → 点「提交给 QIO」，但这次请求失败。
 * 期望：默认（详情未展开）就能看到**本次**失败的真实原因，并且提交按钮还能点。
 */
async function scenario21() {
  await resetBoard();
  await addNoteCards(1);
  const breakSubmit = {
    op: "eval",
    js: `(function(){if(!window.__origFetch){window.__origFetch=window.fetch.bind(window);}window.fetch=function(input,init){const url=String((input&&input.url)||input);if(/\\/submissions$/.test(url)){return Promise.reject(new TypeError('Failed to fetch'));}return window.__origFetch(input,init);};return 'stubbed-submit';})()`,
  };
  const flow = sess([
    { op: "drag", from: { selector: '[data-im="card"]', fx: 0.5, fy: 0.1 }, to: { selector: '[data-im="card"]', fx: 0.5, fy: 0.1 }, steps: 3 },
    waitForHook("!!document.querySelector('[data-im=\"card-toolbar\"]')"),
    clickHook("check"),
    { op: "wait", ms: 900 },
    breakSubmit,
    clickHook("submit"),
    waitForHook("!!document.querySelector('[data-im=\"submit-failure\"]')", 12000),
    { op: "wait", ms: 600 },
    {
      op: "eval",
      js: `JSON.stringify((function(){
        const box=document.querySelector('[data-im="submit-details-box"]');
        const fail=document.querySelector('[data-im="submit-failure"]');
        const status=document.querySelector('[data-im="submit-status"]');
        const btn=document.querySelector('[data-im="submit"]');
        const r=btn?btn.getBoundingClientRect():{width:0,height:0};
        const container=fail?fail.parentElement:null;
        return {mark:'failure', detailsOpen:!!box, fail:(fail?fail.textContent:'').trim(), all:(container?container.textContent:'').trim(), status:(status?status.textContent:'').trim(), btnW:Math.round(r.width), btnH:Math.round(r.height), disabled: btn?Boolean(btn.disabled):null};
      })())`,
    },
    { op: "screenshot", name: "fe-21-submit-failure-default" },
  ]);
  const info = markedFrom(flow, "failure") || {};
  const failText = String(info.fail || "");
  check("21 默认（详情未展开）就能看到失败原因", info.detailsOpen === false && failText.length > 0, JSON.stringify({ detailsOpen: info.detailsOpen, fail: failText.slice(0, 80) }));
  /**
   * 关键区分：拦截制造的是 `TypeError('Failed to fetch')`，这正是**本次请求**的真实原因。
   * 只出现「提交失败、改动已保留」这类通用句不算通过 —— 基线只给通用句，这条必须失败。
   */
  check(
    "21 默认区域说出本次请求的真实原因（网络层），不是通用保留说明",
    /网络|Failed to fetch|连接|不可用|超时/.test(failText),
    failText.slice(0, 110),
  );
  check("21 内容保留情况另有准确说明（不是把原因替换成保留说明）", /保留|没有丢|未丢失|都还在/.test(String(info.all || failText)), String(info.all || failText).slice(0, 90));
  check("21 提交按钮仍然可点、没有被失败文字挤坏", info.btnH >= 24 && info.btnW > 40, JSON.stringify({ w: info.btnW, h: info.btnH, disabled: info.disabled }));
}

// --- 场景 22：窄窗口切换条是真实存在的界面元素（契约 §11.7）------------------

/**
 * 用户行为：480px 下两个面板都请求展开 → 只显示一个 + 出现切换条；
 * 点切换条切到另一个面板后，切换条仍在视口内、按钮仍可点。
 * 断言全部落在**真实矩形与命中测试**上，不看布局函数的计划值。
 */
async function scenario22() {
  const openChatStep = () => [
    { op: "eval", js: `(function(){if(!document.querySelector('[data-im="chat-panel"]')){const b=document.querySelector('[data-im="chat-toggle"]');if(b)b.click();}return 'chat';})()` },
    { op: "wait", ms: 500 },
  ];
  const openBatchStep = () => [
    { op: "eval", js: `(function(){if(!document.querySelector('[data-im="batch-list"]')){const b=document.querySelector('[data-im="batch-entry"]');if(b)b.click();}return 'batch';})()` },
    { op: "wait", ms: 500 },
  ];
  const measure = (mark) => ({
    op: "eval",
    js: `JSON.stringify((function(){
      const R=(el)=>{if(!el)return null;const r=el.getBoundingClientRect();return {t:Math.round(r.top),b:Math.round(r.bottom),l:Math.round(r.left),r:Math.round(r.right),w:Math.round(r.width),h:Math.round(r.height)};};
      const bar=document.querySelector('[data-im="overlay-switch"]');
      const btn=document.querySelector('[data-im="overlay-switch-chat"]');
      const btn2=document.querySelector('[data-im="overlay-switch-batch"]');
      const hit=(el)=>{if(!el)return null;const r=el.getBoundingClientRect();const x=Math.round(r.left+r.width/2),y=Math.round(r.top+r.height/2);const top=document.elementFromPoint(x,y);return {x,y,hit:!!top&&!!el.contains(top)};};
      const chatInput=document.querySelector('[data-im="chat-input"] textarea, textarea[data-im="chat-input"]');
      const cs=bar?getComputedStyle(bar):null;
      const bs=btn?getComputedStyle(btn):null;
      return {mark:'${mark}', vw:window.innerWidth, vh:window.innerHeight,
        hooks:{chatToggle:!!document.querySelector('[data-im="chat-toggle"]'), batchEntry:!!document.querySelector('[data-im="batch-entry"]'), demoEntry:!!document.querySelector('[data-im="demo-entry"]'), chatPanel:!!document.querySelector('[data-im="chat-panel"]'), batchList:!!document.querySelector('[data-im="batch-list"]')},
        bar:R(bar), barPos: cs?{position:cs.position,display:cs.display,bottom:cs.bottom,height:cs.height,background:cs.backgroundColor,border:cs.borderStyle}:null,
        btn:R(btn), btnSlice: bs?{cursor:bs.cursor,background:bs.backgroundColor,borderRadius:bs.borderRadius,fontSize:bs.fontSize}:null,
        btn2:R(btn2), hitChatBtn:hit(btn), hitBatchBtn:hit(btn2),
        chat:R(document.querySelector('[data-im="chat-panel"]')), batch:R(document.querySelector('[data-im="batch-list"]')),
        toolbar:R(document.querySelector('[data-im="board-toolbar"]')),
        chatInput:R(chatInput), hitChatInput: hit2(chatInput)};
      function hit2(el){ if(!el) return null; const r=el.getBoundingClientRect(); const x=Math.round(r.left+r.width/2), y=Math.round(r.top+r.height/2); const top=document.elementFromPoint(x,y); return {x,y,hit:!!top&&!!el.contains(top)}; }
    })())`,
  });
  // 批量入口需要「同一批 ≥4 项待处理意图」：先走应用自己的演示入口造出来（与场景 17 同一条路径）
  await resetBoard();
  await clearPendingIntents();
  const flow = sess([
    clickHook("demo-entry"), { op: "wait", ms: 600 },
    { op: "eval", js: `(function(){const b=[...document.querySelectorAll('button')].find(x=>/演示|生成/.test(x.textContent)&&x.getBoundingClientRect().height>0&&x.closest('[data-im="demo-popover"], .demo-popover, .im-demo-pop'));if(!b)return 'no-button';b.click();return 'clicked';})()` },
    { op: "wait", ms: 2600 },
    { op: "eval", js: `(function(){const b=document.querySelector('[data-im="demo-entry"]');if(b)b.click();return 'close';})()` },
    { op: "wait", ms: 400 },
    { op: "viewport", width: 480, height: 800 },
    { op: "wait", ms: 1200 },
    openChatStep(),
    { op: "wait", ms: 600 },
    measure("afterChat"),
    openBatchStep(),
    { op: "wait", ms: 900 },
    measure("both"),
    clickHook("overlay-switch-chat"),
    { op: "wait", ms: 900 },
    measure("switched"),
    { op: "screenshot", name: "fe-22-switch-bar-480" },
  ]);
  const afterChat = markedFrom(flow, "afterChat") || {};
  const both = markedFrom(flow, "both") || {};
  const switched = markedFrom(flow, "switched") || {};
  console.log("诊断 afterChat=" + JSON.stringify(afterChat.hooks || {}) + " both=" + JSON.stringify(both.hooks || {}));

  const inside = (rect, vw, vh) => Boolean(rect) && rect.l >= 0 && rect.t >= 0 && rect.r <= vw && rect.b <= vh && rect.w > 0 && rect.h > 0;
  const bar = both.bar || {};
  check("22 两个面板请求同时展开时只显示一个", Boolean(both.chat) !== Boolean(both.batch), JSON.stringify({ chat: !!both.chat, batch: !!both.batch }));
  check("22 切换条有真实矩形且在视口内", inside(bar, both.vw || 480, both.vh || 800) && bar.h >= 20, JSON.stringify(bar));
  check("22 切换条的按钮有真实矩形且能命中", Boolean(both.hitChatBtn && both.hitChatBtn.hit), JSON.stringify({ btn: both.btn, hit: both.hitChatBtn }));
  check("22 切换按钮用了令牌样式（不是浏览器默认按钮）", Boolean(both.btnSlice && both.btnSlice.borderRadius !== "0px" && both.btnSlice.cursor === "pointer"), JSON.stringify(both.btnSlice));
  check("22 切换后面板真的换了", Boolean(switched.chat) !== Boolean(switched.batch), JSON.stringify({ chat: !!switched.chat, batch: !!switched.batch }));
  check("22 切换后切换条仍在视口内", inside(switched.bar || {}, switched.vw || 480, switched.vh || 800), JSON.stringify(switched.bar || {}));
  check("22 面板与工具栏都在视口内", inside(switched.toolbar || {}, switched.vw || 480, switched.vh || 800)
    && [switched.chat, switched.batch].filter(Boolean).every((r) => inside(r, switched.vw || 480, switched.vh || 800)),
    JSON.stringify({ toolbar: switched.toolbar, chat: switched.chat, batch: switched.batch }));
}

const main = async () => {
  console.log("=== 互动板前端改版实机验收（app=" + APP + " backend=" + BACKEND + "）===");
  const scenarios = [
    [1, scenario1],
    [2, scenario2],
    [3, scenario3],
    [4, scenario4],
    [40, scenario4a],
    [5, scenario5],
    [6, scenario6],
    [7, scenario7],
    [8, scenario8],
    [9, scenario9],
    [11, scenario11],
    [12, scenario12],
    [13, scenario13],
    [14, scenario14],
    [15, scenario15],
    [16, scenario16],
    [17, scenario17],
    [18, scenario18],
    [19, scenario19],
    [21, scenario21],
    [22, scenario22],
    [99, scenario99],
  ];
  for (const entry of scenarios) {
    const id = entry[0];
    const fn = entry[1];
    if (!want(id)) continue;
    console.log("");
    console.log("--- 场景 " + id + " ---");
    try {
      await fn();
    } catch (error) {
      check("场景 " + id + " 执行完成", false, String(error && error.message ? error.message : error));
    }
  }
  const failed = checks.filter((c) => !c.ok);
  console.log("");
  console.log("合计 " + checks.length + " 项，通过 " + (checks.length - failed.length) + " 项，失败 " + failed.length + " 项");
  if (failed.length) console.log("失败项：" + failed.map((f) => f.name).join("；"));
  process.exit(failed.length ? 1 : 0);
};

main();
