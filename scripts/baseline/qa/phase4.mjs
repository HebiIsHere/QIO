// 第四阶段（视觉统一 / 交互收口 / 动效语言）的真实界面验收。
//
// 用法（服务需在跑：后端 8734、前端 5199）：
//   python scripts/e2e_up.py
//   backend\.venv\Scripts\python.exe scripts\baseline\seed_phase2_topics.py --topics 40
//   node scripts/baseline/qa/phase4.mjs
// 输出：%TEMP%\qio-baseline\qa\report-phase4.json 与 shots\phase4-*.png
//
// 原则：真实运行、真实点击、拿客观证据（几何 / computed style / 帧序列），
// 不用「看代码推测」代替验证；没跑到的场景在报告里明确标成未运行。
import { createRequire } from "node:module";
import { existsSync, mkdirSync, writeFileSync } from "node:fs";

const require = createRequire(import.meta.url);

/**
 * Playwright 的位置在不同机器/不同 Codex 运行时版本之间会变（2026-09-20 那次运行时更新
 * 就让硬编码的 codex-runtimes 路径直接消失了）。所以按候选顺序找，找不到就给一条能照做的提示。
 *
 * 装一个不下载浏览器的轻量版本即可（用本机已有的 Chrome/Edge）：
 *   npm install --prefix %TEMP%\qio-pw playwright-core
 */
const PW_CANDIDATES = [
  process.env.QIO_PLAYWRIGHT,
  `${process.env.TEMP}\\qio-pw\\node_modules\\playwright-core`,
  `${process.env.TEMP}\\qio-pw\\node_modules\\playwright`,
  "C:\\Users\\zxy\\.cache\\codex-runtimes\\codex-primary-runtime\\dependencies\\node\\node_modules\\playwright",
  "playwright",
].filter(Boolean);

function loadPlaywright() {
  const tried = [];
  for (const c of PW_CANDIDATES) {
    try {
      return require(c);
    } catch (e) {
      tried.push(`${c}（${e && e.code ? e.code : "加载失败"}）`);
    }
  }
  throw new Error(
    `找不到 playwright / playwright-core。已尝试：\n  ${tried.join("\n  ")}\n` +
      `解决：npm install --prefix %TEMP%\\qio-pw playwright-core，或设置 QIO_PLAYWRIGHT=<目录>`,
  );
}

const { chromium } = loadPlaywright();

/** 本机浏览器：优先 Chrome，退回 Edge（都用系统已装的，不下载浏览器） */
const CHROME_CANDIDATES = [
  process.env.QIO_CHROME,
  "C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe",
  "C:\\Program Files (x86)\\Google\\Chrome\\Application\\chrome.exe",
].filter(Boolean);
const chromePath = CHROME_CANDIDATES.find((p) => existsSync(p)) ?? null;

const BASE = "http://127.0.0.1:5199";
const API = "http://127.0.0.1:8734";
const OUT = `${process.env.TEMP}\\qio-baseline\\qa`;
const SHOTS = `${OUT}\\shots`;
mkdirSync(SHOTS, { recursive: true });

const report = { startedAt: new Date().toISOString(), cases: [], notes: [], notRun: [] };
const rec = (id, title, passed, actual, detail = null) => {
  report.cases.push({ id, title, passed: passed === null ? null : !!passed, actual: String(actual), detail });
  const tag = passed === null ? "SKIP" : passed ? "PASS" : "FAIL";
  console.log(`[${tag}] ${id} ${title} :: ${actual}`);
};
const note = (t) => {
  report.notes.push(t);
  console.log(`[NOTE] ${t}`);
};

let seq = Math.floor(Date.now() / 1000) % 100000;
const url = (hash = "#/") => `${BASE}/?fresh=${++seq}${hash}`;
const wait = (ms) => new Promise((r) => setTimeout(r, ms));
const shot = async (page, name) => page.screenshot({ path: `${SHOTS}\\phase4-${name}.png` });

const browser = await chromium.launch({
  ...(chromePath ? { executablePath: chromePath } : { channel: "msedge" }),
  headless: true,
  args: ["--use-gl=angle", "--use-angle=swiftshader", "--enable-unsafe-swiftshader"],
});
const context = await browser.newContext({ viewport: { width: 1440, height: 900 } });
await context.addInitScript(() => {
  try {
    if (!localStorage.getItem("qio-theme")) localStorage.setItem("qio-theme", "dark");
  } catch {
    /* localStorage 不可用时忽略 */
  }
});
const page = await context.newPage();
const consoleErrors = [];
const dialogs = [];
page.on("console", (m) => {
  if (m.type() === "error") consoleErrors.push(m.text().slice(0, 200));
});
page.on("pageerror", (e) => consoleErrors.push("pageerror: " + String(e.message).slice(0, 200)));
// 原生确认框是第四阶段明确要消灭的东西：一旦出现就记下来
page.on("dialog", async (d) => {
  dialogs.push(`${d.type()}:${d.message().slice(0, 80)}`);
  await d.dismiss().catch(() => {});
});

const css = (sel, prop) =>
  page.evaluate(
    ([s, p]) => {
      const el = document.querySelector(s);
      if (!el) return null;
      return getComputedStyle(el).getPropertyValue(p).trim();
    },
    [sel, prop],
  );

const openPlanet = async () => {
  // 用 DOM 点击：Playwright 的 actionability 会等元素「稳定」，而入口小球一直在呼吸/自转，
  // 等待期间空闲挂载就把小球预热了 —— 那样点开走的是「已预热」路径，根本不会出现加载条。
  await page.$eval(".dock", (el) => el.click());
  await page.waitForSelector(".planet-view", { state: "visible", timeout: 8000 });
};

/**
 * 量一块区域里「最亮 - 最暗」的差（像素对比度）。
 *
 * 为什么需要它：对话页在星球转场里是否真的还看得见，**没法用 DOM 读出来** ——
 * 它没有被改透明度，只是被上层盖住；而「上层铺底的 opacity」这种代理指标会骗人
 * （实测就骗过一次：铺底 opacity 一路正常，但父层自带不透明底色，对话页其实早就没了）。
 * 所以直接看像素：有文字的区域对比度高，被纯色盖住的区域对比度接近 0。
 *
 * 截png → 交回页面用 2D canvas 解码 → 统计亮度极差。
 */
async function regionContrast(box) {
  const png = await page.screenshot({ clip: box });
  return await page.evaluate(async (b64) => {
    const img = new Image();
    img.src = "data:image/png;base64," + b64;
    await img.decode();
    const c = document.createElement("canvas");
    c.width = img.width;
    c.height = img.height;
    const ctx = c.getContext("2d");
    ctx.drawImage(img, 0, 0);
    const d = ctx.getImageData(0, 0, c.width, c.height).data;
    let min = 255;
    let max = 0;
    for (let i = 0; i < d.length; i += 4) {
      const lum = 0.2126 * d[i] + 0.7152 * d[i + 1] + 0.0722 * d[i + 2];
      if (lum < min) min = lum;
      if (lum > max) max = lum;
    }
    return Math.round(max - min);
  }, png.toString("base64"));
}

/** 两张截图的平均像素差（0–255）。用来量化「交接前后长得像不像」 */
async function regionMeanDiff(bufA, bufB) {
  return await page.evaluate(
    async ([a, b]) => {
      const load = async (b64) => {
        const img = new Image();
        img.src = "data:image/png;base64," + b64;
        await img.decode();
        const c = document.createElement("canvas");
        c.width = img.width;
        c.height = img.height;
        const ctx = c.getContext("2d");
        ctx.drawImage(img, 0, 0);
        return ctx.getImageData(0, 0, c.width, c.height);
      };
      const ia = await load(a);
      const ib = await load(b);
      if (ia.width !== ib.width || ia.height !== ib.height) return null;
      let sum = 0;
      let n = 0;
      for (let i = 0; i < ia.data.length; i += 4) {
        sum += Math.abs(ia.data[i] - ib.data[i]) + Math.abs(ia.data[i + 1] - ib.data[i + 1]) + Math.abs(ia.data[i + 2] - ib.data[i + 2]);
        n += 3;
      }
      return Math.round((sum / n) * 100) / 100;
    },
    [bufA.toString("base64"), bufB.toString("base64")],
  );
}

/** 对话页里一条消息文字所在的区域（左侧、避开右侧的星球与浮动组件） */
const CHAT_TEXT_BOX = { x: 120, y: 55, width: 420, height: 70 };

/** 采样一段过渡：按固定间隔记录元素几何/透明度，用来看「是否连续变化」而不是瞬切 */
async function sampleTransition(sel, ms, stepMs = 75) {
  const frames = [];
  const started = Date.now();
  while (Date.now() - started < ms) {
    const box = await page
      .evaluate((s) => {
        const el = document.querySelector(s);
        if (!el) return null;
        const r = el.getBoundingClientRect();
        const st = getComputedStyle(el);
        // 画布自身状态：转场期间如果靠 CSS 遮罩裁出球体圆盘，这里会看到 mask 不是 none
        const canvas = el.querySelector(".planet-canvas") || document.querySelector(".planet-canvas");
        const cs = canvas ? getComputedStyle(canvas) : null;
        return {
          w: Math.round(r.width),
          h: Math.round(r.height),
          x: Math.round(r.left),
          y: Math.round(r.top),
          o: Number(Number(st.opacity).toFixed(2)),
          t: st.transform,
          k: el.getAttribute("data-stage-k"),
          mask: cs ? (cs.maskImage || cs.webkitMaskImage || "none") : "none",
          // 「放大」与「转向当前话题」是不是同一段动作：看聚焦对象在长大过程中是否已经确定
          selected: window.__qioPlanetStage?.()?.selected ?? null,
          rotationY: window.__qioPlanetStage?.()?.debug?.rotationY ?? null,
          // 球体在画布内的半径：配合 scale 才能算出**屏幕上**的直径。
          // 为什么不直接看 scale：球态现在是「画布本身就是球那么大」（scale≈1），
          // 内部缩放系数已经不是「球有多小」的度量了。
          radius: window.__qioPlanetStage?.()?.sphere?.radius ?? null,
          // 铺底（::before）的不透明度 = 对话页被盖掉的程度。
          // 它应该「随星球长大一起渐隐」，而不是在长大之前就完全不透明。
          curtain: (() => {
            const view = document.querySelector(".planet-view");
            return view ? Number(getComputedStyle(view, "::before").opacity) : null;
          })(),
        };
      }, sel)
      .catch(() => null);
    if (box) frames.push({ ms: Date.now() - started, ...box });
    await wait(stepMs);
  }
  return frames;
}

async function scenario1Chat() {
  await page.goto(url("#/"));
  await page.waitForSelector("#composer-input", { timeout: 10000 });
  await wait(400);
  await shot(page, "s1-chat");
  // 聊天页不一定有 .qio-btn（按钮只在有内容时出现），因此按「第一个真实存在的高频控件」取
  const durBtn = await page.evaluate(() => {
    const el = document.querySelector(".qio-btn, .chip, .qio-input, .composer");
    if (!el) return null;
    return { sel: el.className, dur: getComputedStyle(el).transitionDuration };
  });
  rec(
    "S1.1",
    "高频控件时长落在高频档（≤200ms）",
    durBtn !== null && Number(String(durBtn.dur).split(",")[0].replace("s", "")) <= 0.2,
    JSON.stringify(durBtn),
  );
  const durComposer = await css(".composer", "transition-duration");
  rec("S1.2", "输入区过渡走令牌（不是裸数值）", durComposer !== null, `composer transition-duration=${durComposer}`);
  rec("S1.3", "普通对话没有任何原生确认框", dialogs.length === 0, `dialogs=${JSON.stringify(dialogs)}`);
}

