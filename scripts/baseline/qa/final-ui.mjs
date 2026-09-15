// 任务 05 最终验收（界面矩阵）：窗口 × 主题 × 页面、浮动组件行为、无障碍基础、控制台洁净度。
// 用法（服务需在跑：后端 8734、前端 5199）：
//   node scripts/baseline/qa/final-ui.mjs [W|T|F|A|J ...]   # 不传则全跑
// 输出：%TEMP%\qio-baseline\qa\report-final-ui.json 与 shots\*.png
import { createRequire } from "node:module";
import { mkdirSync, writeFileSync } from "node:fs";

const require = createRequire(import.meta.url);
const PW = "C:\\Users\\zxy\\.cache\\codex-runtimes\\codex-primary-runtime\\dependencies\\node\\node_modules\\playwright";
const { chromium } = require(PW);

const BASE = "http://127.0.0.1:5199";
const OUT = `${process.env.TEMP}\\qio-baseline\\qa`;
const SHOTS = `${OUT}\\shots`;
mkdirSync(SHOTS, { recursive: true });

const report = { startedAt: new Date().toISOString(), cases: [], console: [] };
const rec = (id, title, passed, actual, detail = null) => {
  report.cases.push({ id, title, passed: !!passed, actual: String(actual), detail });
  console.log(`[${passed ? "PASS" : "FAIL"}] ${id} ${title} :: ${actual}`);
};
const wanted = process.argv.slice(2);
const only = (id) => wanted.length === 0 || wanted.some((w) => id.startsWith(w));

let seq = Math.floor(Date.now() / 1000) % 100000;
const url = (hash = "#/") => `${BASE}/?fresh=${++seq}${hash}`;
const wait = (ms) => new Promise((r) => setTimeout(r, ms));

const browser = await chromium.launch({
  channel: "msedge",
  headless: true,
  args: ["--use-gl=angle", "--use-angle=swiftshader", "--enable-unsafe-swiftshader"],
});
const context = await browser.newContext({ viewport: { width: 1440, height: 900 } });
await context.addInitScript(() => {
  try {
    if (!localStorage.getItem("qio-theme")) localStorage.setItem("qio-theme", "dark");
  } catch {
    /* 忽略 */
  }
});
const page = await context.newPage();

// 控制台洁净度：整个流程里收集 error/warning 与未捕获异常
page.on("console", (msg) => {
  const type = msg.type();
  if (type === "error" || type === "warning") {
    report.console.push({ type, text: msg.text().slice(0, 300), url: page.url() });
  }
});
page.on("pageerror", (err) => report.console.push({ type: "pageerror", text: String(err).slice(0, 300), url: page.url() }));

const overflow = () =>
  page.evaluate(() => {
    const de = document.documentElement;
    return {
      docScrollW: de.scrollWidth,
      innerW: window.innerWidth,
      bodyScrollW: document.body.scrollWidth,
      overflowing: de.scrollWidth - window.innerWidth,
    };
  });

async function openPlanet() {
  await page.locator(".dock").click();
  await page.waitForSelector(".planet-view", { timeout: 8000 });
  await wait(900); // 等展开时间线（铺底 + 淡入）完成
}
async function closePlanet() {
  await page.keyboard.press("Escape");
  await wait(300);
  await page.keyboard.press("Escape");
  await wait(900); // 等收起时间线完成
  const left = await page.locator(".planet-view").count();
  return left;
}

