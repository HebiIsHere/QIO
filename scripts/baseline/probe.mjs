// 基线取证脚本（Playwright + 系统 Edge，无新增依赖）。
// 目的：把「历史录像里看到的现象」在当前代码上复现成可检验的事实，并留下截图与客观数字。
// 用法（服务需先起：backend 8734 + frontend 5199）：
//   node scripts/baseline/probe.mjs shots draft copy planet number queue anchor
// 证据输出：%TEMP%\qio-baseline\shots\*.png 与 <TEMP>\qio-baseline\report.json
// 说明：每个用例都用带新参数的地址打开（IAB/Vite 模块缓存不会重新校验），
//       并且只使用隔离数据目录中的假数据，不调用真实模型、不使用真实密钥。
import { createRequire } from "node:module";
import { mkdirSync, writeFileSync } from "node:fs";

const require = createRequire(import.meta.url);
const PW = "C:\\Users\\zxy\\.cache\\codex-runtimes\\codex-primary-runtime\\dependencies\\node\\node_modules\\playwright";
const { chromium } = require(PW);

const BASE = "http://127.0.0.1:5199";
const API = "http://127.0.0.1:8734";
const OUT = `${process.env.TEMP}\\qio-baseline`;
const SHOTS = `${OUT}\\shots`;
mkdirSync(SHOTS, { recursive: true });

const NAME = "基线-前端稳定化长对话与阅读连续性";
const wanted = process.argv.slice(2);
const only = (name) => wanted.length === 0 || wanted.includes(name);

const report = { startedAt: new Date().toISOString(), base: BASE, cases: [] };
function rec(id, title, passed, actual, detail) {
  report.cases.push({ id, title, passed, actual: String(actual), detail: detail ?? null });
  console.log(`[${passed ? "PASS" : "FAIL"}] ${id} ${title} :: ${actual}`);
}

let freshSeq = Math.floor(Date.now() / 1000) % 100000;
const freshUrl = (hash = "#/") => `${BASE}/?fresh=${++freshSeq}${hash}`;

const browser = await chromium.launch({
  channel: "msedge",
  headless: true,
  args: ["--use-gl=angle", "--use-angle=swiftshader", "--enable-unsafe-swiftshader"],
});

const context = await browser.newContext({ viewport: { width: 1440, height: 900 } });
await context.grantPermissions(["clipboard-read", "clipboard-write"], { origin: BASE }).catch(() => {});
const page = await context.newPage();
const consoleErrors = [];
page.on("console", (m) => { if (m.type() === "error") consoleErrors.push(m.text().slice(0, 300)); });
page.on("pageerror", (e) => consoleErrors.push("pageerror: " + String(e.message).slice(0, 300)));

async function shot(name, fullPage = false) {
  const buf = await page.screenshot({ fullPage });
  writeFileSync(`${SHOTS}\\${name}.png`, buf);
  return `${SHOTS}\\${name}.png`;
}

async function openChat() {
  await page.goto(freshUrl("#/"), { waitUntil: "domcontentloaded" });
  await page.waitForSelector(".composer textarea", { timeout: 15000 });
  await page.waitForTimeout(1200);
}

async function openSettings(section = null) {
  if (!page.url().includes("/settings")) {
    await page.goto(freshUrl("#/settings"), { waitUntil: "domcontentloaded" });
  }
  await page.waitForSelector(".nav button.tab", { timeout: 15000 });
  await page.waitForTimeout(600);
  if (section) {
    await page.locator(".nav button.tab", { hasText: section }).first().click();
    await page.waitForTimeout(400);
  }
}

// ---------------------------------------------------------------- shots
if (only("shots")) {
  try {
    await page.setViewportSize({ width: 1440, height: 900 });
    await openChat();
    await page.locator(".stream").evaluate((el) => { el.scrollTop = 0; });
    await page.waitForTimeout(400);
    const p1 = await shot("wide-chat");
    await openSettings();
    const p2 = await shot("wide-settings-appearance");
    await openSettings("凭据");
    const p3 = await shot("wide-settings-credentials");
    await page.goto(freshUrl("#/"), { waitUntil: "domcontentloaded" });
    await page.waitForSelector(".dock", { timeout: 15000 });
    await page.waitForTimeout(800);
    await page.locator(".dock").click();
    await page.waitForTimeout(2500);
    const topics = await page.locator(".topic-list li").count();
    const p4 = await shot("wide-planet");
    await page.setViewportSize({ width: 880, height: 700 });
    await page.waitForTimeout(800);
    const p5 = await shot("narrow-planet");
    await page.locator(".close-btn").click();
    await page.waitForTimeout(1600);
    await openChat();
    const p6 = await shot("narrow-chat");
    await openSettings();
    const p7 = await shot("narrow-settings");
    rec("BASE-SHOTS", "正常/窄窗口截图", true,
      `chat/settings/planet 各尺寸已截图；宽屏星球话题点=${topics}`,
      [p1, p2, p3, p4, p5, p6, p7].join(" | "));
  } catch (e) {
    rec("BASE-SHOTS", "正常/窄窗口截图", false, e.message.slice(0, 200));
  }
}

// ---------------------------------------------------------------- draft 导航/发送失败
if (only("draft")) {
  try {
    await page.setViewportSize({ width: 1440, height: 900 });
    await openChat();
    await page.fill(".composer textarea", "121345");
    await page.locator(".settings-float").click();
    await page.waitForSelector(".back", { timeout: 10000 });
    await page.waitForTimeout(700);
    await page.locator(".back").click();
    await page.waitForSelector(".composer textarea", { timeout: 10000 });
    await page.waitForTimeout(800);
    const after = await page.inputValue(".composer textarea");
    rec("DRAFT-NAV", "切到设置再返回：草稿是否保留", after === "121345",
      `返回后输入框='${after}'（期望 121345）`);
  } catch (e) {
    rec("DRAFT-NAV", "切到设置再返回：草稿是否保留", false, e.message.slice(0, 200));
  }
  try {
    await page.unroute("**/api/turns**").catch(() => {});
    await page.route("**/api/turns**", (route) => route.abort("connectionfailed"));
    await openChat();
    await page.fill(".composer textarea", "这条发送会失败-121345");
    await page.locator(".send-btn").click();
    await page.waitForTimeout(2000);
    const after = await page.inputValue(".composer textarea");
    const err = await page.locator(".notice.err, .cancel-error").count();
    rec("DRAFT-SEND-FAIL", "发送失败后草稿是否还在输入框里", after.includes("121345"),
      `输入框='${after}'，错误提示元素=${err}（期望草稿保留）`);
  } catch (e) {
    rec("DRAFT-SEND-FAIL", "发送失败后草稿是否还在输入框里", false, e.message.slice(0, 200));
  } finally {
    await page.unroute("**/api/turns**").catch(() => {});
  }
}

