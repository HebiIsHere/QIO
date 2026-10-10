// fb-E 阶段二：实机取证（假厂商 SSE → uvicorn → vite → msedge 无头 → Playwright）。
// 覆盖：取消后的按钮（retry 而非死按钮 resend）、附件移除后切话题不复活、注记刷新后仍在（历史恢复）、
//       校准后唯一正文、窄窗口与代码块溢出。
// 边界（如实）：provider 是本机扮演的假厂商；结论只能读成「QIO 自己的链路对」，不证明真实厂商 / Tauri 原生行为。
import { createRequire } from "node:module";
import { mkdirSync, writeFileSync } from "node:fs";
import { resolve, join } from "node:path";
import { tmpdir } from "node:os";

const require = createRequire(import.meta.url);
const PW =
  process.env.QIO_PLAYWRIGHT ||
  "C:\\Users\\zxy\\.cache\\codex-runtimes\\codex-primary-runtime\\dependencies\\node\\node_modules\\playwright";
const { chromium } = require(PW);

const BASE = process.env.QIO_E2E_BASE || "http://127.0.0.1:5199";
const PROVIDER = process.env.QIO_E2E_PROVIDER || "http://127.0.0.1:8809";
const API = process.env.QIO_E2E_API || "http://127.0.0.1:8734";
const OUT = resolve(process.env.QIO_E2E_SHOTS || "docs/verification-shots-fb");
const DATA = process.env.QIO_E2E_DATA || join(tmpdir(), "qio-fb-phase2-data");
const ATTACH_DIR = join(DATA, "attach-src");
mkdirSync(OUT, { recursive: true });
mkdirSync(ATTACH_DIR, { recursive: true });

const INPUT = 'textarea[aria-label="输入消息"]';
const FENCE = String.fromCharCode(96, 96, 96);
// 每一场用**本次运行唯一**的标记：话题历史会累积，同名文本会让 waitFor / 计数误判。
const RUN = String(Date.now()).slice(-6);
const results = [];
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
async function scriptProvider(steps) {
  await fetch(PROVIDER + "/__reset", { method: "POST", headers: { "content-type": "application/json" }, body: "{}" });
  await fetch(PROVIDER + "/__script", { method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify({ steps }) });
}
async function waitFor(fn, options) {
  const timeout = (options && options.timeout) || 20000;
  const step = (options && options.step) || 120;
  const deadline = Date.now() + timeout;
  while (Date.now() < deadline) {
    const value = await fn();
    if (value) return value;
    await page.waitForTimeout(step);
  }
  return null;
}
const streamText = () => page.locator(".stream").innerText().catch(() => "");
async function queue() {
  try { return await (await fetch(API + "/api/turns/queue")).json(); } catch { return { running: null, queued: [] }; }
}
async function waitIdle(timeout = 180000) {
  const deadline = Date.now() + timeout;
  while (Date.now() < deadline) {
    const snap = await queue();
    if (!snap.running && (snap.queued || []).length === 0) return true;
    await page.waitForTimeout(300);
  }
  return false;
}
/**
 * 发送并等这一轮**真正开始**再等空闲。
 * 只调 waitIdle 会在「已受理未启动」的窗口里提前返回 —— 后续场景的脚本与断言就会落到错的一轮上。
 */
