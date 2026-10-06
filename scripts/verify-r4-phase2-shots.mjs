// D 阶段二（R4）：实机交互取证（假厂商 SSE → uvicorn → vite → msedge 无头 → Playwright）。
//
// S1 正式回答流式 + 端到端时间线：回答区在 provider 结束前就有字（截图 + 首个正文显示时刻早于 stream_end）
// S2 断流前后 DOM 对照：回答在 .message.assistant，[data-test=turn-process] 内不含
// S3 带附件重试：界面点「重试」→ 新轮附件是克隆（新 id）、原文件删除后仍读出同一内容
// S4 上传写盘失败：界面上明确失败与原因，不是无限转圈
// S5 回归：运行中默认折叠；历史附件「打开」→ GET /content 200
//
// 结论只能读成「QIO 自己的链路对」：provider 是本机扮演的假厂商，不证明任何真实厂商行为。
// 环境变量：QIO_E2E_BASE / QIO_E2E_PROVIDER / QIO_E2E_API / QIO_E2E_SHOTS / QIO_E2E_DATA

import { createRequire } from "node:module";
import { mkdirSync, writeFileSync, unlinkSync, existsSync, rmSync } from "node:fs";
import { resolve, join } from "node:path";
import { tmpdir } from "node:os";

const require = createRequire(import.meta.url);
const PW =
  process.env.QIO_PLAYWRIGHT ||
  "C:\\\\Users\\\\zxy\\\\.cache\\\\codex-runtimes\\\\codex-primary-runtime\\\\dependencies\\\\node\\\\node_modules\\\\playwright";
const { chromium } = require(PW);

const BASE = process.env.QIO_E2E_BASE || "http://127.0.0.1:5199";
const PROVIDER = process.env.QIO_E2E_PROVIDER || "http://127.0.0.1:8798";
const API = process.env.QIO_E2E_API || "http://127.0.0.1:8734";
const OUT = resolve(process.env.QIO_E2E_SHOTS || "docs/verification-shots-r4-phase2");
const DATA = process.env.QIO_E2E_DATA || join(tmpdir(), "qio-r4-phase2-data");
const ATTACH_DIR = join(DATA, "attach-src");
mkdirSync(OUT, { recursive: true });
mkdirSync(ATTACH_DIR, { recursive: true });

const INPUT = 'textarea[aria-label="输入消息"]';
const MARKER = "R4-阶段二附件内容：只有这份副本里才有的标记 7f3a";
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
  try {
    const resp = await fetch(PROVIDER + "/__timeline");
    return (await resp.json()).timeline || [];
  } catch {
    return [];
  }
}

async function scriptProvider(steps, fallback) {
  await fetch(PROVIDER + "/__reset", { method: "POST", headers: { "content-type": "application/json" }, body: "{}" });
  await fetch(PROVIDER + "/__script", {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({ steps, ...(fallback ? { default: fallback } : {}) }),
  });
}

async function waitFor(fn, { timeout = 20000, step = 150 } = {}) {
  const deadline = Date.now() + timeout;
  while (Date.now() < deadline) {
    const value = await fn();
    if (value) return value;
    await page.waitForTimeout(step);
  }
  return null;
}

function answerText() {
  return page.locator(".stream .message.assistant").allInnerTexts();
}

function processText() {
  return page.locator('[data-test="turn-process"]').allInnerTexts();
}

async function send(text) {
  const input = page.locator(INPUT);
  await input.waitFor({ state: "visible", timeout: 20000 });
  await input.fill(text);
  const button = page.locator(".send-btn");
  await button.waitFor({ state: "visible", timeout: 10000 });
  for (let i = 0; i < 120; i++) {
    if (await button.first().isEnabled()) break;
    await page.waitForTimeout(100);
  }
  await button.first().click();
  await page.evaluate(() => document.activeElement && document.activeElement.blur());
}

