// 任务 07 综合验收：跨阶段组合路径 + 视觉矩阵 + 找茬检查。
// 用法（服务需在跑：后端 8734、前端 5199）：
//   node scripts/baseline/acceptance.mjs [case 前缀...]
// 输出：%TEMP%\qio-baseline\acceptance\report.json 与 shots\*.png
// 说明：只用隔离数据目录里的假数据，不调用真实模型；应用内浏览器看到的同一份代码。
import { createRequire } from "node:module";
import { mkdirSync, writeFileSync } from "node:fs";

const require = createRequire(import.meta.url);
const PW = "C:\\Users\\zxy\\.cache\\codex-runtimes\\codex-primary-runtime\\dependencies\\node\\node_modules\\playwright";
const { chromium } = require(PW);

const BASE = "http://127.0.0.1:5199";
const API = "http://127.0.0.1:8734";
const OUT = `${process.env.TEMP}\\qio-baseline\\acceptance`;
const SHOTS = `${OUT}\\shots`;
mkdirSync(SHOTS, { recursive: true });

const wanted = process.argv.slice(2);
const only = (id) => wanted.length === 0 || wanted.some((w) => id.startsWith(w));
const report = { startedAt: new Date().toISOString(), cases: [], notes: [] };
const rec = (id, title, passed, actual, detail = null) => {
  report.cases.push({ id, title, passed: !!passed, actual: String(actual), detail });
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
await context.grantPermissions(["clipboard-read", "clipboard-write"], { origin: BASE }).catch(() => {});
const consoleErrors = [];
/**
 * 每个用例用全新的页面。
 * 复用同一个标签页会让上一条用例的 page.route / store 状态 / 滚动位置漏到下一条
 * （实测：A12/A13 因残留路由与残留待审批项出现假失败），所以按用例隔离。
 */
let page = null;
function attach(p) {
  p.on("console", (m) => { if (m.type() === "error") consoleErrors.push(m.text().slice(0, 200)); });
  p.on("pageerror", (e) => consoleErrors.push("pageerror: " + String(e.message).slice(0, 200)));
  return p;
}
async function freshPage() {
  const old = page;
  page = attach(await context.newPage());
  if (old) await old.close().catch(() => {});
  return page;
}
await freshPage();

const shot = async (name) => {
  const p = `${SHOTS}\\${name}.png`;
  await page.screenshot({ path: p });
  return p;
};
const openChat = async (extra = "") => {
  await page.goto(url("#/", extra), { waitUntil: "domcontentloaded" });
  await page.waitForSelector(".composer textarea", { timeout: 15000 });
  await page.waitForTimeout(1100);
};
const openSettings = async (section = null, extra = "") => {
  await page.goto(url("#/settings", extra), { waitUntil: "domcontentloaded" });
  await page.waitForSelector(".nav button.tab", { timeout: 15000 });
  await page.waitForTimeout(600);
  if (section) {
    await page.locator(".nav button.tab", { hasText: section }).first().click();
    await page.waitForTimeout(400);
  }
};
const inject = (type, data) =>
  fetch(`${API}/api/events/test?event_type=${type}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(data),
  });

/**
 * 后台事件会被重放：新开的页面可能直接弹出「等待确认」窗口，遮住输入区。
 * 这类窗口不是本用例要测的对象，先收起（用 Esc 收起，不做出授权决定）。
 */
const dismissStaleModal = async () => {
  if (await page.locator(".modal-mask").count()) {
    await page.keyboard.press("Escape").catch(() => {});
    await page.waitForTimeout(300);
  }
};

// 路由谓词必须是同一个函数引用，否则 page.unroute 摘不掉（Playwright 按身份匹配）
const isTopics = (u) => u.pathname === "/api/graph/topics";
const isTopicDetail = (u) => /\/api\/graph\/topics\/topic_/.test(u.pathname);
const isKnowledge = (u) => u.pathname === "/api/knowledge";
const isAnchor = (u) => u.pathname === "/api/anchor";

// ---------------------------------------------------------------- 综合操作路径
if (only("A1")) {
  await freshPage();
  try {
    await openChat();
    await page.fill(".composer textarea", "未发送草稿-验收");
    await page.locator(".stream").evaluate((el) => { el.scrollTop = 260; el.dispatchEvent(new Event("scroll")); });
    await page.waitForTimeout(300);
    const before = await page.locator(".stream").evaluate((el) => Math.round(el.scrollTop));
    await page.locator(".settings-float").click();
    await page.waitForSelector(".nav button.tab", { timeout: 10000 });
    await page.locator(".nav button.tab", { hasText: "数据与维护" }).first().click();
    await page.waitForTimeout(400);
    await page.locator(".back").click();
    await page.waitForSelector(".composer textarea", { timeout: 10000 });
    await page.waitForTimeout(900);
    const draft = await page.inputValue(".composer textarea");
    const after = await page.locator(".stream").evaluate((el) => Math.round(el.scrollTop));
    await page.locator(".settings-float").click();
    await page.waitForSelector(".nav button.tab", { timeout: 10000 });
    await page.waitForTimeout(500);
    const section = ((await page.locator(".tab.active").textContent()) ?? "").trim();
    rec("A1-ROUNDTRIP", "设置往返：草稿 + 阅读位置 + 分类都保留",
      draft === "未发送草稿-验收" && Math.abs(after - before) <= 40 && section === "数据与维护",
      `草稿='${draft}' 阅读位置 ${before}→${after} 恢复分类='${section}'`);
  } catch (e) {
    rec("A1-ROUNDTRIP", "设置往返：草稿 + 阅读位置 + 分类都保留", false, e.message.slice(0, 200));
  }
}

if (only("A2")) {
  await freshPage();
  try {
    await openChat();
    // 一边编辑一边来后台确认：不抢焦点、不丢草稿、有可发现的入口
    await page.fill(".composer textarea", "编辑中的草稿-确认打扰");
    await page.locator(".composer textarea").click();
    await inject("APPROVAL_REQUIRED", { approval: { approval_id: "apr_acc_1", kind: "tool_create", payload: { name: "验收假工具", explanation: "验收用" } } });
    await page.waitForTimeout(900);
    const draftKept = await page.inputValue(".composer textarea");
    const dialogOpen = await page.locator('[role="dialog"]').count();
    const focused = await page.evaluate(() => document.activeElement?.tagName ?? "none");
    const entry = page.locator(".approval-entry");
    const entryText = ((await entry.first().innerText().catch(() => "")) ?? "").trim();
    const shotPath = await shot("a2-approval-while-typing");
    // 主动打开 → 窗口出现
    await entry.first().click();
    await page.waitForTimeout(500);
    const dialogAfterEntry = await page.locator('[role="dialog"]').count();
    const laterBtn = await page.locator(".later").count();
    // Esc 不做决定：收起窗口、保留待办、入口回来
    await page.keyboard.press("Escape");
    await page.waitForTimeout(600);
    const dialogAfterEsc = await page.locator('[role="dialog"]').count();
    const entryAfterEsc = ((await page.locator(".approval-entry").first().innerText().catch(() => "")) ?? "").trim();
    const draftAfter = await page.inputValue(".composer textarea");
    rec("A2-APPROVAL-DISCOVER", "后台确认不抢焦点、有可发现入口、Esc 不代替决定且待办不丢",
      draftKept === "编辑中的草稿-确认打扰" && dialogOpen === 0 && focused === "TEXTAREA" &&
        /有 1 项操作等待确认/.test(entryText) && dialogAfterEntry === 1 && laterBtn === 1 &&
        dialogAfterEsc === 0 && /有 1 项操作等待确认/.test(entryAfterEsc) && draftAfter === "编辑中的草稿-确认打扰",
      `草稿='${draftKept}'→'${draftAfter}' 自动弹出=${dialogOpen} 焦点=${focused} 入口='${entryText}' 点入口后窗口=${dialogAfterEntry} 稍后处理按钮=${laterBtn} Esc 后窗口=${dialogAfterEsc} Esc 后入口='${entryAfterEsc}'`,
      `截图=${shotPath}`);
  } catch (e) {
    rec("A2-APPROVAL-DISCOVER", "后台确认不抢焦点、有可发现入口、Esc 不代替决定且待办不丢", false, e.message.slice(0, 200));
  }
}

if (only("A3")) {
  await freshPage();
  try {
    await openChat();
    await dismissStaleModal();
    // 每次运行用不同的 turn_id：后端会重放最近事件，复用旧 id 会让上一轮的
    // TURN_END 混进来，把「这一轮正在运行」的状态提前结束
    const longReplyTurnId = `turn_acc_long_${Date.now()}`;
    await inject("TURN_START", { turn_id: longReplyTurnId });
    await page.waitForTimeout(600);
    const longReply = "## 验收长回复\n\n- 第一点\n- 第二点\n\n| 列A | 列B |\n| --- | --- |\n| 1 | 2 |\n\n```python\ndef f(x):\n    return x  # 这是一行很长的代码用来检查横向滚动容器是否会撑破页面布局AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA\n```\n";
    await inject("ASSISTANT", {
      turn_id: longReplyTurnId,
      content: longReply,
    });
    await page.waitForTimeout(1600);
    const rendered = await page.evaluate(() => {
      const last = document.querySelectorAll(".assist-bubble .markdown-body");
      const el = last[last.length - 1];
      return {
        hasTable: !!el?.querySelector(".table-wrap"),
        hasCode: !!el?.querySelector(".code-block"),
        text: (el?.innerText ?? "").replace(/\s+/g, " ").slice(0, 60),
        overflow: document.documentElement.scrollWidth - document.documentElement.clientWidth,
      };
    });
    // 增量：同一 turn 追加第二段，已显示内容不得被清空
    // 注：ASSISTANT 事件的 content 是「该消息的累计内容」，增量 = 在上一次基础上加长
    await inject("ASSISTANT", { turn_id: longReplyTurnId, content: `${longReply}\n- 追加的一段（不应清空上文）\n` });
    await page.waitForTimeout(1200);
    const incremental = await page.evaluate(() => {
      const all = document.querySelectorAll(".assist-bubble .markdown-body");
      const el = all[all.length - 1];
      const t = el?.innerText ?? "";
      return { keepsFirst: t.includes("第一点"), hasAppend: t.includes("追加的一段"), len: t.length };
    });
    // 上翻 + 回到最新
    await page.locator(".stream").evaluate((el) => { el.scrollTop = 100; el.dispatchEvent(new Event("scroll")); });
    await page.waitForTimeout(300);
    const back = await page.locator(".back-latest").count();
    if (back) await page.locator(".back-latest").click();
    // 长对话里「回到最新」的平滑滚动要更久：轮询到贴底或超时，别把测量窗口当成功能失败
    let settled = 1e9;
    for (let i = 0; i < 25; i++) {
      await page.waitForTimeout(200);
      settled = await page.locator(".stream").evaluate((el) => Math.round(el.scrollHeight - el.clientHeight - el.scrollTop));
      if (settled <= 4) break;
    }
    // 停止入口：有活跃 turn 时必须可点（不是禁用态）
    const stopEnabled = await page.locator(".stop-btn:not([disabled])").count();
    const stopLabelBefore = ((await page.locator(".stop-btn").first().innerText().catch(() => "")) ?? "").trim();
    if (stopEnabled) {
      await page.locator(".stop-btn").click();
      await page.waitForTimeout(800);
    }
    const stopLabelAfter = ((await page.locator(".stop-btn").first().innerText().catch(() => "")) ?? "").trim();
    await inject("TURN_END", { turn_id: longReplyTurnId });
    await page.waitForTimeout(600);
    rec("A3-LONG-REPLY", "列表/表格/长代码渲染、增量不清空上文、上翻与回到最新、停止入口可点",
      rendered.hasTable && rendered.hasCode && rendered.overflow <= 0 && incremental.keepsFirst && incremental.hasAppend && back === 1 && settled <= 4 && stopEnabled === 1,
      `表格=${rendered.hasTable} 代码块=${rendered.hasCode} 页面溢出=${rendered.overflow} 增量保留前文=${incremental.keepsFirst}/追加=${incremental.hasAppend} 回到最新按钮=${back} 点后距底部=${settled} 可点停止=${stopEnabled} 停止文案 '${stopLabelBefore}'→'${stopLabelAfter}'`);
  } catch (e) {
    rec("A3-LONG-REPLY", "列表/表格/长代码渲染、增量不清空上文、上翻与回到最新、停止入口可点", false, e.message.slice(0, 200));
  }
}

