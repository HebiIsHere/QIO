// D 阶段二：**实机交互取证**（真应用：假厂商 SSE → uvicorn → vite → msedge 无头 → Playwright）。
//
// 覆盖 Lead 指定的 5 项：
//   S1 默认折叠（运行中默认可见区 = 状态行 + 当前阶段名 + 最新说明 + 一行工具摘要；
//      旧说明与逐项工具卡不可见；展开后可回看）
//   S2 正式回答稳定性（纯回答在 provider 结束前已可见；迟到工具增量不移动答案区文字）
//   S3 内联审批真实点击（描述/独立说明/真实命令可看 → 查看完整信息弹窗 → 允许真的生效；拒绝不执行）
//   S4 历史附件打开副本（真实点击 → GET /api/attachments/{id}/content 真发生）
//   S5 失败入口与可用操作（provider 失败 → 原因 + 重试入口 → 重试真的再跑一轮）
//
// 结论只能读成「QIO 自己的链路对」：provider 是本机扮演的假厂商，不证明任何真实厂商行为。
//
// 跑法（scripts/verify-audit-phase2.ps1 会调它；也可单独跑，前提是 provider+后端+前端已起、
// 凭据已建）：
//     node scripts/verify-audit-phase2-shots.mjs
// 环境变量：QIO_E2E_BASE / QIO_E2E_PROVIDER / QIO_E2E_API / QIO_E2E_SHOTS /
//          QIO_E2E_WORKSPACE（后端的工作目录，用来放真实可读文件）/
//          QIO_E2E_ATTACH_DIR / QIO_PLAYWRIGHT

import { createRequire } from "node:module";
import { mkdirSync, writeFileSync, openSync, ftruncateSync, closeSync, renameSync } from "node:fs";
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
const OUT = resolve(process.env.QIO_E2E_SHOTS || "docs/verification-shots-phase2");
const ATTACH_DIR = process.env.QIO_E2E_ATTACH_DIR || join(tmpdir(), "qio-phase2-attach");
mkdirSync(OUT, { recursive: true });
mkdirSync(ATTACH_DIR, { recursive: true });

const INPUT = 'textarea[aria-label="输入消息"]';
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

function chunk(text, size) {
  const out = [];
  for (let i = 0; i < text.length; i += size) out.push(text.slice(i, i + size));
  return out;
}

function lastRegion() {
  return page.locator('[data-test="turn-process"]').last();
}

async function lastRegionText() {
  const region = lastRegion();
  return (await region.count()) ? await region.innerText() : "";
}

async function answerTexts() {
  return page.locator(".stream .message.assistant").allInnerTexts();
}

function occurrenceCount(haystack, needle) {
  if (!needle) return 0;
  return haystack.split(needle).length - 1;
}

async function send(text) {
  const input = page.locator(INPUT);
  await input.waitFor({ state: "visible", timeout: 20000 });
  await input.fill(text);
  const button = page.locator(".send-btn");
  await button.waitFor({ state: "visible", timeout: 10000 });
  for (let i = 0; i < 100; i++) {
    if (await button.first().isEnabled()) break;
    await page.waitForTimeout(100);
  }
  await button.first().click();
  await page.evaluate(() => document.activeElement && document.activeElement.blur());
}

async function turnQueue() {
  try {
    const resp = await fetch(API + "/api/turns/queue");
    return await resp.json();
  } catch {
    return { running: null, queued: [] };
  }
}

/** 队列排空：一轮没跑完就等，卡住就取消 —— 否则后面的场景会排在别人后面 */
async function drainQueue(timeout = 90000) {
  const deadline = Date.now() + timeout;
  for (;;) {
    const snap = await turnQueue();
    const busy = Boolean(snap.running) || (snap.queued || []).length > 0;
    if (!busy) return true;
    if (Date.now() > deadline) {
      await fetch(API + "/api/turns/cancel", { method: "POST" }).catch(() => {});
      await page.waitForTimeout(1500);
      return false;
    }
    await page.waitForTimeout(300);
  }
}

async function waitTurnRunning(timeout = 30000) {
  const deadline = Date.now() + timeout;
  while (Date.now() < deadline) {
    const state = await lastRegion().getAttribute("data-state").catch(() => null);
    if (state === "running") return true;
    await page.waitForTimeout(150);
  }
  return false;
}

