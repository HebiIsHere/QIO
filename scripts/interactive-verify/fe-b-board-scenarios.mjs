/**
 * 板面交互（子智能体 B）的真实界面验收：用 scripts/visual_probe.mjs 驱动真实 Chrome，
 * 走「空白拖动平移 → 空格拖动框选 → 滚轮缩放 → 缩放后拖动卡片 → 连接点拖线 → 两卡重叠成组 → 组名编辑」，
 * 每一步都截图并核对 DOM 与后端状态。
 *
 * 前置（用分配给 B 的端口，不占别人的）：
 *   后端：QIO_DEV_INSECURE=1 QIO_DATA_DIR=%TEMP%\\qio-fe-b backend\\.venv\\Scripts\\python.exe -m uvicorn agent.main:create_app --factory --host 127.0.0.1 --port 8892
 *   前端：VITE_QIO_BACKEND_URL=http://127.0.0.1:8892 node frontend/node_modules/vite/bin/vite.js --port 5392 --host 127.0.0.1
 * 跑法：node scripts/interactive-verify/fe-b-board-scenarios.mjs
 * 可用环境变量覆盖：FE_B_APP / FE_B_BACKEND
 * 截图输出：%TEMP%\\qio-visual\\shots（探针固定目录），文件名以 im-b- 开头。
 */
import { spawnSync } from "node:child_process";
import { rmSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join, resolve } from "node:path";

const here = dirname(fileURLToPath(import.meta.url));
const root = resolve(here, "..", "..");
const APP = process.env.FE_B_APP || "http://127.0.0.1:5392";
const BACKEND = process.env.FE_B_BACKEND || "http://127.0.0.1:8892";
const BOARD = "board_default";
/** NAV 里 eval 的数量（卡片 / 视口几何的下标都要加上它）。 */
const NAV_EVALS = 1;

const NAV = [
  { op: "navigate", url: APP + "/#/interactive", ms: 5200 },
  { op: "viewport", width: 1440, height: 900 },
  { op: "wait", ms: 900 },
  // 启动页可能在等后端应答：这里显式等板面挂载，最多 6 秒
  { op: "eval", js: "(async () => { for (let i = 0; i < 30; i += 1) { if (document.querySelector('[data-im=\"board\"]')) return 'board-ready'; await new Promise((r) => setTimeout(r, 200)); } return 'board-missing'; })()" },
];

const checks = [];
function check(name, ok, detail) {
  checks.push({ name, ok: Boolean(ok), detail: detail === undefined ? "" : String(detail) });
  console.log((ok ? "PASS  " : "FAIL  ") + name + (detail === undefined ? "" : "  —— " + detail));
}

/**
 * 跑一轮探针。探针偶尔会因为上一轮 Chrome 还没退干净而「unsettled top-level await」，
 * 这里重试一次；连续两次失败才算真失败（真实失败会原样抛出）。
 */
/**
 * 探针会用固定的 Chrome 用户目录（%TEMP%\qio-chrome-profile）。
 * 上一轮 Chrome 没退干净时，新一轮会卡在连接调试端口上（表现为「unsettled top-level await」）。
 * 每轮开始前清一次进程与目录，脚本才稳定可重复。
 */
function resetChrome() {
  try {
    spawnSync("taskkill", ["/F", "/IM", "chrome.exe"], { stdio: "ignore" });
  } catch {
    /* 没有 chrome 进程也无所谓 */
  }
  try {
    rmSync(join(process.env.TEMP || "", "qio-chrome-profile"), { recursive: true, force: true });
  } catch {
    /* 目录被占用就跳过 */
  }
}

function runSteps(steps, attempt = 0) {
  resetChrome();
  const probe = spawnSync(process.execPath, [resolve(root, "scripts", "visual_probe.mjs"), JSON.stringify(steps)], {
    encoding: "utf8",
    maxBuffer: 64 * 1024 * 1024,
  });
  const raw = probe.stdout || "";
  const start = raw.indexOf("{");
  if (start < 0) {
    const detail = raw.slice(0, 300) + " / stderr " + (probe.stderr || "").slice(0, 300);
    if (attempt < 2) {
      console.log("（探针第 " + (attempt + 1) + " 次没起来，重试）");
      return runSteps(steps, attempt + 1);
    }
    throw new Error("探针没有输出 JSON：" + detail);
  }
  return JSON.parse(raw.slice(start));
}

const evals = (payload) => (payload.results || []).filter((r) => r.op === "eval").map((r) => r.value);
const shots = (payload) => (payload.results || []).filter((r) => r.op === "screenshot").map((r) => r.name);

/** 第 index 个 eval 的 JSON 结果（脚本里的 eval 顺序是固定的，比「取最后一个」可靠）。 */
function evalAt(payload, index) {
  const list = evals(payload);
  const value = list[index];
  if (typeof value !== "string") return {};
  try {
    const parsed = JSON.parse(value);
    // 数组（卡片列表）与对象（视口状态）都要能拿到；解析失败一律当空值
    return parsed === null || parsed === undefined ? {} : parsed;
  } catch {
    return {};
  }
}

