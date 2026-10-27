/**
 * 渲染收尾与减少动态效果探针。
 *
 * 三件事，每个都给「可复核的读数」而不是描述：
 * 1. **动画结束后是否还在绘帧**：静置 2.5s，读两次渲染计数（usePlanetScene 的 DEV 钩子
 *    __qioPlanetScene().renders）与 CDP 的 Frames 指标；两者都应当不再增长。
 * 2. **交互恢复后是否重新绘帧**：点击一下画布，计数必须重新增长（停帧不能把交互冻住）。
 * 3. **减少动态效果下直接到位**：把偏好设为 reduced（写 localStorage(qio-motion) + 重载，
 *    与设置页写的是同一份），再展开管理区，逐帧看 --mp：应当在一两帧内到达目标。
 *
 * 用法：
 *   node scripts/mp-verify/probe.mjs --url http://127.0.0.1:5103 --label rev \
 *        --out docs/mp-verify/c-planet/probe [--viewport 1440x900]
 */
import { mkdirSync, writeFileSync } from "node:fs";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { Browser } from "./lib/cdp.mjs";
import { DEFAULT_MANAGE_SELECTORS, collectMetrics, openPlanetFromDock, toggleManagement, waitPlanetReady, waitSceneStatic } from "./lib/page.mjs";

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
const outDir = resolve(String(flag("out", join(here, "..", "..", "docs", "mp-verify", "c-planet", "probe"))));
const viewportArg = String(flag("viewport", "1440x900"));
const [vpW, vpH] = viewportArg.split("x").map(Number);
const openSelectors = String(flag("manage-open", DEFAULT_MANAGE_SELECTORS.open.join("|"))).split("|");
const closeSelectors = String(flag("manage-close", DEFAULT_MANAGE_SELECTORS.close.join("|"))).split("|");
mkdirSync(outDir, { recursive: true });

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
async function renderCounter(browser) {
  const scene = await browser.evalJs(
    "(() => { const s = (typeof window.__qioPlanetScene === 'function') ? window.__qioPlanetScene() : null; return s ? { renders: s.renders ?? null, loopActive: s.loopActive ?? null, paused: s.paused ?? null } : null; })()",
  );
  const metrics = await browser.metrics();
  return { scene, cdpFrames: metrics.Frames ?? null, scriptDuration: metrics.ScriptDuration ?? null };
}

async function openApp(browser, query = "") {
  await browser.navigate(url + "/?" + (query ? query + "&" : "") + "verify=" + Date.now() + "#/", 2600);
  await browser.waitFor("!!document.querySelector('[data-planet-entry], .dock')", { timeout: 25000 });
  await openPlanetFromDock(browser);
  await waitPlanetReady(browser);
  await waitSceneStatic(browser);
}