// ---------------------------------------------------------------- 复制反馈
if (only("copy")) {
  // A：剪贴板写入被拒绝
  try {
    const p = await context.newPage();
    await p.addInitScript(() => {
      Object.defineProperty(navigator, "clipboard", {
        configurable: true,
        value: { writeText: () => Promise.reject(new Error("clipboard denied")) },
      });
    });
    await p.goto(freshUrl("#/"), { waitUntil: "domcontentloaded" });
    await p.waitForSelector(".code-block .code-copy", { timeout: 15000 });
    await p.waitForTimeout(800);
    const btn = p.locator(".code-block .code-copy").first();
    await btn.click({ force: true });
    await p.waitForTimeout(300);
    const txt = (await btn.textContent())?.trim();
    rec("COPY-FAIL", "剪贴板写入失败时代码按钮文案", txt !== "已复制",
      `失败后按钮文案='${txt}'（期望提示失败，而不是已复制）`);
    const clipInfo = await p.evaluate(() => {
      const el = document.querySelector(".code-block .code-copy");
      if (!el) return "no-button";
      el.scrollIntoView({ block: "center" });
      return "scrollable";
    });
    await p.close();
    report.codeCopyScroll = clipInfo;
  } catch (e) {
    rec("COPY-FAIL", "剪贴板写入失败时代码按钮文案", false, e.message.slice(0, 200));
  }
  // B：剪贴板对象不存在
  try {
    const p = await context.newPage();
    await p.addInitScript(() => {
      Object.defineProperty(navigator, "clipboard", { configurable: true, value: undefined });
    });
    await p.goto(freshUrl("#/"), { waitUntil: "domcontentloaded" });
    await p.waitForSelector(".code-block .code-copy", { timeout: 15000 });
    await p.waitForTimeout(800);
    const btn = p.locator(".code-block .code-copy").first();
    await btn.click({ force: true });
    await p.waitForTimeout(300);
    const txt = (await btn.textContent())?.trim();
    rec("COPY-NOCLIP", "无剪贴板 API 时代码按钮文案", txt !== "已复制",
      `按钮文案='${txt}'（期望提示不可用，而不是已复制）`);
    await p.close();
  } catch (e) {
    rec("COPY-NOCLIP", "无剪贴板 API 时代码按钮文案", false, e.message.slice(0, 200));
  }
  // C：剪贴板可用时，复制内容是否与代码一致
  try {
    await openChat();
    const code = (await page.locator(".code-block pre code").first().textContent()) ?? "";
    let before = null;
    try { before = await page.evaluate(() => navigator.clipboard.readText()); } catch (e) { before = `read-failed: ${e.message}`; }
    await page.locator(".code-block pre").first().hover();
    await page.locator(".code-block .code-copy").first().click();
    await page.waitForTimeout(600);
    const btnTxt = (await page.locator(".code-block .code-copy").first().textContent())?.trim();
    let clip = null;
    try { clip = await page.evaluate(() => navigator.clipboard.readText()); } catch (e) { clip = `read-failed: ${e.message}`; }
    // Windows 剪贴板会把 \n 规范化为 \r\n：比较前统一换行符
    const norm = (s) => s.replace(/\r\n/g, "\n").trim();
    const same = typeof clip === "string" && norm(clip) === norm(code);
    let diffInfo = "";
    if (typeof clip === "string") {
      let i = 0;
      while (i < Math.min(clip.length, code.length) && clip[i] === code[i]) i += 1;
      diffInfo = `首个差异位置=${i} 剪贴板附近=${JSON.stringify(clip.slice(Math.max(0, i - 20), i + 20))} 代码附近=${JSON.stringify(code.slice(Math.max(0, i - 20), i + 20))}`;
    }
    rec("COPY-CONTENT", "复制内容是否与代码块一致", same,
      `按钮='${btnTxt}' 复制前剪贴板长度=${typeof before === "string" ? before.length : String(before)} 复制后=${typeof clip === "string" ? clip.length : String(clip)} 代码长度=${code.length}`,
      `剪贴板首行=${typeof clip === "string" ? JSON.stringify(clip.slice(0, 60)) : String(clip)}；代码首行=${JSON.stringify(code.slice(0, 60))}；${diffInfo}`);
  } catch (e) {
    rec("COPY-CONTENT", "复制内容是否与代码块一致", false, e.message.slice(0, 200));
  }
}

// ---------------------------------------------------------------- 星球详情失败
if (only("planet")) {
  try {
    await page.setViewportSize({ width: 1440, height: 900 });
    await page.route("**/api/graph/topics/**", (route) =>
      route.fulfill({ status: 500, contentType: "application/json", body: '{"detail":"boom"}' }));
    await page.goto(freshUrl("#/"), { waitUntil: "domcontentloaded" });
    await page.waitForSelector(".dock", { timeout: 15000 });
    await page.waitForTimeout(900);
    await page.locator(".dock").click();
    await page.waitForTimeout(2500);
    await page.locator(".panel-toggle").click();
    await page.waitForTimeout(600);
    await page.locator(".topic-list li").first().click();
    await page.waitForTimeout(2500);
    const loading = await page.locator(".detail-loading").count();
    const detail = await page.locator(".detail").count();
    const err = await page.locator(".detail-error").count();
    const errText = err ? ((await page.locator(".detail-error").first().textContent()) ?? "").trim() : "";
    const errVisible = err ? await page.locator(".detail-error").first().isVisible() : false;
    const shotPath = await shot("planet-detail-error");
    const bodyText = ((await page.locator(".panel-inner").innerText()) ?? "").replace(/\s+/g, " ").slice(0, 200);
    rec("PLANET-DETAIL-ERR", "详情加载失败时错误是否可见", err > 0 && errVisible,
      `loading=${loading} detail=${detail} detail-error=${err} visible=${errVisible} 文本='${errText}'`,
      `面板可见文本=${bodyText} | ${shotPath}`);
    const retry = await page.locator(".detail-error button", { hasText: "重试" }).count();
    rec("PLANET-DETAIL-RETRY", "详情加载失败是否提供重试入口", retry > 0,
      `页面上「重试」相关元素=${retry}`);
  } catch (e) {
    rec("PLANET-DETAIL-ERR", "详情加载失败时错误是否可见", false, e.message.slice(0, 200));
  } finally {
    await page.unroute("**/api/graph/topics/**").catch(() => {});
  }
  // 关闭耗时：点「收起星球」到覆盖层从 DOM 移除
  try {
    await page.goto(freshUrl("#/"), { waitUntil: "domcontentloaded" });
    await page.waitForSelector(".dock", { timeout: 15000 });
    await page.waitForTimeout(900);
    await page.locator(".dock").click();
    await page.waitForTimeout(2600);
    const ms = await page.evaluate(async () => {
      const t0 = performance.now();
      document.querySelector(".close-btn")?.click();
      return await new Promise((res) => {
        const tick = () => {
          if (!document.querySelector(".planet-view")) res(Math.round(performance.now() - t0));
          else requestAnimationFrame(tick);
        };
        tick();
      });
    });
    report.planetCloseMs = ms;
    await page.waitForTimeout(400);
    // 侧栏开合后的二次居中：连续两次快速开合，观察是否出现「动画结束后再跳一次」
    await page.locator(".dock").click();
    await page.waitForTimeout(2600);
    await page.locator(".panel-toggle").click();
    await page.waitForTimeout(600);
    await page.locator(".topic-list li").first().click();
    await page.waitForTimeout(1400);
    const samples = [];
    const sampler = async () => {
      for (let i = 0; i < 40; i++) {
        samples.push(await page.evaluate(() => {
          const p = document.querySelector(".panel");
          const c = document.querySelector(".planet-canvas");
          return { t: Math.round(performance.now()), w: p ? Math.round(p.getBoundingClientRect().width) : -1, cw: c ? Math.round(c.getBoundingClientRect().width) : -1 };
        }));
        await page.waitForTimeout(25);
      }
    };
    await page.locator(".panel-toggle").click();
    await sampler();
    const first = samples.find((s) => s.w > 0);
    const last = samples[samples.length - 1];
    report.panelOpenWidths = { first, last, count: samples.length };
    rec("PLANET-CLOSE", "关闭星球总时长（含相机拉回与淡出）", ms < 900,
      `点击到覆盖层移除=${ms}ms`, "目标：短而平稳，避免 700ms 级别的默认移动");
    await shot("planet-panel-open");
  } catch (e) {
    rec("PLANET-CLOSE", "关闭星球总时长（含相机拉回与淡出）", false, e.message.slice(0, 200));
  }
  // 减少动画偏好下，脚本动画是否也缩短
  try {
    const p = await context.newPage();
    await p.emulateMedia({ reducedMotion: "reduce" });
    await p.goto(freshUrl("#/"), { waitUntil: "domcontentloaded" });
    await p.waitForSelector(".dock", { timeout: 15000 });
    await p.waitForTimeout(900);
    await p.locator(".dock").click();
    await p.waitForTimeout(2600);
    const ms = await p.evaluate(async () => {
      const t0 = performance.now();
      document.querySelector(".close-btn")?.click();
      return await new Promise((res) => {
        const tick = () => {
          if (!document.querySelector(".planet-view")) res(Math.round(performance.now() - t0));
          else requestAnimationFrame(tick);
        };
        tick();
      });
    });
    await p.close();
    report.planetCloseMsReducedMotion = ms;
    rec("PLANET-REDUCED", "prefers-reduced-motion 下脚本动画是否缩短", ms < 300,
      `减少动画偏好下关闭耗时=${ms}ms（与普通模式 ${report.planetCloseMs ?? "n/a"}ms 对比）`);
  } catch (e) {
    rec("PLANET-REDUCED", "prefers-reduced-motion 下脚本动画是否缩短", false, e.message.slice(0, 200));
  }
}

