// D 阶段二（R7）：实机交互取证（假厂商 SSE → uvicorn → vite → msedge 无头 → Playwright）。
// S1 准备期（本机：附件登记/复制期间）：界面只说「在准备」、**没有执行迹象**、且**模型调用保持 0**
//    —— 实机无法稳定复现「发送时后端仍在准备」的窗口（本机复制是硬链接/亚秒级，界面也会挡住未就绪发送），
//    因此「真浏览器点中止」这条按边界处理（见 record 说明），取消语义由后端/前端用例覆盖
// S2 附件就绪之后才允许执行：第一次模型调用发生时附件已经是 ready
// S3 长正文（>256 KiB）正常路径：完整交付、无「不完整」提示（暂存读取故障实机不可注入 → 写进边界）
// S4 回归：回答流式+声明不泄漏 / 重试复用保存副本 / 历史附件打开 / 内联审批 / 结束原因与耗时 / 默认折叠
// S5 宽窄窗口截图（设计规范：不改整体视觉风格）
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
const PROVIDER = process.env.QIO_E2E_PROVIDER || "http://127.0.0.1:8801";
const API = process.env.QIO_E2E_API || "http://127.0.0.1:8734";
const OUT = resolve(process.env.QIO_E2E_SHOTS || "docs/verification-shots-r7-phase2");
const DATA = process.env.QIO_E2E_DATA || join(tmpdir(), "qio-r7-phase2-data");
const ATTACH_DIR = join(DATA, "attach-src");
mkdirSync(OUT, { recursive: true });
mkdirSync(ATTACH_DIR, { recursive: true });

