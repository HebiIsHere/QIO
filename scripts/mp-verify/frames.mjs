/**
 * 展开 / 收起 / 中途反向的**逐帧采样**。
 *
 * 做法：在页面里装一个 rAF 采样器，每帧记录
 *   时间、--mp、data-mp、managing、画布矩形（含 CSS 变换）、内容区矩形、选中标签位置。
 * 然后点真实按钮触发动作（中途反向由页面内 rAF 精确触发：mp 跨过阈值那一帧就点收起，
 * 不靠 Node 侧的 sleep 猜时间）。
 *
 * **时间线只有一条**：采样一律走真实时长（不加 ?planetdemo=slow，也不用 CDP 的动画放缓）。
 * 需要「看得清每一帧画面」时才单独跑一遍慢放截图（--strip），慢放会明确写进报告，
 * 两遍的目的不同、数据不混用。
 *
 * 用法：
 *   node scripts/mp-verify/frames.mjs --url http://127.0.0.1:5103 --label rev \
 *        --out docs/mp-verify/c-planet/frames --actions expand,collapse,reverse [--strip]
 */
import { mkdirSync, writeFileSync } from "node:fs";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { Browser } from "./lib/cdp.mjs";
import { collectMetrics, DEFAULT_MANAGE_SELECTORS, openPlanetFromDock, toggleManagement, waitPlanetReady, waitSceneStatic } from "./lib/page.mjs";

const here = dirname(fileURLToPath(import.meta.url));
const args = process.argv.slice(2);
const flag = (name, fallback) => {
  const index = args.indexOf("--" + name);
  if (index < 0) return fallback;
  const value = args[index + 1];
  return value === undefined || value.startsWith("--") ? true : value;
};

const url = String(flag("url", "http://127.0.0.1:5103")).replace(/\/$/, "");
const label = String(flag("label", "rev"));
const outDir = resolve(String(flag("out", join(here, "..", "..", "docs", "mp-verify", "c-planet", "frames"))));
const viewportArg = String(flag("viewport", "1440x900"));
const [vpW, vpH] = viewportArg.split("x").map(Number);
const actions = String(flag("actions", "expand,collapse,reverse")).split(",").map((s) => s.trim()).filter(Boolean);
const openSelectors = String(flag("manage-open", DEFAULT_MANAGE_SELECTORS.open.join("|"))).split("|");
const closeSelectors = String(flag("manage-close", DEFAULT_MANAGE_SELECTORS.close.join("|"))).split("|");
const strip = Boolean(flag("strip", false));
const reverseAt = Number(flag("reverse-at", 0.5));

mkdirSync(outDir, { recursive: true });

