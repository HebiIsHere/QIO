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
import { fileURLToPath } from "node:url";
import { dirname, resolve } from "node:path";

const here = dirname(fileURLToPath(import.meta.url));
const root = resolve(here, "..", "..");
const APP = process.env.FE_B_APP || "http://127.0.0.1:5392";
const BACKEND = process.env.FE_B_BACKEND || "http://127.0.0.1:8892";
const BOARD = "board_default";
const NAV = [
  { op: "navigate", url: APP + "/#/interactive", ms: 4200 },
  { op: "viewport", width: 1440, height: 900 },
  { op: "wait", ms: 700 },
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
function runSteps(steps, attempt = 0) {
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
    return JSON.parse(value);
  } catch {
    return {};
  }
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

async function api(path, init) {
  const resp = await fetch(BACKEND + path, {
    ...(init || {}),
    headers: { "Content-Type": "application/json", ...((init && init.headers) || {}) },
  });
  return resp.json();
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
  await api("/api/onboarding/seen", { method: "POST", body: JSON.stringify({ seen: true }) }).catch(() => undefined);
  await resetBoard();

  // --- 准备：两张文字卡片，位置固定，便于算屏幕坐标 -------------------------
  await seedCards([
    { id: "b_card_a", x: 200, y: 200, w: 220, h: 140, content: "卡片 A：发布节奏需要确认" },
    { id: "b_card_b", x: 900, y: 200, w: 220, h: 140, content: "卡片 B：材料整理清单" },
  ]);

  const initial = runSteps([
    ...NAV,
    { op: "eval", js: CARDS_EXPR },
    { op: "eval", js: VIEW_EXPR },
    { op: "screenshot", name: "im-b-00-initial" },
  ]);
  const cards0 = evalAt(initial, 0);
  const view0 = viewportState(initial);
  check("板面加载出两张卡片", cards0.length === 2, JSON.stringify(cards0.map((c) => c.id)));
  check("初始缩放为 100%", Math.abs(view0.scale - 1) < 0.001, view0.scale);

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
    { op: "eval", js: "JSON.stringify({ start: [" + emptyX + "," + emptyY + "], atStart: (document.elementFromPoint(" + emptyX + "," + emptyY + ") || {}).className || null, zz: (document.querySelector('[data-im=\"zz-state\"]') || {}).textContent })" },
    { op: "screenshot", name: "im-b-11-panned" },
  ]);
  const view1 = viewportState(panned);
  const panProbe = evalAt(panned, 2);
  console.log("zz-state after pan:", (evals(panned) || []).join(" | ").slice(0, 400));
  const cards1 = evalAt(panned, 1);
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

  // --- 2. 空格 + 拖动空白处 = 框选卡片 --------------------------------------
  // 空白处：容器左下角附近（卡片都在上方），并打印当时是否真的在空白处
  const spaceX = Math.round(view1.left + 40);
  const spaceY = Math.round(view1.top + view1.height - 80);
  const framed = runSteps([
    ...NAV,
    keyDownSpace(),
    { op: "wait", ms: 250 },
    { op: "eval", js: "JSON.stringify({ space: (document.querySelector('[data-im=\"zz-state\"]') || {}).textContent, at: (document.elementFromPoint(" + spaceX + "," + spaceY + ") || {}).className || null })" },
    mouse("mousePressed", spaceX, spaceY),
    { op: "wait", ms: 150 },
    mouse("mouseMoved", spaceX + 420, spaceY - 320),
    { op: "wait", ms: 250 },
    { op: "eval", js: "JSON.stringify({ rect: !!document.querySelector('.select-rect') })" },
    { op: "screenshot", name: "im-b-20-space-framing" },
    mouse("mouseReleased", spaceX + 420, spaceY - 320),
    { op: "wait", ms: 700 },
    keyUpSpace(),
    { op: "wait", ms: 400 },
    { op: "eval", js: CARDS_EXPR },
    { op: "eval", js: "JSON.stringify({ toolbar: document.querySelectorAll('[data-im=\"card-toolbar\"]').length, connect: document.querySelectorAll('[data-im=\"connect-point\"]').length })" },
    { op: "screenshot", name: "im-b-21-space-framed" },
  ]);
  const spaceProbe = evalAt(framed, 0);
  const rectSeen = evalAt(framed, 2);
  const cards2 = evalAt(framed, 3);
  check("空格 + 拖动空白处出现框选矩形", rectSeen.rect === true, JSON.stringify(rectSeen));
  const selectedIds = (cards2 || []).filter((c) => c.selected).map((c) => c.id);
  check("框选选中了范围内的卡片", selectedIds.length >= 1, JSON.stringify(selectedIds));

  // --- 3. 滚轮以指针附近为缩放中心 ------------------------------------------
  const anchorBoard = { x: 260, y: 240 };
  const anchor = toScreen(view1, anchorBoard.x, anchorBoard.y);
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
  const cards3 = evalAt(zoomed, 1);
  const anchorAfter = toScreen(view2, anchorBoard.x, anchorBoard.y);
  check("滚轮放大（缩放 > 120%）", view2.scale > 1.2, "scale=" + view2.scale);
  check(
    "指针下的板面点在缩放前后落在同一屏幕位置（误差 < 2px）",
    Math.abs(anchorAfter.x - anchor.x) < 2 && Math.abs(anchorAfter.y - anchor.y) < 2,
    JSON.stringify({ before: anchor, after: anchorAfter }),
  );

  // --- 4. 缩放后拖动卡片：坐标按板面坐标换算 --------------------------------
  const cardA3 = (cards3 || []).find((c) => c.id === "b_card_a");
  const beforePos = boardPos(cardA3, view2);
  const grab = { x: Math.round(cardA3.left + cardA3.width / 2), y: Math.round(cardA3.top + 20) };
  const deltaScreen = { x: 160, y: 120 };
  const dragged = runSteps([
    ...NAV,
    mouse("mousePressed", grab.x, grab.y),
    { op: "wait", ms: 200 },
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
  const cards4 = evalAt(dragged, 0);
  const cardA4 = (cards4 || []).find((c) => c.id === "b_card_a");
  const afterPos = boardPos(cardA4, view2);
  const expectedDx = deltaScreen.x / view2.scale;
  const expectedDy = deltaScreen.y / view2.scale;
  check(
    "缩放后拖动卡片：板面位移 = 屏幕位移 ÷ 缩放（不是屏幕像素）",
    Math.abs(afterPos[0] - beforePos[0] - expectedDx) < 4 && Math.abs(afterPos[1] - beforePos[1] - expectedDy) < 4,
    JSON.stringify({
      实际位移: [afterPos[0] - beforePos[0], afterPos[1] - beforePos[1]],
      期望位移: [Math.round(expectedDx), Math.round(expectedDy)],
      scale: view2.scale,
    }),
  );
  const savedAfterDrag = await boardState();
  const savedA = savedAfterDrag.state.cards.find((c) => c.id === "b_card_a") || {};
  check("拖动结果真的保存到后端", Math.abs(savedA.x - afterPos[0]) < 6, JSON.stringify([Math.round(savedA.x), Math.round(savedA.y)]));

  // --- 5. 连接点拖线建链 -----------------------------------------------------
  const cards5 = evalAt(runSteps([...NAV, { op: "eval", js: CARDS_EXPR }]), 0);
  const a5 = cards5.find((c) => c.id === "b_card_a");
  const b5 = cards5.find((c) => c.id === "b_card_b");
  const selectRun = runSteps([
    ...NAV,
    mouse("mousePressed", a5.x, a5.y),
    { op: "wait", ms: 150 },
    mouse("mouseReleased", a5.x, a5.y),
    { op: "wait", ms: 500 },
    { op: "eval", js: "JSON.stringify({ connect: document.querySelectorAll('[data-im=\"connect-point\"]').length, toolbar: document.querySelectorAll('[data-im=\"card-toolbar\"]').length })" },
    { op: "eval", js: "(() => { const p = document.querySelector('[data-im=\"connect-point\"][data-side=\"right\"]'); if (!p) return JSON.stringify({}); const r = p.getBoundingClientRect(); return JSON.stringify({ x: Math.round(r.left + r.width / 2), y: Math.round(r.top + r.height / 2) }); })()" },
  ]);
  const connectInfo = evalAt(selectRun, 0);
  const pointPos = evalAt(selectRun, 1);
  check("选中卡片后出现连接点与局部工具栏", connectInfo.connect >= 4 && connectInfo.toolbar >= 1, JSON.stringify(connectInfo));
  check("拿到连接点的屏幕坐标", Number.isFinite(pointPos.x) && Number.isFinite(pointPos.y), JSON.stringify(pointPos));

  const linkDraftRun = runSteps([
    ...NAV,
    mouse("mousePressed", a5.x, a5.y),
    { op: "wait", ms: 150 },
    mouse("mouseReleased", a5.x, a5.y),
    { op: "wait", ms: 450 },
    mouse("mousePressed", pointPos.x, pointPos.y),
    { op: "wait", ms: 250 },
    mouse("mouseMoved", b5.x, b5.y),
    { op: "wait", ms: 300 },
    { op: "eval", js: "JSON.stringify({ draft: !!document.querySelector('[data-im=\"link-draft\"]'), target: (document.querySelector('[data-im=\"link-draft\"]') || { getAttribute: () => null }).getAttribute('data-target') })" },
    { op: "screenshot", name: "im-b-50-link-draft" },
    mouse("mouseReleased", b5.x, b5.y),
    { op: "wait", ms: 1000 },
    { op: "screenshot", name: "im-b-51-link-created" },
  ]);
  const draftInfo = evalAt(linkDraftRun, 0);
  check("拖线期间显示待建连线与有效目标", draftInfo.draft === true && draftInfo.target === "b_card_b", JSON.stringify(draftInfo));
  const savedAfterLink = await boardState();
  const liveLinks = savedAfterLink.state.links.filter((l) => !l.deleted);
  check("松手后建立关系并保存", liveLinks.length === 1, JSON.stringify(liveLinks.map((l) => [l.src, l.dst])));

  const invalidX = Math.round(view0.left + 60);
  const invalidY = Math.round(view0.top + view0.height - 80);
  const cancelRun = runSteps([
    ...NAV,
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

  // --- 6. 两张未分组卡片重叠成组 --------------------------------------------
  await resetBoard();
  await seedCards([
    { id: "b_card_a", x: 200, y: 240, w: 220, h: 140, content: "卡片 A：发布节奏需要确认" },
    { id: "b_card_b", x: 700, y: 240, w: 220, h: 140, content: "卡片 B：材料整理清单" },
  ]);
  const fresh = runSteps([...NAV, { op: "eval", js: CARDS_EXPR }, { op: "eval", js: VIEW_EXPR }]);
  const freshCards = evalAt(fresh, 0);
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
  const mergeHintInfo = evalAt(mergeRun, 0);
  const mergeAfterInfo = evalAt(mergeRun, 1);
  check("明确重叠时提示「松开后合并成组」且松手前还没有组", mergeHintInfo.hint === true && mergeHintInfo.groups === 0, JSON.stringify(mergeHintInfo));
  check("松手后自动成组", mergeAfterInfo.groups >= 1, JSON.stringify(mergeAfterInfo));
  const mergedState = await boardState();
  const mergedGroups = mergedState.state.groups.filter((g) => !g.deleted);
  check("成组结果保存到后端：一个组、两名成员、系统默认名", mergedGroups.length === 1 && mergedGroups[0].members.length === 2 && /^组 ?[0-9]+$/.test(mergedGroups[0].name), JSON.stringify(mergedGroups.map((g) => ({ name: g.name, members: g.members }))));

  // 靠近但没重叠：不提示、不成组
  await resetBoard();
  await seedCards([
    { id: "b_card_a", x: 200, y: 240, w: 220, h: 140, content: "卡片 A" },
    { id: "b_card_b", x: 900, y: 240, w: 220, h: 140, content: "卡片 B" },
  ]);
  const nearCards = evalAt(runSteps([...NAV, { op: "eval", js: CARDS_EXPR }]), 0);
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
  const nearHint = evalAt(nearRun, 0);
  const nearAfter = evalAt(nearRun, 1);
  check("靠近但没重叠：不提示、松手也不成组", nearHint.hint === false && nearAfter.groups === 0, JSON.stringify({ nearHint, nearAfter }));

  // --- 7. 组名编辑：输入即用，留空保留默认名 --------------------------------
  await resetBoard();
  await seedCards([
    { id: "b_card_a", x: 200, y: 240, w: 220, h: 140, content: "卡片 A" },
    { id: "b_card_b", x: 700, y: 240, w: 220, h: 140, content: "卡片 B" },
  ]);
  const seedNow = evalAt(runSteps([...NAV, { op: "eval", js: CARDS_EXPR }]), 0);
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
  check("重叠成组后组名是系统默认名（组 N）", /^组 ?[0-9]+$/.test(groupName || ""), groupName);

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
  const renameInfo = evalAt(renameRun, 2);
  const initialNameInfo = evalAt(renameRun, 0);
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
  const reloadInfo = evalAt(reloadRun, 0);
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
  const inputSpaceInfo = evalAt(inputSpaceRun, 0);
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