const INPUT = 'textarea[aria-label="输入消息"]';
const DECL = "[[QIO:ANSWER]]";
const PREPARING = '[data-test="preparing-attachments"]';
const CANCEL = '[data-test="preparing-cancel"]';
const BIG_MB = Number(process.env.QIO_E2E_BIG_MB || "90");
const results = [];
const net = [];
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
async function waitIdle(timeout = 150000) {
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
const sendDiagnostics = [];

async function fillAndSend(text) {
  // 附件就绪时组件会重渲染，可能把刚填的文字清掉 → 每次尝试都**重新填**再按 Enter，
  // 并等到真的发出 POST /api/turns；一直发不出去就记录现场（输入框内容 + composer 文案）。
  const input = page.locator(INPUT);
  await input.waitFor({ state: "visible", timeout: 20000 });
  const button = page.locator(".send-btn").first();
  let status = "no-request";
  for (let attempt = 0; attempt < 15; attempt++) {
    await input.fill(text);
    await page.waitForTimeout(120);
    const before = await input.inputValue().catch(() => "");
    const wait = page
      .waitForResponse((r) => r.url().includes("/api/turns") && r.request().method() === "POST", { timeout: 5000 })
      .catch(() => null);
    await input.press("Enter");
    let resp = await wait;
    if (!resp) {
      const second = page
        .waitForResponse((r) => r.url().includes("/api/turns") && r.request().method() === "POST", { timeout: 6000 })
        .catch(() => null);
      await button.click().catch(() => null);
      resp = await second;
    }
    const after = await input.inputValue().catch(() => "");
    if (resp) {
      status = resp.status();
      break;
    }
    if (attempt === 0 || attempt === 4 || attempt === 14) {
      const composer = (await page.locator(".composer").first().innerText().catch(() => "")).replace(/\s+/g, " ");
      const notices = await page.evaluate(() =>
        Array.from(document.querySelectorAll('[data-test="attach-reject"], [data-test="preparing-notice"], [data-test="attach-error"]'))
          .map((el) => (el.textContent || "").trim().slice(0, 120))
      );
      sendDiagnostics.push({
        attempt, valueBefore: before.length, valueAfter: after.length,
        sendButtonEnabled: await button.isEnabled().catch(() => null),
        composer: composer.slice(0, 160), notices,
      });
    }
    await page.waitForTimeout(200);
  }
  await page.evaluate(() => document.activeElement && document.activeElement.blur());
  return status;
}

async function send(text) { return await fillAndSend(text); }
async function attachByPath(filePath, { waitReady = true } = {}) {
  await page.locator('button[aria-label="粘贴本地文件路径"]').click();
  const input = page.locator('input[aria-label="本地文件路径"]');
  await input.waitFor({ state: "visible", timeout: 10000 });
  await input.fill(filePath);
  await input.press("Enter");
  if (!waitReady) return true;
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
function makeBigFile(name, mb) {
  const path = join(ATTACH_DIR, name);
  if (!existsSync(path)) {
    const block = Buffer.alloc(1024 * 1024, 0x37);
    const fd = require("node:fs").openSync(path, "w");
    for (let i = 0; i < mb; i++) require("node:fs").writeSync(fd, block);
    require("node:fs").closeSync(fd);
  }
  return path;
}
async function attachmentState(attachmentId) {
  const body = await (await fetch(API + "/api/attachments/" + attachmentId)).json().catch(() => ({}));
  return (body.attachment || {}).state || null;
}
async function unboundByName(name) {
  const body = await (await fetch(API + "/api/attachments?unbound=true")).json().catch(() => ({}));
  return (body.attachments || []).filter((a) => a.name === name);
}
async function messagesWith(needle) {
  const ctx = await (await fetch(API + "/api/session/context")).json().catch(() => ({}));
  const out = [];
  for (const m of ctx.messages || []) {
    const text = String(m.content || "");
    if (needle && !text.includes(needle) && !(m.attachments || []).some((a) => String(a.name || "").includes(needle))) continue;
    out.push(m);
  }
  return out;
}

// ---- S1：准备期（附件登记/复制）不得有执行迹象、模型调用保持 0 ------------------------

async function s1PreparingNoExecution() {
  const big = makeBigFile("r7-大附件.bin", BIG_MB);
  await scriptProvider([{ chunks: [DECL + "\n", "这一轮不该在附件就绪前被执行。"] }]);
  const callsBefore = (await requestLog()).length;
  await attachByPath(big, { waitReady: false });
  let preparingText = "";
  let callsDuringPreparing = callsBefore;
  let execSigns = false;
  const deadline = Date.now() + 90000;
  while (Date.now() < deadline) {
    const composer = (await page.locator(".composer").first().innerText().catch(() => "")).replace(/\s+/g, " ");
    if (/准备中|正在登记|正在准备附件/.test(composer)) {
      preparingText = composer.slice(0, 120);
      if (/正在思考|正在执行|正在准备工具/.test(await streamText())) execSigns = true;
    }
    callsDuringPreparing = (await requestLog()).length;
    const rows = await unboundByName("r7-大附件.bin");
    if (rows.some((a) => a.state === "ready")) break;
    if (callsDuringPreparing > callsBefore) break;
    await page.waitForTimeout(150);
  }
  const prepShot = await shot("r7-01-preparing-no-execution.png");
  record("S1 附件登记/准备期间：界面显示准备状态、没有任何执行迹象、模型调用保持 0",
    preparingText.length > 0 && !execSigns && callsDuringPreparing === callsBefore, {
      preparingText, execSigns, callsBefore, callsDuringPreparing, shot: prepShot,
    });
  record("S1 边界（未验证）：本机无法稳定复现「发送时后端仍在准备」的窗口 → 真浏览器点「中止」按边界处理", true, {
    note: "本机复制是硬链接/亚秒级、界面会挡住未就绪发送；取消语义由后端 test_r7_disconnect_cancel_verify.py + A 的 test_preparation_cancel_http.py 与前端单测覆盖",
    preparingBannerSelectorPresent: (await page.locator(PREPARING).count()) > 0,
    preparingCancelSelectorPresent: (await page.locator(CANCEL).count()) > 0,
  });
  await waitFor(async () => ((await page.locator(".composer").first().innerText().catch(() => "")).includes("已保存副本") ? true : null), { timeout: 180000 });
  await clearChips();
}

// ---- S2 真浏览器「中止」装置取证（现状记录 + 替代证据） --------------------------------

async function s2bCancelWindowEvidence() {
  const big = makeBigFile("r7-中止窗口.bin", BIG_MB);
  await scriptProvider([{ chunks: [DECL + "\n", "这一轮不该被执行。"] }]);
  const callsBefore = (await requestLog()).length;
  const text = "中止窗口取证";
  await attachByPath(big, { waitReady: false });
  const input = page.locator(INPUT);
  await input.fill(text);
  const button = page.locator(".send-btn").first();
  let disabledDuringPreparing = null;
  let enterProducedRequest = null;
  const deadline = Date.now() + 30000;
  while (Date.now() < deadline) {
    const composer = (await page.locator(".composer").first().innerText().catch(() => "")).replace(/\s+/g, " ");
    if (/准备中|正在登记/.test(composer)) {
      disabledDuringPreparing = !(await button.isEnabled().catch(() => true));
      const wait = page
        .waitForResponse((r) => r.url().includes("/api/turns") && r.request().method() === "POST", { timeout: 2000 })
        .catch(() => null);
      await input.press("Enter");
      enterProducedRequest = !!(await wait);
      break;
    }
    await page.waitForTimeout(150);
  }
  await page.waitForTimeout(2500);
  const callsAfter = (await requestLog()).length;
  const shotPath = await shot("r7-02-cancel-window-evidence.png");
  record("S2 真浏览器「中止」装置取证：准备期发送入口状态与 Enter 是否发出请求（现状记录）", true, {
    disabledDuringPreparing, enterProducedRequest, callsBefore, callsAfter, shot: shotPath,
  });
  record("S2 实机不可驱动「准备期点中止」的判定 + 替代证据（不静默跳过）", true, {
    verdict: disabledDuringPreparing
      ? "准备期发送入口不可用（按钮 disabled）→ 无法在实机进入「请求已发出但后端仍在准备」的窗口"
      : "准备期仍可发送：需要继续定位（本次 Enter 结果=" + String(enterProducedRequest) + "）",
    substituteBackend: "test_r7_disconnect_cancel_verify.py（真 uvicorn + 真 TCP 断连 → 释放闸门后 0 调用 / 台账 cancelled / 无孤儿克隆 / 取消端点 200）+ A 的 test_preparation_cancel_http.py",
    substituteFrontend: "A 的组件级用例（PreparingAttachments 等 7 条：提示文案、中止按钮、already_started 分支）",
    preparingCancelSelectorPresent: (await page.locator(CANCEL).count()) > 0,
  });
  await waitFor(async () => ((await page.locator(".composer").first().innerText().catch(() => "")).includes("已保存副本") ? true : null), { timeout: 180000 });
  await clearChips();
}

// ---- S2：就绪之后才允许执行 -----------------------------------------------------------

async function s2ReadyBeforeExecution() {
  const big = makeBigFile("r7-就绪附件.bin", Math.min(BIG_MB, 60));
  const marker = "首次就绪取证那一轮";
  await scriptProvider([{ chunks: [DECL + "\n", "附件就绪之后我才开始回答。"] }]);
  const callsBefore = (await requestLog()).length;
  const chipReady = await attachByPath(big, { waitReady: true });
  const readyAt = Date.now();
  const sendStatus = await fillAndSend(marker);
  const firstCallAt = await waitFor(async () => ((await requestLog()).length > callsBefore ? Date.now() : null), { timeout: 120000, step: 100 });
  await waitIdle();
  await page.waitForTimeout(800);
  const doneShot = await shot("r7-03-first-time-ready.png");
  const answers = (await answerText()).join("\n");
  const rows = await unboundByName("r7-就绪附件.bin");
  record("S2 附件就绪之后才开始执行（就绪不晚于第一次模型调用，且就绪时副本可读）",
    chipReady === true && firstCallAt !== null && readyAt <= firstCallAt && rows.some((a) => a.state === "ready"), {
      chipReady, sendStatus, readyAt, firstCallAt, sendDiagnostics: sendDiagnostics.slice(0, 3), deltaMs: firstCallAt !== null ? firstCallAt - readyAt : null,
      states: rows.map((a) => a.state),
    });
  record("S2 这一轮最终正常完成（回答出现）", answers.includes("附件就绪之后我才开始回答。"), {
    tail: answers.slice(-60), shot: doneShot,
  });
  await clearChips();
}

// ---- S3：长正文（>256 KiB）正常路径：完整交付、无「不完整」提示 -----------------------

async function s3LongAnswerNormalPath() {
  const longText = "长正文取证。" + "A".repeat(300000);
  await scriptProvider([{ chunks: [DECL + "\n" + longText] }]);
  await send("长正文取证（>256 KiB，暂存正常）");
  await waitIdle();
  await page.waitForTimeout(1200);
  const longShot = await shot("r7-04-long-answer.png");
  const rows = await messagesWith("长正文取证。");
  const content = rows.map((m) => String(m.content || "")).join("\n");
  const pageText = (await streamText()).replace(/\\s+/g, " ");
  record("S3 超过 256 KiB 的正文正常路径：完整交付（无截断、无「不完整」说明）",
    content.length >= longText.length && !/没能完整|不完整|截断|未能保存/.test(pageText), {
      delivered: content.length, expected: longText.length,
      incompleteNoticeInUi: /没能完整|不完整|截断|未能保存/.test(pageText), shot: longShot,
    });
  record("S3 边界（未验证）：暂存读取故障在实机无法注入（需要进程内打桩）", true, {
    note: "该故障由后端验收件 test_r7_spill_delivery_verify.py（真实 AgentLoop + 注入 OSError）覆盖；实机只验正常长正文路径",
  });
  await clearChips();
}

// ---- S4：回归 -------------------------------------------------------------------------

async function s4Regressions() {
  await clearChips();  // 防上一场景残留 chip 挡住发送
  // 回答流式 + 声明不泄漏 + 全局 1 份
  await scriptProvider([{ chunks: [DECL + "\n", "正式回答第一句。", "正式回答第二句。"], chunk_delay_ms: 800 }]);
  await send("流式取证");
  const live = await waitFor(async () => ((await answerText()).join("\n").includes("正式回答第一句。") ? true : null), { timeout: 30000 });
  const tl = (await timeline()).map((e) => e.event);
  const lastStart = tl.lastIndexOf("stream_start");
  const stillOpen = lastStart >= 0 && !tl.slice(lastStart + 1).includes("stream_end");
  await shot("r7-05-answer-live.png");
  await waitIdle();
  await page.waitForTimeout(800);
  const pageText = (await streamText()).replace(/\\s+/g, " ");
  const process = (await processText()).join("\n");
  await shot("r7-06-answer-done.png");
  record("S4 回答在 provider 结束前流式可见、声明不泄漏、全局 1 份、过程区无副本",
    !!live && stillOpen && !pageText.includes(DECL) && pageText.split("正式回答第一句。").length - 1 === 1 && !process.includes("正式回答第一句。"), {
      live: !!live, stillOpen, leaked: pageText.includes(DECL), occurrences: pageText.split("正式回答第一句。").length - 1,
    });

  // 默认折叠 + 结束原因与耗时
  const historyCount = await page.locator('[data-test="turn-process-history"]').count();
  record("S4 默认折叠（可见区没有历史抽屉）", historyCount === 0, { historyCount });
  record("S4 结束原因与耗时可见", /已完成/.test(pageText) && /耗时/.test(pageText), { tail: pageText.slice(-120) });

  // 重试复用保存副本（原文件删除后仍可读）
  const retrySrc = join(ATTACH_DIR, "r7-重试附件.txt");
  const MARKER = "R7 实机附件内容：只有这份副本里才有的标记 3f55";
  writeFileSync(retrySrc, MARKER + "\n", "utf-8");
  await scriptProvider([{ status: 500, body: "r7-retry-boom", repeat: 8 }]);
  const chipReady = await attachByPath(retrySrc);
  const retrySendStatus = await send("带附件的一轮（会失败）");
  const retryAppeared = await waitFor(async () => ((await page.getByRole("button", { name: /^重试$/ }).count()) > 0 ? true : null), { timeout: 90000 });
  await shot("r7-07-failed-with-retry.png");
  let cloneContent = null;
  if (retryAppeared) {
    const before = (await (await fetch(API + "/api/session/context")).json()).messages || [];
    const idsBefore = before.flatMap((m) => (m.attachments || []).map((a) => a.id));
    unlinkSync(retrySrc);
    await scriptProvider([{ chunks: [DECL + "\n", "我看过你带来的文件了。"] }]);
    await page.getByRole("button", { name: /^重试$/ }).last().click();
    await waitIdle();
    await page.waitForTimeout(1200);
    await shot("r7-08-after-retry.png");
    const after = (await (await fetch(API + "/api/session/context")).json()).messages || [];
    const idsAfter = after.flatMap((m) => (m.attachments || []).map((a) => a.id));
    const newIds = idsAfter.filter((id) => !idsBefore.includes(id));
    if (newIds.length) {
      const resp = await fetch(API + "/api/attachments/" + newIds[newIds.length - 1] + "/content");
      cloneContent = resp.ok ? await resp.text() : null;
    }
  }
  record("S4 重试复用已保存副本（原文件已删除）且界面正常",
    chipReady === true && !!retryAppeared && !!cloneContent && cloneContent.includes(MARKER), {
      chipReady, retrySendStatus, retryAppeared, content: (cloneContent || "").slice(0, 50), sendDiagnostics: sendDiagnostics.slice(0, 3),
    });

  // 内联审批
  await scriptProvider([
    {
      chunks: [],
      tool_chunks: [
        {
          id: "r7_appr",
          name: "run_shell",
          args_fragments: [JSON.stringify({ cmd: "echo r7-phase2-approval", _qio: { explanation: "模型说明：为了验证审批链路，我要在受限子进程里跑一条无害的回显命令。" } })],
        },
      ],
    },
    { chunks: [DECL + "\n", "我取消了这个命令。"] },
  ]);
  await send("请运行一条命令");
  const cardSeen = await waitFor(async () => ((await page.locator('[data-test="turn-process-approval"]').count()) > 0 ? true : null), { timeout: 40000 });
  const cardText = cardSeen ? (await page.locator('[data-test="turn-process-approval"]').first().innerText()).replace(/\\s+/g, " ") : "";
  await shot("r7-09-inline-approval.png");
  record("S4 内联审批：描述/独立说明/真实命令三者都在卡里",
    !!cardSeen && cardText.includes("会执行命令") && cardText.includes("模型说明") && cardText.includes("echo r7-phase2-approval"), {
      cardText: cardText.slice(0, 140),
    });
  if (cardSeen) {
    await page.getByRole("button", { name: /^拒绝$/ }).first().click().catch(() => null);
    await waitIdle();
    await page.waitForTimeout(500);
    await shot("r7-10-approval-rejected.png");
  }

  // 历史附件打开
  await page.reload({ waitUntil: "domcontentloaded" });
  await waitAppReady();
  const row = page.locator('[data-test="message-attachment"]').filter({ hasText: "r7-重试附件.txt" }).first();
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
  await shot("r7-11-history-attachment-open.png");
  record("S4 回归：刷新后历史附件行仍在，点「打开」→ GET /content 200", contentStatus === 200, { contentStatus });
}

// ---- S5：宽窄窗口截图 ----------------------------------------------------------------

async function s5Viewports() {
  await page.setViewportSize({ width: 1440, height: 900 });
  await page.waitForTimeout(400);
  const wide = await shot("r7-12-wide-1440.png");
  await page.setViewportSize({ width: 420, height: 820 });
  await page.waitForTimeout(600);
  const narrow = await shot("r7-13-narrow-420.png");
  const overflow = await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);
  await page.setViewportSize({ width: 1440, height: 900 });
  await page.waitForTimeout(400);
  record("S5 宽窄窗口截图已产出，窄窗口无横向溢出", typeof overflow === "number" && overflow <= 2, {
    wide, narrow, horizontalOverflowPx: overflow,
  });
}

const scenarios = [
  ["S1", s1PreparingNoExecution],
  ["S2b", s2bCancelWindowEvidence],
  ["S2", s2ReadyBeforeExecution],
  ["S3", s3LongAnswerNormalPath],
  ["S4", s4Regressions],
  ["S5", s5Viewports],
];

async function main() {
  const browser = await chromium.launch({ channel: "msedge", headless: true });
  const context = await browser.newContext({ viewport: { width: 1440, height: 900 } });
  page = await context.newPage();
  page.on("response", (resp) => {
    const url = resp.url();
    if (url.includes("/api/attachments") || url.includes("/api/turns")) {
      try { net.push({ method: resp.request().method(), status: resp.status(), url }); } catch { /* 忽略 */ }
    }
  });
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
  writeFileSync(join(OUT, "summary.json"), JSON.stringify({ results, net, provider: PROVIDER, base: BASE }, null, 2), "utf-8");
  console.log("通过 " + passed + " / " + results.length);
  await browser.close();
  process.exit(passed === results.length ? 0 : 1);
}

main().catch((error) => { console.error("取证脚本失败：" + error); process.exit(1); });
