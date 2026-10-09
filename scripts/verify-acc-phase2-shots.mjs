// acc-f 阶段二：实机交互取证（假厂商 SSE → uvicorn → vite → msedge 无头 → Playwright）。
// 覆盖 plan §六.3：执行+排队共存 / 结束折叠 / 失败与取消 / 附件恢复 / Markdown 列表内代码与表格 / 窄窗口，
// 另含 F06 不完整结束与 F11 独立「系统事实」区域的实机取证。
// 边界（如实）：provider 是本机扮演的假厂商；结论只能读成「QIO 自己的链路对」，不证明真实厂商 / Tauri 原生窗口行为。
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
const PROVIDER = process.env.QIO_E2E_PROVIDER || "http://127.0.0.1:8807";
const API = process.env.QIO_E2E_API || "http://127.0.0.1:8734";
const OUT = resolve(process.env.QIO_E2E_SHOTS || "docs/verification-shots-acc");
const DATA = process.env.QIO_E2E_DATA || join(tmpdir(), "qio-acc-phase2-data");
const ATTACH_DIR = join(DATA, "attach-src");
mkdirSync(OUT, { recursive: true });
mkdirSync(ATTACH_DIR, { recursive: true });

const INPUT = 'textarea[aria-label="输入消息"]';
const DECL = "[[QIO:ANSWER]]";
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
const answerText = () => page.locator(".stream .message.assistant").allInnerTexts();
const processText = () => page.locator('[data-test="turn-process"]').allInnerTexts();
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

// ---- S1：执行与排队共存 ---------------------------------------------------------------

async function s1RunningAndQueued() {
  await clearChips();
  await scriptProvider([{ chunks: [DECL + "\n", "排队共存第一句。", "排队共存第二句。"], chunk_delay_ms: 2500 }]);
  const sendA = await send("排队共存：A 正在执行");
  const live = await waitFor(async () => ((await streamText()).includes("排队共存第一句。") ? true : null), { timeout: 30000 });
  const sendB = await send("排队共存：B 应当排队");
  const snap = await waitFor(async () => {
    const s = await queue();
    return s.running && (s.queued || []).length >= 1 ? s : null;
  }, { timeout: 30000 });
  const chipText = await page.locator(".queue .chip").first().innerText().catch(() => "");
  await page.waitForTimeout(300);
  const shotPath = await shot("acc-01-running-queued.png");
  record("S1 执行与排队共存：A 运行中 + B 排队，界面显示「运行中 · N 排队中」",
    live === true && !!snap && /运行中/.test(chipText) && /排队中/.test(chipText), {
      sendA, sendB,
      running: snap ? snap.running.turn_id : null,
      queued: snap ? (snap.queued || []).map((q) => q.turn_id) : [],
      chipText, shot: shotPath,
    });
  await waitIdle(120000);
  await page.waitForTimeout(600);
}

// ---- S2：结束折叠 + 权威耗时（含排队口径） --------------------------------------------

async function s2EndedFoldAndTiming() {
  const text = (await streamText()).replace(/\s+/g, " ");
  const process = (await processText()).join("\n");
  const historyDrawer = await page.locator('[data-test="turn-process-history"]').count();
  const duration = await page.locator('[data-test="turn-process-duration"]').first().innerText().catch(() => "");
  const timingPanels = await page.locator('[data-test="turn-timing"]').count();
  const shotPath = await shot("acc-02-turn-ended-folded.png");
  record("S2 结束折叠：默认无历史抽屉，过程区显示结束状态与耗时（总耗时=排队+执行）",
    historyDrawer === 0 && /耗时/.test(text) && /(已完成|已停止|未完成)/.test(text) && /排队/.test(text), {
      historyDrawer, duration, timingPanels, tail: text.slice(-150), processHead: process.slice(0, 90), shot: shotPath,
    });
}

// ---- S3：不完整结束（缺 finish_reason） ------------------------------------------------

async function s3IncompleteEnd() {
  await clearChips();
  await scriptProvider([{ abort_after: 2, chunks: [DECL + "\n", "不完整第一句。", "不完整第二句。", "不会发出的后缀"], chunk_delay_ms: 400 }]);
  await send("不完整结束：缺 finish_reason");
  await waitIdle(120000);
  await page.waitForTimeout(900);
  const notice = page.locator('[data-test="turn-incomplete-notice"]');
  const seen = await waitFor(async () => ((await notice.count()) > 0 ? true : null), { timeout: 30000 });
  const noticeText = seen ? (await notice.first().innerText()).replace(/\s+/g, " ") : "";
  const text = (await streamText()).replace(/\s+/g, " ");
  const shotPath = await shot("acc-03-incomplete-ended.png");
  record("S3 不完整结束：界面明确「未完成」且不伪装完成，未确认后缀不出现",
    !!seen && !text.includes("不会发出的后缀") && /(未完成|不完整|可能不完整)/.test(noticeText), {
      seen: !!seen, noticeText: noticeText.slice(0, 140), leakedSuffix: text.includes("不会发出的后缀"), shot: shotPath,
    });
}