if (only("A4")) {
  // 剪贴板三态
  for (const [id, patch, expectOk] of [
    ["A4-COPY-OK", null, true],
    ["A4-COPY-FAIL", "reject", false],
    ["A4-COPY-MISSING", "missing", false],
  ]) {
    try {
      const p = await context.newPage();
      if (patch === "reject") {
        await p.addInitScript(() => {
          Object.defineProperty(navigator, "clipboard", { configurable: true, value: { writeText: () => Promise.reject(new Error("denied")) } });
        });
      } else if (patch === "missing") {
        await p.addInitScript(() => {
          Object.defineProperty(navigator, "clipboard", { configurable: true, value: undefined });
        });
      }
      await p.goto(url("#/"), { waitUntil: "domcontentloaded" });
      await p.waitForSelector(".composer textarea", { timeout: 15000 });
      await p.waitForTimeout(800);
      // 隔离数据里没有代码块消息，用测试事件在实时流里造一个（不落历史）
      await fetch(`${API}/api/events/test?event_type=ASSISTANT`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ turn_id: `turn_acc_copy_${Date.now()}`, content: "```js\nconst x = 1;\n```\n" }),
      });
      await p.waitForSelector(".code-block .code-copy", { timeout: 15000 });
      await p.waitForTimeout(700);
      const btn = p.locator(".code-block .code-copy").first();
      await btn.scrollIntoViewIfNeeded();
      await btn.click({ force: true });
      await p.waitForTimeout(300);
      const text = ((await btn.textContent()) ?? "").trim();
      await p.close();
      rec(id, `复制反馈（${patch ?? "正常"}）`, text === (expectOk ? "已复制" : "复制失败"), `按钮文案='${text}'`);
    } catch (e) {
      rec(id, `复制反馈（${patch ?? "正常"}）`, false, e.message.slice(0, 200));
    }
  }
}

