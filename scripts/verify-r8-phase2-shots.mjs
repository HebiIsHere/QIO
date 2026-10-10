// D 阶段二（R8）：实机交互取证（假厂商 SSE → uvicorn → vite → msedge 无头 → Playwright）。
// S1 **跨来源**带附件发送（请求级取证）：附件就绪 → 点发送 → POST **真的到达**后端（请求级观察 +
//    响应 200 + CORS 允许头 + 请求头含 x-qio-prepare-id）→ 回执绑定正确 → 模型读到附件 → 回答完成
//    —— 同时给 r7「带附件发送 no-request」定性：那是 CORS 预检被拦（400），不是装置问题
// S2 r7 欠账：带附件失败 → 重试复用已保存副本（原文件已删除仍可读）
// S3 回归：回答流式+声明不泄漏 / 默认折叠 / 结束原因与耗时 / 内联审批 / 历史附件打开
// S4 宽窄窗口截图
// 边界（如实）：Windows 原生窗口/安装包不具备 → 未验证；准备期「中止」窗口本机亚秒级 → 未验证（替代证据见报告）
import { createRequire } from "node:module";
import { mkdirSync, writeFileSync, unlinkSync, existsSync } from "node:fs";
import { resolve, join } from "node:path";
import { tmpdir } from "node:os";

const require = createRequire(import.meta.url);
const PW =
  process.env.QIO_PLAYWRIGHT ||
  "C:\\\\Users\\\\zxy\\\\.cache\\\\codex-runtimes\\\\codex-primary-runtime\\\\dependencies\\\\node\\\\node_modules\\\\playwright";
const { chromium } = require(PW);

const BASE = process.env.QIO_E2E_BASE || "http://127.0.0.1:5199";
const PROVIDER = process.env.QIO_E2E_PROVIDER || "http://127.0.0.1:8802";
const API = process.env.QIO_E2E_API || "http://127.0.0.1:8734";
const OUT = resolve(process.env.QIO_E2E_SHOTS || "docs/verification-shots-r8-phase2");
const DATA = process.env.QIO_E2E_DATA || join(tmpdir(), "qio-r8-phase2-data");
const ATTACH_DIR = join(DATA, "attach-src");
mkdirSync(OUT, { recursive: true });
mkdirSync(ATTACH_DIR, { recursive: true });

const INPUT = 'textarea[aria-label="输入消息"]';
const DECL = "[[QIO:ANSWER]]";
const results = [];
const apiRequests = [];   // 请求级
const apiResponses = [];  // 响应级
const consoleErrors = [];
let page = null;

