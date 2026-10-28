/**
 * 设置页视觉核查：四种窗口尺寸 × 深/浅主题 × 全场景截图 + 脚本内自检。
 *
 * 为什么都要脚本判：横向滚动、原生控件计数、圆角来源、宽窄结构等价、键盘焦点、
 * 减少动画这些「看起来正常」的东西，肉眼在 96 张截图里是看不出来的，
 * 必须逐容器算 scrollWidth、逐元素算 computed border-radius。
 *
 * 产物：
 * - docs/verification-shots-settings/settings-<主题>-<WxH>-<场景>.png
 * - docs/verification-shots-settings/report.json（每条检查 pass/fail + 证据）
 * - docs/verification-shots-settings/report.md（人读摘要）
 *
 * 用法：
 *   node scripts/settings-verify/visual.mjs --url http://127.0.0.1:5106 \
 *        --out docs/verification-shots-settings --report-label baseline
 *   可选：--viewports 1440x900,480x600   --themes dark,light   --quick
 *         --allow-fail（基线负对照用：报告照写、退出码仍为 0）
 */
import { mkdirSync, writeFileSync } from "node:fs";
import { join } from "node:path";
import { VerifyBrowser } from "./lib/browser.mjs";
import {
  openSettings,
  probeState,
  clickCategory,
  clickNavByText,
  categoryReport,
  sampleAnimations,
  waitModelsPanel,
} from "./lib/probe.mjs";
import { installFixtures } from "./lib/fixtures.mjs";
import { DEFAULT_VIEWPORTS, SETTINGS_CATEGORIES, SHOTS_DIR, EVIDENCE_DIR, stamp } from "./lib/env.mjs";

const args = process.argv.slice(2);
const flag = (name, fallback) => {
  const index = args.indexOf("--" + name);
  if (index < 0) return fallback;
  const value = args[index + 1];
  return value === undefined || value.startsWith("--") ? true : value;
};
const sleep = (ms) => new Promise((done) => setTimeout(done, ms));

const appUrl = String(flag("url", "http://127.0.0.1:5106")).replace(/\/$/, "");
const cdpPort = Number(flag("cdp-port", 9806));
const apiBase = String(flag("api", process.env.QIO_SETTINGS_API || "http://127.0.0.1:8756")).replace(/\/$/, "");
const label = String(flag("report-label", "run-" + stamp()));
const allowFail = !!flag("allow-fail", false);
const quick = !!flag("quick", false);
const outDir = String(flag("out", SHOTS_DIR));
const parseList = (value, fallback) => String(value ?? "").split(",").map((item) => item.trim()).filter(Boolean).length ? String(value).split(",").map((item) => item.trim()).filter(Boolean) : fallback;
const viewportList = parseList(flag("viewports", ""), quick ? ["1440x900"] : DEFAULT_VIEWPORTS.map((vp) => vp.width + "x" + vp.height)).map((text) => {
  const [width, height] = text.split("x").map(Number);
  return { width, height };
});
const themeList = parseList(flag("themes", ""), quick ? ["dark"] : ["dark", "light"]);
const advancedCategories = ["regular", "memory", "models"];

mkdirSync(outDir, { recursive: true });
mkdirSync(EVIDENCE_DIR, { recursive: true });

const run = {
  label,
  startedAt: new Date().toISOString(),
  appUrl,
  viewports: viewportList,
  themes: themeList,
  checks: [],
  skipped: [],
  screenshots: [],
  perViewport: {},
  notes: [],
};

function record(scope, name, ok, detail) {
  run.checks.push({ scope, name, ok: !!ok, detail: detail === undefined ? null : detail });
  if (!ok) console.log("  [FAIL] " + scope + " / " + name + " :: " + JSON.stringify(detail).slice(0, 300));
  return ok;
}

function skip(scope, name, reason) {
  run.skipped.push({ scope, name, reason });
  console.log("  [SKIP] " + scope + " / " + name + " :: " + reason);
}

async function shot(browser, name) {
  const file = join(outDir, name + ".png");
  await browser.shotFile(file, null, true);
  run.screenshots.push(file);
  return file;
}

