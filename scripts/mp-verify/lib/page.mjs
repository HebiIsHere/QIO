/**
 * 记忆星球页的「打开 → 等待就绪 → 读几何/状态」共用逻辑。
 *
 * 只依赖稳定的 DOM 契约（.planet-view / .planet-stage / .planet-canvas / .panel），
 * 不依赖某个组件的内部实现；诊断字段优先读 usePlanetScene 暴露的**只读** DEV 钩子
 * （window.__qioPlanetScene）。钩子缺了也不至于让脚本废掉：那时退化成 DOM 测量。
 */

export const ENTRY_SELECTORS = ["[data-planet-entry]", ".dock"];

/** 依次试多个选择器，返回第一个真实存在的。 */
export async function firstSelector(browser, candidates) {
  for (const selector of candidates) {
    const rect = await browser.rectOf(selector);
    if (rect && rect.width > 0 && rect.height > 0) return selector;
  }
  return null;
}

export async function openPlanetFromDock(browser, { timeout = 20000 } = {}) {
  const entry = await firstSelector(browser, ENTRY_SELECTORS);
  if (!entry) throw new Error("找不到星球入口（试过 " + ENTRY_SELECTORS.join(" / ") + "）");
  await browser.clickSelector(entry);
  await browser.waitFor("!!document.querySelector('.planet-view')", { timeout });
  return entry;
}

/**
 * 等星球场景真正就绪：视图在、画布有尺寸、Dev 钩子报告 WebGL 可用、
 * 并且**连续两次采样的球体几何一致**（转场结束）。
 */
export async function waitPlanetReady(browser, { timeout = 45000 } = {}) {
  const deadline = Date.now() + timeout;
  let stable = 0;
  let last = null;
  while (Date.now() < deadline) {
    const state = await browser.evalJs(`(() => {
      const view = document.querySelector('.planet-view');
      const canvas = document.querySelector('.planet-canvas');
      if (!view || !canvas) return { ready: false, reason: '视图未挂载' };
      if (canvas.offsetWidth < 20 || canvas.offsetHeight < 20) return { ready: false, reason: '画布无尺寸' };
      const scene = (typeof window.__qioPlanetScene === 'function') ? window.__qioPlanetScene() : null;
      let sphere = null;
      if (typeof window.__qioPlanetStage === 'function') {
        const stage = window.__qioPlanetStage();
        sphere = stage && stage.sphere ? stage.sphere : null;
      }
      const klass = view.className || '';
      // .planet-view 在球态也常驻（对话页入口那颗小球是同一个场景缩到 108px）：
      // 只等它出现会把「球态」当成「展开完成」，量到的是 48px 半径的小球。
      const opened = /(^|\\s)entered(\\s|$)/.test(klass) && !/(^|\\s)ball(\\s|$)/.test(klass);
      if (!opened) return { ready: false, reason: '仍在球态（入口小球）', className: klass };
      if (canvas.offsetWidth < 200) return { ready: false, reason: '画布仍是球态尺寸', className: klass };
      const settled = !/settling|closing/.test(klass);
      return {
        ready: Boolean(settled && (!scene || scene.webglOK !== false)),
        webglOK: scene ? scene.webglOK : null,
        className: klass,
        sphere,
        reason: settled ? '' : '仍在转场',
      };
    })()`);
    if (state.ready) {
      const key = JSON.stringify(state.sphere ?? null);
      if (last !== null && key === last) stable += 1;
      else stable = 0;
      last = key;
      if (stable >= 1) return state;
    } else {
      stable = 0;
    }
    await new Promise((r) => setTimeout(r, 180));
  }
  throw new Error("星球场景在 " + timeout + "ms 内没有就绪");
}

/**
 * 页面一次性度量（几何 + 状态 + 横向滚动判据）。
 * 横向滚动判据按契约：任何层级 scrollWidth <= clientWidth + 1。
 */
export async function collectMetrics(browser) {
  return browser.evalJs(`(() => {
    const rectOf = (el) => {
      if (!el) return null;
      const r = el.getBoundingClientRect();
      return { x: +r.x.toFixed(2), y: +r.y.toFixed(2), width: +r.width.toFixed(2), height: +r.height.toFixed(2),
               left: +r.left.toFixed(2), top: +r.top.toFixed(2), right: +r.right.toFixed(2), bottom: +r.bottom.toFixed(2) };
    };
    const view = document.querySelector('.planet-view');
    const content = document.querySelector('.planet-content') || view;
    const stage = document.querySelector('.planet-stage');
    const canvas = document.querySelector('.planet-canvas');
    const panel = document.querySelector('aside.panel') || document.querySelector('.panel');
    const root = document.documentElement;
    const style = view ? getComputedStyle(view) : null;

    const overflow = [];
    for (const el of document.querySelectorAll('body *')) {
      if (el.scrollWidth > el.clientWidth + 1 && el.clientWidth > 0) {
        const cs = getComputedStyle(el);
        if (cs.overflowX === 'auto' || cs.overflowX === 'scroll') {
          const r = el.getBoundingClientRect();
          if (r.width > 0 && r.height > 0) {
            overflow.push({ tag: el.tagName.toLowerCase(), cls: (el.className || '').toString().slice(0, 60),
                            scrollWidth: el.scrollWidth, clientWidth: el.clientWidth });
          }
        }
      }
    }

    const viewRect = rectOf(view);
    const contentRect = rectOf(content);
    const canvasRect = rectOf(canvas);
    const sphere = canvasRect && canvasRect.width > 0
      ? { cx: +(canvasRect.left + canvasRect.width / 2).toFixed(2), cy: +(canvasRect.top + canvasRect.height / 2).toFixed(2),
          canvasW: canvasRect.width, canvasH: canvasRect.height }
      : null;

    let sceneDebug = null;
    try {
      if (typeof window.__qioPlanetScene === 'function') sceneDebug = window.__qioPlanetScene();
    } catch (e) { sceneDebug = { error: String(e && e.message || e) }; }
    // PlanetView 自己的连续体诊断（A 的文件，DEV 构建才有）：sphere 是**布局值**半径，
    // 不含 CSS 变换；管理态的屏幕位置一律用 canvas.getBoundingClientRect() 量。
    let stageHook = null;
    try {
      if (typeof window.__qioPlanetStage === 'function') stageHook = window.__qioPlanetStage();
    } catch (e) { stageHook = { error: String(e && e.message || e) }; }

    return {
      viewport: { width: window.innerWidth, height: window.innerHeight, dpr: window.devicePixelRatio },
      titlebar: parseFloat(getComputedStyle(root).getPropertyValue('--titlebar-height')) || 0,
      radiusToken: getComputedStyle(root).getPropertyValue('--qio-ui-radius').trim(),
      view: viewRect,
      content: contentRect,
      stage: rectOf(stage),
      canvas: canvasRect,
      panel: rectOf(panel),
      sphereFromCanvas: sphere,
      classes: view ? String(view.className) : null,
      mp: style ? style.getPropertyValue('--mp').trim() : null,
      dataMp: view ? view.getAttribute('data-mp') : null,
      managing: Boolean(view && view.classList.contains('managing')),
      docScroll: { scrollWidth: root.scrollWidth, clientWidth: root.clientWidth,
                   horizontalOverflow: root.scrollWidth > root.clientWidth + 1 },
      bodyScroll: { scrollWidth: document.body.scrollWidth, clientWidth: document.body.clientWidth },
      overflowScrollers: overflow,
      scene: sceneDebug,
      stageHook,
    };
  })()`);
}