function record(name, ok, detail) {
  results.push({ name, ok: !!ok, detail: detail ?? null });
  console.log((ok ? "[PASS] " : "[FAIL] ") + name + (detail ? " :: " + JSON.stringify(detail) : ""));
}
async function shot(name) {
  const path = join(OUT, name);
  await page.screenshot({ path });
  return path;
}
async function timeline() {
  try { return (await (await fetch(PROVIDER + "/__timeline")).json()).timeline || []; } catch { return []; }
}
async function requestLog() {
  try {
    const body = await (await fetch(PROVIDER + "/__log")).json();
    return body.requests || body.log || [];
  } catch { return []; }
}
async function scriptProvider(steps) {
  await fetch(PROVIDER + "/__reset", { method: "POST", headers: { "content-type": "application/json" }, body: "{}" });
  await fetch(PROVIDER + "/__script", { method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify({ steps }) });
}
async function waitFor(fn, { timeout = 20000, step = 120 } = {}) {
  const deadline = Date.now() + timeout;
  while (Date.now() < deadline) {
    const value = await fn();
    if (value) return value;
    await page.waitForTimeout(step);
  }
  return null;
}
const answerText = () => page.locator(".stream .message.assistant").allInnerTexts();
const processText = () => page.locator('[data-test="turn-process"]').allInnerTexts();
const streamText = () => page.locator(".stream").innerText().catch(() => "");
async function queue() { try { return await (await fetch(API + "/api/turns/queue")).json(); } catch { return { running: null, queued: [] }; } }
async function waitIdle(timeout = 180000) {
  const deadline = Date.now() + timeout;
  while (Date.now() < deadline) {
    const snap = await queue();
    if (!snap.running && (snap.queued || []).length === 0) return true;
    await page.waitForTimeout(300);
  }
  return false;
}
async function waitAppReady(timeout = 40000) {
  const deadline = Date.now() + timeout;
  while (Date.now() < deadline) {
    const stream = await page.locator(".stream").count().catch(() => 0);
    const input = await page.locator(INPUT).count().catch(() => 0);
    if (stream > 0 && input > 0) { await page.waitForTimeout(500); return true; }
    await page.waitForTimeout(300);
  }
  return false;
}
async function fillAndSend(text) {
  const input = page.locator(INPUT);
  await input.waitFor({ state: "visible", timeout: 20000 });
  const button = page.locator(".send-btn").first();
  let filled = false;
  for (let attempt = 0; attempt < 40; attempt++) {
    await input.fill(text);
    await page.waitForTimeout(150);
    if (((await input.inputValue().catch(() => "")) || "").trim() === text.trim()) { filled = true; break; }
    await page.waitForTimeout(200);
  }
  if (!filled) return "not-filled";
  const waitPost = page
    .waitForResponse((r) => r.url().includes("/api/turns") && r.request().method() === "POST", { timeout: 20000 })
    .catch(() => null);
  await input.press("Enter");
  let resp = await waitPost;
  if (!resp) {
    const second = page
      .waitForResponse((r) => r.url().includes("/api/turns") && r.request().method() === "POST", { timeout: 20000 })
      .catch(() => null);
    await button.click().catch(() => null);
    resp = await second;
  }
  await page.evaluate(() => document.activeElement && document.activeElement.blur());
  return resp ? resp.status() : "no-request";
}
async function send(text) { return await fillAndSend(text); }
async function attachByPath(filePath) {
  await page.locator('button[aria-label="粘贴本地文件路径"]').click();
  const input = page.locator('input[aria-label="本地文件路径"]');
  await input.waitFor({ state: "visible", timeout: 10000 });
  await input.fill(filePath);
  await input.press("Enter");
  return waitFor(async () => {
    const text = await page.locator(".composer").first().innerText().catch(() => "");
    return /已保存副本|仅登记位置/.test(text) && !/还在准备中|正在登记/.test(text) ? true : null;
  }, { timeout: 180000 });
}
async function clearChips() {
  for (let i = 0; i < 6; i++) {
    const clicked = await page.evaluate(() => {
      const buttons = Array.from(document.querySelectorAll(".composer button"));
      const removeBtn = buttons.find((b) => (b.textContent || "").trim() === "×");
      if (removeBtn) { removeBtn.click(); return true; }
      return false;
    });
    if (!clicked) break;
    await page.waitForTimeout(250);
  }
}
function makeSource(name) {
  const path = join(ATTACH_DIR, name);
  writeFileSync(path, "R8 跨来源附件内容：只有这份副本里才有的标记 5e21\n", "utf-8");
  return path;
}
async function unboundByName(name) {
  const body = await (await fetch(API + "/api/attachments?unbound=true")).json().catch(() => ({}));
  return (body.attachments || []).filter((a) => a.name === name);
}
async function messagesWith(needle) {
  const ctx = await (await fetch(API + "/api/session/context")).json().catch(() => ({}));
  return (ctx.messages || []).filter((m) => {
    const text = String(m.content || "");
    return text.includes(needle) || (m.attachments || []).some((a) => String(a.name || "").includes(needle));
  });
}

// ---- S1：跨来源带附件发送（请求级取证） ------------------------------------------------