const SAMPLER = `(opts) => {
  const state = { frames: [], on: true, t0: performance.now(), triggeredAt: null, triggerMp: null };
  const root = document.querySelector('.planet-view');
  const stage = document.querySelector('.planet-stage');
  const canvas = document.querySelector('.planet-canvas');
  const content = document.querySelector('.planet-content') || root;
  const readMp = () => {
    if (!root) return null;
    const raw = getComputedStyle(root).getPropertyValue('--mp');
    if (!raw) return null;
    const v = Number.parseFloat(raw);
    return Number.isFinite(v) ? v : null;
  };
  const clickFirst = (selectors) => {
    for (const selector of selectors) {
      const el = document.querySelector(selector);
      if (!el) continue;
      const r = el.getBoundingClientRect();
      if (r.width <= 0 || r.height <= 0) continue;
      el.click();
      return selector;
    }
    return null;
  };
  window.__mpSampler = state;
  window.__mpSamplerStop = () => { state.on = false; return state.frames; };
  const tick = () => {
    if (!state.on) return;
    const t = Math.round((performance.now() - state.t0) * 10) / 10;
    const mp = readMp();
    const cr = canvas ? canvas.getBoundingClientRect() : null;
    const sr = stage ? stage.getBoundingClientRect() : null;
    const kr = content ? content.getBoundingClientRect() : null;
    let label = null;
    try {
      if (typeof window.__qioPlanetStage === 'function') {
        const stageInfo = window.__qioPlanetStage();
        label = stageInfo && stageInfo.selectedLabel ? { x: Math.round(stageInfo.selectedLabel.x), y: Math.round(stageInfo.selectedLabel.y) } : null;
      }
    } catch (e) { label = null; }
    let renders = null;
    try {
      if (typeof window.__qioPlanetScene === 'function') {
        const scene = window.__qioPlanetScene();
        renders = scene && typeof scene.renders === 'number' ? scene.renders : null;
      }
    } catch (e) { renders = null; }
    state.frames.push({
      t,
      mp,
      managing: root ? root.classList.contains('managing') : null,
      dataMp: root ? root.getAttribute('data-mp') : null,
      canvas: cr ? { left: +cr.left.toFixed(1), centerX: +(cr.left + cr.width / 2).toFixed(1), width: +cr.width.toFixed(1), height: +cr.height.toFixed(1) } : null,
      stage: sr ? { left: +sr.left.toFixed(1), width: +sr.width.toFixed(1) } : null,
      content: kr ? { left: +kr.left.toFixed(1), width: +kr.width.toFixed(1) } : null,
      label,
      renders,
    });
    // 动作触发（在页面内按帧判定，精确到那一帧）
    if (opts.trigger === 'whenMpAtLeast' && state.triggeredAt === null && mp !== null && mp >= opts.value) {
      state.triggeredAt = t;
      state.triggerMp = mp;
      state.triggeredSelector = clickFirst(opts.selectors || []);
    }
    if (opts.trigger === 'whenMpAtMost' && state.triggeredAt === null && mp !== null && mp <= opts.value) {
      state.triggeredAt = t;
      state.triggerMp = mp;
      state.triggeredSelector = clickFirst(opts.selectors || []);
    }
    requestAnimationFrame(tick);
  };
  requestAnimationFrame(tick);
  return true;
}`;