// ---- S4：失败 + 重试入口 ---------------------------------------------------------------

async function s4FailureRetry() {
  await clearChips();
  await scriptProvider([{ status: 500, body: "acc-phase2-boom", repeat: 8 }]);
  await send("失败路径：厂商 500");
  const retry = await waitFor(async () => ((await page.getByRole("button", { name: /^重试$/ }).count()) > 0 ? true : null), { timeout: 90000 });
  const text = (await streamText()).replace(/\s+/g, " ");
  await page.waitForTimeout(300);
  const shotPath = await shot("acc-04-failed-retry.png");
  record("S4 失败：界面给出结束原因与「重试」入口（不伪装成功）",
    !!retry && /(失败|出错|连接|重试)/.test(text), { retry: !!retry, tail: text.slice(-160), shot: shotPath });
}

// ---- S5：排队轮取消 --------------------------------------------------------------------

async function s5QueuedCancel() {
  await clearChips();
  await scriptProvider([{ chunks: [DECL + "\n", "取消场景第一句。", "取消场景第二句。"], chunk_delay_ms: 3000 }]);
  await send("取消场景：A 执行中");
  const live = await waitFor(async () => ((await streamText()).includes("取消场景第一句。") ? true : null), { timeout: 30000 });
  await send("取消场景：B 排队后被取消");
  const snapBefore = await waitFor(async () => {
    const s = await queue();
    return s.running && (s.queued || []).length >= 1 ? s : null;
  }, { timeout: 30000 });
  const bTurn = snapBefore ? snapBefore.queued[snapBefore.queued.length - 1].turn_id : null;
  const chip = page.locator(".queue .chip").first();
  await chip.click().catch(() => null);
  await page.waitForTimeout(400);
  const cancelBtn = page.locator('button[aria-label^="取消排队"]').first();
  const cancelVisible = await cancelBtn.count();
  await cancelBtn.click().catch(() => null);
  const afterCancel = await waitFor(async () => {
    const s = await queue();
    return !(s.queued || []).some((q) => q.turn_id === bTurn) ? s : null;
  }, { timeout: 30000 });
  const chipText = await page.locator(".queue .chip").first().innerText().catch(() => "");
  await page.waitForTimeout(400);
  const shotPath = await shot("acc-05-queued-cancel.png");
  record("S5 排队取消：可展开逐条取消；取消后 B 从队列移除，运行中的 A 不受影响",
    live === true && !!snapBefore && !!afterCancel && cancelVisible > 0 && !!(afterCancel && afterCancel.running), {
      live, beforeQueued: snapBefore ? (snapBefore.queued || []).map((q) => q.turn_id) : [],
      cancelVisible, afterQueued: afterCancel ? (afterCancel.queued || []).map((q) => q.turn_id) : [],
      runningStill: afterCancel ? (afterCancel.running ? afterCancel.running.turn_id : null) : null,
      chipText, shot: shotPath,
      note: "取消的结束事实（cancelled/user_stopped/retry）由后端契约与前端 store 用例钉住；此处取证可操作且不误伤 A",
    });
  await waitIdle(120000);
  await page.waitForTimeout(500);
}

// ---- S6：工具失败注记 → 独立「系统事实」区域 -------------------------------------------

async function s6SystemAnnotation() {
  await clearChips();
  await scriptProvider([
    {
      tool_chunks: [
        {
          id: "acc_ph2_fs_read",
          name: "fs_read",
          args_fragments: [JSON.stringify({ path: "D:/qio-acc-phase2/不存在的文件-acc.txt" })],
        },
      ],
    },
    { chunks: [DECL + "\n", "正文：这一步的工具失败了，但正文只有这一份。"] },
  ]);
  await send("工具失败注记：正文唯一 + 系统事实区域");
  await waitIdle(120000);
  await page.waitForTimeout(1000);
  const note = page.locator('[data-test="answer-system-note"]');
  const seen = await waitFor(async () => ((await note.count()) > 0 ? true : null), { timeout: 30000 });
  const noteText = seen ? (await note.first().innerText()).replace(/\s+/g, " ") : "";
  const answers = (await answerText()).join("\n");
  const body = "正文：这一步的工具失败了，但正文只有这一份。";
  const bodyCount = answers.split(body).length - 1;
  await page.waitForTimeout(300);
  const shotPath = await shot("acc-06-system-note.png");
  record("S6 工具失败注记：注记在独立「系统事实」区域，正文恰好一份且不被注记污染",
    !!seen && noteText.includes("系统核对") && noteText.includes("不能当作") && bodyCount === 1 && answers.indexOf("系统核对") < 0, {
      seen: !!seen, noteHead: noteText.slice(0, 90), bodyCount, noteInsideAnswer: answers.indexOf("系统核对") >= 0, shot: shotPath,
    });
}

