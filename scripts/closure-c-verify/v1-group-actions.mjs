/**
 * V1 反例采集：组操作（设为有序 / 解除组 / 移出成员）是否真的可达并且真的生效。
 *
 * 为什么不是「看一眼截图」：组头部是**绝对定位**的一块，卡片是后画的兄弟节点；
 * 只要卡片压在组头部上，「命中测试」就会落到卡片上 —— 按钮看着在那儿，点下去却没反应。
 * 所以这里用真实指针事件（CDP Input.dispatchMouseEvent）去点，并核对：
 *   1) 按钮中心点的 elementFromPoint 命中链里有没有它自己（可达）；
 *   2) 点击之后**服务端板面状态**是否真的变了（有序 / 组消失 / 成员减少）。
 *
 * 与「视口外 / 滚动区外 / 正常打开浮层覆盖」的区分：
 *   - 中心点不在视口内 → offscreen（不算被遮挡）；
 *   - 中心点落在浮层（聊天 / 批量 / 工具栏 / 合并提示 / 确认框）内 → 记为 panel，单独列出；
 *   - 其它情况命中别的元素 → covered（这是缺陷）。
 *
 * 用法：node scripts/closure-c-verify/v1-group-actions.mjs --label=before
 * 环境变量：QIO_C_APP_PORT / QIO_C_BACKEND_PORT / QIO_C_APP / QIO_C_BACKEND。
 */
import { mkdirSync, writeFileSync } from "node:fs";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { Browser } from "./cdp.mjs";

const here = dirname(fileURLToPath(import.meta.url));
const arg = (name, fallback) => {
  const hit = process.argv.find((a) => a.startsWith("--" + name + "="));
  return hit ? hit.split("=").slice(1).join("=") : fallback;
};
const LABEL = arg("label", "before");
const APP = process.env.QIO_C_APP || "http://127.0.0.1:" + (process.env.QIO_C_APP_PORT || 5421);
const BACKEND = process.env.QIO_C_BACKEND || "http://127.0.0.1:" + (process.env.QIO_C_BACKEND_PORT || 8921);
const OUT = resolve(here, arg("out", "shots"));
const SHOT_DIR = join(OUT, LABEL + "-v1");
mkdirSync(SHOT_DIR, { recursive: true });
const BOARD = "board_default";
const BT = String.fromCharCode(96);
const DS = "$" + "{";

async function api(path, init) {
  const resp = await fetch(BACKEND + path, {
    ...(init || {}),
    headers: { "Content-Type": "application/json", Connection: "close", ...((init && init.headers) || {}) },
  });
  return resp.json();
}
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

/** 固定反例数据：两组，卡片故意压在组头部上（组框顶边由成员最小 y 决定，这是真实会发生的位置）。 */
/**
 * 写入前先 GET 当前已保存的 seq（B 的服务端版本保护）：
 * PUT 的 state.seq 与已保存 seq 不一致会 409 stale_state（不落库）。
 * 固定写 0/1 的脚本在非空板面上会失败，所以这里每一步都用「刚读到的真实版本」。
 */
async function currentSeq() {
  try {
    const data = await api("/api/interactive/boards/" + BOARD + "/state");
    const state = data.state || data;
    return Number.isFinite(Number(state.seq)) ? Number(state.seq) : 0;
  } catch (error) {
    return 0;
  }
}

async function seed() {
  const now = new Date().toISOString();
  const card = (id, x, y, content) => ({
    id, kind: "text", x, y, w: 200, h: 120, content, meta: {}, deleted: false, folded: false,
    hidden: false, checked: false, bookmarked: false, createdAt: now, updatedAt: now,
  });
  const cards = [
    card("cA", 40, 40, "A：压在本组头部上的卡片（用于验证组头部按钮是否被卡片压住）"),
    card("cB", 300, 60, "B：与 A 同组的第二张"),
    card("cC", 40, 420, "C：另一组的第一张"),
    card("cD", 300, 440, "D：另一组的第二张"),
    card("cE", 700, 40, "E：单独一张，用来验证组外卡片不受影响"),
  ];
  const group = (id, ordered, members) => ({
    id, name: "默认组名", defaultName: true, ordered, deleted: false, members, x: 0, y: 0, w: 1, h: 1, createdAt: now, updatedAt: now,
  });
  const groups = [group("g_left", true, ["cA", "cB"]), group("g_right", false, ["cC", "cD"])];
  const clearedSeq = await currentSeq();
  const cleared = await api("/api/interactive/boards/" + BOARD + "/state", {
    method: "PUT",
    body: JSON.stringify({ state: { boardId: BOARD, seq: clearedSeq, updatedAt: now, cards: [], groups: [], links: [], selection: [] }, reason: "v1-reset" }),
  });
  const seededSeq = await currentSeq();
  const seeded = await api("/api/interactive/boards/" + BOARD + "/state", {
    method: "PUT",
    body: JSON.stringify({ state: { boardId: BOARD, seq: seededSeq, updatedAt: now, cards, groups, links: [], selection: [] }, reason: "v1-seed" }),
  });
  return {
    cards: cards.length,
    groups: groups.length,
    clearedSeq: clearedSeq,
    seededSeq: seededSeq,
    errors: [cleared?.detail?.error, seeded?.detail?.error].filter(Boolean),
  };
}