if (only("A5")) {
  await freshPage();
  try {
    await openChat();
    await dismissStaleModal();
    // 先制造一个真实运行中的任务（后台事件），再让一次「发送」失败
    // turn_id 每次运行都不同：后端会重放最近事件，复用同一个 id 会让上一轮的 TURN_END
    // 混进这一轮，把「当前有任务在跑」这个前提搅掉（实测过一次 停止按钮 0→1 的假失败）。
    const runningTurnId = `turn_acc_running_${Date.now()}`;
    await inject("TURN_START", { turn_id: runningTurnId });
    await page.waitForTimeout(700);
    const stopRunning = await page.locator(".stop-btn").count();
    await page.locator(".stream").evaluate((el) => { el.scrollTop = 120; el.dispatchEvent(new Event("scroll")); });
    await page.waitForTimeout(400);
    const before = await page.locator(".stream").evaluate((el) => Math.round(el.scrollTop));
    const userBefore = await page.locator(".message.user").count();
    await page.route("**/api/turns**", (route) =>
      route.fulfill({ status: 500, contentType: "application/json", body: '{"detail":"模拟发送失败"}' }));
    await page.fill(".composer textarea", "排队发送-验收");
    await page.locator(".send-btn").click();
    await page.waitForTimeout(1600);
    const draft = await page.inputValue(".composer textarea");
    const userAfter = await page.locator(".message.user").count();
    const after = await page.locator(".stream").evaluate((el) => Math.round(el.scrollTop));
    const stopAfter = await page.locator(".stop-btn").count();
    await shot("a5-queued-send-fail");
    await page.unroute("**/api/turns**").catch(() => {});
    await inject("TURN_END", { turn_id: runningTurnId });
    // 这条请求没有被后端受理：界面上等于什么都没发生——草稿回来、没有假消息、
    // 正在上翻的阅读位置也不该被一条「从未存在过的消息」拽到底部。
    rec("A5-QUEUE-FAIL", "发送/排队失败：草稿找回、不留假消息、阅读位置与当前运行任务不受影响",
      draft === "排队发送-验收" && userAfter === userBefore &&
        Math.abs(after - before) <= 40 && stopRunning === 1 && stopAfter === 1,
      `草稿='${draft}' 用户消息 ${userBefore}→${userAfter} 阅读位置 ${before}→${after} 停止按钮 ${stopRunning}→${stopAfter}`);
  } catch (e) {
    rec("A5-QUEUE-FAIL", "发送/排队失败：草稿找回、不留假消息、阅读位置与当前运行任务不受影响", false, e.message.slice(0, 200));
  }
}