async function queue() {
  try {
    return await (await fetch(API + "/api/turns/queue")).json();
  } catch {
    return { running: null, queued: [] };
  }
}

async function waitIdle(timeout = 90000) {
  const deadline = Date.now() + timeout;
  while (Date.now() < deadline) {
    const snap = await queue();
    if (!snap.running && (snap.queued || []).length === 0) return true;
    await page.waitForTimeout(300);
  }
  return false;
}

async function sessionMessages() {
  try {
    const body = await (await fetch(API + "/api/session/context")).json();
    return body.messages || [];
  } catch {
    return [];
  }
}

/** 刷新/重进之后等应用真的渲染出来（实测会先白屏几秒）。 */
async function waitAppReady(timeout = 40000) {
  const deadline = Date.now() + timeout;
  while (Date.now() < deadline) {
    const stream = await page.locator(".stream").count().catch(() => 0);
    const input = await page.locator(INPUT).count().catch(() => 0);
    if (stream > 0 && input > 0) {
      await page.waitForTimeout(600);
      return true;
    }
    await page.waitForTimeout(300);
  }
  return false;
}

// ---- S1：正式回答流式 + 端到端时间线 ------------------------------------------------

async function s1AnswerStreamingTimeline() {
  // 只比较**发送之后新增**的 stream_end（预热轮也会留下自己的 stream_end —— 实测踩过）
  await scriptProvider([
    { chunks: [] },
    { chunks: ["正式回答第一句。", "正式回答第二句。", "正式回答第三句。"], chunk_delay_ms: 700 },
  ]);

  await send("直接回答我（时间线取证）");
  const observed = await waitFor(async () => {
    const texts = await answerText();
    const joined = texts.join("\n");
    return joined.includes("正式回答第一句。") ? Date.now() : null;
  }, { timeout: 30000 });
  const midShot = await shot("r4-01-answer-live-before-provider-end.png");
  const tlAtObservation = await timeline();
  const answers = (await answerText()).join("\n");
  const process = (await processText()).join("\n");
  // 只看**最后一次 stream_start 之后**有没有 stream_end：工作调用自己的 stream_end 不算
  // （一轮里先有一次空的工作调用，它有始有终 —— 用总数比较会误判，实测踩过）
  const eventsAtObservation = (tlAtObservation || []).map((e) => e.event);
  const lastStart = eventsAtObservation.lastIndexOf("stream_start");
  const ended = eventsAtObservation.slice(lastStart + 1).includes("stream_end");

  record("S1 正式回答在 provider 结束前出现在正式回答容器里（.message.assistant）", !!observed && answers.includes("正式回答第一句。"), {
    observed: !!observed,
    answers: answers.slice(0, 160),
    streamEndSeenAtObservation: !ended,
    shot: midShot,
  });
  record("S1 命中时刻过程区不含这段正式回答", !!observed && !process.includes("正式回答第一句。"), {
    process: process.slice(0, 160),
  });

  // 等回答调用（最后一次 stream_start）之后真的出现 stream_end，量出「首字显示 → provider 结束」的毫秒差
  let endedAtMs = null;
  const deadline = Date.now() + 40000;
  while (Date.now() < deadline) {
    const now = (await timeline()).map((e) => e.event);
    const start = now.lastIndexOf("stream_start");
    if (start >= 0 && now.slice(start + 1).includes("stream_end")) {
      endedAtMs = Date.now();
      break;
    }
    await page.waitForTimeout(150);
  }
  await waitIdle();
  const tl = await timeline();
  const chunkSents = (tl || []).filter((e) => e.event === "chunk_sent").length;
  const afterShot = await shot("r4-02-answer-complete.png");
  record("S1 端到端时间线：首个正文显示时刻早于 provider 结束", !!observed && !!endedAtMs && !ended, {
    firstTextVisibleAtMs: observed,
    providerStreamEndAtMs: endedAtMs,
    leadMs: endedAtMs && observed ? endedAtMs - observed : null,
    answerCallStillOpenAtObservation: !ended,
    chunksSent: chunkSents,
    timelineEvents: (tl || []).map((e) => e.event),
    shot: afterShot,
  });
  const finalAnswers = (await answerText()).join("\n");
  const finalProcess = (await processText()).join("\n");
  record("S1 完成后：正式回答只在回答容器里（过程区没有副本，全局一份）",
    finalAnswers.includes("正式回答第三句。") && !finalProcess.includes("正式回答第三句。"), {
      duplicateCount: finalAnswers.split("正式回答第三句。").length - 1,
      inProcess: finalProcess.includes("正式回答第三句。"),
    });
}