async function serverBoard() {
  const data = await api("/api/interactive/boards/" + BOARD + "/state");
  return data.state || data;
}

const READY = `(async () => { const t0 = Date.now(); for (;;) { const g = document.querySelector('.board-surface [data-im=group]'); const c = document.querySelector('.board-surface [data-im=card]'); if (g && c) return 'ready'; if (Date.now() - t0 > 12000) return 'timeout'; await new Promise((r) => setTimeout(r, 150)); } })()`;

/**
 * 采集前必须断言真实状态：组与卡片都渲染出来才算「就绪」。
 * 只等一次会在慢场景里量到空页面（实测：有的尺寸那一格一行数据都没有），
 * 把「没渲染」当成「没有问题」正是这类验收最容易自欺的地方。
 */
async function ensureReady(browser) {
  for (let attempt = 0; attempt < 3; attempt += 1) {
    const state = await browser.evalJs(READY);
    if (state === "ready") return;
    await browser.reload(4000);
  }
  throw new Error("板面没有渲染出组与卡片（真实状态断言失败），采集中止");
}

/** 只读的几何 + 命中测量（一次 eval 全取回）。 */
const METRICS = `(() => {
  const box = (el) => { const b = el.getBoundingClientRect(); return { x: Math.round(b.x), y: Math.round(b.y), w: Math.round(b.width), h: Math.round(b.height), right: Math.round(b.right), bottom: Math.round(b.bottom) }; };
  const shown = (el) => { const b = el.getBoundingClientRect(); const st = getComputedStyle(el); return b.width > 1 && b.height > 1 && st.visibility !== 'hidden' && st.display !== 'none'; };
  const describe = (el) => !el ? 'null' : el.tagName.toLowerCase() + (el.getAttribute && el.getAttribute('data-im') ? '[data-im=' + el.getAttribute('data-im') + ']' : '') + (el.getAttribute && el.getAttribute('data-group-id') ? '[g=' + el.getAttribute('data-group-id') + ']' : '');
  const PANELS = ['[data-im="chat-panel"]', '[data-im="batch-list"]', '[data-im="board-toolbar"]', '[data-im="group-merge-hint"]', '[data-im="impact-dialog"]', '[data-im="tasks-popover"]', '[data-im="search-panel"]', '[data-im="help-popover"]', '[data-im="card-toolbar"]'];
  const inPanel = (x, y) => { for (const sel of PANELS) { for (const el of document.querySelectorAll(sel)) { if (!shown(el)) continue; const b = el.getBoundingClientRect(); if (x >= b.left && x <= b.right && y >= b.top && y <= b.bottom) return sel; } } return null; };
  const inViewport = (r) => r.left >= 0 && r.top >= 0 && r.right <= window.innerWidth && r.bottom <= window.innerHeight;

  const targets = [];
  for (const group of document.querySelectorAll('[data-im="group"]')) {
    const gid = group.getAttribute('data-group-id');
    const ordered = group.getAttribute('data-ordered') === 'true';
    for (const role of ['toggle-ordered', 'dissolve-group']) {
      const el = group.querySelector('[data-im="' + role + '"]');
      if (el) targets.push({ role: role, groupId: gid, el: el });
    }
    for (const el of group.querySelectorAll('[data-im="leave-member"]')) targets.push({ role: 'leave-member', groupId: gid, cardId: el.getAttribute('data-card-id'), el: el });
    for (const el of group.querySelectorAll('[data-im="move-member-up"],[data-im="move-member-down"]')) targets.push({ role: el.getAttribute('data-im'), groupId: gid, cardId: el.getAttribute('data-card-id'), el: el });
    targets.push({ role: 'group-name', groupId: gid, el: group.querySelector('[data-im="group-name"]') });
    void ordered;
  }

  const rows = [];
  for (const t of targets) {
    if (!t.el || !shown(t.el)) { rows.push({ role: t.role, groupId: t.groupId, cardId: t.cardId, why: 'not-shown' }); continue; }
    const r = t.el.getBoundingClientRect();
    const cx = r.left + r.width / 2;
    const cy = r.top + r.height / 2;
    const base = { role: t.role, groupId: t.groupId, cardId: t.cardId, box: box(t.el) };
    if (!inViewport(r)) { rows.push(Object.assign({}, base, { why: 'offscreen' })); continue; }
    const panel = inPanel(cx, cy);
    if (panel) { rows.push(Object.assign({}, base, { why: 'panel', panel: panel })); continue; }
    const hit = document.elementFromPoint(cx, cy);
    const ok = hit && (hit === t.el || t.el.contains(hit) || hit.contains(t.el));
    rows.push(Object.assign({}, base, { why: ok ? 'reachable' : 'covered', hit: describe(hit), hitParent: describe(hit && hit.parentElement), stack: document.elementsFromPoint(cx, cy).slice(0, 4).map(describe) }));
  }
  return JSON.stringify({ viewport: [window.innerWidth, window.innerHeight], rows: rows });
})()`;