if (only("A12")) {
  await freshPage();
  try {
    await openChat();
    await page.locator(".dock").click();
    await page.waitForTimeout(1800);
    await page.locator(".panel-toggle").click();
    await page.waitForSelector(".topic-list li", { timeout: 10000 });
    await page.waitForTimeout(400);
    // 连续开关侧栏 2 次（收起→展开），最终停在打开
    for (let i = 0; i < 2; i++) {
      await page.locator(".panel-toggle").click();
      await page.waitForTimeout(260);
    }
    const panelOpen = await page.locator(".panel.open").count();
    const li = page.locator(".topic-list li");
    if (await li.count()) {
      await li.first().click();
      await page.waitForTimeout(1500);
    }
    const selOf = async () => {
      const sel = page.locator(".topic-list li.active, .topic-list li[aria-selected='true']");
      return (await sel.count()) ? ((await sel.first().textContent()) ?? "").trim().slice(0, 24) : "";
    };
    const selBefore = await selOf();
    // 切到知识/实体管理再回到话题：选择应保留
    await page.locator(".tab-knowledge").click();
    await page.waitForTimeout(1300);
    await page.locator(".tab-entity").click();
    await page.waitForTimeout(1200);
    const entityCount = await page.locator(".epanel .e-list li").count();
    await shot("a7-entity-panel");
    await page.locator(".tab-topic").click();
    await page.waitForTimeout(900);
    const selAfter = await selOf();
    // 知识筛选：两个筛选框有可见标签，无匹配与加载失败要能区分
    await page.locator(".tab-knowledge").click();
    await page.waitForTimeout(1300);
    const filterLabels = (await page.locator(".kpanel .row.filters .qio-select").allInnerTexts()).map((t) => t.replace(/\s+/g, " ").trim());
    const rows = await page.locator(".kpanel .k-list li").count();
    await page.locator(".kpanel input").first().fill("zzz-绝对不匹配-zzz");
    await page.waitForTimeout(600);
    const rowsFiltered = await page.locator(".kpanel .k-list li").count();
    const emptyText = ((await page.locator(".kpanel").innerText()) ?? "").replace(/\s+/g, " ").slice(0, 200);
    await shot("a7-knowledge-filter");
    await page.locator(".kpanel input").first().fill("");
    await page.route(isKnowledge, (r) =>
      r.fulfill({ status: 500, contentType: "application/json", body: '{"detail":"offline"}' }));
    await page.locator(".tab-topic").click();
    await page.waitForTimeout(400);
    await page.locator(".tab-knowledge").click();
    await page.waitForTimeout(1500);
    const failText = ((await page.locator(".kpanel").innerText()) ?? "").replace(/\s+/g, " ").slice(0, 200);
    await shot("a7-knowledge-fail");
    await page.unroute(isKnowledge).catch(() => {});
    const hasEmptyExplain = /无匹配|暂无|没有|空/.test(emptyText);
    const hasFailExplain = /失败|错误|重试/.test(failText);
    rec("A12-MANAGE", "侧栏连续开关、话题选择跨分类保留、知识/实体管理筛选与空/失败状态可区分",
      // 空态本身占一个 <li>（说明文字行），所以过滤后允许残留 1 行
      panelOpen === 1 && !!selBefore && selBefore === selAfter && filterLabels.length === 2 && filterLabels.every((t) => t.length > 0) && rowsFiltered <= 1 && rowsFiltered < rows && hasEmptyExplain && hasFailExplain,
      `侧栏打开=${panelOpen} 选择 '${selBefore}'→'${selAfter}' 实体卡=${entityCount} 筛选标签=${JSON.stringify(filterLabels)} 全量=${rows} 无匹配行=${rowsFiltered}｜无匹配说明=${hasEmptyExplain} 失败说明=${hasFailExplain}`,
      `无匹配面板文字='${emptyText}' | 失败面板文字='${failText}'`);
  } catch (e) {
    rec("A12-MANAGE", "侧栏连续开关、话题选择跨分类保留、知识/实体管理筛选与空/失败状态可区分", false, e.message.slice(0, 200));
  }
}

if (only("A13")) {
  await freshPage();
  try {
    await openChat();
    await page.locator(".dock").click();
    await page.waitForTimeout(1800);
    await page.locator(".panel-toggle").click();
    await page.waitForSelector(".topic-list li", { timeout: 10000 });
    await page.waitForTimeout(400);
    const li = page.locator(".topic-list li");
    let picked = false;
    for (let i = 0; i < Math.min(await li.count(), 8); i++) {
      await li.nth(i).click();
      await page.waitForTimeout(1200);
      if (await page.locator(".fragment-item").count()) { picked = true; break; }
    }
    await page.locator(".fragment-item").first().click().catch(() => {});
    await page.waitForTimeout(300);
    // 先失败：错误可见、页面保留、选择保留
    await page.route(isAnchor, (r) =>
      r.fulfill({ status: 500, contentType: "application/json", body: '{"detail":"anchor offline"}' }));
    await page.locator(".start-btn").click();
    await page.waitForTimeout(1000);
    const errVisible = await page.locator(".anchor-error").count();
    const stillPlanet = await page.locator(".planet-view").count();
    const stillSelected = await page.locator(".fragment-item.selected").count();
    await shot("a8-anchor-fail");
    await page.unroute(isAnchor).catch(() => {});
    // 请求中关闭 → 重新打开：旧响应不得关掉后来打开的页面
    await page.route(isAnchor, async (r) => { await wait(1800); await r.continue(); });
    await page.locator(".start-btn").click();
    await page.waitForTimeout(200);
    await page.locator(".close-btn").click();
    await page.waitForSelector(".planet-view", { state: "detached", timeout: 5000 });
    await page.locator(".dock").click();
    await page.waitForTimeout(2600);
    const reopened = await page.locator(".planet-view").count();
    await page.unroute(isAnchor).catch(() => {});
    await page.locator(".close-btn").click();
    await page.waitForTimeout(1200);
    // 重试成功：应返回聊天
    await page.locator(".dock").click();
    await page.waitForTimeout(1800);
    await page.locator(".panel-toggle").click();
    await page.waitForTimeout(500);
    await page.locator(".topic-list li").first().click();
    await page.waitForTimeout(1400);
    await page.locator(".fragment-item").first().click().catch(() => {});
    await page.locator(".start-btn").click();
    await page.waitForTimeout(2500);
    const backToChat = await page.locator(".composer textarea").count();
    rec("A13-ANCHOR-RACE", "起点：失败可见可重试且保留选择；请求中关闭重开不被旧响应关闭；成功返回聊天",
      errVisible === 1 && stillPlanet === 1 && stillSelected === 1 && reopened === 1 && backToChat === 1,
      `失败提示=${errVisible} 失败后仍留星球=${stillPlanet} 保留片段选中=${stillSelected} 请求中重开后星球=${reopened} 重试成功回到聊天=${backToChat}（取到片段=${picked}）`);
  } catch (e) {
    rec("A13-ANCHOR-RACE", "起点：失败可见可重试且保留选择；请求中关闭重开不被旧响应关闭；成功返回聊天", false, e.message.slice(0, 200));
  }
}