// ---- S2：断流前后 DOM 对照 -----------------------------------------------------------

async function s2AbortDomContrast() {
  const partial = "断流之前已经显示的一句。";
  await scriptProvider([
    { chunks: [] },
    { chunks: [partial, "这一句不会到达。"], chunk_delay_ms: 900, abort_after: 1 },
  ]);
  await send("断流取证");
  const seen = await waitFor(async () => {
    const joined = (await answerText()).join("\n");
    return joined.includes(partial) ? true : null;
  }, { timeout: 30000 });
  const midShot = await shot("r4-03-answer-before-abort.png");
  const midAnswers = (await answerText()).join("\n");
  const midProcess = (await processText()).join("\n");
  record("S2 断流前：已显示的正式回答在 .message.assistant 里、过程区不含", !!seen && midAnswers.includes(partial) && !midProcess.includes(partial), {
    answers: midAnswers.slice(0, 140),
    process: midProcess.slice(0, 140),
    shot: midShot,
  });

  await waitIdle();
  const after = await waitFor(async () => {
    const joined = (await answerText()).join("\n");
    return joined.includes(partial) ? true : null;
  }, { timeout: 20000 });
  const afterShot = await shot("r4-04-answer-after-abort.png");
  const afterAnswers = (await answerText()).join("\n");
  const afterProcess = (await processText()).join("\n");
  record("S2 断流：未到达的那一句一个字都没出现（真的断在中间）", !afterAnswers.includes("这一句不会到达。") && !afterProcess.includes("这一句不会到达。"), {
    answers: afterAnswers.slice(0, 120),
  });
  record("S2 断流后：文字仍在回答容器里、过程区仍不含它、全局只有一份", !!after && !afterProcess.includes(partial), {
    duplicateCount: afterAnswers.split(partial).length - 1,
    inProcess: afterProcess.includes(partial),
    answers: afterAnswers.slice(0, 140),
    shot: afterShot,
  });
}

// ---- S3：带附件重试（真实界面「重试」入口） -------------------------------------------

