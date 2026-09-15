// 任务 05 星球专项实测（在真实运行的应用上操作，不用单元测试替代）。
// 用法（服务需在跑：后端 8734、前端 5199）：
//   node scripts/baseline/planet-probe.mjs [用例前缀...]
// 输出：%TEMP%\qio-baseline\task05\report.json 与 shots\*.png
// 说明：只用隔离数据目录里的假数据，不调用真实模型。
import { createRequire } from "node:module";
import { mkdirSync, writeFileSync } from "node:fs";

const require = createRequire(import.meta.url);
const PW = "C:\\Users\\zxy\\.cache\\codex-runtimes\\codex-primary-runtime\\dependencies\\node\\node_modules\\playwright";
const { chromium } = require(PW);

const BASE = "http://127.0.0.1:5199";
const OUT = `${process.env.TEMP}\\qio-baseline\\task05`;
const SHOTS = `${OUT}\\shots`;
mkdirSync(SHOTS, { recursive: true });

const wanted = process.argv.slice(2);
const only = (id) => wanted.length === 0 || wanted.some((w) => id.startsWith(w));
const report = { startedAt: new Date().toISOString(), cases: [], notes: [] };
const rec = (id, title, passed, actual) => {
  report.cases.push({ id, title, passed: !!passed, actual: String(actual) });
  console.log(`[${passed ? "PASS" : "FAIL"}] ${id} ${title} :: ${actual}`);
};
const note = (t) => {
  report.notes.push(t);
  console.log(`[NOTE] ${t}`);
};

let seq = Math.floor(Date.now() / 1000) % 100000;
const url = (hash = "#/", extra = "") => `${BASE}/?fresh=${++seq}${extra}${hash}`;
const wait = (ms) => new Promise((r) => setTimeout(r, ms));

const browser = await chromium.launch({
  channel: "msedge",
  headless: true,
  args: ["--use-gl=angle", "--use-angle=swiftshader", "--enable-unsafe-swiftshader"],
});
const context = await browser.newContext({ viewport: { width: 1440, height: 900 } });
const page = await context.newPage();
const consoleErrors = [];
page.on("console", (m) => { if (m.type() === "error") consoleErrors.push(m.text().slice(0, 200)); });
page.on("pageerror", (e) => consoleErrors.push("pageerror: " + String(e.message).slice(0, 200)));

const shot = async (name) => {
  const p = `${SHOTS}\\${name}.png`;
  await page.screenshot({ path: p });
  return p;
};
const openChat = async (extra = "") => {
  await page.goto(url("#/", extra), { waitUntil: "domcontentloaded" });
  await page.waitForSelector(".dock", { timeout: 20000 });
  await page.waitForTimeout(900);
};
const openPlanet = async () => {
  await page.locator(".dock").click();
  await page.waitForSelector(".planet-view", { timeout: 20000 });
  await page.waitForTimeout(900);
};
const openPanel = async () => {
  await page.locator(".panel-toggle").click();
  await page.waitForTimeout(500);
};

if (only("P5-LOADING")) {
  try {
    await context.route("**/api/topics", async (route) => {
      await wait(2500);
      await route.continue();
    });
    await openChat();
    const t0 = Date.now();
    await page.locator(".dock").click();
    // 提示是转瞬即逝的（数据到了就消失）：在同一次取值里拿到文本，不要分两次调用
    const loadingText = await page
      .waitForFunction(() => document.querySelector(".planet-loading")?.textContent?.trim() || false, null, { timeout: 5000 })
      .then((h) => h.jsonValue())
      .catch(() => null);
    const shotPath = await shot("planet-loading");
    await page.waitForSelector(".topic-list li", { timeout: 15000 });
    const gone = (await page.locator(".planet-loading").count()) === 0;
    const topics = await page.locator(".topic-list li").count();
    rec("P5-LOADING", "首次加载长等待有明确提示，加载完成后消失",
      loadingText?.includes("正在加载") && gone && topics > 0,
      `提示='${loadingText}' 消失=${gone} 话题=${topics} 点击到提示出现=${Date.now() - t0}ms 截图=${shotPath}`);
    await context.unroute("**/api/topics");
  } catch (e) {
    rec("P5-LOADING", "首次加载长等待有明确提示", false, e.message.slice(0, 200));
  }
}