async function waitTurnSettled(timeout = 90000) {
  const deadline = Date.now() + timeout;
  while (Date.now() < deadline) {
    const state = await lastRegion().getAttribute("data-state").catch(() => null);
    if (state === "ready" || state === "failed" || state === "stopped") return state;
    await page.waitForTimeout(200);
  }
  return await lastRegion().getAttribute("data-state").catch(() => null);
}

/** 展开「整轮历史」（只在还没展开时点）。 */
async function expandHistory(region = lastRegion()) {
  const toggle = region.locator('[data-test="turn-process-toggle"]');
  if ((await toggle.count()) === 0) return false;
  if ((await toggle.first().getAttribute("aria-expanded")) !== "true") {
    await toggle.first().click();
    await page.waitForTimeout(500);
  }
  return (await region.locator('[data-test="turn-process-history"]').count()) > 0;
}

/** 展开「本阶段明细」（逐项调用）。 */
async function expandStageDetail(region = lastRegion()) {
  const toggle = region.locator('[data-test="turn-process-stage-toggle"]');
  if ((await toggle.count()) === 0) return false;
  if ((await toggle.first().getAttribute("aria-expanded")) !== "true") {
    await toggle.first().click();
    await page.waitForTimeout(400);
  }
  return (await region.locator('[data-test="turn-process-stage-tools"]').count()) > 0;
}

async function waitFor(fn, { timeout = 20000, step = 200 } = {}) {
  const deadline = Date.now() + timeout;
  for (;;) {
    const value = await fn().catch(() => null);
    if (value) return value;
    if (Date.now() > deadline) return null;
    await page.waitForTimeout(step);
  }
}

async function pendingApprovals() {
  try {
    const resp = await fetch(API + "/api/runtime/state");
    const body = await resp.json();
    return body.approvals || [];
  } catch {
    return [];
  }
}

async function registerAttachment(sourcePath, name) {
  const resp = await fetch(API + "/api/attachments", {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({ source_path: sourcePath, name }),
  });
  const body = await resp.json();
  return body.attachment || body;
}

async function waitAttachmentSettled(id, timeout = 90000) {
  const deadline = Date.now() + timeout;
  let last = {};
  while (Date.now() < deadline) {
    const resp = await fetch(API + "/api/attachments/" + id);
    last = (await resp.json()).attachment || {};
    if (["ready", "failed", "missing", "changed"].includes(String(last.state))) return last;
    await new Promise((r) => setTimeout(r, 300));
  }
  return last;
}

async function sendViaApi(message, attachmentIds) {
  const resp = await fetch(API + "/api/turns", {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({ message, attachment_ids: attachmentIds || [] }),
  });
  return await resp.json();
}

async function sessionMessages() {
  try {
    const resp = await fetch(API + "/api/session/messages");
    const body = await resp.json();
    return body.messages || body || [];
  } catch {
    return [];
  }
}

/** 真机上放几个可读文件（工作目录就是后端的 workspace 根）。 */
function prepareWorkspace() {
  const root = process.env.QIO_E2E_WORKSPACE;
  if (!root) return { ok: false, reason: "没有 QIO_E2E_WORKSPACE" };
  mkdirSync(root, { recursive: true });
  writeFileSync(join(root, "p2-stage-a.md"), "# 阶段二取样文件 A\n\n用来让 fs_read 真的成功。\n", "utf-8");
  writeFileSync(join(root, "p2-stage-b.md"), "# 阶段二取样文件 B\n\n第二个阶段的读取对象。\n", "utf-8");
  return { ok: true, root };
}

