/**
 * 无限点阵画布 · 本地窗口验收（CDP 驱动真实 Edge，无第三方依赖）。
 *
 * 走的是**正式互动页**（/interactive → InteractiveWorkspace → BoardCanvas），
 * 只把浏览器换成脚本控制的真实窗口；保存走正式入口（store → api.saveBoardState → 后端）。
 *
 * 路径：空板向左上平移 → 远处建两张卡 → 连线并填写关系 → 保存 → 关闭重开
 *       → 查看全部 → 再次点中原关系。
 *
 * 输出：每一步的坐标 / 关系端点 ID / 断言结果，以及截图。
 */
import { spawn } from "node:child_process";
import { mkdirSync, writeFileSync } from "node:fs";
import { setTimeout as sleep } from "node:timers/promises";

const APP_URL = process.env.QIO_APP_URL ?? "http://localhost:1420/";
const OUT_DIR = process.env.QIO_OUT_DIR ?? "D:\\qio-dev\\qio-action\\_infinite-canvas-evidence";
const EDGE = process.env.QIO_BROWSER ?? "C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe";
const CDP_PORT = Number(process.env.QIO_CDP_PORT ?? 9333);

mkdirSync(OUT_DIR, { recursive: true });

const log = [];
function record(step, data) {
  const entry = { step, at: new Date().toISOString(), ...data };
  log.push(entry);
  console.log("[" + step + "]", JSON.stringify(data));
}

/* ---------------- 最小 CDP 客户端 ---------------- */

class Cdp {
  constructor(ws) {
    this.ws = ws;
    this.nextId = 1;
    this.pending = new Map();
    this.events = [];
    ws.addEventListener("message", (event) => {
      const msg = JSON.parse(event.data);
      if (msg.id && this.pending.has(msg.id)) {
        const { resolve, reject } = this.pending.get(msg.id);
        this.pending.delete(msg.id);
        if (msg.error) reject(new Error(JSON.stringify(msg.error)));
        else resolve(msg.result);
        return;
      }
      if (msg.method) this.events.push(msg);
    });
  }

  send(method, params = {}) {
    const id = this.nextId++;
    return new Promise((resolve, reject) => {
      this.pending.set(id, { resolve, reject });
      this.ws.send(JSON.stringify({ id, method, params }));
      setTimeout(() => {
        if (this.pending.has(id)) {
          this.pending.delete(id);
          reject(new Error("CDP timeout: " + method));
        }
      }, 30000);
    });
  }

  async evaluate(expression) {
    const result = await this.send("Runtime.evaluate", {
      expression,
      awaitPromise: true,
      returnByValue: true,
      userGesture: true,
    });
    if (result.exceptionDetails) {
      throw new Error("evaluate failed: " + JSON.stringify(result.exceptionDetails).slice(0, 600));
    }
    return result.result?.value;
  }

  async screenshot(name) {
    const shot = await this.send("Page.captureScreenshot", { format: "png" });
    const path = OUT_DIR + "\\" + name + ".png";
    writeFileSync(path, Buffer.from(shot.data, "base64"));
    return path;
  }
}

async function connect() {
  for (let i = 0; i < 40; i += 1) {
    try {
      const list = await (await fetch("http://127.0.0.1:" + CDP_PORT + "/json/list")).json();
      const page = list.find((item) => item.type === "page");
      if (page) {
        const ws = new WebSocket(page.webSocketDebuggerUrl);
        await new Promise((resolve, reject) => {
          ws.addEventListener("open", resolve, { once: true });
          ws.addEventListener("error", reject, { once: true });
        });
        return new Cdp(ws);
      }
    } catch {
      /* 浏览器还没起来 */
    }
    await sleep(500);
  }
  throw new Error("连不上 CDP：" + CDP_PORT);
}

/* ---------------- 页面内辅助（注入到页面上下文） ---------------- */

