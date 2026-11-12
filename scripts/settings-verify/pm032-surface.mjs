/**
 * pm032-surface.mjs —— settings-surface 分支（#25 / #70 / #90 + #82/#87/#89 的 CSS 钩子）实测取证脚本。
 *
 * 只读复用 scripts/settings-verify/lib/*；本文件是 surface 组唯一新增脚本。
 *
 * 用法：
 *   node scripts/settings-verify/pm032-surface.mjs \
 *     --url http://127.0.0.1:5109 --api http://127.0.0.1:8759 \
 *     --out D:\qio-dev\_lead-logs\pm032-settings\surface\shots \
 *     --label surf --phase before [--quick] [--cdp-port 9819]
 *
 * 产物：<out>/surface-measure-<phase>.json + surface-<phase>-*.png
 *
 * 实测项（原始数值全部写进 JSON）：
 * 1) #90：五个分区逐一切换时 .set-main / .set-panels / 可见 .set-section 的外框宽度，
 *         以及分区内部阅读宽度（标题区/说明区）的宽度；外框宽度不得随分类跳变。
 * 2) #70：每条可见 .set-row 的「标签文字右边界 → 控件左边界」横向距离；
 *         窄窗下控件是否在标签下方（上下排列）；左右留白是否对称。
 * 3) #82：800x600 确认层几何：遮罩/面板/标题/按钮与顶部全局栏是否重叠。
 * 4) #87：危险确认框（清除）的按钮度量：危险底色是否生效、与取消等宽同高、按钮组是否居中。
 * 5) #89：KeyForm 弹窗标题与底部按钮组是否同轴、按钮是否等宽同高。
 */
import { mkdirSync, writeFileSync } from "node:fs";
import { join, resolve } from "node:path";
import { VerifyBrowser } from "./lib/browser.mjs";
import { installFixtures } from "./lib/fixtures.mjs";
import { installHelpers, openSettings } from "./lib/probe.mjs";

const args = process.argv.slice(2);
const flag = (name, fallback) => {
  const index = args.indexOf("--" + name);
  if (index < 0) return fallback;
  const value = args[index + 1];
  return value === undefined || value.startsWith("--") ? true : value;
};
const sleep = (ms) => new Promise((done) => setTimeout(done, ms));

const appUrl = String(flag("url", "http://127.0.0.1:5109")).replace(/\/$/, "");
const apiBase = String(flag("api", "http://127.0.0.1:8759")).replace(/\/$/, "");
const label = String(flag("label", "surf"));
const phase = String(flag("phase", "before"));
const quick = !!flag("quick", false);
const outDir = resolve(String(flag("out", join(process.cwd(), "_surface-shots"))));
const cdpPort = Number(flag("cdp-port", 9819));
const viewportList = quick
  ? [{ width: 1440, height: 900 }, { width: 800, height: 600 }]
  : [{ width: 1440, height: 900 }, { width: 1024, height: 768 }, { width: 800, height: 600 }];
const themeList = ["dark", "light"];
const CATEGORIES = [
  { id: "regular", label: "常规" },
  { id: "models", label: "模型与服务" },
  { id: "memory", label: "记忆与整理" },
  { id: "tools", label: "工具与权限" },
  { id: "about", label: "关于与更新" },
];

mkdirSync(outDir, { recursive: true });