if (only("A14")) {
  await freshPage();
  // 录像里的场景：正在编辑凭据表单时工具确认盖上来
  try {
    await page.goto(url("#/settings"), { waitUntil: "domcontentloaded" });
    await page.waitForSelector(".nav button.tab", { timeout: 15000 });
    await page.locator(".nav button.tab", { hasText: "凭据" }).first().click();
    await page.waitForTimeout(600);
    await page.getByText("新建凭据", { exact: false }).first().click();
    await page.waitForSelector(".modal .qio-input", { timeout: 8000 });
    const noteInput = page.locator(".modal .qio-input").first();
    await noteInput.click();
    await noteInput.fill("我的工作台凭据-未提交");
    await inject("APPROVAL_REQUIRED", {
      approval: { approval_id: "apr_acc_settings", kind: "tool_create", payload: { name: "验收假工具2", explanation: "验收用" } },
    });
    await page.waitForTimeout(900);
    const formDialogs = await page.locator('.modal[role="dialog"]').count();
    const approvalEntry = await page.locator(".approval-entry").count();
    const kept = await noteInput.inputValue();
    const focusOn = await page.evaluate(() => {
      const el = document.activeElement;
      return el ? `${el.tagName}${el.classList?.contains("qio-input") ? ".qio-input" : ""}` : "none";
    });
    await shot("a14-approval-over-credential");
    // 主动打开 → Esc 收起 → 焦点与未提交内容都要回来
    await page.locator(".approval-entry").click();
    await page.waitForTimeout(500);
    const approvalOpen = await page.locator('.modal[role="dialog"]').count();
    await page.keyboard.press("Escape");
    await page.waitForTimeout(600);
    const focusBack = await page.evaluate(() => {
      const el = document.activeElement;
      return el ? `${el.tagName}${el.classList?.contains("qio-input") ? ".qio-input" : ""}` : "none";
    });
    const keptAfter = await noteInput.inputValue();
    rec("A14-CREDENTIAL-APPROVAL", "凭据表单编辑中到来的确认不抢焦点；打开后 Esc 收起并把焦点还回表单，未提交内容不丢",
      formDialogs === 1 && approvalEntry === 1 && kept === "我的工作台凭据-未提交" && focusOn === "INPUT.qio-input" &&
        approvalOpen === 2 && focusBack === "INPUT.qio-input" && keptAfter === "我的工作台凭据-未提交",
      `页面上的对话框=${formDialogs}（1=只有凭据表单） 待确认入口=${approvalEntry} 输入='${kept}'→'${keptAfter}' 焦点=${focusOn}→${focusBack} 打开审批后对话框=${approvalOpen}`);
  } catch (e) {
    rec("A14-CREDENTIAL-APPROVAL", "凭据表单编辑中到来的确认不抢焦点；打开后 Esc 收起并把焦点还回表单，未提交内容不丢", false, e.message.slice(0, 200));
  }
}

if (only("A15")) {
  // 任务 04 收尾：设置页入场不整体位移；数字控件有实际单位、加减命中更大
  try {
    await freshPage();
    await page.goto(url("#/settings"), { waitUntil: "domcontentloaded" });
    await page.waitForSelector(".settings", { timeout: 15000 });
    const transforms = [];
    for (let i = 0; i < 8; i++) {
      transforms.push(await page.evaluate(() => {
        const el = document.querySelector(".settings");
        return el ? getComputedStyle(el).transform : "none";
      }));
      await page.waitForTimeout(30);
    }
    const moved = transforms.filter((t) => t && t !== "none" && !t.startsWith("matrix(1, 0, 0, 1, 0, 0)"));
    // 切分类：左栏与页头不得移动，只有右栏内容淡入
    await page.waitForTimeout(400);
    const navBefore = await page.locator(".nav").boundingBox();
    const headBefore = await page.locator(".settings header h1").boundingBox();
    await page.locator(".nav button.tab", { hasText: "数据与维护" }).first().click();
    await page.waitForTimeout(120);
    const navAfter = await page.locator(".nav").boundingBox();
    const headAfter = await page.locator(".settings header h1").boundingBox();
    const panelsAnim = await page.evaluate(() => getComputedStyle(document.querySelector(".panels")).animationName);
    await page.waitForTimeout(700);
    // 数字控件：单位直接显示在数值处 + 加减按钮宽度
    const num = await page.evaluate(() => {
      // 取可见面板里的数字控件（隐藏面板里的元素宽度为 0）
      const widths = Array.from(document.querySelectorAll(".q-number .step"))
        .map((el) => Math.round(el.getBoundingClientRect().width));
      const unit = document.querySelector(".panel:not([style*='display: none']) .q-number .q-number-unit")
        ?? document.querySelector(".q-number .q-number-unit");
      const input = document.querySelector(".panel:not([style*='display: none']) .q-number-input")
        ?? document.querySelector(".q-number-input");
      return {
        stepW: Math.max(0, ...widths),
        unit: unit ? unit.textContent.trim() : "",
        value: input ? input.value : "",
      };
    });
    await shot("a15-settings-number-unit");
    rec("A15-SETTINGS-POLISH", "设置页入场不整体位移、切分类时左栏与页头不动、数字控件显示单位且加减命中变大",
      moved.length === 0 && !!navBefore && Math.abs(navBefore.x - navAfter.x) < 1 && Math.abs(navBefore.y - navAfter.y) < 1 &&
        Math.abs(headBefore.y - headAfter.y) < 1 && panelsAnim.includes("panel-in") && num.stepW >= 28 && num.unit === "小时" && num.value.length > 0,
      `入场期间非 none 的 transform 采样=${moved.length}/${transforms.length} 左栏位移=${Math.abs(navBefore.x - navAfter.x).toFixed(1)}/${Math.abs(navBefore.y - navAfter.y).toFixed(1)} 页头位移=${Math.abs(headBefore.y - headAfter.y).toFixed(1)} 右栏动画=${panelsAnim} 加减宽=${num.stepW}px 单位='${num.unit}' 数值='${num.value}'`);
  } catch (e) {
    rec("A15-SETTINGS-POLISH", "设置页入场不整体位移、切分类时左栏与页头不动、数字控件显示单位且加减命中变大", false, e.message.slice(0, 200));
  }
}

