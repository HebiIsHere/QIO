// QIO 互动模式底部操作区 · 预览动效基准测量（阶段 1）
//
// 目的：把「用户确认的参考预览」QIO_Animated_Chat_Preview.html 的开合动效变成可复现的
// 逐帧数值基准，供实现（conversationMotion.ts + InteractiveConversation.vue）校准。
// 浏览器层复用仓库既有的 scripts/verify-pm-qio032/cdp.mjs（系统 Edge + CDP，无 Playwright）。
//
// 跑法：
//   node scripts/verify-interactive-action/preview-baseline.mjs
//   node scripts/verify-interactive-action/preview-baseline.mjs --width=1000 --height=600 --out=<目录>
// 产物：<out>/preview-baseline.json + <out>/shots/preview-*.png
//
// 测量口径（与 PLAN.md §5-1 对齐）：
//   * 原点 t0 = 页面内 #toggle 的 click 事件时间（click 里同步 setExpanded → startMotion）。
//   * 每帧记录 performance.now()、revealMotion.value/target、heightMotion.value/target、
//     leftStroke.style.strokeDasharray（= visible length）、getTotalLength()、路径几何。
//   * 到角 = 首帧 visible >= horizontal，其中 horizontal = (shell.clientWidth/2 - halfHandle)
//     - (edge + corner)，edge=0.75、corner=14、halfHandle=lever.clientWidth/2-4，
//     与 drawFrame() 里的 d 属性同源。
//   * 帧量化值之外，再按相邻两帧线性插值给一个亚帧估计，并给出 50%/90% 高度、settle 时间。
import { createHash } from "node:crypto";
import { mkdirSync, readFileSync, writeFileSync } from "node:fs";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";
import { CdpBrowser, Page, sleep } from "../verify-pm-qio032/cdp.mjs";

const HERE = dirname(fileURLToPath(import.meta.url));
const ROOT = resolve(HERE, "..", "..");

function argValue(name, fallback) {
  const prefix = "--" + name + "=";
  for (let i = 2; i < process.argv.length; i += 1) {
    if (process.argv[i].indexOf(prefix) === 0) return process.argv[i].slice(prefix.length);
  }
  return fallback;
}

const PREVIEW_FILE = resolve(argValue("file", "C:/Users/zxy/Downloads/QIO_Animated_Chat_Preview.html"));
const OUT_DIR = resolve(argValue("out", join(ROOT, "_lead-logs", "interactive-action")));
const SHOTS = join(OUT_DIR, "shots");
const WIDTH = Number(argValue("width", "1000"));
const HEIGHT = Number(argValue("height", "600"));
const EXTRA_WIDTH = Number(argValue("extra-width", "720"));
const CDP_PORT = Number(argValue("cdp-port", process.env.QIO_CDP_PORT || "9333"));
mkdirSync(SHOTS, { recursive: true });

const results = [];
function record(name, ok, detail) {
  const status = ok === null ? "UNVERIFIED" : ok ? "PASS" : "FAIL";
  results.push({ name: name, status: status, detail: detail === undefined ? null : detail });
  console.log("[" + status + "] " + name + (detail === undefined ? "" : " :: " + JSON.stringify(detail).slice(0, 700)));
}

function round(value, digits) {
  if (value === null || value === undefined || !isFinite(value)) return null;
  const factor = Math.pow(10, digits === undefined ? 1 : digits);
  return Math.round(value * factor) / factor;
}

/** 临界阻尼进度：1-(1+rt)e^{-rt}（与预览 approach() 的解析解同源）。 */
function criticallyDampedProgress(t, rate) {
  return 1 - (1 + rate * t) * Math.exp(-rate * t);
}
/** 反解：达到 progress 的解析耗时（临界阻尼）。 */
function criticallyDampedDuration(progress, rate) {
  if (progress <= 0) return 0;
  if (progress >= 1) return null;
  let low = 0;
  let high = 5;
  for (let i = 0; i < 80; i += 1) {
    const mid = (low + high) / 2;
    if (criticallyDampedProgress(mid, rate) < progress) low = mid;
    else high = mid;
  }
  return high;
}