const run = {
  label,
  phase,
  startedAt: new Date().toISOString(),
  appUrl,
  viewports: viewportList,
  themes: themeList,
  checks: [],
  screenshots: [],
  frames: {},
  rows: [],
  dialogs: {},
  notes: [],
};
function record(name, ok, detail) {
  run.checks.push({ name, ok: !!ok, detail: detail === undefined ? null : detail });
  if (!ok) console.log("  [FAIL] " + name + " :: " + JSON.stringify(detail).slice(0, 700));
  else console.log("  [ok] " + name);
  return ok;
}
async function shot(browser, name) {
  const file = join(outDir, "surface-" + phase + "-" + name + ".png");
  await browser.shotFile(file);
  run.screenshots.push(file);
  return file;
}
async function evalPageFn(browser, fn, ...params) {
  const source = "(" + fn.toString() + ")(" + params.map((value) => JSON.stringify(value)).join(",") + ")";
  return browser.evalJs(source);
}
async function clickTab(browser, id) {
  const clicked = await browser.evalJs(
    "(() => { const b = document.querySelector('[data-settings-nav-tab=" + JSON.stringify(id) + "]'); if (!b) return false; b.click(); return true; })()",
  );
  await sleep(420);
  await browser.evalJs("(() => { const r = document.querySelector('[data-testid=settings-page]'); if (r) r.scrollTop = 0; return true; })()");
  await sleep(160);
  return clicked;
}

/** 页面内采集：#90 外框/内部阅读宽度 + #70 行内布局。 */
function layoutExpr() {
  const box = (el) => {
    if (!el) return null;
    const r = el.getBoundingClientRect();
    return { left: r.left, right: r.right, top: r.top, bottom: r.bottom, width: r.width, height: r.height };
  };
  const cs = (el) => (el ? getComputedStyle(el) : null);
  const out = { viewport: { width: window.innerWidth, height: window.innerHeight } };
  const root = document.querySelector(".qio-settings");
  const main = document.querySelector(".qio-settings .set-main");
  const panels = document.querySelector(".qio-settings .set-panels");
  const visibleSection = [...document.querySelectorAll(".qio-settings .set-section")].find((el) => cs(el).display !== "none");
  out.root = box(root);
  out.main = box(main);
  out.panels = box(panels);
  out.section = box(visibleSection);
  out.sectionId = visibleSection ? visibleSection.getAttribute("data-settings-section") : null;
  out.mainStyle = main
    ? { paddingLeft: cs(main).paddingLeft, paddingRight: cs(main).paddingRight, borderLeft: cs(main).borderLeftWidth, borderRight: cs(main).borderRightWidth }
    : null;
  out.navLayout = root ? root.getAttribute("data-nav-layout") : null;
  const head = visibleSection ? visibleSection.querySelector(".set-section__head") : null;
  const desc = visibleSection ? visibleSection.querySelector(".set-section__desc") : null;
  const firstRow = visibleSection ? visibleSection.querySelector(".set-row") : null;
  out.reading = { head: box(head), desc: box(desc), firstRow: box(firstRow) };
  let widest = 0;
  if (visibleSection) {
    for (const child of visibleSection.children) {
      const r = child.getBoundingClientRect();
      if (r.width > widest) widest = r.width;
    }
  }
  out.widestChild = widest;

  const textRight = (el) => {
    if (!el) return null;
    const walker = document.createTreeWalker(el, NodeFilter.SHOW_TEXT, null);
    let max = null;
    while (walker.nextNode()) {
      const node = walker.currentNode;
      if (!node.nodeValue || !node.nodeValue.trim()) continue;
      const range = document.createRange();
      range.selectNodeContents(node);
      for (const rc of range.getClientRects()) {
        if (rc.width <= 0) continue;
        max = max === null ? rc.right : Math.max(max, rc.right);
      }
    }
    return max;
  };
  const rows = [];
  if (visibleSection) {
    for (const row of visibleSection.querySelectorAll(".set-row")) {
      if (cs(row).display === "none") continue;
      const txt = row.querySelector(".set-row__txt");
      const title = row.querySelector(".set-row__title");
      const ctl = row.querySelector(".set-row__ctl");
      const tr = box(txt);
      const cr = box(ctl);
      const labelRight = textRight(title);
      const controlFirst = ctl ? [...ctl.children].find((el) => cs(el).display !== "none" && box(el)) : null;
      const cbr = box(controlFirst);
      rows.push({
        key: row.getAttribute("data-settings-key"),
        row: box(row),
        txt: tr,
        ctl: cr,
        labelTextRight: labelRight,
        controlFirst: cbr,
        gapLabelToControl: labelRight !== null && cbr !== null ? cbr.left - labelRight : null,
        ctlBelowTxt: tr !== null && cr !== null ? cr.top >= tr.bottom - 0.75 : null,
        rowPadLeft: cs(row).paddingLeft,
        rowPadRight: cs(row).paddingRight,
        ctlMaxWidth: cs(ctl) ? cs(ctl).maxWidth : null,
        ctlMarginLeft: cs(ctl) ? cs(ctl).marginLeft : null,
      });
    }
  }
  out.rows = rows;
  if (out.main && out.panels) {
    out.gutters = {
      left: out.panels.left - out.main.left,
      right: out.main.right - out.panels.right,
      delta: Math.abs(out.panels.left - out.main.left - (out.main.right - out.panels.right)),
    };
  } else {
    out.gutters = null;
  }
  return out;
}

