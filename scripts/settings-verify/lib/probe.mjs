/**
 * 探针的 Node 侧封装：装/用 window.__stg，导航与「一次采集全部事实」。
 * 上层脚本只消费 JSON，不在各处散落选择器。
 */
import { PAGE_HELPERS } from "./page-helpers.mjs";
import { SETTINGS_CATEGORIES } from "./env.mjs";

const sleep = (ms) => new Promise((done) => setTimeout(done, ms));

export async function installHelpers(browser) {
  await browser.evalJs(PAGE_HELPERS);
  const ok = await browser.evalJs("!!window.__stg && typeof window.__stg.categories === 'function'");
  if (!ok) throw new Error("页面探针安装失败：window.__stg 不存在");
}

/** 打开设置页：清 URL 缓存参数 → 等设置根出现 → 等一帧网络稳定。 */
export async function openSettings(browser, appUrl, { wait = 900 } = {}) {
  const base = String(appUrl).replace(/\/$/, "");
  await browser.navigate(base + "/?verify=" + Date.now() + "#/settings", 2400);
  await browser.waitFor("!!document.querySelector('[data-settings-page], .qio-settings, [data-settings-root], .settings')", { timeout: 30000 });
  await installHelpers(browser);
  await sleep(wait);
  // 页面可能还在拉设置数据：等到不再有 pending 的渲染抖动（连续两次签名一致）
  await waitSignatureStable(browser, { rounds: 3, interval: 220 });
  return true;
}

async function signatureKey(browser) {
  return browser.evalJs("(() => { const s = window.__stg.signature(); return s.source + ':' + s.items.length + ':' + s.items.slice(0, 6).join('|'); })()");
}

export async function waitSignatureStable(browser, { rounds = 3, interval = 220 } = {}) {
  let last = null;
  let same = 0;
  for (let i = 0; i < rounds * 4; i += 1) {
    const current = await signatureKey(browser);
    if (current === last) same += 1;
    else same = 0;
    last = current;
    if (same >= rounds) return true;
    await sleep(interval);
  }
  return false;
}

export async function clickCategory(browser, category) {
  const aliases = [category.label, ...(category.aliases ?? []), category.id];
  const clicked = await browser.evalJs(
    "(() => { const nav = document.querySelector(\"[data-settings-nav], nav[role=tablist], .settings-nav, .settings-tabs, .nav\"); if (!nav) return { ok: false, reason: 'nav-not-found' }; " +
      "const items = [...nav.querySelectorAll('[role=tab], [data-settings-category], button, a')].filter((el) => window.__stg.visible(el)); " +
      "const want = " + JSON.stringify(aliases) + ".map((v) => window.__stg.norm(v)); " +
      "const hit = items.find((el) => { const t = window.__stg.norm(el.innerText || el.textContent || ''); const id = window.__stg.norm(el.getAttribute('data-settings-category') || el.getAttribute('data-category') || ''); return want.some((w) => t === w || t.includes(w) || (id && id === w)); }); " +
      "if (!hit) return { ok: false, reason: 'category-not-found', seen: items.map((el) => window.__stg.norm(el.innerText || el.textContent || '')) }; " +
      "hit.click(); return { ok: true, clicked: window.__stg.norm(hit.innerText || hit.textContent || '') }; })()",
  );
  if (clicked?.ok) await sleep(360);
  return clicked;
}

