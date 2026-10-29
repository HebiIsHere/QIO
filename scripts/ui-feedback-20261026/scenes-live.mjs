// 真实应用场景：审批门控 / 通知排队 / 全局异常（Lead）
// 需要 python scripts/e2e_up.py 已启动（8734 后端 + 5199 vite），并已 seed onboarding。
import { launch, shot, api, settle, writeJson, APP } from "./lib.mjs";

const OUT = "D:\\qio-dev\\_uf-shots\\live\\";
const results = { steps: [], checks: {} };
const { browser, page, logs } = await launch({ width: 1280, height: 820 });

async function publish(type, data) {
  const r = await api("/api/events/test?event_type=" + type, "POST", data ?? {});
  results.steps.push({ action: "publish " + type, status: r.status });
  return r;
}
function check(name, ok, detail = "") {
  results.checks[name] = { ok: Boolean(ok), detail: String(detail) };
  console.log((ok ? "PASS " : "FAIL ") + name + (detail ? "  [" + detail + "]" : ""));
}
async function state() {
  return page.evaluate(() => ({
    modal: Boolean(document.querySelector('[role="dialog"][aria-modal="true"][data-unified-approval-modal], [data-unified-approval-modal] .ua-modal')),
    modalCount: document.querySelectorAll('[role="dialog"][aria-modal="true"]').length,
    approvalEntry: document.querySelector('[data-entry="approvals"]')?.getAttribute("aria-label") || "",
    notice: document.querySelector('[data-test="top-notice"]')?.innerText || "",
    issueEntry: document.querySelector('[data-uf-issue-entry]')?.getAttribute("aria-label") || "",
    focused: document.activeElement ? document.activeElement.tagName + "." + String(document.activeElement.className || "").split(" ")[0] : "",
    body: document.body.innerText.slice(0, 300),
  }));
}
const approvalPayload = (id, name) => ({
  approval: {
    approval_id: id,
    kind: "tool_create",
    label: "创建工具",
    purpose: "这个工具只能读取临时目录；用于截图验证",
    state: "waiting",
    requires_confirmations: 1,
    created_at: new Date().toISOString(),
    expires_at: new Date(Date.now() + 30 * 60 * 1000).toISOString(),
    payload: {
      name,
      description: "用于验证审批门控与对称视觉的示例申请",
      source: "main_agent",
      capabilities: ["访问：临时目录", "副作用：无", "网络：不允许"],
    },
  },
});

