// 任务 06 性能测量：七个关键场景的帧间隔 + 点击到第一处可见反馈 + 泄漏/穿透检查。
// 用法：
//   node scripts/baseline/t06-perf.mjs --mode=dev
//   node scripts/baseline/t06-perf.mjs --mode=prod --port=5299
// 输出：%TEMP%\qio-baseline\t06\report-<mode>.json
// 说明：只做测量，不改产品源码；环境是 headless Edge + SwiftShader 软件渲染，
//       数值用于发现卡顿与回归，不代表真机 GPU 表现。
import { createRequire } from "node:module";
import { mkdirSync, writeFileSync } from "node:fs";

const require = createRequire(import.meta.url);
const PW = "C:\\Users\\zxy\\.cache\\codex-runtimes\\codex-primary-runtime\\dependencies\\node\\node_modules\\playwright";
const { chromium } = require(PW);

const args = Object.fromEntries(
  process.argv.slice(2).map((a) => {
    const m = a.match(/^--([^=]+)=?(.*)$/);
    return m ? [m[1], m[2] || true] : [a, true];
  }),
);
const API = "http://127.0.0.1:8734";
const PORT = args.port ?? 5199;
const BASE = `http://127.0.0.1:${PORT}`;
const MODE = args.mode ?? "dev";
const ONLY = args.only ? String(args.only).split(",") : null;
const OUT = `${process.env.TEMP}\\qio-baseline\\t06`;
mkdirSync(OUT, { recursive: true });

const report = { mode: MODE, base: BASE, startedAt: new Date().toISOString(), conditions: {}, scenarios: [], leaks: null, visibility: null, notes: [] };
const rec = (id, title, m, detail = null) => {
  report.scenarios.push({ id, title, ...m, detail });
  const line = m
    ? `${m.frames} 帧/${m.durationMs}ms · ${m.avgFps}fps · p50=${m.p50} p95=${m.p95} max=${m.max} · >50ms=${m.over50}${m.feedbackMs != null ? ` · 反馈=${m.feedbackMs}ms` : ""}`
    : "未测";
  console.log(`[${id}] ${title} :: ${line}`);
};
const run = (id) => !ONLY || ONLY.includes(id);
const wait = (ms) => new Promise((r) => setTimeout(r, ms));