// ---------------------------------------------------------------- 数字输入提交
if (only("number")) {
  try {
    await page.setViewportSize({ width: 1440, height: 900 });
    const calls = [];
    await page.route("**/api/settings/loop", (route) => {
      if (route.request().method() === "PUT") calls.push(route.request().postData() ?? "");
      return route.continue();
    });
    await openSettings("对话与记忆");
    const input = page.locator('.q-number input[aria-label="迭代上限"]');
    await input.waitFor({ timeout: 10000 });
    const before = await input.inputValue();
    await input.click();
    await page.keyboard.press("ControlOrMeta+a");
    await input.type("200");
    await page.waitForTimeout(150);
    const midValue = await input.inputValue();
    await page.locator(".nav button.tab", { hasText: "外观" }).first().click();
    await page.waitForTimeout(1200);
    rec("NUM-MANUAL", "手工输入数字后失焦是否发起保存", calls.length > 0,
      `初始=${before} 输入后=${midValue} PUT /api/settings/loop 次数=${calls.length}`,
      `请求体=${calls.join(" ; ").slice(0, 200)}`);
    const notice = await page.locator(".msg").allTextContents();
    report.numberNotice = notice;
    await page.reload({ waitUntil: "domcontentloaded" });
    await page.waitForSelector(".nav button.tab", { timeout: 15000 });
    await page.waitForTimeout(600);
    await page.locator(".nav button.tab", { hasText: "对话与记忆" }).first().click();
    await page.waitForTimeout(900);
    const after = await page.locator('.q-number input[aria-label="迭代上限"]').inputValue();
    const server = await (await fetch(`${API}/api/settings/loop`)).json();
    rec("NUM-MANUAL-PERSIST", "手工输入的值是否被持久化（真实刷新后）", after === "200" && server.max_iterations === 200,
      `刷新后输入框='${after}' 服务端 max_iterations=${server.max_iterations}（期望 200）`);
  } catch (e) {
    rec("NUM-MANUAL", "手工输入数字后失焦是否发起保存", false, e.message.slice(0, 200));
  } finally {
    await page.unroute("**/api/settings/loop").catch(() => {});
  }
  // 步进按钮（回归基线）
  try {
    const calls = [];
    await page.route("**/api/settings/loop", (route) => {
      if (route.request().method() === "PUT") calls.push(route.request().postData() ?? "");
      return route.continue();
    });
    await openSettings("对话与记忆");
    const input = page.locator('.q-number input[aria-label="迭代上限"]');
    await input.waitFor({ timeout: 10000 });
    const before = await input.inputValue();
    await page.locator('.q-number:has(input[aria-label="迭代上限"]) .step.up').click();
    await page.waitForTimeout(1200);
    const after = await input.inputValue();
    rec("NUM-STEP", "步进按钮提交（回归）", calls.length > 0 && after !== before,
      `点击 + 前=${before} 后=${after} PUT 次数=${calls.length}`);
    await page.unroute("**/api/settings/loop").catch(() => {});
  } catch (e) {
    rec("NUM-STEP", "步进按钮提交（回归）", false, e.message.slice(0, 200));
  }
}

// ---------------------------------------------------------------- 任务开始强制跟随 + 排队失败归属
if (only("queue")) {
  try {
    await openChat();
    await page.locator(".stream").evaluate((el) => { el.scrollTop = 0; });
    await page.waitForTimeout(600);
    const topBefore = await page.locator(".stream").evaluate((el) => Math.round(el.scrollTop));
    await fetch(`${API}/api/events/test?event_type=TURN_START`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ turn_id: "turn_baseline_probe" }),
    });
    await page.waitForTimeout(900);
    const topAfter = await page.locator(".stream").evaluate((el) => Math.round(el.scrollTop));
    const stopVisible = await page.locator(".stop-btn").count();
    rec("QUEUE-FOLLOW", "后台任务开始不打断向上阅读的位置", topAfter <= topBefore + 200,
      `TURN_START 前 scrollTop=${topBefore} 后=${topAfter} 停止按钮=${stopVisible}`,
      "期望：非本机发起的任务开始不把阅读位置拉到底部");
    await fetch(`${API}/api/events/test?event_type=TURN_END`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ turn_id: "turn_baseline_probe" }),
    }).catch(() => {});
    await page.waitForTimeout(500);
  } catch (e) {
    rec("QUEUE-FOLLOW", "任务开始（含非本机发起）是否强制拉回底部", false, e.message.slice(0, 200));
  }
  try {
    await page.route("**/api/turns**", (route) => route.abort("connectionfailed"));
    await openChat();
    await fetch(`${API}/api/events/test?event_type=TURN_START`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ turn_id: "turn_baseline_running" }),
    });
    await page.waitForTimeout(600);
    const runningBefore = await page.locator(".stop-btn").count();
    await page.fill(".composer textarea", "排队发送会失败");
    await page.locator(".send-btn").click();
    await page.waitForTimeout(1800);
    const runningAfter = await page.locator(".stop-btn").count();
    const queuedTag = await page.locator(".queued-tag").count();
    rec("QUEUE-FAIL", "排队请求失败不影响仍在运行的任务状态",
      runningBefore > 0 && runningAfter > 0,
      `失败前停止按钮=${runningBefore} 失败后=${runningAfter}（期望仍在运行） 等待中标记=${queuedTag}`);
  } catch (e) {
    rec("QUEUE-FAIL", "排队请求失败是否误清除仍在运行的任务状态", false, e.message.slice(0, 200));
  } finally {
    await page.unroute("**/api/turns**").catch(() => {});
    await fetch(`${API}/api/events/test?event_type=TURN_END`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ turn_id: "turn_baseline_running" }),
    }).catch(() => {});
  }
}