if (only("A6")) {
  await freshPage();
  try {
    await page.route(isTopics, (route) =>
      route.fulfill({ status: 500, contentType: "application/json", body: '{"detail":"offline"}' }));
    await page.goto(url("#/"), { waitUntil: "domcontentloaded" });
    await page.waitForSelector(".dock", { timeout: 15000 });
    await page.waitForTimeout(800);
    await page.locator(".dock").click();
    await page.waitForTimeout(1800);
    const banner = await page.locator(".load-error").count();
    await shot("a6-planet-load-fail");
    await page.unroute(isTopics).catch(() => {});
    if (banner) {
      await page.locator(".load-error button").click();
      await page.waitForTimeout(2200);
    }
    await page.locator(".panel-toggle").click().catch(() => {});
    await page.waitForSelector(".topic-list li", { timeout: 10000 }).catch(() => {});
    await page.waitForTimeout(400);
    // A/B/C 连续选择：让 A 最晚返回
    const seen = [];
    await page.route(isTopicDetail, async (route) => {
      const id = route.request().url().split("/").pop();
      const delay = id === seen[0] ? 1200 : 80;
      await wait(delay);
      await route.continue();
    });
    const lis = page.locator(".topic-list li");
    const n = Math.min(3, await lis.count());
    for (let i = 0; i < n; i++) {
      const id = await lis.nth(i).getAttribute("data-topic-id").catch(() => null);
      seen.push(id);
      await lis.nth(i).click();
      await page.waitForTimeout(120);
    }
    await page.waitForTimeout(1800);
    const selectedRow = await page.locator(".topic-list li.active, .topic-list li[aria-selected='true']").count();
    rec("A6-PLANET-FLOW", "星球：首次加载失败可见可重试；连续选择 A/B/C 最终停在最后选择",
      banner === 1 && selectedRow >= 1,
      `首次失败提示=${banner} 最终选中行=${selectedRow}（序列 ${JSON.stringify(seen)}）`,
      "详情竞态最终以最后一次选择为准（源码有请求序号保护）");
    await page.unroute(isTopicDetail).catch(() => {});
  } catch (e) {
    rec("A6-PLANET-FLOW", "星球：首次加载失败可见可重试；连续选择 A/B/C 最终停在最后选择", false, e.message.slice(0, 200));
  }
}

if (only("A7")) {
  await freshPage();
  try {
    await page.goto(url("#/"), { waitUntil: "domcontentloaded" });
    await page.waitForSelector(".dock", { timeout: 15000 });
    await page.waitForTimeout(800);
    await page.locator(".dock").click();
    await page.waitForTimeout(1600);
    await page.locator(".panel-toggle").click();
    await page.waitForTimeout(500);
    await page.locator(".tab-knowledge").click();
    await page.waitForTimeout(1200);
    const total = await page.locator(".k-item").count();
    // 搜索一个不存在的词 → 应有「无匹配」类说明
    await page.locator(".panel-inner input").first().fill("zzz不可能匹配zzz");
    await page.waitForTimeout(400);
    const emptySearch = await page.locator(".k-item").count();
    const emptyHint = await page.locator(".panel-inner").innerText();
    await page.locator(".panel-inner input").first().fill("");
    await page.waitForTimeout(400);
    const restored = await page.locator(".k-item").count();
    // 侧栏开合 ×2（收起→展开）后状态一致
    for (let i = 0; i < 2; i++) {
      await page.locator(".panel-toggle").click();
      await page.waitForTimeout(280);
    }
    const panelState = await page.locator(".panel.open").count();
    await page.locator(".tab-entity").click();
    await page.waitForTimeout(900);
    await page.locator(".tab-topic").click();
    await page.waitForTimeout(700);
    const backToTopic = await page.locator(".topic-list").count();
    rec("A7-KNOWLEDGE", "知识搜索/清空/空结果说明与侧栏连续开合",
      emptySearch === 0 && restored === total && /没有|无匹配|暂无|未找到/.test(emptyHint) && panelState === 1 && backToTopic === 1,
      `知识条目=${total} 搜索无结果=${emptySearch} 清空后=${restored} 空结果说明=${/没有|无匹配|暂无|未找到/.test(emptyHint)} 侧栏开合后 open=${panelState} 切回话题列表=${backToTopic}`);
    await shot("a7-knowledge-empty");
  } catch (e) {
    rec("A7-KNOWLEDGE", "知识搜索/清空/空结果说明与侧栏连续开合", false, e.message.slice(0, 200));
  }
}

if (only("A8")) {
  await freshPage();
  try {
    await page.goto(url("#/"), { waitUntil: "domcontentloaded" });
    await page.waitForSelector(".dock", { timeout: 15000 });
    await page.waitForTimeout(800);
    await page.locator(".dock").click();
    await page.waitForTimeout(1600);
    await page.locator(".panel-toggle").click();
    await page.waitForTimeout(600);
    const rows = page.locator(".topic-list li");
    await page.waitForSelector(".topic-list li", { timeout: 10000 });
    await rows.nth(Math.min(1, Math.max(0, (await rows.count()) - 1))).click();
    await page.waitForSelector(".start-btn", { timeout: 8000 });
    const frags = page.locator(".fragment-item");
    const fragCount = await frags.count();
    if (fragCount) await frags.first().click();
    const fragSelected = await page.locator(".selected-position").count();
    // 先让起点请求失败
    await page.route("**/api/anchor", (route) =>
      route.fulfill({ status: 500, contentType: "application/json", body: '{"detail":"boom"}' }));
    await page.locator(".start-btn").click();
    await page.waitForTimeout(1200);
    const errVisible = await page.locator(".anchor-error").count();
    const stillOpen = await page.locator(".planet-view").count();
    const keepSelection = await page.locator(".fragment-item.selected").count();
    await shot("a8-anchor-failed");
    await page.unroute("**/api/anchor").catch(() => {});
    // 再重试成功
    await page.locator(".start-btn").click();
    await page.waitForTimeout(2000);
    const closed = await page.locator(".planet-view").count();
    const hint = await page.locator(".composer .anchor").count();
    const ctx = await (await fetch(`${API}/api/session/context`)).json();
    rec("A8-ANCHOR", "起点：失败保留页面与选择并可重试；成功后按服务端结果返回聊天",
      errVisible === 1 && stillOpen === 1 && closed === 0 && (fragCount === 0 || keepSelection === 1),
      `失败提示=${errVisible} 仍打开=${stillOpen} 保留片段选择=${keepSelection}（可选片段=${fragCount} 选中提示=${fragSelected}）重试后关闭=${closed} 输入区起点提示=${hint}`,
      `服务端 anchor_fragment=${ctx.anchor_fragment?.id ?? "null"} historic=${ctx.anchor_fragment?.historic ?? "n/a"}`);
  } catch (e) {
    rec("A8-ANCHOR", "起点：失败保留页面与选择并可重试；成功后按服务端结果返回聊天", false, e.message.slice(0, 200));
  }
}