const browser = await chromium.launch({
  channel: "msedge",
  headless: true,
  args: ["--use-gl=angle", "--use-angle=swiftshader", "--enable-unsafe-swiftshader"],
});
const context = await browser.newContext({ viewport: { width: 1440, height: 900 } });
// 计数钩子 + 帧采样：每个页面都自动注入
await context.addInitScript(() => {
  const c = { ro: 0, mo: 0, listenersAdd: 0, listenersRemove: 0, listeners: 0, timers: 0, rafs: 0, byTarget: {} };
  window.__counts = c;
  // 细分到目标类型：window/document/element/other —— 判断「净增长」是不是挂在长生命周期目标上
  const kindOf = (t) => (t === window ? "window" : t === document ? "document" : t instanceof Element ? "element" : "other");
  // 记录被绑定过监听器的元素：报告时区分「还挂在文档里（疑似泄漏）」与「已随 DOM 移除（可回收）」
  const elTargets = new Set();
  const bump = (kind, delta) => {
    const k = c.byTarget[kind] ?? (c.byTarget[kind] = { add: 0, remove: 0, net: 0 });
    if (delta > 0) k.add++;
    else k.remove++;
    k.net = k.add - k.remove;
  };
  const RO = window.ResizeObserver;
  if (RO) window.ResizeObserver = class extends RO { constructor(...a) { super(...a); c.ro++; } };
  const MO = window.MutationObserver;
  if (MO) window.MutationObserver = class extends MO { constructor(...a) { super(...a); c.mo++; } };
  const add = EventTarget.prototype.addEventListener;
  EventTarget.prototype.addEventListener = function (...a) { c.listenersAdd++; c.listeners = c.listenersAdd - c.listenersRemove; bump(kindOf(this), 1); if (this instanceof Element) elTargets.add(this); return add.apply(this, a); };
  const remove = EventTarget.prototype.removeEventListener;
  EventTarget.prototype.removeEventListener = function (...a) { c.listenersRemove++; c.listeners = c.listenersAdd - c.listenersRemove; bump(kindOf(this), -1); return remove.apply(this, a); };
  const st = window.setTimeout;
  window.setTimeout = function (...a) { c.timers++; return st.apply(this, a); };
  const raf = window.requestAnimationFrame.bind(window);
  window.requestAnimationFrame = function (cb) { c.rafs++; return raf(cb); };
  window.__startFrames = () => {
    window.__f = [];
    let last = performance.now();
    const tick = (now) => {
      window.__f.push(now - last);
      last = now;
      window.__raf = window.requestAnimationFrame(tick);
    };
    window.__raf = window.requestAnimationFrame(tick);
  };
  window.__liveTargets = () => {
    let connected = 0;
    let detached = 0;
    for (const el of elTargets) {
      if (document.contains(el)) connected++;
      else detached++;
    }
    return { elementTargets: elTargets.size, stillInDom: connected, detached };
  };
  window.__stopFrames = () => {
    cancelAnimationFrame(window.__raf);
    const f = window.__f || [];
    const s = [...f].sort((a, b) => a - b);
    const at = (p) => (s.length ? Math.round(s[Math.min(s.length - 1, Math.floor(s.length * p))] * 10) / 10 : 0);
    const total = f.reduce((a, b) => a + b, 0);
    return {
      frames: f.length,
      durationMs: Math.round(total),
      avgFps: total ? Math.round((f.length / (total / 1000)) * 10) / 10 : 0,
      p50: at(0.5),
      p95: at(0.95),
      max: s.length ? Math.round(s[s.length - 1] * 10) / 10 : 0,
      over50: f.filter((x) => x > 50).length,
    };
  };
});

const page = await context.newPage();
const consoleErrors = [];
page.on("console", (m) => { if (m.type() === "error") consoleErrors.push(m.text().slice(0, 160)); });
page.on("pageerror", (e) => consoleErrors.push("pageerror: " + String(e.message).slice(0, 160)));

let seq = Math.floor(Date.now() / 1000) % 100000;
const url = (hash = "#/") => `${BASE}/?fresh=${++seq}${hash}`;
const openChat = async () => {
  await page.goto(url("#/"), { waitUntil: "domcontentloaded" });
  await page.waitForSelector(".composer textarea", { timeout: 20000 });
  await page.waitForTimeout(1200);
};
const openSettings = async (section = null) => {
  await page.goto(url("#/settings"), { waitUntil: "domcontentloaded" });
  await page.waitForSelector(".nav button.tab", { timeout: 20000 });
  await page.waitForTimeout(700);
  if (section) {
    await page.locator(".nav button.tab", { hasText: section }).first().click();
    await page.waitForTimeout(400);
  }
};
const inject = (type, data) =>
  fetch(`${API}/api/events/test?event_type=${type}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(data),
  });
const startFrames = () => page.evaluate(() => window.__startFrames());
const stopFrames = () => page.evaluate(() => window.__stopFrames());
const frames = async (label, action) => {
  await startFrames();
  if (action) await action();
  const m = await stopFrames();
  return m;
};
/** 页面内计时：点击到「第一处可见反馈」出现（不含 Playwright 往返） */
const clickFeedback = (selector, predicate) =>
  page.evaluate(async ({ selector, predicate }) => {
    const done = new Function(`return (${predicate});`)();
    const el = document.querySelector(selector);
    if (!el) return -1;
    const t0 = performance.now();
    el.click();
    return await new Promise((res) => {
      const tick = () => {
        const elapsed = Math.round(performance.now() - t0);
        if (done()) res(elapsed);
        else if (elapsed > 4000) res(-1);
        else requestAnimationFrame(tick);
      };
      tick();
    });
  }, { selector, predicate });
/** 制造一段持续生成的长回复（cumulative content，与后端 ASSISTANT 契约一致） */
async function streamLongReply(seconds, turnId) {
  await inject("TURN_START", { turn_id: turnId });
  let text = "";
  const end = Date.now() + seconds * 1000;
  let i = 0;
  while (Date.now() < end) {
    i += 1;
    text += `第${i}段增量内容，用来观察长回复期间的帧表现与布局稳定性。\n\n`;
    if (i % 5 === 0) text += "| 列A | 列B |\n| --- | --- |\n| 1 | 2 |\n\n";
    if (i % 7 === 0) text += "```python\ndef f(x):\n    return x  # 长代码行用来检查横向滚动容器 AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA\n```\n\n";
    await inject("ASSISTANT", { turn_id: turnId, content: text });
    await wait(40);
  }
  return text.length;
}