/** 管理区开合：点真实按钮（优先 data 属性，退化到文字/aria），点不到就如实报错。 */
export async function toggleManagement(browser, action, selectors) {
  const candidates = action === "open" ? selectors.open : selectors.close;
  const selector = await firstSelector(browser, candidates);
  if (!selector) {
    throw new Error(
      "找不到管理区" + (action === "open" ? "展开" : "收起") + "按钮（试过 " + candidates.join(" / ") + "）",
    );
  }
  await browser.clickSelector(selector);
  return selector;
}

export const DEFAULT_MANAGE_SELECTORS = {
  open: ["[data-mp-open]", "[data-manage-open]", '[aria-label*="管理"]', ".page-tools .manage", ".manage-btn"],
  close: ["[data-mp-close]", "[data-manage-close]", '[aria-label*="收起管理"]', '[aria-label*="关闭管理"]', ".panel .close"],
};
/**
 * 等场景「静下来」：朝向与渲染帧计数在连续两次采样之间都不再变化。
 *
 * 为什么必须等：星球空闲时可能还在缓慢自转（入口小球的「微微旋转」），
 * 朝向每帧都在变；那时截的图与另一次截图不可能逐像素相同，
 * 「基线 vs 改版」的对比就成了自欺欺人。这个函数把「静」变成可测量的条件。
 */
export async function waitSceneStatic(browser, { timeout = 8000, interval = 260, needStable = 2 } = {}) {
  const deadline = Date.now() + timeout;
  let last = null;
  let stable = 0;
  const samples = [];
  while (Date.now() < deadline) {
    const probe = await browser.evalJs(`(() => {
      const scene = (typeof window.__qioPlanetScene === 'function') ? window.__qioPlanetScene() : null;
      const view = document.querySelector('.planet-view');
      const stage = document.querySelector('.planet-stage');
      const canvas = document.querySelector('.planet-canvas');
      const style = view ? getComputedStyle(view) : null;
      const rectOf = (el) => { if (!el) return null; const r = el.getBoundingClientRect(); return [Math.round(r.left), Math.round(r.top), Math.round(r.width), Math.round(r.height)].join(','); };
      return {
        quaternion: scene && scene.quaternion ? scene.quaternion : null,
        renders: scene && typeof scene.renders === 'number' ? scene.renders : null,
        mp: style ? style.getPropertyValue('--mp') ? style.getPropertyValue('--mp').trim() : null : null,
        // 布局/变换也要静下来：入场与展开的 CSS 过渡没跑完时，舞台矩形每帧都在变。
        // 基线没有 DEV 钩子，只能靠这三个几何值判断「静」。
        stage: rectOf(stage),
        canvas: rectOf(canvas),
        canvasTransform: canvas ? getComputedStyle(canvas).transform : null,
        now: Math.round(performance.now()),
      };
    })()`);
    samples.push(probe);
    const key = JSON.stringify([probe.quaternion, probe.renders, probe.mp, probe.stage, probe.canvas, probe.canvasTransform]);
    if (last !== null && key === last) stable += 1;
    else stable = 0;
    last = key;
    if (stable >= needStable) return { stable: true, samples };
    await new Promise((r) => setTimeout(r, interval));
  }
  return { stable: false, samples };
}

/** 读渲染计数（没有 DEV 钩子时退回 CDP 的 Frames 指标）。 */
export async function readRenderCounter(browser) {
  const scene = await browser.evalJs(
    "(() => { const s = (typeof window.__qioPlanetScene === 'function') ? window.__qioPlanetScene() : null; return s ? { renders: s.renders ?? null, loopActive: s.loopActive ?? null, paused: s.paused ?? null } : null; })()",
  );
  if (scene && typeof scene.renders === "number") return { source: "scene", ...scene, frames: scene.renders };
  const metrics = await browser.metrics();
  return { source: "cdp", frames: metrics.Frames ?? null, loopActive: null, paused: null };
}