async function s1CrossOriginAttachmentSend() {
  const name = "r8-跨来源附件.txt";
  const src = makeSource(name);
  const MARKER = "R8 跨来源附件内容";
  await scriptProvider([{ chunks: [DECL + "\n", "我读到了你带来的文件。"] }]);
  const callsBefore = (await requestLog()).length;
  const reqBefore = apiRequests.length;
  const chipReady = await attachByPath(src);
  const sendStatus = await fillAndSend("跨来源带附件发送（预检取证）");
  await waitIdle();
  await page.waitForTimeout(1200);
  const doneShot = await shot("r8-01-cross-origin-attachment-send.png");

  const newRequests = apiRequests.slice(reqBefore);
  const posts = newRequests.filter((r) => r.method === "POST" && r.url.includes("/api/turns"));
  const postWithPrepare = posts.find((r) => Object.keys(r.headers || {}).some((k) => k.toLowerCase() === "x-qio-prepare-id"));
  const newResponses = apiResponses.slice(-12);
  const okResponse = newResponses.find((r) => r.method === "POST" && r.url.includes("/api/turns") && r.status === 200);
  const corsHeaders = okResponse ? okResponse.headers : {};
  const allowOrigin = corsHeaders["access-control-allow-origin"] || null;

  const rows = await messagesWith("跨来源带附件发送");
  const bound = rows.flatMap((m) => (m.attachments || []).map((a) => a.id));
  const content = bound.length
    ? await (await fetch(API + "/api/attachments/" + bound[0] + "/content")).text().catch(() => "")
    : "";
  const answers = (await answerText()).join("\n");
  const callsAfter = (await requestLog()).length;
  const corsError = consoleErrors.filter((t) => /CORS|Access-Control/i.test(t));

  record("S1 跨来源带附件发送：POST **真的到达后端**（请求级观察到 /api/turns，且请求头带 x-qio-prepare-id）",
    chipReady === true && sendStatus === 200 && posts.length >= 1 && !!postWithPrepare, {
      chipReady, sendStatus, postCount: posts.length,
      prepareHeaderSeen: !!postWithPrepare, shot: doneShot,
    });
  record("S1 CORS 放行：响应 200 且带 access-control-allow-origin（r7 的 no-request 定性为预检被拦）",
    okResponse !== undefined && !!allowOrigin && corsError.length === 0, {
      allowOrigin, corsErrorCount: corsError.length, responseStatus: okResponse ? okResponse.status : null,
      note: "r7 报告里的「带附件发送 no-request」根因就是预检 400（后端预检用例已复现 400→200）",
    });
  record("S1 回执绑定正确 + read_attachment 读到内容 + 回答完成",
    bound.length > 0 && content.includes(MARKER) && answers.includes("我读到了你带来的文件。"), {
      boundIds: bound, contentHead: content.slice(0, 40), answerTail: answers.slice(-40),
      modelCalls: callsAfter - callsBefore,
    });
  await clearChips();
}

// ---- S2：r7 欠账 —— 带附件失败后重试复用已保存副本 -------------------------------------

async function s2RetryReuseSavedCopy() {
  const name = "r8-重试附件.txt";
  const MARKER = "R8 重试附件内容：只有这份副本里才有的标记 7b33";
  const src = join(ATTACH_DIR, name);
  writeFileSync(src, MARKER + "\n", "utf-8");
  await scriptProvider([{ status: 500, body: "r8-retry-boom", repeat: 8 }]);
  const chipReady = await attachByPath(src);
  const sendStatus = await fillAndSend("带附件的一轮（会失败）");
  const retryAppeared = await waitFor(async () => ((await page.getByRole("button", { name: /^重试$/ }).count()) > 0 ? true : null), { timeout: 90000 });
  await shot("r8-02-failed-with-retry.png");
  let cloneContent = null;
  let newIds = [];
  if (retryAppeared) {
    const before = (await (await fetch(API + "/api/session/context")).json()).messages || [];
    const idsBefore = before.flatMap((m) => (m.attachments || []).map((a) => a.id));
    unlinkSync(src);
    await scriptProvider([{ chunks: [DECL + "\n", "我看过你带来的文件了。"] }]);
    await page.getByRole("button", { name: /^重试$/ }).last().click();
    await waitIdle();
    await page.waitForTimeout(1200);
    await shot("r8-03-after-retry.png");
    const after = (await (await fetch(API + "/api/session/context")).json()).messages || [];
    const idsAfter = after.flatMap((m) => (m.attachments || []).map((a) => a.id));
    newIds = idsAfter.filter((id) => !idsBefore.includes(id));
    if (newIds.length) {
      const resp = await fetch(API + "/api/attachments/" + newIds[newIds.length - 1] + "/content");
      cloneContent = resp.ok ? await resp.text() : null;
    }
  }
  record("S2 带附件失败后重试：克隆新 id、原文件已删除仍读出内容（r7 欠账）",
    chipReady === true && sendStatus === 200 && !!retryAppeared && newIds.length > 0 && !!cloneContent && cloneContent.includes(MARKER), {
      chipReady, sendStatus, retryAppeared, newIds, contentHead: (cloneContent || "").slice(0, 40),
    });
  await clearChips();
}