async function s3RetryWithAttachment() {
  const src = join(ATTACH_DIR, "r4-重试附件.txt");
  writeFileSync(src, MARKER + "\\n", "utf-8");

  await scriptProvider([{ status: 500, body: "r4-retry-first-turn-boom", repeat: 6 }]);
  await page.locator('button[aria-label="粘贴本地文件路径"]').click();
  const pathInput = page.locator('input[aria-label="本地文件路径"]');
  await pathInput.waitFor({ state: "visible", timeout: 10000 });
  await pathInput.fill(src);
  await pathInput.press("Enter");
  const chipReady = await waitFor(async () => {
    const text = await page.locator(".composer").first().innerText().catch(() => "");
    return /已保存副本/.test(text) && !/还在准备中/.test(text) ? true : null;
  }, { timeout: 40000 });
  await send("带附件的一轮（第一轮会失败）");
  await waitIdle();
  await page.waitForTimeout(1200);

  const retryBtn = page.getByRole("button", { name: /^重试$/ }).last();
  const hasRetry = (await retryBtn.count()) > 0;
  const failedShot = await shot("r4-05-failed-turn-with-retry.png");
  record("S3 第一轮失败后界面出现真实「重试」入口", chipReady === true && hasRetry, { chipReady, hasRetry, shot: failedShot });
  if (!hasRetry) return;

  const before = await sessionMessages();
  const beforeIds = before.flatMap((m) => (m.attachments || []).map((a) => a.id));
  const originalId = beforeIds[beforeIds.length - 1] || null;

  // 原文件删除：重试必须靠已保存的副本
  unlinkSync(src);
  await scriptProvider([{ chunks: ["我看过你带来的文件了。"] }]);
  await retryBtn.click();
  const started = await waitFor(async () => {
    const snap = await queue();
    return snap.running || (snap.queued || []).length ? true : null;
  }, { timeout: 25000 });
  await waitIdle();
  await page.waitForTimeout(1500);
  const retryShot = await shot("r4-06-after-retry.png");

  const after = await sessionMessages();
  const afterIds = after.flatMap((m) => (m.attachments || []).map((a) => a.id));
  const newIds = afterIds.filter((id) => id !== originalId);
  let content = null;
  let state = null;
  if (newIds.length) {
    const meta = await (await fetch(API + "/api/attachments/" + newIds[newIds.length - 1])).json();
    state = (meta.attachment || {}).state;
    const resp = await fetch(API + "/api/attachments/" + newIds[newIds.length - 1] + "/content");
    content = resp.ok ? await resp.text() : null;
  }
  record("S3 点「重试」真的开了新轮，且新轮附件是**克隆**（新 id、原文件已删除）",
    !!started && newIds.length > 0 && newIds[newIds.length - 1] !== originalId, {
      originalId, newIds, state, shot: retryShot,
    });
  record("S3 克隆副本仍能读出原附件内容（原文件已删除）", !!content && content.includes(MARKER), {
    content: (content || "").slice(0, 80), state,
  });
  return { cloneId: newIds[newIds.length - 1] || null };
}

// ---- S4：上传写盘失败（界面反馈） ----------------------------------------------------

async function s4UploadFailureFeedback() {
  // 复制路径是 <data>/attachments/<年>/<月>/<id>__<name>：把**月目录**临时换成同名文件，
  // mkdir 必失败（S3 已经把该目录建出来了，所以要先挪走再放文件；收尾还原，别影响 S5）。
  const now = new Date();
  const monthDir = join(DATA, "attachments", String(now.getFullYear()), String(now.getMonth() + 1).padStart(2, "0"));
  const aside = monthDir + ".r4-aside";
  let swapped = false;
  try {
    mkdirSync(join(DATA, "attachments"), { recursive: true });
    if (existsSync(monthDir)) {
      rmSync(aside, { recursive: true, force: true });
      require("node:fs").renameSync(monthDir, aside);
      swapped = true;
    }
    writeFileSync(monthDir, "受控错误：这里本该是目录（写盘失败取证）", "utf-8");
  } catch (error) {
    record("S4 装置：制造写盘失败", false, String(error).slice(0, 200));
    return;
  }

  const src = join(ATTACH_DIR, "r4-写盘失败.txt");
  writeFileSync(src, "这份上传必须失败。\n", "utf-8");
  let outcome = null;
  try {
    await page.locator('button[aria-label="粘贴本地文件路径"]').click();
    const pathInput = page.locator('input[aria-label="本地文件路径"]');
    await pathInput.waitFor({ state: "visible", timeout: 10000 });
    await pathInput.fill(src);
    await pathInput.press("Enter");
    outcome = await waitFor(async () => {
      const text = await page.locator(".composer").first().innerText().catch(() => "");
      if (!/r4-写盘失败/.test(text)) return null;
      const noLongerPreparing = !/还在准备中/.test(text) && !/准备中/.test(text);
      const saysFailed = /失败|出错|打不开|不能|写不|不可用|没有成功|不在原位|丢失/.test(text);
      return noLongerPreparing && saysFailed ? text : null;
    }, { timeout: 45000 });
  } finally {
    rmSync(monthDir, { force: true });
    if (swapped) require("node:fs").renameSync(aside, monthDir);
  }
  const shotPath = await shot("r4-07-upload-failed.png");
  const composerText = await page.locator(".composer").first().innerText().catch(() => "");
  record("S4 写盘失败：界面在有限时间内给出明确失败与原因（不是无限转圈）", !!outcome, {
    composerText: composerText.replace(/\s+/g, " ").slice(0, 220),
    shot: shotPath,
  });
  const rows = await (await fetch(API + "/api/attachments?unbound=true")).json().catch(() => ({ attachments: [] }));
  const stuck = (rows.attachments || []).filter((a) => a.name === "r4-写盘失败.txt");
  record("S4 写盘失败：没有停在「准备中」的残留记录", stuck.every((a) => a.state !== "prepared"), {
    rows: stuck.map((a) => [a.name, a.state]),
  });
  // 收尾：把失败 chip 从输入区移除，否则它会挡住后面场景的发送（发送闸门会如实拒绝 —— 实测踩过）
  await page.evaluate(() => {
    Array.from(document.querySelectorAll(".composer button"))
      .filter((b) => (b.textContent || "").trim() === "×")
      .forEach((b) => b.click());
  });
  await page.waitForTimeout(400);
}