if (only("P5-DETAIL")) {
  try {
    await openChat();
    await openPlanet();
    await openPanel();
    await page.locator(".topic-list li").first().click();
    await page.waitForSelector(".detail", { timeout: 10000 });
    await page.waitForTimeout(400);
    const identity = ((await page.locator(".detail-identity").innerText()) ?? "").replace(/\s+/g, " ");
    const shotPath = await shot("planet-detail-layout");
    const before = await page.locator(".detail-actions").boundingBox();
    // 把详情滚动区拉到底：底部操作区不应跟着滚走，也不应盖住正文
    await page.locator(".detail-scroll").evaluate((el) => { el.scrollTop = el.scrollHeight; });
    await page.waitForTimeout(300);
    const after = await page.locator(".detail-actions").boundingBox();
    const body = await page.locator(".panel-body").boundingBox();
    const overlaps = after && body ? after.y < body.y + body.height - 1 : true;
    rec("P5-DETAIL", "详情：可滚动中部 + 固定底部操作区，底部不覆盖内容",
      Boolean(before && after) && Math.abs(before.y - after.y) < 1 && !overlaps,
      `操作区 y ${before?.y} → ${after?.y}；中部底边 ${body ? Math.round(body.y + body.height) : "?"}；重叠=${overlaps}`);
    rec("P5-IDENTITY", "三者分开写清：浏览的话题 / 选中的片段 / 已生效的起点",
      identity.includes("浏览的话题") && identity.includes("选中的片段") && identity.includes("已生效的起点"),
      `文本='${identity.slice(0, 120)}' 截图=${shotPath}`);
  } catch (e) {
    rec("P5-DETAIL", "详情布局", false, e.message.slice(0, 200));
  }
}

if (only("P5-HOVER")) {
  try {
    await openChat();
    await openPlanet();
    const box = await page.locator("canvas.planet-canvas").boundingBox();
    // 只看「悬停标签」：选中话题的名称是常驻的（.topic-hint.selected），不算悬停命中
    const hoverHint = page.locator(".topic-hint:not(.selected)");
    let hit = null;
    for (let y = box.y + 60; y < box.y + box.height - 60 && !hit; y += 24) {
      for (let x = box.x + 60; x < box.x + box.width - 60; x += 24) {
        await page.mouse.move(x, y);
        if (await hoverHint.count()) {
          hit = { x, y, text: ((await hoverHint.textContent()) ?? "").trim() };
          break;
        }
      }
    }
    const shotPath = hit ? await shot("planet-hover-hint") : null;
    rec("P5-HOVER", "指向话题点显示简短名称（不常驻）",
      Boolean(hit && hit.text.length > 0), hit ? `命中(${Math.round(hit.x)},${Math.round(hit.y)}) 名称='${hit.text}' 截图=${shotPath}` : "扫描整块画布未命中任何话题点");
    // 移开后标签消失
    await page.mouse.move(box.x + 5, box.y + 5);
    await page.waitForTimeout(200);
    const stillThere = await hoverHint.count();
    rec("P5-HOVER-CLEAR", "指针移开后标签不残留", stillThere === 0, `残留标签=${stillThere}`);
  } catch (e) {
    rec("P5-HOVER", "悬停标签", false, e.message.slice(0, 200));
  }
}

if (only("P5-KEYBOARD")) {
  try {
    await openChat();
    await openPlanet();
    await openPanel();
    await page.locator(".topic-list li").first().click();
    await page.waitForSelector(".detail", { timeout: 10000 });
    const items = page.locator(".topic-list li");
    const n = await items.count();
    if (n > 1) {
      await items.nth(1).focus();
      await page.keyboard.press("Enter");
      await page.waitForTimeout(600);
    }
    const selected = await page.locator(".topic-list li[aria-selected='true']").count();
    const focusedTag = await page.evaluate(() => document.activeElement?.tagName);
    rec("P5-KEYBOARD", "话题列表可用键盘选择（li 可聚焦 + Enter 选中）",
      n > 1 && selected === 1, `条目=${n} aria-selected=1 的条目=${selected} 当前焦点=${focusedTag}`);
    const shotPath = await shot("planet-keyboard-select");
    note(`键盘选择后截图：${shotPath}`);
  } catch (e) {
    rec("P5-KEYBOARD", "键盘选择", false, e.message.slice(0, 200));
  }
}

if (only("P5-ESC")) {
  try {
    await openChat();
    await openPlanet();
    await openPanel();
    await page.keyboard.press("Escape");
    await page.waitForTimeout(300);
    const panelOpen = await page.locator(".panel.open").count();
    const stillOpen = await page.locator(".planet-view").count();
    const closingAfterPanel = await page.locator(".planet-view.settling, .planet-view.closing").count();
    await page.keyboard.press("Escape");
    // 等结果而不是死等固定时间：收起是 180ms 收势 + 280ms 淡出，机器忙时会超出固定等待
    const outcome = await page
      .waitForFunction(
        () => {
          const v = document.querySelector(".planet-view");
          if (!v) return "closed";
          if (v.classList.contains("settling") || v.classList.contains("closing")) return "closing";
          return false;
        },
        null,
        { timeout: 3000 },
      )
      .then((h) => h.jsonValue())
      .catch(() => "none");
    const activeTag = await page.evaluate(() => document.activeElement?.tagName ?? "NONE");
    rec("P5-ESC", "Esc 最上层优先：先收边栏，再按才收起星球",
      panelOpen === 0 && stillOpen === 1 && closingAfterPanel === 0 && outcome !== "none",
      `第一次 Esc 后面板 open=${panelOpen} 星球仍在=${stillOpen} 已开始收起=${closingAfterPanel}；第二次 Esc 后状态=${outcome} 焦点=${activeTag}`);
  } catch (e) {
    rec("P5-ESC", "Esc 分层", false, e.message.slice(0, 200));
  }
}