async function scenario2PlanetOpenClose() {
  // 基线：还没打开星球时，对话页在这块区域里是有文字的（对比度高）
  const chatContrastBefore = await regionContrast(CHAT_TEXT_BOX);
  const dockOk = await page.evaluate(() => {
    const dock = document.querySelector("[data-planet-entry]");
    if (!dock) return { ok: false, why: "缺少 [data-planet-entry] 入口" };
    /**
     * 入口小球现在由**真实星球场景**承担：星球层缩到入口尺度常驻（`.planet-view.ball`，
     * 同一个 WebGL canvas），对话页那枚按钮只负责点击/拖动。
     * 兜底：WebGL 不可用（或 three.js 还没加载完）时，入口仍是 2D 压缩态 `[data-compressed-planet]`。
     */
    const ballLayer = document.querySelector(".planet-view.ball");
    const realCanvas = ballLayer?.querySelector("canvas.planet-canvas") ?? null;
    const orb = dock.querySelector("[data-compressed-planet]");
    const ballRect = ballLayer ? ballLayer.querySelector(".planet-stage")?.getBoundingClientRect() : null;
    const dockRect = dock.getBoundingClientRect();
    // 真实渲染在位的判据：球态层可见、且球心落在入口里面（不是别的地方）
    const centered =
      ballRect && ballRect.width > 0
        ? ballRect.left < dockRect.left + dockRect.width && ballRect.right > dockRect.left
        : false;
    return {
      ok: Boolean(realCanvas) || Boolean(orb),
      real: Boolean(realCanvas),
      orbFallback: Boolean(orb),
      centered,
    };
  });
  rec(
    "S2.1",
    "入口小球就是这颗星球本身（真实场景渲染；WebGL 不可用时退化为 2D 压缩态）",
    dockOk.ok === true,
    JSON.stringify(dockOk),
  );

  /**
   * S2.16：球态**按自己的分辨率渲染**（画布就是球那么大、scale≈1），不做 CSS 降采样。
   *
   * 用户实测反馈「缩小后的星球轮廓全是像素」：全屏渲染缩到 ~10% 时，1 设备像素宽的轮廓
   * 只剩 0.1px，采样大部分落空。修法是把球态的 canvas 设成球尺寸（108px，见组件注释），
   * 于是球态既没有降采样也没有放大。
   */
  const ballRender = await page.evaluate(() => {
    const c = document.querySelector(".planet-canvas");
    const st = document.querySelector(".planet-stage");
    const h = window.__qioPlanetStage?.();
    const m = st ? String(getComputedStyle(st).transform).match(/matrix\(([^)]+)\)/) : null;
    return {
      canvasCss: c ? [c.clientWidth, c.clientHeight] : null,
      canvasBacking: c ? [c.width, c.height] : null,
      scale: m ? Math.round(Number(m[1].split(",")[0]) * 1000) / 1000 : null,
      sphereRadius: h?.sphere?.radius ? Math.round(h.sphere.radius) : null,
    };
  });
  rec(
    "S2.16",
    "入口小球按自身分辨率渲染（画布=球尺寸、scale≈1，无 CSS 降采样）",
    ballRender.canvasCss !== null &&
      ballRender.canvasCss[0] <= 160 &&
      ballRender.scale !== null &&
      Math.abs(ballRender.scale - 1) < 0.08,
    `画布 CSS=${ballRender.canvasCss}（绘图缓冲 ${ballRender.canvasBacking}），scale=${ballRender.scale}，球体半径=${ballRender.sphereRadius}px`,
  );
  /**
   * S2.17：球态的环宽要**按比例**缩小，不能靠「加粗」来换可见度。
   *
   * 判据是「环宽 / 球体屏幕直径」这个相对值：全屏时约 0.67%（设计值 5px / 750px），
   * 球态应当接近同一个比例（保底 1px 让它略高一点，但不该差出量级）。
   * 曾经把小球的目标宽度定成固定 1.7px（相对 1.8%），小球看起来是一圈圈加粗的同心环。
   */
  const relativeRing = (s) => {
    if (!s.ringWidthScale || !s.sphereRadius) return null;
    return (s.ringWidthScale * 5) / (2 * s.sphereRadius);
  };
  const ballRel = await page.evaluate(() => {
    const h = window.__qioPlanetStage?.();
    return { ringWidthScale: h?.debug?.ringWidthScale ?? null, sphereRadius: h?.sphere?.radius ? Math.round(h.sphere.radius) : null };
  });
  await openPlanetAndWaitReady();
  await wait(500);
  const fullRel = await page.evaluate(() => {
    const h = window.__qioPlanetStage?.();
    return { ringWidthScale: h?.debug?.ringWidthScale ?? null, sphereRadius: h?.sphere?.radius ? Math.round(h.sphere.radius) : null };
  });
  const relBall = relativeRing(ballRel);
  const relFull = relativeRing(fullRel);
  const relRatio = relBall && relFull ? relBall / relFull : null;
  rec(
    "S2.17",
    "球态的环宽按比例缩小（不是靠加粗换可见度）",
    relRatio !== null && relRatio > 0.4 && relRatio < 2.5,
    `球态相对宽度=${relBall === null ? "无" : (relBall * 100).toFixed(2) + "%"}（补偿倍数 ${ballRel.ringWidthScale}、球半径 ${ballRel.sphereRadius}px）；全屏=${relFull === null ? "无" : (relFull * 100).toFixed(2) + "%"}；比值=${relRatio === null ? "无" : relRatio.toFixed(2)}`,
  );

  /**
   * S2.18：收缩动画的**最后一帧**要与静止的球态长得一样。
   *
   * 用户实测反馈：「星球收缩时动画最后一帧和在缩小状态时的样子完全不同」。
   * 量法：在球的位置固定取一小块，分别截「收缩过程中最后一张」与「静止球态」，比平均像素差。
   *
   * 这里同时修掉了这条断言以前的一个测量错误：它用 CDP `Animation.setPlaybackRate` 把
   * CSS 过渡放慢 4 倍，但脚本（`sleep` / `tokenMs`）的时间线没有跟着慢 —— 于是「点击到
   * 交接」的 420ms 真的跑完了，而过渡只走了不到 5%，截到的「最后一帧」其实是刚起步的
   * 全屏构图（对照区域基本是空的），两个数字都不可信。
   * 现在改用产品自带的 `?planetdemo=slow`：它同时放大 CSS 令牌与脚本时长，两条时间线一致。
   */
  const dockRect = await page.evaluate(() => {
    const d = document.querySelector("[data-planet-entry]").getBoundingClientRect();
    return { x: Math.round(d.x), y: Math.round(d.y), w: Math.round(d.width), h: Math.round(d.height) };
  });
  const ballClip = {
    x: Math.max(0, dockRect.x - 18),
    y: Math.max(0, dockRect.y - 18),
    width: 150,
    height: 150,
  };
  await page.goto(url("#/").replace("?fresh=", "?planetdemo=slow&fresh="));
  await page.waitForSelector("#composer-input", { timeout: 10000 });
  await page
    .waitForFunction(() => (window.__qioPlanetStage?.() ?? {}).ball === true, { timeout: 12000 })
    .catch(() => note("等入口小球（慢放演示）超时"));
  await wait(900);
  /**
   * 两次收起，各服务一条断言：
   * ① 只做**页面内逐帧记录** → S2.19（尾段是否在球分辨率下渲染、换布局是否错位）；
   * ② 再收一次，这次**抓像素** → S2.18（「最后一帧 vs 静止球态」）。
   * 必须分开跑：截图在软件渲染下一次要 100–300ms，会和 rAF 抢帧 ——
   * 实测揉在一起时尾段只被采到 1 帧（S2.19 判不了）。
   */
  await openPlanetAndWaitReady();
  await wait(600);
  await installPlanetTrace(2600);
  await page.$eval(".close-btn", (el) => el.click()).catch(() => {});
  await page
    .waitForFunction(() => !!document.querySelector(".planet-view.ball"), { timeout: 8000 })
    .catch(() => note("等回球态超时"));
  await wait(400);

  /**
   * S2.19：收缩尾段真的切到了**球自己的分辨率**，而且切的那一帧没错位。
   *
   * 为什么需要它：S2.18 只比「最后一帧 vs 球态」，而"最后一帧"是不是在大画布上渲染
   * （= 1 设备像素的轮廓被降采样成断续的点）它看不出来。这条按**逐帧状态**判：
   * - 尾段该出现（用户要求：最后一段在球分辨率下渲染），且画布就是球尺寸 108；
   * - 屏幕上球直径在尾段不超过入口球的 1.35 倍（超过就说明切早了，球会被放大得发虚）；
   * - 半径单调收（没有回弹），且球心必须落在**应有轨迹**上（见下面的说明）。
   */
  const trace = await readPlanetTrace();
  const entryR = Math.max(4, Math.min(dockRect.w, dockRect.h) / 2);
  const tailFrames = trace.filter((f) => f.tail);
  const screen = (f) => {
    if (!f || !f.radius || !f.k || f.tx === null || f.ty === null) return null;
    const left = f.left ?? 0;
    const top = f.top ?? 0;
    return {
      r: f.radius * f.k,
      cx: left + f.tx + f.k * ((f.sphereCx ?? 0) - left),
      cy: top + f.ty + f.k * ((f.sphereCy ?? 0) - top),
    };
  };
  const tailScreen = tailFrames.map(screen).filter(Boolean);
  const maxTailR = tailScreen.length ? Math.max(...tailScreen.map((s) => s.r)) : null;
  let maxUpR = 0;
  /**
   * 「换布局那一下没错位」用**轨迹一致性**判，不用「单帧位移」判：
   * 本环境是软件渲染，实测帧间隔 p50=19ms、p90=90ms、最大 281ms ——
   * 一帧里球本来就可能走十几像素（那是掉帧，不是错位）。
   * 所以把每帧的屏幕半径换算成曲线进度 p，再算它**应该在**的球心，
   * 看实际球心偏离多少：错位/闪一帧是几十像素的偏离，正常帧只有零点几像素。
   */
  const firstScreen = trace.map(screen).find(Boolean) ?? null;
  const endCx = dockRect.x + dockRect.w / 2;
  const endCy = dockRect.y + dockRect.h / 2;
  let maxPathDev = 0;
  for (let i = 1; i < tailScreen.length; i++) {
    maxUpR = Math.max(maxUpR, tailScreen[i].r - tailScreen[i - 1].r);
  }
  for (const s of tailScreen) {
    if (!firstScreen || firstScreen.r - entryR < 1) continue;
    const p = Math.max(0, Math.min(1, (firstScreen.r - s.r) / (firstScreen.r - entryR)));
    const expectCx = firstScreen.cx + (endCx - firstScreen.cx) * p;
    const expectCy = firstScreen.cy + (endCy - firstScreen.cy) * p;
    maxPathDev = Math.max(maxPathDev, Math.hypot(s.cx - expectCx, s.cy - expectCy));
  }
  const tailCanvas = tailFrames.map((f) => f.canvas);
  rec(
    "S2.19",
    "收缩尾段在球分辨率下渲染，且换布局那一下不错位",
    // 帧数只要求 ≥1：软件渲染下单次收起可能只采到 1 帧尾段（实测），
    // 而「是否真的切到球分辨率 + 有没有错位」用画布尺寸、半径、轨迹三条判据就够。
    tailFrames.length >= 1 &&
      tailCanvas.every((c) => c !== null && c <= 160) &&
      maxTailR !== null &&
      maxTailR <= entryR * 1.45 &&
      maxUpR <= 1.5 &&
      maxPathDev <= 4,
    `尾段帧数=${tailFrames.length}（画布 ${[...new Set(tailCanvas)].join("/")}）；` +
      `尾段最大半径=${maxTailR === null ? "无" : maxTailR.toFixed(1)}px（入口 ${entryR}px）；` +
      `单帧最大半径回升=${maxUpR.toFixed(2)}px；球心偏离应有轨迹=${maxPathDev.toFixed(2)}px`,
  );

  /**
   * S2.18：收缩动画的**最后一帧**要与静止球态长得一样。
   *
   * 单独再收一次（S2.19 那次不截图，见上面的说明），在球的位置固定取一小块，
   * 比「收缩过程中最后一张（此时画布已经是球尺寸）」与「交接后的静止球态」。
   * 差异应该很小：同一对象、同一密度、同一渲染分辨率。
   */
  await openPlanetAndWaitReady();
  await wait(600);
  await page.$eval(".close-btn", (el) => el.click()).catch(() => {});
  let lastTailPng = null;
  for (let i = 0; i < 40; i++) {
    const s = await page.evaluate(() => {
      const v = document.querySelector(".planet-view");
      const c = document.querySelector(".planet-canvas");
      return { ball: !!v?.classList.contains("ball"), css: c ? c.clientWidth : null };
    });
    if (s.ball) break;
    if (s.css !== null && s.css <= 200) lastTailPng = await page.screenshot({ clip: ballClip });
    await wait(45);
  }
  await page
    .waitForFunction(() => !!document.querySelector(".planet-view.ball"), { timeout: 8000 })
    .catch(() => note("S2.18：等回球态超时"));
  await wait(250);
  const settledPng = await page.screenshot({ clip: ballClip });
  const handoffDiff = lastTailPng ? await regionMeanDiff(lastTailPng, settledPng) : null;
  rec(
    "S2.18",
    "收缩动画的最后一帧与静止球态差异很小（同一对象、同一渲染分辨率）",
    handoffDiff !== null && handoffDiff < 12,
    `收缩末帧 vs 静止球态 平均像素差=${handoffDiff === null ? "无（没抓到收缩末帧）" : handoffDiff}/255`,
  );

  /**
   * S2.20：收缩过程中**不能有单帧闪现**。
   *
   * 用户实测：「星球在缩小的途中动画出现了闪现」。逐帧抓合成画面（CDP screencast）后
   * 定位到：尾段切换布局的那一帧，环宽补偿还是旧约定下的值（整层缩放 0.13 时的 ≈2.0），
   * 而画布已经变成球自己的 108px（缓冲像素 = 屏幕像素）—— 于是那一帧的环被画成 12px 宽，
   * 看起来是一块实心亮斑；下一帧读到新变换才恢复正常。
   *
   * 判据只看**向上跳变**：球变小是单调衰减（向下跳是正常的），异常帧是「比邻居亮得多」。
   */
  /**
   * 把铺底钉成全不透明再抓帧：球体与对话页的 accent 控件是**同一个色相**，
   * 不钉住的话「粉色像素」会被代码块语法高亮之类淹没（实测整幅量到恒定值 / 量到 24px 的假环厚）。
   * 铺底只影响背景，不影响球体怎么渲染，所以这是更干净的量法。
   */
  await page.evaluate(() => {
    const s = document.createElement("style");
    s.textContent = `.planet-view::before { opacity: 1 !important; transition: none !important; }`;
    document.head.appendChild(s);
  });
  const flashCdp = await context.newCDPSession(page);
  const flashFrames = [];
  flashCdp.on("Page.screencastFrame", async (ev) => {
    flashFrames.push(ev.data);
    await flashCdp.send("Page.screencastFrameAck", { sessionId: ev.sessionId }).catch(() => {});
  });
  await openPlanetAndWaitReady();
  await wait(500);
  await flashCdp.send("Page.enable");
  await flashCdp.send("Page.startScreencast", { format: "png", everyNthFrame: 1 });
  await page.$eval(".close-btn", (el) => el.click()).catch(() => {});
  await page
    .waitForFunction(() => !!document.querySelector(".planet-view.ball"), { timeout: 8000 })
    .catch(() => note("S2.20：等回球态超时"));
  await wait(300);
  await flashCdp.send("Page.stopScreencast").catch(() => {});
  // 逐帧量落点窗口内的粉色墨迹（球体的环与轮廓）
  const inks = [];
  for (let i = 0; i < flashFrames.length; i += 8) {
    const res = await page.evaluate(
      async ([list, box]) => {
        const out = [];
        for (const b64 of list) {
          const img = new Image();
          img.src = "data:image/png;base64," + b64;
          await img.decode();
          const c = document.createElement("canvas");
          c.width = img.width;
          c.height = img.height;
          const ctx = c.getContext("2d");
          ctx.drawImage(img, 0, 0);
          const d = ctx.getImageData(0, 0, c.width, c.height).data;
          const sx = c.width / 1440;
          const sy = c.height / 900;
          const x0 = Math.max(0, Math.round(box.x * sx));
          const y0 = Math.max(0, Math.round(box.y * sy));
          const x1 = Math.min(c.width, Math.round((box.x + box.w) * sx));
          const y1 = Math.min(c.height, Math.round((box.y + box.h) * sy));
          let n = 0;
          for (let y = y0; y < y1; y++) {
            for (let x = x0; x < x1; x++) {
              const j = (y * c.width + x) * 4;
              if (d[j] > 120 && d[j] - d[j + 1] > 55 && d[j] - d[j + 2] > 20) n++;
            }
          }
          out.push(n);
        }
        return out;
      },
      [
        flashFrames.slice(i, i + 8),
        { x: dockRect.x - 110, y: dockRect.y - 110, w: dockRect.w + 220, h: dockRect.h + 220 },
      ],
    );
    inks.push(...res);
  }
  const med = (arr) => {
    const v = arr.slice().sort((a, b) => a - b);
    return v.length ? v[Math.floor(v.length / 2)] : 0;
  };
  let worst = null;
  for (let i = 2; i < inks.length - 2; i++) {
    const neighbors = med([inks[i - 2], inks[i - 1], inks[i + 1], inks[i + 2]]);
    if (neighbors < 60) continue; // 邻居几乎没有球（还没进落点窗口 / 已交接）时不判
    const ratio = inks[i] / neighbors;
    if (!worst || ratio > worst.ratio) worst = { i, ratio, ink: inks[i], neighbors };
  }
  rec(
    "S2.20",
    "收缩过程中没有单帧闪现（没有一帧把环画成粗条/实心斑）",
    flashFrames.length >= 8 && worst !== null && worst.ratio < 3,
    `采样 ${flashFrames.length} 帧；最亮的单帧墨迹=${worst ? `${worst.ink}（邻居中位 ${worst.neighbors}，×${worst.ratio.toFixed(2)}）` : "无"}` +
      `（阈值 3× 是粗判；逐帧画面与人眼复核见 scripts/baseline/qa/planet-flash-probe.mjs）`,
  );
  // 慢放演示只是为这一条而开的：后面的时长/连贯性断言必须回到正常速度
  await page.goto(url("#/"));
  await page.waitForSelector("#composer-input", { timeout: 10000 });
  await page
    .waitForFunction(() => (window.__qioPlanetStage?.() ?? {}).ball === true, { timeout: 12000 })
    .catch(() => note("等入口小球（恢复常速）超时"));
  await ensureBallMode();

  /**
   * 「连续长大」这一段用产品自带的 `?planetdemo=slow`（时长 ×3）来采样。
   *
   * 为什么：判据是**形状**（起点是球那么大、终点接近全屏、中间至少有一帧在两者之间），
   * 而本机是软件渲染，跨进程采样经常只有 ~11fps —— 520ms 的长大整段落在两帧之间，
   * 实测出现过「中间态 0 帧」的假失败。放大时长只是让同样的形状被采样到，不改形状本身。
   */
  await page.goto(url("#/").replace("?fresh=", "?planetdemo=slow&fresh="));
  await page.waitForSelector("#composer-input", { timeout: 10000 });
  await page
    .waitForFunction(() => (window.__qioPlanetStage?.() ?? {}).ball === true, { timeout: 12000 })
    .catch(() => note("等入口小球（S2.2 慢放）超时"));
  await wait(700);
  const entryRect = await page.evaluate(() => {
    const el = document.querySelector("[data-planet-entry]");
    if (!el) return null;
    const r = el.getBoundingClientRect();
    return { x: Math.round(r.x), y: Math.round(r.y), w: Math.round(r.width), h: Math.round(r.height) };
  });

  const t0 = Date.now();
  // 点入口后**立刻**开始采样：阶段 A 只有 180ms，慢一步就看不到起点尺度。
  // 长大的是「星球层」本身（同一个对象在变尺度），所以采样它的 transform。
  // 用**页面内**逐帧记录（见 installPlanetTrace）：外面轮询会漏掉最开始的帧。
  await installPlanetTrace(4000);
  await page.$eval(".dock", (el) => el.click());
  // 窗口要能覆盖「懒加载 chunk + WebGL 冷启动 + 520ms 长大」——软件渲染下冷启动
  // 能占掉 1 秒以上，窗口太短会在长大没跑完时就停止采样（实测出现过 max scale 只有 0.32）。
  await wait(3200);
  const samples = await readPlanetTrace();
  await page.waitForSelector(".planet-view", { state: "visible", timeout: 8000 });
  const total = Date.now() - t0;
  await wait(300);
  await shot(page, "s2-planet-open");
  const scaleOf = (t) => {
    const m = String(t).match(/matrix\(([^)]+)\)/);
    return m ? Number(m[1].split(",")[0]) : null;
  };
  /**
   * 用**屏幕上的直径**当判据（= 2 × 画布内半径 × 当前缩放）。
   *
   * 不能再用「整层 scale」：球态现在是「画布本身就是球那么大」（scale≈1，
   * 相机聚焦时甚至 0.87），内部缩放系数已经不代表球有多小。
   */
  const diameters = samples
    .map((s) => (s.radius && s.scale ? 2 * s.radius * s.scale : null))
    .filter((v) => v !== null);
  const minD = diameters.length ? Math.min(...diameters) : null;
  const maxD = diameters.length ? Math.max(...diameters) : null;
  const grewFromSmall = minD !== null && maxD !== null && minD < 200 && maxD > 600;
  // 软件渲染下帧率不稳定（同一脚本有时 5 帧有时 10 帧），所以判定「形状」而不是帧数：
  // 起点必须是球那么大、终点接近全屏，并且中间至少有一帧落在两者之间（不是一帧跳完）。
  const midFrames = diameters.filter((v) => v > 200 && v < 600).length;
  rec(
    "S2.2",
    "进入是同一个对象连续长大（从小球尺度到全屏构图，不是单帧瞬切）",
    samples.length >= 3 && grewFromSmall && midFrames >= 1,
    `采样 ${samples.length} 帧（其中中间态 ${midFrames} 帧），屏幕直径 ${minD === null ? "无" : Math.round(minD)}px → ${maxD === null ? "无" : Math.round(maxD)}px，总时长≈${total}ms，入口矩形=${JSON.stringify(entryRect)}`,
    samples.filter((s) => s.scale !== null).slice(0, 12),
  );
  const maskedFrames = samples.filter((s) => s.mask && s.mask !== "none").length;
  const canvasInfo = await page.evaluate(() => {
    const c = document.querySelector(".planet-canvas");
    if (!c) return null;
    const gl = c.getContext("webgl2") || c.getContext("webgl");
    return { alpha: gl ? gl.getContextAttributes().alpha : null };
  });
  rec(
    "S2.6",
    "球体靠场景自身透明清屏成立（转场期间不用 CSS 遮罩裁画布）",
    maskedFrames === 0 && canvasInfo !== null && canvasInfo.alpha === true,
    `遮罩帧 ${maskedFrames}/${samples.length}；WebGL 上下文 alpha=${canvasInfo ? canvasInfo.alpha : "无上下文"}`,
  );
  /**
   * 铺底必须与「长大」同步：对话页是在星球变大的过程里逐渐被盖掉的，
   * 而不是在长大之前就黑掉（那样用户看到的是「先黑屏，再突然出现一个星球」）。
   */
  /**
   * S2.14：放大与「转向当前话题」必须是**同一段动作**。
   *
   * 用户实测反馈过「进入时有两段动画：一段放大进入，一段回正」—— 那是把聚焦排在展开之后造成的。
   * 判据：在星球还小的帧里，聚焦对象就应该已经确定、并且自转已经在动。
   */
  const earlyFrames = samples.filter((s) => {
    // 「还在长大」= 屏幕直径还没到全屏（约 800px），同样不依赖内部 scale
    const d = s.scale && s.radius ? 2 * s.radius * s.scale : null;
    return d !== null && d < 700;
  });
  const focusStartedEarly = earlyFrames.some((s) => Boolean(s.selected));
  const rotations = earlyFrames.map((s) => s.rotationY).filter((v) => typeof v === "number");
  const rotatedDuringGrowth =
    rotations.length >= 2 && Math.max(...rotations) - Math.min(...rotations) > 0.001;
  rec(
    "S2.14",
    "放大与转向当前话题是同一段动作（长大过程中就已经在转，不是长大完再回正）",
    focusStartedEarly && rotatedDuringGrowth,
    `长大中的帧 ${earlyFrames.length} 帧；其中已确定聚焦对象=${focusStartedEarly}；其间自转变化=${rotations.length >= 2 ? (Math.max(...rotations) - Math.min(...rotations)).toFixed(3) : "样本不足"}`,
  );
  const midGrowth = samples.filter((s) => {
    // 「还在长大、且铺底还没盖满」的帧。判定带放宽是有意的：
    // 软件渲染下采样很稀（一次展开只采到 1–3 帧落在带内），窄带会把正常行为判成失败。
    const d = s.scale && s.radius ? 2 * s.radius * s.scale : null;
    return d !== null && d < 780;
  });
  const worstCurtain = midGrowth.length
    ? Math.max(...midGrowth.map((s) => (typeof s.curtain === "number" ? s.curtain : 0)))
    : null;
  rec(
    "S2.8",
    "对话页随星球长大一起渐隐（不是先黑屏再出现星球）",
    midGrowth.length > 0 && worstCurtain !== null && worstCurtain < 0.95,
    `长大中途帧 ${midGrowth.length} 帧，其间铺底最大不透明度=${worstCurtain}（代理指标，见 S2.9）`,
    midGrowth.slice(0, 6),
  );
  const glass = await css(".qio-glass", "backdrop-filter");
  rec("S2.3", "Planet 浮层使用晶体玻璃（backdrop-filter）", glass !== null && glass !== "none", `backdrop-filter=${glass}`);

  await page.$eval(".close-btn", (el) => el.click()).catch(() => note("未找到 .close-btn"));
  const closing = await sampleTransition(".planet-stage", 2400, 50);
  const closeScales = closing
    .map((s) => {
      const m = String(s.t).match(/matrix\(([^)]+)\)/);
      return m ? Number(m[1].split(",")[0]) : null;
    })
    .filter((v) => v !== null);
  const closeMin = closeScales.length ? Math.min(...closeScales) : null;
  const closeMax = closeScales.length ? Math.max(...closeScales) : null;
  rec(
    "S2.4",
    "退出是反向收拢（缩回入口尺度，不是直接消失）",
    closing.length >= 4 && closeMin !== null && closeMin < 0.5,
    `采样 ${closing.length} 帧，scale ${closeMax} → ${closeMin}`,
    closing.filter((s) => s.t).slice(-6),
  );
  await wait(700);
  const closed = await page.evaluate(() => {
    const v = document.querySelector(".planet-view");
    if (!v) return { ball: false, why: "星球层未挂载" };
    return { ball: v.classList.contains("ball"), cls: v.getAttribute("class") };
  });
  /**
   * 退出后不再是「层被隐藏」，而是回到入口球态（画布缩成球那么大、继续渲染）。
   * 关键不变量：它不再占满全屏（画布本身就是球的大小，而不是全屏画布缩很小）。
   */
  const ballAfterClose = await readBallState();
  rec(
    "S2.5",
    "退出后回到对话页：星球层缩回入口，不再占满全屏",
    closed.ball === true && ballAfterClose.canvasCss !== null && ballAfterClose.canvasCss <= 160,
    `${JSON.stringify(closed)}；画布=${ballAfterClose.canvasCss}px scale=${ballAfterClose.scale}`,
  );
  await shot(page, "s2-after-close");

  /**
   * 第二次打开也必须从入口小球的尺度开始。
   *
   * 这条是用户实测反馈的回归点：球体的屏幕几何此前是用 `canvas.getBoundingClientRect()`
   * 量的，而 canvas 在星球层的 transform 里 —— 第二次打开时层上还残留上一次的 `--stage-k`，
   * 量到的画布高度只有真实的百分之十几，算出的球体半径跟着变小，起始缩放被算成 ≈1，
   * 于是「星球不再从小球长大，而是直接出现」。首次打开永远是干净的，所以只测一次是测不出来的。
   */
  await installPlanetTrace(2600);
  await page.$eval(".dock", (el) => el.click());
  await wait(2400);
  const reopenSamples = await readPlanetTrace();
  await wait(1200);
  // 同样用**屏幕直径**判定（球态 ≈96px）；内部 scale 在球态≈1，已经不是「有多小」的度量了
  const reopenDiameters = reopenSamples
    .map((s) => (s.radius && s.scale ? 2 * s.radius * s.scale : null))
    .filter((v) => v !== null);
  const reopenMin = reopenDiameters.length ? Math.min(...reopenDiameters) : null;
  rec(
    "S2.7",
    "第二次打开同样从入口小球的尺度开始（不因上一次的残留缩放而失去长大）",
    reopenMin !== null && reopenMin < 200,
    `第二次打开最小屏幕直径=${reopenMin === null ? "无" : Math.round(reopenMin)}px（球态约 96px）`,
    reopenSamples.filter((s) => s.scale !== null).slice(0, 8),
  );
  await page.$eval(".close-btn", (el) => el.click()).catch(() => {});
  await wait(900);

  /**
   * S2.9：真正去「看」对话页还在不在。
   *
   * 打开过程用产品自带的 `?planetdemo=slow` 放慢（CSS 令牌与脚本时长一起放大），
   * 在星球还小的时候截一块对话文字区域，量它的像素对比度。
   * 对话页还在 → 有明显明暗差；被不透明层盖住 → 接近 0。
   *
   * 以前这里用的是 CDP `Animation.setPlaybackRate`：它只放慢 CSS，脚本时间线照跑，
   * 「星球还小」的窗口会被压到 200ms 左右，跨进程轮询经常整段错过（实测报「直径≈无」）。
   */
  await page.goto(url("#/").replace("?fresh=", "?planetdemo=slow&fresh="));
  await page.waitForSelector("#composer-input", { timeout: 10000 });
  await page
    .waitForFunction(() => (window.__qioPlanetStage?.() ?? {}).ball === true, { timeout: 12000 })
    .catch(() => note("等入口小球（S2.9 慢放）超时"));
  await wait(700);
  /**
   * 采样方式：常速打开 + 页面内逐帧记录「星球多大 / 铺底多不透明 / 过渡当前时间」，
   * 然后**把两条过渡回放到「星球还小」的那一帧**再截图量像素。
   *
   * 为什么不用「放慢 + 轮询挑时机」这种写法（走过两次弯路）：
   * ① CDP `Animation.setPlaybackRate` 只放慢 CSS，脚本时间线照跑；
   * ② 页面内 `updatePlaybackRate` 逐帧放慢时，铺底的过渡并不会跟着停 ——
   *    等循环盯到「星球 120px」时已经过了好几秒，铺底早就全不透明了，
   *    量出来是 contrast=0、截图整片纯色（无效状态，不是产品的问题）。
   * 回放没有这个毛病：它量的是**那一刻真实存在过的画面**。
   *
   * 观察者必须在**点击之前**装好：点击之后主线程可能被首帧的重活占住（本机 1–3s），
   * 循环真正跑起来时窗口可能已经过去（实测星球已经长到 797px）。
   */
  await page.evaluate(() => {
    const w = window;
    w.__qioScrub = null;
    const t0 = performance.now();
    const isStage = (a) => {
      const el = a.effect && a.effect.target;
      return el instanceof Element && el.classList.contains("planet-stage");
    };
    const isCurtain = (a) => {
      const el = a.effect && a.effect.target;
      return el instanceof Element && el.classList.contains("planet-view") && a.effect.pseudoElement === "::before";
    };
    const measure = () => {
      const st = document.querySelector(".planet-stage");
      const h = window.__qioPlanetStage?.();
      const view = document.querySelector(".planet-view");
      // 「星球现在多大」必须读 computed transform（钩子里的 k 是目标值，不是当前值）
      const m = st ? String(getComputedStyle(st).transform).match(/matrix\(([^)]+)\)/) : null;
      const k = m ? Number(m[1].split(",")[0]) : null;
      const radius = h?.sphere?.radius ?? null;
      return {
        d: k && radius ? Math.round(2 * radius * k) : null,
        curtain: view ? Number(Number(getComputedStyle(view, "::before").opacity).toFixed(3)) : null,
      };
    };
    const raf = () => new Promise((r) => requestAnimationFrame(() => r()));
    const run = async () => {
      // ① 等长大的过渡出现（点击后才创建），一出现就按住 —— 按住才拖得动它
      let stage = null;
      let curtain = null;
      while (performance.now() - t0 < 8000) {
        const all = document.getAnimations();
        all.forEach((a) => a.pause());
        stage = all.find(isStage) ?? null;
        curtain = all.find(isCurtain) ?? null;
        if (stage && curtain) break;
        await raf();
      }
      if (!stage || !curtain) {
        w.__qioScrub = { error: "没等到长大与铺底两条过渡", stage: !!stage, curtain: !!curtain };
        return;
      }
      // ② 手动把两条过渡一起扫一遍（同一 currentTime = 同一时刻）
      const dur = Number(stage.effect?.getTiming?.().duration) || 520;
      const frames = [];
      for (let t = 0; t <= dur; t += Math.max(8, dur / 60)) {
        stage.currentTime = t;
        curtain.currentTime = t;
        await raf();
        frames.push({ t: Math.round(t), ...measure() });
      }
      // ③ 停在「星球还小」的那一帧
      const inWindow = frames.filter((f) => f.d !== null && f.d > 150 && f.d < 400);
      const pick = inWindow.length ? inWindow[Math.floor(inWindow.length / 2)] : null;
      if (pick) {
        stage.currentTime = pick.t;
        curtain.currentTime = pick.t;
      }
      w.__qioScrub = { duration: Math.round(dur), frames: frames.length, window: inWindow.length, pick };
    };
    run();
  });
  await page.$eval(".dock", (el) => el.click());
  await page
    .waitForFunction(() => window.__qioScrub !== null, { timeout: 20000 })
    .catch(() => note("S2.9：等「回放长大过程」超时"));
  await wait(300); // 让合成器按回放后的状态重绘
  const scrub = await page.evaluate(() => window.__qioScrub);
  const earlyScale = scrub?.pick?.d ? 1 : null;
  const earlyDiameter = scrub?.pick?.d ?? null;
  const chatContrastDuring = await regionContrast(CHAT_TEXT_BOX);
  await shot(page, "s2-early-growth-chat");
  await page.evaluate(() => document.getAnimations().forEach((a) => a.play())).catch(() => {});
  rec(
    "S2.9",
    "星球还小的时候，对话页确实还在（不是先被不透明层盖住）",
    earlyScale !== null && chatContrastDuring >= Math.max(30, chatContrastBefore * 0.3),
    `打开前对话区对比度=${chatContrastBefore}；回放到星球屏幕直径≈${earlyDiameter === null ? "无" : earlyDiameter}px 时对比度=${chatContrastDuring}` +
      (scrub ? `（扫过 ${scrub.frames} 帧、其中「球还小」${scrub.window ?? 0} 帧，铺底不透明度=${scrub.pick?.curtain ?? "无"}）` : ""),
  );
  // 慢放/定格只服务这一条：直接重载，回到常速页面（后面的时长类断言要用常速）
  /**
   * S2.21：打开时把**当前在聊的话题**转到画布正中心，浏览记忆不能把它顶掉。
   *
   * 用户实测要求：「星球打开时，如果打开前目前的话题并未在最中心，则应该在打开的过程中旋转至中心」。
   * 原来的定位规则是「起点没变就回到上次浏览的话题」—— 关掉星球前随手看过别的，
   * 再打开时中心就是那个「看过的话题」，当前在聊的话题反而留在旁边。
   * 判据用 app 自己算的投影锚点（`__qioPlanetStage().selectedLabel`）与画布中心的距离；
   * 不读标签 DOM 的矩形 —— 标签自身带 46px 的 CSS 偏移，按它量会稳定偏 150px 左右。
   */
  const readFocus = () =>
    page.evaluate(() => {
      const h = window.__qioPlanetStage?.() ?? null;
      const st = document.querySelector(".planet-stage");
      const c = document.querySelector(".planet-canvas");
      if (!st || !c) return null;
      let left = 0;
      let top = 0;
      let node = st;
      while (node) {
        left += node.offsetLeft;
        top += node.offsetTop;
        node = node.offsetParent;
      }
      return {
        selected: h?.selected ?? null,
        label: h?.selectedLabel ? { x: h.selectedLabel.x, y: h.selectedLabel.y } : null,
        center: { x: left + c.clientWidth / 2, y: top + c.clientHeight / 2 },
      };
    });
  const offCenter = (f) =>
    f && f.label ? Math.round(Math.hypot(f.label.x - f.center.x, f.label.y - f.center.y)) : null;
  await ensureBallMode();
  await openPlanetAndWaitReady();
  await wait(900);
  const focus1 = await readFocus();
  // 在面板里改选另一个话题：这会写进「浏览记忆」
  await page.click(".panel-toggle").catch(() => {});
  await page
    .waitForFunction(() => (document.querySelector(".panel")?.getBoundingClientRect().width ?? 0) > 300, { timeout: 4000 })
    .catch(() => {});
  const switched = await page.evaluate(() => {
    const rows = [...document.querySelectorAll("ul.topic-list li")];
    const other = rows.find((li) => !li.classList.contains("active"));
    if (!other) return null;
    const title = other.querySelector(".name")?.textContent?.trim() ?? "";
    other.click();
    return { title, total: rows.length };
  });
  await wait(1000);
  const focus2 = await readFocus();
  await page.$eval(".close-btn", (el) => el.click()).catch(() => {});
  await ensureBallMode();
  await openPlanetAndWaitReady();
  await wait(1100);
  const focus3 = await readFocus();
  const off1 = offCenter(focus1);
  const off3 = offCenter(focus3);
  /**
   * S2.22：收起时把球**转回打开前的朝向**，而且这件事发生在**体量收缩段**里。
   *
   * 用户要求：「展开和收回的时候**同时**进行球的旋转动画」，并选定「反向转回打开前的朝向」。
   * 量法：球态记 q0 → 打开后记 q1（展开转过去）→ 收起全程逐帧记朝向 →
   * 断言末帧朝向 ≈ q0、且中途确实经过了中间角度（= 转发生在收起里，而不是瞬间归位）。
   * 若这次展开本来就没转多少（话题恰好在中心），本次判为「未运行」而不是失败。
   */
  const quatAngleDeg = (a, b) => {
    if (!a || !b) return null;
    const dot = Math.abs(a[0] * b[0] + a[1] * b[1] + a[2] * b[2] + a[3] * b[3]);
    return Math.round((2 * Math.acos(Math.min(1, dot)) * 180) / Math.PI);
  };
  const readQuat = () => page.evaluate(() => window.__qioPlanetStage?.().debug?.quaternion ?? null);
  let rotateBackResult = null;
  for (let attempt = 0; attempt < 3 && rotateBackResult === null; attempt++) {
    await ensureBallMode();
    // 球态在慢速自转：等一小会儿，让「打开前的朝向」与「话题正对镜头」拉开角度
    await wait(attempt === 0 ? 700 : 2600);
    const q0 = await readQuat();
    await openPlanetAndWaitReady();
    await wait(900);
    const q1 = await readQuat();
    const openAngle = quatAngleDeg(q0, q1);
    if (openAngle === null || openAngle < 10) continue; // 这次没转多少，换一轮再来
    await installPlanetTrace(2600);
    await page.$eval(".close-btn", (el) => el.click()).catch(() => {});
    await page
      .waitForFunction(() => !!document.querySelector(".planet-view.ball"), { timeout: 8000 })
      .catch(() => {});
    const q2 = await readQuat();
    const closeTrace = await readPlanetTrace();
    const angles = closeTrace.map((f) => quatAngleDeg(q0, f.quaternion)).filter((v) => v !== null);
    rotateBackResult = {
      openAngle,
      endAngle: quatAngleDeg(q0, q2),
      firstAngle: angles.length ? angles[0] : null,
      midAngles: angles.filter((v, i) => i > 0 && i < angles.length - 1),
      frames: angles.length,
    };
  }
  const midOk =
    rotateBackResult !== null &&
    rotateBackResult.midAngles.some((v) => v < rotateBackResult.openAngle - 5 && v > 8);
  rec(
    "S2.22",
    "收起时把球转回打开前的朝向（且转发生在体量收缩段里）",
    rotateBackResult === null
      ? null
      : rotateBackResult.endAngle !== null && rotateBackResult.endAngle <= 6 && midOk,
    rotateBackResult === null
      ? "未运行：三次打开的话题都在中心附近，展开本来就没转"
      : `展开转了 ${rotateBackResult.openAngle}°；收起逐帧采样 ${rotateBackResult.frames} 帧，` +
        `首帧偏离=${rotateBackResult.firstAngle}°、末帧偏离=${rotateBackResult.endAngle}°；` +
        `中途是否经过中间角度=${midOk}`,
  );
  rec(
    "S2.21",
    "打开时把当前在聊的话题转到画布正中心（浏览记忆不顶掉它）",
    switched !== null && off1 !== null && off3 !== null && off1 <= 12 && off3 <= 12 && focus3.selected === focus1.selected,
    `打开后：话题=${focus1.selected} 偏离中心=${off1}px；` +
      `面板里改选「${switched ? switched.title : "未找到其它话题"}」后收起再打开：话题=${focus3.selected} 偏离中心=${off3}px` +
      `（第一轮记录的当前话题=${focus1.selected}，第二轮的选中态=${focus2 ? focus2.selected : "无"}）`,
  );

  // 慢放/定格只服务这一条：直接重载，回到常速页面（后面的时长类断言要用常速）
  await page.goto(url("#/"));
  await page.waitForSelector("#composer-input", { timeout: 10000 });
  await page
    .waitForFunction(() => (window.__qioPlanetStage?.() ?? {}).ball === true, { timeout: 12000 })
    .catch(() => note("等入口小球（S2.9 恢复常速）超时"));
  await wait(400);

  /**
   * S2.10：收起只能有**一段**缩小（星球层缩回入口小球）。
   *
   * 以前这里有两段：先是相机后撤让球体看起来变小，然后星球层再缩回小球 —— 用户实测反馈
   * 「收起有两段动画，一段先缩小一点，第二段才缩回小星球」。相机后撤是旧设计（收势 + 整层淡出）
   * 的遗留物，在「同一个对象收拢回入口」的编排里它只是多余的第二段缩小。
   *
   * 量法：用开发钩子读球体的屏幕半径（相机后撤会让它变小）+ 星球层的 scale（锁定缩放段）。
   */
  // 先完整打开一次（收势阶段要有东西可收）
  await page.$eval(".dock", (el) => el.click());
  await page
    .waitForFunction(() => window.__qioPlanetStage?.().phase === "ready", { timeout: 8000 })
    .catch(() => note("S2.10：等「场景接管」超时，仍继续采样"));
  await wait(500);
  const closeTrace = [];
  await page.$eval(".close-btn", (el) => el.click());
  for (let i = 0; i < 40; i++) {
    const s = await page
      .evaluate(() => {
        const hook = window.__qioPlanetStage?.();
        const st = document.querySelector(".planet-stage");
        const m = st ? String(getComputedStyle(st).transform).match(/matrix\(([^)]+)\)/) : null;
        return {
          radius: hook?.sphere?.radius ?? null,
          scale: m ? Number(m[1].split(",")[0]) : null,
          phase: hook?.phase ?? null,
        };
      })
      .catch(() => null);
    if (s) closeTrace.push(s);
    await wait(30);
  }
  /**
   * 「收势」阶段的采样：星球层还没开始缩放（scale ≈ 1）。
   *
   * 注意要排掉 **returning（体量收缩）**里的帧：收缩尾段切到球布局后，星球层的 scale
   * 又回到 ≈1（画布本身就只剩球那么大），只按 scale 过滤会把「确实在缩小的尾段」算进
   * 收势阶段，于是判成「收势阶段球体大小在变」的假失败（实测漂移 88%）。
   * 阶段由连续体自己给出：收势是 collapsing，体量收缩是 returning。
   */
  const settleFrames = closeTrace.filter(
    (s) => s.phase === "collapsing" && s.scale !== null && s.scale > 0.98 && s.radius,
  );
  const radii = settleFrames.map((s) => s.radius);
  const radiusDrift = radii.length >= 2 ? (Math.max(...radii) - Math.min(...radii)) / Math.max(...radii) : null;
  rec(
    "S2.10",
    "收起只有一段缩小：收势阶段球体屏幕大小不变（没有相机后撤带来的额外缩小）",
    radiusDrift !== null && radiusDrift < 0.05,
    `收势阶段采样 ${settleFrames.length} 帧，球体半径 ${radii.length ? Math.round(Math.min(...radii)) + "~" + Math.round(Math.max(...radii)) : "无"}，漂移=${radiusDrift === null ? "无" : (radiusDrift * 100).toFixed(1) + "%"}`,
  );
  await wait(1200);

  /**
   * S2.11：收起之后不是「消失」，而是回到入口小球 —— 而且这颗小球就是真实渲染在慢速自转。
   * 判据用场景自己的自转角与话题窗口，不靠像素猜。
   */
  const ballState = await readBallState();
  await wait(1500);
  const rotationLater = await page.evaluate(() => window.__qioPlanetStage?.().debug?.rotationY ?? null);
  rec(
    "S2.11",
    "收起后回到入口球态：真实渲染缩在入口、有真实话题形状、慢速自转",
    ballState.ball === true &&
      ballState.canvasCss !== null &&
      ballState.canvasCss <= 160 &&
      ballState.windowSize > 0 &&
      rotationLater !== null &&
      ballState.rotationY !== null &&
      rotationLater > ballState.rotationY,
    `ball=${ballState.ball} 画布=${ballState.canvasCss}px scale=${ballState.scale} 话题窗口=${ballState.windowSize} 低帧率=${ballState.lowPower} 自转 ${ballState.rotationY} → ${rotationLater}`,
  );

  /** S2.12：收起之后对话页真的回来了（像素级，不是「层被隐藏了」这种代理指标） */
  const chatContrastAfterClose = await regionContrast(CHAT_TEXT_BOX);
  rec(
    "S2.12",
    "收起后对话页真的回来了（文字区域重新有明暗差）",
    chatContrastAfterClose >= Math.max(30, chatContrastBefore * 0.6),
    `收起后对话区对比度=${chatContrastAfterClose}（打开前 ${chatContrastBefore}）`,
  );

  /**
   * S2.13：打开右侧话题栏之后再收起 —— 话题栏必须跟着退场，不能留在对话页上。
   *
   * 用户实测反馈：「打开星球并打开右侧话题页，再关闭星球回到对话页时，话题页不会随之退出」。
   * 根因：入口小球常驻之后，收起不再是「整层隐藏」，而右侧面板的显隐只由 open/entered 驱动，
   * 于是它跟着球态一起留在屏幕上。
   */
  await page.$eval(".dock", (el) => el.click());
  await page
    .waitForFunction(() => (window.__qioPlanetStage?.() ?? {}).phase === "ready", { timeout: 12000 })
    .catch(() => note("S2.13：等「场景接管」超时"));
  await page.click(".panel-toggle").catch(() => note("S2.13：未找到边栏展开钮"));
  // 等面板宽度**稳定**再读：软件渲染下单次 evaluate/click 都可能吃掉上百毫秒，
  // 固定 500ms 会在负载高时读到过渡中途（实测读到 120px，把正常行为判成失败）。
  await page
    .waitForFunction(
      () => {
        const el = document.querySelector(".panel");
        if (!el) return true;
        return el.getBoundingClientRect().width > 300;
      },
      { timeout: 4000 },
    )
    .catch(() => note("S2.13：面板宽度没到展开态"));
  const panelOpened = await page.evaluate(() => {
    const el = document.querySelector(".panel");
    return el ? { width: Math.round(el.getBoundingClientRect().width), opacity: Number(getComputedStyle(el).opacity) } : null;
  });
  await page.$eval(".close-btn", (el) => el.click());
  await wait(1600);
  const panelAfterClose = await page.evaluate(() => {
    const el = document.querySelector(".panel");
    if (!el) return { gone: true };
    const st = getComputedStyle(el);
    return { gone: false, width: Math.round(el.getBoundingClientRect().width), opacity: Number(Number(st.opacity).toFixed(2)) };
  });
  rec(
    "S2.13",
    "收起星球时右侧话题栏一起退场（不留在对话页上）",
    panelOpened !== null &&
      panelOpened.width > 200 &&
      (panelAfterClose.gone === true || (panelAfterClose.width < 20 && panelAfterClose.opacity < 0.05)),
    `打开面板时=${JSON.stringify(panelOpened)}；收起后=${JSON.stringify(panelAfterClose)}`,
  );

  /**
   * S2.15：拖动入口小球后松手（贴边补间）时，**星球本体的位置必须跟着走**。
   *
   * 用户实测反馈：「松开手后标注有动画，但星球本身没有，在原地停滞一段时间后闪现靠边」。
   * 根因：贴边是 CSS 过渡，内联 left/top 只在开始时改一次，属性观察器不会再触发 ——
   * 只靠观察器时球层会停在原地。现在过渡期间逐帧跟随，这条断言守着它。
   */
  const dockBox = await page.locator("[data-planet-entry]").boundingBox();
  if (dockBox) {
    const dx = dockBox.x + dockBox.width / 2;
    const dy = dockBox.y + dockBox.height / 2;
    await page.mouse.move(dx, dy);
    await page.mouse.down();
    await page.mouse.move(dx - 300, dy - 150, { steps: 8 });
    // 页面内逐帧记录：贴边补间只有 ~200ms（本机软件渲染下每帧还可能被拉长到 90ms+），
    // 跨进程轮询会漏掉大半（实测只采到 1 帧），于是「逐帧跟随」这条根本判不了。
    await page.evaluate(() => {
      const trace = [];
      const t0 = performance.now();
      window.__qioSnapTrace = trace;
      const step = () => {
        const dock = document.querySelector("[data-planet-entry]");
        const stage = document.querySelector(".planet-stage");
        if (dock && stage) {
          const dr = dock.getBoundingClientRect();
          const sr = stage.getBoundingClientRect();
          trace.push({
            ms: Math.round(performance.now() - t0),
            snapping: dock.classList.contains("fw-snapping"),
            dock: [dr.left + dr.width / 2, dr.top + dr.height / 2],
            ball: [sr.left + sr.width / 2, sr.top + sr.height / 2],
          });
        }
        if (performance.now() - t0 < 1200) requestAnimationFrame(step);
      };
      requestAnimationFrame(step);
    });
    await page.mouse.up();
    await wait(1400);
    const snapTrace = await page.evaluate(() => window.__qioSnapTrace ?? []);
    const during = snapTrace.filter((s) => s.snapping);
    /**
     * 判据是「**不超过一帧的滞后**」，不是「偏差必须小于某个像素数」。
     *
     * 入口的贴边是 CSS 过渡（合成器推进），球层是 rAF 跟随 —— 后者天然可能落后一帧；
     * 本机软件渲染下帧间隔能到 90ms、单帧位移能到 84px，于是「偏差 < 8px」这种写法
     * 会把正常行为判成失败（实测就是这么误判的）。而旧 bug（球层停在原地、最后闪现）
     * 的特征是偏差远大于入口自己一帧走了多少 —— 用这个形状判，帧率再抖也成立。
     */
    let maxDelta = 0;
    let maxLagPx = -1e9;
    for (let i = 1; i < during.length; i++) {
      const delta = Math.hypot(during[i].dock[0] - during[i].ball[0], during[i].dock[1] - during[i].ball[1]);
      const dockMoved = Math.hypot(during[i].dock[0] - during[i - 1].dock[0], during[i].dock[1] - during[i - 1].dock[1]);
      maxDelta = Math.max(maxDelta, delta);
      maxLagPx = Math.max(maxLagPx, delta - dockMoved);
    }
    const ballTraveled = during.length
      ? Math.hypot(during[during.length - 1].ball[0] - during[0].ball[0], during[during.length - 1].ball[1] - during[0].ball[1])
      : 0;
    const dockTraveled = during.length
      ? Math.hypot(during[during.length - 1].dock[0] - during[0].dock[0], during[during.length - 1].dock[1] - during[0].dock[1])
      : 0;
    /**
     * 判据是「**入口动了，球层有没有跟着动**」：
     * - 入口这次几乎没动（已经贴在该贴的边上）→ 这次场景不适用，报「未运行」，不当失败；
     * - 入口动了而球层没跟上（旧 bug 的形状）→ 失败。
     */
    if (dockTraveled <= 10) {
      rec(
        "S2.15",
        "贴边补间期间星球本体的位置跟着入口走",
        null,
        `未运行：这次贴边入口只移动了 ${Math.round(dockTraveled)}px（补间采样 ${during.length} 帧）`,
      );
    } else {
      rec(
        "S2.15",
        "贴边补间期间星球本体的位置跟着入口走（不出现「停住再闪现」）",
        during.length >= 1 && maxLagPx <= 4 && ballTraveled > 0.6 * dockTraveled,
        `补间采样 ${during.length} 帧；入口移动 ${Math.round(dockTraveled)}px、球层移动 ${Math.round(ballTraveled)}px；` +
          `最大偏差=${Math.round(maxDelta)}px；` +
          `偏差超出「入口单帧位移」的最大值=${maxLagPx < -1e8 ? "无" : Math.round(maxLagPx) + "px"}`,
      );
    }
  } else {
    rec("S2.15", "贴边补间期间星球本体的位置跟着入口走", null, "未运行：没有拿到入口小球的几何");
  }
}