/** 在页面里安装 rAF 采样器；函数整体 toString() 后注入页面，自包含。 */
function installSampler() {
  var state = {
    frames: [],
    collecting: false,
    clickT: null,
    pointerDownT: null,
    mouseUpT: null,
    installedAt: performance.now(),
  };
  window.__previewProbe = state;
  var shell = document.getElementById("shell");
  var lever = document.getElementById("toggle");
  var composer = document.getElementById("composer");
  var chat = document.getElementById("chat");
  var outline = document.getElementById("outline");
  var leftStroke = document.getElementById("frameLeft");
  var rightStroke = document.getElementById("frameRight");

  function num(value) {
    var n = parseFloat(value);
    return isFinite(n) ? n : null;
  }
  function readMotion() {
    try {
      return {
        reveal: revealMotion.value,
        revealTarget: revealMotion.target,
        height: heightMotion.value,
        heightTarget: heightMotion.target,
      };
    } catch (error) {
      return null;
    }
  }
  function snapshot() {
    var motion = readMotion();
    var shellW = shell.clientWidth;
    var center = shellW / 2;
    var halfHandle = lever.clientWidth / 2 - 4;
    var shellRect = shell.getBoundingClientRect();
    var inputRect = composer.getBoundingClientRect();
    var inputTop = inputRect.top - shellRect.top;
    var y = lever.offsetTop + lever.offsetHeight / 2;
    var end = inputTop + inputRect.height / 2;
    var edge = 0.75;
    var corner = 14;
    var horizontal = center - halfHandle - (edge + corner);
    var length = null;
    try {
      length = leftStroke.getTotalLength();
    } catch (error) {
      length = null;
    }
    var dash = leftStroke.style.strokeDasharray;
    var visible = null;
    if (dash && dash !== "none") visible = num(String(dash).split(" ")[0]);
    var shellStyle = getComputedStyle(shell);
    var chatStyle = getComputedStyle(chat);
    return {
      t: performance.now(),
      shellW: shellW,
      center: center,
      halfHandle: halfHandle,
      y: y,
      inputTop: inputTop,
      inputHeight: inputRect.height,
      end: end,
      horizontal: horizontal,
      length: length,
      reveal: motion ? motion.reveal : null,
      revealTarget: motion ? motion.revealTarget : null,
      height: motion ? motion.height : null,
      heightTarget: motion ? motion.heightTarget : null,
      visible: visible,
      dash: dash,
      rightDash: rightStroke ? rightStroke.style.strokeDasharray : null,
      chatShown: num(chatStyle.height),
      shellH: shellRect.height,
      shellTop: shellRect.top,
      expanded: shell.getAttribute("data-open") === "true",
      glassOpacity: shellStyle.getPropertyValue("--glass-opacity").trim(),
      glassInset: shellStyle.getPropertyValue("--glass-inset").trim(),
      outlineHidden: outline.hasAttribute("hidden"),
      innerWidth: window.innerWidth,
      innerHeight: window.innerHeight,
    };
  }
  state.snapshot = snapshot;
  state.start = function () {
    state.frames = [];
    state.collecting = true;
    state.t0 = performance.now();
    var tick = function () {
      if (!state.collecting) return;
      state.frames.push(snapshot());
      window.requestAnimationFrame(tick);
    };
    window.requestAnimationFrame(tick);
    return state.t0;
  };
  state.stop = function () {
    state.collecting = false;
    return state.frames.length;
  };
  lever.addEventListener("pointerdown", function () { state.pointerDownT = performance.now(); }, true);
  lever.addEventListener("click", function () { state.clickT = performance.now(); }, true);
  document.addEventListener("pointerup", function () { state.mouseUpT = performance.now(); }, true);
  return snapshot();
}

async function waitPreviewReady(page) {
  await page.waitFor(
    "document.getElementById('shell') && document.getElementById('toggle') && document.getElementById('shell').clientWidth > 0 && document.querySelectorAll('#notes .note').length >= 2",
    { timeout: 20000, label: "预览页就绪" }
  );
  await sleep(350);
}