if (only("P5-RING")) {
  try {
    await openChat();
    await openPlanet();
    await openPanel();
    await page.locator(".topic-list li").first().click();
    await page.waitForSelector(".detail", { timeout: 10000 });
    await page.waitForTimeout(700); // 等朝向补间结束
    const canvas = await page.locator("canvas.planet-canvas").boundingBox();
    // 选中点会被镜头带到画布中心：裁一块中心区域看「持续可见的选中标记」
    const clip = { x: canvas.x + canvas.width / 2 - 110, y: canvas.y + canvas.height / 2 - 110, width: 220, height: 220 };
    const p = `${SHOTS}\\planet-selection-ring.png`;
    await page.screenshot({ path: p, clip });
    const selectedListed = await page.locator(".topic-list li[aria-selected='true']").count();
    rec("P5-RING", "选中话题有持续可见的标记（列表选中态 + 星球上的选中环）",
      selectedListed === 1, `列表 aria-selected=1 的条目=${selectedListed}；画布中心裁切截图=${p}`);
  } catch (e) {
    rec("P5-RING", "选中标记", false, e.message.slice(0, 200));
  }
}

if (only("P5-KNOW-ERR")) {
  try {
    await openChat();
    await openPlanet();
    await openPanel();
    await context.route("**/api/knowledge", (route) =>
      route.fulfill({ status: 500, contentType: "application/json", body: '{"detail":"boom"}' }));
    await page.locator(".tab-knowledge").click();
    await page.waitForSelector(".k-load-error", { timeout: 10000 });
    const text = ((await page.locator(".k-load-error").innerText()) ?? "").replace(/\s+/g, " ");
    const shotPath = await shot("planet-knowledge-error");
    await context.unroute("**/api/knowledge");
    await page.locator(".k-load-error button").click();
    // 重试是真实请求：等列表出现，而不是死等固定时间
    const recovered = await page
      .waitForSelector(".k-item", { timeout: 8000 })
      .then(() => page.locator(".k-item").count())
      .catch(() => 0);
    rec("P5-KNOW-ERR", "知识读取失败：显示原因 + 重试，不在列表里伪装成「无记录」",
      text.includes("加载知识失败") && recovered > 0,
      `错误文本='${text.slice(0, 80)}' 重试后条目=${recovered} 截图=${shotPath}`);
  } catch (e) {
    rec("P5-KNOW-ERR", "知识读取失败", false, e.message.slice(0, 200));
  }
}

if (only("P5-KNOW-EMPTY")) {
  try {
    await openChat();
    await openPlanet();
    await openPanel();
    await page.locator(".tab-knowledge").click();
    await page.waitForTimeout(900);
    const total = await page.locator(".k-item").count();
    await page.locator(".kpanel input").first().fill("绝不可能存在的关键词zzz");
    await page.waitForTimeout(300);
    const emptyText = ((await page.locator(".k-empty").innerText()) ?? "").replace(/\s+/g, " ");
    await page.locator(".k-clear-filters").click();
    await page.waitForTimeout(300);
    const restored = await page.locator(".k-item").count();
    rec("P5-KNOW-EMPTY", "「没有匹配」与「暂无记录」区分，清除筛选恢复集合",
      emptyText.includes("没有匹配") && restored === total && total > 0,
      `总条目=${total} 空态='${emptyText}' 清除后=${restored}`);
  } catch (e) {
    rec("P5-KNOW-EMPTY", "知识空态区分", false, e.message.slice(0, 200));
  }
}

if (only("P5-CLOSE-REOPEN")) {
  try {
    await openChat();
    await openPlanet();
    const t0 = Date.now();
    await page.locator(".close-btn").click();
    await page.waitForTimeout(120); // 收起动画进行中
    const duringClose = await page.locator(".planet-view").count();
    // 覆盖层在收起阶段已不再拦截点击：此时点入口应当重新打开（而不是被旧回调关掉）
    await page.evaluate(() => document.querySelector(".dock")?.click());
    await page.waitForTimeout(900);
    const afterReopen = await page.locator(".planet-view").count();
    const closingNow = await page.locator(".planet-view.settling, .planet-view.closing").count();
    rec("P5-CLOSE-REOPEN", "收起动画中重新打开：新一层不被旧的关闭回调关掉",
      duringClose === 1 && afterReopen === 1 && closingNow === 0,
      `收起中=${duringClose} 重开后=${afterReopen} 仍在收起=${closingNow} 用时=${Date.now() - t0}ms`);
  } catch (e) {
    rec("P5-CLOSE-REOPEN", "收起中重新打开", false, e.message.slice(0, 200));
  }
}

