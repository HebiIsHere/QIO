// QIO 提示/通知/弹窗统一改造 · A（基础控件）截图与几何复核。
//
// 跑法（在 frontend/ 下开两个终端）：
//   1) npx vite --port 5200 --strictPort
//   2) node ui-catalog/uf-shots.mjs            （默认 http://127.0.0.1:5200/ui-catalog.html）
//      也可 BASE=http://127.0.0.1:5200/ui-catalog.html node ui-catalog/uf-shots.mjs
//
// 证据输出：frontend/e2e-shots/uf-a/（该目录已被 .gitignore 忽略，可再生产物）
//   - *.png：状态截图（深色/浅色、1200/480/320 宽）
//   - results.json：真实 DOM rect 复核结果（标题中心偏差、左右槽等宽、按钮等宽同高、
//     窄窗纵排、用途说明浮层尺寸与视口内位置、图标数量）
//
// 为什么要有这个脚本：jsdom 不做排版，单测只能守结构与样式来源；
// “标题是否真的在组件中轴上”“动作按钮是否真的同宽同高”必须用真实浏览器量。
import { createRequire } from "node:module";
import { fileURLToPath } from "node:url";
import { mkdirSync, writeFileSync } from "node:fs";

const require = createRequire(import.meta.url);
const PW = "C:\\Users\\zxy\\.cache\\codex-runtimes\\codex-primary-runtime\\dependencies\\node\\node_modules\\playwright";
const { chromium } = require(PW);

const OUT = fileURLToPath(new URL("../e2e-shots/uf-a/", import.meta.url));
mkdirSync(OUT, { recursive: true });
const BASE = process.env.BASE || "http://127.0.0.1:5200/ui-catalog.html";

const results = { base: BASE, startedAt: new Date().toISOString(), checks: [], measurements: {}, logs: [], screenshots: [] };
let failed = 0;
function check(name, ok, detail) {
  results.checks.push({ name, ok: Boolean(ok), detail });
  if (!ok) failed += 1;
}
function shot(name) {
  results.screenshots.push(name);
  return OUT + name;
}