// ---------------------------------------------------------------- 测试条件
try {
  await openChat();
  const ui = await page.evaluate(() => ({
    dpr: window.devicePixelRatio,
    w: window.innerWidth,
    h: window.innerHeight,
    ua: navigator.userAgent,
  }));
  let rafHz = 0;
  // 采样 600ms 估算刷新率（不能 start/stop 立刻停，否则算出来是负数）
  const f = await frames("hz", async () => {
    await wait(600);
  });
  rafHz = f.avgFps;
  const [topics, ctx] = await Promise.all([
    fetch(`${API}/api/graph/topics`).then((r) => r.json()),
    fetch(`${API}/api/session/context`).then((r) => r.json()),
  ]);
  report.conditions = {
    mode: MODE,
    base: BASE,
    viewport: `${ui.w}x${ui.h}`,
    devicePixelRatio: ui.dpr,
    rAFHz: rafHz,
    userAgent: ui.ua,
    topics: topics.topics?.length ?? 0,
    currentTopicMessages: ctx.messages?.length ?? 0,
    renderer: "headless Edge (Chromium) + ANGLE/SwiftShader 软件渲染",
  };
  console.log(`条件：${JSON.stringify(report.conditions)}`);
} catch (e) {
  report.notes.push(`条件采集失败：${e.message.slice(0, 200)}`);
}