/** 依次尝试几个「打开」按钮文案，返回第一个命中的。 */
async function clickFirstLabel(browser, labels) {
  for (const text of labels) {
    const result = await browser.evalJs("window.__stg.clickLabel(" + JSON.stringify(text) + ")");
    if (result && result.ok) return result;
  }
  return { ok: false, tried: labels };
}

async function findConfirm(browser) {
  const attempts = [];
  const opens = ["管理", "更多操作", "详情", "编辑", "概览"];
  for (const text of opens) {
    const opened = await browser.evalJs("window.__stg.clickLabel(" + JSON.stringify(text) + ")");
    attempts.push({ step: text, ok: !!opened?.ok });
    if (opened?.ok) await sleep(260);
  }
  const dangerous = ["删除", "撤销", "清除"];
  for (const text of dangerous) {
    const clicked = await browser.evalJs("window.__stg.clickLabel(" + JSON.stringify(text) + ")");
    attempts.push({ step: text, ok: !!clicked?.ok });
    if (!clicked?.ok) continue;
    await sleep(420);
    const visible = await browser.evalJs("!!document.querySelector('.qio-confirm--layer, .qio-confirm-scrim, [role=dialog][aria-modal=true]')");
    if (visible) return { ok: true, action: text, attempts };
  }
  return { ok: false, attempts };
}

async function runKeyboard(browser, scope) {
  // 从 body 开始按 Tab：真实键盘事件，才能拿到 :focus-visible 的焦点指示。
  await browser.evalJs("(() => { document.activeElement && document.activeElement.blur && document.activeElement.blur(); document.body.focus(); return true; })()");
  const sequence = [];
  for (let i = 0; i < 34; i += 1) {
    await browser.pressKey("Tab");
    await sleep(60);
    const info = await browser.evalJs("window.__stg.focusInfo()");
    if (info?.has) sequence.push(info);
  }
  const distinct = [];
  const seen = new Set();
  for (const item of sequence) {
    const key = JSON.stringify(item.el);
    if (seen.has(key)) continue;
    seen.add(key);
    distinct.push(item);
  }
  const missingIndicator = distinct.filter((item) => !item.indicator);
  const inRoot = await browser.evalJs("(() => { const r = window.__stg.root(); return r ? true : false; })()");
  const navReached = distinct.some((item) => (item.el.role === "tab") || /tab/.test(item.el.cls) || /(常规|模型|记忆|工具|关于)/.test(item.el.text));
  const inputReached = distinct.some((item) => ["input", "textarea", "select"].includes(item.el.tag) || item.el.role === "switch" || item.el.tag === "button");
  record(scope, "keyboard-reaches-controls", distinct.length >= 8 && navReached && inputReached, { distinct: distinct.length, navReached, inputReached, missingIndicator: missingIndicator.slice(0, 5) });
  record(scope, "keyboard-focus-visible", missingIndicator.length === 0, { missing: missingIndicator.length, total: distinct.length, samples: missingIndicator.slice(0, 5) });
  return { sequence: sequence.length, distinct: distinct.length, missingIndicator: missingIndicator.length };
}

async function runReducedMotion(browser, vp, theme) {
  const scope = "reduced-motion-" + theme + "-" + vp.width + "x" + vp.height;
  await browser.setViewport(vp.width, vp.height);
  await browser.setMotion("reduced");
  await openSettings(browser, appUrl);
  const motionAttr = await browser.evalJs("document.documentElement.getAttribute('data-motion')");
  record(scope, "motion-attr-reduced", motionAttr === "reduced", motionAttr);
  const violations = [];
  const actions = [
    async () => clickCategory(browser, SETTINGS_CATEGORIES[1]),
    async () => browser.evalJs("window.__stg.expandAdvanced()"),
    async () => browser.evalJs("window.__stg.openSearch()"),
    async () => clickCategory(browser, SETTINGS_CATEGORIES[0]),
  ];
  for (const action of actions) {
    const started = action();
    const sampled = await sampleAnimations(browser, { rounds: 4, interval: 80 });
    await started;
    violations.push(...sampled);
  }
  record(scope, "reduced-motion-no-displacement-animation", violations.length === 0, violations.slice(0, 6));
  const file = join(outDir, "settings-reduced-motion-" + theme + "-" + vp.width + "x" + vp.height + ".png");
  await browser.shotFile(file, null, true);
  run.screenshots.push(file);
  await browser.setMotion("standard");
  await openSettings(browser, appUrl);
  return violations.length;
}