/** 一轮动作：可选先展开，再触发动作，采样到最后稳定。 */
async function runAction(browser, action) {
  await browser.evalJs("(() => { const stop = window.__mpSamplerStop; if (typeof stop === 'function') stop(); return true; })()");
  // 起始状态：展开动作从收起态开始；收起/反向从展开态开始
  if (action !== "expand") {
    const already = await browser.evalJs(
      "(() => { const v = document.querySelector('.planet-view'); const raw = v ? getComputedStyle(v).getPropertyValue('--mp') : ''; return raw ? Number.parseFloat(raw) : 0; })()",
    );
    if (!(already > 0.999)) {
      await toggleManagement(browser, "open", { open: openSelectors, close: closeSelectors }).catch(() => {});
      await new Promise((r) => setTimeout(r, 1200));
    }
  } else {
    const already = await browser.evalJs(
      "(() => { const v = document.querySelector('.planet-view'); const raw = v ? getComputedStyle(v).getPropertyValue('--mp') : ''; return raw ? Number.parseFloat(raw) : 0; })()",
    );
    if (already > 0.001) {
      await toggleManagement(browser, "close", { open: openSelectors, close: closeSelectors }).catch(() => {});
      await new Promise((r) => setTimeout(r, 1200));
    }
  }
  await new Promise((r) => setTimeout(r, 400));

  let samplerOpts = { trigger: "none" };
  if (action === "expand") samplerOpts = {};
  if (action === "collapse") samplerOpts = {};
  if (action === "reverse") samplerOpts = { trigger: "whenMpAtLeast", value: reverseAt, selectors: closeSelectors };
  await browser.evalJs("(" + SAMPLER.toString() + ")(" + JSON.stringify(samplerOpts) + ")");

  const t0 = Date.now();
  if (action === "expand") await toggleManagement(browser, "open", { open: openSelectors, close: closeSelectors });
  if (action === "collapse") await toggleManagement(browser, "close", { open: openSelectors, close: closeSelectors });
  if (action === "reverse") await toggleManagement(browser, "open", { open: openSelectors, close: closeSelectors });

  // 等到 mp 到达目标并稳定（最多 6s），再多留 300ms 采「静止后」的帧
  const deadline = Date.now() + 6000;
  const target = action === "expand" ? 1 : 0;
  let stableSince = null;
  while (Date.now() < deadline) {
    const mp = await browser.evalJs("(() => { const v = document.querySelector('.planet-view'); const raw = v ? getComputedStyle(v).getPropertyValue('--mp') : ''; return raw ? Number.parseFloat(raw) : null; })()");
    const done = mp === null ? true : Math.abs(mp - target) <= 0.002;
    if (done) {
      if (stableSince === null) stableSince = Date.now();
      if (Date.now() - stableSince >= 320) break;
    } else {
      stableSince = null;
    }
    await new Promise((r) => setTimeout(r, 60));
  }
  const frames = await browser.evalJs("(() => { const stop = window.__mpSamplerStop; return typeof stop === 'function' ? stop() : []; })()");
  const elapsed = Date.now() - t0;
  const contentLeft = frames.find((f) => f.content)?.content.left ?? 0;
  const curve = frames
    .filter((f) => f.mp !== null)
    .map((f) => ({
      t: f.t,
      mp: Number(f.mp.toFixed(4)),
      centerX: f.canvas ? Number((f.canvas.centerX - contentLeft).toFixed(1)) : null,
      canvasLeft: f.canvas ? Number((f.canvas.left - contentLeft).toFixed(1)) : null,
      labelX: f.label ? Number((f.label.x - contentLeft).toFixed(1)) : null,
      renders: f.renders,
    }));

  // 连续性分析
  let maxStep = 0;
  let jumps = [];
  let reversals = 0;
  let prevDirection = 0;
  for (let i = 1; i < curve.length; i += 1) {
    const step = curve[i].mp - curve[i - 1].mp;
    if (Math.abs(step) > maxStep) maxStep = Math.abs(step);
    if (Math.abs(step) > 0.16) jumps.push({ at: curve[i].t, step: Number(step.toFixed(4)) });
    const direction = Math.sign(step);
    if (direction !== 0 && prevDirection !== 0 && direction !== prevDirection) reversals += 1;
    if (direction !== 0) prevDirection = direction;
  }
  const trigger = await browser.evalJs("(() => ({ at: window.__mpSampler ? window.__mpSampler.triggeredAt : null, mp: window.__mpSampler ? window.__mpSampler.triggerMp : null, selector: window.__mpSampler ? window.__mpSampler.triggeredSelector : null }))()");
  const overshoot = (() => {
    if (action !== "reverse" || trigger.at === null) return null;
    const after = curve.filter((f) => f.t >= trigger.at);
    if (!after.length) return null;
    return Number((Math.max(...after.map((f) => f.mp)) - trigger.mp).toFixed(4));
  })();

  const summary = {
    action,
    viewport: viewportArg,
    elapsedMs: elapsed,
    frames: curve.length,
    frameRate: curve.length > 1 ? Number(((curve.length - 1) / ((curve[curve.length - 1].t - curve[0].t) / 1000)).toFixed(1)) : null,
    durationMs: curve.length ? Number((curve[curve.length - 1].t - curve[0].t).toFixed(1)) : 0,
    maxStep: Number(maxStep.toFixed(4)),
    jumps,
    reversals,
    trigger,
    reverseOvershoot: overshoot,
    startMp: curve.length ? curve[0].mp : null,
    endMp: curve.length ? curve[curve.length - 1].mp : null,
    centerX: { start: curve[0]?.centerX ?? null, end: curve[curve.length - 1]?.centerX ?? null },
    labelX: { start: curve[0]?.labelX ?? null, end: curve[curve.length - 1]?.labelX ?? null },
    curve,
  };
  return summary;
}

