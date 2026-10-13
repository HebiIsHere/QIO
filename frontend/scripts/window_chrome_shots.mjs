// 无边框窗口的自定义标题条：实拍 + 命中验证。
//
// 为什么要「命中验证」而不只是截图：标题条是窗口层（z-index 最高的那一层），
// 它一旦铺满顶部，底下那些贴顶的控件会变成「看得见、点不到」—— 截图看不出来，
// elementFromPoint 一看就露。本脚本对每个受影响控件做中心点命中，并把结果写进
// e2e-shots/window-chrome/results.json。
//
// 跑法：
//   python scripts/e2e_up.py            # 前端 5199 + 后端 8734（在仓库根目录）
//   node frontend/scripts/window_chrome_shots.mjs
// 也可以用 BASE=http://127.0.0.1:1420 node ... 指向 tauri dev 的 vite。
import { createRequire } from "node:module";
import { fileURLToPath } from "node:url";
import { mkdirSync, writeFileSync } from "node:fs";

const require = createRequire(import.meta.url);
// 本机可用的 Playwright（与 screenshot_planet.mjs / verify_planet.mjs 同一份运行时）
const PW = "C:\\Users\\zxy\\.cache\\codex-runtimes\\codex-primary-runtime\\dependencies\\node\\node_modules\\playwright";
const { chromium } = require(PW);

const OUT = fileURLToPath(new URL("../e2e-shots/window-chrome/", import.meta.url));
mkdirSync(OUT, { recursive: true });
const BASE = process.env.BASE || "http://127.0.0.1:5199/";