// ---- S5：回归（默认折叠 + 历史附件打开） ---------------------------------------------

async function s5Regressions() {
  await scriptProvider([
    {
      chunks: ["先读一下文件。"],
      chunk_delay_ms: 120,
      tool_chunks: [{ id: "r4_g1", name: "echo", args_fragments: ['{"text": "r4"}'] }],
    },
    { chunks: [] },
    { chunks: ["回归取证：这一轮结束了。"] },
  ]);
  await send("回归取证（默认折叠）");
  const runningSeen = await waitFor(async () => {
    const running = await page.locator('[data-test="turn-process"]').count();
    return running > 0 ? true : null;
  }, { timeout: 25000 });
  const historyCount = await page.locator('[data-test="turn-process-history"]').count();
  const collapseShot = await shot("r4-08-running-default-collapsed.png");
  record("S5 运行中默认折叠：默认可见区没有历史抽屉", !!runningSeen && historyCount === 0, {
    historyCount, shot: collapseShot,
  });

  await waitIdle();
  await page.waitForTimeout(1200);
  await page.reload({ waitUntil: "domcontentloaded" });
  await waitAppReady();
  const row = page.locator('[data-test="message-attachment"]').first();
  const rowCount = await row.count();
  let contentStatus = null;
  if (rowCount) {
    const openBtn = row.locator("button").filter({ hasText: /打开|查看|下载/ }).first();
    if ((await openBtn.count()) > 0) {
      const wait = page.waitForResponse((r) => r.url().includes("/content"), { timeout: 25000 });
      await openBtn.click();
      const resp = await wait.catch(() => null);
      contentStatus = resp ? resp.status() : null;
    }
  }
  const histShot = await shot("r4-09-history-attachment-open.png");
  record("S5 回归：刷新后历史附件行仍在，点「打开」→ GET /content 200", rowCount > 0 && contentStatus === 200, {
    rowCount, contentStatus, shot: histShot,
  });
}

// ---- main ---------------------------------------------------------------------------

// ---- S6：刷新后重试（失败那一轮的入口在历史恢复后还在不在） ---------------------------