/** #82 普通确认层（记忆与整理 → 立即整理）。 */
function normalConfirmExpr() {
  const box = (el) => {
    if (!el) return null;
    const r = el.getBoundingClientRect();
    return { left: r.left, right: r.right, top: r.top, bottom: r.bottom, width: r.width, height: r.height };
  };
  const scrim = document.querySelector(".qio-confirm-scrim");
  const panel = scrim ? scrim.querySelector(".qio-confirm") : null;
  const chrome = document.querySelector(".window-chrome");
  const root = document.querySelector(".qio-settings");
  const main = document.querySelector(".qio-settings .set-main");
  if (!scrim || !panel) return { found: false, scrim: !!scrim, panel: !!panel };
  const cs = (el) => getComputedStyle(el);
  return {
    found: true,
    viewport: { width: window.innerWidth, height: window.innerHeight },
    root: box(root),
    main: box(main),
    scrim: box(scrim),
    panel: box(panel),
    title: box(panel.querySelector(".qio-confirm__title")),
    cancel: box(panel.querySelector("[data-qio-confirm-action=cancel]")),
    confirm: box(panel.querySelector("[data-qio-confirm-action=confirm]")),
    chrome: box(chrome),
    chromeZ: chrome ? cs(chrome).zIndex : null,
    scrimZ: cs(scrim).zIndex,
    scrimPosition: cs(scrim).position,
    panelZ: cs(panel).zIndex,
    scrimTransform: cs(scrim).transform,
    panelTransform: cs(panel).transform,
    scrimInViewport: (() => {
      const r = scrim.getBoundingClientRect();
      return r.left >= -0.5 && r.top >= -0.5 && r.right <= window.innerWidth + 0.5 && r.bottom <= window.innerHeight + 0.5;
    })(),
  };
}

/** #87 危险确认层（模型与服务 → 清除博查 Key）。 */
function dangerConfirmExpr() {
  const box = (el) => {
    if (!el) return null;
    const r = el.getBoundingClientRect();
    return { left: r.left, right: r.right, top: r.top, bottom: r.bottom, width: r.width, height: r.height, cx: r.left + r.width / 2, cy: r.top + r.height / 2 };
  };
  const panel = document.querySelector(".qio-confirm--layer");
  if (!panel) return { found: false, dangerButtons: [...document.querySelectorAll(".qio-btn.danger")].length };
  const cancel = panel.querySelector("[data-qio-confirm-action=cancel]");
  const confirm = panel.querySelector("[data-qio-confirm-action=confirm]");
  const actions = panel.querySelector(".qio-confirm__actions");
  const cs = (el) => getComputedStyle(el);
  const pr = panel.getBoundingClientRect();
  const ar = actions ? actions.getBoundingClientRect() : null;
  return {
    found: true,
    panel: box(panel),
    title: box(panel.querySelector(".qio-confirm__title")),
    actions: box(actions),
    cancel: box(cancel),
    confirm: box(confirm),
    panelCenterX: pr.left + pr.width / 2,
    actionsCenterX: ar ? ar.left + ar.width / 2 : null,
    confirmColor: confirm ? cs(confirm).color : null,
    confirmBackground: confirm ? cs(confirm).backgroundColor : null,
    cancelColor: cancel ? cs(cancel).color : null,
    equalWidth: cancel && confirm ? Math.abs(cancel.getBoundingClientRect().width - confirm.getBoundingClientRect().width) : null,
    equalHeight: cancel && confirm ? Math.abs(cancel.getBoundingClientRect().height - confirm.getBoundingClientRect().height) : null,
    confirmClasses: confirm ? confirm.className : null,
  };
}