// ---------------------------------------------------------------- 七个场景
if (run("S1")) {
  try {
    await openChat();
    await startFrames();
    const fb1 = await clickFeedback(".settings-float", "() => document.querySelector('.settings') !== null");
    await page.waitForTimeout(700);
    const fb2 = await clickFeedback(".back", "() => document.querySelector('.composer textarea') !== null");
    await page.waitForTimeout(600);
    rec("S1", "长回答生成中打开设置再返回", { ...(await stopFrames()), feedbackMs: `${fb1}/${fb2}` }, "两次点击各自的页面内反馈耗时");
  } catch (e) {
    rec("S1", "长回答生成中打开设置再返回", null, e.message.slice(0, 200));
  }
}
if (run("S1B")) {
  try {
    await openChat();
    // 挂起本轮的 /api/turns：让界面停在「运行中」以便测流式期间切页
    await page.route("**/api/turns**", async (r) => {
      await wait(12000);
      // 页面可能已经导航导致请求被取消：忽略「已经处理过」的错误
      await r.fulfill({ status: 200, contentType: "application/json", body: "{}" }).catch(() => {});
    });
    await page.fill(".composer textarea", "长回答中切页");
    await page.locator(".send-btn").click();
    const streaming = streamLongReply(6, "turn_s1b");
    const m = await frames("s1b", async () => {
      await wait(900);
      await clickFeedback(".settings-float", "() => document.querySelector('.settings') !== null");
      await page.waitForTimeout(1600);
      await clickFeedback(".back", "() => document.querySelector('.composer textarea') !== null");
      await page.waitForTimeout(1200);
    });
    await streaming;
    await page.unroute("**/api/turns**").catch(() => {});
    rec("S1B", "长回答持续生成时打开设置再返回（真流式）", m);
  } catch (e) {
    rec("S1B", "长回答持续生成时打开设置再返回（真流式）", null, e.message.slice(0, 200));
  }
}
if (run("S2")) {
  try {
    await openChat();
    await inject("TURN_START", { turn_id: "turn_s2" });
    const streaming = streamLongReply(6, "turn_s2");
    await wait(600);
    await startFrames();
    const fb = await clickFeedback(".dock", "() => document.querySelector('.planet-view') !== null");
    await page.waitForTimeout(1200);
    await page.locator(".panel-toggle").click();
    await page.waitForTimeout(500);
    const li = page.locator(".topic-list li");
    if (await li.count()) {
      await li.first().click();
      await page.waitForTimeout(900);
    }
    const m = await stopFrames();
    await streaming;
    rec("S2", "长回答生成中打开星球、选择话题、展开侧栏", { ...m, feedbackMs: fb });
  } catch (e) {
    rec("S2", "长回答生成中打开星球、选择话题、展开侧栏", null, e.message.slice(0, 200));
  }
}
if (run("S3")) {
  try {
    await page.goto(url("#/"), { waitUntil: "domcontentloaded" });
    await page.waitForSelector(".dock", { timeout: 20000 });
    await page.waitForTimeout(900);
    await page.locator(".dock").click();
    await page.waitForTimeout(1600);
    await page.locator(".panel-toggle").click();
    await page.waitForTimeout(500);
    const li = page.locator(".topic-list li");
    await li.first().click();
    await page.waitForTimeout(1200);
    await page.locator(".tab-knowledge").click();
    await page.waitForTimeout(1200);
    await startFrames();
    // 旋转（拖动画布）+ 连续选择
    await page.mouse.move(520, 420);
    await page.mouse.down();
    for (let i = 0; i < 14; i++) {
      await page.mouse.move(520 + i * 6, 420 + (i % 3) * 5);
      await wait(25);
    }
    await page.mouse.up();
    await page.locator(".tab-topic").click();
    await page.waitForTimeout(400);
    for (let i = 0; i < 4; i++) {
      const n = await li.count();
      if (!n) break;
      await li.nth(Math.min(i + 1, n - 1)).click();
      await page.waitForTimeout(200);
    }
    await page.waitForTimeout(800);
    rec("S3", "多话题 + 长详情 + 管理列表同时存在时旋转与选择", await stopFrames());
  } catch (e) {
    rec("S3", "多话题 + 长详情 + 管理列表同时存在时旋转与选择", null, e.message.slice(0, 200));
  }
}
if (run("S4")) {
  try {
    await openChat();
    await inject("TURN_START", { turn_id: "turn_s4" });
    await inject("ASSISTANT", { turn_id: "turn_s4", content: "# 大消息\n\n| 列A | 列B | 列C |\n| --- | --- | --- |\n| 1 | 2 | 3 |\n\n```js\nconst long = \"AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA\";\n```\n" });
    await inject("TURN_END", { turn_id: "turn_s4" });
    await page.waitForTimeout(1200);
    const m = await frames("s4", async () => {
      for (let i = 0; i < 40; i++) {
        await page.mouse.wheel(0, i % 2 === 0 ? 260 : -180);
        await wait(25);
      }
      await page.waitForTimeout(300);
    });
    rec("S4", "大代码块/表格消息滚动", m);
  } catch (e) {
    rec("S4", "大代码块/表格消息滚动", null, e.message.slice(0, 200));
  }
}
if (run("S5")) {
  try {
    await openChat();
    const m = await frames("s5", async () => {
      for (let i = 0; i < 3; i++) {
        await clickFeedback(".settings-float", "() => document.querySelector('.settings') !== null");
        await page.waitForTimeout(260);
        await clickFeedback(".back", "() => document.querySelector('.composer textarea') !== null");
        await page.waitForTimeout(260);
      }
    });
    const fbMenu = await (async () => {
      await openSettings("外观");
      return clickFeedback(".panel .qio-select", "() => document.querySelector('.qio-select.open .qio-select-menu') !== null");
    })();
    rec("S5", "连续开关页面与菜单", { ...m, feedbackMs: `菜单=${fbMenu}` });
  } catch (e) {
    rec("S5", "连续开关页面与菜单", null, e.message.slice(0, 200));
  }
}
if (run("S6")) {
  try {
    await openChat();
    await page.locator(".composer textarea").click();
    const m = await frames("s6", async () => {
      for (let i = 0; i < 24; i++) {
        await page.keyboard.type("后台确认到来时正在输入");
        await wait(40);
      }
      await inject("APPROVAL_REQUIRED", { approval: { approval_id: "apr_t06", kind: "tool_create", payload: { name: "测量用", explanation: "测量后台确认对输入的影响" } } });
      await page.waitForTimeout(900);
      for (let i = 0; i < 12; i++) {
        await page.keyboard.type("继续输入");
        await wait(40);
      }
    });
    const entry = await page.getByText("等待确认", { exact: false }).count();
    rec("S6", "后台确认到来时正在输入", { ...m, feedbackMs: entry > 0 ? 0 : -1 }, `确认入口出现=${entry}`);
  } catch (e) {
    rec("S6", "后台确认到来时正在输入", null, e.message.slice(0, 200));
  }
}
if (run("S7")) {
  try {
    await openChat();
    // 缩放窗口
    const m1 = await frames("s7a", async () => {
      for (const [w, h] of [[1024, 768], [760, 720], [1440, 900]]) {
        await page.setViewportSize({ width: w, height: h });
        await wait(500);
      }
    });
    rec("S7a", "连续缩放窗口", m1);
    // 减少动画运行时切换
    await openSettings("外观");
    const m2 = await frames("s7b", async () => {
      await page.locator(".motion-opt", { hasText: "减少动画" }).first().click();
      await page.waitForTimeout(600);
      await page.locator(".motion-opt", { hasText: "跟随系统" }).first().click();
      await page.waitForTimeout(600);
    });
    rec("S7b", "减少动画运行时切换", m2);
    // 切到后台：星球打开的情况下，看 rAF 是否仍在跑
    await page.goto(url("#/"), { waitUntil: "domcontentloaded" });
    await page.waitForSelector(".dock", { timeout: 20000 });
    await page.waitForTimeout(900);
    await page.locator(".dock").click();
    await page.waitForTimeout(1600);
    const before = await frames("vis-before", async () => { await wait(1000); });
    const after = await page.evaluate(async () => {
      Object.defineProperty(document, "visibilityState", { configurable: true, get: () => "hidden" });
      Object.defineProperty(document, "hidden", { configurable: true, get: () => true });
      document.dispatchEvent(new Event("visibilitychange"));
      window.__startFrames();
      await new Promise((r) => setTimeout(r, 1000));
      const hz = window.__stopFrames();
      Object.defineProperty(document, "visibilityState", { configurable: true, get: () => "visible" });
      Object.defineProperty(document, "hidden", { configurable: true, get: () => false });
      document.dispatchEvent(new Event("visibilitychange"));
      return hz;
    });
    report.visibility = { planetOpenForeground: before, planetOpenHidden: after };
    console.log(`[VIS] 前台 ${before.frames} 帧/${before.durationMs}ms → 标记隐藏后 ${after.frames} 帧/${after.durationMs}ms`);
  } catch (e) {
    rec("S7a", "连续缩放窗口", null, e.message.slice(0, 200));
  }
}