async function runViewport(browser, vp, theme) {
  const tag = theme + "-" + vp.width + "x" + vp.height;
  const scope = tag;
  console.log("\n=== " + scope + " ===");
  const entry = { viewport: vp, theme, screenshots: 0 };
  run.perViewport[tag] = entry;

  await browser.setViewport(vp.width, vp.height);
  await browser.setStoredPreference("qio-theme", theme);
  await browser.setStoredPreference("qio-motion", "standard");
  await openSettings(browser, appUrl);
  // B 的模型面板挂载时异步读目录，成功后会给「当前生效参数」补一行 tools.effective.usages。
  // 结构签名必须等这份数据落定再采，否则同尺寸两次采样的 key 数量会不同（真实踩过）。
  const factsSelector = "!!document.querySelector('[data-settings-key=\"tools.effective.usages\"]')";
  entry.modelFactsReady = await browser.waitFor(factsSelector, { timeout: 12000 }).catch(() => false);
  if (!entry.modelFactsReady) {
    // 模型面板偶发读取超时：点一次「重新读取」再等，避免把加载态当成结构差异
    await browser.evalJs("(() => { const panel = document.querySelector('[data-testid=models-panel]'); if (!panel) return false; const btn = [...panel.querySelectorAll('button')].find((el) => window.__stg.norm(el.innerText || '') === '重新读取'); if (btn) { btn.click(); return true; } return false; })()");
    entry.modelFactsReady = await browser.waitFor(factsSelector, { timeout: 12000 }).catch(() => false);
  }
  record(scope, "model-facts-published", entry.modelFactsReady, { modelFactsReady: entry.modelFactsReady });

  const state = await probeState(browser);
  entry.signature = state.signatures;
  entry.theme = state.theme;
  record(scope, "settings-root-present", state.rootFound, state.rootRect);
  const modelsPlaceholder = !!state.modelsPlaceholder;
  if (modelsPlaceholder) run.modelsPlaceholder = true;
  record(scope, "theme-applied", state.theme === theme, { expected: theme, actual: state.theme });
  record(scope, "radius-token-is-14px", state.radius.token === "14px", state.radius.token);

  const topBar = state.topBar;
  record(scope, "global-bar-single-and-fixed", topBar.chromeCount === 1 && topBar.chromeInsideRoot === 0 && topBar.chrome?.position === "fixed", topBar);
  record(scope, "global-bar-not-replaced", topBar.fixedInside.length === 0, topBar.fixedInside);
  record(scope, "global-bar-no-overlap", topBar.overlap === false, { rootTop: topBar.root?.top, chromeBottom: topBar.chrome?.rect?.bottom });

  const categoryInfo = await categoryReport(browser);
  record(scope, "five-categories", categoryInfo.missing.length === 0 && categoryInfo.count === 5 && categoryInfo.unexpected.length === 0, categoryInfo);

  const agg = { hscroll: [], native: [], radius: [], radiusEdge: [], duplicates: [], nav: [], advancedMissing: [], catReachable: [] };
  for (const category of SETTINGS_CATEGORIES) {
    const clicked = await clickCategory(browser, category);
    if (!clicked?.ok) {
      record(scope, "category-" + category.id + "-reachable", false, clicked);
      agg.catReachable.push(false);
      continue;
    }
    agg.catReachable.push(true);
    // 模型与服务面板要等目录接口的 fixture 返回后才有内容，多等一会儿再判
    await sleep(category.id === "models" ? 400 : 280);
    if (category.id === "models" && !modelsPlaceholder) {
      entry.modelsPanelWait = await waitModelsPanel(browser);
    }
    const st = await probeState(browser);
    if (category.id === "models" && !modelsPlaceholder) {
      const modelsInfo = await browser.evalJs("(() => { const panel = document.querySelector('[data-testid=models-panel]'); const visible = panel ? window.__stg.visible(panel) : false; const text = panel ? window.__stg.norm(panel.innerText || '') : ''; const usageNames = panel ? [...panel.querySelectorAll('.row__name')].map((el) => window.__stg.norm(el.innerText || el.textContent || '')) : []; return { visible, textLength: text.length, hasKeyAccess: text.includes('Key接入与管理'), hasDefault: text.includes('默认模型'), hasUsage: text.includes('用途分配'), usageNames: usageNames.slice(0, 20), usageCount: usageNames.length }; })()");
      record(scope, "models-panel-content", !!(modelsInfo.visible && modelsInfo.hasKeyAccess && modelsInfo.hasDefault && modelsInfo.hasUsage && modelsInfo.usageCount >= 8), modelsInfo);
      entry.modelsPanel = modelsInfo;
    }
    agg.hscroll.push(...st.hscroll.hard.map((item) => ({ cat: category.id, ...item })), ...st.hscroll.designed.map((item) => ({ cat: category.id, designed: true, ...item })));
    agg.native.push(...st.nativeControls.samples.map((item) => ({ cat: category.id, ...item })));
    agg.radius.push(...(st.radius.audit?.violations ?? []).map((item) => ({ cat: category.id, ...item })));
    agg.radiusEdge.push(...(st.radius.audit?.edgeSamples ?? []).map((item) => ({ cat: category.id, ...item })));
    const duplicates = await browser.evalJs("window.__stg.duplicateTexts()");
    agg.duplicates.push(...(duplicates ?? []).map((item) => ({ cat: category.id, ...item })));
    agg.nav.push(st.nav.layoutAttr || st.nav.mode);
    await shot(browser, "settings-" + tag + "-cat-" + category.id);
    entry.screenshots += 1;

    if (advancedCategories.includes(category.id)) {
      const advanced = await browser.evalJs("window.__stg.expandAdvanced()");
      if (advanced.opened > 0) {
        await sleep(260);
        await shot(browser, "settings-" + tag + "-cat-" + category.id + "-advanced");
        entry.screenshots += 1;
        record(scope, "advanced-" + category.id + "-expanded", true, { opened: advanced.opened });
      } else {
        agg.advancedMissing.push(category.id);
        record(scope, "advanced-" + category.id + "-expanded", false, advanced);
      }
    }
  }

  record(scope, "no-horizontal-scroll", agg.hscroll.length === 0, agg.hscroll.slice(0, 8));
  record(scope, "no-native-select-number-checkbox", agg.native.length === 0, agg.native.slice(0, 8));
  entry.radiusViolations = agg.radius;
  entry.radiusEdgeAttached = agg.radiusEdge;
  record(scope, "radius-uniform-token", agg.radius.length === 0, agg.radius.slice(0, 8));
  // 贴边形状（一侧直角 + 其余角 14px）单列：这是导航标签贴边的既定形状，需产品裁定，不混进「非令牌半径」
  record(scope, "radius-edge-attached-reported", agg.radiusEdge.length === 0, agg.radiusEdge.slice(0, 6));
  const expectedNav = vp.width >= 1024 ? "side" : "top";
  const navModes = [...new Set(agg.nav)];
  record(scope, "nav-layout-" + expectedNav, navModes.length === 1 && navModes[0] === expectedNav, { expected: expectedNav, modes: navModes });

  // 单一纵向滚动视口 + 全局栏不被自绘标题栏替代（A 声明 2）
  const scrollState = await browser.evalJs("window.__stg.scrollAudit()");
  entry.scrollAudit = scrollState;
  const scrollOk = !!scrollState
    && scrollState.nested.length === 0
    && scrollState.document.horizontalOk
    && (!scrollState.main || /qio-settings|\.settings/.test((scrollState.main.el && scrollState.main.el.cls) || ""))
    && (scrollState.main !== null || scrollState.rootOverflowY === "auto" || scrollState.rootOverflowY === "scroll");
  record(scope, "single-vertical-scroll", scrollOk, scrollState);
  record(scope, "no-duplicate-heading-desc", agg.duplicates.length === 0, agg.duplicates.slice(0, 8));
  // 原生对话框从未被调用（A 声明 3）
  const dialogs = await browser.evalJs("window.__stg.dialogCalls()");
  const dialogCount = dialogs ? dialogs.confirm + dialogs.alert + dialogs.prompt : 0;
  record(scope, "no-native-dialogs", dialogCount === 0, dialogs);

  // 搜索：面板 + 命中 + 定位
  await clickCategory(browser, SETTINGS_CATEGORIES[1]);
  const searchOpened = await browser.evalJs("window.__stg.openSearch()");
  if (!searchOpened?.found) {
    record(scope, "search-panel", false, searchOpened);
  } else {
    await sleep(320);
    await browser.evalJs("window.__stg.search('返回条数')");
    await sleep(480);
    const results = await browser.evalJs("window.__stg.searchResults()");
    await shot(browser, "settings-" + tag + "-search-results");
    entry.screenshots += 1;
    record(scope, "search-results", Array.isArray(results) && results.length > 0, { count: results?.length ?? 0, sample: results?.slice(0, 3) });
    if (results?.length) {
      const before = await browser.evalJs("window.__stg.activeCategory()");
      const clickedResult = await browser.evalJs("window.__stg.clickSearchResult(0)");
      await sleep(700);
      const after = await browser.evalJs("window.__stg.activeCategory()");
      const highlighted = await browser.evalJs("window.__stg.highlighted()");
      await shot(browser, "settings-" + tag + "-search-hit");
      entry.screenshots += 1;
      record(scope, "search-locate-highlight", Array.isArray(highlighted) && highlighted.length > 0, { before, after, highlighted, clicked: clickedResult });
    }
  }

  // 长表单：Key 接入（打开表单即可，不提交、不发网络请求）
  if (modelsPlaceholder) {
    skip(scope, "long-form-opens", "模型与服务里的 Key 面板由 B 提供，当前分支是占位（data-testid=models-panel-placeholder），不判红");
  } else {
  await clickCategory(browser, SETTINGS_CATEGORIES[1]);
  const formBefore = await browser.evalJs("window.__stg.root().querySelectorAll('input, textarea').length");
  let openedForm = await clickFirstLabel(browser, ["添加 Key", "新建 Key", "新建凭据", "添加连接", "接入新的服务商", "新建连接", "连接新的服务商", "新建"]);
  if (!openedForm?.ok) {
    // 基线把 Key 管理放在独立的「凭据」分类里；新方案在「模型与服务」内。两种都要能走到。
    const navFallback = await clickNavByText(browser, "凭据");
    if (navFallback?.ok) {
      await sleep(360);
      openedForm = await clickFirstLabel(browser, ["新建 Key", "新建凭据", "添加 Key", "接入新的服务商", "新建连接", "新建"]);
    }
  }
  await sleep(620);
  const formAfter = await browser.evalJs("window.__stg.root().querySelectorAll('input, textarea').length");
  const modalVisible = await browser.evalJs("!!document.querySelector('.modal-mask .modal, [role=dialog][aria-modal=true], .qio-modal, .qio-modal--layer')");
  await shot(browser, "settings-" + tag + "-longform-key");
  entry.screenshots += 1;
  record(scope, "long-form-opens", !!(openedForm?.ok && (modalVisible || formAfter > formBefore)), { openedForm, formBefore, formAfter, modalVisible });
  await browser.pressKey("Escape");
  await sleep(300);
  const stillModal = await browser.evalJs("!!document.querySelector('.modal-mask .modal')");
  if (stillModal) {
    await browser.evalJs("(() => { const close = document.querySelector('.modal .close, [data-qio-modal-close], .qio-modal__close'); if (close) close.click(); return !!close; })()");
    await sleep(300);
  }
  }

  // 中央确认弹窗：危险动作（fixture 的凭据卡提供入口）
  if (modelsPlaceholder) {
    skip(scope, "confirm-layer", "Key 管理面板由 B 提供，当前是占位；危险操作确认层在集成后才有入口，不判红");
  } else {
  await clickCategory(browser, SETTINGS_CATEGORIES[1]);
  let confirm = await findConfirm(browser);
  if (!confirm.ok) {
    // 基线：管理动作在「凭据」分类的卡片里；新方案在「模型与服务 → Key 详情」。
    const navFallback = await clickNavByText(browser, "凭据");
    if (navFallback?.ok) {
      await sleep(400);
      confirm = await findConfirm(browser);
    }
  }
  if (confirm.ok) {
    await sleep(260);
    await shot(browser, "settings-" + tag + "-confirm");
    entry.screenshots += 1;
    record(scope, "confirm-layer", true, { action: confirm.action });
    await browser.pressKey("Escape");
    await sleep(320);
  } else {
    record(scope, "confirm-layer", false, confirm);
  }
  }

  // 长文本：关于分类里展开许可 / 第三方声明
  const aboutClicked = await clickCategory(browser, SETTINGS_CATEGORIES[4]);
  if (aboutClicked?.ok) {
    const expanded = await browser.evalJs("(() => { let n = 0; const root = window.__stg.root(); for (const el of [...root.querySelectorAll('[data-settings-fold-toggle], summary')]) { if (!window.__stg.visible(el)) continue; if (el.getAttribute('aria-expanded') === 'true') continue; if (el.parentElement && el.parentElement.tagName === 'DETAILS' && el.parentElement.open) continue; el.click(); n += 1; } return n; })()");
    await sleep(700);
    const textLength = await browser.evalJs("(window.__stg.pageText() || '').length");
    await shot(browser, "settings-" + tag + "-longtext");
    entry.screenshots += 1;
    record(scope, "long-text-readable", textLength > 4000, { expanded, textLength });
    // 长文本把内容撑高后再审一次：仍然只有一个纵向滚动视口，且就是设置根
    const scrollUnderOverflow = await browser.evalJs("window.__stg.scrollAudit()");
    entry.scrollAuditOverflow = scrollUnderOverflow;
    record(scope, "single-vertical-scroll-under-overflow", !!scrollUnderOverflow?.main && scrollUnderOverflow.nested.length === 0 && scrollUnderOverflow.document.horizontalOk, scrollUnderOverflow);
  }

  // 运行期错误：只看真正的 JS 异常与失败请求。
  // 已知「等待其它子智能体交付」的端点单列（例如 C 的搜索状态接口），不算本页缺陷也不静默吞掉。
  const knownMissingEndpoints = [/\/api\/settings\/search\/status/];
  const consoleErrors = (browser.consoleErrors ?? []).filter(Boolean);
  const httpFails = (browser.httpFails ?? []).filter((item) => !/favicon/i.test(item));
  const benignHttp = httpFails.filter((item) => knownMissingEndpoints.some((pattern) => pattern.test(item)));
  // 导航/窗口尺寸切换会让在飞的请求被浏览器取消（ERR_ABORTED）：这是采集行为，不是页面缺陷，单列出来
  const aborted = httpFails.filter((item) => /ERR_ABORTED/.test(item));
  const realHttpFails = httpFails.filter((item) => !knownMissingEndpoints.some((pattern) => pattern.test(item)) && !/ERR_ABORTED/.test(item));
  const realConsoleErrors = consoleErrors.filter((text) => !(/Failed to load resource/.test(text) && realHttpFails.length === 0));
  record(scope, "no-runtime-errors", realConsoleErrors.length === 0 && realHttpFails.length === 0, { consoleErrors: realConsoleErrors.slice(0, 6), httpFails: realHttpFails.slice(0, 6) });
  entry.httpFails = httpFails.slice(0, 10);
  entry.pendingEndpoints = benignHttp.slice(0, 6);
  entry.abortedRequests = aborted.slice(0, 8);
  entry.consoleErrorDetails = (browser.consoleErrorDetails ?? []).slice(0, 12);
  browser.consoleErrors.length = 0;
  browser.consoleErrorDetails.length = 0;
  browser.httpFails.length = 0;

  // 键盘可达 + 减少动画（只在代表性尺寸做，避免 8 组重复拖长采集时间）
  if ((vp.width === 1440 && theme === "dark") || (vp.width === 480 && theme === "dark")) {
    entry.keyboard = await runKeyboard(browser, scope);
    await openSettings(browser, appUrl);
  }
  if ((vp.width === 1440 && theme === "dark") || (vp.width === 480 && theme === "dark")) {
    entry.reducedMotionViolations = await runReducedMotion(browser, vp, theme);
  }
  return entry;
}