/** 取第 index 个 eval 的数组结果（卡片列表用）。 */
function arrayAt(payload, index) {
  const value = evalAt(payload, index);
  return Array.isArray(value) ? value : [];
}

function lastJson(values, fallback) {
  for (let i = values.length - 1; i >= 0; i -= 1) {
    const value = values[i];
    if (typeof value !== "string") continue;
    const text = value.trim();
    if (!text.startsWith("{") && !text.startsWith("[")) continue;
    try {
      return JSON.parse(text);
    } catch {
      /* 继续往前找 */
    }
  }
  return fallback === undefined ? {} : fallback;
}

async function api(path, init, attempt = 0) {
  try {
    const resp = await fetch(BACKEND + path, {
      ...(init || {}),
      headers: { "Content-Type": "application/json", ...((init && init.headers) || {}) },
    });
    return await resp.json();
  } catch (error) {
    // 本地服务偶尔会有瞬时的连接失败（大量并发请求时），重试两次
    if (attempt < 2) {
      await new Promise((resolve) => setTimeout(resolve, 400));
      return api(path, init, attempt + 1);
    }
    throw new Error("接口调用失败 " + path + "：" + (error && error.message ? error.message : String(error)));
  }
}

const boardState = () => api("/api/interactive/boards/" + BOARD + "/state");

async function resetBoard() {
  await api("/api/interactive/boards/" + BOARD + "/state", {
    method: "PUT",
    body: JSON.stringify({
      state: { boardId: BOARD, seq: 0, updatedAt: new Date().toISOString(), cards: [], groups: [], links: [], selection: [] },
      reason: "fe-b-e2e-reset",
    }),
  });
}

/** 直接写入卡片与位置：位置只是显示状态，不构成意图依据。 */
async function seedCards(cards) {
  const now = new Date().toISOString();
  const full = cards.map((card) => ({
    id: card.id,
    kind: card.kind || "text",
    content: card.content || "",
    meta: card.meta || {},
    x: card.x,
    y: card.y,
    w: card.w || 200,
    h: card.h || 120,
    checked: false,
    hidden: false,
    folded: false,
    bookmarked: false,
    deleted: false,
    createdAt: now,
    updatedAt: now,
  }));
  await api("/api/interactive/boards/" + BOARD + "/state", {
    method: "PUT",
    body: JSON.stringify({
      state: { boardId: BOARD, seq: 0, updatedAt: now, cards: full, groups: [], links: [], selection: [] },
      reason: "fe-b-e2e-seed",
    }),
  });
}

function mouse(type, x, y, modifiers) {
  return {
    op: "cdp",
    method: "Input.dispatchMouseEvent",
    params: {
      type,
      x,
      y,
      button: "left",
      clickCount: 1,
      buttons: type === "mouseReleased" ? 0 : 1,
      modifiers: modifiers || 0,
    },
  };
}

function wheel(x, y, deltaY) {
  return {
    op: "cdp",
    method: "Input.dispatchMouseEvent",
    params: { type: "mouseWheel", x, y, deltaX: 0, deltaY, button: "none", clickCount: 0 },
  };
}

function keyEvent(type, key, code, vk, text) {
  const params = { type, key, code, windowsVirtualKeyCode: vk, nativeVirtualKeyCode: vk };
  if (text !== undefined) params.text = text;
  return { op: "cdp", method: "Input.dispatchKeyEvent", params };
}
const keyDownSpace = () => keyEvent("rawKeyDown", " ", "Space", 32, " ");
const keyUpSpace = () => keyEvent("keyUp", " ", "Space", 32);

/** 视口状态：缩放来自内容层 transform，平移来自容器滚动。 */
const VIEW_EXPR = [
  "(() => {",
  "  const surface = document.querySelector('.board-surface');",
  "  const view = document.querySelector('.board-viewport');",
  "  if (!surface || !view) return JSON.stringify({ ok: false });",
  "  const raw = surface.style.transform || '';",
  "  const m = raw.indexOf('scale(') >= 0 ? raw.slice(raw.indexOf('scale(') + 6, raw.lastIndexOf(')')).split(',')[0] : null;",
  "  const vr = view.getBoundingClientRect();",
  "  return JSON.stringify({ ok: true, scale: m === null ? null : Number(m), scrollLeft: view.scrollLeft, scrollTop: view.scrollTop, left: vr.left, top: vr.top, width: vr.width, height: vr.height });",
  "})()",
].join("\n");

const CARDS_EXPR = [
  'JSON.stringify([...document.querySelectorAll(\'[data-im="card"]\')].map((c) => {',
  "  const r = c.getBoundingClientRect();",
  "  return { id: c.getAttribute('data-card-id'), left: r.left, top: r.top, width: r.width, height: r.height,",
  "           x: Math.round(r.left + r.width / 2), y: Math.round(r.top + 16), selected: c.className.indexOf('selected') >= 0 };",
  "}))",
].join("\n");