/** 真实鼠标点击（走 CDP，触发完整的 pointerdown/mousedown/click 路径）。 */
async function realClick(browser, selector) {
  const raw = await browser.evalJs("(() => { const el = document.querySelector(" + JSON.stringify(selector) + "); if (!el) return 'null'; const b = el.getBoundingClientRect(); return JSON.stringify({ x: b.left + b.width / 2, y: b.top + b.height / 2 }); })()");
  if (raw === "null") return { ok: false, why: "not-found", selector: selector };
  const rect = JSON.parse(raw);
  await browser.send("Input.dispatchMouseEvent", { type: "mouseMoved", x: Math.round(rect.x), y: Math.round(rect.y), buttons: 0 });
  await browser.send("Input.dispatchMouseEvent", { type: "mousePressed", x: Math.round(rect.x), y: Math.round(rect.y), button: "left", buttons: 1, clickCount: 1 });
  await sleep(40);
  await browser.send("Input.dispatchMouseEvent", { type: "mouseReleased", x: Math.round(rect.x), y: Math.round(rect.y), button: "left", buttons: 0, clickCount: 1 });
  await sleep(120);
  return { ok: true, at: { x: Math.round(rect.x), y: Math.round(rect.y) } };
}

/** 等真实状态出现（保存是异步的，只看界面文字会把「还没保存」当成失败）。 */
async function waitBoard(predicate, timeoutMs = 6000) {
  const t0 = Date.now();
  let last = null;
  while (Date.now() - t0 < timeoutMs) {
    try { last = await serverBoard(); if (predicate(last)) return { ok: true, state: last }; } catch (e) { /* 还在保存 */ }
    await sleep(250);
  }
  return { ok: false, state: last };
}

async function seedIntents() {
  await api("/api/interactive/boards/" + BOARD + "/intents", { method: "POST", body: JSON.stringify({ demo: true }) });
  const list = await api("/api/interactive/boards/" + BOARD + "/intents");
  return (list.intents || []).map((i) => i.id);
}

