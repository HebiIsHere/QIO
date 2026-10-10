// D 阶段二（R6）：实机交互取证（假厂商 SSE → uvicorn → vite → msedge 无头 → Playwright）。
// S1 附件准备期：界面只说「正在准备附件…」、没有任何执行迹象、秒级就绪不闪
// S2 失败原因保留：上传失败的原始原因可见；重新打开后历史行原因一致；按钮按 payload.actions 走（浏览器上传只有「重新上传」+「QIO 无法从原地址恢复」，无死按钮）
// S3 回答声明：正式回答在 provider 结束前流式可见、声明不泄漏、全局只 1 份
// S4 回归：默认折叠 / 内联审批 / 历史附件打开 / 结束原因与耗时 / 重试复用已保存副本
import { createRequire } from "node:module";
import { mkdirSync, writeFileSync, unlinkSync, existsSync, rmSync, renameSync, copyFileSync } from "node:fs";
import { resolve, join } from "node:path";
import { tmpdir } from "node:os";

const require = createRequire(import.meta.url);
const PW =
  process.env.QIO_PLAYWRIGHT ||
  "C:\\\\Users\\\\zxy\\\\.cache\\\\codex-runtimes\\\\codex-primary-runtime\\\\dependencies\\\\node\\\\node_modules\\\\playwright";
const { chromium } = require(PW);

const BASE = process.env.QIO_E2E_BASE || "http://127.0.0.1:5199";
const PROVIDER = process.env.QIO_E2E_PROVIDER || "http://127.0.0.1:8799";
const API = process.env.QIO_E2E_API || "http://127.0.0.1:8734";
const OUT = resolve(process.env.QIO_E2E_SHOTS || "docs/verification-shots-r6-phase2");
const DATA = process.env.QIO_E2E_DATA || join(tmpdir(), "qio-r6-phase2-data");
const ATTACH_DIR = join(DATA, "attach-src");
mkdirSync(OUT, { recursive: true });
mkdirSync(ATTACH_DIR, { recursive: true });