const report = { url, label, viewport: viewportArg, startedAt: new Date().toISOString(), checks: {}, notes: [] };
const browser = new Browser({ port: Number(flag("cdp-port", process.env.QIO_MP_CDP_PORT ?? 9801)), width: vpW, height: vpH });
const delta = (a, b) => (typeof a === "number" && typeof b === "number" ? b - a : null);
try {
  await browser.launch();
  await browser.setViewport(vpW, vpH);
  await openApp(browser);
  report.metrics = await collectMetrics(browser);

  // 1) 静置后是否停帧
  const before = await renderCounter(browser);
  await sleep(2500);
  const after = await renderCounter(browser);
  report.checks.idleStopsRendering = {
    rendersBefore: before.scene?.renders ?? null,
    rendersAfter: after.scene?.renders ?? null,
    rendersDelta: delta(before.scene?.renders, after.scene?.renders),
    cdpFramesBefore: before.cdpFrames,
    cdpFramesAfter: after.cdpFrames,
    cdpFramesDelta: delta(before.cdpFrames, after.cdpFrames),
    loopActive: after.scene?.loopActive ?? null,
    paused: after.scene?.paused ?? null,
    verdict:
      after.scene && typeof after.scene.renders === "number"
        ? delta(before.scene.renders, after.scene.renders) === 0
          ? "静置 2.5s 渲染计数不再增长（停了）"
          : "静置期间仍在绘帧"
        : "没有渲染计数钩子，只有 CDP Frames 作为旁证",
  };

  // 2) 交互恢复后重新绘帧
  const rect = await browser.rectOf(".planet-canvas");
  if (rect) {
    await browser.clickAt(rect.x + rect.width / 2, rect.y + rect.height / 2);
    await sleep(220);
  }
  const interactive = await renderCounter(browser);
  report.checks.interactionResumesRendering = {
    rendersAfterInteraction: interactive.scene?.renders ?? null,
    rendersDelta: delta(after.scene?.renders, interactive.scene?.renders),
    loopActive: interactive.scene?.loopActive ?? null,
    verdict:
      interactive.scene && typeof interactive.scene.renders === "number"
        ? (delta(after.scene.renders, interactive.scene.renders) ?? 0) > 0
          ? "交互后渲染恢复"
          : "交互后没有重新绘帧（需要排查）"
        : "无渲染计数钩子",
  };

  // 3) 展开动作期间的帧率
  await browser.evalJs("(() => { window.__probe = { frames: [], on: true }; const t0 = performance.now(); const tick = () => { if (!window.__probe.on) return; window.__probe.frames.push(Math.round(performance.now() - t0)); requestAnimationFrame(tick); }; requestAnimationFrame(tick); return true; })()");
  await toggleManagement(browser, "open", { open: openSelectors, close: closeSelectors }).catch(() => {});
  await sleep(1600);
  const animationFrames = await browser.evalJs("(() => { window.__probe.on = false; const f = window.__probe.frames; return { frames: f.length, spanMs: f.length ? f[f.length - 1] - f[0] : 0 }; })()");
  report.checks.animationFrameRate = {
    ...animationFrames,
    fps: animationFrames.spanMs ? Number(((animationFrames.frames - 1) / (animationFrames.spanMs / 1000)).toFixed(1)) : null,
  };
  // 4) 减少动态效果：写 localStorage(qio-motion=reduced) 后重开，展开应当一帧到位
  await browser.evalJs("(() => { try { localStorage.setItem('qio-motion', 'reduced'); } catch (e) {} return true; })()");
  await openApp(browser);
  const motionMode = await browser.evalJs("document.documentElement.getAttribute('data-motion')");
  await browser.evalJs("(() => { window.__probe2 = { frames: [], on: true }; const t0 = performance.now(); const tick = () => { if (!window.__probe2.on) return; const v = document.querySelector('.planet-view'); const raw = v ? getComputedStyle(v).getPropertyValue('--mp') : ''; window.__probe2.frames.push({ t: Math.round(performance.now() - t0), mp: raw ? Number.parseFloat(raw) : null }); requestAnimationFrame(tick); }; requestAnimationFrame(tick); return true; })()");
  await toggleManagement(browser, "open", { open: openSelectors, close: closeSelectors }).catch(() => {});
  await sleep(900);
  const reducedFrames = await browser.evalJs("(() => { window.__probe2.on = false; return window.__probe2.frames; })()");
  const reached = reducedFrames.filter((f) => f.mp !== null && f.mp >= 0.999);
  report.checks.reducedMotion = {
    dataMotion: motionMode,
    frames: reducedFrames.length,
    framesToTarget: reached.length ? reducedFrames.indexOf(reached[0]) : null,
    msToTarget: reached.length ? reached[0].t : null,
    verdict:
      motionMode !== "reduced"
        ? "减少动画偏好没有生效（data-motion=" + motionMode + "），该项未验证"
        : reached.length && reached[0].t <= 120
          ? "减少动画下 " + reached[0].t + "ms 内直接到位（约一帧）"
          : reached.length
            ? "减少动画下 " + reached[0].t + "ms 到位（超过一帧，需要排查）"
            : "减少动画下没有测到到达目标",
    curve: reducedFrames.slice(0, 24),
  };
  report.notes.push("reduced 模式通过 localStorage(qio-motion=reduced) + 重载生效，与设置页写的是同一份偏好；脚本结束时会把该偏好删掉。");
  console.log(JSON.stringify(report.checks, null, 2));
} catch (error) {
  report.error = String(error && error.stack ? error.stack : error);
  console.error("探针失败：" + report.error);
} finally {
  try {
    await browser.evalJs("(() => { try { localStorage.removeItem('qio-motion'); } catch (e) {} return true; })()");
  } catch (error) {
    void error;
  }
  report.finishedAt = new Date().toISOString();
  writeFileSync(join(outDir, label + "-" + viewportArg + "-probe.json"), JSON.stringify(report, null, 2), "utf8");
  await browser.close();
  console.log("报告：" + join(outDir, label + "-" + viewportArg + "-probe.json"));
}