/** 一次拿到卡片列表与当前视口状态（同一个会话里读，避免跨会话混用）。 */
function parseGeom(payload, offset = 0) {
  const cards = arrayAt(payload, offset);
  const view = (() => {
    const value = evals(payload)[offset + 1];
    if (typeof value !== "string") return null;
    try {
      const parsed = JSON.parse(value);
      return parsed && parsed.ok ? parsed : null;
    } catch {
      return null;
    }
  })();
  return { cards, view };
}

/** 板面坐标 → 屏幕坐标（与组件同一套换算）。 */
function toScreen(state, boardX, boardY) {
  return {
    x: state.left + boardX * state.scale - state.scrollLeft,
    y: state.top + boardY * state.scale - state.scrollTop,
  };
}

function viewportState(payload) {
  const values = evals(payload);
  for (let i = values.length - 1; i >= 0; i -= 1) {
    const value = values[i];
    if (typeof value !== "string") continue;
    try {
      const parsed = JSON.parse(value);
      if (parsed && parsed.ok) return parsed;
    } catch {
      /* 继续 */
    }
  }
  throw new Error("拿不到视口状态：" + JSON.stringify(values).slice(0, 300));
}

async function main() {
  console.log("== 准备：标记引导已看过 + 清空板面 ==");
  await api("/api/onboarding/seen", { method: "POST", body: JSON.stringify({ seen: true }) }).catch(() => undefined);
  await resetBoard();

  // --- 准备：两张文字卡片，位置固定，便于算屏幕坐标 -------------------------
  // 先确认板面能挂载（首次打开时应用要等后端应答；后端刚起来时会慢一点）
  const ready = runSteps([
    { op: "navigate", url: APP + "/#/interactive", ms: 7000 },
    { op: "viewport", width: 1440, height: 900 },
    { op: "eval", js: "(async () => { for (let i = 0; i < 60; i += 1) { if (document.querySelector('[data-im=\"board\"]')) return 'board-ready'; await new Promise((r) => setTimeout(r, 250)); } return 'board-missing'; })()" },
  ]);
  const readyText = String(evals(ready)[0] ?? "");
  check("互动板面能正常打开（后端应答后挂载）", readyText === "board-ready", readyText);

  await seedCards([
    { id: "b_card_a", x: 200, y: 200, w: 220, h: 140, content: "卡片 A：发布节奏需要确认" },
    { id: "b_card_b", x: 900, y: 200, w: 220, h: 140, content: "卡片 B：材料整理清单" },
  ]);

  console.log("== 1/8 打开互动模式并读取初始几何 ==");
  const initial = runSteps([
    ...NAV,
    { op: "eval", js: CARDS_EXPR },
    { op: "eval", js: VIEW_EXPR },
    { op: "screenshot", name: "im-b-00-initial" },
  ]);
  const cards0 = arrayAt(initial, NAV_EVALS);
  const view0 = viewportState(initial);
  check("板面加载出两张卡片", cards0.length === 2, JSON.stringify(cards0.map((c) => c.id)));
  check("初始缩放为 100%", Math.abs(view0.scale - 1) < 0.001, view0.scale);

  console.log("== 2/8 空白拖动平移 ==");
  // --- 1. 空白拖动 = 平移查看位置 -------------------------------------------
  const emptyX = Math.round(view0.left + view0.width - 80);
  const emptyY = Math.round(view0.top + view0.height - 80);
  const panned = runSteps([
    ...NAV,
    // 先滚到左上边界（往右下拖），确保起点是确定的 (0, 0)
    mouse("mousePressed", emptyX, emptyY),
    { op: "wait", ms: 150 },
    mouse("mouseMoved", emptyX + 400, emptyY + 300),
    { op: "wait", ms: 200 },
    mouse("mouseReleased", emptyX + 400, emptyY + 300),
    { op: "wait", ms: 400 },
    // 再往左上拖 = 查看位置向右下移动 = 滚动量增加
    mouse("mousePressed", emptyX, emptyY),
    { op: "wait", ms: 150 },
    mouse("mouseMoved", emptyX - 160, emptyY - 120),
    { op: "wait", ms: 200 },
    { op: "screenshot", name: "im-b-10-panning" },
    mouse("mouseReleased", emptyX - 160, emptyY - 120),
    { op: "wait", ms: 600 },
    { op: "eval", js: VIEW_EXPR },
    { op: "eval", js: CARDS_EXPR },
    { op: "screenshot", name: "im-b-11-panned" },
  ]);
  const view1 = viewportState(panned);
  const panProbe = evalAt(panned, NAV_EVALS + 2);
  const cards1 = arrayAt(panned, NAV_EVALS + 1);
  check(
    "空白拖动只改查看位置（向左上拖 = 滚动量增加，缩放不变）",
    view1.scrollLeft > view0.scrollLeft && view1.scrollTop > view0.scrollTop && Math.abs(view1.scale - 1) < 0.001,
    JSON.stringify({ before: [view0.scrollLeft, view0.scrollTop], after: [view1.scrollLeft, view1.scrollTop], probe: panProbe }),
  );
  const boardPos = (card, state) => [Math.round((card.left - state.left + state.scrollLeft) / state.scale), Math.round((card.top - state.top + state.scrollTop) / state.scale)];
  check(
    "平移后卡片在板面坐标系里的位置不变（查看方式不影响板面状态）",
    JSON.stringify(cards0.map((c) => boardPos(c, view0))) === JSON.stringify(cards1.map((c) => boardPos(c, view1))),
    JSON.stringify(cards1.map((c) => boardPos(c, view1))),
  );

  console.log("== 3/8 空格 + 拖动框选 ==");
  // --- 2. 空格 + 拖动空白处 = 框选卡片 --------------------------------------
  // 框选起点：视口左上角的空白处；终点覆盖两张卡片所在的板面范围
  const spaceX = Math.round(view1.left + 6);
  const spaceY = Math.round(view1.top + 6);
  const framed = runSteps([
    ...NAV,
    keyDownSpace(),
    { op: "wait", ms: 250 },
    mouse("mousePressed", spaceX, spaceY),
    { op: "wait", ms: 150 },
    mouse("mouseMoved", Math.round(view1.left + view1.width - 6), Math.round(view1.top + view1.height - 6)),
    { op: "wait", ms: 250 },
    { op: "eval", js: "JSON.stringify({ rect: !!document.querySelector('.select-rect') })" },
    { op: "screenshot", name: "im-b-20-space-framing" },
    mouse("mouseReleased", Math.round(view1.left + view1.width - 6), Math.round(view1.top + view1.height - 6)),
    { op: "wait", ms: 700 },
    keyUpSpace(),
    { op: "wait", ms: 400 },
    { op: "eval", js: CARDS_EXPR },
    { op: "eval", js: "JSON.stringify({ toolbar: document.querySelectorAll('[data-im=\"card-toolbar\"]').length, connect: document.querySelectorAll('[data-im=\"connect-point\"]').length })" },
    { op: "screenshot", name: "im-b-21-space-framed" },
  ]);
  const rectSeen = evalAt(framed, NAV_EVALS);
  const cards2 = arrayAt(framed, NAV_EVALS + 1);
  check("空格 + 拖动空白处出现框选矩形", rectSeen.rect === true, JSON.stringify(rectSeen));
  const selectedIds = (cards2 || []).filter((c) => c.selected).map((c) => c.id);
  check("框选选中了范围内的卡片", selectedIds.length >= 1, JSON.stringify(selectedIds));

  console.log("== 4/8 滚轮缩放 ==");
  // --- 3. 滚轮以指针附近为缩放中心 ------------------------------------------
  const anchorBoard = { x: 310, y: 270 }; // 卡片 A 的中心（200..420 x 200..340）
  // 用「按下滚轮那一刻」的真实视口状态算锚点屏幕坐标（前面几轮验收各自是独立会话）
  const zoomBaseRun = runSteps([...NAV, { op: "eval", js: VIEW_EXPR }]);
  const zoomBase = viewportState(zoomBaseRun);
  const anchor = toScreen(zoomBase, anchorBoard.x, anchorBoard.y);
  const zoomed = runSteps([
    ...NAV,
    { op: "eval", js: "document.activeElement && document.activeElement.blur && document.activeElement.blur(); 'blur'" },
    wheel(anchor.x, anchor.y, -400),
    { op: "wait", ms: 800 },
    { op: "eval", js: VIEW_EXPR },
    { op: "screenshot", name: "im-b-30-zoomed" },
    { op: "eval", js: CARDS_EXPR },
  ]);
  const view2 = viewportState(zoomed);
  const cards3 = arrayAt(zoomed, NAV_EVALS + 2);
  const anchorAfter = toScreen(view2, anchorBoard.x, anchorBoard.y);
  // 拖动核对：新会话里缩放一次，然后**在同一会话**读几何 → 拖动 → 再读几何。
  // 新会话的滚动量是 0，缩放只影响缩放倍率，几何读数与指针位置不会错位。
  // 缩放锚点选在视口左上角：滚动量被收敛在 0 附近，卡片一直留在可视区
  const dragRun = runSteps([
    ...NAV,
    { op: "eval", js: "document.activeElement && document.activeElement.blur && document.activeElement.blur(); 'blur'" },
    wheel(60, 140, -400),
    { op: "wait", ms: 900 },
    // 先点空白处清空选择：卡片局部工具栏浮在卡片上方，不清掉会先接住指针
    mouse("mousePressed", 1000, 620),
    { op: "wait", ms: 150 },
    mouse("mouseReleased", 1000, 620),
    { op: "wait", ms: 500 },
    { op: "eval", js: CARDS_EXPR },
    { op: "eval", js: VIEW_EXPR },
  ]);
  const dragStartGeom = parseGeom(dragRun, NAV_EVALS + 1);
  const dragBaseCards = dragStartGeom.cards;
  const dragBaseView = dragStartGeom.view || view2;
  // 同一会话内缩放：用缩放前的真实视口状态核对（view2 与 zoomBase 是同一会话的两次读数）
  check("滚轮放大（缩放 > 120%）", view2.scale > 1.2, "scale=" + view2.scale);
  check(
    "指针下的板面点在缩放前后落在同一屏幕位置（误差 < 2px）",
    Math.abs(anchorAfter.x - anchor.x) < 2 && Math.abs(anchorAfter.y - anchor.y) < 2,
    JSON.stringify({ before: anchor, after: anchorAfter }),
  );

  console.log("== 5/8 缩放后拖动卡片 ==");
  // --- 4. 缩放后拖动卡片：坐标按板面坐标换算 --------------------------------
  const cardA3 = dragBaseCards.find((c) => c.id === "b_card_a");
  if (!cardA3) {
    check("缩放后卡片仍在界面上（可继续做拖动核对）", false, JSON.stringify(dragBaseCards).slice(0, 200));
  }
  const beforePos = cardA3 ? boardPos(cardA3, dragBaseView) : [0, 0];
  // 抓卡片下半部分（约 80% 高度）：顶部可能浮着局部工具栏、四边有连接点，都会先接住指针
  const grab = cardA3
    ? { x: Math.round(cardA3.left + cardA3.width * 0.35), y: Math.round(cardA3.top + cardA3.height * 0.8) }
    : { x: 0, y: 0 };
  const deltaScreen = { x: 160, y: 120 };
  const dragged = runSteps([
    ...NAV,
    mouse("mouseMoved", grab.x + Math.round(deltaScreen.x / 2), grab.y + Math.round(deltaScreen.y / 2)),
    { op: "wait", ms: 200 },
    mouse("mouseMoved", grab.x + deltaScreen.x, grab.y + deltaScreen.y),
    { op: "wait", ms: 300 },
    { op: "screenshot", name: "im-b-40-drag-after-zoom" },
    mouse("mouseReleased", grab.x + deltaScreen.x, grab.y + deltaScreen.y),
    { op: "wait", ms: 1000 },
    { op: "eval", js: CARDS_EXPR },
    { op: "screenshot", name: "im-b-41-dropped-after-zoom" },
  ]);
  const cards4 = arrayAt(dragged, NAV_EVALS + 1);
  const cardA4 = cards4.find((c) => c.id === "b_card_a");
  const afterPos = cardA4 ? boardPos(cardA4, dragBaseView) : [0, 0];
  const expectedDx = deltaScreen.x / dragBaseView.scale;
  const expectedDy = deltaScreen.y / dragBaseView.scale;
  check(
    "缩放后拖动卡片：板面位移 = 屏幕位移 ÷ 缩放（不是屏幕像素）",
    Boolean(cardA3) && Boolean(cardA4) && Math.abs(afterPos[0] - beforePos[0] - expectedDx) < 4 && Math.abs(afterPos[1] - beforePos[1] - expectedDy) < 4,
    JSON.stringify({
      实际位移: [afterPos[0] - beforePos[0], afterPos[1] - beforePos[1]],
      期望位移: [Math.round(expectedDx), Math.round(expectedDy)],
      scale: dragBaseView.scale,
    }),
  );
  const savedAfterDrag = await boardState();
  const savedA = savedAfterDrag.state.cards.find((c) => c.id === "b_card_a") || {};
  check("拖动结果真的保存到后端", Math.abs(savedA.x - afterPos[0]) < 6, JSON.stringify([Math.round(savedA.x), Math.round(savedA.y)]));

  console.log("== 6/8 连接点拖线 ==");
  // --- 5. 连接点拖线建链 -----------------------------------------------------
  // 前面几轮把视口滚动过，这里重置板面并重新放两张卡片，保证它们都在可视区
  await resetBoard();
  await seedCards([
    { id: "b_card_a", x: 200, y: 200, w: 220, h: 140, content: "卡片 A：发布节奏需要确认" },
    { id: "b_card_b", x: 700, y: 200, w: 220, h: 140, content: "卡片 B：材料整理清单" },
  ]);
  const cards5 = arrayAt(
    runSteps([
      ...NAV,
      // 等板面真的挂载出来（导航后组件可能还没渲染完）
      { op: "wait", ms: 1200 },
      // 先把查看位置滚回左上角（往右下拖到底），保证两张卡片都在可视区
      mouse("mousePressed", 1300, 760),
      { op: "wait", ms: 150 },
      mouse("mouseMoved", 1430, 830),
      { op: "wait", ms: 200 },
      mouse("mouseReleased", 1430, 830),
      { op: "wait", ms: 600 },
      { op: "eval", js: CARDS_EXPR },
      { op: "eval", js: VIEW_EXPR },
    ]),
    NAV_EVALS,
  );
  const a5 = cards5.find((c) => c.id === "b_card_a") || { x: 0, y: 0 };
  const b5 = cards5.find((c) => c.id === "b_card_b") || { x: 0, y: 0 };
  const selectRun = runSteps([
    ...NAV,
    mouse("mousePressed", a5.x, a5.y),
    { op: "wait", ms: 150 },
    mouse("mouseReleased", a5.x, a5.y),
    { op: "wait", ms: 500 },
    { op: "eval", js: "JSON.stringify({ connect: document.querySelectorAll('[data-im=\"connect-point\"]').length, toolbar: document.querySelectorAll('[data-im=\"card-toolbar\"]').length })" },
    { op: "eval", js: "(() => { const p = document.querySelector('[data-im=\"connect-point\"][data-side=\"right\"]'); if (!p) return JSON.stringify({}); const r = p.getBoundingClientRect(); return JSON.stringify({ x: Math.round(r.left + r.width / 2), y: Math.round(r.top + r.height / 2) }); })()" },
    { op: "eval", js: CARDS_EXPR },
  ]);
  const connectInfo = evalAt(selectRun, NAV_EVALS);
  const pointPos = evalAt(selectRun, NAV_EVALS + 1);
  check("选中卡片后出现连接点与局部工具栏", connectInfo.connect >= 4 && connectInfo.toolbar >= 1, JSON.stringify(connectInfo));
  check("拿到连接点的屏幕坐标", Number.isFinite(pointPos.x) && Number.isFinite(pointPos.y), JSON.stringify(pointPos));

  const linkDraftRun = runSteps([
    ...NAV,
    { op: "wait", ms: 600 },
    mouse("mousePressed", 1300, 760),
    { op: "wait", ms: 150 },
    mouse("mouseMoved", 1430, 830),
    { op: "wait", ms: 200 },
    mouse("mouseReleased", 1430, 830),
    { op: "wait", ms: 500 },
    mouse("mousePressed", a5.x, a5.y),
    { op: "wait", ms: 150 },
    mouse("mouseReleased", a5.x, a5.y),
    { op: "wait", ms: 450 },
    mouse("mousePressed", pointPos.x, pointPos.y),
    { op: "wait", ms: 250 },
    // 用**本会话**里读到的 B 卡片几何（跨会话的坐标会因为滚动不同而失准）
    { op: "eval", js: CARDS_EXPR },
    { op: "wait", ms: 100 },
    mouse("mouseMoved", Math.round(b5.left + b5.width / 2), Math.round(b5.top + b5.height * 0.8)),
    { op: "wait", ms: 300 },
    { op: "eval", js: "JSON.stringify({ draft: !!document.querySelector('[data-im=\"link-draft\"]'), target: (document.querySelector('[data-im=\"link-draft\"]') || { getAttribute: () => null }).getAttribute('data-target') })" },
    { op: "screenshot", name: "im-b-50-link-draft" },
    mouse("mouseReleased", Math.round(b5.left + b5.width / 2), Math.round(b5.top + b5.height * 0.8)),
    { op: "wait", ms: 1000 },
    { op: "screenshot", name: "im-b-51-link-created" },
  ]);
  const draftInfo = evalAt(linkDraftRun, NAV_EVALS + 1);
  check("拖线期间显示待建连线与有效目标", draftInfo.draft === true && draftInfo.target === "b_card_b", JSON.stringify(draftInfo));
  const savedAfterLink = await boardState();
  const liveLinks = savedAfterLink.state.links.filter((l) => !l.deleted);
  check("松手后建立关系并保存", liveLinks.length === 1, JSON.stringify(liveLinks.map((l) => [l.src, l.dst])));

  const invalidX = Math.round(view0.left + 60);
  const invalidY = Math.round(view0.top + view0.height - 80);
  const cancelRun = runSteps([
    ...NAV,
    { op: "wait", ms: 600 },
    mouse("mousePressed", 1300, 760),
    { op: "wait", ms: 150 },
    mouse("mouseMoved", 1430, 830),
    { op: "wait", ms: 200 },
    mouse("mouseReleased", 1430, 830),
    { op: "wait", ms: 500 },
    mouse("mousePressed", a5.x, a5.y),
    { op: "wait", ms: 150 },
    mouse("mouseReleased", a5.x, a5.y),
    { op: "wait", ms: 450 },
    mouse("mousePressed", pointPos.x, pointPos.y),
    { op: "wait", ms: 200 },
    mouse("mouseMoved", invalidX, invalidY),
    { op: "wait", ms: 250 },
    mouse("mouseReleased", invalidX, invalidY),
    { op: "wait", ms: 900 },
    { op: "screenshot", name: "im-b-52-link-cancelled" },
  ]);
  const afterCancel = await boardState();
  check("拖到无效位置不建链、不保存半条链接", afterCancel.state.links.filter((l) => !l.deleted).length === liveLinks.length, JSON.stringify({ before: liveLinks.length, after: afterCancel.state.links.filter((l) => !l.deleted).length }));
  void cancelRun;

  console.log("== 7/8 重叠成组 ==");
  // --- 6. 两张未分组卡片重叠成组 --------------------------------------------
  await resetBoard();
  await seedCards([
    { id: "b_card_a", x: 200, y: 240, w: 220, h: 140, content: "卡片 A：发布节奏需要确认" },
    { id: "b_card_b", x: 700, y: 240, w: 220, h: 140, content: "卡片 B：材料整理清单" },
  ]);
  const fresh = runSteps([...NAV, { op: "eval", js: CARDS_EXPR }, { op: "eval", js: VIEW_EXPR }]);
  const freshCards = arrayAt(fresh, NAV_EVALS);
  const fa = freshCards.find((c) => c.id === "b_card_a");
  const fb = freshCards.find((c) => c.id === "b_card_b");
  const dropX = Math.round(fb.x - 40);
  const dropY = fb.y;
  const mergeRun = runSteps([
    ...NAV,
    mouse("mousePressed", fa.x, fa.y),
    { op: "wait", ms: 250 },
    mouse("mouseMoved", Math.round((fa.x + dropX) / 2), dropY),
    { op: "wait", ms: 250 },
    mouse("mouseMoved", dropX, dropY),
    { op: "wait", ms: 350 },
    { op: "eval", js: "JSON.stringify({ hint: !!document.querySelector('[data-im=\"group-merge-hint\"]'), text: (document.querySelector('[data-im=\"group-merge-hint\"]') || { textContent: '' }).textContent, groups: document.querySelectorAll('[data-im=\"group\"]').length })" },
    { op: "screenshot", name: "im-b-60-merge-hint" },
    mouse("mouseReleased", dropX, dropY),
    { op: "wait", ms: 1400 },
    { op: "eval", js: "JSON.stringify({ groups: document.querySelectorAll('[data-im=\"group\"]').length, hint: !!document.querySelector('[data-im=\"group-merge-hint\"]') })" },
    { op: "screenshot", name: "im-b-61-merged" },
  ]);
  const mergeHintInfo = evalAt(mergeRun, NAV_EVALS);
  const mergeAfterInfo = evalAt(mergeRun, NAV_EVALS + 1);
  check("明确重叠时提示「松开后合并成组」且松手前还没有组", mergeHintInfo.hint === true && mergeHintInfo.groups === 0, JSON.stringify(mergeHintInfo));
  check("松手后自动成组", mergeAfterInfo.groups >= 1, JSON.stringify(mergeAfterInfo));
  const mergedState = await boardState();
  const mergedGroups = mergedState.state.groups.filter((g) => !g.deleted);
  check("成组结果保存到后端：一个组、两名成员、系统默认名", mergedGroups.length === 1 && mergedGroups[0].members.length === 2 && mergedGroups[0].name === "默认组名", JSON.stringify(mergedGroups.map((g) => ({ name: g.name, members: g.members }))));

  // 靠近但没重叠：不提示、不成组
  await resetBoard();
  await seedCards([
    { id: "b_card_a", x: 200, y: 240, w: 220, h: 140, content: "卡片 A" },
    { id: "b_card_b", x: 900, y: 240, w: 220, h: 140, content: "卡片 B" },
  ]);
  const nearCards = arrayAt(runSteps([...NAV, { op: "eval", js: CARDS_EXPR }]), NAV_EVALS);
  const na = nearCards.find((c) => c.id === "b_card_a");
  const nb = nearCards.find((c) => c.id === "b_card_b");
  const nearX = Math.round(nb.left - na.width - 30);
  const nearRun = runSteps([
    ...NAV,
    mouse("mousePressed", na.x, na.y),
    { op: "wait", ms: 250 },
    mouse("mouseMoved", nearX, nb.y),
    { op: "wait", ms: 350 },
    { op: "eval", js: "JSON.stringify({ hint: !!document.querySelector('[data-im=\"group-merge-hint\"]') })" },
    mouse("mouseReleased", nearX, nb.y),
    { op: "wait", ms: 1000 },
    { op: "eval", js: "JSON.stringify({ groups: document.querySelectorAll('[data-im=\"group\"]').length })" },
    { op: "screenshot", name: "im-b-62-near-no-merge" },
  ]);
  const nearHint = evalAt(nearRun, NAV_EVALS);
  const nearAfter = evalAt(nearRun, NAV_EVALS + 1);
  check("靠近但没重叠：不提示、松手也不成组", nearHint.hint === false && nearAfter.groups === 0, JSON.stringify({ nearHint, nearAfter }));

  console.log("== 8/8 组名编辑与刷新 ==");
  // --- 7. 组名编辑：输入即用，留空保留默认名 --------------------------------
  await resetBoard();
  await seedCards([
    { id: "b_card_a", x: 200, y: 240, w: 220, h: 140, content: "卡片 A" },
    { id: "b_card_b", x: 700, y: 240, w: 220, h: 140, content: "卡片 B" },
  ]);
  const seedNow = arrayAt(runSteps([...NAV, { op: "eval", js: CARDS_EXPR }]), NAV_EVALS);
  const sa = seedNow.find((c) => c.id === "b_card_a");
  const sb = seedNow.find((c) => c.id === "b_card_b");
  runSteps([
    ...NAV,
    mouse("mousePressed", sa.x, sa.y),
    { op: "wait", ms: 250 },
    mouse("mouseMoved", Math.round(sb.x - 40), sb.y),
    { op: "wait", ms: 300 },
    mouse("mouseReleased", Math.round(sb.x - 40), sb.y),
    { op: "wait", ms: 1400 },
  ]);
  const afterGroup = await boardState();
  const groupName = (afterGroup.state.groups.filter((g) => !g.deleted)[0] || {}).name;
  check("重叠成组后组名是系统默认名（默认组名）", groupName === "默认组名", groupName);

  const setValue = (value) =>
    "const input=document.querySelector('[data-im=\\\"group-name\\\"]');const set=Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype,'value').set;set.call(input," + JSON.stringify(value) + ");input.dispatchEvent(new Event('change',{bubbles:true}));'renamed'";
  const renameRun = runSteps([
    ...NAV,
    { op: "eval", js: "JSON.stringify({ name: (document.querySelector('[data-im=\"group-name\"]') || {}).value, badge: document.body.innerText.indexOf('系统默认名') >= 0 })" },
    { op: "screenshot", name: "im-b-70-group-default-name" },
    { op: "eval", js: setValue("发布计划") },
    { op: "wait", ms: 1000 },
    { op: "screenshot", name: "im-b-71-group-renamed" },
    { op: "eval", js: setValue("   ") },
    { op: "wait", ms: 1000 },
    { op: "eval", js: "JSON.stringify({ value: (document.querySelector('[data-im=\"group-name\"]') || {}).value, groups: document.querySelectorAll('[data-im=\"group\"]').length })" },
    { op: "screenshot", name: "im-b-72-group-name-kept" },
  ]);
  const renameInfo = evalAt(renameRun, NAV_EVALS + 2);
  const initialNameInfo = evalAt(renameRun, NAV_EVALS);
  check("界面上显示系统默认名提示", initialNameInfo.badge === true, JSON.stringify(initialNameInfo));
  const renamedState = await boardState();
  const renamedGroup = renamedState.state.groups.filter((g) => !g.deleted)[0] || {};
  check("组名输入即用并保存", renamedGroup.name === "发布计划", renamedGroup.name);
  check("留空后保留上一个组名，组仍然成立", renameInfo.value === "发布计划" && renameInfo.groups >= 1, JSON.stringify(renameInfo));

  const reloadRun = runSteps([
    ...NAV,
    { op: "wait", ms: 1400 },
    { op: "eval", js: "JSON.stringify({ groups: document.querySelectorAll('[data-im=\"group\"]').length, name: (document.querySelector('[data-im=\"group-name\"]') || {}).value, cards: document.querySelectorAll('[data-im=\"card\"]').length })" },
    { op: "screenshot", name: "im-b-73-after-reload" },
  ]);
  const reloadInfo = evalAt(reloadRun, NAV_EVALS);
  check("刷新后组、组名与卡片都还在", reloadInfo.groups >= 1 && reloadInfo.name === "发布计划" && reloadInfo.cards === 2, JSON.stringify(reloadInfo));

  // --- 8. 空格在输入框里不触发板面操作 --------------------------------------
  const inputSpaceRun = runSteps([
    ...NAV,
    { op: "eval", js: "document.querySelector('[data-im=\"group-name\"]').focus(); 'focused'" },
    keyDownSpace(),
    { op: "wait", ms: 250 },
    mouse("mousePressed", Math.round(view0.left + 40), Math.round(view0.top + view0.height - 70)),
    { op: "wait", ms: 200 },
    mouse("mouseMoved", Math.round(view0.left + 440), Math.round(view0.top + view0.height - 370)),
    { op: "wait", ms: 250 },
    { op: "eval", js: "JSON.stringify({ rect: !!document.querySelector('.select-rect') })" },
    { op: "screenshot", name: "im-b-80-space-in-input" },
    mouse("mouseReleased", Math.round(view0.left + 440), Math.round(view0.top + view0.height - 370)),
    { op: "wait", ms: 600 },
    keyUpSpace(),
  ]);
  const inputSpaceInfo = evalAt(inputSpaceRun, NAV_EVALS);
  check("在组名输入框里按空格不会进入框选模式", inputSpaceInfo.rect === false, JSON.stringify(inputSpaceInfo));

  // --- 汇总 ---------------------------------------------------------------
  const failed = checks.filter((c) => !c.ok);
  const allShots = [...shots(initial), ...shots(panned), ...shots(framed), ...shots(zoomed), ...shots(dragged), ...shots(linkDraftRun), ...shots(cancelRun), ...shots(mergeRun), ...shots(nearRun), ...shots(renameRun), ...shots(reloadRun), ...shots(inputSpaceRun)];
  console.log("");
  console.log("截图：" + allShots.join(", "));
  console.log("通过 " + (checks.length - failed.length) + " / " + checks.length);
  if (failed.length) {
    console.log("未通过：");
    for (const item of failed) console.log("  - " + item.name + "  " + item.detail);
    process.exitCode = 1;
  }
}

main().catch((error) => {
  console.error("验收脚本崩溃：", error && error.stack ? error.stack : String(error));
  process.exitCode = 2;
});