async function fastClick(page, selector) {
  const info = await page.box(selector);
  if (!info || !info.visible) throw new Error("点不到：" + selector);
  const base = { x: info.cx, y: info.cy, button: "left", buttons: 1, clickCount: 1 };
  await page.browser.send("Input.dispatchMouseEvent", { type: "mouseMoved", x: info.cx, y: info.cy, button: "none", buttons: 0 }, page.sessionId);
  await page.browser.send("Input.dispatchMouseEvent", { type: "mousePressed", ...base }, page.sessionId);
  await sleep(18);
  await page.browser.send("Input.dispatchMouseEvent", { type: "mouseReleased", ...base, buttons: 0 }, page.sessionId);
  return info;
}

async function startSampler(page) {
  await page.eval("(function(){ window.__previewProbe.start(); return true; })()");
}
async function stopSampler(page) {
  return page.eval("(function(){ var s = window.__previewProbe; s.stop(); return { clickT: s.clickT, pointerDownT: s.pointerDownT, mouseUpT: s.mouseUpT, frames: s.frames }; })()");
}

function analyze(frames, originT, phase) {
  const after = frames.filter((f) => f.t >= originT - 2);
  const before = frames.filter((f) => f.t < originT);
  const pre = before.length ? before[before.length - 1] : null;
  const last = after.length ? after[after.length - 1] : null;
  const rel = (t) => round(t - originT, 1);
  const intervals = [];
  for (let i = 1; i < after.length; i += 1) intervals.push(after[i].t - after[i - 1].t);
  intervals.sort((a, b) => a - b);
  const frameStats = {
    samples: after.length,
    durationMs: after.length ? rel(last.t) : null,
    intervalMedianMs: intervals.length ? round(intervals[Math.floor(intervals.length / 2)], 1) : null,
    intervalMaxMs: intervals.length ? round(intervals[intervals.length - 1], 1) : null,
  };

  let heightTarget = null;
  for (let i = after.length - 1; i >= 0; i -= 1) {
    if (after[i].heightTarget !== null) {
      heightTarget = after[i].heightTarget;
      break;
    }
  }
  let heightStart = pre && pre.height !== null ? pre.height : after.length ? after[0].height : null;
  if (phase === "expand" && heightStart === null) heightStart = 0;

  // 到角：首帧 visible >= horizontal（用亚帧线性插值给一个估计）。
  let cornerIndex = -1;
  if (phase === "expand") {
    for (let i = 0; i < after.length; i += 1) {
      const f = after[i];
      if (f.visible !== null && f.horizontal !== null && f.length !== null && f.reveal !== null && f.reveal > 0 && f.visible >= f.horizontal) {
        cornerIndex = i;
        break;
      }
    }
  }
  let corner = null;
  if (cornerIndex >= 0) {
    const cur = after[cornerIndex];
    const prev = cornerIndex > 0 ? after[cornerIndex - 1] : null;
    let interpMs = rel(cur.t);
    if (prev && prev.visible !== null && cur.visible !== null && cur.visible > prev.visible) {
      const frac = (cur.horizontal - prev.visible) / (cur.visible - prev.visible);
      interpMs = round(prev.t - originT + frac * (cur.t - prev.t), 1);
    }
    corner = {
      frameIndex: cornerIndex,
      frameMs: rel(cur.t),
      interpMs: interpMs,
      visibleAtCross: round(cur.visible, 2),
      horizontalLength: round(cur.horizontal, 2),
      pathLengthAtCross: round(cur.length, 2),
      dynamicCornerFraction: round(cur.horizontal / cur.length, 4),
      revealAtCross: round(cur.reveal, 4),
      nextFrameMs: cornerIndex + 1 < after.length ? rel(after[cornerIndex + 1].t) : null,
    };
  }

  // 静态（settled）几何：到角所需 reveal 比例，以及解析/实测耗时对照。
  let settledFrame = null;
  for (let i = after.length - 1; i >= 0; i -= 1) {
    const f = after[i];
    if (f.length !== null && f.horizontal !== null && f.length > 0 && (phase === "collapse" ? f.reveal === 0 : f.reveal === 1)) {
      settledFrame = f;
      break;
    }
  }
  const staticCornerFraction =
    settledFrame && settledFrame.length ? round(settledFrame.horizontal / settledFrame.length, 4) : null;
  const analyticCornerMs =
    staticCornerFraction !== null ? round(criticallyDampedDuration(staticCornerFraction, 11) * 1000, 1) : null;

  // 高度 50% / 90%（展开向上；收起向下）。
  function heightCross(fraction, direction) {
    const goal = direction === "up" ? heightStart + (heightTarget - heightStart) * fraction : heightStart * (1 - fraction);
    for (let i = 0; i < after.length; i += 1) {
      const f = after[i];
      if (f.height === null) continue;
      const hit = direction === "up" ? f.height >= goal : f.height <= goal;
      if (!hit) continue;
      const prev = i > 0 ? after[i - 1] : null;
      if (prev && prev.height !== null && f.height !== prev.height) {
        const frac = (goal - prev.height) / (f.height - prev.height);
        return round(prev.t - originT + frac * (f.t - prev.t), 1);
      }
      return rel(f.t);
    }
    return null;
  }
  const height50 = phase === "expand" ? heightCross(0.5, "up") : heightCross(0.5, "down");
  const height90 = phase === "expand" ? heightCross(0.9, "up") : heightCross(0.9, "down");

  // settle：最后一个「还在变」的帧之后的下一帧。
  let lastChanging = -1;
  for (let i = 0; i < after.length; i += 1) {
    const f = after[i];
    const revealTarget = f.revealTarget;
    const hTarget = f.heightTarget;
    if (f.reveal === null || f.height === null) continue;
    if (Math.abs(f.reveal - (revealTarget === null ? (phase === "expand" ? 1 : 0) : revealTarget)) > 0.0005) {
      lastChanging = i;
      continue;
    }
    if (hTarget !== null && Math.abs(f.height - hTarget) > 0.02) lastChanging = i;
  }
  const settleMs = lastChanging >= 0 && lastChanging + 1 < after.length ? rel(after[lastChanging + 1].t) : after.length ? rel(last.t) : null;

  // 同帧更新：reveal 与 height 是否在同一帧一起变化（PLAN：高度与揭示同一帧更新）。
  let sameFrameChanges = 0;
  let revealOnly = 0;
  let heightOnly = 0;
  for (let i = 1; i < after.length; i += 1) {
    const a = after[i - 1];
    const b = after[i];
    if (a.reveal === null || b.reveal === null || a.height === null || b.height === null) continue;
    const dr = Math.abs(b.reveal - a.reveal);
    const dh = Math.abs(b.height - a.height);
    if (dr > 1e-5 && dh > 0.01) sameFrameChanges += 1;
    else if (dr > 1e-5) revealOnly += 1;
    else if (dh > 0.01) heightOnly += 1;
  }

  // 单调查验 + 过冲。
  let monotonic = true;
  let overshoot = false;
  for (let i = 1; i < after.length; i += 1) {
    const a = after[i - 1];
    const b = after[i];
    if (a.reveal === null || b.reveal === null) continue;
    if (phase === "expand" && b.reveal < a.reveal - 0.001) monotonic = false;
    if (phase === "collapse" && b.reveal > a.reveal + 0.001) monotonic = false;
    if (phase === "expand" && (b.reveal > 1.0005 || (heightTarget !== null && b.height > heightTarget + 0.5))) overshoot = true;
    if (phase === "collapse" && b.height < -0.5) overshoot = true;
  }

  // 左右对称：同一 dash 字符串应用到两条路径。
  let symmetric = true;
  for (let i = 0; i < after.length; i += 1) {
    if (after[i].dash !== after[i].rightDash) symmetric = false;
  }

  // 竖边接续：到角之后仍有可见长度增长（visible 继续增长到全长），且全长 > 横线长度。
  let verticalContinued = null;
  if (phase === "expand" && cornerIndex >= 0) {
    const cur = after[cornerIndex];
    const maxVisible = Math.max.apply(null, after.map((f) => (f.visible === null ? -1 : f.visible)));
    verticalContinued = maxVisible > cur.horizontal + 1 && (settledFrame ? settledFrame.length > settledFrame.horizontal : false);
  }

  // 首帧延迟与起点检查。
  const firstFrameMs = after.length ? rel(after[0].t) : null;
  const preReveal = pre ? pre.reveal : null;
  const preHeight = pre ? pre.height : null;

  return {
    phase: phase,
    originT: round(originT, 2),
    pre: pre
      ? {
          reveal: round(pre.reveal, 4),
          height: round(pre.height, 2),
          expanded: pre.expanded,
          outlineHidden: pre.outlineHidden,
          shellW: pre.shellW,
        }
      : null,
    frameStats: frameStats,
    firstFrameMs: firstFrameMs,
    corner: corner,
    settled: settledFrame
      ? {
          reveal: round(settledFrame.reveal, 4),
          height: round(settledFrame.height, 2),
          length: round(settledFrame.length, 2),
          horizontal: round(settledFrame.horizontal, 2),
          shellW: settledFrame.shellW,
          halfHandle: round(settledFrame.halfHandle, 2),
          end: round(settledFrame.end, 2),
          glassOpacity: settledFrame.glassOpacity,
          glassInset: settledFrame.glassInset,
          outlineHidden: settledFrame.outlineHidden,
          expanded: settledFrame.expanded,
        }
      : null,
    staticCornerFraction: staticCornerFraction,
    analyticCornerMsRate11: analyticCornerMs,
    height50Ms: height50,
    height90Ms: height90,
    settleMs: settleMs,
    updateCadence: { sameFrameChanges: sameFrameChanges, revealOnlyFrames: revealOnly, heightOnlyFrames: heightOnly },
    monotonic: monotonic,
    overshoot: overshoot,
    symmetricStrokes: symmetric,
    verticalContinued: verticalContinued,
    height: { start: round(preHeight, 2), target: round(heightTarget, 2), end: settledFrame ? round(settledFrame.height, 2) : null },
    durationFrames: after.length,
  };
}