async function run() {
  const seeded = await seed();
  const browser = await new Browser({ outDir: OUT }).launch();
  const scenarios = [
    { id: "1440x900-base", w: 1440, h: 900, panels: [] },
    { id: "1440x900-pan-zoom", w: 1440, h: 900, panels: [], panZoom: true },
    { id: "1440x900-chat-batch", w: 1440, h: 900, panels: ["chat", "batch"] },
    { id: "1024x768-base", w: 1024, h: 768, panels: [] },
    { id: "800x600-base", w: 800, h: 600, panels: [] },
    { id: "800x600-chat", w: 800, h: 600, panels: ["chat"] },
    { id: "480x600-base", w: 480, h: 600, panels: [] },
  ];
  const results = [];
  try {
    for (const sc of scenarios) {
      await seed();
      await browser.navigate(APP + "/?c=" + Date.now() + "#/interactive", 4500);
      await browser.setViewport(sc.w, sc.h);
      await ensureReady(browser);
      if (sc.panZoom) {
        await browser.evalJs("(() => { const v = document.querySelector('.board-viewport'); if (v) { v.scrollLeft = 90; v.scrollTop = 60; v.dispatchEvent(new Event('scroll')); } return 'panned'; })()");
        await browser.send("Input.dispatchMouseEvent", { type: "mouseWheel", x: Math.round(sc.w / 2), y: Math.round(sc.h / 2), deltaX: 0, deltaY: -120 });
        await sleep(800);
      }
      if (sc.panels.indexOf("chat") >= 0) {
        await browser.evalJs(`(async () => { const b = document.querySelector('[data-im="chat-toggle"]'); if (b && !document.querySelector('[data-im="chat-panel"]')) b.click(); await new Promise((r) => setTimeout(r, 800)); return 'chat'; })()`);
      }
      if (sc.panels.indexOf("batch") >= 0) {
        const ids = await seedIntents();
        await browser.evalJs("(function () { try { localStorage.setItem('qio.interactive.intentBatches', JSON.stringify({ version: 2, records: " + JSON.stringify(ids.map((id) => ({ id: id, key: "v1", at: Date.now() }))) + " })); return 'seeded'; } catch (e) { return 'ls-fail'; } })()");
        await browser.reload(3500);
        await ensureReady(browser);
        await browser.evalJs(`(async () => { const b = document.querySelector('[data-im="batch-entry"]'); if (b && !document.querySelector('[data-im="batch-list"]')) b.click(); await new Promise((r) => setTimeout(r, 900)); return 'batch'; })()`);
      }
      await sleep(400);
      const metrics = JSON.parse(await browser.evalJs(METRICS));
      const shot = await browser.shotFile(LABEL + "-v1/" + sc.id);
      results.push({ scenario: sc.id, size: sc.w + "x" + sc.h, metrics: metrics, shot: shot, actions: [] });
    }

    // 真实点击核对：先看可达，再点，再核对服务端事实
    await seed();
    await browser.navigate(APP + "/?c=" + Date.now() + "#/interactive", 4500);
    await browser.setViewport(1024, 768);
    await ensureReady(browser);
    const act = [];
    const before1 = await serverBoard();
    const click1 = await realClick(browser, '[data-im="group"][data-group-id="g_right"] [data-im="toggle-ordered"]');
    const r1 = await waitBoard((s) => Boolean((s.groups || []).find((g) => g.id === "g_right" && g.ordered === true)));
    act.push({ action: "设为有序(g_right)", click: click1, before: (before1.groups || []).find((g) => g.id === "g_right").ordered, ok: r1.ok, after: (r1.state && (r1.state.groups || []).find((g) => g.id === "g_right")) ? r1.state.groups.find((g) => g.id === "g_right").ordered : null });

    const click2 = await realClick(browser, '[data-im="group"][data-group-id="g_left"] [data-im="leave-member"]');
    const r2 = await waitBoard((s) => !((s.groups || []).find((g) => g.id === "g_left") || { members: [] }).members.includes("cA"));
    act.push({ action: "移出成员(g_left/cA)", click: click2, ok: r2.ok, after: (r2.state && (r2.state.groups || []).find((g) => g.id === "g_left")) ? r2.state.groups.find((g) => g.id === "g_left").members : null });

    await seed();
    await browser.navigate(APP + "/?c=" + Date.now() + "#/interactive", 4500);
    await browser.setViewport(1024, 768);
    await ensureReady(browser);
    const click3 = await realClick(browser, '[data-im="group"][data-group-id="g_left"] [data-im="dissolve-group"]');
    const r3 = await waitBoard((s) => !(s.groups || []).some((g) => g.id === "g_left"));
    act.push({ action: "解除组(g_left)", click: click3, ok: r3.ok, afterGroups: (r3.state && (r3.state.groups || []).map((g) => g.id)) || [] });
    results.push({ scenario: "1024x768-actions", size: "1024x768", metrics: null, shot: null, actions: act });
  } finally {
    await browser.close();
  }
  const rows = [];
  for (const r of results) {
    if (!r.metrics) continue;
    for (const row of r.metrics.rows) rows.push(Object.assign({ scenario: r.scenario }, row));
  }
  const count = (why) => rows.filter((r) => r.why === why).length;
  const summary = { total: rows.length, reachable: count("reachable"), covered: count("covered"), panel: count("panel"), offscreen: count("offscreen"), notShown: count("not-shown") };
  const report = { label: LABEL, app: APP, backend: BACKEND, seeded: seeded, createdAt: new Date().toISOString(), summary: summary, results: results, rows: rows, httpFails: [...new Set(browser.httpFails)], consoleErrors: browser.consoleErrors.slice(-10) };
  const file = join(OUT, LABEL + "-v1-report.json");
  writeFileSync(file, JSON.stringify(report, null, 2), "utf8");
  console.log(JSON.stringify(summary));
  for (const r of rows) if (r.why === "covered" || r.why === "panel") console.log(r.scenario + "  " + r.why + "  " + r.role + " g=" + r.groupId + " card=" + (r.cardId || "-") + " by=" + (r.hit || r.panel));
  for (const res of results) if (res.actions.length) console.log("actions " + res.scenario + " " + JSON.stringify(res.actions));
  console.log("报告：" + file);
}

run().catch((error) => { console.error("采集失败：" + (error && error.stack ? error.stack : error)); process.exit(1); });