async function scenario3PlanetBrowse() {
  await openPlanet();
  await wait(800);
  await page.click(".panel-toggle").catch(() => note("未找到 .panel-toggle（边栏展开钮）"));
  await wait(500);
  await shot(page, "s3-planet-panel");
  const panel = await page.evaluate(() => {
    const el = document.querySelector(".panel");
    if (!el) return null;
    const st = getComputedStyle(el);
    return { bg: st.backgroundColor, border: st.borderLeftColor, opacity: st.opacity };
  });
  rec("S3.1", "星球侧栏不是普通网页白底弹窗", panel !== null && panel.bg !== "rgb(255, 255, 255)", JSON.stringify(panel));
  await page.keyboard.press("Escape");
  await wait(500);
  const afterEsc = await page.evaluate(() => ({
    planet: !!document.querySelector(".planet-view"),
    panelOpen: !!document.querySelector(".panel.open"),
  }));
  rec("S3.2", "Esc 分层：先收边栏，星球仍在", afterEsc.planet && !afterEsc.panelOpen, JSON.stringify(afterEsc));
  await page.keyboard.press("Escape");
  await wait(1600);
  const planetGone = await page.evaluate(() => {
    const v = document.querySelector(".planet-view");
    if (!v) return true;
    return v.classList.contains("ball");
  });
  const ballAfterEsc = await readBallState();
  rec(
    "S3.3",
    "再次 Esc 收起星球（回到入口球态：画布缩回球大小）",
    planetGone === true && ballAfterEsc.canvasCss !== null && ballAfterEsc.canvasCss <= 160,
    `回到入口=${planetGone}；画布=${ballAfterEsc.canvasCss}px`,
  );
}

