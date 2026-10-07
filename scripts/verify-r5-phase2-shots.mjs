// D 阶段二（R5）：实机交互取证（假厂商 SSE → uvicorn → vite → msedge 无头 → Playwright）。
//
// S1 上传失败/客户端暂停 → 界面反馈（明确失败，不是无限转圈）
// S2 正式回答在 provider 结束前流式可见且不重复（含未声明的降级路径）
// S3 带附件重试（复制克隆）→ 界面正常、内容正确
// S4 回归：默认折叠 / 内联审批 / 历史附件打开 / 结束原因与耗时
//
// 口径：provider 是本机扮演的假厂商，结论只说明「QIO 自己的链路对」。
// 已知边界（报告里如实写）：强制 os.link 失败需要在后端进程内打桩，实机（独立进程）做不到；
//   那条由后端验收件 tests/test_r5_clone_thread_verify.py（线程闸门 + 记录执行线程）覆盖。

import { createRequire } from "node:module";
import { mkdirSync, writeFileSync, unlinkSync, existsSync, rmSync, renameSync } from "node:fs";
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
const OUT = resolve(process.env.QIO_E2E_SHOTS || "docs/verification-shots-r5-phase2");
const DATA = process.env.QIO_E2E_DATA || join(tmpdir(), "qio-r5-phase2-data");
const ATTACH_DIR = join(DATA, "attach-src");
mkdirSync(OUT, { recursive: true });
mkdirSync(ATTACH_DIR, { recursive: true });

const INPUT = 'textarea[aria-label="输入消息"]';
const DECL = "[[QIO:ANSWER]]";
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
    return (await (await fetch(PROVIDER + "/__timeline")).json()).timeline || [];
  } catch {
    return [];
  }
}

async function requestLog() {
  try {
    const body = await (await fetch(PROVIDER + "/__log")).json();
    return body.requests || body.log || [];
  } catch {
    return [];
  }
}