const browser = await chromium.launch({ channel: "msedge", headless: true });
try {
  const context = await browser.newContext({ viewport: { width: 1440, height: 900 } });
  const page = await context.newPage();
  page.on("console", (m) => {
    if (m.type() === "error") results.logs.push(`console.error: ${m.text()}`);
  });
  page.on("pageerror", (e) => results.logs.push(`pageerror: ${e.message}`));

  async function openGallery(theme) {
    await page.addInitScript((t) => {
      try {
        if (!localStorage.getItem("qio-theme")) localStorage.setItem("qio-theme", t);
      } catch {}
    }, theme);
    await page.goto(BASE, { waitUntil: "domcontentloaded", timeout: 30000 });
    await page.waitForSelector("html[data-gallery-ready='1']", { timeout: 20000 });
    await page.waitForTimeout(350);
  }

  await openGallery("dark");

  // ---------- 0. 基础可用性 ----------
  const blockCount = await page.evaluate(() => document.querySelectorAll(".blk").length);
  check("画廊块数量 ≥ 40（新增状态已注册）", blockCount >= 40, { blockCount });

  // ---------- 1. QIcon 全表 ----------
  await page.locator("#atoms-qicon-all").scrollIntoViewIfNeeded();
  const iconCount = await page.locator("#atoms-qicon-all .icon-cell").count();
  check("图标全表 ≥ 40 个图标", iconCount >= 40, { iconCount });
  await page.locator("#atoms-qicon-all").screenshot({ path: shot("dark-01-icons.png") });

  // ---------- 2. QIconButton 状态 + 14px/32px/无 title ----------
  await page.locator("#atoms-qiconbutton-states").scrollIntoViewIfNeeded();
  const iconBtn = await page.evaluate(() => {
    const el = document.querySelector("#atoms-qiconbutton-states .qio-icon-btn");
    const r = el.getBoundingClientRect();
    const cs = getComputedStyle(el);
    return {
      w: r.width,
      h: r.height,
      radius: cs.borderTopLeftRadius,
      title: el.getAttribute("title"),
      ariaLabel: el.getAttribute("aria-label"),
      tag: el.tagName,
    };
  });
  check("图标按钮 32×32", iconBtn.w === 32 && iconBtn.h === 32, iconBtn);
  check("图标按钮 14px 圆角", iconBtn.radius === "14px", iconBtn);
  check("图标按钮有可访问名称且无原生 title", iconBtn.ariaLabel && iconBtn.title === null, iconBtn);
  await page.locator("#atoms-qiconbutton-states").screenshot({ path: shot("dark-02-iconbuttons.png") });


  // ---------- 2.5 键盘焦点：焦点环可见 + 说明立即出现 ----------
  await page.locator("#atoms-qiconbutton-focus").scrollIntoViewIfNeeded();
  await page.locator('#atoms-qiconbutton-focus [data-role="focus-anchor"]').click();
  await page.keyboard.press("Tab");
  await page.waitForTimeout(160);
  const focusState = await page.evaluate(() => {
    const el = document.querySelector('[data-role="icon-focus"]');
    if (!el) return null;
    const cs = getComputedStyle(el);
    return {
      focused: document.activeElement === el,
      outlineStyle: cs.outlineStyle,
      outlineWidth: cs.outlineWidth,
      tip: document.querySelector('[role="tooltip"]')?.textContent?.trim() ?? null,
    };
  });
  check("键盘聚焦落在图标按钮上", focusState?.focused === true, focusState);
  check("键盘聚焦：焦点环可见（2px solid）", focusState?.outlineStyle === "solid" && focusState?.outlineWidth === "2px", focusState);
  check("键盘聚焦：用途说明立即出现（不等 500ms）", focusState?.tip === "打开设置页", focusState);
  results.measurements.focus = focusState;
  await page.locator("#atoms-qiconbutton-focus").screenshot({ path: shot("dark-02b-iconbutton-focus.png") });
  await page.mouse.move(0, 0);
  await page.keyboard.press("Escape");
  await page.waitForTimeout(150);
  // ---------- 3. 用途说明：500ms 后出现，浮层 14px、位于视口内 ----------
  await page.locator("#atoms-qiconbutton-tip").scrollIntoViewIfNeeded();
  await page.hover('[data-role="tip-target"]');
  await page.waitForTimeout(150);
  const tipAt150 = await page.locator('[role="tooltip"]').count();
  await page.waitForTimeout(600);
  const tip = await page.evaluate(() => {
    const el = document.querySelector('[role="tooltip"]');
    if (!el) return null;
    const r = el.getBoundingClientRect();
    const cs = getComputedStyle(el);
    return {
      text: el.textContent.trim(),
      role: el.getAttribute("role"),
      radius: cs.borderTopLeftRadius,
      inside:
        r.left >= 0 && r.top >= 0 && r.right <= innerWidth + 0.5 && r.bottom <= innerHeight + 0.5,
      rect: { left: r.left, top: r.top, width: r.width, height: r.height },
    };
  });
  check("悬停 150ms 时不出现用途说明（500ms 延迟）", tipAt150 === 0, { tipAt150 });
  check("悬停 500ms 后出现 role=tooltip 的用途说明", Boolean(tip) && tip.role === "tooltip", tip);
  check("用途说明浮层 14px 圆角", tip?.radius === "14px", tip);
  check("用途说明浮层完全在视口内", tip?.inside === true, tip);
  results.measurements.tip = tip;
  const tipClip = await page.evaluate(() => {
    const tipEl = document.querySelector('[role="tooltip"]');
    const btn = document.querySelector('[data-role="tip-target"]');
    if (!tipEl || !btn) return null;
    const t = tipEl.getBoundingClientRect();
    const b = btn.getBoundingClientRect();
    const left = Math.max(0, Math.min(t.left, b.left) - 28);
    const top = Math.max(0, Math.min(t.top, b.top) - 28);
    const right = Math.min(innerWidth, Math.max(t.right, b.right) + 28);
    const bottom = Math.min(innerHeight, Math.max(t.bottom, b.bottom) + 28);
    return { x: left, y: top, width: right - left, height: bottom - top };
  });
  await page.screenshot({ path: shot("dark-03-tip.png"), clip: tipClip ?? undefined });

  // 长用途说明：换行且不越界
  await page.mouse.move(0, 0);
  await page.waitForTimeout(120);
  await page.hover('[data-role="tip-target-long"]');
  await page.waitForTimeout(650);
  const longTip = await page.evaluate(() => {
    const el = document.querySelector('[role="tooltip"]');
    if (!el) return null;
    const r = el.getBoundingClientRect();
    return {
      width: r.width,
      height: r.height,
      inside: r.left >= 0 && r.right <= innerWidth + 0.5 && r.top >= 0 && r.bottom <= innerHeight + 0.5,
      text: el.textContent.trim().slice(0, 16),
    };
  });
  check("长用途说明换行（高度 > 单行）且不越出视口", Boolean(longTip) && longTip.height > 24 && longTip.inside, longTip);
  results.measurements.longTip = longTip;

  // ---------- 4. 几何复核工具 ----------
  async function measureDialog() {
    return page.evaluate(() => {
      const dialog = document.querySelector('[role="dialog"]');
      if (!dialog) return null;
      const rect = (el) => {
        const b = el.getBoundingClientRect();
        return { left: b.left, top: b.top, width: b.width, height: b.height, cx: b.left + b.width / 2 };
      };
      const head = dialog.querySelector(".qio-modal-head");
      const slots = head ? [...head.children] : [];
      const title = dialog.querySelector(".qio-modal-title");
      const actions = dialog.querySelector(".qio-modal-actions");
      const buttons = actions ? [...actions.children] : [];
      const headR = rect(head);
      const titleR = rect(title);
      return {
        dialog: rect(dialog),
        head: headR,
        title: titleR,
        titleCenterDiff: Math.abs(titleR.cx - headR.cx),
        dialogCenterDiff: Math.abs(rect(dialog).cx - innerWidth / 2),
        slotWidthDiff: Math.abs(rect(slots[0]).width - rect(slots[2]).width),
        slotFocusable: slots[0].querySelectorAll("button, [href], input, [tabindex]").length,
        closeLabel: slots[2]?.querySelector("button")?.getAttribute("aria-label") ?? null,
        actions: actions
          ? {
              direction: getComputedStyle(actions).flexDirection,
              widths: buttons.map((b) => rect(b).width),
              heights: buttons.map((b) => rect(b).height),
              centerDiff: Math.abs(rect(actions).cx - rect(dialog).cx),
            }
          : null,
      };
    });
  }
  function assertSymmetric(label, m) {
    check(`${label}：标题中心与标题栏中心偏差 ≤1px`, m && m.titleCenterDiff <= 1, m && { diff: m.titleCenterDiff });
    check(`${label}：左右槽宽度差 ≤1px`, m && m.slotWidthDiff <= 1, m && { diff: m.slotWidthDiff });
    check(`${label}：弹窗水平居中偏差 ≤1px`, m && m.dialogCenterDiff <= 1, m && { diff: m.dialogCenterDiff });
    check(`${label}：动作按钮等宽（差 ≤1px）`, m && m.actions && Math.max(...m.actions.widths) - Math.min(...m.actions.widths) <= 1, m && m.actions);
    check(`${label}：动作按钮同高（差 ≤1px）`, m && m.actions && Math.max(...m.actions.heights) - Math.min(...m.actions.heights) <= 1, m && m.actions);
    check(`${label}：动作按钮组整体居中（偏差 ≤1px）`, m && m.actions && m.actions.centerDiff <= 1, m && m.actions);
  }

  // ---------- 5. QModal 1200 宽 ----------
  await page.locator("#atoms-qmodal-normal [data-role='modal-trigger']").click();
  await page.waitForTimeout(420);
  const modal1200 = await measureDialog();
  assertSymmetric("QModal 1200", modal1200);
  check("QModal 关闭按钮在右槽且有可访问名称", modal1200?.closeLabel === "关闭", modal1200 && { closeLabel: modal1200.closeLabel });
  check("QModal 左槽没有可聚焦控件", modal1200?.slotFocusable === 0, modal1200 && { slotFocusable: modal1200.slotFocusable });
  results.measurements.modal1200 = modal1200;
  await page.screenshot({ path: shot("dark-04-modal-1200.png") });
  await page.keyboard.press("Escape");
  await page.waitForTimeout(320);
  check("QModal Esc 关闭", (await page.locator('[role="dialog"]').count()) === 0);


  // ---------- 5.5 QModal busy：关闭按钮禁用、Esc/外部点击都不关闭 ----------
  await page.locator("#atoms-qmodal-busy [data-role='modal-trigger']").click();
  await page.waitForTimeout(420);
  const busyState = await page.evaluate(() => {
    const dialog = document.querySelector('[role="dialog"]');
    if (!dialog) return null;
    const close = dialog.querySelector(".qio-modal-head__slot--end button");
    return {
      ariaBusy: dialog.getAttribute("aria-busy"),
      closeDisabled: close?.disabled === true,
      closeAriaBusy: close?.getAttribute("aria-busy"),
    };
  });
  check("QModal busy：aria-busy=true 且关闭按钮禁用", busyState?.ariaBusy === "true" && busyState?.closeDisabled === true, busyState);
  await page.keyboard.press("Escape");
  await page.waitForTimeout(250);
  check("QModal busy：Esc 不关闭", (await page.locator('[role="dialog"]').count()) === 1);
  results.measurements.busyModal = busyState;
  await page.screenshot({ path: shot("dark-05b-modal-busy.png") });
  await page.locator('.qio-modal-actions [data-role="modal-cancel"]').click();
  await page.waitForTimeout(300);
  check("QModal busy：业务按钮仍可操作（稍后处理可关闭）", (await page.locator('[role="dialog"]').count()) === 0);

  // ---------- 6. QConfirm layer 1200 宽 ----------
  await page.locator("#atoms-qconfirm-layer-long [data-role='trigger']").click();
  await page.waitForTimeout(420);
  const confirm1200 = await measureDialog();
  assertSymmetric("QConfirm 1200", confirm1200);
  check("QConfirm 左槽没有可聚焦控件", confirm1200?.slotFocusable === 0, confirm1200 && { slotFocusable: confirm1200.slotFocusable });
  results.measurements.confirm1200 = confirm1200;
  await page.screenshot({ path: shot("dark-05-confirm-1200.png") });

  // ---------- 7. 窄窗 320：按钮组纵排且等宽 ----------
  await page.setViewportSize({ width: 320, height: 720 });
  await page.waitForTimeout(400);
  const confirm320 = await measureDialog();
  assertSymmetric("QConfirm 320", confirm320);
  check("QConfirm 320：动作按钮纵排", confirm320?.actions?.direction === "column", confirm320 && confirm320.actions);
  results.measurements.confirm320 = confirm320;
  await page.screenshot({ path: shot("dark-06-confirm-320.png") });
  await page.keyboard.press("Escape");
  await page.waitForTimeout(300);

  await page.locator("#atoms-qmodal-normal [data-role='modal-trigger']").click();
  await page.waitForTimeout(420);
  const modal320 = await measureDialog();
  assertSymmetric("QModal 320", modal320);
  check("QModal 320：动作按钮纵排", modal320?.actions?.direction === "column", modal320 && modal320.actions);
  results.measurements.modal320 = modal320;
  await page.screenshot({ path: shot("dark-07-modal-320.png") });
  await page.keyboard.press("Escape");
  await page.waitForTimeout(300);

  // ---------- 8. 480 宽（断点本身） ----------
  await page.setViewportSize({ width: 480, height: 760 });
  await page.waitForTimeout(300);
  await page.locator("#atoms-qmodal-danger-long [data-role='modal-trigger']").click();
  await page.waitForTimeout(420);
  const modal480 = await measureDialog();
  assertSymmetric("QModal 480", modal480);
  results.measurements.modal480 = modal480;
  await page.screenshot({ path: shot("dark-08-modal-480.png") });
  await page.keyboard.press("Escape");
  await page.waitForTimeout(250);

  // ---------- 7.5 QModal 三选项：三个按钮等宽同高、按钮组居中 ----------
  await page.setViewportSize({ width: 1440, height: 900 });
  await page.waitForTimeout(300);
  await page.locator("#atoms-qmodal-three-actions [data-role='modal-trigger']").click();
  await page.waitForTimeout(420);
  const modalThree = await measureDialog();
  assertSymmetric("QModal 三选项", modalThree);
  check("QModal 三选项：恰好 3 个动作按钮", modalThree?.actions?.widths.length === 3, modalThree && modalThree.actions);
  results.measurements.modalThree = modalThree;
  await page.screenshot({ path: shot("dark-09-modal-three-actions.png") });
  await page.keyboard.press("Escape");
  await page.waitForTimeout(250);

  // ---------- 9. 表单状态截图（输入/下拉/可搜索下拉/徽标） ----------
  await page.setViewportSize({ width: 1440, height: 900 });
  await page.waitForTimeout(300);
  for (const id of ["atoms-qinput-busy", "atoms-qselect-placeholder", "atoms-qselect-busy", "atoms-badges", "atoms-qnumber-default"]) {
    await page.locator("#" + id).scrollIntoViewIfNeeded();
    await page.locator("#" + id).screenshot({ path: shot(`dark-09-${id}.png`) });
  }
  await page.locator("#atoms-qcombo-open").scrollIntoViewIfNeeded();
  await page.locator("#atoms-qcombo-open .qio-combo-trigger").click();
  await page.waitForTimeout(280);
  const comboRadius = await page.evaluate(() => {
    const menu = document.querySelector("#atoms-qcombo-open .qio-combo-menu");
    const trigger = document.querySelector("#atoms-qcombo-open .qio-combo-trigger");
    if (!menu) return null;
    return {
      menuRadius: getComputedStyle(menu).borderTopLeftRadius,
      triggerRadius: getComputedStyle(trigger).borderTopLeftRadius,
    };
  });
  check("QCombo 菜单与触发框 14px 圆角", comboRadius?.menuRadius === "14px" && comboRadius?.triggerRadius === "14px", comboRadius);
  await page.locator("#atoms-qcombo-open").screenshot({ path: shot("dark-10-qcombo-open.png") });
  await page.keyboard.press("Escape");
  await page.waitForTimeout(200);

  await page.locator("#atoms-qcombo-empty").scrollIntoViewIfNeeded();
  await page.locator("#atoms-qcombo-empty .qio-combo-trigger").click();
  await page.waitForTimeout(200);
  await page.locator("#atoms-qcombo-empty .qio-combo-search").fill("zzzz");
  await page.waitForTimeout(200);
  const emptyText = await page.locator("#atoms-qcombo-empty .qio-combo-empty").textContent();
  check("QCombo 空结果有原位说明", Boolean(emptyText && emptyText.trim().length), { emptyText });
  await page.locator("#atoms-qcombo-empty").screenshot({ path: shot("dark-11-qcombo-empty.png") });
  await page.keyboard.press("Escape");


  await page.locator("#atoms-qcombo-disabled-busy").scrollIntoViewIfNeeded();
  const comboTriggers = page.locator("#atoms-qcombo-disabled-busy .qio-combo-trigger");
  await comboTriggers.nth(0).click({ force: true }).catch(() => {});
  await comboTriggers.nth(1).click({ force: true }).catch(() => {});
  await page.waitForTimeout(200);
  const comboBlocked = await page.evaluate(
    () => document.querySelectorAll("#atoms-qcombo-disabled-busy .qio-combo-menu").length,
  );
  check("QCombo 禁用/处理中不展开菜单", comboBlocked === 0, { openMenus: comboBlocked });
  await page.locator("#atoms-qcombo-disabled-busy").screenshot({ path: shot("dark-10b-qcombo-disabled-busy.png") });

  // ---------- 10. 浅色主题 ----------
  await page.evaluate(() => localStorage.setItem("qio-theme", "light"));
  await page.reload({ waitUntil: "domcontentloaded" });
  await page.waitForSelector("html[data-gallery-ready='1']", { timeout: 20000 });
  await page.waitForTimeout(350);
  check("浅色主题已应用", (await page.evaluate(() => document.documentElement.dataset.theme)) === "light");
  const lightTipRadius = await page.evaluate(() => {
    return getComputedStyle(document.documentElement).getPropertyValue("--qio-ui-radius").trim();
  });
  check("浅色主题共用同一圆角 token", lightTipRadius === "14px", { lightTipRadius });
  await page.locator("#atoms-qmodal-normal [data-role='modal-trigger']").click();
  await page.waitForTimeout(420);
  const lightModal = await measureDialog();
  assertSymmetric("QModal 浅色", lightModal);
  results.measurements.lightModal = lightModal;
  await page.screenshot({ path: shot("light-12-modal-1200.png") });
  await page.keyboard.press("Escape");
  await page.waitForTimeout(250);
  await page.locator("#atoms-qiconbutton-tip").scrollIntoViewIfNeeded();
  await page.hover('[data-role="tip-target"]');
  await page.waitForTimeout(650);
  const lightTipClip = await page.evaluate(() => {
    const tipEl = document.querySelector('[role="tooltip"]');
    const btn = document.querySelector('[data-role="tip-target"]');
    if (!tipEl || !btn) return null;
    const t = tipEl.getBoundingClientRect();
    const b = btn.getBoundingClientRect();
    const left = Math.max(0, Math.min(t.left, b.left) - 28);
    const top = Math.max(0, Math.min(t.top, b.top) - 28);
    const right = Math.min(innerWidth, Math.max(t.right, b.right) + 28);
    const bottom = Math.min(innerHeight, Math.max(t.bottom, b.bottom) + 28);
    return { x: left, y: top, width: right - left, height: bottom - top };
  });
  await page.screenshot({ path: shot("light-13-tip.png"), clip: lightTipClip ?? undefined });
  await page.evaluate(() => document.querySelector("#atoms-qicon-all")?.scrollIntoView());
  await page.waitForTimeout(200);
  await page.locator("#atoms-qicon-all").screenshot({ path: shot("light-14-icons.png") });
  await page.evaluate(() => window.scrollTo(0, 0));
  await page.waitForTimeout(200);
  await page.screenshot({ path: shot("light-15-top.png") });

  // ---------- 11. 触控 coarse 命中区（尽力验证；CSS 守卫在单测里另有断言） ----------
  const touchContext = await browser.newContext({ viewport: { width: 390, height: 844 }, hasTouch: true, isMobile: true });
  const touchPage = await touchContext.newPage();
  await touchPage.addInitScript(() => {
    try {
      localStorage.setItem("qio-theme", "dark");
    } catch {}
  });
  await touchPage.goto(BASE, { waitUntil: "domcontentloaded", timeout: 30000 });
  await touchPage.waitForSelector("html[data-gallery-ready='1']", { timeout: 20000 });
  await touchPage.waitForTimeout(300);
  const coarse = await touchPage.evaluate(() => {
    const el = document.querySelector("#atoms-qiconbutton-states .qio-icon-btn");
    const r = el.getBoundingClientRect();
    return { coarse: matchMedia("(pointer: coarse)").matches, width: r.width, height: r.height };
  });
  results.measurements.touch = coarse;
  if (coarse.coarse) {
    check("触控 coarse：图标按钮有效命中区 44×44", coarse.width >= 44 && coarse.height >= 44, coarse);
  } else {
    check("触控粗指针模拟不可用（不判红，已由样式守卫覆盖）", true, coarse);
  }
  await touchContext.close();
} finally {
  results.finishedAt = new Date().toISOString();
  results.failed = failed;
  writeFileSync(OUT + "results.json", JSON.stringify(results, null, 2), "utf8");
  console.log(JSON.stringify({ checks: results.checks.length, failed, measurements: results.measurements }, null, 2));
  console.log(`失败项：${results.checks.filter((c) => !c.ok).map((c) => c.name).join("、") || "无"}`);
  console.log(`证据目录：${OUT}`);
  await browser.close();
}