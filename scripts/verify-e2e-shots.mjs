// D 阶段二：用 Playwright 驱动真实页面，覆盖 Lead 要求的截图场景。
//
// 为什么不是 msedge --screenshot：那个只能截「首屏静态」，无法覆盖
// 运行中/完成收起/历史展开/审批内联/附件准备中这些**状态**。这里用 Playwright
// （channel=msedge，仍然是无头 Edge）驱动真实交互后截图。
//
// 跑法（verify-e2e.ps1 会调它；也可单独跑，前提是后端+前端已起、凭据已建）：
//     node scripts/verify-e2e-shots.mjs
// 环境变量：QIO_E2E_BASE（默认 http://127.0.0.1:5199）、QIO_E2E_PROVIDER（默认 8798）、
//          QIO_E2E_SHOTS（默认 docs/verification-shots）、QIO_PLAYWRIGHT（覆盖 playwright 路径）

import { createRequire } from "node:module";
import { mkdirSync, writeFileSync } from "node:fs";
import { resolve, join } from "node:path";
import { tmpdir } from "node:os";

const require = createRequire(import.meta.url);
const PW =
  process.env.QIO_PLAYWRIGHT ||
  "C:\\\\Users\\\\zxy\\\\.cache\\\\codex-runtimes\\\\codex-primary-runtime\\\\dependencies\\\\node\\\\node_modules\\\\playwright";
const { chromium } = require(PW);

const BASE = process.env.QIO_E2E_BASE || "http://127.0.0.1:5199";
const PROVIDER = process.env.QIO_E2E_PROVIDER || "http://127.0.0.1:8798";
// 后端 API（前端 dev server 与后端不同端口）：用来把首次引导标记成「已看」，
// 否则欢迎向导会盖住页面，点击全被挡住（实测踩过）。
const API = process.env.QIO_E2E_API || "http://127.0.0.1:8734";
const INPUT = 'textarea[aria-label="输入消息"]';
const OUT = resolve(process.env.QIO_E2E_SHOTS || "docs/verification-shots");
mkdirSync(OUT, { recursive: true });

const results = [];
function record(name, ok, detail) {
  results.push({ name, ok, detail });
  console.log((ok ? "[PASS] " : "[FAIL] ") + name + (detail ? " :: " + JSON.stringify(detail) : ""));
}

async function scriptProvider(steps, fallback) {
  await fetch(PROVIDER + "/__reset", { method: "POST", headers: { "content-type": "application/json" }, body: "{}" });
  await fetch(PROVIDER + "/__script", {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({ steps, ...(fallback ? { default: fallback } : {}) }),
  });
}

async function shot(page, name) {
  const path = join(OUT, name);
  await page.screenshot({ path });
  return path;
}

const LONG_ANSWER = [
  "先给你一段较长的回答，用来检查长回答与代码块的排版。",
  "",
  "## 关键步骤",
  "",
  "1. 统一过程区只保留一个入口；",
  "2. 阶段历史可以展开回看；",
  "3. 完成之后自动收起，但总耗时仍然看得见。",
  "",
  "~~~python",
  "def verify(process, stages):",
  "    assert len(process) == 1",
  "    for stage in stages:",
  "        assert stage['index'] >= 1",
  "    return 'ok'",
  "~~~",
  "",
  "| 项目 | 期望 |",
  "| --- | --- |",
  "| 过程区 | 每轮一个 |",
  "| 耗时 | 未展开可见 |",
  "",
  "最后再补一句：上面这段足够长，用来检查窄窗口下的换行与溢出。",
].join("\n");

function chunk(text, size) {
  const out = [];
  for (let i = 0; i < text.length; i += size) out.push(text.slice(i, i + size));
  return out;
}

async function waitReady(page, timeout = 60000) {
  await page.waitForSelector('[data-test="turn-process"][data-state="ready"]', { timeout });
}

/** 发一条消息：必须用对话输入框，并且等发送按钮真的可用（禁用时点击会等到超时）。 */
async function send(page, text) {
  const input = page.locator(INPUT);
  await input.waitFor({ state: "visible", timeout: 15000 });
  await input.fill(text);
  const button = page.locator(".send-btn");
  await button.waitFor({ state: "visible", timeout: 10000 });
  for (let i = 0; i < 60; i++) {
    if (await button.first().isEnabled()) break;
    await page.waitForTimeout(100);
  }
  await button.first().click();
  // 审批到达时若用户仍在输入框里，界面**按设计**不自动展开（只亮入口）。
  // 真机截图要看到审批，就必须先失焦，模拟用户离开输入框。
  await page.evaluate(() => document.activeElement && document.activeElement.blur());
}