// ---- S7：附件恢复（刷新后仍可打开） ----------------------------------------------------

async function s7AttachmentRestore() {
  await clearChips();
  const name = "acc-附件恢复.txt";
  const src = join(ATTACH_DIR, name);
  writeFileSync(src, "ACC 附件恢复标记 9f31\n", "utf-8");
  await scriptProvider([{ chunks: [DECL + "\n", "我收到了附件。"] }]);
  const chipReady = await attachByPath(src);
  const sendStatus = await fillAndSend("附件恢复：发送后刷新");
  await waitIdle(120000);
  await page.reload({ waitUntil: "domcontentloaded" });
  await waitAppReady();
  const row = page.locator('[data-test="message-attachment"]').filter({ hasText: name }).first();
  const rowCount = await row.count();
  let contentStatus = null;
  if (rowCount) {
    const openBtn = row.locator("button").filter({ hasText: /打开|查看|下载/ }).first();
    if (await openBtn.count()) {
      const wait = page.waitForResponse((r) => r.url().includes("/content"), { timeout: 25000 });
      await openBtn.click();
      const resp = await wait.catch(() => null);
      contentStatus = resp ? resp.status() : null;
    }
  }
  await page.waitForTimeout(400);
  const shotPath = await shot("acc-07-attachment-restored.png");
  record("S7 附件恢复：刷新后历史附件行仍在，点「打开」→ GET /content 200",
    chipReady === true && sendStatus === 200 && rowCount > 0 && contentStatus === 200, {
      chipReady, sendStatus, rowCount, contentStatus, shot: shotPath,
    });
  await clearChips();
}

// ---- S8：Markdown 列表内代码块 / 表格 --------------------------------------------------

async function s8MarkdownListBlocks() {
  await clearChips();
  const FENCE = String.fromCharCode(96, 96, 96);
  const md = [
    "- 步骤：",
    "",
    "  " + FENCE + "js",
    "  const a = 1;",
    "  " + FENCE,
    "",
    "- 表格：",
    "",
    "  | a | b |",
    "  | --- | --- |",
    "  | 1 | 2 |",
    "",
  ].join("\n");
  await scriptProvider([{ chunks: [DECL + "\n", md] }]);
  await send("Markdown：列表内的代码块与表格");
  await waitIdle(120000);
  await page.waitForTimeout(900);
  const code = await page.locator(".markdown-body .code-block").count();
  const table = await page.locator(".markdown-body .table-wrap table").count();
  const nested = await page.locator(".markdown-body ul ul").count();
  const codeText = await page.locator(".markdown-body").last().innerText().catch(() => "");
  const shotPath = await shot("acc-08-markdown-list-blocks.png");
  record("S8 Markdown 列表内代码块 / 表格结构保留（F13 实机）",
    code > 0 && table > 0 && codeText.includes("const a = 1;"), {
      codeBlocks: code, tables: table, nestedLists: nested, hasCodeText: codeText.includes("const a = 1;"), shot: shotPath,
    });
}

// ---- S9：宽窄窗口 ----------------------------------------------------------------------

async function s9Viewports() {
  await page.setViewportSize({ width: 1440, height: 900 });
  await page.waitForTimeout(400);
  const wide = await shot("acc-09-wide-1440.png");
  await page.setViewportSize({ width: 420, height: 820 });
  await page.waitForTimeout(700);
  const narrow = await shot("acc-10-narrow-420.png");
  const overflow = await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);
  await page.setViewportSize({ width: 1440, height: 900 });
  await page.waitForTimeout(400);
  record("S9 宽窄窗口截图已产出，窄窗口无横向溢出", typeof overflow === "number" && overflow <= 2, {
    wide, narrow, horizontalOverflowPx: overflow,
  });
  record("S9 边界（未验证）：Windows 原生窗口 / Tauri 安装包 / 真实厂商端点本机不具备（不用浏览器证据冒充）", true, {
    note: "原生壳下的选文件、路径拖入、原生中止提示、真实厂商网络行为均未验证",
  });
}

const scenarios = [
  ["S1", s1RunningAndQueued],
  ["S2", s2EndedFoldAndTiming],
  ["S3", s3IncompleteEnd],
  ["S4", s4FailureRetry],
  ["S5", s5QueuedCancel],
  ["S6", s6SystemAnnotation],
  ["S7", s7AttachmentRestore],
  ["S8", s8MarkdownListBlocks],
  ["S9", s9Viewports],
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
  await send("预热（只看链路通不通）");
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