const PAGE_HELPERS = `
window.__qio = (() => {
  const viewport = () => document.querySelector('.board-viewport');
  const surface = () => document.querySelector('.board-surface');
  const view = () => {
    const el = surface();
    if (!el) return { scale: 1, x: 0, y: 0 };
    const t = el.style.transform || '';
    const s = /scale\\(([\\d.]+)\\)/.exec(t);
    const m = /translate\\((-?[\\d.]+)px,\\s*(-?[\\d.]+)px\\)/.exec(t);
    return {
      scale: s ? Number(s[1]) : 1,
      x: m ? Number(m[1]) : 0,
      y: m ? Number(m[2]) : 0,
    };
  };
  const rect = () => {
    const el = viewport();
    const r = el.getBoundingClientRect();
    return { left: r.left, top: r.top, width: r.width, height: r.height };
  };
  const toScreen = (bx, by) => {
    const v = view();
    const r = rect();
    return { x: r.left + bx * v.scale + v.x, y: r.top + by * v.scale + v.y };
  };
  const toBoard = (cx, cy) => {
    const v = view();
    const r = rect();
    return { x: (cx - r.left - v.x) / v.scale, y: (cy - r.top - v.y) / v.scale };
  };
  const pointer = (type, x, y, extra = {}) => {
    const target = document.elementFromPoint(x, y) || viewport();
    target.dispatchEvent(new PointerEvent(type, {
      clientX: x, clientY: y, button: 0, buttons: type === 'pointerup' ? 0 : 1,
      bubbles: true, cancelable: true, pointerId: 1, pointerType: 'mouse', isPrimary: true, ...extra,
    }));
  };
  const cards = () => [...document.querySelectorAll('[data-im="card"]')].map((el) => ({
    id: el.getAttribute('data-card-id'),
    left: Number.parseFloat(el.style.left),
    top: Number.parseFloat(el.style.top),
    width: Number.parseFloat(el.style.width),
    height: Number.parseFloat(el.style.height),
  }));
  const links = () => [...document.querySelectorAll('[data-link-id]')].map((el) => ({
    id: el.getAttribute('data-link-id'),
    src: el.getAttribute('data-src'),
    dst: el.getAttribute('data-dst'),
    label: (el.querySelector('.label')?.textContent || '').trim(),
  }));
  const grid = () => {
    const el = viewport();
    return { size: el.style.backgroundSize, position: el.style.backgroundPosition };
  };
  return { view, rect, toScreen, toBoard, pointer, cards, links, grid, viewport, surface };
})();
true;
`;

/* ---------------- 主流程 ---------------- */

const browser = spawn(EDGE, [
  "--headless=new",
  "--remote-debugging-port=" + CDP_PORT,
  "--user-data-dir=" + OUT_DIR + "\\profile",
  "--no-first-run",
  "--no-default-browser-check",
  "--disable-gpu",
  "--window-size=1440,900",
  "about:blank",
], { stdio: "ignore" });

