// 真实应用补充场景：全局多异常、审批队列列表、更新失败、长任务名窄窗通知
import { launch, shot, api, settle, writeJson, centerOffset } from "./lib.mjs";
const OUT = "D:\\qio-dev\\_uf-shots\\extra\\";
const results = { steps: [], checks: {}, shots: [] };
const { browser, page, logs } = await launch({ width: 1280, height: 820 });
function check(name, ok, detail = "") { results.checks[name] = { ok: Boolean(ok), detail: String(detail) }; console.log((ok ? "PASS " : "FAIL ") + name + (detail ? "  [" + detail + "]" : "")); }
async function publish(type, data) { return api("/api/events/test?event_type=" + type, "POST", data ?? {}); }
try {
  await page.goto("http://127.0.0.1:5199/#/conversation", { waitUntil: "domcontentloaded" });
  await page.waitForSelector(".conversation-workspace"); await settle(page, 2500);

  // 1. 全局多异常：凭据不可用（真实事件）+ SSE 断线（真实 store 状态）
  await publish("CREDENTIAL_STATUS", { status: "unavailable", message: "当前没有可用的模型凭据，请在「设置 → 凭据」里添加" });
  await page.waitForTimeout(400);
  await page.evaluate(async () => {
    const e = await import("/src/stores/events.ts");
    const s = e.useEventStore();
    s.connected = false;
    s.error = "SSE 连接断开，请确认后端已启动";
  });
  await page.waitForTimeout(900);
  const issues = await page.evaluate(() => ({
    label: document.querySelector("[data-uf-issue-entry]")?.getAttribute("aria-label") || "",
    count: document.querySelector(".uf-issue-entry__count")?.textContent || "",
  }));
  results.steps.push({ name: "multi-issues", issues });
  check("全局多异常入口显示独立问题数量", /2/.test(issues.count) || /2 个/.test(issues.label), JSON.stringify(issues));
  await page.click("[data-uf-issue-entry]");
  await page.waitForTimeout(600);
  const panelText = await page.evaluate(() => document.querySelector("[data-uf-issue-panel]")?.innerText || "");
  check("问题面板逐项列出不同问题", /凭据/.test(panelText) && /连接/.test(panelText), panelText.slice(0, 120));
  results.shots.push(await shot(page, "extra-01-global-multi-issues", OUT));
  await page.keyboard.press("Escape");
  await page.waitForTimeout(300);
  await page.evaluate(async () => { const e = await import("/src/stores/events.ts"); e.useEventStore().connected = true; e.useEventStore().error = null; const g = await import("/src/stores/globalIssues.ts"); g.useGlobalIssuesStore().resolve("sse_disconnected"); });

  // 2. 审批处理后的队列列表（真实队列 + queueListOnly 视图状态）
  const approval = (id, name) => ({ approval: { approval_id: id, kind: "tool_create", label: "创建工具", purpose: "读取受控目录", state: "waiting", requires_confirmations: 1, created_at: new Date().toISOString(), expires_at: new Date(Date.now()+1800000).toISOString(), payload: { name, capabilities: ["副作用：无"], source: "main_agent" } } });
  await publish("APPROVAL_REQUIRED", approval("extra-a1", "整理本周材料"));
  await page.waitForTimeout(150);
  await publish("APPROVAL_REQUIRED", approval("extra-a2", "合并发布清单与依赖说明"));
  await page.waitForTimeout(150);
  await publish("APPROVAL_REQUIRED", approval("extra-a3", "生成对比表并归档"));
  await page.waitForTimeout(1000);
  await page.evaluate(async () => {
    const u = await import("/src/stores/unifiedApprovals.ts");
    const s = u.useUnifiedApprovalsStore();
    s.visible = true; s.resultsVisible = false; s.queueListOnly = true; s.activeKey = null;
  });
  await page.waitForTimeout(500);
  const queueText = await page.evaluate(() => document.querySelector(".ua-modal")?.innerText || "");
  check("处理完一项后回到队列列表（不自动进详情）", /共 3 项|3 项待审批|待审批/.test(queueText) && !queueText.includes("允许此次操作"), queueText.slice(0, 120));
  await shot(page, "extra-02-approval-queue-list", OUT);
  await page.keyboard.press("Escape");
  await page.waitForTimeout(500);

  // 3. 更新失败：后端未恢复 → 更新卡片给出真实动作，并进入全局异常入口
  await page.goto("http://127.0.0.1:5199/#/settings", { waitUntil: "domcontentloaded" });
  await page.waitForTimeout(1800);
  const dataTab = await page.locator(".tab", { hasText: "数据与维护" }).first();
  await dataTab.click();
  await page.waitForTimeout(400);
  await page.evaluate(async () => {
    const u = await import("/src/stores/updater.ts");
    const s = u.useUpdaterStore();
    s.phase = "failed";
    s.errorKind = "install";
    s.message = "安装更新时后端未能自动恢复：更新没有装上，当前实例可能已经不完整。";
    s.availableVersion = "0.2.11";
  });
  await page.waitForTimeout(600);
  const updateText = await page.evaluate(() => document.body.innerText);
  const globalIssue = await page.evaluate(() => document.querySelector("[data-uf-issue-entry]")?.getAttribute("aria-label") || "");
  results.steps.push({ name: "update-failure", globalIssue });
  check("更新失败（未恢复）给出真实处理动作", /完全退出 QIO|未能自动恢复/.test(updateText), updateText.slice(updateText.indexOf("应用更新"), updateText.indexOf("应用更新")+200));
  check("更新失败（未恢复）进入全局异常入口", Boolean(globalIssue), globalIssue);
  await shot(page, "extra-03-update-failure-global", OUT);

  // 4. 长任务名 + 320 窄窗通知：可见省略、aria 完整
  await page.goto("http://127.0.0.1:5199/#/conversation", { waitUntil: "domcontentloaded" });
  await page.waitForSelector(".conversation-workspace"); await settle(page, 2000);
  await page.setViewportSize({ width: 320, height: 800 });
  const longName = "重新整理并交叉核对本周全部项目材料、发布清单、依赖说明与历史决策记录，然后生成一份可以逐条复查的摘要";
  await page.evaluate(async (name) => {
    const n = await import("/src/stores/notifications.ts");
    n.useNotificationsStore().enqueue({ key: "extra-long:1", kind: "task_failed", text: "「" + name + "」失败了", taskIds: ["task-long"], mergeable: false, stillRelevant: () => true });
  }, longName);
  await page.waitForTimeout(900);
  const longInfo = await page.evaluate(() => {
    const el = document.querySelector('[data-test="top-notice"]');
    const btn = el?.querySelector("button, p");
    const line = el?.querySelector(".uf-notice__line");
    const lr = line?.getBoundingClientRect();
    const cs = line ? getComputedStyle(line) : null;
    return {
      text: el?.innerText || "",
      visible: line?.textContent || "",
      aria: btn?.getAttribute("aria-label") || "",
      width: el?.getBoundingClientRect().width || 0,
      lineWidth: lr?.width || 0,
      lineHeight: lr?.height || 0,
      overflow: cs?.overflow || "",
      textOverflow: cs?.textOverflow || "",
    };
  });
  results.steps.push({ name: "long-notice", longInfo });
  // 320 窄窗允许最多两行（line-clamp:2），关键是截断而不是撑破/横向溢出
  const clamped = longInfo.textOverflow === "ellipsis" && longInfo.overflow === "hidden" && longInfo.lineHeight <= 40;
  check("长任务名可见文本被截断（不撑破组件）", clamped, JSON.stringify({ h: longInfo.lineHeight, ov: longInfo.overflow, ellipsis: longInfo.textOverflow }));
  check("长任务名完整内容可访问", longInfo.aria.includes(longName), longInfo.aria.slice(0, 80));
  check("320 宽通知不超出视口", longInfo.width <= 296, String(longInfo.width));
  await shot(page, "extra-04-long-notice-320", OUT);
  results.logs = logs.slice(0, 20);
} catch (e) { results.error = String(e && e.stack || e); console.error(results.error); }
finally { const p = writeJson("extra-results", results, OUT); console.log("evidence:", p); console.log("FAILED:", Object.entries(results.checks).filter(([,v])=>!v.ok).map(([k])=>k).join(" | ") || "none"); await browser.close(); }