/** #89 KeyForm 弹窗（模型与服务 → 添加 Key）。 */
function keyFormExpr() {
  const box = (el) => {
    if (!el) return null;
    const r = el.getBoundingClientRect();
    return { left: r.left, right: r.right, top: r.top, bottom: r.bottom, width: r.width, height: r.height, cx: r.left + r.width / 2, cy: r.top + r.height / 2 };
  };
  const modal = document.querySelector(".qio-modal");
  if (!modal) return { found: false };
  const title = modal.querySelector(".qio-modal-title");
  const actions = modal.querySelector(".key-form .actions");
  const buttons = actions ? [...actions.querySelectorAll("button")] : [];
  const mr = modal.getBoundingClientRect();
  const tr = title ? title.getBoundingClientRect() : null;
  const ar = actions ? actions.getBoundingClientRect() : null;
  const chrome = document.querySelector(".window-chrome");
  return {
    found: true,
    viewport: { width: window.innerWidth, height: window.innerHeight },
    modal: box(modal),
    title: box(title),
    titleText: title ? title.innerText : null,
    titleCenterX: tr ? tr.left + tr.width / 2 : null,
    actions: box(actions),
    actionsCenterX: ar ? ar.left + ar.width / 2 : null,
    modalCenterX: mr.left + mr.width / 2,
    chrome: box(chrome),
    modalTopVsChromeBottom: mr.top - (chrome ? chrome.getBoundingClientRect().bottom : 0),
    buttons: buttons.map((b) => ({ text: (b.innerText || "").trim(), ...box(b), display: getComputedStyle(b).display })),
    keyFormActionsJustify: actions ? getComputedStyle(actions).justifyContent : null,
  };
}

async function measureDialogs(browser, viewport, theme) {
  const key = "dialogs-" + theme + "-" + viewport.width + "x" + viewport.height;
  const result = {};

  // ---- #82：记忆与整理 → 立即整理（QConfirm layer） ----
  await clickTab(browser, "memory");
  const clickedNormal = await browser.evalJs("window.__stg.clickLabel('立即整理')");
  await sleep(520);
  result.normalConfirm = await evalPageFn(browser, normalConfirmExpr);
  result.normalConfirm.clicked = clickedNormal;
  result.normalConfirm.screenshot = await shot(browser, "dialog-82-confirm-" + theme + "-" + viewport.width + "x" + viewport.height);
  await browser.pressKey("Escape");
  await sleep(360);

  // ---- #87：模型与服务 → 清除博查 Key（danger 档 QConfirm layer） ----
  await clickTab(browser, "models");
  await sleep(700);
  const openedClear = await browser.evalJs(
    "(() => { const hit = [...document.querySelectorAll('button')].find((el) => { const r = el.getBoundingClientRect(); return r.width > 0 && r.height > 0 && (el.innerText || '').replace(/\\s/g, '') === '清除'; }); if (!hit) return { ok: false, seen: [...document.querySelectorAll('.qio-btn.danger')].map((el) => el.innerText) }; hit.click(); return { ok: true }; })()",
  );
  await sleep(520);
  result.dangerConfirm = await evalPageFn(browser, dangerConfirmExpr);
  result.dangerConfirm.opened = openedClear;
  result.dangerConfirm.screenshot = await shot(browser, "dialog-87-danger-" + theme + "-" + viewport.width + "x" + viewport.height);
  await browser.pressKey("Escape");
  await sleep(360);

  // ---- #89：模型与服务 → 添加 Key（QModal + KeyForm） ----
  const clickedAdd = await browser.evalJs("window.__stg.clickLabel('添加 Key')");
  await sleep(700);
  result.keyForm = await evalPageFn(browser, keyFormExpr);
  result.keyForm.clicked = clickedAdd;
  result.keyForm.screenshot = await shot(browser, "dialog-89-keyform-" + theme + "-" + viewport.width + "x" + viewport.height);
  await browser.pressKey("Escape");
  await sleep(360);

  run.dialogs[key] = result;
  return result;
}