async function scenario4KnowledgeEntity() {
  await openPlanet();
  await wait(700);
  await page.click(".panel-toggle").catch(() => {});
  await wait(400);
  await page.click(".tab-knowledge").catch(() => note("未找到 .tab-knowledge"));
  await wait(800);
  await shot(page, "s4-knowledge");
  const reading = await page.evaluate(() => {
    const panel = document.querySelector(".kpanel");
    if (!panel) return null;
    const visibleTextareas = [...panel.querySelectorAll("textarea")].filter((t) => t.offsetParent !== null).length;
    return { visibleTextareas, badges: panel.querySelectorAll(".qio-state").length };
  });
  rec("S4.1", "知识页默认是阅读态（不铺满编辑控件）", reading !== null && reading.visibleTextareas === 0, JSON.stringify(reading));
  rec("S4.2", "知识条目状态用统一状态徽章", reading !== null && reading.badges > 0, `qio-state 数量=${reading ? reading.badges : 0}`);
  await page.click(".tab-entity").catch(() => note("未找到 .tab-entity"));
  await wait(800);
  await shot(page, "s4-entity");
  rec("S4.3", "知识/实体浏览全程没有原生确认框", dialogs.length === 0, `dialogs=${JSON.stringify(dialogs)}`);
  await page.keyboard.press("Escape");
  await wait(400);
  await page.keyboard.press("Escape");
  await wait(1000);
}