async function main() {
  const browser = await chromium.launch({ channel: "msedge", headless: true });
  const page = await browser.newPage({ viewport: { width: 1440, height: 900 } });
  try {
    try {
      await fetch(API + "/api/onboarding/seen", { method: "POST" });
    } catch (error) {
      record("标记首次引导已看", false, String(error).slice(0, 200));
    }
    await page.goto(BASE, { waitUntil: "domcontentloaded", timeout: 30000 });
    await page.waitForTimeout(2000);
    // 兜底：向导还在就点关闭，否则后面的点击会被模态挡住
    const wizard = page.locator(".onboarding");
    if ((await wizard.count()) > 0) {
      const closer = page.locator(".onboarding-close");
      if ((await closer.count()) > 0) await closer.first().click();
      await page.waitForTimeout(600);
      record("首次引导向导已关闭", (await page.locator(".onboarding").count()) === 0, null);
    }

    // ---- S1/S2/S3：运行中 → 阶段历史展开 → 完成自动收起（长回答 + 代码块）----
    try {
      const chunks = chunk(LONG_ANSWER, 18);
      await scriptProvider([
        { chunks: chunks, chunk_delay_ms: 120 },
      ]);
      await send(page, "请给一段较长的回答，带代码块");
      await page.waitForSelector('[data-test="turn-process"][data-state="running"]', { timeout: 20000 });
      record("统一过程区：运行中截图", true, await shot(page, "01-process-running.png"));

      // 这一轮没人碰过开关 → 完成后必须自动收起（手动开合过的那一轮不收起，是另一条规则）
      await waitReady(page);
      await page.waitForTimeout(800);
      const collapsed = (await page.locator('[data-test="turn-process-history"]').count()) === 0;
      record("完成自动收起截图", collapsed, await shot(page, "03-process-collapsed-complete.png"));
      const answerText = await page.locator(".stream").innerText();
      record(
        "长回答与代码块截图",
        answerText.includes("def verify") && answerText.includes("关键步骤"),
        await shot(page, "04-long-answer-codeblock.png"),
      );

      // 阶段历史要有阶段才看得见：另起一轮，让 provider 发两个带 _qio.stage 的工具轮
      const stageArgs = (path, text, op, name) =>
        JSON.stringify({ path, _qio: { kind: "progress", text, stage: { op, name } } });
      await scriptProvider([
        {
          tool_chunks: [
            { id: "c1", name: "fs_read", args_fragments: [stageArgs("README.md", "正在读取仓库结构", "start", "读取仓库结构")] },
          ],
          chunk_delay_ms: 10,
        },
        {
          tool_chunks: [
            { id: "c2", name: "fs_read", args_fragments: [stageArgs("AGENTS.md", "正在核对实现", "next", "核对实现")] },
          ],
          chunk_delay_ms: 10,
        },
        { chunks: ["两步都做完了。"], chunk_delay_ms: 10 },
      ]);
      await send(page, "请分两步读两个文件");
      await page.waitForSelector('[data-test="turn-process"][data-state="running"]', { timeout: 20000 });
      // 等阶段真的出现（STAGE 事件 + 工具轮），最多 20s；失败时把过程区文本带进诊断
      let historyVisible = false;
      let regionText = "";
      for (let i = 0; i < 100; i++) {
        const region = page.locator('[data-test="turn-process"]').last();
        regionText = (await region.count()) ? await region.innerText() : "";
        if (await page.locator('[data-test="turn-process-history"]').count()) {
          historyVisible = true;
          break;
        }
        if (regionText.includes("读取仓库结构") || regionText.includes("核对实现")) {
          // 必须是**最后一个**过程区的开关：前面轮次的过程区也在 DOM 里
          const toggle = page.locator('[data-test="turn-process"]').last().locator('[data-test="turn-process-toggle"]');
          if ((await toggle.count()) > 0 && (await toggle.first().getAttribute("aria-expanded")) !== "true") {
            await toggle.first().click();
          }
        }
        await page.waitForTimeout(200);
      }
      await page.waitForTimeout(400);
      record("阶段历史展开截图", historyVisible, {
        shot: await shot(page, "02-stage-history.png"),
        region: regionText.slice(0, 400),
      });

    } catch (error) {
      record("统一过程区场景", false, String(error).slice(0, 300));
    }

    // ---- S4：窄窗口 ----
    try {
      await page.setViewportSize({ width: 480, height: 900 });
      await page.waitForTimeout(800);
      record("窄窗口截图", true, await shot(page, "05-narrow.png"));
      await page.setViewportSize({ width: 1440, height: 900 });
      await page.waitForTimeout(400);
    } catch (error) {
      record("窄窗口截图", false, String(error).slice(0, 300));
    }

    // ---- S5：审批内联（fs_write 属写操作，按策略应请求确认）----
    try {
      // run_shell 无条件请求审批（cmd_tools.py:153），且我们随后会点「拒绝」，不会真的执行
      await scriptProvider([
        // repeat：凭据验证会先吃掉若干步，本轮必须还能拿到这个工具调用
        { tool: "run_shell", args: { cmd: "echo qio-verify-approval-probe" }, repeat: 4 },
        { chunks: ["已跳过。"], chunk_delay_ms: 10 },
      ]);
      await send(page, "请写入一个文件");
      const approval = page.locator('[data-test="turn-process-approval"]');
      let inline = false;
      try {
        await approval.waitFor({ state: "visible", timeout: 15000 });
        inline = true;
      } catch {
        inline = false;
      }
      // 兜底：审批也可能走独立模态（同一审批只能有一个入口，不重复截图）
      const modal = page.locator('[role="dialog"]', { hasText: "拒绝" });
      let modalVisible = (await modal.count()) > 0 && (await modal.first().isVisible());
      if (!inline && !modalVisible) {
        // 兜底：审批入口可能只是一个「有待办」的按钮/徽标（不自动展开的那条路径）
        const entry = page.locator("button, [role='button']", { hasText: /确认|待办|审批/ });
        if ((await entry.count()) > 0) {
          await entry.first().click();
          await page.waitForTimeout(600);
          modalVisible = (await modal.count()) > 0 && (await modal.first().isVisible());
          if (!modalVisible && (await approval.count()) > 0) inline = true;
        }
      }
      record("审批内联截图", inline, {
        shot: await shot(page, "06-approval-inline.png"),
        inline,
        modal: modalVisible,
      });
      const reject = inline
        ? page.locator('[data-test="turn-process-approval"] button', { hasText: "拒绝" })
        : page.locator('[role="dialog"] button', { hasText: "拒绝" });
      if ((await reject.count()) > 0) await reject.first().click();
      await page.waitForTimeout(1200);
      try {
        await fetch(API + "/api/turns/cancel", { method: "POST" });
      } catch {
        /* 没有在跑的轮次也无所谓 */
      }
      await page.waitForTimeout(800);
    } catch (error) {
      record("审批内联截图", false, String(error).slice(0, 200));
    }

    // ---- S6：附件准备中（浏览器回退：input[type=file] 走字节上传）----
    try {
      // 大文件写到系统临时目录：它是 60MB 的探针，绝不能进仓库（截图目录只放图）
      const big = join(tmpdir(), "qio-verify-big-attachment.bin");
      writeFileSync(big, Buffer.alloc(60 * 1024 * 1024, 7));
      await page.setInputFiles(".file-input", big);
      const hint = page.locator(".attach-hint");
      await hint.waitFor({ state: "visible", timeout: 8000 });
      record("附件准备中截图", true, await shot(page, "07-attachment-preparing.png"));
      await page.waitForTimeout(4000);
      record("附件登记完成截图", true, await shot(page, "08-attachment-ready.png"));
    } catch (error) {
      record("附件准备中截图", false, String(error).slice(0, 200));
    }
  } finally {
    await browser.close();
  }

  const failed = results.filter((item) => !item.ok);
  const summary = { base: BASE, provider: PROVIDER, out: OUT, results, failed: failed.map((f) => f.name) };
  writeFileSync(join(OUT, "shots-summary.json"), JSON.stringify(summary, null, 2) + "\n", "utf-8");
  console.log(JSON.stringify(summary, null, 2));
  process.exit(failed.length ? 1 : 0);
}

main().catch((error) => {
  console.error("verify-e2e-shots 失败：", error);
  process.exit(2);
});