const browser = await chromium.launch({ channel: "msedge", headless: true });
const result = { base: BASE, logs: [], steps: [] };
try {
  const page = await browser.newPage({ viewport: { width: 1200, height: 800 } });
  // 假装在 Tauri 壳里：窗口按钮只在壳里渲染。stub 按 @tauri-apps/api 2.11 真正用到的东西给
  // （metadata.currentWindow.label / transformCallback / invoke），invoke 记下命令，
  // 用来确认按钮真的走到了壳接口。
  //
  // 注意 `qio_backend_info` 必须真的答一个端口：0.2.x 起 services/backend.ts 是
  // 「有桥就只认壳发布的连接信息」，只 stub 一个空壳会让界面永远停在启动握手
  // （实测踩过：页面只有一条「正在启动 QIO 后端」）。这里答 e2e_up.py 起的 8734。
  await page.addInitScript(() => {
    window.__calls = [];
    window.__cbs = {};
    window.__TAURI_INTERNALS__ = {
      metadata: {
        currentWindow: { label: "main" },
        currentWebview: { label: "main", windowLabel: "main" },
      },
      transformCallback: (cb) => {
        const id = Math.floor(Math.random() * 1e8);
        window.__cbs[id] = cb;
        return id;
      },
      unregisterCallback: () => {},
      convertFileSrc: (p) => p,
      invoke: async (cmd) => {
        window.__calls.push(cmd);
        if (cmd === "qio_backend_info") return { port: 8734, token: "" };
        if (cmd === "plugin:window|is_maximized") return false;
        if (cmd === "plugin:event|listen") return 1;
        return null;
      },
    };
  });
  page.on("console", (m) => result.logs.push(`${m.type()}: ${m.text()}`));
  page.on("pageerror", (e) => result.logs.push(`pageerror: ${e.message}`));

  await page.goto(BASE + "?wc=1", { waitUntil: "domcontentloaded", timeout: 30000 });
  await page.waitForSelector(".app-shell", { timeout: 20000 });
  // 等真正的首屏：只有它出现了，下面的几何与命中才有意义（否则量到的是启动提示）
  await page.waitForSelector(".conversation, .onboarding", { timeout: 30000 });
  await page.waitForTimeout(4000);
  // 首次运行会弹首次引导：如实拍一张，然后临时隐藏它，看底下的对话页
  if (await page.$(".onboarding")) {
    await page.screenshot({ path: OUT + "01-onboarding.png" });
    await page.addStyleTag({ content: ".onboarding{display:none !important}" });
    await page.waitForTimeout(300);
  }

  const probe = () =>
    page.evaluate(() => {
      const q = (s) => document.querySelector(s);
      const rectOf = (s) => {
        const e = q(s);
        if (!e) return null;
        const r = e.getBoundingClientRect();
        return { x: Math.round(r.x), y: Math.round(r.y), w: Math.round(r.width), h: Math.round(r.height) };
      };
      const hit = (s) => {
        const e = q(s);
        if (!e) return null;
        const r = e.getBoundingClientRect();
        const t = document.elementFromPoint(r.x + r.width / 2, r.y + r.height / 2);
        return {
          target: t ? `${t.tagName.toLowerCase()}.${(t.className || "").toString().split(" ")[0]}` : null,
          ok: !!(t && (t === e || e.contains(t))),
        };
      };
      const band = q(".window-chrome");
      return {
        band: rectOf(".window-chrome"),
        bandHeight: band ? getComputedStyle(band).height : null,
        buttons: [...document.querySelectorAll(".wc-btn")].map((b) => b.getAttribute("aria-label")),
        // 条上多点命中：整条（除按钮那一块）都必须还是拖拽面，任何一段被盖住都会拖不动窗口
        bandHitStrip: [60, 300, 600, Math.round(innerWidth / 2) + 100, innerWidth - 160].map((x) => {
          const t = document.elementFromPoint(x, 16);
          return {
            x,
            target: t ? `${t.tagName.toLowerCase()}.${(t.className || "").toString().split(" ")[0]}` : null,
            drag: t ? t.hasAttribute("data-tauri-drag-region") : false,
          };
        }),
        hits: {
          dragSurface: hit("[data-tauri-drag-region]"),
          minimize: hit('[aria-label="最小化"]'),
          maximize: hit('[aria-label="最大化"]'),
          close: hit(".wc-close"),
          gear: hit(".settings-float"),
        },
        conversation: rectOf(".conversation"),
        gearRect: rectOf(".settings-float"),
        planet: {
          view: rectOf(".planet-view"),
          closeBtn: rectOf(".planet-view .close-btn"),
          closeBtnHit: hit(".planet-view .close-btn"),
        },
      };
    });

  result.steps.push({ name: "1200x800 对话页", metrics: await probe() });
  await page.screenshot({ path: OUT + "02-conversation-1200x800.png" });

  await page.setViewportSize({ width: 800, height: 600 });
  await page.waitForTimeout(800);
  result.steps.push({ name: "800x600 对话页（安装包最小尺寸）", metrics: await probe() });
  await page.screenshot({ path: OUT + "03-conversation-800x600.png" });

  // 点右上角齿轮：证明它没被标题条压住（压住就点不到、路由不会变）
  await page.setViewportSize({ width: 1200, height: 800 });
  await page.waitForTimeout(500);
  const beforeHash = await page.evaluate(() => location.hash);
  await page.click(".settings-float");
  await page.waitForTimeout(1200);
  const afterHash = await page.evaluate(() => location.hash);
  result.steps.push({ name: "点齿轮", beforeHash, afterHash });
  await page.screenshot({ path: OUT + "04-settings.png" });

  // 星球页：整层 fixed inset:0，验证标题条仍在它之上（拖拽面与按钮都还在）
  await page.goto(BASE + "#/", { waitUntil: "domcontentloaded" });
  await page.waitForTimeout(3500);
  if (await page.$(".onboarding")) await page.addStyleTag({ content: ".onboarding{display:none !important}" });
  await page.waitForTimeout(300);
  if (await page.$(".dock")) {
    await page.click(".dock");
    await page.waitForTimeout(5000);
  }
  result.steps.push({ name: "星球页", metrics: await probe() });
  await page.screenshot({ path: OUT + "05-planet.png" });

  // 点三个窗口按钮：确认「前端 → @tauri-apps/api → 壳命令」这条链路真的通
  await page.evaluate(() => {
    window.__calls.length = 0;
  });
  for (const label of ["最小化", "最大化", "关闭"]) {
    const btn = await page.$(`.wc-btn[aria-label="${label}"]`);
    if (btn) await btn.click();
    await page.waitForTimeout(250);
  }
  result.steps.push({ name: "点三个窗口按钮", calls: await page.evaluate(() => window.__calls.slice()) });
  await page.screenshot({ path: OUT + "06-after-window-buttons.png" });

  // 结论式断言：脚本自己判红绿，别让读图的人去猜
  const checks = {
    "标题条高度 = 32px": result.steps.every((s) => !s.metrics || s.metrics.bandHeight === "32px"),
    "壳里有三个窗口按钮": result.steps.every((s) => !s.metrics || s.metrics.buttons.length === 3),
    "整条都是拖拽面": result.steps.every((s) => !s.metrics || s.metrics.bandHitStrip.every((h) => h.drag)),
    "页面内容让出条高（y=32）": result.steps.every((s) => !s.metrics || !s.metrics.conversation || s.metrics.conversation.y === 32),
    "条上按钮/拖拽面可命中": result.steps.every(
      (s) => !s.metrics || (s.metrics.hits.close?.ok && s.metrics.hits.minimize?.ok && s.metrics.hits.dragSurface?.ok),
    ),
    "设置齿轮可命中": result.steps.every((s) => !s.metrics || s.metrics.hits.gear?.ok || s.metrics.planet.view !== null),
    "点齿轮进设置页": result.steps.some((s) => s.afterHash === "#/settings"),
    "星球页「收起星球」可命中": result.steps.some((s) => s.metrics?.planet.closeBtnHit?.ok === true),
    "三个按钮打到壳命令": ["minimize", "toggle_maximize", "close"].every((c) =>
      result.steps.some((s) => s.calls?.includes(`plugin:window|${c}`)),
    ),
  };
  result.checks = checks;
  console.log(JSON.stringify(checks, null, 2));
  console.log(`失败项：${Object.entries(checks).filter(([, v]) => !v).map(([k]) => k).join("、") || "无"}`);
} finally {
  writeFileSync(OUT + "results.json", JSON.stringify(result, null, 2), "utf8");
  await browser.close();
}