// ---------------------------------------------------------------- 起点请求中关闭/重开 + 后台审批打断表单
if (only("anchor")) {
  try {
    await page.route("**/api/anchor", async (route) => {
      await new Promise((r) => setTimeout(r, 1500));
      await route.fulfill({ status: 500, contentType: "application/json", body: '{"detail":"anchor boom"}' });
    });
    await page.goto(freshUrl("#/"), { waitUntil: "domcontentloaded" });
    await page.waitForSelector(".dock", { timeout: 15000 });
    await page.waitForTimeout(900);
    await page.locator(".dock").click();
    await page.waitForTimeout(2600);
    await page.locator(".panel-toggle").click();
    await page.waitForTimeout(600);
    await page.locator(".topic-list li").nth(2).click();
    await page.waitForSelector(".start-btn", { timeout: 10000 });
    await page.locator(".start-btn").click();
    await page.waitForTimeout(300);
    await page.locator(".close-btn").click();
    await page.waitForTimeout(2600);
    const planetGone = (await page.locator(".planet-view").count()) === 0;
    const composer = await page.locator(".composer textarea").count();
    const visibleError = await page.locator(".notice.err").allTextContents();
    rec("ANCHOR-INFLIGHT", "起点请求进行中关闭星球：状态一致且失败仍可见",
      planetGone && composer > 0 && visibleError.length > 0 && visibleError.join("|").includes("未切换话题"),
      `覆盖层已关闭=${planetGone} 输入区可用=${composer > 0} 对话页错误='${visibleError.join("|")}'`,
      "失败发生在关闭之后：错误必须回到对话页可见，否则用户以为起点已经切好");
    await page.waitForTimeout(1600);
    const errAfter = await page.locator(".notice.err, .anchor-error").allTextContents();
    report.anchorErrorAfter = errAfter;
  } catch (e) {
    rec("ANCHOR-INFLIGHT", "起点请求进行中关闭星球后的状态一致性", false, e.message.slice(0, 200));
  } finally {
    await page.unroute("**/api/anchor").catch(() => {});
  }
  try {
    await openSettings("凭据");
    const newBtn = page.getByText("新建凭据", { exact: false }).first();
    if (await newBtn.count()) {
      await newBtn.click();
      await page.waitForTimeout(500);
    }
    const formBefore = await page.locator(".modal-mask").count();
    // 先在表单里写字：用来验证「后台审批打断表单」是否真的丢内容
    const noteInput = page.locator('input[placeholder="可选，如 GPT-5 Studio"]');
    if (await noteInput.count()) {
      await noteInput.fill("正在填写的备注-基线条目");
    }
    await fetch(`${API}/api/events/test?event_type=APPROVAL_REQUIRED`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ approval: { approval_id: "apr_baseline_1", kind: "tool_create", payload: { name: "基线-假工具", explanation: "基线取证用的假审批" } } }),
    });
    await page.waitForTimeout(900);
    const approveModal = await page.locator('[role="dialog"]').count();
    const focused = await page.evaluate(() => {
      const el = document.activeElement;
      return el ? `${el.tagName}:${(el.getAttribute("aria-label") || el.textContent || "").slice(0, 30)}` : "none";
    });
    const shotPath = await shot("approval-over-credential-form");
    rec("APPROVAL-OVERLAY", "后台审批是否打断正在填写的表单", formBefore > 0 && approveModal > 0,
      `凭据弹窗=${formBefore} 审批弹窗=${approveModal} 焦点=${focused}`,
      `证据截图=${shotPath}` + (formBefore === 0 ? "（未打开凭据弹窗，本次仅记录审批弹窗出现）" : ""));
    await page.locator('[role="dialog"] button').filter({ hasText: "拒绝" }).first().click().catch(() => {});
    await page.waitForTimeout(1000);
    const noteAfter = (await noteInput.count()) ? await noteInput.inputValue() : "";
    const focusAfter = await page.evaluate(() => {
      const el = document.activeElement;
      return el ? `${el.tagName}` : "none";
    });
    const dialogsAfter = await page.locator('[role="dialog"]').count();
    rec("APPROVAL-FORM-KEEP", "关闭审批后表单内容与焦点是否还在", noteAfter === "正在填写的备注-基线条目",
      `备注='${noteAfter}' 焦点标签=${focusAfter} 仍在的对话框=${dialogsAfter}`,
      dialogsAfter > 1
        ? "假 approval_id 无法被后端受理（正确行为：失败保留待审批项），因此关闭路径的焦点恢复在本轮未做端到端确认"
        : "对话框已关闭");
  } catch (e) {
    rec("APPROVAL-OVERLAY", "后台审批是否打断正在填写的表单", false, e.message.slice(0, 200));
  }
}

// ---------------------------------------------------------------- 知识页默认筛选
if (only("knowledge")) {
  try {
    await page.goto(freshUrl("#/"), { waitUntil: "domcontentloaded" });
    await page.waitForSelector(".dock", { timeout: 15000 });
    await page.waitForTimeout(900);
    await page.locator(".dock").click();
    await page.waitForTimeout(2600);
    await page.locator(".panel-toggle").click();
    await page.waitForTimeout(600);
    await page.locator(".tab-knowledge").click();
    await page.waitForTimeout(1600);
    const selects = await page.locator(".filters .qio-select .qio-select-val").allTextContents();
    const items = await page.locator(".k-item").count();
    const api = await (await fetch(`${API}/api/knowledge`)).json();
    const shotPath = await shot("planet-knowledge-tab");
    rec("KNOW-FILTER", "知识页筛选框是否有可见标签/选项", selects.every((s) => s.trim().length > 0),
      `筛选框关闭态可见文本=${JSON.stringify(selects)} 列表条目=${items} 后端条目=${(api.knowledge ?? []).length}`,
      `截图=${shotPath}`);
    const optionLists = [];
    for (let i = 0; i < 2; i++) {
      await page.locator(".filters .qio-select").nth(i).click();
      await page.waitForTimeout(400);
      optionLists.push(await page.locator(".qio-select.open .opt").allTextContents());
      await shot(`planet-knowledge-select-${i}`);
      await page.locator(".filters .qio-select").nth(i).click();
      await page.waitForTimeout(200);
    }
    const hasReset = optionLists.every((o) => o.some((t) => /全部|不限|清除/.test(t)));
    rec("KNOW-FILTER-OPTS", "筛选框展开后是否有可选项/是否有「全部」回退项",
      optionLists.every((o) => o.length > 0) && hasReset,
      `展开项=${JSON.stringify(optionLists)} 有回退项=${hasReset}`);
  } catch (e) {
    rec("KNOW-FILTER", "知识页筛选框是否有可见标签/选项", false, e.message.slice(0, 200));
  }
}