async function scenario5Approval() {
  /**
   * 这个场景会 POST 一条测试审批事件到**共享的**后端，而前端是 SSE 订阅：
   * 所有打开着的页面都会收到它 —— 包括用户自己正在用的那个窗口
   * （2026-09-20 实测：跑一次验收脚本，就把一个「工具创建审批」弹窗弹到了用户脸上，
   * 还盖住了整页导致后续点击全部无效）。所以默认不跑，需要时显式打开。
   */
  if (process.env.QIO_QA_INJECT !== "1") {
    report.notRun.push(
      "S5 审批场景：会向共享后端注入一条测试审批事件（所有打开的页面都会弹窗），默认不跑；需要时用 QIO_QA_INJECT=1 显式开启",
    );
    rec("S5.1", "审批卡使用统一卡片语言", null, "未运行：默认不注入（避免污染正在使用的页面）");
    rec("S5.2", "审批流程没有触发原生 confirm/alert", null, "未运行：同上");
    return;
  }
  note("本次会向共享后端注入测试审批事件：所有已连接的页面（含你正在用的那个）都会弹出审批窗口");
  await page.goto(url("#/"));
  await page.waitForSelector("#composer-input", { timeout: 10000 });
  await wait(400);
  try {
    await page.request.post(`${API}/api/events/test?event_type=APPROVAL_REQUIRED`, {
      data: {
        approval: {
          approval_id: `qa_p4_${Date.now()}`,
          kind: "tool_create",
          payload: {
            name: "fetch_doc",
            description: "抓取指定文档并写入工作区",
            explanation: "你要求自动归档资料，需要在联网抓取后落盘",
            capabilities: ["联网：是（限 api.example.com）", "写入文件：是", "副作用：write"],
            policy_fingerprint: "fp_phase4",
            test_summary: "3/3 检查通过",
            test_details: [{ name: "dry_run", passed: true, detail: "无异常" }],
          },
        },
      },
    });
  } catch (e) {
    rec("S5.1", "注入审批事件", null, `未运行：${e && e.message ? e.message : e}`);
    return;
  }
  await page.waitForSelector(".modal, .approval-entry", { timeout: 6000 }).catch(() => {});
  await wait(500);
  await shot(page, "s5-approval");
  const card = await page.evaluate(() => {
    const el = document.querySelector(".modal") || document.querySelector(".approval-entry");
    if (!el) return null;
    return { classes: el.className, dataState: el.getAttribute("data-state"), states: el.querySelectorAll(".qio-state").length };
  });
  rec("S5.1", "审批卡使用统一卡片语言", card !== null && card.states > 0, JSON.stringify(card));
  rec("S5.2", "审批流程没有触发原生 confirm/alert", dialogs.length === 0, `dialogs=${JSON.stringify(dialogs)}`);
}