async function main() {
  const browser = new VerifyBrowser({ port: cdpPort, apiBase, width: 1440, height: 900 });
  await browser.launch();
  try {
    // 只读 fixture（模型目录 / 凭据 / 搜索状态）；额外让博查显示“已配置”，
    // 否则 #87 的危险「清除」按钮没有入口。写请求仍走真实隔离后端。
    await browser.route({ method: "GET", path: /\/api\/settings\/search(\?|$)/ }, () => ({
      body: { searxng_url: "", bocha_has_key: true, keyless_fallback: true, top_k_default: 5, max_fetch_chars: 15000 },
      headers: { "Cache-Control": "no-store" },
    }));
    await installFixtures(browser);
    await browser.installDialogGuards();
    console.log("浏览器已启动，开始 " + phase + " 实测…");

    // localStorage 只在应用源下有效：先导航到应用再写主题偏好，否则写在 about:blank 上无效。
    await browser.navigate(appUrl + "/", 2400);
    for (const theme of themeList) {
      await browser.setTheme(theme);
      for (const viewport of viewportList) {
        await browser.setViewport(viewport.width, viewport.height);
        await openSettings(browser, appUrl);
        await installHelpers(browser);
        // 首次启动会弹欢迎向导（固定遮罩 + 弹窗仲裁），会挡住确认层并污染截图。
        // 先把它关掉；关闭只影响本次引导，不改产品逻辑。
        await sleep(700);
        const dismissed = await browser.evalJs(
          "(() => { const b = document.querySelector('button[aria-label=\"关闭引导\"]'); if (!b) return false; const r = b.getBoundingClientRect(); if (!r.width) return false; b.click(); return true; })()",
        );
        if (dismissed) await sleep(600);
        await browser.evalJs("(() => { const r = document.querySelector('[data-testid=settings-page]'); if (r) r.scrollTop = 0; return true; })()");
        await sleep(200);
        const themeTag = theme + "-" + viewport.width + "x" + viewport.height;
        await shot(browser, "first-" + themeTag);
        const frameKey = themeTag;
        run.frames[frameKey] = { theme, viewport, categories: {} };

        for (const category of CATEGORIES) {
          const ok = await clickTab(browser, category.id);
          const layout = await evalPageFn(browser, layoutExpr);
          layout.navClickOk = ok;
          run.frames[frameKey].categories[category.id] = {
            mainWidth: layout.main ? layout.main.width : null,
            panelsWidth: layout.panels ? layout.panels.width : null,
            sectionWidth: layout.section ? layout.section.width : null,
            widestChild: layout.widestChild,
            reading: layout.reading,
            sectionId: layout.sectionId,
            navLayout: layout.navLayout,
            gutters: layout.gutters,
            mainStyle: layout.mainStyle,
          };
          for (const row of layout.rows) {
            run.rows.push({ theme, viewport, category: category.id, ...row });
          }
          if (category.id === "about" || category.id === "tools" || category.id === "memory") {
            await shot(browser, "switched-" + category.id + "-" + themeTag);
          }
          console.log(
            "  " + themeTag + " / " + category.id + " -> main=" + (layout.main ? layout.main.width.toFixed(2) : "?") +
              " panels=" + (layout.panels ? layout.panels.width.toFixed(2) : "?") +
              " section=" + (layout.section ? layout.section.width.toFixed(2) : "?") +
              " rows=" + layout.rows.length,
          );
        }

        const widths = Object.values(run.frames[frameKey].categories).map((c) => c.mainWidth).filter((w) => w !== null);
        const panelWidths = Object.values(run.frames[frameKey].categories).map((c) => c.panelsWidth).filter((w) => w !== null);
        const sectionWidths = Object.values(run.frames[frameKey].categories).map((c) => c.sectionWidth).filter((w) => w !== null);
        const spread = widths.length ? Math.max(...widths) - Math.min(...widths) : null;
        const panelSpread = panelWidths.length ? Math.max(...panelWidths) - Math.min(...panelWidths) : null;
        record(
          "#90 外框宽度五分区一致 " + themeTag,
          spread !== null && spread <= 1 && panelSpread !== null && panelSpread <= 1,
          {
            mainWidths: widths.map((w) => +w.toFixed(2)),
            panelWidths: panelWidths.map((w) => +w.toFixed(2)),
            sectionWidths: sectionWidths.map((w) => +w.toFixed(2)),
            spread: +spread.toFixed(2),
            panelSpread: +panelSpread.toFixed(2),
          },
        );

        const rows = run.rows.filter(
          (row) => row.theme === theme && row.viewport.width === viewport.width && row.viewport.height === viewport.height,
        );
        const gaps = rows.map((row) => row.gapLabelToControl).filter((v) => typeof v === "number");
        if (viewport.width > 900) {
          const sorted = [...gaps].sort((a, b) => a - b);
          const median = sorted.length ? sorted[Math.floor(sorted.length / 2)] : null;
          record("#70 宽窗行内横向距离（中位数）" + themeTag, median !== null && median <= 40, {
            count: gaps.length,
            min: sorted.length ? +sorted[0].toFixed(2) : null,
            median: median === null ? null : +median.toFixed(2),
            max: sorted.length ? +sorted[sorted.length - 1].toFixed(2) : null,
            samples: rows.slice(0, 10).map((row) => ({
              key: row.key,
              gap: row.gapLabelToControl === null ? null : +row.gapLabelToControl.toFixed(2),
              labelRight: row.labelTextRight === null ? null : +row.labelTextRight.toFixed(2),
              ctlLeft: row.controlFirst ? +row.controlFirst.left.toFixed(2) : null,
            })),
          });
          const first = Object.values(run.frames[frameKey].categories)[0];
          const delta = first && first.gutters ? first.gutters.delta : null;
          record("#70 左右留白对称 " + themeTag, delta !== null && delta <= 1, first ? first.gutters : null);
        } else {
          const notStacked = rows.filter((row) => row.ctlBelowTxt === false).map((row) => row.key);
          record("#70 窄窗控件在标签下方（上下排列）" + themeTag, rows.length > 0 && notStacked.length === 0, {
            rows: rows.length,
            notStacked,
          });
        }

        if (viewport.width === 800 || viewport.width === 1440) {
          try {
            const dialogs = await measureDialogs(browser, viewport, theme);
            const scope = themeTag;
            if (dialogs.normalConfirm && dialogs.normalConfirm.found) {
              const d = dialogs.normalConfirm;
              const chromeBottom = d.chrome ? d.chrome.bottom : 0;
              record("#82 确认层不被顶部全局栏遮挡 " + scope, d.panel && d.panel.top >= chromeBottom - 0.75, {
                chromeBottom,
                panelTop: d.panel ? +d.panel.top.toFixed(2) : null,
                panelBottom: d.panel ? +d.panel.bottom.toFixed(2) : null,
                scrim: d.scrim,
                scrimInViewport: d.scrimInViewport,
                chromeZ: d.chromeZ,
                scrimZ: d.scrimZ,
                scrimPosition: d.scrimPosition,
              });
              record("#82 遮罩覆盖整个视口 " + scope, d.scrimInViewport === true, {
                scrim: d.scrim,
                root: d.root,
                main: d.main,
                viewport: d.viewport,
                scrimTransform: d.scrimTransform,
              });
            } else {
              record("#82 找到设置内确认层 " + scope, false, dialogs.normalConfirm);
            }
            if (dialogs.dangerConfirm && dialogs.dangerConfirm.found) {
              const d = dialogs.dangerConfirm;
              record(
                "#87 危险按钮底色不再被普通按钮规则覆盖 " + scope,
                !!d.confirmBackground && !/rgba\(0, 0, 0, 0\)/.test(d.confirmBackground),
                { confirmColor: d.confirmColor, confirmBackground: d.confirmBackground, confirmClasses: d.confirmClasses, cancelColor: d.cancelColor },
              );
              record(
                "#87 确认按钮与取消等宽同高且整体居中 " + scope,
                d.equalWidth !== null && d.equalWidth <= 1 && d.equalHeight !== null && d.equalHeight <= 1 &&
                  d.actionsCenterX !== null && Math.abs(d.actionsCenterX - d.panelCenterX) <= 1,
                {
                  equalWidth: d.equalWidth,
                  equalHeight: d.equalHeight,
                  actionsCenterX: d.actionsCenterX,
                  panelCenterX: d.panelCenterX,
                  actions: d.actions,
                  buttons: { cancel: d.cancel, confirm: d.confirm },
                },
              );
            } else {
              record("#87 找到危险确认层 " + scope, false, dialogs.dangerConfirm);
            }
            if (dialogs.keyForm && dialogs.keyForm.found) {
              const d = dialogs.keyForm;
              const widths = d.buttons.map((b) => +b.width.toFixed(2));
              record(
                "#89 KeyForm 标题与底部按钮组同轴 " + scope,
                d.titleCenterX !== null && d.actionsCenterX !== null && Math.abs(d.titleCenterX - d.actionsCenterX) <= 1,
                {
                  titleCenterX: d.titleCenterX,
                  actionsCenterX: d.actionsCenterX,
                  modalCenterX: d.modalCenterX,
                  modal: d.modal,
                  title: d.title,
                  actions: d.actions,
                  justify: d.keyFormActionsJustify,
                },
              );
              record(
                "#89 KeyForm 底部按钮等宽同高 " + scope,
                widths.length >= 2 && Math.max(...widths) - Math.min(...widths) <= 1 &&
                  Math.max(...d.buttons.map((b) => b.height)) - Math.min(...d.buttons.map((b) => b.height)) <= 1,
                { buttons: d.buttons.map((b) => ({ text: b.text, width: +b.width.toFixed(2), height: +b.height.toFixed(2) })) },
              );
            } else {
              record("#89 找到 KeyForm 弹窗 " + scope, false, dialogs.keyForm);
            }
          } catch (error) {
            run.notes.push("弹窗实测失败 " + themeTag + "：" + String(error && error.stack ? error.stack : error));
            console.log("  [WARN] 弹窗实测失败 " + themeTag + "：" + String(error).slice(0, 300));
          }
        }
      }
    }

    record("无未捕获 JS 异常", browser.consoleErrors.length === 0, browser.consoleErrors.slice(0, 10));
    const httpFails = browser.httpFails.filter((line) => !/favicon/.test(line));
    record("无 4xx/5xx 请求（忽略 favicon）", httpFails.length === 0, httpFails.slice(0, 10));
  } finally {
    await browser.close();
  }
  const file = join(outDir, "surface-measure-" + phase + ".json");
  writeFileSync(file, JSON.stringify(run, null, 2), "utf8");
  const failed = run.checks.filter((check) => !check.ok);
  console.log("\n结果：" + (run.checks.length - failed.length) + "/" + run.checks.length + " 通过；失败 " + failed.length);
  for (const item of failed) console.log("  FAIL " + item.name);
  console.log("证据：" + file);
  console.log("截图：" + run.screenshots.length + " 张，目录 " + outDir);
  if (failed.length) process.exitCode = 1;
}

main().catch((error) => {
  console.error("失败：" + (error && error.stack ? error.stack : error));
  process.exitCode = 2;
});