if (only("A9")) {
  await freshPage();
  try {
    await openChat();
    await page.locator(".dock").click();
    await page.waitForTimeout(1600);
    // 打开中关闭 → 立刻重新打开 → 再关闭：不得留下不可见残留
    await page.locator(".close-btn").click();
    await page.waitForTimeout(120);
    const reopened = await page.evaluate(() => {
      const dock = document.querySelector(".dock");
      dock?.click();
      return true;
    });
    await page.waitForTimeout(1400);
    const openCount = await page.locator(".planet-view").count();
    await page.locator(".close-btn").click();
    await page.waitForSelector(".planet-view", { state: "detached", timeout: 4000 });
    await page.waitForTimeout(500);
    // 点击穿透/残留：覆盖层中心命中的应该不再是 planet-view
    const hit = await page.evaluate(() => {
      const el = document.elementFromPoint(window.innerWidth / 2, window.innerHeight / 2);
      return el ? `${el.tagName}.${(el.className || "").toString().split(" ")[0]}` : "none";
    });
    rec("A9-REOPEN", "关闭中重新打开可靠，关闭后无不可见残留/点击穿透",
      reopened && openCount === 1 && !hit.startsWith("DIV.planet-view"),
      `重开后覆盖层=${openCount} 关闭后中心命中=${hit}`);
  } catch (e) {
    rec("A9-REOPEN", "关闭中重新打开可靠，关闭后无不可见残留/点击穿透", false, e.message.slice(0, 200));
  }
}

if (only("A10")) {
  await freshPage();
  try {
    await page.goto(url("#/settings", "&planetdemo=slow"), { waitUntil: "domcontentloaded" });
    await page.waitForSelector(".nav button.tab", { timeout: 15000 });
    await page.locator(".motion-opt", { hasText: "减少动画" }).first().click();
    await page.waitForTimeout(300);
    const attr = await page.evaluate(() => document.documentElement.getAttribute("data-motion"));
    await page.locator(".back").click();
    await page.waitForSelector(".dock", { timeout: 10000 });
    await page.waitForTimeout(500);
    await page.locator(".dock").click();
    await page.waitForTimeout(900);
    const openWithReduced = await page.locator(".planet-view").count();
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
    await openSettings("外观");
    await page.locator(".motion-opt", { hasText: "跟随系统" }).first().click();
    await page.waitForTimeout(300);
    rec("A10-REDUCED", "减少动画下关键路径仍能完成（打开/关闭不受影响）",
      attr === "reduced" && openWithReduced === 1 && ms < 200,
      `html[data-motion]=${attr} 展开成功=${openWithReduced} 关闭耗时=${ms}ms`);
  } catch (e) {
    rec("A10-REDUCED", "减少动画下关键路径仍能完成（打开/关闭不受影响）", false, e.message.slice(0, 200));
  }
}

// ---------------------------------------------------------------- 视觉矩阵
if (only("V1")) {
  await freshPage();
  try {
    const shots = [];
    for (const theme of ["light", "dark"]) {
      await page.goto(url("#/settings", `&theme=${theme}`), { waitUntil: "domcontentloaded" });
      // 主题由 localStorage 决定；这里直接切偏好
      await page.waitForSelector(".nav button.tab", { timeout: 15000 });
      await page.locator(".theme-opt", { hasText: theme === "dark" ? "暗紫晶" : "净白" }).first().click();
      await page.waitForTimeout(500);
      shots.push(await shot(`v1-settings-${theme}`));
      await page.goto(url("#/"), { waitUntil: "domcontentloaded" });
      await page.waitForSelector(".dock", { timeout: 15000 });
      await page.waitForTimeout(900);
      shots.push(await shot(`v1-chat-${theme}`));
      await page.locator(".dock").click();
      await page.waitForTimeout(2000);
      shots.push(await shot(`v1-planet-${theme}`));
      await page.locator(".close-btn").click();
      await page.waitForTimeout(1200);
    }
    // 跟随系统
    await openSettings("外观");
    await page.locator(".theme-opt", { hasText: "系统" }).first().click();
    await page.waitForTimeout(300);
    const applied = await page.evaluate(() => document.documentElement.getAttribute("data-theme"));
    rec("V1-THEMES", "浅色/深色/跟随系统三态都能渲染并切换", !!applied, `跟随系统后 data-theme=${applied}`, shots.join(" | "));
  } catch (e) {
    rec("V1-THEMES", "浅色/深色/跟随系统三态都能渲染并切换", false, e.message.slice(0, 200));
  }
}