async function scenario6ToolCreation() {
  const card = await page.evaluate(() => {
    const el = document.querySelector('.create-card, [data-card="tool-creation"]');
    if (!el) return null;
    return { classes: el.className, dataState: el.getAttribute("data-state") };
  });
  if (card === null) {
    report.notRun.push("S6：本轮界面上没有工具创建卡（需要真实模型触发创建流程）");
    rec("S6.1", "工具创建卡家族契约", null, "未运行：界面上没有工具创建卡");
    return;
  }
  rec("S6.1", "工具创建卡家族契约", card.dataState !== null, JSON.stringify(card));
}

async function scenario7ReducedMotion() {
  const p2 = await context.newPage();
  await p2.goto(url("#/"));
  await p2.evaluate(() => localStorage.setItem("qio-motion", "reduced"));
  await p2.goto(url("#/"));
  await p2.waitForSelector("#composer-input", { timeout: 10000 });
  await wait(400);
  const probe = await p2.evaluate(() => {
    const el = document.querySelector(".qio-btn") || document.querySelector(".composer");
    const st = el ? getComputedStyle(el) : null;
    const root = getComputedStyle(document.documentElement);
    return {
      motion: document.documentElement.getAttribute("data-motion"),
      dur: st ? st.transitionDuration : "",
      shift1: root.getPropertyValue("--shift-1").trim(),
      ease3: root.getPropertyValue("--ease-3-settle").trim(),
    };
  });
  const firstDur = String(probe.dur).split(",")[0].trim();
  const durMs = firstDur.endsWith("ms") ? Number(firstDur.replace("ms", "")) : Number(firstDur.replace("s", "")) * 1000;
  rec("S7.1", "reduced-motion 下保留连续性（时长压缩但不归零）", probe.motion === "reduced" && durMs > 0, JSON.stringify({ ...probe, durMs }));
  rec("S7.2", "reduced-motion 下位移令牌归零", probe.shift1 === "0px", `--shift-1=${probe.shift1}`);
  rec("S7.3", "reduced-motion 下低频弹性曲线退化为标准曲线", !probe.ease3.includes("1.04"), `--ease-3-settle=${probe.ease3}`);
  await p2.click(".dock").catch(() => note("reduced-motion 下未找到入口"));
  await p2.waitForSelector(".planet-view", { state: "visible", timeout: 8000 }).catch(() => {});
  await wait(500);
  await p2.screenshot({ path: `${SHOTS}\\phase4-s7-reduced-planet.png` });
  const opacity = await p2.evaluate(() => {
    const v = document.querySelector(".planet-view");
    return v ? Number(getComputedStyle(v).opacity) : 0;
  });
  rec("S7.4", "reduced-motion 下星球仍然完整可用（不是空白）", opacity > 0.8, `opacity=${opacity}`);
  await p2.close();
}