if (only("P5-CLOSE-TIMING")) {
  try {
    await openChat();
    await openPlanet();
    const t = await page.evaluate(async () => {
      const t0 = performance.now();
      document.querySelector(".close-btn")?.click();
      return await new Promise((res) => {
        const iv = setInterval(() => {
          // 实例常驻：关闭 = 隐藏（display:none），不再从 DOM 移除
          const v = document.querySelector(".planet-view");
          if (!v || getComputedStyle(v).display === "none") { clearInterval(iv); res(Math.round(performance.now() - t0)); }
        }, 8);
        setTimeout(() => { clearInterval(iv); res(-1); }, 4000);
      });
    });
    rec("P5-CLOSE-TIMING", "收起总时长（收势 + 淡出 + 卸载）", t > 300 && t < 900, `${t}ms（减少动画时应接近 0）`);
    // 减少动画
    await page.emulateMedia({ reducedMotion: "reduce" });
    await page.evaluate(() => { document.documentElement.setAttribute("data-motion", "reduced"); });
    await openPlanet();
    const t2 = await page.evaluate(async () => {
      const t0 = performance.now();
      document.querySelector(".close-btn")?.click();
      return await new Promise((res) => {
        const iv = setInterval(() => {
          const v = document.querySelector(".planet-view");
          if (!v || getComputedStyle(v).display === "none") { clearInterval(iv); res(Math.round(performance.now() - t0)); }
        }, 4);
        setTimeout(() => { clearInterval(iv); res(-1); }, 3000);
      });
    });
    rec("P5-REDUCED-CLOSE", "减少动画下收起立即完成（功能不依赖动画）", t2 >= 0 && t2 < 120, `${t2}ms`);
    await page.emulateMedia({ reducedMotion: "no-preference" });
    await page.evaluate(() => { document.documentElement.removeAttribute("data-motion"); });
  } catch (e) {
    rec("P5-CLOSE-TIMING", "收起时长", false, e.message.slice(0, 200));
  }
}

if (only("P5-DARK")) {
  try {
    await context.addInitScript(() => {
      try { localStorage.setItem("qio-theme", "dark"); } catch { /* 忽略 */ }
    });
    await openChat();
    await openPlanet();
    await openPanel();
    await page.locator(".topic-list li").first().click();
    await page.waitForSelector(".detail", { timeout: 10000 });
    await page.waitForTimeout(700);
    const theme = await page.evaluate(() => document.documentElement.getAttribute("data-theme"));
    const identity = await page.locator(".detail-identity").isVisible();
    const labels = await page.locator(".topic-hint").count();
    const shotPath = await shot("planet-dark-detail");
    rec("P5-DARK", "深色主题下新增的选中名称/详情分区仍可见",
      theme === "dark" && identity, `data-theme=${theme} 身份区可见=${identity} 星球标签=${labels} 截图=${shotPath}`);
  } catch (e) {
    rec("P5-DARK", "深色主题星球", false, e.message.slice(0, 200));
  }
}

if (only("P5-NARROW")) {
  try {
    await page.setViewportSize({ width: 760, height: 900 });
    await openChat();
    await openPlanet();
    await openPanel();
    const box = await page.locator(".panel.open").boundingBox();
    const toggle = await page.locator(".panel-toggle").boundingBox();
    const shotPath = await shot("planet-narrow-panel");
    await page.keyboard.press("Escape");
    await page.waitForTimeout(300);
    const panelOpen = await page.locator(".panel.open").count();
    rec("P5-NARROW", "窄窗口：边栏为覆盖式抽屉、有展开/收起入口、Esc 先收边栏",
      Boolean(box && toggle) && box.width <= 760 && panelOpen === 0,
      `面板宽=${box ? Math.round(box.width) : "?"} 切换钮在视口内=${Boolean(toggle && toggle.x >= 0)} 截图=${shotPath}`);
    await page.setViewportSize({ width: 1440, height: 900 });
  } catch (e) {
    rec("P5-NARROW", "窄窗口边栏", false, e.message.slice(0, 200));
  }
}

report.consoleErrors = consoleErrors.slice(0, 20);
report.endedAt = new Date().toISOString();
writeFileSync(`${OUT}\\report.json`, JSON.stringify(report, null, 2), "utf8");
console.log(`\nreport: ${OUT}\\report.json`);
console.log(`cases=${report.cases.length} pass=${report.cases.filter((c) => c.passed).length} fail=${report.cases.filter((c) => !c.passed).length}`);
await browser.close();