const report = { url, label, viewport: viewportArg, startedAt: new Date().toISOString(), actions: [], notes: [] };
const browser = new Browser({ port: Number(flag("cdp-port", process.env.QIO_MP_CDP_PORT ?? 9801)), width: vpW, height: vpH });
try {
  await browser.launch();
  await browser.setViewport(vpW, vpH);
  await browser.navigate(url + "/?verify=" + Date.now() + "#/", 2600);
  await browser.waitFor("!!document.querySelector('[data-planet-entry], .dock')", { timeout: 25000 });
  await openPlanetFromDock(browser);
  await waitPlanetReady(browser);
  await waitSceneStatic(browser);
  report.baseMetrics = await collectMetrics(browser);

  for (const action of actions) {
    const summary = await runAction(browser, action);
    report.actions.push(summary);
    writeFileSync(join(outDir, label + "-" + viewportArg + "-" + action + ".json"), JSON.stringify(summary, null, 2), "utf8");
    console.log(
      "[" + action + "] frames=" + summary.frames + " dur=" + summary.durationMs + "ms fps≈" + summary.frameRate +
        " maxStep=" + summary.maxStep + " jumps=" + summary.jumps.length +
        " centerX " + summary.centerX.start + " → " + summary.centerX.end +
        " labelX " + summary.labelX.start + " → " + summary.labelX.end +
        (summary.reverseOvershoot !== null ? " overshoot=" + summary.reverseOvershoot : "") +
        " trigger@" + (summary.trigger.at === null ? "-" : summary.trigger.at + "ms"),
    );
  }

  if (strip) {
    // 慢放截图条：**另一遍**，只用来看阶段顺序，曲线数据仍以上面那遍为准
    report.notes.push("strip：使用 ?planetdemo=slow 的慢放时间线单独跑一遍截图，仅用于目视阶段顺序；数值曲线来自真实时长那一遍。");
    for (const action of actions.filter((a) => a !== "reverse")) {
      await browser.navigate(url + "/?planetdemo=slow&verify=" + Date.now() + "#/", 2600);
      await browser.waitFor("!!document.querySelector('[data-planet-entry], .dock')", { timeout: 25000 });
      await openPlanetFromDock(browser);
      await waitPlanetReady(browser);
      await waitSceneStatic(browser);
      if (action === "collapse") {
        await toggleManagement(browser, "open", { open: openSelectors, close: closeSelectors }).catch(() => {});
        await new Promise((r) => setTimeout(r, 2400));
      }
      const fire = action === "expand" ? "open" : "close";
      const selector =
        fire === "open"
          ? await browser.evalJs("(() => { for (const s of " + JSON.stringify(openSelectors) + ") { const el = document.querySelector(s); if (el) return s; } return null; })()")
          : await browser.evalJs("(() => { for (const s of " + JSON.stringify(closeSelectors) + ") { const el = document.querySelector(s); if (el) return s; } return null; })()");
      if (!selector) {
        report.notes.push(action + " 慢放截图跳过：找不到按钮");
        continue;
      }
      const rect = await browser.rectOf(selector);
      const marks = [120, 420, 760, 1100, 1500, 1900];
      const start = Date.now();
      await browser.clickAt(rect.x + rect.width / 2, rect.y + rect.height / 2);
      for (let i = 0; i < marks.length; i += 1) {
        const wait = marks[i] - (Date.now() - start);
        if (wait > 0) await new Promise((r) => setTimeout(r, wait));
        await browser.shotFile(join(outDir, label + "-" + viewportArg + "-" + action + "-slow-" + String(i).padStart(2, "0") + ".png"), null, false);
      }
    }
  }
} catch (error) {
  report.error = String(error && error.stack ? error.stack : error);
  console.error("逐帧采样失败：" + report.error);
} finally {
  report.consoleErrors = browser.consoleErrors;
  report.finishedAt = new Date().toISOString();
  writeFileSync(join(outDir, label + "-" + viewportArg + "-report.json"), JSON.stringify(report, null, 2), "utf8");
  await browser.close();
  console.log("报告：" + join(outDir, label + "-" + viewportArg + "-report.json"));
}