async function measureOnce(page, phase, { settleMs = 1400 } = {}) {
  await startSampler(page);
  const clickInfo = await fastClick(page, "#toggle");
  await sleep(settleMs);
  const data = await stopSampler(page);
  const originT = data.clickT !== null ? data.clickT : data.pointerDownT;
  const analysis = analyze(data.frames, originT, phase);
  return {
    clickInfo: { cx: round(clickInfo.cx, 1), cy: round(clickInfo.cy, 1), w: round(clickInfo.width, 1), h: round(clickInfo.height, 1) },
    pointerDownT: data.pointerDownT,
    clickT: data.clickT,
    mouseUpT: data.mouseUpT,
    pressToClickMs: data.pointerDownT !== null && data.clickT !== null ? round(data.clickT - data.pointerDownT, 1) : null,
    frames: data.frames,
    metrics: analysis,
  };
}

async function screenshotRun(page, delayMs, filePath) {
  await page.reload();
  await waitPreviewReady(page);
  await page.eval("(" + installSampler.toString() + ")()");
  await startSampler(page);
  await fastClick(page, "#toggle");
  if (delayMs > 0) await sleep(delayMs);
  await page.screenshot(filePath);
  const elapsed = await page.eval("(function(){ var s = window.__previewProbe; var t = s.clickT; return t === null ? null : performance.now() - t; })()");
  await stopSampler(page).catch(() => null);
  return { file: filePath, requestedDelayMs: delayMs, elapsedAtCaptureMs: round(elapsed, 1) };
}