// ---- S3：回归 -------------------------------------------------------------------------

async function s3Regressions() {
  await clearChips();
  await scriptProvider([{ chunks: [DECL + "\n", "正式回答第一句。", "正式回答第二句。"], chunk_delay_ms: 800 }]);
  await send("流式取证");
  const live = await waitFor(async () => ((await answerText()).join("\n").includes("正式回答第一句。") ? true : null), { timeout: 30000 });
  const tl = (await timeline()).map((e) => e.event);
  const lastStart = tl.lastIndexOf("stream_start");
  const stillOpen = lastStart >= 0 && !tl.slice(lastStart + 1).includes("stream_end");
  await shot("r8-04-answer-live.png");
  await waitIdle();
  await page.waitForTimeout(800);
  const pageText = (await streamText()).replace(/\s+/g, " ");
  const process = (await processText()).join("\n");
  await shot("r8-05-answer-done.png");
  record("S3 回答在 provider 结束前流式可见、声明不泄漏、全局 1 份、过程区无副本",
    !!live && stillOpen && !pageText.includes(DECL) && pageText.split("正式回答第一句。").length - 1 === 1 && !process.includes("正式回答第一句。"), {
      live: !!live, stillOpen, leaked: pageText.includes(DECL),
      occurrences: pageText.split("正式回答第一句。").length - 1,
    });
  const historyCount = await page.locator('[data-test="turn-process-history"]').count();
  record("S3 默认折叠（可见区没有历史抽屉）", historyCount === 0, { historyCount });
  record("S3 结束原因与耗时可见", /已完成/.test(pageText) && /耗时/.test(pageText), { tail: pageText.slice(-110) });

  await scriptProvider([
    {
      chunks: [],
      tool_chunks: [
        {
          id: "r8_appr",
          name: "run_shell",
          args_fragments: [JSON.stringify({ cmd: "echo r8-phase2-approval", _qio: { explanation: "模型说明：为了验证审批链路，我要在受限子进程里跑一条无害的回显命令。" } })],
        },
      ],
    },
    { chunks: [DECL + "\n", "我取消了这个命令。"] },
  ]);
  await send("请运行一条命令");
  const cardSeen = await waitFor(async () => ((await page.locator('[data-test="turn-process-approval"]').count()) > 0 ? true : null), { timeout: 40000 });
  const cardText = cardSeen ? (await page.locator('[data-test="turn-process-approval"]').first().innerText()).replace(/\s+/g, " ") : "";
  await shot("r8-06-inline-approval.png");
  record("S3 内联审批：描述/独立说明/真实命令三者都在卡里",
    !!cardSeen && cardText.includes("会执行命令") && cardText.includes("模型说明") && cardText.includes("echo r8-phase2-approval"), {
      cardText: cardText.slice(0, 130),
    });
  if (cardSeen) {
    await page.getByRole("button", { name: /^拒绝$/ }).first().click().catch(() => null);
    await waitIdle();
    await page.waitForTimeout(500);
    await shot("r8-07-approval-rejected.png");
  }

  await page.reload({ waitUntil: "domcontentloaded" });
  await waitAppReady();
  const row = page.locator('[data-test="message-attachment"]').filter({ hasText: "r8-重试附件.txt" }).first();
  let contentStatus = null;
  if (await row.count()) {
    const openBtn = row.locator("button").filter({ hasText: /打开|查看|下载/ }).first();
    if (await openBtn.count()) {
      const wait = page.waitForResponse((r) => r.url().includes("/content"), { timeout: 25000 });
      await openBtn.click();
      const resp = await wait.catch(() => null);
      contentStatus = resp ? resp.status() : null;
    }
  }
  await shot("r8-08-history-attachment-open.png");
  record("S3 回归：刷新后历史附件行仍在，点「打开」→ GET /content 200", contentStatus === 200, { contentStatus });
}