// ---------------------------------------------------------------- 任务01 状态一致性
if (only("t01")) {
  // 1) 草稿 → 星球 → 返回
  try {
    await page.setViewportSize({ width: 1440, height: 900 });
    await openChat();
    await page.fill(".composer textarea", "星球往返草稿");
    await page.locator(".dock").click();
    await page.waitForTimeout(2600);
    await page.locator(".close-btn").click();
    await page.waitForSelector(".planet-view", { state: "detached", timeout: 8000 });
    await page.waitForTimeout(600);
    const v = await page.inputValue(".composer textarea");
    rec("T01-DRAFT-PLANET", "草稿 → 星球 → 返回：草稿保留", v === "星球往返草稿", `返回后='${v}'`);
  } catch (e) {
    rec("T01-DRAFT-PLANET", "草稿 → 星球 → 返回：草稿保留", false, e.message.slice(0, 200));
  }
  // 2) 阅读位置：向上滚动 → 设置 → 返回
  try {
    await openChat();
    await page.locator(".stream").evaluate((el) => {
      el.scrollTop = 300;
      el.dispatchEvent(new Event("scroll"));
    });
    await page.waitForTimeout(400);
    const before = await page.locator(".stream").evaluate((el) => Math.round(el.scrollTop));
    await page.locator(".settings-float").click();
    await page.waitForSelector(".back", { timeout: 10000 });
    await page.waitForTimeout(600);
    await page.locator(".back").click();
    await page.waitForSelector(".stream", { timeout: 10000 });
    await page.waitForTimeout(900);
    const after = await page.locator(".stream").evaluate((el) => Math.round(el.scrollTop));
    rec("T01-SCROLL", "阅读位置：向上阅读 → 设置 → 返回后仍在原位置", Math.abs(after - before) <= 40,
      `之前=${before} 之后=${after}`);
  } catch (e) {
    rec("T01-SCROLL", "阅读位置：向上阅读 → 设置 → 返回后仍在原位置", false, e.message.slice(0, 200));
  }
  // 3) 设置分类记忆
  try {
    await page.locator(".settings-float").click();
    await page.waitForSelector(".nav button.tab", { timeout: 10000 });
    await page.locator(".nav button.tab", { hasText: "凭据" }).first().click();
    await page.waitForTimeout(400);
    const first = (await page.locator(".tab.active").textContent())?.trim();
    await page.locator(".back").click();
    await page.waitForSelector(".composer textarea", { timeout: 10000 });
    await page.waitForTimeout(500);
    await page.locator(".settings-float").click();
    await page.waitForSelector(".nav button.tab", { timeout: 10000 });
    await page.waitForTimeout(500);
    const second = (await page.locator(".tab.active").textContent())?.trim();
    rec("T01-SETTINGS-TAB", "再次进入设置恢复上次分类", first === "凭据" && second === "凭据",
      `第一次=${first} 再次进入=${second}`);
    await page.locator(".back").click();
  } catch (e) {
    rec("T01-SETTINGS-TAB", "再次进入设置恢复上次分类", false, e.message.slice(0, 200));
  }
  // 4) 星球首次数据加载失败：可见失败 + 重试
  try {
    let fail = true;
    await page.route(
      (url) => url.pathname === "/api/graph/topics",
      (route) =>
        fail
          ? route.fulfill({ status: 500, contentType: "application/json", body: '{"detail":"offline"}' })
          : route.continue(),
    );
    await page.goto(freshUrl("#/"), { waitUntil: "domcontentloaded" });
    await page.waitForSelector(".dock", { timeout: 15000 });
    await page.waitForTimeout(900);
    await page.locator(".dock").click();
    await page.waitForTimeout(2000);
    const bannerCount = await page.locator(".load-error").count();
    const bannerText = bannerCount ? ((await page.locator(".load-error").textContent()) ?? "").trim() : "";
    fail = false;
    let retried = 0;
    if (bannerCount) {
      await page.locator(".load-error button").click();
      await page.waitForTimeout(1500);
    }
    retried = await page.locator(".topic-list li").count();
    const bannerAfter = await page.locator(".load-error").count();
    rec("T01-PLANET-LOAD-FAIL", "星球首次加载失败可见且可重试",
      bannerCount > 0 && bannerAfter === 0 && retried > 0,
      `失败提示=${bannerCount}（${bannerText.slice(0, 40)}）重试后话题=${retried} 提示仍在=${bannerAfter}`);
    await shot("planet-load-error-retry");
  } catch (e) {
    rec("T01-PLANET-LOAD-FAIL", "星球首次加载失败可见且可重试", false, e.message.slice(0, 200));
  } finally {
    await page.unroute((url) => url.pathname === "/api/graph/topics").catch(() => {});
  }
  // 5) 触屏（无 hover）下复制入口是否可见
  try {
    const touchCtx = await browser.newContext({ viewport: { width: 900, height: 800 }, hasTouch: true, isMobile: false });
    const p = await touchCtx.newPage();
    await p.goto(freshUrl("#/"), { waitUntil: "domcontentloaded" });
    await p.waitForSelector(".code-copy", { timeout: 15000 });
    await p.waitForTimeout(800);
    const opacity = await p.locator(".code-block .code-copy").first().evaluate(
      (el) => getComputedStyle(el).opacity,
    );
    const copyOpacity = await p.locator(".copy-btn").first().evaluate((el) => getComputedStyle(el).opacity);
    await touchCtx.close();
    rec("T01-TOUCH-COPY", "触屏（无悬停）下复制入口默认可见", opacity === "1" && copyOpacity === "1",
      `代码复制按钮 opacity=${opacity} 消息复制按钮 opacity=${copyOpacity}`);
  } catch (e) {
    rec("T01-TOUCH-COPY", "触屏（无悬停）下复制入口默认可见", false, e.message.slice(0, 200));
  }
}

// ---------------------------------------------------------------- 任务02 统一反馈与动画规则
if (only("t02")) {
  const cssOf = (sel, prop) =>
    page.locator(sel).first().evaluate((el, p) => getComputedStyle(el).getPropertyValue(p), prop);

  // 1) 时长令牌是否落到实际元素上
  try {
    await page.setViewportSize({ width: 1440, height: 900 });
    await openSettings("外观");
    const pageAnim = await cssOf(".settings", "animation-duration");
    const btnTrans = await cssOf(".qio-btn", "transition-duration");
    const btnProps = await cssOf(".qio-btn", "transition-property");
    const motionBtns = await page.locator(".motion-opt").allTextContents();
    rec("T02-TOKENS", "公共时长令牌落到设置页与基础按钮",
      pageAnim.includes("0.22") && btnProps.includes("transform"),
      `设置页入场 animation-duration=${pageAnim}；.qio-btn transition-property=${btnProps} duration=${btnTrans}`,
      `动画偏好选项=${JSON.stringify(motionBtns)}`);
  } catch (e) {
    rec("T02-TOKENS", "公共时长令牌落到设置页与基础按钮", false, e.message.slice(0, 200));
  }

  // 2) 下拉菜单：出现后有可见变化，关闭后不再拦截点击
  try {
    await openSettings("外观");
    // 只看当前可见的面板：v-show 隐藏的面板里也有 .qio-select，first() 可能命中不可见元素
    const combo = page.locator(".panel:visible .qio-select").first();
    await combo.click();
    await page.waitForTimeout(260);
    const openStyle = await page.locator(".qio-select.open .qio-select-menu").first().evaluate((el) => {
      const s = getComputedStyle(el);
      return { opacity: s.opacity, visibility: s.visibility, pointer: s.pointerEvents, transition: s.transitionDuration };
    });
    await combo.click(); // 关闭
    await page.waitForTimeout(260);
    const closedStyle = await page.locator(".qio-select-menu").first().evaluate((el) => {
      const s = getComputedStyle(el);
      return { opacity: s.opacity, visibility: s.visibility, pointer: s.pointerEvents };
    });
    rec("T02-MENU", "菜单出现有过渡，关闭后不拦截点击",
      openStyle.opacity === "1" && openStyle.pointer === "auto" && closedStyle.pointer === "none" && closedStyle.visibility === "hidden",
      `打开=${JSON.stringify(openStyle)} 关闭=${JSON.stringify(closedStyle)}`);
  } catch (e) {
    rec("T02-MENU", "菜单出现有过渡，关闭后不拦截点击", false, e.message.slice(0, 200));
  }

  // 3) 凭据弹窗：出现有过渡，退出期间不误触下层，退出后从 DOM 移除
  try {
    await openSettings("凭据");
    await page.getByText("新建凭据", { exact: false }).first().click();
    await page.waitForTimeout(250);
    const enter = await page.locator(".modal-mask").first().evaluate((el) => ({
      anim: getComputedStyle(el).animationDuration,
      modalAnim: getComputedStyle(el.querySelector(".modal")).animationDuration,
    }));
    await page.locator(".modal-head .close").click();
    await page.waitForTimeout(60);
    const during = await page.locator(".modal-mask").count();
    const duringLeaving = during ? await page.locator(".modal-mask.leaving").count() : 0;
    await page.waitForTimeout(320);
    const after = await page.locator(".modal-mask").count();
    rec("T02-MODAL", "弹窗出现有过渡、退出后不留残留",
      enter.anim !== "0s" && duringLeaving === 1 && after === 0,
      `入场 mask=${enter.anim} modal=${enter.modalAnim}；关闭 60ms 时仍在 DOM=${during}（leaving=${duringLeaving}）；320ms 后=${after}`);
  } catch (e) {
    rec("T02-MODAL", "弹窗出现有过渡、退出后不留残留", false, e.message.slice(0, 200));
  }

  // 4) 减少动画：设置里切换后立即生效，且星球关闭不再等待补间
  try {
    await openSettings("外观");
    await page.locator(".motion-opt", { hasText: "减少动画" }).first().click();
    await page.waitForTimeout(200);
    const attr = await page.evaluate(() => document.documentElement.getAttribute("data-motion"));
    const reducedAnim = await cssOf(".settings", "animation-duration");
    await page.locator(".back").click();
    await page.waitForSelector(".dock", { timeout: 10000 });
    await page.waitForTimeout(600);
    await page.locator(".dock").click();
    await page.waitForTimeout(1200);
    const ms = await page.evaluate(async () => {
      const t0 = performance.now();
      document.querySelector(".close-btn")?.click();
      return await new Promise((res) => {
        const tick = () => {
          if (!document.querySelector(".planet-view")) res(Math.round(performance.now() - t0));
          else requestAnimationFrame(tick);
        };
        tick();
      });
    });
    rec("T02-REDUCED", "设置「减少动画」运行中生效（页面过渡与星球镜头）",
      attr === "reduced" && ms < 120,
      `html[data-motion]=${attr} 设置页 animation-duration=${reducedAnim} 星球关闭=${ms}ms`);
    // 恢复默认，避免影响后续用例
    await openSettings("外观");
    await page.locator(".motion-opt", { hasText: "跟随系统" }).first().click();
    await page.waitForTimeout(200);
  } catch (e) {
    rec("T02-REDUCED", "设置「减少动画」运行中生效（页面过渡与星球镜头）", false, e.message.slice(0, 200));
  }

  // 5) 侧栏开合时长：与重定位同时发生，不串联
  try {
    await page.goto(freshUrl("#/"), { waitUntil: "domcontentloaded" });
    await page.waitForSelector(".dock", { timeout: 15000 });
    await page.waitForTimeout(900);
    await page.locator(".dock").click();
    await page.waitForTimeout(1400);
    const widthTransition = await page.locator(".panel").first().evaluate(
      (el) => getComputedStyle(el).transitionDuration,
    );
    // 在页面内计时：Playwright 的点击往返有上百毫秒开销，不能算进「类是否立即生效」
    const toOpen = await page.evaluate(async () => {
      const t0 = performance.now();
      document.querySelector(".panel-toggle")?.click();
      return await new Promise((res) => {
        const tick = () => {
          if (document.querySelector(".panel")?.classList.contains("open")) res(Math.round(performance.now() - t0));
          else requestAnimationFrame(tick);
        };
        tick();
      });
    });
    await page.waitForTimeout(400);
    rec("T02-SIDEBAR", "侧栏开合在 180–240ms 档位内完成",
      widthTransition.includes("0.21") && toOpen < 120,
      `panel transition-duration=${widthTransition} 点击到 open 类生效=${toOpen}ms（状态立即生效，动画只是过渡）`);
  } catch (e) {
    rec("T02-SIDEBAR", "侧栏开合在 180–240ms 档位内完成", false, e.message.slice(0, 200));
  }
}