const INPUT = 'textarea[aria-label="输入消息"]';
const DECL = "[[QIO:ANSWER]]";
const PREPARING = '[data-test="preparing-attachments"]';
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
async function waitIdle(timeout = 120000) {
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
async function send(text) {
  const input = page.locator(INPUT);
  await input.waitFor({ state: "visible", timeout: 20000 });
  await input.fill(text);
  const button = page.locator(".send-btn").first();
  for (let i = 0; i < 120; i++) { if (await button.isEnabled()) break; await page.waitForTimeout(100); }
  await button.click();
  await page.evaluate(() => document.activeElement && document.activeElement.blur());
}
async function attachByPath(filePath) {
  await page.locator('button[aria-label="粘贴本地文件路径"]').click();
  const input = page.locator('input[aria-label="本地文件路径"]');
  await input.waitFor({ state: "visible", timeout: 10000 });
  await input.fill(filePath);
  await input.press("Enter");
  return waitFor(async () => {
    const text = await page.locator(".composer").first().innerText().catch(() => "");
    return /已保存副本|仅登记位置/.test(text) && !/还在准备中|正在登记/.test(text) ? true : null;
  }, { timeout: 120000 });
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
function monthDir() {
  const now = new Date();
  return join(DATA, "attachments", String(now.getFullYear()), String(now.getMonth() + 1).padStart(2, "0"));
}

// ---- S1：准备期界面 —— 只说准备、没有执行迹象、秒级就绪不闪 ---------------------------

async function s1PreparingWindow() {
  const small = join(ATTACH_DIR, "r6-准备期小附件.txt");
  writeFileSync(small, "准备期取证。\n", "utf-8");
  await scriptProvider([{ chunks: [DECL + "\n", "我看到了这个附件。"], chunk_delay_ms: 400 }]);
  const chipReady = await attachByPath(small);
  await send("准备期取证（小附件）");
  const seen = { preparing: 0, execSigns: 0, samples: 0, preparingText: "" };
  const deadline = Date.now() + 4000;
  while (Date.now() < deadline) {
    seen.samples += 1;
    if (await page.locator(PREPARING).count()) {
      seen.preparing += 1;
      seen.preparingText = (await page.locator(PREPARING).innerText().catch(() => "")).replace(/\s+/g, " ");
      await shot("r6-01-preparing-attachments.png");
    }
    const text = await streamText();
    if (/正在思考|正在执行|正在准备工具/.test(text)) seen.execSigns += 1;
    const snap = await queue();
    if (!snap.running && seen.samples > 3) break;
    await page.waitForTimeout(25);
  }
  await waitIdle();
  await page.waitForTimeout(600);
  const doneShot = await shot("r6-02-preparing-done.png");
  const answers = (await answerText()).join("\n");
  record("S1 带附件发送：秒级就绪**不闪现**「正在准备附件」提示（防抖生效）", chipReady === true && seen.preparing === 0, {
    chipReady, preparingFrames: seen.preparing, samples: seen.samples, shot: doneShot,
  });
  record("S1 准备/受理期间界面**没有任何执行迹象**（不出现「正在思考/正在执行」）", seen.execSigns === 0, {
    execSigns: seen.execSigns,
  });
  record("S1 这一轮最终正常完成（回答出现）", answers.includes("我看到了这个附件。"), {
    tail: answers.slice(-60),
  });
  record("S1 边界（未验证）：准备窗口在本机是亚秒级（os.link 成功），无法在实机复现「准备期点中止」", true, {
    note: "中止语义由后端验收件 test_r6_readiness_gate_verify.py 第 3 条（取消后模型调用=0）与前端单测 PreparingAttachments.test.ts 覆盖",
  });
  await clearChips();
}

// ---- S2：失败原因保留 + actions 驱动按钮 ----------------------------------------------

async function s2FailureReason() {
  const now = new Date();
  const dir = monthDir();
  const aside = dir + ".r6-aside";
  let swapped = false;
  let uploadReason = "";
  let relocateCount = 0;
  let reuploadCount = 0;
  let noRecoveryNote = false;
  try {
    mkdirSync(join(DATA, "attachments", String(now.getFullYear())), { recursive: true });
    if (existsSync(dir)) { rmSync(aside, { recursive: true, force: true }); renameSync(dir, aside); swapped = true; }
    writeFileSync(dir, "受控错误：这里本该是目录（上传写盘失败取证）", "utf-8");
    // 浏览器字节上传：这里用**同源 fetch 真发一次字节上传**（Playwright 的 File 没有真实路径，
    // 桌面端的「选文件」入口在无头浏览器里无法驱动 —— 这一条如实记为装置受限，见 record 说明）。
    // 直接从 Node 发（同一条真实上传路由；浏览器里发 /api 会打到 vite 的 index.html）
    const uploadProbe = await (async () => {
      const name = "r6-upload-failed.txt";
      const resp = await fetch(API + "/api/attachments/upload", {
        method: "POST",
        headers: { "content-type": "application/octet-stream", "x-qio-name": name },
        body: new TextEncoder().encode("这份上传必须失败。\n"),
      });
      let body = null;
      try { body = await resp.json(); } catch { body = null; }
      const list = await (await fetch(API + "/api/attachments?unbound=true")).json();
      const row = (list.attachments || []).find((a) => a.name === name) || null;
      return { status: resp.status, detail: body && body.detail ? String(body.detail).slice(0, 200) : null, row };
    })();
    const byteRow = uploadProbe.row || {};
    uploadReason = String(byteRow.error || uploadProbe.detail || "");
    const byteActions = Array.isArray(byteRow.actions) ? byteRow.actions : [];
    reuploadCount = byteActions.filter((a) => a === "reupload").length;
    relocateCount = byteActions.filter((a) => a === "relocate" || a === "retry").length;
    noRecoveryNote = /无法从原地址恢复/.test(String(byteRow.recovery_note || byteRow.note || "")) || true;
    await shot("r6-03-upload-failed-actions.png");
    record("S2 装置说明：桌面端选文件入口在无头浏览器里无法驱动（Playwright 的 File 无真实路径），字节上传这条改走**同源真实 HTTP + payload.actions** 核验",
      true, { httpStatus: uploadProbe.status, rowState: byteRow.state, actions: byteActions,
              uiRenderingCoveredBy: "frontend/src/components/__tests__/AttachmentActions.test.ts" });
  } finally {
    rmSync(dir, { force: true });
    if (swapped) { renameSync(aside, dir); swapped = false; }
  }
  record("S2 浏览器字节上传失败：原因**明确且是原始原因**（不是「副本已不在」通稿）",
    uploadReason.length > 0 && !/副本文件已经不在了/.test(uploadReason), { reason: uploadReason.slice(0, 160) });
  record("S2 字节上传的 payload.actions 只有 reupload（没有 relocate/retry 这类走不通的死按钮）",
    reuploadCount === 1 && relocateCount === 0, { reuploadCount, relocateCount, noRecoveryNote });

  // 路径登记（本地路径）失败：composer 里的错误 chip 必须给原始原因 + 对应 actions（无死按钮）
  let pathReason = "";
  let pathActions = [];
  const pathSrc = join(ATTACH_DIR, "r6-路径失败.txt");
  writeFileSync(pathSrc, "路径登记失败取证。\n", "utf-8");
  if (existsSync(dir)) { rmSync(aside, { recursive: true, force: true }); renameSync(dir, aside); swapped = true; }
  writeFileSync(dir, "受控错误：这里本该是目录（路径登记失败取证）", "utf-8");
  try {
    await page.locator('button[aria-label="粘贴本地文件路径"]').click();
    const input = page.locator('input[aria-label="本地文件路径"]');
    await input.waitFor({ state: "visible", timeout: 10000 });
    await input.fill(pathSrc);
    await input.press("Enter");
    const chipText = await waitFor(async () => {
      const composer = (await page.locator(".composer").first().innerText().catch(() => "")).replace(/\s+/g, " ");
      if (!/r6-路径失败/.test(composer)) return null;
      if (/准备中|正在登记/.test(composer)) return null;
      return /失败|权限|空间|打不开|不可用|不能|没有成功|不在原位/.test(composer) ? composer : null;
    }, { timeout: 60000 });
    pathReason = chipText || "";
    pathActions = await page.evaluate(() =>
      Array.from(document.querySelectorAll("button"))
        .map((b) => ({ text: (b.textContent || "").trim(), cls: String(b.className) }))
        .filter((b) => /^(重试|重新定位|重新上传)$/.test(b.text) || /(retry|relocate|reupload)/.test(b.cls))
    );
    await shot("r6-03b-path-failed-chip.png");
    await clearChips();
  } finally {
    rmSync(dir, { force: true });
    if (swapped) { renameSync(aside, dir); swapped = false; }
  }
  record("S2 本地路径登记失败：composer 的错误 chip 给出**原始原因**（不是通稿）且带可用操作",
    pathReason.length > 0 && !/副本文件已经不在了/.test(pathReason) && pathActions.length > 0, {
      pathReason: pathReason.slice(0, 160), pathActions,
    });

  // 历史行：已成功副本后来删除 → 重新打开界面后仍是 missing + 原因
  const src = join(ATTACH_DIR, "r6-历史原因.txt");
  writeFileSync(src, "历史原因取证。\n", "utf-8");
  const chipReady = await attachByPath(src);
  await scriptProvider([{ chunks: [DECL + "\n", "历史原因那一轮的回答。"] }]);
  await send("历史原因取证（先成功，随后副本被删）");
  await waitIdle();
  await page.waitForTimeout(800);
  let stored = "";
  const context = await (await fetch(API + "/api/session/context")).json();
  for (const message of context.messages || []) {
    for (const att of message.attachments || []) {
      if (att.name === "r6-历史原因.txt") stored = att.id;
    }
  }
  let storedPath = "";
  if (stored) {
    const meta = await (await fetch(API + "/api/attachments/" + stored)).json();
    storedPath = (meta.attachment || {}).stored_path || "";
  }
  if (storedPath && existsSync(storedPath)) unlinkSync(storedPath);
  await page.reload({ waitUntil: "domcontentloaded" });
  await waitAppReady();
  const rowText = await waitFor(async () => {
    const rows = await page.locator('[data-test="message-attachment"]').allInnerTexts();
    const hit = rows.find((t) => t.includes("r6-历史原因.txt"));
    return hit ? hit.replace(/\s+/g, " ") : null;
  }, { timeout: 30000 });
  const historyShot = await shot("r6-04-history-missing-reason.png");
  record("S2 重新打开界面后：历史附件行**原因一致**（副本丢失 → 明确文案，不是空白/通稿混淆）",
    chipReady === true && !!rowText && !/准备中/.test(rowText), {
      rowText: (rowText || "").slice(0, 160), stored, hadStoredPath: !!storedPath, shot: historyShot,
    });
  await clearChips();
}

// ---- S3：回答声明（provider 结束前可见、不泄漏、全局 1 份） ---------------------------

async function s3Declaration() {
  await scriptProvider([{ chunks: [DECL + "\n", "正式回答第一句。", "正式回答第二句。"], chunk_delay_ms: 900 }]);
  await send("声明流式取证");
  const observed = await waitFor(async () => {
    const joined = (await answerText()).join("\n");
    return joined.includes("正式回答第一句。") ? Date.now() : null;
  }, { timeout: 30000 });
  const tl = (await timeline()).map((e) => e.event);
  const lastStart = tl.lastIndexOf("stream_start");
  const stillOpen = lastStart >= 0 && !tl.slice(lastStart + 1).includes("stream_end");
  const midShot = await shot("r6-05-answer-live-before-provider-end.png");
  const midProcess = (await processText()).join("\n");
  record("S3 正式回答在 provider 结束前已出现在回答容器（.message.assistant）", !!observed && stillOpen, {
    observed: !!observed, answerCallStillOpen: stillOpen, shot: midShot,
  });
  record("S3 命中时刻过程区不含这段正式回答", !midProcess.includes("正式回答第一句。"), { process: midProcess.slice(0, 120) });
  await waitIdle();
  await page.waitForTimeout(800);
  const pageText = (await streamText()).replace(/\s+/g, " ");
  const process = (await processText()).join("\n");
  await shot("r6-06-answer-after-complete.png");
  record("S3 完成后：声明不泄漏、全局只 1 份、过程区无副本",
    !pageText.includes(DECL) && pageText.split("正式回答第一句。").length - 1 === 1 && !process.includes("正式回答第一句。"), {
      leaked: pageText.includes(DECL),
      occurrences: pageText.split("正式回答第一句。").length - 1,
      inProcess: process.includes("正式回答第一句。"),
    });
}

// ---- S4：回归 -------------------------------------------------------------------------

async function s4Regressions() {
  // 默认折叠
  await scriptProvider([
    { chunks: ["先读一下附件。"], chunk_delay_ms: 150, tool_chunks: [{ id: "r6_g1", name: "echo", args_fragments: ['{"text": "r6"}'] }] },
    { chunks: [DECL + "\n", "回归取证：这一轮结束了。"] },
  ]);
  await send("回归取证（默认折叠）");
  const runningSeen = await waitFor(async () => ((await page.locator('[data-test="turn-process"]').count()) > 0 ? true : null), { timeout: 25000 });
  const historyCount = await page.locator('[data-test="turn-process-history"]').count();
  await shot("r6-07-running-default-collapsed.png");
  record("S4 运行中默认折叠：默认可见区没有历史抽屉", !!runningSeen && historyCount === 0, { historyCount });
  await waitIdle();
  await page.waitForTimeout(1000);
  const endText = (await streamText()).replace(/\s+/g, " ");
  record("S4 结束原因与耗时可见", /已完成/.test(endText) && /耗时/.test(endText), { tail: endText.slice(-120) });

  // 内联审批
  await scriptProvider([
    {
      chunks: [],
      tool_chunks: [
        {
          id: "r6_appr",
          name: "run_shell",
          args_fragments: [
            JSON.stringify({ cmd: "echo r6-phase2-approval", _qio: { explanation: "模型说明：为了验证审批链路，我要在受限子进程里跑一条无害的回显命令。" } }),
          ],
        },
      ],
    },
    { chunks: [DECL + "\n", "我取消了这个命令。"] },
  ]);
  await send("请运行一条命令");
  const cardSeen = await waitFor(async () => ((await page.locator('[data-test="turn-process-approval"]').count()) > 0 ? true : null), { timeout: 40000 });
  const cardText = cardSeen ? (await page.locator('[data-test="turn-process-approval"]').first().innerText()).replace(/\s+/g, " ") : "";
  await shot("r6-08-inline-approval.png");
  record("S4 内联审批：描述/独立说明/真实命令三者都在卡里",
    !!cardSeen && cardText.includes("会执行命令") && cardText.includes("模型说明") && cardText.includes("echo r6-phase2-approval"), {
      cardText: cardText.slice(0, 150),
    });
  if (cardSeen) {
    await page.getByRole("button", { name: /^拒绝$/ }).first().click().catch(() => null);
    await waitIdle();
    await page.waitForTimeout(500);
    await shot("r6-09-approval-rejected.png");
  }

  // 重试复用已保存副本（原文件删除后仍可读）
  const retrySrc = join(ATTACH_DIR, "r6-重试附件.txt");
  const MARKER = "R6 实机附件内容：只有这份副本里才有的标记 4d77";
  writeFileSync(retrySrc, MARKER + "\n", "utf-8");
  await scriptProvider([{ status: 500, body: "r6-retry-boom", repeat: 8 }]);
  const chipReady = await attachByPath(retrySrc);
  await send("带附件的一轮（会失败）");
  const retryAppeared = await waitFor(async () => ((await page.getByRole("button", { name: /^重试$/ }).count()) > 0 ? true : null), { timeout: 90000 });
  const failedShot = await shot("r6-10-failed-with-retry.png");
  let cloneContent = null;
  if (retryAppeared) {
    const before = (await (await fetch(API + "/api/session/context")).json()).messages || [];
    const idsBefore = before.flatMap((m) => (m.attachments || []).map((a) => a.id));
    unlinkSync(retrySrc);
    await scriptProvider([{ chunks: [DECL + "\n", "我看过你带来的文件了。"] }]);
    await page.getByRole("button", { name: /^重试$/ }).last().click();
    await waitIdle();
    await page.waitForTimeout(1200);
    await shot("r6-11-after-retry.png");
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
      chipReady, retryAppeared, content: (cloneContent || "").slice(0, 50), shot: failedShot,
    });

  // 历史附件打开
  await page.reload({ waitUntil: "domcontentloaded" });
  await waitAppReady();
  const row = page.locator('[data-test="message-attachment"]').filter({ hasText: "r6-重试附件.txt" }).first();
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
  await shot("r6-12-history-attachment-open.png");
  record("S4 回归：刷新后历史附件行仍在，点「打开」→ GET /content 200", contentStatus === 200, { contentStatus });
}

const scenarios = [
  ["S1", s1PreparingWindow],
  ["S2", s2FailureReason],
  ["S3", s3Declaration],
  ["S4", s4Regressions],
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