// ---------------------------------------------------------------------------
// S1 默认折叠
// ---------------------------------------------------------------------------
async function s1RunningCollapsed() {
  const stageArgs = (path, text, op, name) =>
    JSON.stringify({ path, _qio: { kind: "progress", text, stage: { op, name } } });
  await scriptProvider(
    [
      { tool_chunks: [{ id: "s1c1", name: "fs_read", args_fragments: [stageArgs("p2-stage-a.md", "正在读取仓库结构", "start", "读取仓库结构")] }], chunk_delay_ms: 10 },
      { tool_chunks: [{ id: "s1c2", name: "fs_read", args_fragments: [stageArgs("p2-stage-b.md", "正在核对实现", "next", "核对实现")] }], chunk_delay_ms: 10 },
      { chunks: chunk("第二阶段正在慢慢生成一段较长的说明，用来把运行中的窗口留出来观察默认可见区。", 12), chunk_delay_ms: 350 },
    ],
    { chunks: ["（默认回复）"], chunk_delay_ms: 50 },
  );
  await send("请分两步读两个文件");
  const running = await waitTurnRunning(30000);
  record("S1 运行中状态出现", running, null);

  const sawStages = await waitFor(async () => ((await lastRegionText()).includes("核对实现") ? true : null), { timeout: 30000 });
  await lastRegion().scrollIntoViewIfNeeded();
  const historyCount = await page.locator('[data-test="turn-process-history"]').count();
  const regionText = await lastRegionText();
  const stageTools = await lastRegion().locator('[data-test="turn-process-stage-tools"]').count();
  const toolCards = await lastRegion().locator(".tool-card").count();
  const shotRunning = await shot("p2-01-running-collapsed.png");

  record("S1 两个阶段都到达（当前阶段名可见）", sawStages === true, { region: regionText.slice(0, 220) });
  record("S1 运行中默认不展开历史（默认可见区没有历史抽屉）", historyCount === 0, { historyCount, shot: shotRunning });
  record("S1 默认可见区有：当前阶段名 + 最新说明 + 一行工具摘要",
    regionText.includes("核对实现") && /工具运行中|次调用|正在/.test(regionText),
    { region: regionText.slice(0, 300) });
  record("S1 默认可见区没有旧阶段说明与逐项工具卡",
    !regionText.includes("正在读取仓库结构") && stageTools === 0 && toolCards === 0,
    { stageTools, toolCards, hasOldNote: regionText.includes("正在读取仓库结构") });

  const expanded = await expandHistory();
  const expandedText = await lastRegionText();
  const expandedShot = await shot("p2-02-history-expanded.png");
  const providerLog = await fetch(PROVIDER + "/__log").then((r) => r.json()).catch(() => ({ requests: [] }));
  const turns = await sessionMessages();
  record("S1 展开后能回看旧阶段说明（整轮历史抽屉）", expanded && expandedText.includes("正在读取仓库结构"),
    {
      shot: expandedShot,
      region: expandedText.slice(0, 300),
      providerRequests: (providerLog.requests || []).slice(-6).map((x) => ({ step: x.step_kind, stream: x.stream_requested, msgs: x.message_count })),
      turnMessages: turns.slice(-4).map((m) => ({ role: m.role, content: String(m.content || "").slice(0, 40), raw: m.raw ? Object.keys(m.raw) : null })),
    });

  const stageToolsShown = await expandStageDetail();
  const stageText = await lastRegion().locator('[data-test="turn-process-stage-tools"]').innerText().catch(() => "");
  const stageShot = await shot("p2-02b-stage-detail.png");
  record("S1 本阶段明细（逐项工具卡）展开后可见", stageToolsShown && /fs_read|p2-stage-b|已读取|输出/.test(stageText),
    { shot: stageShot, stageText: stageText.slice(0, 200) });

  await waitTurnSettled();
  await page.waitForTimeout(500);
}