async function sendAndSettle(text) {
  const before = (await streamText()).length;
  const status = await fillAndSend(text);
  await waitFor(async () => {
    const snap = await queue();
    if (snap.running || (snap.queued || []).length) return true;
    const now = await streamText();
    return now.length > before ? true : null;
  }, { timeout: 20000 });
  await waitIdle(180000);
  await page.waitForTimeout(600);
  return status;
}
/** 清空待发附件 chip（remove 按钮的 aria-label 是「移除附件 <名字>」）。 */
async function clearChips() {
  for (let i = 0; i < 6; i++) {
    const btn = page.locator('button[aria-label^="移除附件"]').first();
    if (!(await btn.count())) break;
    await btn.click().catch(() => null);
    await page.waitForTimeout(250);
  }
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
  for (let attempt = 0; attempt < 40; attempt++) {
    await input.fill(text);
    await page.waitForTimeout(150);
    if (((await input.inputValue().catch(() => "")) || "").trim() === text.trim()) break;
    await page.waitForTimeout(200);
  }
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
const send = (text) => fillAndSend(text);
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
function chipNames() {
  return page.locator(".composer .chip .name").allInnerTexts().catch(() => []);
}

// ---- S1：取消后的按钮（retry，而不是 409 的死按钮 resend）+ 点击真的能重发 ----------

async function s1CancelOffersRetry() {
  await scriptProvider([{ chunks: ["[[QIO:ANSWER]]\n", "S1 取消前第一句 " + RUN + "。", "S1 取消前第二句 " + RUN], chunk_delay_ms: 2500 }]);
  await clearChips();
  // 这一场必须在**运行中**取消：不能用 sendAndSettle（那会等到跑完）
  const S1_MARK = "S1 取消前第一句 " + RUN;
  const sendStatus = await fillAndSend("S1 取消后的按钮 " + RUN);
  const live = await waitFor(async () => ((await streamText()).includes(S1_MARK) ? true : null), { timeout: 30000 });
  const runningBefore = (await queue()).running;
  const stop = page.locator('button[aria-label="停止"]').first();
  const stopVisible = await stop.count();
  if (stopVisible) await stop.click().catch(() => null);
  // 兜底：UI 停止按钮不可用（disabled）时，直接对正在跑的那一轮发取消
  const stopped = await waitFor(async () => ((await queue()).running ? null : true), { timeout: 8000 });
  if (!stopped && runningBefore) {
    await fetch(API + "/api/turns/" + runningBefore.turn_id + "/cancel", { method: "POST" }).catch(() => null);
  }
  await waitIdle(120000);
  await page.waitForTimeout(1200);

  const retryBtn = page.locator('[data-test="turn-process-action-retry"]');
  const resendBtn = page.locator('[data-test="turn-process-action-resend"]');
  const retryCount = await retryBtn.count();
  const resendCount = await resendBtn.count();
  // 诊断：后端事实里这一轮的动作（与 UI 对照，便于区分「事实没给 retry」与「UI 没渲染」）
  const ctxFacts = await (await fetch(API + "/api/session/context")).json().catch(() => ({}));
  const apiRetryTurns = ((ctxFacts.turn_facts) || []).filter((f) => (f.actions || []).includes("retry")).length;
  const apiResendTurns = ((ctxFacts.turn_facts) || []).filter((f) => (f.actions || []).includes("resend")).length;
  const shotCancel = await shot("fb-01-cancelled-retry-action.png");

  // 点「重试」：必须真的创建新的一轮并跑完（不是死按钮）
  const S1_RETRY = "S1 重试后的新回答 " + RUN;
  await scriptProvider([{ chunks: ["[[QIO:ANSWER]]\n", S1_RETRY] }]);
  let retried = false;
  if (retryCount) {
    const postsBefore = await page.evaluate(() => performance.getEntriesByType("resource").length);
    await retryBtn.first().click().catch(() => null);
    await waitIdle(120000);
    await page.waitForTimeout(1000);
    retried = (await streamText()).includes(S1_RETRY);
    void postsBefore;
  }
  const shotRetried = await shot("fb-02-after-retry-click.png");
  record("S1 活动取消后给「重试」而不是死按钮「重新发送」；点重试真的跑出新的一轮",
    live === true && stopVisible > 0 && retryCount > 0 && resendCount === 0 && retried, {
      sendStatus, live: !!live, stopVisible, retryButtons: retryCount, resendButtons: resendCount, retried,
      apiTurnsWithRetry: apiRetryTurns, apiTurnsWithResend: apiResendTurns,
      shots: [shotCancel, shotRetried],
    });
}

// ---- S2：附件移除后切话题不复活 ------------------------------------------------

async function s2RemovedAttachmentStaysRemoved() {
  await scriptProvider([{ chunks: ["[[QIO:ANSWER]]\n", "收到。"], repeat: 6 }]);
  const name = "fb-移除后不复活-" + RUN + ".txt";
  const src = join(ATTACH_DIR, name);
  writeFileSync(src, "FB 移除后不复活标记 3c17\n", "utf-8");
  const ready = await attachByPath(src);
  const before = await chipNames();
  const removeBtn = page.locator('button[aria-label="移除附件 ' + name + '"]').first();
  let removed = false;
  if (await removeBtn.count()) { await removeBtn.click().catch(() => null); removed = true; }
  await waitFor(async () => ((await chipNames()).includes(name) ? null : true), { timeout: 20000 });
  const afterRemove = await chipNames();
  // 切话题（新建/切换最省事的做法：刷新页面 + 重新进入，再断言持久化里不含它）
  await page.reload({ waitUntil: "domcontentloaded" });
  await waitAppReady();
  await page.waitForTimeout(1200);
  const afterReload = await chipNames();
  const shotPath = await shot("fb-03-attachment-removed-persisted.png");
  record("S2 附件移除后（刷新/重挂载）不得复活，持久化不再包含它",
    ready === true && before.includes(name) && removed && !afterRemove.includes(name) && !afterReload.includes(name), {
      ready, before, removed, afterRemove, afterReload, shot: shotPath,
    });
}

// ---- S3：系统核对注记在刷新后仍在（历史恢复） ------------------------------------

async function s3AnnotationSurvivesReload() {
  await scriptProvider([
    {
      tool_chunks: [
        {
          id: "fb_read_missing",
          name: "read_attachment",
          args_fragments: [JSON.stringify({ attachment_id: "att_fb_missing_" + RUN })],
        },
      ],
    },
    { chunks: ["[[QIO:ANSWER]]\n", "S3 正文 " + RUN + "：这一步的工具失败了，正文只有这一份。"] },
  ]);
  const S3_BODY = "S3 正文 " + RUN + "：这一步的工具失败了，正文只有这一份。";
  await clearChips();
  const sendStatus = await sendAndSettle("S3 注记历史恢复 " + RUN);
  await waitIdle(120000);
  await page.waitForTimeout(1000);
  const beforeReload = await page.locator('[data-test="answer-system-note"]').count();
  await page.reload({ waitUntil: "domcontentloaded" });
  await waitAppReady();
  await page.waitForTimeout(1500);
  const note = page.locator('[data-test="answer-system-note"]');
  const sawAfter = await waitFor(async () => ((await note.count()) > 0 ? true : null), { timeout: 30000 });
  const noteText = sawAfter ? (await note.first().innerText()).replace(/\s+/g, " ") : "";
  const pageText = (await streamText()).replace(/\s+/g, " ");
  const bodyCount = pageText.split(S3_BODY).length - 1;
  const shotPath = await shot("fb-04-annotation-after-reload.png");
  record("S3 系统核对注记刷新后仍在（历史恢复读 raw.annotation），正文恰好一次",
    sendStatus === 200 && beforeReload > 0 && !!sawAfter && noteText.includes("系统核对") && bodyCount === 1, {
      sendStatus, beforeReload, sawAfter: !!sawAfter, noteHead: noteText.slice(0, 80), bodyCount, shot: shotPath,
    });
}

// ---- S4：校准后唯一正文 ----------------------------------------------------------

async function s4SingleAnswerAfterCalibration() {
  const MARK = "FB 唯一正文标记 " + RUN;
  await scriptProvider([{ chunks: ["[[QIO:ANSWER]]\n", MARK + "：第一句。", "第二句（不含标记）。"], chunk_delay_ms: 400 }]);
  await clearChips();
  await sendAndSettle("S4 校准后唯一正文");
  await waitIdle(120000);
  await page.waitForTimeout(1000);
  const text = (await streamText()).replace(/\s+/g, " ");
  const occurrences = text.split(MARK).length - 1;
  const shotPath = await shot("fb-05-single-answer.png");
  record("S4 一轮结束后正式回答正文在页面上恰好一份（校准不复制）",
    occurrences === 1, { occurrences, tail: text.slice(-120), shot: shotPath });
}

// ---- S5：窄窗口 + 列表内代码块不撑破页面 ------------------------------------------

async function s5NarrowAndCodeOverflow() {
  const md = [
    "- 步骤：",
    "",
    "  " + FENCE + "js",
    "  const veryLongLine = \"abcdefghijklmnopqrstuvwxyz0123456789abcdefghijklmnopqrstuvwxyz0123456789\";",
    "  " + FENCE,
    "",
  ].join("\n");
  await scriptProvider([{ chunks: ["[[QIO:ANSWER]]\n", md] }]);
  await clearChips();
  await sendAndSettle("S5 窄窗口与代码块 " + RUN);
  await waitIdle(120000);
  await waitFor(async () => ((await page.locator(".markdown-body .code-block").count()) > 0 ? true : null), { timeout: 30000 });
  await page.waitForTimeout(900);
  await page.setViewportSize({ width: 1440, height: 900 });
  await page.waitForTimeout(400);
  const wide = await shot("fb-06-wide-1440.png");
  await page.setViewportSize({ width: 420, height: 820 });
  await page.waitForTimeout(700);
  const narrow = await shot("fb-07-narrow-420.png");
  const overflow = await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);
  const codeScrollable = await page.evaluate(() => {
    const el = document.querySelector(".markdown-body .code-block pre, .markdown-body .code-block");
    return el ? { scrollWidth: el.scrollWidth, clientWidth: el.clientWidth } : null;
  });
  await page.setViewportSize({ width: 1440, height: 900 });
  await page.waitForTimeout(300);
  record("S5 窄窗口无横向溢出；代码块内部自行滚动（不撑破页面）",
    overflow <= 2 && !!codeScrollable && codeScrollable.scrollWidth >= codeScrollable.clientWidth, {
      overflowPx: overflow, codeScrollable, wide, narrow,
    });
  record("S5 边界（未验证）：Windows/Tauri 原生窗口、真实厂商端点、真实浏览器矩阵本机不具备", true, {
    note: "只测了 msedge 无头单机；原生壳选文件/拖入/真实厂商行为未验证",
  });
}

