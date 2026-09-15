// 任务 04 PART C 验收：审批弹窗的信息顺序、按钮层级、键盘行为、失败语义。
// 用法（服务需在跑：后端 8734、前端 5199）：
//   node scripts/baseline/qa/approval.mjs
// 输出：%TEMP%\qio-baseline\qa\report-approval.json 与 shots\*.png
// 说明：只用隔离数据目录里的假审批事件，不调用真实模型、不使用真实密钥。
import { createRequire } from "node:module";
import { mkdirSync, writeFileSync } from "node:fs";

const require = createRequire(import.meta.url);
const PW = "C:\\Users\\zxy\\.cache\\codex-runtimes\\codex-primary-runtime\\dependencies\\node\\node_modules\\playwright";
const { chromium } = require(PW);

const BASE = "http://127.0.0.1:5199";
const API = "http://127.0.0.1:8734";
const OUT = `${process.env.TEMP}\\qio-baseline\\qa`;
const SHOTS = `${OUT}\\shots`;
mkdirSync(SHOTS, { recursive: true });

const report = { startedAt: new Date().toISOString(), cases: [] };
const rec = (id, title, passed, actual, detail = null) => {
  report.cases.push({ id, title, passed: !!passed, actual: String(actual), detail });
  console.log(`[${passed ? "PASS" : "FAIL"}] ${id} ${title} :: ${actual}`);
};

let seq = Math.floor(Date.now() / 1000) % 100000;
const url = () => `${BASE}/?fresh=${++seq}#/`;
const wait = (ms) => new Promise((r) => setTimeout(r, ms));

const browser = await chromium.launch({
  channel: "msedge",
  headless: true,
  args: ["--use-gl=angle", "--use-angle=swiftshader", "--enable-unsafe-swiftshader"],
});
const context = await browser.newContext({ viewport: { width: 1280, height: 860 } });
// 先声明使用暗紫晶主题，否则无头浏览器默认 prefers-color-scheme=light，会把「暗色截图」拍成浅色。
// 只在用户没有主题偏好时写入，后面切浅色的步骤才能生效。
await context.addInitScript(() => {
  try {
    if (!localStorage.getItem("qio-theme")) localStorage.setItem("qio-theme", "dark");
  } catch {
    /* localStorage 不可用时忽略 */
  }
});
const page = await context.newPage();

/** 注入一条审批事件（后端测试注入口，不产生真实副作用） */
async function inject(approvalId, kind, payload) {
  await page.request.post(`${API}/api/events/test?event_type=APPROVAL_REQUIRED`, {
    data: { approval: { approval_id: approvalId, kind, payload } },
  });
  await page.waitForSelector('.modal-mask .modal', { timeout: 5000 });
  await wait(300);
}

const CAPS_WRITE = [
  "联网：是（限 api.example.com）",
  "读取文件：否",
  "写入文件：是",
  "启动进程：否",
  "使用凭据：无",
  "副作用：write",
];