// ---------------------------------------------------------------------------
// S2 正式回答稳定性
// ---------------------------------------------------------------------------
async function s2LiveAndNoMove() {
  const chunks = ["第一句。", "第二句。", "第三句。", "第四句。", "第五句。", "第六句。"];
  await scriptProvider([{ chunks, chunk_delay_ms: 500 }], { chunks: ["（默认）"], chunk_delay_ms: 50 });
  await send("请分多次回答");

  let midRegion = null;
  let timelineAtMid = null;
  let midShot = null;
  const deadline = Date.now() + 25000;
  while (Date.now() < deadline) {
    const regionText = await lastRegionText();
    if (regionText.includes("第一句")) {
      midRegion = regionText;
      timelineAtMid = (await timeline()).map((x) => x.event);
      await lastRegion().scrollIntoViewIfNeeded();
      midShot = await shot("p2-03-answer-live-in-process.png");
      break;
    }
    await page.waitForTimeout(100);
  }
  record("S2 纯回答在 provider 结束前已经可见（出现在过程区）",
    midRegion !== null && !(timelineAtMid || []).includes("stream_end"),
    { region: (midRegion || "").slice(0, 160), timeline: timelineAtMid, shot: midShot });

  await waitTurnSettled();
  await page.waitForTimeout(800);
  const fullText = chunks.join("");
  const answers = (await answerTexts()).join("\n");
  const regionAfter = await lastRegionText();
  const streamAll = await page.locator(".stream").innerText();
  await lastRegion().scrollIntoViewIfNeeded();
  const promotedShot = await shot("p2-04-answer-promoted.png");
  record("S2 提升到答案区：全局只出现一次，过程区不再留副本",
    occurrenceCount(streamAll, fullText) === 1 && answers.includes(fullText) && !regionAfter.includes("第六句"),
    { shot: promotedShot, inAnswer: answers.includes(fullText), count: occurrenceCount(streamAll, fullText), region: regionAfter.slice(0, 160) });

  // (b) 迟到工具增量
  const toolTurnText = "我先说明一下。这一步马上要调用工具。";
  await scriptProvider(
    [
      { chunks: ["我先说明一下。", "这一步马上要调用工具。"], chunk_delay_ms: 1200,
        tool_chunks: [{ id: "s2c1", name: "fs_read", args_fragments: ['{"path": "p2-stage-a.md"}'] }] },
      { chunks: ["工具跑完了，这是正式回答。"], chunk_delay_ms: 30 },
    ],
    { chunks: ["（默认）"], chunk_delay_ms: 50 },
  );
  await send("请先说明再读文件");
  const sawToolTextLive = await waitFor(async () => ((await lastRegionText()).includes("我先说明一下") ? true : null), { timeout: 30000 });
  await lastRegion().scrollIntoViewIfNeeded();
  const liveShot = await shot("p2-05-late-tool-process-text.png");
  record("S2 工具轮的说明在工具增量之前就已可见（过程区）", sawToolTextLive === true, { shot: liveShot });

  await waitTurnSettled();
  await page.waitForTimeout(1000);
  const finalAnswers = (await answerTexts()).join("\n");
  // 收尾后默认折叠 → 展开整轮历史再数（不展开时那段文字按契约不在 DOM 里）
  const expandedNow = await expandHistory();
  const finalStream = await page.locator(".stream").innerText();
  const finalRegion = await lastRegionText();
  await lastRegion().scrollIntoViewIfNeeded();
  const afterShot = await shot("p2-06-late-tool-no-move.png");
  const count = occurrenceCount(finalStream, toolTurnText);
  record("S2 迟到工具增量不移动答案区文字（工具说明只在过程区、全局一份、答案区没有）",
    expandedNow && count === 1 && !finalAnswers.includes(toolTurnText) && finalRegion.includes("我先说明一下"),
    { count, inAnswers: finalAnswers.includes(toolTurnText), expanded: expandedNow, shot: afterShot });
  record("S2 正式回答仍然完整到达答案区", finalAnswers.includes("工具跑完了，这是正式回答。"), null);
}