// ---------------------------------------------------------------- 泄漏与穿透
if (run("S8")) {
  try {
    await openChat();
    const first = await clickFeedback(".dock", "() => document.querySelector('.planet-view') !== null");
    await page.waitForTimeout(1800);
    await page.locator(".close-btn").click();
    await page.waitForSelector(".planet-view", { state: "hidden", timeout: 6000 }).catch(() => {});
    await page.waitForTimeout(600);
    // 第二次打开：实例常驻（不再重建 WebGL 场景）
    const second = await clickFeedback(".dock", "() => { const el = document.querySelector('.planet-view'); return !!el && getComputedStyle(el).display !== 'none'; }");
    await page.waitForTimeout(1200);
    const sceneCount = await page.evaluate(() => document.querySelectorAll("canvas.planet-canvas").length);
    report.scenarios.push({ id: "S8", title: "星球实例复用：第一次/第二次打开", frames: 0, durationMs: 0, avgFps: 0, p50: 0, p95: 0, max: 0, over50: 0, feedbackMs: `首次=${first}ms 再次=${second}ms`, detail: `页面里 canvas 实例数=${sceneCount}` });
    console.log(`[S8] 星球实例复用 :: 首次打开 ${first}ms → 第二次打开 ${second}ms（canvas 实例数=${sceneCount}）`);
  } catch (e) {
    rec("S8", "星球实例复用：第一次/第二次打开", null, e.message.slice(0, 200));
  }
}

