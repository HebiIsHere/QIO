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
/**
 * 反向触发时刻（毫秒，从点展开算起）。
 *
 * 为什么按**时间**而不是按 --mp 阈值：软件光栅下偶尔掉帧（实测有 220ms 的停顿），
 * 进度会在一帧里跨过 0.5，把「阈值触发」变成「几乎展开完才反向」—— 那测的就不是中途反向了。
 * 按时间触发才稳定落在动画中段。
 */
const reverseAfterMs = Number(flag("reverse-after-ms", 240));

mkdirSync(outDir, { recursive: true });

const SAMPLER = `(opts) => {
  const state = { frames: [], on: true, t0: performance.now(), triggeredAt: null, triggerMp: null, firstMoveAt: null };
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
    if (state.firstMoveAt === null && mp !== null && mp > 0.001) state.firstMoveAt = t;
    if (
      opts.trigger === 'whenElapsedAtLeast' &&
      state.triggeredAt === null &&
      state.firstMoveAt !== null &&
      t - state.firstMoveAt >= opts.value
    ) {
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
  // 起始状态：展开与「中途反向」都从**收起态**开始（反向要的是「正展开到一半被收起打断」）；
  // 只有收起动作才需要先展开。
  if (action === "collapse") {
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
  if (action === "reverse") {
    // 面板可能按 A 的新编排「过渡结束才挂载」，那时收起按钮还不存在 —— 退回到展开按钮（它是开关）
    samplerOpts = {
      trigger: "whenElapsedAtLeast",
      value: reverseAfterMs,
      selectors: [...closeSelectors, ...openSelectors],
    };
  }
  await browser.evalJs("(" + SAMPLER.toString() + ")(" + JSON.stringify(samplerOpts) + ")");

  /**
   * 归一化的基准必须是**动画时长**（--mo-3-expand / --mo-3-collapse），不是采样窗口长度。
   * 用采样窗口当基准会把 normStep 系统性放大（窗口里还包含点击派发、长帧、动画结束后的等待）。
   */
  const animDurationMs = await browser.evalJs(
    "(() => { const v = document.querySelector('.planet-view') || document.documentElement; const cs = getComputedStyle(v); const pick = (name) => { const raw = cs.getPropertyValue(name).trim(); const value = Number.parseFloat(raw); if (!Number.isFinite(value)) return null; return raw.endsWith('ms') ? value : raw.endsWith('s') ? value * 1000 : value; }; return { expand: pick('--mo-3-expand'), collapse: pick('--mo-3-collapse') }; })()",
  );

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
  let maxRate = 0;
  const jumps = [];
  let reversals = 0;
  let prevDirection = 0;
  for (let i = 1; i < curve.length; i += 1) {
    const step = curve[i].mp - curve[i - 1].mp;
    const dt = Math.max(1e-6, curve[i].t - curve[i - 1].t);
    if (Math.abs(step) > maxStep) maxStep = Math.abs(step);
    maxRate = Math.max(maxRate, (Math.abs(step) / dt) * 1000);
    /**
     * 「瞬移」的判据必须按**速率**看，不能只看单帧差值：
     * 软件光栅下偶尔会掉一帧（30–200ms），一个慢帧里进度当然走得多。
     * 这里只有「一帧 40ms 内走了 0.16 以上」（≈4 进度/秒以上）才算可疑跳变。
     */
    if (Math.abs(step) > 0.16 && dt < 40) jumps.push({ at: curve[i].t, step: Number(step.toFixed(4)), dt });
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

  /**
   * 逐帧明细 + 判读口径（Lead 定）：
   * - 本机是 SwiftShader 软件渲染，单帧 25–110ms、偶发 200ms 长帧，长帧里进度自然走得远；
   * - 所以「瞬移」不能用 |Δmp| 绝对值判断，而要**按帧时间归一化**：
   *   normStep = Δmp / (dt / durationMs) —— 一个「理想帧长」里本该走的步进；
   * - normStep > 1.6 才算可疑；同时把长帧单独列出（longFrames，dt > 60ms）。
   */
  const totalDuration = curve.length ? Math.max(1, curve[curve.length - 1].t - curve[0].t) : 1;
  /** 归一化基准：优先用令牌给的动画时长（520 / 420ms），拿不到才退回采样窗口 */
  const baseDuration =
    action === "collapse"
      ? animDurationMs && animDurationMs.collapse
        ? animDurationMs.collapse
        : totalDuration
      : animDurationMs && animDurationMs.expand
        ? animDurationMs.expand
        : totalDuration;
  const perFrame = [];
  const longFrames = [];
  const teleports = [];
  for (let i = 1; i < curve.length; i += 1) {
    const dt = Number((curve[i].t - curve[i - 1].t).toFixed(1));
    const step = Number((curve[i].mp - curve[i - 1].mp).toFixed(4));
    const normStep = Number((step / (dt / baseDuration)).toFixed(3));
    perFrame.push({ t: Math.round(curve[i].t), dt, step, normStep });
    if (dt > 60) longFrames.push({ t: Math.round(curve[i].t), dt });
    if (Math.abs(normStep) > 1.6) teleports.push({ t: Math.round(curve[i].t), dt, step, normStep });
  }

  const summary = {
    action,
    perFrame,
    longFrames,
    teleports,
    verdict:
      teleports.length === 0
        ? "无瞬移（归一化步进全部 ≤1.6）"
        : "存在可疑跳变：" + teleports.length + " 处（归一化步进 >1.6）",
    viewport: viewportArg,
    elapsedMs: elapsed,
    frames: curve.length,
    frameRate: curve.length > 1 ? Number(((curve.length - 1) / ((curve[curve.length - 1].t - curve[0].t) / 1000)).toFixed(1)) : null,
    durationMs: curve.length ? Number((curve[curve.length - 1].t - curve[0].t).toFixed(1)) : 0,
    maxStep: Number(maxStep.toFixed(4)),
    /** 最大进度速率（进度/秒）：与令牌曲线在 1.0 距离上的峰值速率对比，用来判断有没有「跳一下」 */
    maxRate: Number(maxRate.toFixed(2)),
    /** 归一化基准（ms）：令牌里的动画时长 */
    baseDurationMs: baseDuration,
    animDurationTokens: animDurationMs,
    jumps,
    reversals,
    trigger,
    firstMoveAt: await browser.evalJs("(() => (window.__mpSampler ? window.__mpSampler.firstMoveAt : null))()"),
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