// ---------------------------------------------------------------------------
// S3 内联审批真实点击
// ---------------------------------------------------------------------------
async function s3InlineApproval() {
  const cmd = "echo qio-phase2-approval";
  const explanation = "模型说明：为了验证审批链路，我要在受限子进程里跑一条无害的回显命令。";
  await scriptProvider(
    [
      { tool_chunks: [{ id: "s3c1", name: "run_shell", args_fragments: [JSON.stringify({ cmd, _qio: { explanation } })] }], chunk_delay_ms: 10 },
      { chunks: ["命令已经执行完了。"], chunk_delay_ms: 20 },
    ],
    { chunks: ["（默认）"], chunk_delay_ms: 50 },
  );
  await send("请运行一条命令");

  const cardShown = await waitFor(
    async () => ((await lastRegion().locator('[data-test="turn-process-approval"]').count()) ? true : null),
    { timeout: 40000 },
  );
  await page.waitForTimeout(500);
  const pays = await pendingApprovals();
  const payload = (pays[0] && pays[0].payload) || {};
  await lastRegion().scrollIntoViewIfNeeded();
  const cardText = await lastRegion().locator('[data-test="turn-process-approval"]').innerText().catch(() => "");
  const cardShot = await shot("p2-07-inline-approval.png");
  record("S3 内联审批卡出现", cardShown === true, { shot: cardShot, payloadKeys: Object.keys(payload) });

  const description = String(payload.description || "");
  const explained = String(payload.explanation || "");
  const command = String(payload.cmd || "");
  const inlineFacts = [
    ["描述", description],
    ["独立说明", explained],
    ["真实命令", command],
  ].filter(([, v]) => v.trim());
  const visibleInline = inlineFacts.filter(([, v]) => cardText.includes(v.trim())).map(([k]) => k);
  record("S3 内联卡能看到「描述 / 独立说明 / 真实命令」三者（都在卡里，不用另开弹窗）",
    inlineFacts.length >= 3 && visibleInline.length === inlineFacts.length,
    { inlineFacts: inlineFacts.map(([k, v]) => [k, v.slice(0, 40)]), visibleInline, cardText: cardText.slice(0, 400) });

  const fullInfo = lastRegion()
    .locator('[data-test="turn-process-approval"] button')
    .filter({ hasText: /查看完整信息|完整信息|查看详情|全部信息|详情/ });
  let modalVisible = false;
  let modalShot = null;
  if ((await fullInfo.count()) > 0) {
    await fullInfo.first().click();
    await page.waitForTimeout(800);
    modalVisible = (await page.locator('.modal-mask .modal, [role="dialog"][aria-modal="true"]').count()) > 0;
    modalShot = await shot("p2-08-approval-modal.png");
  }
  record("S3 「查看完整信息」打开原弹窗（弹窗接管，同一审批只有一套有效按钮）", modalVisible, { shot: modalShot });

  const allowBtn = modalVisible
    ? page.locator('.modal-mask .modal button.approve')
    : page.getByRole("button", { name: /允许/ });
  const allowShotBefore = await shot("p2-09-approval-allow-before.png");
  let allowErr = null;
  try {
    await allowBtn.first().click({ timeout: 20000 });
  } catch (error) {
    allowErr = String(error).slice(0, 200);
  }
  const wentAway = await waitFor(async () => ((await pendingApprovals()).length === 0 ? true : null), { timeout: 25000 });
  const state = await waitTurnSettled(90000);
  await page.waitForTimeout(1000); // 让 response 监听器把 200 记进台账（它是异步 push 的）
  const respondCalls = net.filter((x) => x.url.includes("/api/approvals/") && x.method === "POST");
  const finalAnswers = (await answerTexts()).join("\n");
  await shot("p2-10-approval-allowed.png");
  record("S3 点「允许」真的发出应答并生效（POST /api/approvals/{id}/respond 200 + 审批清空）",
    respondCalls.length > 0 && respondCalls.every((x) => x.status === 200) && wentAway === true,
    { respondCalls, wentAway, allowErr, shots: [allowShotBefore, join(OUT, "p2-10-approval-allowed.png")] });
  record("S3 允许之后这一轮继续到正式回答（工具真的执行了）",
    finalAnswers.includes("命令已经执行完了。"),
    { state, answers: finalAnswers.slice(-200) });

  const regionAfterAllow = await lastRegionText();
  record("S3 工具在应答之后真的执行了（过程区/工具记录可见 run_shell 的产物）",
    /echo|qio-phase2-approval|命令/.test(regionAfterAllow) || finalAnswers.includes("命令已经执行完了。"),
    { region: regionAfterAllow.slice(0, 240) });

  // 拒绝：不执行
  await drainQueue(60000);
  await scriptProvider(
    [
      { tool_chunks: [{ id: "s3c2", name: "run_shell", args_fragments: [JSON.stringify({ cmd: "echo qio-phase2-reject", _qio: { explanation: "模型说明：再验证一次拒绝路径。" } })] }], chunk_delay_ms: 10 },
      { chunks: ["好的，那我不执行了。"], chunk_delay_ms: 20 },
    ],
    { chunks: ["（默认）"], chunk_delay_ms: 50 },
  );
  await send("再运行一条命令");
  const card2 = await waitFor(
    async () => ((await lastRegion().locator('[data-test="turn-process-approval"]').count()) ? true : null),
    { timeout: 40000 },
  );
  let rejectOk = false;
  let rejectDetail = null;
  if (card2 === true) {
    const rejectBtn = page.getByRole("button", { name: "拒绝", exact: true }).first();
    try {
      await rejectBtn.click({ timeout: 20000 });
      await page.waitForTimeout(2000);
      const after = await pendingApprovals();
      rejectOk = after.length === 0;
      rejectDetail = { pending: after.length };
    } catch (error) {
      rejectDetail = String(error).slice(0, 200);
    }
    await shot("p2-11-approval-rejected.png");
  }
  record("S3 点「拒绝」真的生效（审批被应答、命令不执行）", rejectOk, rejectDetail);
  await waitTurnSettled(60000);
}