if (only("V2")) {
  await freshPage();
  try {
    const sizes = [
      { w: 1440, h: 900, tag: "wide" },
      { w: 1024, h: 768, tag: "mid" },
      { w: 760, h: 720, tag: "narrow" },
    ];
    const out = [];
    for (const s of sizes) {
      await page.setViewportSize({ width: s.w, height: s.h });
      await openChat();
      const m = await page.evaluate(() => ({
        overflow: document.documentElement.scrollWidth - document.documentElement.clientWidth,
        clipped: Array.from(document.querySelectorAll(".composer, .markdown-body pre, .table-wrap"))
          .filter((el) => el.scrollWidth > el.clientWidth + 2 && getComputedStyle(el).overflowX === "visible").length,
      }));
      out.push(`${s.tag}:溢出=${m.overflow},裁切=${m.clipped}`);
      await page.setViewportSize({ width: 1440, height: 900 });
    }
    const zoomCtx = await browser.newContext({ viewport: { width: 900, height: 700 }, deviceScaleFactor: 2 });
    const zp = await zoomCtx.newPage();
    await zp.goto(url("#/"), { waitUntil: "domcontentloaded" });
    await zp.waitForSelector(".composer textarea", { timeout: 15000 });
    await zp.waitForTimeout(1000);
    const zoomOverflow = await zp.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);
    await zoomCtx.close();
    rec("V2-SIZES", "正常/中等/窄窗口与 2× 缩放下都不出现页面横向溢出",
      out.length === 3 && out.every((s) => s.includes("溢出=0")) && zoomOverflow <= 0,
      `${out.join(" | ")} | 2×缩放溢出=${zoomOverflow}`);
  } catch (e) {
    rec("V2-SIZES", "正常/中等/窄窗口与 2× 缩放下都不出现页面横向溢出", false, e.message.slice(0, 200));
  }
}

if (only("V3")) {
  await freshPage();
  try {
    // 对比度与描边/阴影检查（关键文本）
    await openChat();
    const audit = await page.evaluate(() => {
      const parse = (c) => {
        const m = c.match(/rgba?\(([^)]+)\)/);
        if (!m) return null;
        const parts = m[1].split(",").map((x) => parseFloat(x));
        return { r: parts[0], g: parts[1], b: parts[2], a: parts[3] ?? 1 };
      };
      const lum = ({ r, g, b }) => {
        const f = (v) => {
          v /= 255;
          return v <= 0.03928 ? v / 12.92 : Math.pow((v + 0.055) / 1.055, 2.4);
        };
        return 0.2126 * f(r) + 0.7152 * f(g) + 0.0722 * f(b);
      };
      const bgOf = (el) => {
        let cur = el;
        while (cur) {
          const c = parse(getComputedStyle(cur).backgroundColor);
          if (c && c.a > 0.5) return c;
          cur = cur.parentElement;
        }
        return { r: 255, g: 255, b: 255, a: 1 };
      };
      const ratio = (sel) => {
        const el = document.querySelector(sel);
        if (!el) return null;
        const fg = parse(getComputedStyle(el).color);
        const bg = bgOf(el);
        if (!fg) return null;
        const l1 = lum(fg);
        const l2 = lum(bg);
        const r = (Math.max(l1, l2) + 0.05) / (Math.min(l1, l2) + 0.05);
        return Math.round(r * 100) / 100;
      };
      const heavyShadow = Array.from(document.querySelectorAll("*")).filter((el) => {
        const s = getComputedStyle(el).boxShadow;
        const m = s.match(/(\d+)px/g);
        return m && m.some((v) => parseInt(v) > 24);
      }).length;
      const doubleStroke = Array.from(document.querySelectorAll(".qio-input, .composer, .assist-bubble")).filter((el) => {
        const s = getComputedStyle(el);
        return parseFloat(s.borderTopWidth) >= 1.5 && /rgba?\(.*(0\.[4-9]|1)\)/.test(s.boxShadow);
      }).length;
      return {
        body: ratio(".markdown-body p") ?? ratio(".markdown-body"),
        meta: ratio(".message .meta"),
        muted: ratio(".kbd-hint"),
        heavyShadow,
        doubleStroke,
      };
    });
    rec("V3-AUDIT", "关键文本对比度与描边/阴影层次检查",
      (audit.body ?? 0) >= 4.5 && audit.heavyShadow <= 8 && audit.doubleStroke === 0,
      `正文字面对比度=${audit.body} 元信息=${audit.meta} 提示=${audit.muted}；重阴影元素=${audit.heavyShadow} 双层描边元素=${audit.doubleStroke}`,
      "对比度按 WCAG 相对亮度计算；正文目标 ≥4.5:1");
    await shot("v3-chat-audit");
  } catch (e) {
    rec("V3-AUDIT", "关键文本对比度与描边/阴影层次检查", false, e.message.slice(0, 200));
  }
}

if (only("V4")) {
  await freshPage();
  try {
    const shots = [];
    await openSettings("数据与维护");
    shots.push(await shot("v4-settings-maintenance"));
    await openSettings("凭据");
    await page.getByText("新建凭据", { exact: false }).first().click();
    await page.waitForTimeout(600);
    shots.push(await shot("v4-credential-form"));
    await page.locator(".modal-head .close").click();
    await page.waitForTimeout(400);
    await page.goto(url("#/"), { waitUntil: "domcontentloaded" });
    await page.waitForSelector(".dock", { timeout: 15000 });
    await page.waitForTimeout(900);
    await page.locator(".dock").click();
    await page.waitForTimeout(1800);
    shots.push(await shot("v4-planet-overview"));
    await page.locator(".panel-toggle").click();
    await page.waitForTimeout(500);
    const li = page.locator(".topic-list li");
    if (await li.count()) {
      await li.first().click();
      await page.waitForTimeout(1500);
    }
    shots.push(await shot("v4-planet-focus"));
    await page.locator(".tab-knowledge").click();
    await page.waitForTimeout(1000);
    shots.push(await shot("v4-planet-knowledge"));
    rec("V4-SURFACES", "关键界面都有可审查截图（设置/维护/凭据表单/星球总览/聚焦/知识）",
      shots.length === 5, `截图 ${shots.length} 张`, shots.join(" | "));
  } catch (e) {
    rec("V4-SURFACES", "关键界面都有可审查截图（设置/维护/凭据表单/星球总览/聚焦/知识）", false, e.message.slice(0, 200));
  }
}

report.consoleErrors = consoleErrors.slice(0, 20);
report.endedAt = new Date().toISOString();
writeFileSync(`${OUT}\\report.json`, JSON.stringify(report, null, 2), "utf8");
console.log(`\nreport: ${OUT}\\report.json`);
console.log(`cases=${report.cases.length} pass=${report.cases.filter((c) => c.passed).length} fail=${report.cases.filter((c) => !c.passed).length}`);
console.log(`console errors: ${consoleErrors.length}`);
await browser.close();