let cdp = null;
try {
  cdp = await connect();
  await cdp.send("Page.enable");
  await cdp.send("Runtime.enable");
  await cdp.send("Emulation.setDeviceMetricsOverride", { width: 1440, height: 900, deviceScaleFactor: 1, mobile: false });

  // 0) 先把板面清空（上一轮验收可能留下卡片）：走正式保存接口写一份空板面
  const cleared = await (async () => {
    const resp = await fetch("http://127.0.0.1:8734/api/interactive/boards/board_default/state");
    if (!resp.ok) return { error: "read failed " + resp.status };
    const payload = await resp.json();
    const empty = {
      ...payload.state,
      cards: [],
      groups: [],
      links: [],
      selection: [],
    };
    const put = await fetch("http://127.0.0.1:8734/api/interactive/boards/board_default/state", {
      method: "PUT",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ state: empty, reason: "验收前清空板面" }),
    });
    return { ok: put.ok, status: put.status };
  })();
  record("clear-board", cleared);

  // 1) 打开正式互动页
  await cdp.send("Page.navigate", { url: APP_URL + "#/interactive" });
  await sleep(6000);
  await cdp.evaluate(PAGE_HELPERS);
  const ready = await cdp.evaluate("Boolean(document.querySelector('.board-viewport'))");
  record("open", { url: APP_URL + "#/interactive", boardViewport: ready });
  if (!ready) throw new Error("互动页没有渲染出 .board-viewport");
  await cdp.screenshot("01-open");

  // 2) 空板：向左上平移（世界原点右移下移）
  const panResult = await cdp.evaluate(`(async () => {
    const q = window.__qio;
    const r = q.rect();
    // 起点选在视口左下角（空板区域，避开卡片与浮层）
    const startX = r.left + 40;
    const startY = r.top + r.height - 40;
    const before = q.view();
    const vp = q.viewport();
    // 向右下拖 = 世界原点右移下移 = 视野进入**左上远区**（板面 x、y 都是负数）
    vp.dispatchEvent(new PointerEvent('pointerdown', { clientX: startX, clientY: startY, button: 0, buttons: 1, bubbles: true, cancelable: true, pointerId: 1, pointerType: 'mouse', isPrimary: true }));
    await new Promise((res) => setTimeout(res, 60));
    window.dispatchEvent(new PointerEvent('pointermove', { clientX: startX + 900, clientY: startY + 700, button: 0, buttons: 1, bubbles: true, cancelable: true, pointerId: 1, pointerType: 'mouse', isPrimary: true }));
    await new Promise((res) => setTimeout(res, 120));
    window.dispatchEvent(new PointerEvent('pointerup', { clientX: startX + 900, clientY: startY + 700, button: 0, buttons: 0, bubbles: true, cancelable: true, pointerId: 1, pointerType: 'mouse', isPrimary: true }));
    await new Promise((res) => setTimeout(res, 200));
    return { before, after: q.view(), grid: q.grid(), cards: q.cards().length };
  })()`);
  await sleep(400);
  const panAfter = await cdp.evaluate("({ view: window.__qio.view(), grid: window.__qio.grid() })");
  record("pan-empty-board", { ...panResult, settled: panAfter });
  await cdp.screenshot("02-panned");

  // 3) 在远处（负坐标区）双击建两张卡
  const createResult = await cdp.evaluate(`(async () => {
    const q = window.__qio;
    const r = q.rect();
    const make = async (bx, by) => {
      const p = q.toScreen(bx, by);
      const el = document.elementFromPoint(p.x, p.y) || q.viewport();
      for (let i = 0; i < 2; i += 1) {
        el.dispatchEvent(new PointerEvent('pointerdown', { clientX: p.x, clientY: p.y, button: 0, buttons: 1, bubbles: true, cancelable: true, pointerId: 1, pointerType: 'mouse', isPrimary: true }));
        window.dispatchEvent(new PointerEvent('pointerup', { clientX: p.x, clientY: p.y, button: 0, buttons: 0, bubbles: true, cancelable: true, pointerId: 1, pointerType: 'mouse', isPrimary: true }));
        await new Promise((res) => setTimeout(res, 30));
      }
      el.dispatchEvent(new MouseEvent('dblclick', { clientX: p.x, clientY: p.y, button: 0, bubbles: true, cancelable: true }));
      await new Promise((res) => setTimeout(res, 350));
    };
    const v = q.view();
    // A：视野左上（板面负坐标远区）；B：视野右下（更靠近原点）
    const boardA = q.toBoard(r.left + r.width * 0.22, r.top + r.height * 0.22);
    const boardB = q.toBoard(r.left + r.width * 0.72, r.top + r.height * 0.70);
    await make(boardA.x, boardA.y);
    await make(boardB.x, boardB.y);
    return { boardA, boardB, cards: q.cards(), view: q.view() };
  })()`);
  record("create-cards", createResult);
  await cdp.screenshot("03-cards");

  // 4) 连线：从 A 的连接点拖到 B
  const linkResult = await cdp.evaluate(`(async () => {
    const q = window.__qio;
    const cards = q.cards();
    if (cards.length < 2) return { error: 'need two cards', cards };
    const a = cards[0];
    const b = cards[1];
    // 先选中 A（点卡片正文），连接点才会出现
    const centerA = q.toScreen(a.left + a.width / 2, a.top + a.height / 2);
    const cardEl = document.querySelector('[data-im="card"][data-card-id="' + a.id + '"]');
    cardEl.dispatchEvent(new PointerEvent('pointerdown', { clientX: centerA.x, clientY: centerA.y, button: 0, buttons: 1, bubbles: true, cancelable: true, pointerId: 1, pointerType: 'mouse', isPrimary: true }));
    window.dispatchEvent(new PointerEvent('pointerup', { clientX: centerA.x, clientY: centerA.y, button: 0, buttons: 0, bubbles: true, cancelable: true, pointerId: 1, pointerType: 'mouse', isPrimary: true }));
    await new Promise((res) => setTimeout(res, 400));
    const point = document.querySelector('[data-im="connect-point"][data-card-id="' + a.id + '"]');
    if (!point) return { error: 'no connect point', cards };
    const pr = point.getBoundingClientRect();
    const from = { x: pr.left + pr.width / 2, y: pr.top + pr.height / 2 };
    const to = q.toScreen(b.left + b.width / 2, b.top + b.height / 2);
    point.dispatchEvent(new PointerEvent('pointerdown', { clientX: from.x, clientY: from.y, button: 0, buttons: 1, bubbles: true, cancelable: true, pointerId: 1, pointerType: 'mouse', isPrimary: true }));
    await new Promise((res) => setTimeout(res, 120));
    window.dispatchEvent(new PointerEvent('pointermove', { clientX: to.x, clientY: to.y, button: 0, buttons: 1, bubbles: true, cancelable: true, pointerId: 1, pointerType: 'mouse', isPrimary: true }));
    await new Promise((res) => setTimeout(res, 120));
    window.dispatchEvent(new PointerEvent('pointerup', { clientX: to.x, clientY: to.y, button: 0, buttons: 0, bubbles: true, cancelable: true, pointerId: 1, pointerType: 'mouse', isPrimary: true }));
    await new Promise((res) => setTimeout(res, 500));
    return { from, to, links: q.links(), editorOpen: Boolean(document.querySelector('[data-im="link-meaning"]')) };
  })()`);
  record("create-link", linkResult);
  await cdp.screenshot("04-link");

  // 5) 填写关系含义并保存
  const meaningResult = await cdp.evaluate(`(async () => {
    const input = document.querySelector('[data-im="link-meaning"]');
    if (!input) return { error: 'no link editor' };
    input.focus();
    input.value = '属于（本地验收）';
    input.dispatchEvent(new Event('input', { bubbles: true }));
    await new Promise((res) => setTimeout(res, 120));
    const save = [...document.querySelectorAll('button')].find((b) => (b.textContent || '').includes('保存方向与含义'));
    if (!save) return { error: 'no save button' };
    save.click();
    await new Promise((res) => setTimeout(res, 900));
    return { links: window.__qio.links() };
  })()`);
  record("link-meaning", meaningResult);
  await cdp.screenshot("05-meaning");

  // 6) 等自动保存落库（450ms 防抖 + PUT）
  await sleep(2500);
  const savedState = await cdp.evaluate(`(async () => {
    const boardId = 'board_default';
    const resp = await fetch('http://127.0.0.1:8734/api/interactive/boards/' + encodeURIComponent(boardId) + '/state');
    if (!resp.ok) return { error: 'fetch failed ' + resp.status };
    const payload = await resp.json();
    return { boardId, seq: payload.seq, state: payload.state };
  })()`);
  record("saved-state", {
    boardId: savedState.boardId,
    seq: savedState.seq,
    cards: (savedState.state?.cards ?? []).map((c) => ({ id: c.id, x: c.x, y: c.y })),
    links: (savedState.state?.links ?? []).map((l) => ({ id: l.id, src: l.src, dst: l.dst, meaning: l.meaning, curve: l.curve })),
  });
  await cdp.screenshot("06-saved");

  // 7) 关闭重开：重新导航到互动页
  await cdp.send("Page.navigate", { url: APP_URL + "#/interactive" });
  await sleep(6000);
  await cdp.evaluate(PAGE_HELPERS);
  const reopened = await cdp.evaluate(`(() => {
    const q = window.__qio;
    return { view: q.view(), cards: q.cards(), links: q.links() };
  })()`);
  record("reopened", reopened);
  await cdp.screenshot("07-reopened");

  // 8) 把 B 拖到很远（让「查看全部」需要低于 25% 的适配比例），再点查看全部
  const farDrag = await cdp.evaluate(`(async () => {
    const q = window.__qio;
    const cards = q.cards();
    const b = cards[cards.length - 1];
    const from = q.toScreen(b.left + b.width / 2, b.top + b.height / 2);
    const cardEl = document.querySelector('[data-im="card"][data-card-id="' + b.id + '"]');
    cardEl.dispatchEvent(new PointerEvent('pointerdown', { clientX: from.x, clientY: from.y, button: 0, buttons: 1, bubbles: true, cancelable: true, pointerId: 1, pointerType: 'mouse', isPrimary: true }));
    await new Promise((res) => setTimeout(res, 80));
    // 屏幕位移 6000px：板面位移也是 6000（缩放 1），足以让两卡相距很远
    window.dispatchEvent(new PointerEvent('pointermove', { clientX: from.x + 6000, clientY: from.y + 4000, button: 0, buttons: 1, bubbles: true, cancelable: true, pointerId: 1, pointerType: 'mouse', isPrimary: true }));
    await new Promise((res) => setTimeout(res, 150));
    window.dispatchEvent(new PointerEvent('pointerup', { clientX: from.x + 6000, clientY: from.y + 4000, button: 0, buttons: 0, bubbles: true, cancelable: true, pointerId: 1, pointerType: 'mouse', isPrimary: true }));
    await new Promise((res) => setTimeout(res, 900));
    return { cards: q.cards() };
  })()`);
  record("drag-far", farDrag);
  await sleep(1500);

  const viewAllResult = await cdp.evaluate(`(async () => {
    const btn = document.querySelector('[data-im="view-all"]');
    if (!btn) return { error: 'no view-all button' };
    btn.click();
    await new Promise((res) => setTimeout(res, 700));
    const q = window.__qio;
    const r = q.rect();
    const v = q.view();
    const cards = q.cards().map((c) => {
      const p = q.toScreen(c.left, c.top);
      return { id: c.id, screenX: p.x, screenY: p.y, screenRight: p.x + c.width * v.scale, screenBottom: p.y + c.height * v.scale };
    });
    const allInside = cards.every((c) => c.screenX >= r.left - 2 && c.screenY >= r.top - 2 && c.screenRight <= r.left + r.width + 2 && c.screenBottom <= r.top + r.height + 2);
    return { view: v, viewport: r, cards, allInside, belowMinScale: v.scale < 0.25 };
  })()`);
  record("view-all", viewAllResult);
  await cdp.screenshot("08-view-all");

  // 8b) 查看全部之后再滚一级：不跳回 25%
  const afterWheel = await cdp.evaluate(`(async () => {
    const q = window.__qio;
    const r = q.rect();
    const before = q.view();
    q.viewport().dispatchEvent(new WheelEvent('wheel', { clientX: r.left + r.width / 2, clientY: r.top + r.height / 2, deltaY: -100, deltaMode: 0, bubbles: true, cancelable: true }));
    await new Promise((res) => setTimeout(res, 400));
    return { before, after: q.view() };
  })()`);
  record("wheel-after-view-all", afterWheel);

  // 9) 再次点中原关系（点关系标签）
  const relinkResult = await cdp.evaluate(`(async () => {
    const q = window.__qio;
    const label = document.querySelector('[data-link-label] .label-bg');
    if (!label) return { error: 'no link label' };
    const r = label.getBoundingClientRect();
    const x = r.left + r.width / 2;
    const y = r.top + r.height / 2;
    label.dispatchEvent(new MouseEvent('click', { clientX: x, clientY: y, bubbles: true, cancelable: true }));
    await new Promise((res) => setTimeout(res, 400));
    const active = document.querySelector('[data-link-id] .line.active');
    return { clicked: { x, y }, activeLink: active ? active.closest('[data-link-id]').getAttribute('data-link-id') : null, links: q.links() };
  })()`);
  record("relink", relinkResult);
  await cdp.screenshot("09-relink");

  // 10) 窄窗口：点阵是否铺满、底部按钮是否可点
  await cdp.send("Emulation.setDeviceMetricsOverride", { width: 900, height: 700, deviceScaleFactor: 1, mobile: false });
  await sleep(1200);
  const narrow = await cdp.evaluate(`(() => {
    const q = window.__qio;
    const r = q.rect();
    const vp = q.viewport();
    return {
      viewport: r,
      grid: q.grid(),
      fillsWidth: Math.abs(r.width - vp.clientWidth) < 2,
      toolbarButtons: document.querySelectorAll('[data-im="board-toolbar"] button').length,
      viewAllVisible: Boolean(document.querySelector('[data-im="view-all"]')),
    };
  })()`);
  record("narrow-window", narrow);
  await cdp.screenshot("10-narrow");

  writeFileSync(OUT_DIR + "\\report.json", JSON.stringify(log, null, 2));
  console.log("\n=== 报告已写入 " + OUT_DIR + "\\report.json ===");
} catch (err) {
  record("error", { message: String(err && err.message ? err.message : err) });
  writeFileSync(OUT_DIR + "\\report.json", JSON.stringify(log, null, 2));
  process.exitCode = 1;
} finally {
  try {
    await cdp?.send("Browser.close");
  } catch {
    /* 已经关了 */
  }
  browser.kill();
}