const PROBE_SOURCE = [
  "(() => ({",
  "  url: location.href,",
  "  theme: document.documentElement.getAttribute('data-theme'),",
  "  motion: document.documentElement.getAttribute('data-motion'),",
  "  viewport: { width: window.innerWidth, height: window.innerHeight },",
  "  rootFound: !!window.__stg.root(),",
  "  rootRect: window.__stg.root() ? (() => { const r = window.__stg.root().getBoundingClientRect(); return { x: r.x, y: r.y, width: r.width, height: r.height }; })() : null,",
  "  categories: window.__stg.categories(),",
  "  activeCategory: window.__stg.activeCategory(),",
  "  nav: window.__stg.navInfo(),",
  "  signatures: window.__stg.signature(),",
  "  panelsVisible: window.__stg.panels().length,",
  "  nativeControls: window.__stg.nativeControls(),",
  "  radius: (() => { const token = getComputedStyle(document.documentElement).getPropertyValue('--qio-ui-radius').trim(); return { token, audit: window.__stg.radiusAudit(parseFloat(token) || 14) }; })(),",
  "  hscroll: window.__stg.horizontalScroll(),",
  "  topBar: window.__stg.topBar(),",
  "  scroll: window.__stg.scrollPositions(),",
  "  navLayoutAttr: (document.querySelector('[data-settings-page]') || {}).getAttribute ? document.querySelector('[data-settings-page]').getAttribute('data-nav-layout') : null,",
  "  scrollAudit: window.__stg.scrollAudit(),",
  "  dialogs: window.__stg.dialogCalls(),",
  "  modelsPlaceholder: !!document.querySelector('[data-testid=models-panel-placeholder]'),",
  "  headings: [...document.querySelectorAll('[data-settings-page] h2, .qio-settings h2, .settings h1, .settings h2, .settings h3')].slice(0, 40).map((el) => window.__stg.norm(el.innerText || el.textContent || '')).filter(Boolean),",
  "  bodyTextSample: (document.body.innerText || '').slice(0, 400),",
  "}))()",
].join("\n");

/** 按导航项文字点分类（用于「凭据」这类新方案里不再独立存在的兼容入口）。 */
export async function clickNavByText(browser, text) {
  return browser.evalJs(
    "(() => { const nav = document.querySelector(\"[data-settings-nav], nav[role=tablist], .settings-nav, .settings-tabs, .nav\"); if (!nav) return { ok: false, reason: 'nav-not-found' }; " +
      "const want = window.__stg.norm(" + JSON.stringify(String(text)) + "); " +
      "const items = [...nav.querySelectorAll('[role=tab], [data-settings-category], button, a')].filter((el) => window.__stg.visible(el)); " +
      "const hit = items.find((el) => window.__stg.norm(el.innerText || el.textContent || '').includes(want)); " +
      "if (!hit) return { ok: false, reason: 'not-found', seen: items.map((el) => window.__stg.norm(el.innerText || el.textContent || '')) }; " +
      "hit.click(); return { ok: true, clicked: window.__stg.norm(hit.innerText || hit.textContent || '') }; })()",
  );
}

/** 一次采集：全部客观事实（截图之外的断言都读这份）。 */
export async function probeState(browser) {
  return browser.evalJs(PROBE_SOURCE);
}

/** 交互动作后采样「运行中的位移/缩放动画」，用于减少动画检查。 */
export async function sampleAnimations(browser, { minDurationMs = 20, rounds = 4, interval = 90 } = {}) {
  const found = [];
  for (let i = 0; i < rounds; i += 1) {
    const batch = await browser.evalJs("window.__stg.animations(" + Number(minDurationMs) + ")");
    for (const item of batch ?? []) found.push(item);
    await sleep(interval);
  }
  const unique = [];
  const seen = new Set();
  for (const item of found) {
    const key = item.name + "|" + JSON.stringify(item.target) + "|" + item.props.join(",");
    if (seen.has(key)) continue;
    seen.add(key);
    unique.push(item);
  }
  return unique;
}

export const KNOWN_CATEGORIES = SETTINGS_CATEGORIES;

/** 与产品标签无关的「分类是否属于本轮五类」判定（用于基线负对照）。 */
export async function categoryReport(browser) {
  const items = (await browser.evalJs("window.__stg.categories()")) ?? [];
  const labels = items.map((item) => item.label);
  const missing = SETTINGS_CATEGORIES.filter((category) => {
    const aliases = [category.label, ...(category.aliases ?? [])].map((value) => value.replace(/\s/g, ""));
    return !labels.some((label) => aliases.some((alias) => label === alias));
  }).map((category) => category.id);
  const unexpected = labels.filter((label) => {
    return !SETTINGS_CATEGORIES.some((category) => [category.label, ...(category.aliases ?? [])].some((alias) => alias === label));
  });
  return { labels, missing, unexpected, count: labels.length };
}