/**
 * 场景 8：**系统级** reduced-motion 的端到端降级。
 *
 * S7 验的是令牌与 CSS 层；这里用真实的 `prefers-reduced-motion: reduce`
 * （Playwright 的 emulateMedia，不是应用内的 data-motion）跑完整链路：
 * 应用是否解析系统偏好 → 星球三段编排是否被压到很短 → 星球是否仍然完整可用 →
 * 以及用户显式选「标准」时能否覆盖系统偏好。
 */
async function scenario8SystemReducedMotion() {
  const ctx = await browser.newContext({ viewport: { width: 1440, height: 900 }, reducedMotion: "reduce" });
  const page = await ctx.newPage();
  await page.goto(url("#/"));
  await page.waitForSelector("#composer-input", { timeout: 10000 });
  await wait(400);

  /**
   * 先等入口小球就位（真实渲染已经在跑）再点。
   * 否则量到的是「懒加载 chunk + WebGL 冷启动」，而不是「减少动画下的展开编排」——
   * 真实用户点的时候小球早就在那儿了。
   */
  await page
    // 谓词必须对「hook 还不存在」安全：直接 ?.().ball 会抛错，而被 catch 掉就变成「根本没等」
    .waitForFunction(() => (window.__qioPlanetStage?.() ?? {}).ball === true, { timeout: 20000 })
    .catch(() => note("S8：入口球态未在 20s 内就位，本次测量含冷启动"));

  const resolved = await page.evaluate(() => ({
    systemReduced: matchMedia("(prefers-reduced-motion: reduce)").matches,
    dataMotion: document.documentElement.getAttribute("data-motion"),
    stored: localStorage.getItem("qio-motion"),
  }));
  rec(
    "S8.1",
    "系统偏好 reduced-motion 时应用自动解析为「减少动画」",
    resolved.systemReduced === true && resolved.dataMotion === "reduced",
    JSON.stringify(resolved),
  );

  // 打开星球：编排被压到很短，且最终完整可用
  const t0 = Date.now();
  await page.$eval(".dock", (el) => el.click());
  await page.waitForFunction(() => !!document.querySelector(".planet-stage"), { timeout: 8000 }).catch(() => {});
  const stageT0 = Date.now();
  await page
    .waitForFunction(() => window.__qioPlanetStage?.().phase === "ready", { timeout: 8000 })
    .catch(() => {});
  const readyMs = Date.now() - stageT0;
  const stage = await page.evaluate(() => {
    const el = document.querySelector(".planet-stage");
    if (!el) return null;
    const cs = getComputedStyle(el);
    return { k: el.getAttribute("data-stage-k"), duration: cs.transitionDuration, opacity: getComputedStyle(document.querySelector(".planet-view")).opacity };
  });
  // 星球层自己有 90ms 的透明度过渡（球态 → 展开态）；等它落定再读「是否可用」，
  // 否则读到过渡起点会把它误判成空白（实测踩过一次）。
  await wait(400);
  const settledOpacity = await page.evaluate(() => {
    const v = document.querySelector(".planet-view");
    return v ? Number(getComputedStyle(v).opacity) : 0;
  });
  rec(
    "S8.2",
    "减少动画下不走长编排：到「场景接管」的时间很短",
    readyMs < 900 && stage !== null && Number(stage.k) > 0.99,
    `阶段就绪耗时≈${readyMs}ms（点击到挂载≈${stageT0 - t0}ms），scale=${stage ? stage.k : "无"}，transition=${stage ? stage.duration : "无"}`,
  );
  rec(
    "S8.3",
    "减少动画下星球仍然完整可用（不是空白、不靠动画事件收尾）",
    stage !== null && settledOpacity > 0.9,
    `${JSON.stringify(stage)}；稳定后 opacity=${settledOpacity}`,
  );
  await page.screenshot({ path: `${SHOTS}\\phase4-s8-system-reduced.png` });

  // 关闭仍然可用
  await page.keyboard.press("Escape");
  await wait(600);
  const closed = await page.evaluate(() => {
    const v = document.querySelector(".planet-view");
    if (!v) return true;
    /**
     * 契约（第四阶段起）：关闭 = **回到入口球态**（同一个渲染缩回入口），
     * 不是「整层隐藏」—— 球态下这一层本来就该是可见的（那颗小球就是它）。
     * 所以判据是「回到球态」或（WebGL 不可用时的降级路径）「整层隐藏」。
     */
    const c = document.querySelector(".planet-canvas");
    const isBall = v.classList.contains("ball") && (c ? c.clientWidth <= 160 : true);
    const hidden = getComputedStyle(v).display === "none" || Number(getComputedStyle(v).opacity) < 0.05;
    return isBall || hidden;
  });
  rec("S8.4", "减少动画下 Esc 收起仍然正常", closed === true, `planetHidden=${closed}`);

  // 用户显式选「标准」必须能覆盖系统偏好
  await page.evaluate(() => localStorage.setItem("qio-motion", "standard"));
  await page.goto(url("#/"));
  await page.waitForSelector("#composer-input", { timeout: 10000 });
  await wait(400);
  const override = await page.evaluate(() => {
    const root = getComputedStyle(document.documentElement);
    return {
      dataMotion: document.documentElement.getAttribute("data-motion"),
      shift1: root.getPropertyValue("--shift-1").trim(),
      mo3: root.getPropertyValue("--mo-3-expand").trim(),
    };
  });
  rec(
    "S8.5",
    "显式选「标准」可以覆盖系统偏好（位移与低频时长恢复）",
    override.dataMotion === "standard" && override.shift1 !== "0px" && override.mo3 !== "90ms",
    JSON.stringify(override),
  );
  await ctx.close();
}