async function main() {
  // 记录本次会话里发往 /api/approvals 的请求次数（用于「Enter 不会默认批准」）
  let approvalPosts = 0;
  page.on("request", (r) => {
    if (r.method() === "POST" && r.url().includes("/api/approvals")) approvalPosts++;
  });

  await page.goto(url());
  await page.setViewportSize({ width: 1280, height: 860 });
  await inject("qa_t04_1", "tool_create", {
    name: "fetch_doc",
    description: "抓取指定文档并写入工作区",
    explanation: "你要求自动归档资料，需要在联网抓取后落盘",
    capabilities: CAPS_WRITE,
    policy_fingerprint: "fp_qa_123",
    test_summary: "3/3 检查通过",
    test_details: [{ name: "dry_run", passed: true, detail: "无异常" }],
  });

  const facts = await page.evaluate(() => {
    const rows = Array.from(document.querySelectorAll(".modal .facts dt")).map((d) => d.textContent?.trim());
    const dd = Array.from(document.querySelectorAll(".modal .facts dd")).map((d) => d.textContent?.trim());
    const adv = document.querySelector(".modal details.adv");
    const style = (sel) => {
      const el = document.querySelector(sel);
      if (!el) return null;
      const cs = getComputedStyle(el);
      return { bg: cs.backgroundColor, color: cs.color, border: cs.borderTopColor, borderWidth: cs.borderTopWidth };
    };
    return {
      rows,
      dd,
      intent: document.querySelector(".modal .intent")?.textContent?.trim(),
      risks: Array.from(document.querySelectorAll(".modal .risk")).map((r) => r.textContent?.trim()),
      advOpen: adv ? adv.hasAttribute("open") : null,
      advText: adv ? adv.textContent?.replace(/\s+/g, " ").trim() : null,
      approve: style(".modal .approve"),
      reject: style(".modal .reject"),
      highRisk: document.querySelector(".modal")?.classList.contains("risk-high"),
      title: document.querySelector("#approval-title")?.textContent?.trim(),
      capText: document.querySelector(".modal .cap-box")?.textContent?.replace(/\s+/g, " ").trim() || "",
    };
  });

  rec("C1-INTENT", "它想做什么：一句话（取 description，不是枚举名）",
    facts.intent === "抓取指定文档并写入工作区", `intent=${facts.intent}`);
  rec("C1-ACCESS", "它会访问什么：具体行为标签（会联网 / 会修改文件）",
    facts.risks.includes("会联网") && facts.risks.includes("会修改文件"), `risks=${facts.risks.join("、")}`);
  rec("C1-CHANGE", "它会改变什么：显示人话「会写入或修改数据」，不显示 write",
    facts.rows.includes("会改变什么") && facts.dd.some((d) => d === "会写入或修改数据"),
    `rows=${facts.rows.join("/")} | dd=${facts.dd.join(" | ")}`);
  rec("C1-WHY", "为什么需要：独立一行给出原因",
    facts.rows.includes("为什么需要") && facts.dd.some((d) => d.includes("自动归档资料")),
    `dd=${facts.dd.join(" | ")}`);
  rec("C1-VERIFIED", "测试了吗：显示「已验证」+ 摘要",
    facts.dd.some((d) => d.includes("已验证") && d.includes("3/3 检查通过")),
    `dd=${facts.dd.join(" | ")}`);
  rec("C1-ADVANCED", "高级详情默认折叠（open=false），内含策略指纹与 raw params",
    facts.advOpen === false && /策略指纹：fp_qa_123/.test(facts.advText || "") && /policy_fingerprint/.test(facts.advText || ""),
    `open=${facts.advOpen} text=${(facts.advText || "").slice(0, 120)}…`);

  // 展开高级详情（用户能自己打开）
  await page.locator(".modal details.adv > summary").click();
  await wait(200);
  const advOpenAfter = await page.locator(".modal details.adv").evaluate((el) => el.hasAttribute("open"));
  rec("C1-ADVANCED-OPEN", "高级详情可展开", advOpenAfter === true, `open=${advOpenAfter}`);

  rec("C1-CAPLIST", "「它会访问什么」不再泄漏内部枚举（副作用：write 移出默认可读区）",
    !/副作用：/.test(facts.capText) && /policy_fingerprint/.test(facts.advText || ""),
    `cap-box=${facts.capText.slice(0, 60)}`);

  // 首屏可见性：默认滚动位置下「它想做什么」必须在可视区内（内容变多也不能把它挤出去）
  await page.locator(".modal .body").evaluate((el) => { el.scrollTop = 0; });
  await wait(120);
  const firstPaint = await page.evaluate(() => {
    const body = document.querySelector(".modal .body");
    const intentEl = document.querySelector(".modal .intent");
    const riskEl = document.querySelector(".modal .risk-row");
    const actions = document.querySelector(".modal .actions");
    const b = body.getBoundingClientRect();
    const i = intentEl.getBoundingClientRect();
    const r = riskEl.getBoundingClientRect();
    const a = actions.getBoundingClientRect();
    return {
      intentVisible: i.top >= b.top - 1 && i.bottom <= b.bottom + 1,
      riskVisible: r.top >= b.top - 1 && r.bottom <= b.bottom + 1,
      actionsVisible: a.bottom <= window.innerHeight && a.top >= 0,
      bodyScroll: body.scrollHeight - body.clientHeight,
    };
  });
  rec("C1-FIRSTPAINT", "默认滚动位置下「它想做什么」和风险标签都在可视区内",
    firstPaint.intentVisible && firstPaint.riskVisible && firstPaint.actionsVisible,
    `intent=${firstPaint.intentVisible} risk=${firstPaint.riskVisible} 按钮可见=${firstPaint.actionsVisible} 内容可滚动量=${firstPaint.bodyScroll}px`);

  // C3 按钮层级：批准=品牌强调色，拒绝=中性描边（不再是红色实心）
  const approveIsAccent = /197, 27, 125/.test(facts.approve?.bg || "");
  const rejectIsNeutral = /rgba\(0, 0, 0, 0\)/.test(facts.reject?.bg || "") && parseFloat(facts.reject?.borderWidth || "0") >= 1;
  rec("C3-HIERARCHY", "按钮层级：拒绝＝secondary 中性描边，批准＝primary",
    rejectIsNeutral && !!facts.approve,
    `reject(bg=${facts.reject?.bg}, border=${facts.reject?.borderWidth}) approve(bg=${facts.approve?.bg})`);
  rec("C3-HIGHRISK", "高风险操作给批准按钮降调（risk-high 生效）",
    facts.highRisk === true && !approveIsAccent,
    `risk-high=${facts.highRisk} approve.bg=${facts.approve?.bg}`);

  const shotDark = `${SHOTS}\\approval-dark-highrisk.png`;
  await page.screenshot({ path: shotDark });

  // C4 键盘：Enter 不应直接批准；ESC 不做决定；Tab 在对话框内循环
  const before = approvalPosts;
  await page.keyboard.press("Enter");
  await wait(400);
  const stillOpen = await page.locator(".modal").count();
  rec("C4-ENTER", "对话框获得焦点时按 Enter 不会默认批准",
    stillOpen === 1 && approvalPosts === before,
    `modal=${stillOpen} approvals请求=${approvalPosts - before}`);

  await page.keyboard.press("Escape");
  await wait(300);
  // 任务 04：Esc 按最上层处理＝收起窗口（稍后处理），不做决定、待办保留、入口亮出来
  const entryAfterEsc = await page.locator(".approval-entry").count();
  rec("C4-ESC", "ESC 不做出决定（收起窗口、保留待办、从入口可再打开）",
    (await page.locator(".modal").count()) === 0 && approvalPosts === before && entryAfterEsc === 1,
    `approvals请求=${approvalPosts - before} 收起后待确认入口=${entryAfterEsc}`);
  // 从入口重新打开，继续下面的焦点陷阱检查
  await page.locator(".approval-entry").click();
  await wait(400);

  const tabInfo = await page.evaluate(async () => {
    const root = document.querySelector(".modal");
    root?.focus();
    const focusables = Array.from(
      root.querySelectorAll('button:not([disabled]), input:not([disabled]), [href], [tabindex]:not([tabindex="-1"])'),
    );
    return { count: focusables.length, labels: focusables.map((f) => (f.textContent || f.getAttribute("aria-label") || "").trim().slice(0, 12)) };
  });
  await page.keyboard.press("Tab");
  const focusedAfterTab = await page.evaluate(() => {
    const el = document.activeElement;
    return { inDialog: !!el?.closest(".modal"), label: (el?.textContent || "").trim().slice(0, 12) };
  });
  rec("C4-TAB", "Tab 焦点仍在对话框内（焦点陷阱）",
    focusedAfterTab.inDialog === true,
    `可聚焦元素=${tabInfo.count} 焦点=${focusedAfterTab.label}`);

  // C5 失败语义：这是测试注入口造的 approval_id，后端没有对应的待审批记录 →
  // POST /api/approvals 会失败 → 弹窗必须保留且说明「未做出任何授权」（失败 ≠ 已批准）。
  await page.locator(".modal .reject").click();
  await page.waitForTimeout(900);
  const errVisible = await page.locator(".modal .approval-error").count();
  const errText = errVisible ? (await page.locator(".modal .approval-error").innerText()).trim() : "";
  rec("C5-FAIL", "审批失败：弹窗保留且明确「未做出任何授权」",
    errVisible === 1 && errText.includes("未做出任何授权"),
    `error=${errText.slice(0, 80)}`);

  const shotFail = `${SHOTS}\\approval-fail.png`;
  await page.screenshot({ path: shotFail });

  // C3 低风险：批准按钮恢复为品牌强调色（primary），证明「降调」只在高风险时发生
  await page.goto(url()); // 重新加载清空内存里的待审批队列（上面那条是故意失败的）
  await inject("qa_t04_lowrisk", "tool_create", {
    name: "report_only",
    description: "只读取统计信息并生成说明",
    explanation: "用于确认低风险审批的按钮仍是主操作色",
    capabilities: ["联网：否", "读取文件：否", "写入文件：否", "启动进程：否", "使用凭据：无", "副作用：pure"],
    test_summary: "1/1 检查通过",
  });
  const lowRisk = await page.evaluate(() => {
    const el = document.querySelector(".modal .approve");
    return {
      bg: el ? getComputedStyle(el).backgroundColor : null,
      highRisk: document.querySelector(".modal")?.classList.contains("risk-high"),
      text: document.querySelector(".modal").textContent.replace(/\s+/g, " "),
    };
  });
  rec("C3-LOWRISK", "低风险审批：批准按钮是主操作色（不是降调样式）",
    lowRisk.highRisk === false && /197, 27, 125/.test(lowRisk.bg || ""),
    `risk-high=${lowRisk.highRisk} approve.bg=${lowRisk.bg}`);
  rec("C3-READONLY", "只读操作用「只读 / 不修改任何数据」说清楚",
    /只读/.test(lowRisk.text) && /不修改任何数据/.test(lowRisk.text),
    `text=${lowRisk.text.slice(0, 80)}`);

  // 浅色主题下的可读性（截图 + 关键颜色仍是主题变量）
  await page.evaluate(() => localStorage.setItem("qio-theme", "light"));
  await page.goto(url());
  await inject("qa_t04_light", "tool_create", {
    name: "light_doc",
    description: "浅色主题下的审批外观检查",
    explanation: "确认净白主题下信息层级与按钮仍然清楚",
    capabilities: CAPS_WRITE,
    test_summary: "2/2 检查通过",
  });
  const shotLight = `${SHOTS}\\approval-light.png`;
  await page.screenshot({ path: shotLight });
  const lightTheme = await page.evaluate(() => document.documentElement.dataset.theme);
  rec("C6-LIGHT", "浅色主题下同样渲染（截图留档）", lightTheme === "light", `data-theme=${lightTheme}`);

  report.shots = [shotDark, shotFail, shotLight];
  await context.close();
  await browser.close();
  writeFileSync(`${OUT}\\report-approval.json`, JSON.stringify(report, null, 2), "utf-8");
  const failed = report.cases.filter((c) => !c.passed);
  console.log(`\n合计 ${report.cases.length} 项，失败 ${failed.length} 项。报告：${OUT}\\report-approval.json`);
  process.exit(failed.length ? 1 : 0);
}

await main();