// ---------------------------------------------------------------- 任务03 聊天布局与阅读连续性
if (only("t03")) {
  try {
    await page.setViewportSize({ width: 1440, height: 900 });
    await openChat();
    const box = await page.evaluate(() => {
      const r = (el) => (el ? el.getBoundingClientRect() : null);
      const stream = r(document.querySelector(".stream"));
      const user = r(document.querySelector(".message.user"));
      const assist = r(document.querySelector(".message.assistant"));
      return {
        viewport: window.innerWidth,
        stream,
        user,
        assist,
        docOverflow: document.documentElement.scrollWidth - document.documentElement.clientWidth,
        codeOverflow: Array.from(document.querySelectorAll("pre")).map((el) => el.scrollWidth - el.clientWidth),
      };
    });
    const leftGap = Math.round(box.assist.left - box.stream.left);
    const rightGap = Math.round(box.stream.right - box.user.right);
    rec(
      "T03-COLUMN",
      "用户消息与回答在同一个居中内容列内",
      box.docOverflow <= 0 && Math.abs(leftGap - rightGap) < 80 && rightGap > 40,
      `视口=${box.viewport} 回答左边距=${leftGap} 用户右边距=${rightGap} 页面横向溢出=${box.docOverflow} 代码块内部溢出=${JSON.stringify(box.codeOverflow)}`,
    );
  } catch (e) {
    rec("T03-COLUMN", "用户消息与回答在同一个居中内容列内", false, e.message.slice(0, 200));
  }
  try {
    const idle = await page.locator(".composer").evaluate((el) => ({
      border: getComputedStyle(el).borderColor,
      shadow: getComputedStyle(el).boxShadow,
      left: Math.round(el.getBoundingClientRect().left),
      width: Math.round(el.getBoundingClientRect().width),
    }));
    await page.locator(".composer textarea").click();
    await page.waitForTimeout(250);
    const focused = await page.evaluate(() => {
      const c = document.querySelector(".composer");
      const t = document.querySelector(".composer textarea");
      return {
        border: getComputedStyle(c).borderColor,
        innerShadow: getComputedStyle(t).boxShadow,
      };
    });
    rec(
      "T03-COMPOSER-LOOK",
      "输入区默认中性边框，聚焦只加强一层",
      idle.border !== focused.border && (focused.innerShadow === "none" || focused.innerShadow === ""),
      `默认 border=${idle.border}；聚焦 border=${focused.border} 内层阴影=${focused.innerShadow}`,
      `输入区 left=${idle.left} width=${idle.width}`,
    );
  } catch (e) {
    rec("T03-COMPOSER-LOOK", "输入区默认中性边框，聚焦只加强一层", false, e.message.slice(0, 200));
  }
  try {
    await page.locator(".stream").evaluate((el) => {
      el.scrollTop = 200;
      el.dispatchEvent(new Event("scroll"));
    });
    await page.waitForTimeout(300);
    const btnCount = await page.locator(".back-latest").count();
    const btnText = btnCount ? ((await page.locator(".back-latest").textContent()) ?? "").trim() : "";
    if (btnCount) await page.locator(".back-latest").click();
    await page.waitForTimeout(700);
    const backAtBottom = await page
      .locator(".stream")
      .evaluate((el) => Math.round(el.scrollHeight - el.clientHeight - el.scrollTop));
    rec(
      "T03-BACK-LATEST",
      "上翻后出现「回到最新消息」，点击后回到最新",
      btnCount === 1 && btnText.includes("回到最新消息") && backAtBottom <= 4,
      `按钮=${btnCount}（${btnText}）点击后距底部=${backAtBottom}px`,
    );
  } catch (e) {
    rec("T03-BACK-LATEST", "上翻后出现「回到最新消息」，点击后回到最新", false, e.message.slice(0, 200));
  }
  try {
    // 让这一轮「看起来在跑」：本机发送 + 挂起的 /api/turns，与真实体验一致
    await page.route("**/api/turns**", async (route) => {
      await new Promise((r) => setTimeout(r, 4000));
      await route.fulfill({ status: 200, contentType: "application/json", body: '{"ok":true}' });
    });
    await page.fill(".composer textarea", "t03 阶段验证");
    await page.locator(".send-btn").click();
    await page.waitForTimeout(600);
    const waiting = await page.locator(".typing").count();
    const waitingText = waiting ? ((await page.locator(".typing").textContent()) ?? "").trim() : "";
    await fetch(`${API}/api/events/test?event_type=ASSISTANT`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ turn_id: "turn_t03", content: "正在生成的第一段内容" }),
    });
    await page.waitForTimeout(250);
    const fresh = await page.locator(".message.fresh").count();
    const waitingAfter = await page.locator(".typing").count();
    const streaming = await page.locator(".assist-bubble[aria-busy='true']").count();
    await fetch(`${API}/api/events/test?event_type=TURN_END`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ turn_id: "turn_t03" }),
    });
    await page.waitForTimeout(800);
    const freshAfter = await page.locator(".message.fresh").count();
    await page.unroute("**/api/turns**").catch(() => {});
    rec(
      "T03-PHASE",
      "等待/生成状态可辨，新消息只入场一次",
      waiting === 1 && waitingText.includes("等待模型响应") && waitingAfter === 0 && streaming >= 1 && fresh >= 1 && freshAfter === 0,
      `等待指示=${waiting}（${waitingText}）；生成中气泡=${streaming}；新消息入场标记=${fresh}，800ms 后=${freshAfter}`,
    );
  } catch (e) {
    rec("T03-PHASE", "等待/生成状态可辨，新消息只入场一次", false, e.message.slice(0, 200));
  }
  try {
    await page.setViewportSize({ width: 760, height: 720 });
    await page.waitForTimeout(700);
    const narrow = await page.evaluate(() => {
      const c = document.querySelector(".composer").getBoundingClientRect();
      return {
        left: Math.round(c.left),
        width: Math.round(c.width),
        viewport: window.innerWidth,
        docOverflow: document.documentElement.scrollWidth - document.documentElement.clientWidth,
      };
    });
    rec(
      "T03-NARROW",
      "窄窗口输入区贴底整宽且不撑破页面",
      narrow.width >= narrow.viewport - 40 && narrow.docOverflow <= 0,
      `视口=${narrow.viewport} 输入区宽度=${narrow.width} left=${narrow.left} 页面溢出=${narrow.docOverflow}`,
    );
    await shot("t03-narrow-chat");
    await page.setViewportSize({ width: 1440, height: 900 });
    await page.waitForTimeout(500);
    await shot("t03-wide-chat");
    // 输入区不得盖住最后一条内容（滚到底时）
    await page.locator(".stream").evaluate((el) => {
      el.scrollTop = el.scrollHeight;
      el.dispatchEvent(new Event("scroll"));
    });
    await page.waitForTimeout(500);
    const overlap = await page.evaluate(() => {
      const items = Array.from(document.querySelectorAll(".turn"));
      const last = items[items.length - 1];
      const c = document.querySelector(".composer").getBoundingClientRect();
      const l = last ? last.getBoundingClientRect() : null;
      return { composerTop: Math.round(c.top), lastBottom: l ? Math.round(l.bottom) : null };
    });
    rec(
      "T03-NO-COVER",
      "滚到底时输入区不遮住最后一条内容",
      overlap.lastBottom !== null && overlap.lastBottom <= overlap.composerTop,
      `最后一条底部=${overlap.lastBottom} 输入区顶部=${overlap.composerTop}`,
    );
  } catch (e) {
    rec("T03-NARROW", "窄窗口输入区贴底整宽且不撑破页面", false, e.message.slice(0, 200));
  }
}