async function scriptProvider(steps) {
  await fetch(PROVIDER + "/__reset", { method: "POST", headers: { "content-type": "application/json" }, body: "{}" });
  await fetch(PROVIDER + "/__script", {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({ steps }),
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

const answerText = () => page.locator(".stream .message.assistant").allInnerTexts();
const processText = () => page.locator('[data-test="turn-process"]').allInnerTexts();

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

async function attachByPath(filePath) {
  await page.locator('button[aria-label="粘贴本地文件路径"]').click();
  const input = page.locator('input[aria-label="本地文件路径"]');
  await input.waitFor({ state: "visible", timeout: 10000 });
  await input.fill(filePath);
  await input.press("Enter");
  return waitFor(async () => {
    const text = await page.locator(".composer").first().innerText().catch(() => "");
    return /已保存副本/.test(text) && !/还在准备中/.test(text) ? true : null;
  }, { timeout: 60000 });
}

async function clearChips() {
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
    await page.waitForTimeout(250);
  }
}

// ---- S1：上传失败时界面必须明确失败（不是无限转圈） ----------------------------------

async function s1UploadFailureFeedback() {
  const now = new Date();
  const monthDir = join(DATA, "attachments", String(now.getFullYear()), String(now.getMonth() + 1).padStart(2, "0"));
  const aside = monthDir + ".r5-aside";
  let swapped = false;
  const src = join(ATTACH_DIR, "r5-上传失败.txt");
  writeFileSync(src, "这份上传必须失败。\n", "utf-8");
  let outcome = null;
  try {
    // 应用是按需建 <data>/attachments/<年>/<月>；这里先把「年」目录建出来，才能把「月」占成文件
    mkdirSync(join(DATA, "attachments", String(now.getFullYear())), { recursive: true });
    if (existsSync(monthDir)) {
      rmSync(aside, { recursive: true, force: true });
      renameSync(monthDir, aside);
      swapped = true;
    }
    writeFileSync(monthDir, "受控错误：这里本该是目录（写盘失败取证）", "utf-8");
    await page.locator('button[aria-label="粘贴本地文件路径"]').click();
    const input = page.locator('input[aria-label="本地文件路径"]');
    await input.waitFor({ state: "visible", timeout: 10000 });
    await input.fill(src);
    await input.press("Enter");
    outcome = await waitFor(async () => {
      const text = await page.locator(".composer").first().innerText().catch(() => "");
      if (!/r5-上传失败/.test(text)) return null;
      const noLongerPreparing = !/准备中/.test(text);
      const saysFailed = /失败|出错|打不开|不能|写不|不可用|没有成功|不在原位|丢失/.test(text);
      return noLongerPreparing && saysFailed ? text : null;
    }, { timeout: 45000 });
  } finally {
    rmSync(monthDir, { force: true });
    if (swapped) renameSync(aside, monthDir);
  }
  const composerText = (await page.locator(".composer").first().innerText().catch(() => "")).replace(/\s+/g, " ");
  const failureShot = await shot("r5-01-upload-failed.png");
  record("S1 上传写盘失败：界面在有限时间内给出明确失败与原因（不是无限转圈）", !!outcome, {
    composerText: composerText.slice(0, 200),
    shot: failureShot,
  });
  const rows = await (await fetch(API + "/api/attachments?unbound=true")).json().catch(() => ({ attachments: [] }));
  const mine = (rows.attachments || []).filter((a) => a.name === "r5-上传失败.txt");
  record("S1 失败上传没有停在「准备中」的残留记录", mine.every((a) => a.state !== "prepared"), {
    rows: mine.map((a) => [a.name, a.state]),
  });
  await clearChips();
}

// ---- S2：正式回答在 provider 结束前流式可见、不重复 ----------------------------------

async function s2AnswerStreamingNoDuplicate() {
  await scriptProvider([
    { chunks: [] },
    { chunks: [DECL + "\n", "正式回答第一句。", "正式回答第二句。"], chunk_delay_ms: 900 },
  ]);
  const endsBefore = (await timeline()).filter((e) => e.event === "stream_end").length;
  await send("直接回答我（流式取证）");
  const observed = await waitFor(async () => {
    const joined = (await answerText()).join("\n");
    return joined.includes("正式回答第一句。") ? Date.now() : null;
  }, { timeout: 30000 });
  const tlAtObservation = (await timeline()).map((e) => e.event);
  const lastStart = tlAtObservation.lastIndexOf("stream_start");
  const callStillOpen = lastStart >= 0 && !tlAtObservation.slice(lastStart + 1).includes("stream_end");
  const midShot = await shot("r5-02-answer-live-before-provider-end.png");
  const midProcess = (await processText()).join("\n");
  record("S2 回答在 provider 结束前已出现在正式回答容器（.message.assistant）", !!observed && callStillOpen, {
    observed: !!observed,
    answerCallStillOpen: callStillOpen,
    streamEndsBeforeSend: endsBefore,
    shot: midShot,
  });
  record("S2 命中时刻过程区不含这段正式回答", !midProcess.includes("正式回答第一句。"), {
    process: midProcess.slice(0, 140),
  });

  await waitIdle();
  await page.waitForTimeout(800);
  const afterShot = await shot("r5-03-answer-after-complete.png");
  const pageText = (await page.locator(".stream").innerText().catch(() => "")).replace(/\s+/g, " ");
  const answers = (await answerText()).join("\n");
  const process = (await processText()).join("\n");
  const occurrences = pageText.split("正式回答第一句。").length - 1;
  record("S2 完成后：同一答案全局只出现一次（回答区一份、过程区没有副本）",
    occurrences === 1 && answers.includes("正式回答第一句。") && !process.includes("正式回答第一句。"), {
      occurrences,
      inProcess: process.includes("正式回答第一句。"),
      shot: afterShot,
    });
  // 调用台账：/__reset 之后这一轮只应当消耗脚本里的 2 步（工作调用 + 声明回答），
  // 不允许出现「为同一答案再生成一次」的第三次调用
  const log = await requestLog();
  const kinds = log.map((e) => e.step_kind || e.step);
  record("S2 未发生「为同一答案再生成一次」的额外调用（调用台账 ≤ 脚本步数 2）", log.length <= 2, {
    requests: log.length,
    kinds,
  });

  // 未声明的降级路径：一次性交付到回答区、过程区无副本
  await scriptProvider([{ chunks: [] }, { chunks: ["未声明的完整答案：一次性出现。"], chunk_delay_ms: 10 }]);
  await send("未声明路径取证");
  await waitIdle();
  await page.waitForTimeout(800);
  const plainShot = await shot("r5-04-undeclared-fallback.png");
  const plainAnswers = (await answerText()).join("\n");
  const plainProcess = (await processText()).join("\n");
  const plainPage = (await page.locator(".stream").innerText().catch(() => "")).replace(/\s+/g, " ");
  record("S2 未声明降级路径：正文一次性出现在回答容器、过程区不含它、全局一份",
    plainAnswers.includes("未声明的完整答案") && !plainProcess.includes("未声明的完整答案") &&
      plainPage.split("未声明的完整答案").length - 1 === 1, {
      inAnswer: plainAnswers.includes("未声明的完整答案"),
      inProcess: plainProcess.includes("未声明的完整答案"),
      occurrences: plainPage.split("未声明的完整答案").length - 1,
      shot: plainShot,
    });
}

// ---- S3：带附件重试（复制克隆）界面正常、内容正确 ------------------------------------

async function s3RetryWithAttachment() {
  const src = join(ATTACH_DIR, "r5-重试附件.txt");
  const MARKER = "R5 实机附件内容：只有这份副本里才有的标记 5b21";
  writeFileSync(src, MARKER + "\n", "utf-8");
  await scriptProvider([{ status: 500, body: "r5-retry-boom", repeat: 8 }]);
  const chipReady = await attachByPath(src);
  await send("带附件的一轮（第一轮会失败）");
  const retryAppeared = await waitFor(
    async () => ((await page.getByRole("button", { name: /^重试$/ }).count()) > 0 ? true : null),
    { timeout: 90000 },
  );
  const failedShot = await shot("r5-05-failed-with-retry.png");
  record("S3 第一轮失败后出现真实「重试」入口", chipReady === true && !!retryAppeared, {
    chipReady, retryAppeared, shot: failedShot,
  });
  if (!retryAppeared) return;

  const before = (await (await fetch(API + "/api/session/context")).json()).messages || [];
  const idsBefore = before.flatMap((m) => (m.attachments || []).map((a) => a.id));
  const originalId = idsBefore[idsBefore.length - 1] || null;

  unlinkSync(src); // 原文件删除：重试必须靠已保存的副本
  await scriptProvider([{ chunks: [DECL + "\n", "我看过你带来的文件了。"] }]);
  await page.getByRole("button", { name: /^重试$/ }).last().click();
  await waitIdle();
  await page.waitForTimeout(1500);
  const retryShot = await shot("r5-06-after-retry.png");

  const after = (await (await fetch(API + "/api/session/context")).json()).messages || [];
  const idsAfter = after.flatMap((m) => (m.attachments || []).map((a) => a.id));
  const newIds = idsAfter.filter((id) => id !== originalId);
  let content = null;
  let state = null;
  if (newIds.length) {
    const meta = await (await fetch(API + "/api/attachments/" + newIds[newIds.length - 1])).json();
    state = (meta.attachment || {}).state;
    const resp = await fetch(API + "/api/attachments/" + newIds[newIds.length - 1] + "/content");
    content = resp.ok ? await resp.text() : null;
  }
  const answersAfter = (await answerText()).join("\n");
  const lastRegion = (await processText()).slice(-1)[0] || "";
  record("S3 重试真的开了新轮，新轮附件是克隆（新 id、原文件已删除）且内容读得回来",
    newIds.length > 0 && !!content && content.includes(MARKER), {
      originalId, newIds, state, content: (content || "").slice(0, 60), shot: retryShot,
    });
  // 「界面正常」只看**这一轮**：正式回答出现，且最新一轮过程区是已完成（不是失败）。
  // 整页文本会包含上一轮失败的历史，不能拿它当判据。
  record("S3 重试后这一轮界面正常（正式回答出现 + 最新一轮已完成、无失败提示）",
    answersAfter.includes("我看过你带来的文件了。") && /已完成/.test(lastRegion) && !/已失败/.test(lastRegion), {
      answerTail: answersAfter.slice(-60),
      lastRegion: lastRegion.replace(/\s+/g, " ").slice(0, 120),
    });
}

// ---- S4：回归（默认折叠 / 内联审批 / 历史附件打开 / 结束原因） -----------------------

async function s4Regressions() {
  await scriptProvider([
    {
      chunks: ["先读一下文件。"],
      chunk_delay_ms: 120,
      tool_chunks: [{ id: "r5_g1", name: "echo", args_fragments: ['{"text": "r5"}'] }],
    },
    { chunks: [DECL + "\n", "回归取证：这一轮结束了。"] },
  ]);
  await send("回归取证（默认折叠）");
  const runningSeen = await waitFor(
    async () => ((await page.locator('[data-test="turn-process"]').count()) > 0 ? true : null),
    { timeout: 25000 },
  );
  const historyCount = await page.locator('[data-test="turn-process-history"]').count();
  const collapseShot = await shot("r5-07-running-default-collapsed.png");
  record("S4 运行中默认折叠：默认可见区没有历史抽屉", !!runningSeen && historyCount === 0, {
    historyCount, shot: collapseShot,
  });

  await waitIdle();
  await page.waitForTimeout(1200);
  const endText = (await page.locator(".stream").innerText().catch(() => "")).replace(/\s+/g, " ");
  record("S4 结束原因与耗时可见（已完成 + 耗时）", /已完成/.test(endText) && /耗时/.test(endText), {
    tail: endText.slice(-160),
  });

  // 内联审批：脚本一次 run_shell（需要审批）→ 断言三者可见 → 拒绝
  await scriptProvider([
    {
      chunks: [],
      tool_chunks: [
        {
          id: "r5_appr",
          name: "run_shell",
          args_fragments: [
            JSON.stringify({
              cmd: "echo r5-phase2-approval",
              _qio: { explanation: "模型说明：为了验证审批链路，我要在受限子进程里跑一条无害的回显命令。" },
            }),
          ],
        },
      ],
    },
    { chunks: [DECL + "\n", "我取消了这个命令。"] },
  ]);
  await send("请运行一条命令");
  const cardSeen = await waitFor(
    async () => ((await page.locator('[data-test="turn-process-approval"]').count()) > 0 ? true : null),
    { timeout: 40000 },
  );
  const cardText = cardSeen
    ? (await page.locator('[data-test="turn-process-approval"]').first().innerText()).replace(/\s+/g, " ")
    : "";
  const approvalShot = await shot("r5-08-inline-approval.png");
  record("S4 内联审批：描述/独立说明/真实命令三者都在卡里", !!cardSeen &&
    cardText.includes("会执行命令") && cardText.includes("模型说明") && cardText.includes("echo r5-phase2-approval"), {
      cardText: cardText.slice(0, 180), shot: approvalShot,
    });
  if (cardSeen) {
    await page.getByRole("button", { name: /^拒绝$/ }).first().click().catch(() => null);
    await waitIdle();
    await page.waitForTimeout(600);
    await shot("r5-09-approval-rejected.png");
  }

  // 历史附件打开（刷新后）
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
  const histShot = await shot("r5-10-history-attachment-open.png");
  record("S4 回归：刷新后历史附件行仍在，点「打开」→ GET /content 200", rowCount > 0 && contentStatus === 200, {
    rowCount, contentStatus, shot: histShot,
  });
}

const scenarios = [
  ["S1", s1UploadFailureFeedback],
  ["S2", s2AnswerStreamingNoDuplicate],
  ["S3", s3RetryWithAttachment],
  ["S4", s4Regressions],
];

async function main() {
  const browser = await chromium.launch({ channel: "msedge", headless: true });
  const context = await browser.newContext({ viewport: { width: 1440, height: 900 } });
  page = await context.newPage();
  page.on("response", async (resp) => {
    const url = resp.url();
    if (url.includes("/api/attachments") || url.includes("/api/turns")) {
      try {
        net.push({ method: resp.request().method(), status: resp.status(), url });
      } catch {
        /* 忽略 */
      }
    }
  });

  await page.goto(BASE, { waitUntil: "domcontentloaded", timeout: 40000 });
  await waitAppReady();
  await fetch(API + "/api/onboarding/seen", { method: "POST" }).catch(() => null);
  await page.reload({ waitUntil: "domcontentloaded" });
  await waitAppReady();
  record("环境：首次引导已关闭", (await page.locator(".onboarding").count()) === 0, {});

  // 预热：首次使用凭据会多一次非流式调用（吃掉 FIFO 第一步）
  await scriptProvider([{ chunks: ["预热完成。"] }]);
  await send("预热（只看链路通不通）");
  await waitIdle(60000);

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