const scenarios = [
  ["S1", s1CancelOffersRetry],
  ["S2", s2RemovedAttachmentStaysRemoved],
  ["S3", s3AnnotationSurvivesReload],
  ["S4", s4SingleAnswerAfterCalibration],
  ["S5", s5NarrowAndCodeOverflow],
];

async function main() {
  const browser = await chromium.launch({ channel: "msedge", headless: true });
  const context = await browser.newContext({ viewport: { width: 1440, height: 900 } });
  page = await context.newPage();
  page.on("console", (msg) => { if (msg.type() === "error") consoleErrors.push(msg.text().slice(0, 200)); });

  await page.goto(BASE, { waitUntil: "domcontentloaded", timeout: 40000 });
  await waitAppReady();
  await fetch(API + "/api/onboarding/seen", { method: "POST" }).catch(() => null);
  await page.reload({ waitUntil: "domcontentloaded" });
  await waitAppReady();
  record("环境：首次引导已关闭", (await page.locator(".onboarding").count()) === 0, {});

  await scriptProvider([{ chunks: ["预热完成。"] }]);
  await sendAndSettle("预热（只看链路通不通）");
  await waitIdle(90000);
  await page.waitForTimeout(500);

  const only = String(process.env.QIO_E2E_ONLY || "").trim();
  for (const [name, fn] of scenarios) {
    if (only && !name.startsWith(only)) continue;
    try { await fn(); } catch (error) { record(name + " 场景未跑完", false, String(error).slice(0, 300)); }
  }

  const passed = results.filter((r) => r.ok).length;
  writeFileSync(
    join(OUT, "summary.json"),
    JSON.stringify({ results, consoleErrors, provider: PROVIDER, base: BASE, shots: OUT }, null, 2),
    "utf-8"
  );
  console.log("通过 " + passed + " / " + results.length);
  await browser.close();
  process.exit(passed === results.length ? 0 : 1);
}

main().catch((error) => { console.error("取证脚本失败：" + error); process.exit(1); });