async function s6RetryAfterRefresh() {
  // 稳定前置：①带附件的一轮以模拟 provider 错误失败 → ②确认「重试」入口可见 → ③再刷新
  const src = join(ATTACH_DIR, "r4-刷新后重试.txt");
  writeFileSync(src, MARKER + "\n", "utf-8");

  // 这一条要独立于前面几个场景：先重载一次，再清掉可能残留的待发附件 chip
  // （S4 的失败 chip 会挡住发送闸门 → 这一轮根本没发出去，看起来就像「没有重试入口」——实测踩过）
  await page.reload({ waitUntil: "domcontentloaded" });
  await waitAppReady();
  // 点产品自己的 chip 「×」按钮（选择器按文案找，避免依赖内部 class）
  const clearChips = async () => {
    for (let i = 0; i < 6; i++) {
      const clicked = await page.evaluate(() => {
        const buttons = Array.from(document.querySelectorAll(".composer button"));
        const removeBtn = buttons.find((b) => (b.textContent || "").trim() === "×");
        if (removeBtn) {
          removeBtn.click();
          return true;
        }
        return false;
      });
      if (!clicked) break;
      await page.waitForTimeout(300);
    }
  };
  await clearChips();
  const chipsLeft = (await page.locator(".composer").first().innerText()).match(/\d+(\.\d+)?\s?(B|KB|MB)/g)?.length ?? 0;

  await scriptProvider([{ status: 500, body: "r4-refresh-retry-boom", repeat: 8 }]);
  await page.locator('button[aria-label="粘贴本地文件路径"]').click();
  const pathInput = page.locator('input[aria-label="本地文件路径"]');
  await pathInput.waitFor({ state: "visible", timeout: 10000 });
  await pathInput.fill(src);
  await pathInput.press("Enter");
  await waitFor(async () => {
    const text = await page.locator(".composer").first().innerText().catch(() => "");
    return /已保存副本/.test(text) && !/还在准备中/.test(text) ? true : null;
  }, { timeout: 40000 });
  await send("刷新后重试取证（第一轮会失败）");

  // 等真实入口出现（失败轮的「重试」按钮）——不能用 waitIdle 当信号（后端重试期间队列会短暂为空）
  const failedBefore = await waitFor(
    async () => ((await page.getByRole("button", { name: /^重试$/ }).count()) > 0 ? true : null),
    { timeout: 90000 },
  );
  const processBefore = await page.locator('[data-test="turn-process"]').count();
  await shot("r4-10-failed-before-refresh.png");
  record("S6 前置：这一轮以 provider 错误失败并出现真实「重试」入口", !!failedBefore && processBefore > 0, {
    retryBeforeRefresh: !!failedBefore,
    processRegionsBeforeRefresh: processBefore,
    leftoverChipsBeforeSend: chipsLeft,
    composerText: (await page.locator(".composer").first().innerText().catch(() => "")).replace(/\s+/g, " ").slice(0, 160),
  });
  if (!failedBefore) return; // 前置不成立就停在这里，不把装置问题当作产品结论

  const before = await sessionMessages();
  const idsBefore = before.flatMap((m) => (m.attachments || []).map((a) => a.id));
  const originalId = idsBefore[idsBefore.length - 1] || null;

  await page.reload({ waitUntil: "domcontentloaded" });
  await waitAppReady();
  const processAfter = await page.locator('[data-test="turn-process"]').count();
  const retryAfter = (await page.getByRole("button", { name: /^重试$/ }).count()) > 0;
  const afterShot = await shot("r4-11-after-refresh-retry-entry.png");
  record("S6 刷新（历史恢复）之后：结束原因/过程区仍在，且「重试」入口仍在", retryAfter && processAfter > 0, {
    retryBeforeRefresh: true,
    processRegionsBeforeRefresh: processBefore,
    processRegionsAfterRefresh: processAfter,
    retryAfterRefresh: retryAfter,
    netTail: net.filter((x) => x.url.includes("/api/turns")).slice(-3),
    shot: afterShot,
  });
  if (!retryAfter) return; // 刷新后真的不在 → 上面的红就是产品缺陷证据，交 Lead 转 B

  // 点它：新轮必须带上原附件（新 id 克隆），且原文件删除后仍读出原内容
  unlinkSync(src);
  await scriptProvider([{ chunks: ["刷新之后我照样能看到你带来的文件。"] }]);
  const retryBtn = page.getByRole("button", { name: /^重试$/ }).last();
  await retryBtn.click();
  const started = await waitFor(async () => {
    const snap = await queue();
    return snap.running || (snap.queued || []).length ? true : null;
  }, { timeout: 30000 });
  await waitIdle();
  await page.waitForTimeout(1500);
  const after = await sessionMessages();
  const idsAfter = after.flatMap((m) => (m.attachments || []).map((a) => a.id));
  const newIds = idsAfter.filter((id) => id !== originalId);
  let state = null;
  let content = null;
  if (newIds.length) {
    const meta = await (await fetch(API + "/api/attachments/" + newIds[newIds.length - 1])).json();
    state = (meta.attachment || {}).state;
    const resp = await fetch(API + "/api/attachments/" + newIds[newIds.length - 1] + "/content");
    content = resp.ok ? await resp.text() : null;
  }
  const retriedShot = await shot("r4-12-retry-after-refresh-result.png");
  record("S6 刷新后点「重试」：新轮附件是克隆（新 id、原文件已删除）且内容读得回来",
    !!started && newIds.length > 0 && !!content && content.includes(MARKER), {
      originalId, newIds, state, content: (content || "").slice(0, 80), shot: retriedShot,
    });
}

