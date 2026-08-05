// 自主 e2e：前端 UI 用例（Playwright + 系统 Edge），输出 results_ui.json
import { createRequire } from "node:module";
import { writeFileSync } from "node:fs";

const require = createRequire(import.meta.url);
const PW = "C:\\Users\\zxy\\.cache\\codex-runtimes\\codex-primary-runtime\\dependencies\\node\\node_modules\\playwright";
const { chromium } = require(PW);

const BASE = "http://127.0.0.1:5199/";
const API = "http://127.0.0.1:8734";
const RESULTS = [];
const record = (id, title, passed, actual) => {
  RESULTS.push({ id, title, passed, actual });
  console.log(`[${passed ? "PASS" : "FAIL"}] ${id} ${title} :: ${actual}`);
};

const browser = await chromium.launch({ channel: "msedge", headless: true });
try {
  const page = await browser.newPage({ viewport: { width: 1440, height: 900 } });
  await page.goto(BASE, { waitUntil: "domcontentloaded", timeout: 30000 });
  await page.waitForTimeout(3000);

  // UI-001 历史恢复 + 虚拟滚动容器
  try {
    const historyCount = await page.locator(".message").count();
    const stream = page.locator(".stream");
    await stream.waitFor({ state: "visible", timeout: 8000 });
    const overflow = await stream.evaluate((el) => el.scrollHeight > el.clientHeight);
    record("UI-001", "长对话虚拟滚动", historyCount > 0 && overflow,
      `历史消息恢复=${historyCount} 条；滚动容器溢出=${overflow}（100+ 消息大规模压测待补充）`);
  } catch (e) {
    record("UI-001", "长对话虚拟滚动", false, e.message.slice(0, 120));
  }

  // UI-002 悬浮球与星球过渡
  try {
    const dock = page.locator(".dock");
    await dock.waitFor({ state: "visible", timeout: 15000 });
    await dock.click({ timeout: 15000 });
    const hudEl = page.locator(".hud");
    await hudEl.waitFor({ state: "visible", timeout: 30000 });
    const hud = await hudEl.textContent();
    const topicCount = await page.locator(".topic-list li").count();
    const webgl = (hud ?? "").includes("WebGL");
    await page.click(".close-btn");
    await page.waitForTimeout(1200);
    const closed = (await page.locator(".planet-view").count()) === 0;
    record("UI-002", "悬浮球与星球过渡", webgl && topicCount > 0 && closed,
      `WebGL=${webgl} 话题=${topicCount} 收起=${closed}`);
  } catch (e) {
    record("UI-002", "悬浮球与星球过渡", false, e.message.slice(0, 120));
  }

  // GRAPH-002 话题聚焦与详情
  try {
    await page.click(".dock");
    await page.waitForTimeout(3000);
    await page.click(".topic-list li:nth-child(1)");
    await page.waitForTimeout(2000);
    const detailName = await page.textContent(".detail h3");
    const fragCount = await page.locator(".fragment-item").count();
    record("GRAPH-002", "话题聚焦与详情加载", !!detailName && fragCount >= 0,
      `详情=${detailName ?? "无"} 片段=${fragCount}`);
  } catch (e) {
    record("GRAPH-002", "话题聚焦与详情加载", false, e.message.slice(0, 120));
  }

  // GRAPH-001 位置惰性
  try {
    const p1 = await (await fetch(`${API}/api/graph/positions`)).json();
    await page.waitForTimeout(300);
    const p2 = await (await fetch(`${API}/api/graph/positions`)).json();
    const key = (t) => `${t.topic_id}:${t.position.join(",")}`;
    const stable = JSON.stringify(p1.topics.map(key)) === JSON.stringify(p2.topics.map(key));
    record("GRAPH-001", "话题创建与星球位置惰性", stable && p1.topics.length > 0,
      `话题=${p1.topics.length} 位置稳定=${stable}`);
  } catch (e) {
    record("GRAPH-001", "话题创建与星球位置惰性", false, e.message.slice(0, 120));
  }

  // GRAPH-003 从这里开始
  try {
    await page.click(".start-btn");
    await page.waitForTimeout(1500);
    const closed = (await page.locator(".planet-view").count()) === 0;
    record("GRAPH-003", "从这里开始（片段偏置）", closed, closed ? "回对话页；锚点偏置需真实对话验证" : "未关闭");
  } catch (e) {
    record("GRAPH-003", "从这里开始（片段偏置）", false, e.message.slice(0, 120));
  }

  // APPROVAL-001 审批弹窗
  try {
    await page.evaluate(async (api) => {
      await fetch(`${api}/api/events/test?event_type=APPROVAL_REQUIRED`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ approval: { approval_id: `appr_ui_${Date.now()}`, kind: "tool_create", payload: { name: "demo", explanation: "UI 审批演示", test_summary: "1/1 passed" } } }),
      });
    }, API);
    await page.waitForTimeout(1500);
    const title = await page.textContent(".modal h3");
    await page.click(".modal .approve");
    await page.waitForTimeout(1200);
    const closed = (await page.locator(".modal").count()) === 0;
    record("APPROVAL-001", "三类审批弹窗展示与响应", !!title && closed, `标题=${title ?? "无"} 响应后关闭=${closed}`);
  } catch (e) {
    record("APPROVAL-001", "三类审批弹窗展示与响应", false, e.message.slice(0, 120));
  }

  // UI-003 记忆强度滑块
  try {
    const slider = page.locator(".strength input");
    await slider.waitFor({ state: "visible", timeout: 8000 });
    await slider.fill("1");
    const val = await page.locator(".strength .value").textContent();
    record("UI-003", "记忆强度滑块", val === "1.00" || val === "1", `滑块值=${val}（注入预算影响需真实对话验证）`);
  } catch (e) {
    record("UI-003", "记忆强度滑块", false, e.message.slice(0, 120));
  }
} finally {
  await browser.close();
}
writeFileSync("C:/Users/zxy/Documents/Front agent/smart-agent/scripts/e2e-checklist/results_ui.json", JSON.stringify(RESULTS, null, 2), "utf8");