if (run("LEAK")) {
  try {
    await openChat();
    const snapshot = () => page.evaluate(() => ({ ...window.__counts }));
    const base = await snapshot();
    // 星球 ×10
    for (let i = 0; i < 10; i++) {
      await page.locator(".dock").click();
      await page.waitForTimeout(900);
      await page.locator(".close-btn").click();
      await page.waitForSelector(".planet-view", { state: "detached", timeout: 6000 });
      await page.waitForTimeout(150);
    }
    const afterPlanet = await snapshot();
    // 设置 ×10
    for (let i = 0; i < 10; i++) {
      await page.locator(".settings-float").click();
      await page.waitForSelector(".nav button.tab", { timeout: 10000 });
      await page.waitForTimeout(150);
      await page.locator(".back").click();
      await page.waitForSelector(".composer textarea", { timeout: 10000 });
      await page.waitForTimeout(150);
    }
    const afterSettings = await snapshot();
    // 侧栏 ×10
    await page.locator(".dock").click();
    await page.waitForTimeout(1200);
    for (let i = 0; i < 10; i++) {
      await page.locator(".panel-toggle").click();
      await page.waitForTimeout(220);
    }
    const afterPanel = await snapshot();
    await page.locator(".close-btn").click();
    await page.waitForSelector(".planet-view", { state: "detached", timeout: 6000 });
    await page.waitForTimeout(400);
    const hit = await page.evaluate(() => {
      const el = document.elementFromPoint(window.innerWidth / 2, window.innerHeight / 2);
      return el ? `${el.tagName}.${String(el.className || "").split(" ")[0]}` : "none";
    });
    const liveTargets = await page.evaluate(() => window.__liveTargets());
    report.leaks = { base, afterPlanet, afterSettings, afterPanel, centerHitAfterClose: hit, liveTargets };
    console.log(`[LEAK] ${JSON.stringify(report.leaks)}`);
  } catch (e) {
    report.notes.push(`泄漏检查失败：${e.message.slice(0, 200)}`);
  }
}

report.consoleErrors = consoleErrors.slice(0, 20);
report.endedAt = new Date().toISOString();
writeFileSync(`${OUT}\\report-${MODE}.json`, JSON.stringify(report, null, 2), "utf8");
console.log(`\nreport: ${OUT}\\report-${MODE}.json`);
console.log(`console errors: ${consoleErrors.length}`);
await browser.close();