// ---------------------------------------------------------------- 任务03 补充：帧时间 / 触屏 / 键盘 / 逐字节奏
if (only("t03b")) {
  const wait = (ms) => new Promise((r) => setTimeout(r, ms));
  const startFrames = () =>
    page.evaluate(() => {
      window.__frames = [];
      let last = performance.now();
      const tick = (now) => {
        window.__frames.push(now - last);
        last = now;
        window.__raf = requestAnimationFrame(tick);
      };
      window.__raf = requestAnimationFrame(tick);
      return true;
    });
  const stopFrames = () =>
    page.evaluate(() => {
      cancelAnimationFrame(window.__raf);
      const f = window.__frames || [];
      const sorted = [...f].sort((a, b) => a - b);
      const at = (p) => (sorted.length ? Math.round(sorted[Math.min(sorted.length - 1, Math.floor(sorted.length * p))] * 10) / 10 : 0);
      const total = f.reduce((a, b) => a + b, 0);
      return {
        frames: f.length,
        durationMs: Math.round(total),
        avgFps: total ? Math.round((f.length / (total / 1000)) * 10) / 10 : 0,
        p50: at(0.5),
        p95: at(0.95),
        max: sorted.length ? Math.round(sorted[sorted.length - 1] * 10) / 10 : 0,
        over50ms: f.filter((x) => x > 50).length,
      };
    });
  const frameRec = (id, title, s, note) =>
    rec(
      id,
      title,
      s.frames > 10 && s.p95 < 50,
      `${s.frames} 帧 / ${s.durationMs}ms · 平均 ${s.avgFps}fps · p50=${s.p50}ms p95=${s.p95}ms 最大=${s.max}ms · >50ms 的帧=${s.over50ms}`,
      note,
    );

  report.frameEnv = "headless Edge (Chromium) + SwiftShader 软件渲染；数值只用于发现卡顿，不代表真机 GPU 表现";

  // A1 滚动长对话
  try {
    await page.setViewportSize({ width: 1440, height: 900 });
    await openChat();
    await startFrames();
    for (let i = 0; i < 40; i++) {
      await page.mouse.wheel(0, i % 2 === 0 ? 300 : -200);
      await wait(25);
    }
    frameRec("T03B-FRAME-SCROLL", "滚动长对话时的帧间隔", await stopFrames(), report.frameEnv);
  } catch (e) {
    rec("T03B-FRAME-SCROLL", "滚动长对话时的帧间隔", false, e.message.slice(0, 200));
  }

  // A2 逐字生成长回复（40 段增量）
  try {
    await openChat();
    await startFrames();
    await fetch(`${API}/api/events/test?event_type=TURN_START`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ turn_id: "turn_t03b" }),
    });
    let text = "";
    for (let i = 0; i < 40; i++) {
      text += `第${i + 1}段增量内容，用来观察逐字显示与布局。`;
      await fetch(`${API}/api/events/test?event_type=ASSISTANT`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ turn_id: "turn_t03b", content: text }),
      });
      await wait(30);
    }
    frameRec("T03B-FRAME-STREAM", "长回复增量到达时的帧间隔", await stopFrames(), report.frameEnv);
    await fetch(`${API}/api/events/test?event_type=TURN_END`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ turn_id: "turn_t03b" }),
    });
    await page.waitForTimeout(600);
  } catch (e) {
    rec("T03B-FRAME-STREAM", "长回复增量到达时的帧间隔", false, e.message.slice(0, 200));
  }

  // A3 星球打开 + 拖动旋转 + 关闭
  try {
    await page.goto(freshUrl("#/"), { waitUntil: "domcontentloaded" });
    await page.waitForSelector(".dock", { timeout: 15000 });
    await page.waitForTimeout(800);
    await startFrames();
    await page.locator(".dock").click();
    await page.waitForTimeout(900);
    await page.mouse.move(500, 420);
    await page.mouse.down();
    for (let i = 0; i < 20; i++) {
      await page.mouse.move(500 + i * 8, 420 + (i % 3) * 4);
      await wait(25);
    }
    await page.mouse.up();
    await page.waitForTimeout(300);
    await page.locator(".close-btn").click();
    await page.waitForSelector(".planet-view", { state: "detached", timeout: 8000 });
    frameRec("T03B-FRAME-PLANET", "星球打开/拖动/关闭时的帧间隔", await stopFrames(), report.frameEnv);
  } catch (e) {
    rec("T03B-FRAME-PLANET", "星球打开/拖动/关闭时的帧间隔", false, e.message.slice(0, 200));
  }

  // B 键盘滚动（PageUp → End）
  try {
    await openChat();
    await page.locator(".stream").click({ position: { x: 400, y: 200 } });
    await page.locator(".stream").evaluate((el) => {
      el.scrollTop = 400;
      el.dispatchEvent(new Event("scroll"));
    });
    await page.waitForTimeout(250);
    const before = await page.locator(".stream").evaluate((el) => Math.round(el.scrollTop));
    await page.keyboard.press("PageUp");
    await page.waitForTimeout(350);
    const afterUp = await page.locator(".stream").evaluate((el) => Math.round(el.scrollTop));
    const btnAfterKey = await page.locator(".back-latest").count();
    await page.keyboard.press("End");
    await page.waitForTimeout(500);
    const distance = await page
      .locator(".stream")
      .evaluate((el) => Math.round(el.scrollHeight - el.clientHeight - el.scrollTop));
    rec(
      "T03B-KEYBOARD",
      "键盘可以滚动消息区，并且不会与自动跟随打架",
      afterUp < before && btnAfterKey === 1 && distance <= 4,
      `PageUp 前=${before} 后=${afterUp}（应更小）「回到最新」出现=${btnAfterKey}；End 后距底部=${distance}px`,
    );
  } catch (e) {
    rec("T03B-KEYBOARD", "键盘可以滚动消息区，并且不会与自动跟随打架", false, e.message.slice(0, 200));
  }

  // C 触屏：点复制入口 + 滑动消息区
  try {
    const touchCtx = await browser.newContext({ viewport: { width: 900, height: 800 }, hasTouch: true });
    await touchCtx.grantPermissions(["clipboard-read", "clipboard-write"], { origin: BASE }).catch(() => {});
    const p = await touchCtx.newPage();
    await p.goto(freshUrl("#/"), { waitUntil: "domcontentloaded" });
    await p.waitForSelector(".code-block .code-copy", { timeout: 15000 });
    await p.waitForTimeout(900);
    const copyBtn = p.locator(".code-block .code-copy").first();
    let copyTap = "n/a";
    await copyBtn.scrollIntoViewIfNeeded();
    await p.waitForTimeout(300);
    await copyBtn.tap(); // 合成触摸点击（先滚进视口，避免点到视口外）
    await p.waitForTimeout(400);
    copyTap = ((await copyBtn.textContent()) ?? "").trim();
    const client = await touchCtx.newCDPSession(p);
    // 先滚到底部，再用手指向下滑（内容往上走）→ 应当停止跟随
    await p.locator(".stream").evaluate((el) => {
      el.scrollTop = el.scrollHeight;
      el.dispatchEvent(new Event("scroll"));
    });
    await p.waitForTimeout(300);
    const streamBox = await p.locator(".stream").boundingBox();
    const x = Math.round(streamBox.x + streamBox.width / 2);
    const y0 = Math.round(streamBox.y + streamBox.height * 0.35);
    await client.send("Input.dispatchTouchEvent", { type: "touchStart", touchPoints: [{ x, y: y0 }] });
    for (let i = 1; i <= 8; i++) {
      await client.send("Input.dispatchTouchEvent", { type: "touchMove", touchPoints: [{ x, y: y0 + i * 25 }] });
      await wait(16);
    }
    await client.send("Input.dispatchTouchEvent", { type: "touchEnd", touchPoints: [] });
    await p.waitForTimeout(600);
    const swipe = await p.evaluate(() => {
      const el = document.querySelector(".stream");
      return {
        scrollTop: Math.round(el.scrollTop),
        distanceToBottom: Math.round(el.scrollHeight - el.clientHeight - el.scrollTop),
        back: !!document.querySelector(".back-latest"),
      };
    });
    await touchCtx.close();
    rec(
      "T03B-TOUCH",
      "触屏可直接点复制入口，滑动消息区能滚动并停止跟随",
      copyTap === "已复制" && swipe.scrollTop > 0 && swipe.back,
      `点复制后文案='${copyTap}'；向下滑动后 scrollTop=${swipe.scrollTop}（距底部 ${swipe.distanceToBottom}px）「回到最新」=${swipe.back}`,
      "触屏为 CDP 合成的触摸事件（非真机），只验证「不依赖悬停」与手势响应",
    );
  } catch (e) {
    rec("T03B-TOUCH", "触屏可直接点复制入口，滑动消息区能滚动并停止跟随", false, e.message.slice(0, 200));
  }

  // D 逐字显示是否落后于真实到达
  try {
    await openChat();
    let text = "";
    const samples = [];
    for (let i = 0; i < 12; i++) {
      text += `第${i + 1}段。`;
      await fetch(`${API}/api/events/test?event_type=ASSISTANT`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ turn_id: "turn_t03b_lag", content: text }),
      });
      await wait(200);
      const shown = await page.evaluate(() => {
        // 取「最后一条」助手正文：第一条是历史消息，长度对不上
        const all = document.querySelectorAll(".assist-bubble .markdown-body");
        const el = all[all.length - 1];
        return el ? (el.textContent || "").length : -1;
      });
      samples.push({ received: text.length, shown });
    }
    const lags = samples.filter((s) => s.shown >= 0).map((s) => s.received - s.shown);
    const maxLag = lags.length ? Math.max(...lags) : -1;
    await page.waitForTimeout(700);
    const settled = await page.evaluate(() => {
      const all = document.querySelectorAll(".assist-bubble .markdown-body");
      const el = all[all.length - 1];
      return el ? (el.textContent || "").length : -1;
    });
    rec(
      "T03B-PACE",
      "逐字显示按真实到达节奏走，不长期落后",
      maxLag >= 0 && maxLag <= 40 && settled >= text.length - 6,
      `最后收到 ${text.length} 字；采样中的最大落后=${maxLag} 字；停止到达后显示=${settled} 字`,
      `采样=${JSON.stringify(samples.slice(0, 4))}…`,
    );
  } catch (e) {
    rec("T03B-PACE", "逐字显示按真实到达节奏走，不长期落后", false, e.message.slice(0, 200));
  }

  // E 等待动画只在回答附近播一次
  try {
    await page.route("**/api/turns**", async (route) => {
      await wait(4000);
      await route.fulfill({ status: 200, contentType: "application/json", body: '{"ok":true}' });
    });
    await page.fill(".composer textarea", "等待动画检查");
    await page.locator(".send-btn").click();
    await page.waitForTimeout(700);
    const anims = await page.evaluate(() => {
      const name = (sel) => {
        const el = document.querySelector(sel);
        return el ? getComputedStyle(el).animationName : "missing";
      };
      return {
        typingDot: name(".typing-bubble .dot"),
        dockActivity: name(".dock .activity"),
        queueRunMark: name(".row.running .live-mark"),
        queueChipLive: name(".chip .live"),
      };
    });
    await page.unroute("**/api/turns**").catch(() => {});
    rec(
      "T03B-SINGLE-WAIT",
      "同一套等待动画只播一次（其余位置为静态标记）",
      anims.typingDot !== "none" &&
        anims.dockActivity === "none" &&
        (anims.queueRunMark === "none" || anims.queueRunMark === "missing"),
      `回答附近等待动画=${anims.typingDot}；星球入口标记=${anims.dockActivity}；任务列表标记=${anims.queueRunMark}；队列小圆点=${anims.queueChipLive}`,
    );
  } catch (e) {
    rec("T03B-SINGLE-WAIT", "同一套等待动画只播一次（其余位置为静态标记）", false, e.message.slice(0, 200));
  }
}

report.consoleErrors = consoleErrors.slice(0, 20);
report.endedAt = new Date().toISOString();
writeFileSync(`${OUT}\\report.json`, JSON.stringify(report, null, 2), "utf8");
console.log(`\nreport: ${OUT}\\report.json`);
console.log(`console errors: ${consoleErrors.length}`);
for (const e of consoleErrors.slice(0, 10)) console.log("  - " + e);
await browser.close();