/** 取一个浮层的玻璃特征（亮色下必须仍然是「高透低雾 + 冷暖边分离」，不是塑料片） */
const glassInfo = (page, sel) =>
  page.evaluate((s) => {
    const el = document.querySelector(s);
    if (!el) return null;
    const cs = getComputedStyle(el);
    return {
      bg: cs.backgroundImage.slice(0, 56),
      blur: cs.backdropFilter || cs.webkitBackdropFilter || "none",
      warm: cs.borderLeftColor,
      cool: cs.borderRightColor,
      text: (el.textContent || "").trim().slice(0, 40),
    };
  }, sel);

const isLightGlass = (info) =>
  info !== null &&
  /gradient/.test(info.bg) &&
  /blur\(/.test(info.blur) &&
  info.warm !== info.cool && // 冷暖边分离（折射暗示）
  info.text.length > 0;

/**
 * 当前是否处于「入口球态」。
 *
 * 契约（2026-09-20 起）：球态 = `.planet-view.ball` + **画布本身就是球那么大**
 * （108px，scale≈1，见 S2.16）。**不再**用「整层缩放很小」表达 —— 那正是
 * 「轮廓全是像素」的根因。
 */
const readBallState = () =>
  page.evaluate(() => {
    const v = document.querySelector(".planet-view");
    const c = document.querySelector(".planet-canvas");
    const st = document.querySelector(".planet-stage");
    const m = st ? String(getComputedStyle(st).transform).match(/matrix\(([^)]+)\)/) : null;
    const h = window.__qioPlanetStage?.();
    return {
      ball: v ? v.classList.contains("ball") : false,
      canvasCss: c ? c.clientWidth : null,
      scale: m ? Math.round(Number(m[1].split(",")[0]) * 1000) / 1000 : null,
      windowSize: h?.debug?.windowSize ?? null,
      lowPower: h?.debug?.lowPower ?? null,
      rotationY: h?.debug?.rotationY ?? null,
    };
  });

/**
 * 确保回到入口球态（关掉星球并等到球态就位）。
 *
 * 用 DOM 点击而不是 Playwright 的 click：入口小球一直在呼吸/自转，而且 `.close-btn`
 * 在球态下是不可交互的（opacity 0 / pointer-events none），actionability 检查会一直等下去。
 */
async function ensureBallMode() {
  const isBall = await page.evaluate(() => !!document.querySelector(".planet-view.ball"));
  if (!isBall) {
    await page.$eval(".close-btn", (el) => el.click()).catch(() => {});
  }
  await page
    .waitForFunction(() => !!document.querySelector(".planet-view.ball"), { timeout: 8000 })
    .catch(() => note("等回入口球态超时"));
  await wait(300);
}

/** 打开星球并等到「场景接管」（同样绕开 actionability 等待） */
async function openPlanetAndWaitReady(timeout = 12000) {
  await page.$eval(".dock", (el) => el.click());
  await page
    .waitForFunction(() => (window.__qioPlanetStage?.() ?? {}).phase === "ready", { timeout })
    .catch(() => note("等「场景接管」超时"));
}

/**
 * 在页面内逐帧记录星球层的状态（rAF）。
 *
 * 为什么不用「点击后在外面轮询」：每次 `page.evaluate` 都要跨进程往返，
 * 软件渲染 + 首屏繁忙时单次能到几百毫秒 —— 实测第一帧就已经落在长大后半段，
 * 于是「起点是不是球那么大」这类断言会被测量误差判失败（假失败）。
 * 页面内记录没有往返开销，能拿到真正的第一帧。
 */
async function installPlanetTrace(ms = 4000) {
  await page.evaluate((duration) => {
    const trace = [];
    const t0 = performance.now();
    const step = () => {
      const st = document.querySelector(".planet-stage");
      const view = document.querySelector(".planet-view");
      const h = window.__qioPlanetStage?.();
      if (st && h) {
        const m = String(getComputedStyle(st).transform).match(/matrix\(([^)]+)\)/);
        // 层的布局原点（offsetParent 链）：屏幕坐标 = origin + T + k·(布局坐标 − origin)
        let left = 0;
        let top = 0;
        let node = st;
        while (node) {
          left += node.offsetLeft;
          top += node.offsetTop;
          node = node.offsetParent;
        }
        trace.push({
          ms: Math.round(performance.now() - t0),
          left,
          top,
          k: h.k,
          scale: m ? Number(m[1].split(",")[0]) : null,
          tx: m ? Number(m[1].split(",")[4]) : null,
          ty: m ? Number(m[1].split(",")[5]) : null,
          radius: h.sphere?.radius ?? null,
          sphereCx: h.sphere?.cx ?? null,
          sphereCy: h.sphere?.cy ?? null,
          selected: h.selected ?? null,
          rotationY: h.debug?.rotationY ?? null,
          quaternion: h.debug?.quaternion ?? null,
          ball: h.ball ?? null,
          tail: view ? view.classList.contains("tail") : null,
          curtain: view ? Number(getComputedStyle(view, "::before").opacity) : null,
          mask: (() => {
            const c = document.querySelector(".planet-canvas");
            if (!c) return "none";
            const cs = getComputedStyle(c);
            return cs.maskImage || cs.webkitMaskImage || "none";
          })(),
          canvas: document.querySelector(".planet-canvas")?.clientWidth ?? null,
        });
      }
      if (performance.now() - t0 < duration) requestAnimationFrame(step);
      else window.__qioPlanetTrace = trace;
    };
    window.__qioPlanetTrace = trace;
    requestAnimationFrame(step);
  }, ms);
}

async function readPlanetTrace() {
  return await page.evaluate(() => window.__qioPlanetTrace ?? []);
}

/**
 * 场景 9：**净白主题**下的浮层覆盖。
 *
 * S2/S3 只看了暗色下的退出控件与话题标签；这个场景把三类「平时不容易一起出现」的浮层
 * 在亮色下逐个触发出来核对：加载条（拖慢接口）、错误条（让接口失败）、HUD（开发者模式）。
 */
async function scenario9LightGlassLayers() {
  const ctx = await browser.newContext({ viewport: { width: 1440, height: 900 } });
  await ctx.addInitScript(() => {
    try {
      localStorage.setItem("qio-theme", "light");
      localStorage.setItem("qio-developer-mode", "1");
      /**
       * 这一场景要的是**冷路径**（场景还没预热就点开）—— 只有冷路径才会出现「正在加载话题…」。
       * 入口小球现在是空闲时挂载的，什么时候挂上取决于机器忙不忙（实测会飘），
       * 所以这里把空闲挂载关掉，让「加载条」这条断言可复现，而不是碰运气。
       */
      window.requestIdleCallback = () => 0;
    } catch {
      /* 忽略 */
    }
  });
  const page = await ctx.newPage();

  // 1) 加载条：把星球概览接口拖慢，让「正在加载话题…」稳定停留在屏幕上
  await page.route("**/api/planet/overview*", async (route) => {
    await wait(2500);
    // 页面可能已经导航走了：这时 route 已被处理，continue 会抛错
    await route.continue().catch(() => {});
  });
  await page.goto(url("#/"));
  await page.waitForSelector("#composer-input", { timeout: 10000 });
  /**
   * 立刻点开（不等入口小球预热）。
   *
   * 入口小球常驻之后，「已预热」的打开路径不再重新取数据 —— 它也就没有「加载中」状态了，
   * 这是对的（没有要加载的东西就不该假装在加载）。加载条只在**冷路径**出现：
   * 场景还没就绪、用户直接点开。所以这里按冷路径来造场景。
   */
  await page.$eval(".dock", (el) => el.click());
  await page.waitForSelector(".planet-loading", { timeout: 10000 }).catch(() => note("未捕获到加载条（接口太快）"));
  const loading = await glassInfo(page, ".planet-loading");
  await shot(page, "s9-light-loading");
  rec("S9.1", "净白主题：加载条是晶体玻璃", isLightGlass(loading), JSON.stringify(loading));

  // 2) 错误条：让星球概览接口直接失败
  await page.unroute("**/api/planet/overview*");
  await page.route("**/api/planet/overview*", (route) =>
    route.fulfill({
      status: 500,
      contentType: "application/json",
      body: JSON.stringify({ detail: "qa 造错：星球数据暂不可用" }),
    }),
  );
  await page.goto(url("#/"));
  await page.waitForSelector("#composer-input", { timeout: 10000 });
  await page.$eval(".dock", (el) => el.click());
  await page.waitForSelector(".load-error", { timeout: 10000 }).catch(() => note("未捕获到错误条"));
  const errorBar = await glassInfo(page, ".load-error");
  await shot(page, "s9-light-error");
  rec("S9.2", "净白主题：数据加载失败条是晶体玻璃且有可读文案", isLightGlass(errorBar), JSON.stringify(errorBar));
  const errFirst = await page.evaluate(
    () => document.querySelector(".load-error .le-text")?.textContent?.trim() ?? "",
  );
  rec(
    "S9.5",
    "失败条首行是人话（内部接口与状态码只在折叠的技术详情里）",
    errFirst.length > 0 && !/\/api\/|\b5\d\d\b/.test(errFirst),
    `首行=「${errFirst}」`,
  );

  // 3) HUD：开发者模式下的诊断浮层
  await page.unroute("**/api/planet/overview*");
  await page.goto(url("#/"));
  await page.waitForSelector("#composer-input", { timeout: 10000 });
  await page.$eval(".dock", (el) => el.click());
  await page.waitForSelector(".hud", { timeout: 10000 }).catch(() => note("未捕获到 HUD（开发者模式没生效？）"));
  const hud = await glassInfo(page, ".hud");
  await shot(page, "s9-light-hud");
  rec("S9.3", "净白主题：开发者 HUD 是晶体玻璃", isLightGlass(hud), JSON.stringify(hud));

  // 4) 退出控件（亮色下再确认一次，与 S2.3 对应）
  const closeBtn = await glassInfo(page, ".close-btn");
  rec("S9.4", "净白主题：退出控件是晶体玻璃", isLightGlass(closeBtn), JSON.stringify(closeBtn));
  await ctx.close();
}

async function main() {
  try {
    await scenario1Chat();
    await scenario2PlanetOpenClose();
    await scenario3PlanetBrowse();
    await scenario4KnowledgeEntity();
    await scenario5Approval();
    await scenario6ToolCreation();
    await scenario7ReducedMotion();
    await scenario8SystemReducedMotion();
    await scenario9LightGlassLayers();
  } catch (e) {
    report.fatal = String(e && e.stack ? e.stack : e);
    console.log(`[FATAL] ${report.fatal}`);
  }
  report.consoleErrors = consoleErrors.slice(0, 20);
  report.nativeDialogs = dialogs;
  report.finishedAt = new Date().toISOString();
  writeFileSync(`${OUT}\\report-phase4.json`, JSON.stringify(report, null, 2), "utf8");
  const passed = report.cases.filter((c) => c.passed === true).length;
  const failed = report.cases.filter((c) => c.passed === false).length;
  const skipped = report.cases.filter((c) => c.passed === null).length;
  console.log(`\n报告：${OUT}\\report-phase4.json`);
  console.log(`截图：${SHOTS}`);
  console.log(`汇总：PASS ${passed} · FAIL ${failed} · 未运行 ${skipped}`);
  if (consoleErrors.length) console.log(`控制台错误（前 10）：\n${consoleErrors.slice(0, 10).join("\n")}`);
  await browser.close();
}

await main();