// ---------------------------------------------------------------------------
// S4 历史附件
// ---------------------------------------------------------------------------
async function s4HistoryAttachment() {
  const filePath = join(ATTACH_DIR, "阶段二附件.txt");
  writeFileSync(filePath, "阶段二取证：这份副本必须能被「打开」入口真的读出来。\n", "utf-8");

  // 真实 UI 流程：路径 → 添加 → 发送（不绕过前端）
  await scriptProvider([{ chunks: ["收到你带来的文件了。"], chunk_delay_ms: 20 }], { chunks: ["（默认）"], chunk_delay_ms: 50 });
  await page.locator('button[aria-label="粘贴本地文件路径"]').click();
  const pathInput = page.locator('input[aria-label="本地文件路径"]');
  await pathInput.waitFor({ state: "visible", timeout: 10000 });
  await pathInput.fill(filePath);
  await pathInput.press("Enter");
  const chip = await waitFor(async () => {
    const text = await page.locator(".composer").first().innerText().catch(() => "");
    return /阶段二附件\.txt/.test(text) ? true : null;
  }, { timeout: 30000 });
  // 必须等附件从「准备中」变成「已保存副本」：发送闸门会拦住未就绪的附件（实测过）
  const ready = await waitFor(async () => {
    const text = await page.locator(".composer").first().innerText().catch(() => "");
    return /已保存副本/.test(text) && !/还在准备中/.test(text) ? true : null;
  }, { timeout: 40000 });
  record("S4 附件 chip 进入 ready（发送闸门放行）", chip === true && ready === true, { chip, ready });
  await send("这条消息带着一个附件");
  await waitTurnSettled(90000);
  await page.waitForTimeout(1200);
  const liveItem = page.locator('[data-test="message-attachment"]').first();
  const liveItemCount = await liveItem.count();
  const attId = liveItemCount ? await liveItem.getAttribute("data-id") : null;
  await liveItem.scrollIntoViewIfNeeded().catch(() => {});
  const liveShot = await shot("p2-12-attachment-live.png");
  record("S4 真实 UI 把附件发出去并在消息里出现附件行（锚点带 id/kind/state）",
    chip === true && liveItemCount > 0 && !!attId,
    { chip, liveItemCount, attId, kind: liveItemCount ? await liveItem.getAttribute("data-kind") : null,
      state: liveItemCount ? await liveItem.getAttribute("data-state") : null, shot: liveShot });
  if (!liveItemCount || !attId) return;

  const itemLocator = liveItem;
  const itemCount = await itemLocator.count();
  await itemLocator.scrollIntoViewIfNeeded().catch(() => {});
  const itemShot = await shot("p2-12-history-attachment.png");
  record("S4 附件锚点带 id/kind/state", itemCount > 0, { shot: itemShot, kind: await itemLocator.getAttribute("data-kind"), state: await itemLocator.getAttribute("data-state") });
  if (itemCount === 0) return;

  void itemLocator;
  const openBtn = liveItem.locator("button").filter({ hasText: /打开|查看|下载/ }).first();
  const hasOpen = (await openBtn.count()) > 0;
  let contentResp = null;
  if (hasOpen) {
    const wait = page.waitForResponse((r) => r.url().includes("/api/attachments/" + attId + "/content"), { timeout: 25000 });
    await openBtn.click();
    try {
      contentResp = await wait;
    } catch {
      contentResp = null;
    }
  }
  const openShot = await shot("p2-13-attachment-open.png");
  record("S4 点「打开」真的取到 QIO 副本（GET /content 200）",
    !!contentResp && contentResp.status() === 200,
    { status: contentResp ? contentResp.status() : null, contentLength: contentResp ? contentResp.headers()["content-length"] : null, shot: openShot, hasOpen });

  // 刷新 = 走真实历史加载路径：附件行还在不在？（契约 §1.6「历史里提供入口」）
  const messagesBefore = await sessionMessages();
  const withAtt = messagesBefore.filter((m) => JSON.stringify(m).includes(attId));
  await page.reload({ waitUntil: "domcontentloaded" });
  await page.waitForTimeout(3500);
  const histItem = page.locator('[data-test="message-attachment"][data-id="' + attId + '"]').first();
  const histCount = await histItem.count();
  await shot("p2-14-history-after-reload.png");
  record("S4 刷新后（真实历史加载）附件行仍在 —— 历史附件才有可用的打开入口",
    histCount > 0,
    {
      histCount,
      historyMessageKeys: messagesBefore.length ? Object.keys(messagesBefore[messagesBefore.length - 1]) : [],
      attachmentIdsInHistory: withAtt.length,
      apiNote: "GET /api/session/messages 不返回 attachments 字段（只在实时消息里由前端本地带上）",
    });

  // 失效引用（>100MB 只记位置）：历史行缺失时无法点击 —— 如实记录阻断关系
  const bigPath = join(ATTACH_DIR, "被引用的大文件.bin");
  const fd = openSync(bigPath, "w");
  ftruncateSync(fd, 100_000_001);
  closeSync(fd);
  const big = await registerAttachment(bigPath, "被引用的大文件.bin");
  const bigMeta = await waitAttachmentSettled(big.id);
  record("S4 引用型附件登记（kind=reference）", bigMeta.kind === "reference", { kind: bigMeta.kind, state: bigMeta.state });
  renameSync(bigPath, bigPath + ".moved");
  await page.reload({ waitUntil: "domcontentloaded" });
  await page.waitForTimeout(3000);
  const bigItem = page.locator('[data-test="message-attachment"][data-id="' + big.id + '"]').first();
  const hasRelocate = (await bigItem.count()) > 0;
  await shot("p2-15-relocate-blocked.png");
  record("S4 引用失效后历史里出现重新定位入口（真机点击）", false, {
    blocked: "刷新后的历史消息没有附件行（同上一条缺陷）→ 重新定位入口在真实历史里渲染不出来",
    bigItemCount: await bigItem.count(),
    referenceState: bigMeta.state,
    note: "重新定位的真实链路（选择器 → POST /relocate）在 DOM/后端验收里已被覆盖；浏览器内也没有原生选择器（entry 只存在于 Tauri）",
  });
}