const browser = new VerifyBrowser({ port: cdpPort, width: 1440, height: 900, apiBase });
let exitCode = 0;
try {
  await browser.launch();
  await browser.installDialogGuards();
  await browser.navigate(appUrl + "/", 2400);
  const fixtures = await installFixtures(browser);
  run.fixtures = fixtures.hits;
  for (const theme of themeList) {
    for (const vp of viewportList) {
      await runViewport(browser, vp, theme);
    }
  }
  // 宽窄结构等价：同主题下四种尺寸的签名（含顺序）必须完全一致
  for (const theme of themeList) {
    const entries = viewportList.map((vp) => ({ vp, sig: run.perViewport[theme + "-" + vp.width + "x" + vp.height]?.signature })).filter((item) => item.sig);
    if (entries.length < 2) continue;
    const base = entries[0];
    for (const item of entries.slice(1)) {
      const same = JSON.stringify(item.sig.items) === JSON.stringify(base.sig.items);
      record("structure", "same-dom-order-" + theme + "-" + item.vp.width + "x" + item.vp.height + "-vs-" + base.vp.width + "x" + base.vp.height, same, same ? { count: item.sig.items.length } : {
        baseCount: base.sig.items.length,
        otherCount: item.sig.items.length,
        firstDiffIndex: (() => { const n = Math.min(base.sig.items.length, item.sig.items.length); for (let i = 0; i < n; i += 1) if (base.sig.items[i] !== item.sig.items[i]) return { i, base: base.sig.items[i], other: item.sig.items[i] }; return { i: n }; })(),
      });
    }
    const sources = [...new Set(entries.map((item) => item.sig.source))];
    record("structure", "structure-hook-present-" + theme, sources.length === 1 && sources[0] === "data-settings-key", { sources });
  }
} catch (error) {
  run.error = String(error && error.stack ? error.stack : error);
  console.error("视觉核查中断：" + run.error);
  exitCode = 1;
} finally {
  run.finishedAt = new Date().toISOString();
  run.summary = {
    total: run.checks.length,
    passed: run.checks.filter((item) => item.ok).length,
    failed: run.checks.filter((item) => !item.ok).length,
    skipped: run.skipped.length,
    screenshots: run.screenshots.length,
  };
  const reportPath = join(outDir, "report.json");
  writeFileSync(reportPath, JSON.stringify(run, null, 2), "utf8");
  writeFileSync(join(EVIDENCE_DIR, "visual-" + label + ".json"), JSON.stringify(run, null, 2), "utf8");
  const lines = [
    "# 设置页视觉核查报告（" + label + "）",
    "",
    "- 应用：" + appUrl,
    "- 尺寸：" + viewportList.map((vp) => vp.width + "x" + vp.height).join(" / "),
    "- 主题：" + themeList.join(" / "),
    "- 截图：" + run.screenshots.length + " 张（同目录）",
    "- 检查：" + run.summary.total + " 条，通过 " + run.summary.passed + "，失败 " + run.summary.failed,
    run.error ? "- 中断：" + run.error.split("\n")[0] : "",
    "",
    "| 范围 | 检查 | 结果 | 证据摘要 |",
    "| --- | --- | --- | --- |",
    ...run.checks.map((item) => "| " + item.scope + " | " + item.name + " | " + (item.ok ? "通过" : "**失败**") + " | " + JSON.stringify(item.detail).replace(/\|/g, "\\|").slice(0, 220) + " |"),
    "",
    ...(run.skipped.length ? ["## 跳过（占位 / 依赖未交付，不判红也不计入通过）", "", "| 范围 | 检查 | 原因 |", "| --- | --- | --- |", ...run.skipped.map((item) => "| " + item.scope + " | " + item.name + " | " + item.reason + " |"), ""] : []),
  ];
  writeFileSync(join(outDir, "report.md"), lines.join("\n"), "utf8");
  if (run.summary.failed > 0 && !allowFail) exitCode = 1;
  await browser.close();
  console.log("\n截图 " + run.screenshots.length + " 张 → " + outDir);
  console.log("检查 " + run.summary.total + " 条：通过 " + run.summary.passed + " / 失败 " + run.summary.failed + " / 跳过 " + run.summary.skipped);
  console.log("报告：" + reportPath);
}
process.exit(exitCode);