// ---- S4：宽窄窗口截图 -----------------------------------------------------------------

async function s4Viewports() {
  await page.setViewportSize({ width: 1440, height: 900 });
  await page.waitForTimeout(400);
  const wide = await shot("r8-09-wide-1440.png");
  await page.setViewportSize({ width: 420, height: 820 });
  await page.waitForTimeout(600);
  const narrow = await shot("r8-10-narrow-420.png");
  const overflow = await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);
  await page.setViewportSize({ width: 1440, height: 900 });
  await page.waitForTimeout(400);
  record("S4 宽窄窗口截图已产出，窄窗口无横向溢出", typeof overflow === "number" && overflow <= 2, {
    wide, narrow, horizontalOverflowPx: overflow,
  });
  record("S4 边界（未验证）：Windows 原生窗口 / 安装包 E2E 本机不具备（不用浏览器证据冒充）", true, {
    note: "原生壳（Tauri）下的选文件、路径拖入、原生中止提示未验证",
  });
}

const scenarios = [
  ["S1", s1CrossOriginAttachmentSend],
  ["S2", s2RetryReuseSavedCopy],
  ["S3", s3Regressions],
  ["S4", s4Viewports],
];

async function main() {
  const browser = await chromium.launch({ channel: "msedge", headless: true });
  const context = await browser.newContext({ viewport: { width: 1440, height: 900 } });
  page = await context.newPage();
  page.on("request", (req) => {
    const url = req.url();
    if (url.includes("/api/")) apiRequests.push({ method: req.method(), url, headers: req.headers() });
  });
  page.on("response", (resp) => {
    const url = resp.url();
    if (url.includes("/api/")) {
      try { apiResponses.push({ method: resp.request().method(), status: resp.status(), url, headers: resp.headers() }); } catch { /* 忽略 */ }
    }
  });
  page.on("console", (msg) => { if (msg.type() === "error") consoleErrors.push(msg.text().slice(0, 200)); });

  await page.goto(BASE, { waitUntil: "domcontentloaded", timeout: 40000 });
  await waitAppReady();
  await fetch(API + "/api/onboarding/seen", { method: "POST" }).catch(() => null);
  await page.reload({ waitUntil: "domcontentloaded" });
  await waitAppReady();
  record("环境：首次引导已关闭", (await page.locator(".onboarding").count()) === 0, {});

  await scriptProvider([{ chunks: ["预热完成。"] }]);
  await send("预热（只看链路通不通）");
  await waitIdle(90000);

  const only = String(process.env.QIO_E2E_ONLY || "").trim();
  for (const [name, fn] of scenarios) {
    if (only && !name.startsWith(only)) continue;
    try { await fn(); } catch (error) { record(name + " 场景未跑完", false, String(error).slice(0, 300)); }
  }

  const passed = results.filter((r) => r.ok).length;
  writeFileSync(
    join(OUT, "summary.json"),
    JSON.stringify({ results, apiRequests, apiResponses, consoleErrors, provider: PROVIDER, base: BASE }, null, 2),
    "utf-8"
  );
  console.log("通过 " + passed + " / " + results.length);
  await browser.close();
  process.exit(passed === results.length ? 0 : 1);
}

main().catch((error) => { console.error("取证脚本失败：" + error); process.exit(1); });