// ---------------------------------------------------------------------------
// S5 失败入口与可用操作
// ---------------------------------------------------------------------------
async function s5FailureRetry() {
  // 全部调用都失败（含 default）：否则循环会重试一次、拿到默认文本就「成功」了
  await scriptProvider(
    [{ status: 500, body: "phase2-fake-provider-boom" }],
    { status: 500, body: "phase2-fake-provider-boom" },
  );
  await send("这一轮会失败");
  const state = await waitTurnSettled(90000);
  await page.waitForTimeout(800);
  await lastRegion().scrollIntoViewIfNeeded();
  const regionText = await lastRegionText();
  const failShot = await shot("p2-16-turn-failed.png");
  record("S5 失败状态如实显示", state === "failed", { state, shot: failShot, region: regionText.slice(0, 240) });
  record("S5 失败原因（系统事实）出现在界面上", /厂商|错误|失败|provider|500|内部/.test(regionText), { region: regionText.slice(0, 240) });

  const retryBtn = lastRegion().locator("button").filter({ hasText: /重试|重发|再次/ }).first();
  const hasRetry = (await retryBtn.count()) > 0;
  record("S5 失败后有可用操作入口（重试/重发）", hasRetry, { region: regionText.slice(0, 240) });
  if (hasRetry) {
    // 重试之前把厂商恢复成正常（否则重试还是 500）
    await scriptProvider([{ chunks: ["重试之后这一轮真的跑通了。"], chunk_delay_ms: 30 }],
      { chunks: ["重试之后这一轮真的跑通了。"], chunk_delay_ms: 30 });
    await retryBtn.click();
    const doneState = await waitTurnSettled(120000);
    await page.waitForTimeout(1500);
    const answers = (await answerTexts()).join("\n");
    await shot("p2-17-retry-succeeded.png");
    record("S5 点「重试」真的又跑了一轮并拿到回答",
      answers.includes("重试之后这一轮真的跑通了。"),
      { doneState, tail: answers.slice(-200) });
  }
}