const scenarios = [
  ["S1", s1AnswerStreamingTimeline],
  ["S2", s2AbortDomContrast],
  ["S3", s3RetryWithAttachment],
  ["S4", s4UploadFailureFeedback],
  ["S5", s5Regressions],
  ["S6", s6RetryAfterRefresh],
];

async function main() {
  const browser = await chromium.launch({ channel: "msedge", headless: true });
  const context = await browser.newContext({ viewport: { width: 1440, height: 900 } });
  page = await context.newPage();
  page.on("response", async (resp) => {
    const url = resp.url();
    if (url.includes("/api/attachments") || url.includes("/api/turns") || url.includes("/api/approvals")) {
      try {
        net.push({ method: resp.request().method(), status: resp.status(), url });
      } catch {
        /* 忽略 */
      }
    }
  });

  await page.goto(BASE, { waitUntil: "domcontentloaded", timeout: 40000 });
  await waitAppReady();
  // 首次引导会以 aria-modal 对话框拦截所有点击（实测 .send-btn 被 intercepts pointer events）：
  // 用产品自己的接口标记已看过，再重载一次拿干净页面。
  await fetch(API + "/api/onboarding/seen", { method: "POST" }).catch(() => null);
  await page.reload({ waitUntil: "domcontentloaded" });
  await waitAppReady();
  const onboardingLeft = await page.locator(".onboarding").count();
  record("环境：首次引导已关闭（否则点击会被对话框拦截）", onboardingLeft === 0, { onboardingLeft });
  // 预热：首次使用凭据会多一次非流式调用（吃掉 FIFO 第一步）
  await scriptProvider([{ chunks: ["预热完成。"] }]);
  await send("预热（只看链路通不通）");
  await waitIdle(60000);
  record("环境：应用可用（预热一轮完成）", true, {});

  const only = String(process.env.QIO_E2E_ONLY || "").trim();
  for (const [name, fn] of scenarios) {
    if (only && !name.startsWith(only)) continue;
    try {
      await fn();
    } catch (error) {
      record(name + " 场景未跑完", false, String(error).slice(0, 300));
    }
  }

  const passed = results.filter((r) => r.ok).length;
  writeFileSync(join(OUT, "summary.json"), JSON.stringify({ results, net, provider: PROVIDER, base: BASE }, null, 2), "utf-8");
  console.log("通过 " + passed + " / " + results.length);
  await browser.close();
  process.exit(passed === results.length ? 0 : 1);
}

main().catch((error) => {
  console.error("取证脚本失败：" + error);
  process.exit(1);
});