async function main() {
  const startedAt = new Date().toISOString();
  const previewBytes = readFileSync(PREVIEW_FILE);
  const previewHash = createHash("sha256").update(previewBytes).digest("hex");
  let browser = null;
  try {
    browser = await CdpBrowser.launch({ port: CDP_PORT });
    const page = await Page.create(browser);
    await page.setViewport(WIDTH, HEIGHT);
    const fileUrl = pathToFileURL(PREVIEW_FILE).href;
    console.log("预览：" + fileUrl);
    console.log("视口：" + WIDTH + "x" + HEIGHT + "；Edge：" + (browser.version && browser.version.Browser));
    await page.goto(fileUrl);
    await waitPreviewReady(page);
    const installed = await page.eval("(" + installSampler.toString() + ")()");
    record("预览页加载 + 采样器安装（可读到 revealMotion/heightMotion）", installed && installed.reveal !== null, {
      shellW: installed ? installed.shellW : null,
      horizontal: installed ? round(installed.horizontal, 2) : null,
      length: installed ? round(installed.length, 2) : null,
      reveal: installed ? installed.reveal : null,
      outlineHidden: installed ? installed.outlineHidden : null,
    });

    // ---- 1000x600 开合测量 ----
    const expand = await measureOnce(page, "expand", { settleMs: 1500 });
    record("1000x600 点击把手后展开采样到回调", expand.metrics.durationFrames > 20, {
      frames: expand.metrics.durationFrames,
      firstFrameMs: expand.metrics.firstFrameMs,
      cornerFrameMs: expand.metrics.corner && expand.metrics.corner.frameMs,
      cornerInterpMs: expand.metrics.corner && expand.metrics.corner.interpMs,
    });
    await page.screenshot(join(SHOTS, "preview-" + WIDTH + "-expanded.png"));

    const collapse = await measureOnce(page, "collapse", { settleMs: 1500 });
    await page.screenshot(join(SHOTS, "preview-" + WIDTH + "-collapsed.png"));

    // ---- 720x600 对照 ----
    let width720 = null;
    if (EXTRA_WIDTH > 0) {
      await page.setViewport(EXTRA_WIDTH, HEIGHT);
      await sleep(900);
      const expand720 = await measureOnce(page, "expand", { settleMs: 1400 });
      await page.screenshot(join(SHOTS, "preview-" + EXTRA_WIDTH + "-expanded.png"));
      const collapse720 = await measureOnce(page, "collapse", { settleMs: 1400 });
      await page.screenshot(join(SHOTS, "preview-" + EXTRA_WIDTH + "-collapsed.png"));
      width720 = { expand: expand720.metrics, collapse: collapse720.metrics };
      record("720x600 对照采样完成", expand720.metrics.durationFrames > 20, {
        cornerInterpMs: expand720.metrics.corner && expand720.metrics.corner.interpMs,
        staticCornerFraction: expand720.metrics.staticCornerFraction,
      });
    }

    // ---- 关键帧截图（每次重新加载，确保从收起态开始） ----
    await page.setViewport(WIDTH, HEIGHT);
    await sleep(500);
    const keyframes = [0, 60, 120, 180, 260, 400];
    const shots = [];
    for (const delay of keyframes) {
      const name = "preview-" + WIDTH + "-frame-" + String(delay).padStart(3, "0") + "ms.png";
      const shot = await screenshotRun(page, delay, join(SHOTS, name));
      shots.push(shot);
      console.log("截图 " + name + "（实际 " + shot.elapsedAtCaptureMs + "ms）");
    }

    // ---- 结论 ----
    const exp = expand.metrics;
    const col = collapse.metrics;
    record(
      "预览 1000x600：点击→横线首次到达转角在 ~0.22s 量级（PLAN §1.2 基准 216ms）",
      exp.corner !== null && exp.corner.interpMs !== null && exp.corner.interpMs >= 140 && exp.corner.interpMs <= 340,
      {
        clickToCornerFrameMs: exp.corner && exp.corner.frameMs,
        clickToCornerInterpMs: exp.corner && exp.corner.interpMs,
        staticCornerFraction: exp.staticCornerFraction,
        analyticMsRate11: exp.analyticCornerMsRate11,
      }
    );
    record("预览 1000x600：高度 50%/90% 与 settle 可测", exp.height50Ms !== null && exp.height90Ms !== null && exp.settleMs !== null, {
      height50Ms: exp.height50Ms,
      height90Ms: exp.height90Ms,
      settleMs: exp.settleMs,
      height: exp.height,
    });
    record("预览 1000x600：左右线框同 dash（对称）且到角后竖边继续", exp.symmetricStrokes === true && exp.verticalContinued === true, {
      symmetric: exp.symmetricStrokes,
      verticalContinued: exp.verticalContinued,
      lengthAtSettle: exp.settled && exp.settled.length,
      horizontalAtSettle: exp.settled && exp.settled.horizontal,
    });
    record("预览 1000x600：收起过程可测（reveal→0、高度→0、settle）", col.settled !== null, {
      settleMs: col.settleMs,
      height50Ms: col.height50Ms,
      height90Ms: col.height90Ms,
      end: col.height,
    });

    const conclusion = {
      clickToCorner: {
        frameQuantizedMs: exp.corner && exp.corner.frameMs,
        interpolatedMs: exp.corner && exp.corner.interpMs,
        planReferenceMs: 216,
        deltaMs:
          exp.corner && exp.corner.interpMs !== null ? round(exp.corner.interpMs - 216, 1) : null,
        staticCornerFraction: exp.staticCornerFraction,
        analyticMsAtRate11: exp.analyticCornerMsRate11,
        note:
          "corner 定义 = 首帧 visible>=horizontal（horizontal=(shellW/2-halfHandle)-(edge+corner)）。" +
          "实测按动画期间实时 pathLength 判定；静态 settled 比例给的是解析对照。",
      },
      heightCurve: {
        expand50Ms: exp.height50Ms,
        expand90Ms: exp.height90Ms,
        expandSettleMs: exp.settleMs,
        collapse50Ms: col.height50Ms,
        collapse90Ms: col.height90Ms,
        collapseSettleMs: col.settleMs,
      },
      geometry1000: exp.settled,
      geometry720: width720 && width720.expand.settled,
      counts: { expandFrames: exp.durationFrames, collapseFrames: col.durationFrames },
      screenshotKeyframes: shots,
      source: { file: fileUrl, sha256: previewHash, bytes: previewBytes.length },
      caveats: [
        "帧采样由页面 rAF 驱动，量到的是「首帧满足」，实际到角时刻在上一帧与首帧之间（给出线性插值）。",
        "预览的 .dock 宽度是 min(720px, 100%-28px)，1000 视口下操作区 720px；真实组件是 min(100%-24px,1180px)（1000 视口→976px），几何不同，到角时间应按 cornerFraction 折算对照。",
      ],
    };

    const output = {
      meta: {
        generatedAt: startedAt,
        finishedAt: new Date().toISOString(),
        engine: browser.version && browser.version.Browser,
        userAgent: browser.version && browser.version["User-Agent"],
        viewport: { width: WIDTH, height: HEIGHT },
        extraViewport: EXTRA_WIDTH > 0 ? { width: EXTRA_WIDTH, height: HEIGHT } : null,
        previewFile: PREVIEW_FILE,
        previewSha256: previewHash,
        browserLayer: "系统 Edge + CDP（scripts/verify-pm-qio032/cdp.mjs，无 Playwright）",
      },
      checks: results,
      expand1000: expand,
      collapse1000: collapse,
      width720: width720
        ? { expand: width720.expand, collapse: width720.collapse }
        : null,
      screenshots: shots,
      conclusion: conclusion,
    };
    const outFile = join(OUT_DIR, "preview-baseline.json");
    writeFileSync(outFile, JSON.stringify(output, null, 2));
    console.log("");
    console.log("结论：点击→到角（插值）= " + (conclusion.clickToCorner.interpolatedMs === null ? "?" : conclusion.clickToCorner.interpolatedMs + "ms") +
      "（帧量化 " + (conclusion.clickToCorner.frameQuantizedMs === null ? "?" : conclusion.clickToCorner.frameQuantizedMs + "ms") + "）" +
      "；展开高度 50/90% = " + exp.height50Ms + "/" + exp.height90Ms + "ms；settle=" + exp.settleMs + "ms" +
      "；收起 settle=" + col.settleMs + "ms");
    console.log("已写 " + outFile);
    const failed = results.filter((r) => r.status === "FAIL");
    await browser.close();
    browser = null;
    process.exit(failed.length ? 1 : 0);
  } catch (error) {
    console.error("预览基准测量失败：" + (error && error.stack ? error.stack : error));
    if (browser) await browser.close().catch(() => null);
    process.exit(1);
  }
}

main();