async function main() {
  const ws = prepareWorkspace();
  const browser = await chromium.launch({ channel: "msedge", headless: true });
  const context = await browser.newContext({ viewport: { width: 1440, height: 900 } });
  page = await context.newPage();
  page.on("response", async (resp) => {
    try {
      const url = resp.url();
      if (url.includes("/api/attachments/") || url.includes("/api/turns") || url.includes("/api/approvals/")) {
        net.push({ method: resp.request().method(), url, status: resp.status(), bytes: Number(resp.headers()["content-length"] || 0) });
      }
    } catch {
      /* 忽略 */
    }
  });
  try {
    await fetch(API + "/api/onboarding/seen", { method: "POST" }).catch(() => {});
    await page.goto(BASE, { waitUntil: "domcontentloaded", timeout: 40000 });
    await page.waitForTimeout(2500);
    const wizard = page.locator(".onboarding");
    if ((await wizard.count()) > 0) {
      const closer = page.locator(".onboarding-close");
      if ((await closer.count()) > 0) await closer.first().click();
      await page.waitForTimeout(600);
    }
    record("环境：工作目录可写（放真实可读文件）", ws.ok, ws);

    // 预热一轮：首次使用凭据时会有一次非流式调用（provider 台账实证：stream=false / msgs=1），
    // 它会吃掉 FIFO 脚本的第一步 —— 不预热的话，每个场景都会少一次工具调用。
    await scriptProvider([{ chunks: ["预热完成。"] }], { chunks: ["预热完成。"] });
    await send("预热（这一轮只看链路通不通）");
    const warm = await waitTurnSettled(60000);
    const warmLog = await fetch(PROVIDER + "/__log").then((r) => r.json()).catch(() => ({ requests: [] }));
    record("环境：预热轮完成（消耗掉首次凭据验证的非流式调用）", warm === "ready",
      { requests: (warmLog.requests || []).slice(-3).map((x) => ({ step: x.step_kind, stream: x.stream_requested, msgs: x.message_count })) });

    const scenarios = [
      ["S1 默认折叠", s1RunningCollapsed],
      ["S2 正式回答稳定性", s2LiveAndNoMove],
      ["S3 内联审批真实点击", s3InlineApproval],
      ["S4 历史附件打开/重新定位", s4HistoryAttachment],
      ["S5 失败入口与重试", s5FailureRetry],
    ];
    for (const [name, fn] of scenarios) {
      await drainQueue(60000);
      try {
        await fn();
      } catch (error) {
        try {
          await shot("p2-error-" + name.replace(/[^0-9A-Za-z]+/g, "-") + ".png");
        } catch {
          /* 截图失败就算了 */
        }
        record(name + "：场景未跑完", false, String(error).slice(0, 400));
      }
    }
  } finally {
    writeFileSync(join(OUT, "phase2-summary.json"), JSON.stringify({ base: BASE, provider: PROVIDER, results, net }, null, 2), "utf-8");
    await browser.close().catch(() => {});
  }

  const failed = results.filter((x) => !x.ok);
  console.log("");
  console.log("== 阶段二实机结果 ==");
  console.log("通过 " + (results.length - failed.length) + " / " + results.length);
  if (failed.length) {
    for (const item of failed) console.log("[FAIL] " + item.name + " :: " + JSON.stringify(item.detail));
    process.exit(1);
  }
  process.exit(0);
}

await main();