try {
  await page.goto(APP + "#/conversation", { waitUntil: "domcontentloaded", timeout: 30000 });
  await page.waitForSelector(".conversation-workspace", { timeout: 30000 });
  await settle(page, 2500);
  await shot(page, "live-00-conversation", OUT);
  check("反馈宿主已挂载", await page.evaluate(() => Boolean(document.querySelector("[data-uf-feedback-host]"))));

  // 1) 输入中到达审批：不弹，只更新入口
  const textarea = await page.$(".composer textarea");
  check("找到输入框", Boolean(textarea));
  if (textarea) {
    await textarea.focus();
    await page.keyboard.insertText("正在输入，不应被打断");
  }
  await publish("APPROVAL_REQUIRED", approvalPayload("live-a1", "输入中到达的申请"));
  await page.waitForTimeout(800);
  const s1 = await state();
  results.steps.push({ name: "inputing", state: s1 });
  check("输入中不自动弹审批", !s1.modal, s1.body.slice(0, 60));
  check("审批入口有数量", /1/.test(s1.approvalEntry), s1.approvalEntry);
  await shot(page, "live-01-approval-while-typing", OUT);

  // 2) 失焦后延迟再尝试 → 自动弹
  await page.mouse.click(900, 500);
  await page.waitForTimeout(900);
  const s2 = await state();
  results.steps.push({ name: "after-blur", state: s2 });
  check("失焦后自动弹出审批", s2.modal, s2.body.slice(0, 80));
  check("同时只有一个中央弹窗", s2.modalCount <= 1, String(s2.modalCount));
  await settle(page, 400);
  await shot(page, "live-02-approval-modal", OUT);

  // 3) 标题中心/圆角测量
  const geom = await page.evaluate(() => {
    const modal = document.querySelector(".ua-modal");
    const title = document.querySelector("#ua-title");
    if (!modal || !title) return null;
    const mr = modal.getBoundingClientRect();
    const tr = title.getBoundingClientRect();
    const action = modal.querySelector(".ua-actions button");
    const ar = action?.getBoundingClientRect();
    const close = modal.querySelector(".ua-close, [aria-label^='关闭']");
    const cr = close?.getBoundingClientRect();
    const cs = modal ? getComputedStyle(modal) : null;
    return {
      radius: cs?.borderTopLeftRadius,
      titleDx: Math.round((tr.left + tr.width / 2 - (mr.left + mr.width / 2)) * 100) / 100,
      titleDxFromBody: Math.round((tr.left + tr.width / 2 - (mr.left + 20 + (mr.width - 40) / 2)) * 100) / 100,
      closeRightGap: cr ? Math.round(mr.right - cr.right) : null,
      actionHeight: ar ? Math.round(ar.height) : null,
      width: Math.round(mr.width),
    };
  });
  results.steps.push({ name: "modal-geometry", geom });
  check("弹窗 14px 圆角", geom?.radius === "14px", String(geom?.radius));

  // 4) Esc 收起 → 不批准不拒绝，入口保留；不立即再弹同一条
  await page.keyboard.press("Escape");
  await page.waitForTimeout(700);
  const s3 = await state();
  results.steps.push({ name: "after-esc", state: s3 });
  check("Esc 收起弹窗", !s3.modal);
  check("收起后同一条不再自动弹", !s3.modal, s3.body.slice(0, 60));
  check("入口仍显示待审批数量", /1/.test(s3.approvalEntry), s3.approvalEntry);
  await shot(page, "live-03-after-defer", OUT);

  // 5) 新到达申请仍可自动弹（新身份）
  await publish("APPROVAL_REQUIRED", approvalPayload("live-a2", "第二条申请：排队的权限确认"));
  await page.waitForTimeout(900);
  const s4 = await state();
  results.steps.push({ name: "new-arrival", state: s4 });
  check("真正新到达的申请自动弹出", s4.modal, s4.body.slice(0, 80));
  await shot(page, "live-04-new-arrival", OUT);

  // 6) 结果/全局异常：凭据不可用 → 全局栏一个入口
  await page.keyboard.press("Escape");
  await page.waitForTimeout(500);
  await publish("CREDENTIAL_STATUS", { status: "unavailable", message: "当前没有可用的模型凭据，请在「设置 → 凭据」里添加" });
  await page.waitForTimeout(700);
  const s5 = await state();
  results.steps.push({ name: "credential-missing", state: s5 });
  check("全局异常入口显示", Boolean(s5.issueEntry), s5.issueEntry);
  await shot(page, "live-05-global-issue-entry", OUT);
  if (s5.issueEntry) {
    await page.click("[data-uf-issue-entry]");
    await page.waitForTimeout(500);
    const panel = await page.evaluate(() => ({
      open: Boolean(document.querySelector("[data-uf-issue-panel]")),
      text: document.querySelector("[data-uf-issue-panel]")?.innerText.slice(0, 500) || "",
      radius: (() => { const e = document.querySelector("[data-uf-issue-panel]"); return e ? getComputedStyle(e).borderTopLeftRadius : null; })(),
    }));
    results.steps.push({ name: "issue-panel", panel });
    check("问题面板展开", panel.open, panel.text.slice(0, 80));
    check("问题面板 14px 圆角", panel.radius === "14px", String(panel.radius));
    await shot(page, "live-06-global-issue-panel", OUT);
  }

} catch (error) {
  results.error = String(error && error.stack || error);
  console.error(results.error);
} finally {
  results.logs = logs.slice(0, 30);
  const path = writeJson("live-results", results);
  console.log("evidence:", path);
  const failed = Object.entries(results.checks).filter(([, v]) => !v.ok).map(([k]) => k);
  console.log("FAILED_CHECKS:", failed.join(" | ") || "none");
  await browser.close();
}