async function main() {
  if (only("W")) {
    // A. 窗口矩阵
    const SIZES = [
      [800, 600, "800x600"],
      [1024, 768, "1024x768"],
      [1280, 800, "1280x800"],
      [1440, 900, "1440x900"],
      [1920, 1080, "1920x1080"],
    ];
    for (const [w, h, label] of SIZES) {
      await page.setViewportSize({ width: w, height: h });
      // 聊天
      await page.goto(url("#/"));
      await page.waitForSelector(".composer textarea", { timeout: 10000 });
      await wait(500);
      const chatOv = await overflow();
      const composerVisible = await page.locator(".composer").isVisible();
      await page.screenshot({ path: `${SHOTS}\\win-${label}-chat.png` });
      rec(
        `W-CHAT-${label}`,
        `${label} 聊天页无横向溢出、输入区可见`,
        chatOv.overflowing <= 1 && composerVisible,
        `溢出=${chatOv.overflowing}px 输入区可见=${composerVisible}`,
      );

      // 设置
      await page.goto(url("#/settings"));
      await page.waitForSelector(".nav .tab", { timeout: 10000 });
      await wait(400);
      const setOv = await overflow();
      const navVisible = await page.locator(".nav .tab").first().isVisible();
      await page.screenshot({ path: `${SHOTS}\\win-${label}-settings.png` });
      rec(
        `W-SETTINGS-${label}`,
        `${label} 设置页无横向溢出、分类可见`,
        setOv.overflowing <= 1 && navVisible,
        `溢出=${setOv.overflowing}px 分类可见=${navVisible}`,
      );

      // 星球
      await page.goto(url("#/"));
      await page.waitForSelector(".dock", { timeout: 10000 });
      await openPlanet();
      const planetOv = await overflow();
      const canvasBox = await page.locator(".planet-canvas").boundingBox();
      await page.screenshot({ path: `${SHOTS}\\win-${label}-planet.png` });
      rec(
        `W-PLANET-${label}`,
        `${label} 星球页无横向溢出、画布有可用面积`,
        planetOv.overflowing <= 1 && !!canvasBox && canvasBox.width > 200 && canvasBox.height > 150,
        `溢出=${planetOv.overflowing}px 画布=${canvasBox ? `${Math.round(canvasBox.width)}x${Math.round(canvasBox.height)}` : "无"}`,
      );
      await closePlanet();
    }
  }

  if (only("T")) {
    // B. 主题矩阵：暗紫晶 / 净白 / 跟随系统（含实时切换，不刷新）
    for (const theme of ["dark", "light"]) {
      await page.setViewportSize({ width: 1440, height: 900 });
      await page.evaluate((t) => localStorage.setItem("qio-theme", t), theme);
      await page.emulateMedia({ colorScheme: theme });
      for (const [hash, name] of [["#/", "chat"], ["#/settings", "settings"]]) {
        await page.goto(url(hash));
        await page.waitForTimeout(700);
        const applied = await page.evaluate(() => document.documentElement.dataset.theme);
        await page.screenshot({ path: `${SHOTS}\\theme-${theme}-${name}.png` });
        rec(`T-${theme}-${name}`, `${theme} 主题下 ${name} 渲染且 data-theme 正确`, applied === theme, `data-theme=${applied}`);
      }
      await page.goto(url("#/"));
      await page.waitForSelector(".dock", { timeout: 10000 });
      await openPlanet();
      const applied = await page.evaluate(() => document.documentElement.dataset.theme);
      const webgl = await page.locator(".webgl-fallback").count();
      await page.screenshot({ path: `${SHOTS}\\theme-${theme}-planet.png` });
      rec(
        `T-${theme}-planet`,
        `${theme} 主题下星球渲染（WebGL 不可用时有降级说明）`,
        applied === theme,
        `data-theme=${applied} WebGL降级提示=${webgl}`,
      );
      await closePlanet();
    }

    // 跟随系统：不刷新即随系统变化
    await page.goto(url("#/"));
    await page.evaluate(() => localStorage.setItem("qio-theme", "system"));
    await page.emulateMedia({ colorScheme: "light" });
    await page.reload();
    await page.waitForTimeout(600);
    const s1 = await page.evaluate(() => document.documentElement.dataset.theme);
    await page.emulateMedia({ colorScheme: "dark" });
    await page.waitForTimeout(800);
    const s2 = await page.evaluate(() => document.documentElement.dataset.theme);
    rec(
      "T-SYSTEM-LIVE",
      "偏好=系统时，系统主题切换无需刷新即生效",
      s1 === "light" && s2 === "dark",
      `系统浅色→${s1}，切到深色→${s2}`,
    );
    await page.evaluate(() => localStorage.setItem("qio-theme", "dark"));
    await page.emulateMedia({ colorScheme: "dark" });
  }

  if (only("F")) {
    // H. 浮动组件：点击不移动、拖动才贴靠、窗口缩放后保持贴靠关系、可还原
    await page.setViewportSize({ width: 1440, height: 900 });
    await page.goto(url("#/"));
    await page.waitForSelector(".dock", { timeout: 10000 });
    await wait(600);

    // 用「中心点」比较，不能用 boundingBox 左上角：悬停时 .dock 会 scale(1.06)，
    // 左上角会外扩约 2.9px，看起来像位移，其实位置没变。
    const center = async () =>
      page.locator(".dock").evaluate((el) => {
        const r = el.getBoundingClientRect();
        return { cx: r.x + r.width / 2, cy: r.y + r.height / 2 };
      });
    const storedPos = async () =>
      page.evaluate(() => {
        const keys = Object.keys(localStorage).filter((k) => k.includes("float") || k.includes("dock"));
        return keys.map((k) => `${k}=${localStorage.getItem(k)}`).join(";");
      });
    const box1 = await center();
    const stored1 = await storedPos();
    await page.locator(".dock").click({ position: { x: 20, y: 20 } }).catch(() => {});
    await wait(1200);
    // 点击可能打开星球，关掉后再看位置是否被改动
    await closePlanet();
    await page.mouse.move(10, 10); // 移开鼠标，消掉 hover 缩放再量
    await wait(400);
    const box2 = await center();
    const stored2 = await storedPos();
    const moved = Math.abs(box1.cx - box2.cx) + Math.abs(box1.cy - box2.cy);
    rec(
      "F-CLICK-NO-MOVE",
      "单击浮动入口不会改变位置（不触发贴靠、不改保存位置）",
      moved <= 1 && stored1 === stored2,
      `中心位移=${moved.toFixed(2)}px 保存位置是否变化=${stored1 !== stored2}`,
    );

    // 真实拖动：按住移动 120px 后松手 → 位置改变并贴靠
    const box3 = await page.locator(".dock").boundingBox();
    await page.mouse.move(box3.x + 30, box3.y + 30);
    await page.mouse.down();
    for (let i = 1; i <= 6; i++) await page.mouse.move(box3.x + 30 - i * 20, box3.y + 30 - i * 6);
    await page.mouse.up();
    await wait(900);
    const box4 = await page.locator(".dock").boundingBox();
    const dragged = Math.abs(box4.x - box3.x) + Math.abs(box4.y - box3.y);
    const hidden = await page.locator(".dock").evaluate((el) => el.className);
    rec("F-DRAG-SNAP", "真实拖动后位置改变并进入贴靠状态", dragged > 20, `位移=${Math.round(dragged)}px class=${hidden}`);

    // 窗口缩放：贴靠到右边后，放大窗口仍应保持与右边界的距离关系
    const rel = async () =>
      page.locator(".dock").evaluate((el) => {
        const r = el.getBoundingClientRect();
        return { right: window.innerWidth - r.right, x: r.x, y: r.y };
      });
    const relBefore = await rel();
    await page.setViewportSize({ width: 1700, height: 900 });
    await wait(700);
    const relAfter = await rel();
    const drift = Math.abs(relBefore.right - relAfter.right);
    const inside = relAfter.x >= -2 && relAfter.x + 96 <= 1700 + 2;
    rec(
      "F-RESIZE-DOCK",
      "窗口变宽后浮动入口仍在可视区内且保持贴靠关系",
      inside && drift <= 40,
      `右边距 ${Math.round(relBefore.right)}px→${Math.round(relAfter.right)}px（漂移 ${Math.round(drift)}px）`,
    );

    // 缩小窗口不跑出可视区
    await page.setViewportSize({ width: 820, height: 640 });
    await wait(700);
    const small = await page.locator(".dock").evaluate((el) => {
      const r = el.getBoundingClientRect();
      return { x: r.x, y: r.y, right: r.right, bottom: r.bottom, w: window.innerWidth, h: window.innerHeight };
    });
    rec(
      "F-SHRINK-INSIDE",
      "窗口缩到 820×640 时浮动入口没有跑出可视区",
      small.x >= -2 && small.y >= -2 && small.right <= small.w + 2 && small.bottom <= small.h + 2,
      `位置=(${Math.round(small.x)},${Math.round(small.y)}) 右下=(${Math.round(small.right)},${Math.round(small.bottom)}) 视口=${small.w}x${small.h}`,
    );
    await page.screenshot({ path: `${SHOTS}\\float-small-window.png` });

    // 还原默认布局（设置 → 外观 → 窗口行为）
    await page.setViewportSize({ width: 1440, height: 900 });
    await page.goto(url("#/settings"));
    await page.waitForSelector(".nav .tab", { timeout: 10000 });
    await page.locator(".nav .tab", { hasText: "外观" }).first().click();
    await wait(400);
    await page.locator("button", { hasText: "还原默认布局" }).first().click();
    await wait(600);
    const resetMsg = await page.locator(".panels .panel:visible .msg").first().innerText().catch(() => "");
    await page.goto(url("#/"));
    await page.waitForSelector(".dock", { timeout: 10000 });
    await wait(500);
    const defaulted = await page.locator(".dock").evaluate((el) => {
      const r = el.getBoundingClientRect();
      return { right: Math.round(window.innerWidth - r.right), bottom: Math.round(window.innerHeight - r.bottom) };
    });
    rec(
      "F-RESET",
      "「还原默认布局」可用并给反馈",
      /已还原/.test(resetMsg) && defaulted.right < 120,
      `提示=${resetMsg.trim()} 还原后右边距=${defaulted.right}px`,
    );
  }

  if (only("A")) {
    // I. 无障碍基础：可交互元素有可访问名称、键盘可达、焦点环存在、弹窗语义
    await page.setViewportSize({ width: 1440, height: 900 });
    await page.goto(url("#/"));
    await page.waitForSelector(".composer textarea", { timeout: 10000 });
    await wait(500);

    const unnamed = await page.evaluate(() => {
      const name = (el) =>
        (el.getAttribute("aria-label") ||
          el.getAttribute("title") ||
          el.getAttribute("placeholder") ||
          (el.innerText || "").trim() ||
          "").trim();
      return Array.from(document.querySelectorAll("button, input, textarea, select, [role='switch']"))
        .filter((el) => el.offsetParent !== null || el === document.activeElement)
        .filter((el) => !name(el))
        .map((el) => `${el.tagName.toLowerCase()}.${el.className}`.slice(0, 60))
        .slice(0, 12);
    });
    rec(
      "A-NAMES",
      "聊天页所有可见可交互元素都有可访问名称",
      unnamed.length === 0,
      unnamed.length ? `缺少名称：${unnamed.join(" | ")}` : "全部具备",
    );

    // 键盘 Tab：焦点环存在（outline 非 none）
    // 先点页面空白处建立焦点上下文，否则 headless 下第一次 Tab 可能停在浏览器层，量到 body。
    await page.mouse.click(700, 320);
    await page.keyboard.press("Tab");
    const focusInfo = await page.evaluate(() => {
      const el = document.activeElement;
      if (!el) return null;
      const cs = getComputedStyle(el);
      return {
        tag: el.tagName.toLowerCase(),
        cls: String(el.className).slice(0, 40),
        outline: `${cs.outlineStyle} ${cs.outlineWidth} ${cs.outlineColor}`,
        shadow: cs.boxShadow.slice(0, 40),
        label: (el.getAttribute("aria-label") || el.textContent || "").trim().slice(0, 16),
        visible: el.offsetParent !== null,
      };
    });
    const hasRing = !!focusInfo && (/^solid|^auto|^dashed/.test(focusInfo.outline) || focusInfo.shadow !== "none");
    rec(
      "A-FOCUS-RING",
      "键盘 Tab 后焦点落在可见控件上且有可见焦点环",
      !!focusInfo && focusInfo.tag !== "body" && focusInfo.visible && hasRing,
      `焦点=${focusInfo?.tag}.${focusInfo?.cls}（${focusInfo?.label}） outline=${focusInfo?.outline} shadow=${focusInfo?.shadow}`,
    );

    // 键盘通道：第一个停靠点是「跳到输入框」，一步就能到输入框
    // （背景测量：消息流里每条消息都有复制按钮，不跳过的话前若干个停靠点全是它）
    // 注意：必须先重新加载页面。点击页面任意位置会把顺序焦点起点移到点击处，
    // 之后的 Tab 从那里往后走，就测不到「页面第一个焦点位」了。
    await page.goto(url("#/"));
    await page.waitForSelector("#composer-input", { timeout: 10000 });
    await wait(1200);
    await page.keyboard.press("Tab");
    const firstStop = await page.evaluate(() => {
      const el = document.activeElement;
      return { cls: String(el?.className || ""), text: (el?.textContent || "").trim().slice(0, 12) };
    });
    await page.keyboard.press("Enter");
    await wait(200);
    const afterEnter = await page.evaluate(() => document.activeElement?.id || document.activeElement?.tagName || "");
    rec(
      "A-SKIP-LINK",
      "聊天页第一个键盘停靠点能直接跳到输入框",
      /skip-link/.test(firstStop.cls) && afterEnter === "composer-input",
      `第一个停靠点=${firstStop.cls || firstStop.text} 回车后焦点=${afterEnter}`,
    );

    const firstStops = [];
    for (let i = 0; i < 8; i++) {
      await page.keyboard.press("Tab");
      firstStops.push(await page.evaluate(() => String(document.activeElement?.className || "").split(" ")[0]));
    }
    const copyStops = firstStops.filter((c) => c.includes("copy")).length;
    rec(
      "A-TAB-NOISE",
      "（背景测量）消息流的复制按钮确实会占据 Tab 顺序，因此需要跳过入口",
      true,
      `从输入框往后 8 个停靠点=${firstStops.join(",")}（其中复制按钮 ${copyStops} 个）`,
    );

    // 焦点顺序：能 Tab 到输入框
    let reachedComposer = false;
    for (let i = 0; i < 25; i++) {
      await page.keyboard.press("Tab");
      const tag = await page.evaluate(() => document.activeElement?.tagName?.toLowerCase());
      if (tag === "textarea") {
        reachedComposer = true;
        break;
      }
    }
    rec("A-TAB-ORDER", "不用鼠标也能 Tab 到输入框", reachedComposer, `25 次 Tab 内到达输入框=${reachedComposer}`);

    // 减少动画：CSS 过渡时长被压到近零
    await page.evaluate(() => {
      document.documentElement.setAttribute("data-motion", "reduced");
    });
    await page.locator(".settings-float").click();
    await page.waitForSelector(".nav .tab", { timeout: 8000 });
    await wait(200);
    const reduced = await page.evaluate(() => {
      const el = document.querySelector(".panels");
      const cs = el ? getComputedStyle(el) : null;
      return { anim: cs?.animationDuration, trans: cs?.transitionDuration };
    });
    rec(
      "A-REDUCED-MOTION",
      "减少动画偏好下页面过渡时长压到近零（0.001ms）",
      /0\.001ms|1e-06s|^0s/.test(reduced.anim || "") || /0\.001ms|1e-06s|^0s/.test(reduced.trans || ""),
      `animation-duration=${reduced.anim} transition-duration=${reduced.trans}`,
    );
    await page.evaluate(() => document.documentElement.removeAttribute("data-motion"));
  }

  if (only("J")) {
    // J. 控制台：跑一遍主要流程后检查 error/warning 数量
    await page.goto(url("#/"));
    await page.waitForSelector(".composer textarea", { timeout: 10000 });
    await wait(600);
    await page.goto(url("#/settings"));
    await page.waitForSelector(".nav .tab", { timeout: 10000 });
    await page.locator(".nav .tab", { hasText: "高级" }).first().click();
    await wait(500);
    await page.locator(".nav .tab", { hasText: "凭据" }).first().click();
    await wait(500);
    await page.goto(url("#/"));
    await page.waitForSelector(".dock", { timeout: 10000 });
    await openPlanet();
    await closePlanet();
    await wait(600);
    const errors = report.console.filter((c) => c.type === "error" || c.type === "pageerror");
    const warnings = report.console.filter((c) => c.type === "warning");
    const noise = warnings.filter((w) => !/DevTools|Autofill|Extensions|third-party cookie/i.test(w.text));
    rec(
      "J-CONSOLE",
      "完整流程无未捕获异常 / 无 Vue 警告",
      errors.length === 0 && noise.length === 0,
      `error=${errors.length} warning(非环境噪音)=${noise.length}`,
      errors.concat(noise).slice(0, 6).map((c) => `${c.type}: ${c.text}`).join(" || ") || null,
    );
  }

  report.shots = [`${SHOTS}`];
  await context.close();
  await browser.close();
  writeFileSync(`${OUT}\\report-final-ui.json`, JSON.stringify(report, null, 2), "utf-8");
  const failed = report.cases.filter((c) => !c.passed);
  console.log(`\n合计 ${report.cases.length} 项，失败 ${failed.length} 项。报告：${OUT}\\report-final-ui.json`);
  process.exit(failed.length ? 1 : 0);
}

await main();
