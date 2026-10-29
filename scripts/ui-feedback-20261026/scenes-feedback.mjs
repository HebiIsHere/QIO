// 真实应用：通知/仲裁排队/审批门控（设置页与星球覆盖层）/全局异常/一次性设置提醒
import { launch, shot, api, settle, writeJson, centerOffset, measureRadii, nativeAudit } from "./lib.mjs";
const OUT = "D:\\qio-dev\\_uf-shots\\feedback\\";
const URL = "http://127.0.0.1:5199/";
const results = { steps: [], checks: {}, shots: [] };
const { browser, page, logs } = await launch({ width: 1280, height: 820 });
function check(name, ok, detail = "") { results.checks[name] = { ok: Boolean(ok), detail: String(detail) }; console.log((ok ? "PASS " : "FAIL ") + name + (detail ? "  [" + detail + "]" : "")); }
async function publish(type, data) { return api("/api/events/test?event_type=" + type, "POST", data ?? {}); }
const approval = (id, name) => ({ approval: { approval_id: id, kind: "tool_create", label: "创建工具", purpose: "读取受控目录并生成摘要", state: "waiting", requires_confirmations: 1, created_at: new Date().toISOString(), expires_at: new Date(Date.now()+1800000).toISOString(), payload: { name, description: "截图用示例申请", source: "main_agent", capabilities: ["访问：受控临时目录", "副作用：无", "网络：不允许"] } } });
async function enqueue(key, text, taskIds, extra = {}) {
  return page.evaluate(async ([k, t, ids, x]) => {
    const n = await import("/src/stores/notifications.ts");
    n.useNotificationsStore().enqueue({ key: k, kind: "task_failed", text: t, taskIds: ids, mergeable: true, stillRelevant: () => true, ...x });
  }, [key, text, taskIds, extra]);
}
async function storeState() {
  return page.evaluate(async () => {
    const n = await import("/src/stores/notifications.ts");
    const a = await import("/src/stores/modalArbiter.ts");
    const u = await import("/src/stores/unifiedApprovals.ts");
    const s = n.useNotificationsStore();
    return {
      activeKey: s.activeKey, activeText: s.active ? s.active.text : "", notices: s.notices.length, mergeUntil: s.mergeUntil,
      arbiter: a.useModalArbiterStore().stack.map(x=>x.id+":"+x.kind), suspended: Boolean(a.useModalArbiterStore().suspended),
      approvalVisible: u.useUnifiedApprovalsStore().visible,
      focus: document.activeElement ? document.activeElement.tagName + "." + String(document.activeElement.className||"").split(" ")[0] : "",
      noticeBox: (() => { const e = document.querySelector('[data-test="top-notice"]'); if (!e) return null; const r = e.getBoundingClientRect(); return { x: Math.round(r.x), y: Math.round(r.y), w: Math.round(r.width), h: Math.round(r.height), radius: getComputedStyle(e).borderTopLeftRadius }; })(),
      modalBox: (() => { const e = document.querySelector(".ua-modal"); if (!e) return null; const r = e.getBoundingClientRect(); const cs = getComputedStyle(e); return { w: Math.round(r.width), h: Math.round(r.height), display: cs.display, visibility: cs.visibility, radius: cs.borderTopLeftRadius }; })(),
    };
  });
}
async function modalShown() { return page.evaluate(() => { const e = document.querySelector(".ua-modal"); if (!e) return false; const cs = getComputedStyle(e); const r = e.getBoundingClientRect(); return cs.display !== "none" && cs.visibility !== "hidden" && r.width > 0; }); }
try {
  await page.goto(URL + "#/conversation", { waitUntil: "domcontentloaded" });
  await page.waitForSelector(".conversation-workspace"); await settle(page, 2500);

  // A. 单项通知
  const focusBefore = await page.evaluate(() => (document.activeElement?.tagName || "") + "#" + (document.activeElement?.id || ""));
  await enqueue("uf-live:single", "「整理本周项目材料并生成摘要」失败了", ["task-live-1"]);
  await page.waitForTimeout(1000);
  let s = await storeState();
  results.steps.push({ name: "single-notice", state: s });
  check("单项失败通知显示", Boolean(s.noticeBox), JSON.stringify(s.noticeBox));
  check("通知在窗口水平居中", s.noticeBox ? Math.abs(s.noticeBox.x + s.noticeBox.w / 2 - 640) <= 1 : false, s.noticeBox ? String(s.noticeBox.x + s.noticeBox.w / 2) : "");
  check("通知 14px 圆角", s.noticeBox?.radius === "14px", String(s.noticeBox?.radius));
  check("通知不抢焦点", (s.focus.split(".")[0] || "") === focusBefore.split("#")[0], s.focus + " vs " + focusBefore);
  const geom = await centerOffset(page, '[data-test="top-notice"]', ".uf-notice__body");
  results.steps.push({ name: "notice-center", geom });
  check("通知文字中心偏差 ≤1px", geom ? Math.abs(geom.dx) <= 1 : false, geom ? String(geom.dx) : "");
  results.shots.push(await shot(page, "fb-01-notice-single-1280", OUT));
  // 窄窗
  for (const w of [480, 320]) {
    await page.setViewportSize({ width: w, height: 820 }); await page.waitForTimeout(500);
    const ss = await storeState();
    check("窄窗 " + w + " 通知仍在视口内", ss.noticeBox ? ss.noticeBox.x >= 0 && ss.noticeBox.x + ss.noticeBox.w <= w : false, JSON.stringify(ss.noticeBox));
    results.shots.push(await shot(page, "fb-02-notice-" + w, OUT));
  }
  await page.setViewportSize({ width: 1280, height: 820 }); await page.waitForTimeout(400);

  // B. 悬停暂停
  const hoverBox = await page.$('[data-test="top-notice"]');
  if (hoverBox) await hoverBox.hover();
  await page.waitForTimeout(1600);
  s = await storeState();
  check("悬停期间通知不消失", Boolean(s.noticeBox), s.activeKey || "gone");
  await page.mouse.move(20, 400);
  await page.waitForTimeout(4200);
  s = await storeState();
  check("移开后按剩余时间消失", !s.noticeBox, s.activeKey || "gone");

  // C. 多项合并
  await enqueue("uf-live:m1", "「任务甲」失败了", ["task-m1"]);
  await page.waitForTimeout(80);
  await enqueue("uf-live:m2", "「任务乙」失败了", ["task-m2"]);
  await page.waitForTimeout(80);
  await enqueue("uf-live:m3", "「任务丙」失败了", ["task-m3"]);
  await page.waitForTimeout(1000);
  s = await storeState();
  const noticeText = await page.evaluate(() => document.querySelector('[data-test="top-notice"]')?.innerText || "");
  results.steps.push({ name: "merged-notice", state: s, noticeText });
  check("同一时刻只有一条通知", await page.evaluate(() => document.querySelectorAll('[data-test="top-notice"]').length) === 1);
  check("多项合并为 N 项任务失败了", noticeText.includes("3 项任务失败了"), noticeText);
  results.shots.push(await shot(page, "fb-03-notice-merged", OUT));
  // 手动关闭
  await page.click(".uf-notice__close"); await page.waitForTimeout(300);
  check("关闭按钮只关通知", !(await storeState()).noticeBox);

  // D. 中央弹窗打开前已显示的通知应撤下并在弹窗结束后恢复
  await enqueue("uf-live:queue", "「弹窗期间到达的任务」失败了", ["task-q1"]);
  await page.waitForTimeout(900);
  check("排队测试前通知可见", Boolean((await storeState()).noticeBox));
  await publish("APPROVAL_REQUIRED", approval("uf-live-a1", "弹窗期间到达的审批"));
  await page.waitForTimeout(1200);
  s = await storeState();
  check("审批弹窗已打开", await modalShown(), JSON.stringify(s.arbiter));
  const noticeWhileModal = (await storeState()).noticeBox;
  results.steps.push({ name: "notice-vs-modal", state: s, noticeWhileModal });
  check("弹窗期间通知撤下", !noticeWhileModal, JSON.stringify(noticeWhileModal));
  await shot(page, "fb-04-modal-notice-queued", OUT);
  await page.keyboard.press("Escape");
  await page.waitForTimeout(700);
  s = await storeState();
  check("弹窗结束后通知恢复显示", Boolean(s.noticeBox), s.activeKey || "gone");
  await shot(page, "fb-05-notice-after-modal", OUT);
  await page.evaluate(async () => { const n = await import("/src/stores/notifications.ts"); n.useNotificationsStore().reset(); });
  await page.waitForTimeout(300);

  // E. 设置页不自动弹审批
  await page.goto(URL + "#/settings", { waitUntil: "domcontentloaded" });
  await page.waitForTimeout(2000);
  await publish("APPROVAL_REQUIRED", approval("uf-live-a2", "设置页到达的审批"));
  await page.waitForTimeout(1200);
  check("设置页不自动弹审批", !(await modalShown()), "modal=" + (await modalShown()));
  await page.click('[data-test="nonchat-approval-entry"]').catch(() => {});
  await page.waitForTimeout(600);
  check("设置页可经审批入口主动打开", await modalShown());
  results.shots.push(await shot(page, "fb-06-settings-approval", OUT));
  await page.keyboard.press("Escape"); await page.waitForTimeout(400);

  // F. 记忆星球覆盖层不自动弹；关闭后恢复
  await page.goto(URL + "#/conversation", { waitUntil: "domcontentloaded" });
  await page.waitForSelector(".conversation-workspace"); await settle(page, 2500);
  await page.click(".dock"); await page.waitForTimeout(4500);
  const planetOpen = await page.evaluate(() => { const e = document.querySelector(".planet-view"); return e ? e.className : "none"; });
  results.steps.push({ name: "planet", planetOpen });
  await publish("APPROVAL_REQUIRED", approval("uf-live-a3", "星球覆盖层到达的审批"));
  await page.waitForTimeout(1300);
  check("星球覆盖层不自动弹审批", !(await modalShown()), planetOpen);
  results.shots.push(await shot(page, "fb-07-planet-overlay", OUT));
  const closeBtn = await page.$(".planet-view .close-btn");
  if (closeBtn) { await closeBtn.click(); await page.waitForTimeout(1200); }
  await page.evaluate(() => { const a = document.querySelector(".planet-view .close-btn"); if (a) a.click(); });
  await page.waitForTimeout(1600);
  check("退出星球后按门控恢复提醒", await modalShown());
  if (await modalShown()) { await page.keyboard.press("Escape"); await page.waitForTimeout(400); }

  // G. 一次性设置提醒（真实组件 + 真实通知宿主）
  await page.evaluate(async () => {
    localStorage.removeItem("qio.onboarding.setup-notice-shown-v1");
    const o = await import("/src/stores/onboarding.ts");
    const store = o.useOnboardingStore();
    store.loaded = true;
    store.status = { done: false, show_wizard: false, has_content: true, has_credential: true, hint_dismissed: true };
    store.decided = true; store.opened = false; store.dismissed = true;
  });
  await page.waitForTimeout(1200);
  const setupText = await page.evaluate(() => document.querySelector('[data-test="top-notice"]')?.innerText || "");
  check("初始设置未完成的一次性提醒出现", setupText.includes("初始设置尚未完成"), setupText);
  results.shots.push(await shot(page, "fb-08-setup-notice", OUT));
  const storedFlag = await page.evaluate(() => localStorage.getItem("qio.onboarding.setup-notice-shown-v1"));
  check("真正显示后才写已提醒", storedFlag === "1", String(storedFlag));

  // H. 全局异常多个独立问题
  await publish("CREDENTIAL_STATUS", { status: "unavailable", message: "当前没有可用的模型凭据，请在「设置 → 凭据」里添加" });
  await page.waitForTimeout(200);
  await publish("FALLBACK", { message: "当前模型不支持原生工具调用，已使用兼容模式（功能可能受限）" });
  await page.waitForTimeout(900);
  const issueLabel = await page.evaluate(() => document.querySelector("[data-uf-issue-entry]")?.getAttribute("aria-label") || "");
  results.steps.push({ name: "global-issues", issueLabel });
  check("全局异常入口显示独立问题数量", Boolean(issueLabel), issueLabel);
  await page.click("[data-uf-issue-entry]").catch(() => {});
  await page.waitForTimeout(500);
  results.shots.push(await shot(page, "fb-09-global-issues", OUT));

  results.logs = logs.slice(0, 20);
} catch (e) { results.error = String(e && e.stack || e); console.error(results.error); }
finally {
  const p = writeJson("feedback-results", results, OUT);
  console.log("evidence:", p);
  console.log("FAILED:", Object.entries(results.checks).filter(([,v])=>!v.ok).map(([k])=>k).join(" | ") || "none");
  await browser.close();
}
