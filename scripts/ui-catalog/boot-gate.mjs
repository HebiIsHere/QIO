/**
 * 启动状态的实拍复核（2026-09-25）。
 *
 * 拍什么：
 *   `waiting` —— 后端还没起来时，界面必须是"正在启动 QIO 后端…（已等 N 秒）"，
 *                而不是一片空白；等不到 60 秒后要给出原因、日志位置与重试按钮。
 *   `ready`   —— 后端起来后，同一套代码要能自己把主界面渲染出来（不是靠刷新）。
 *
 * 用法（先起前端 dev server，`ready` 阶段再起后端）：
 *   $env:QIO_BASE="http://127.0.0.1:5199"; $env:QIO_API="http://127.0.0.1:8734"
 *   node scripts/ui-catalog/boot-gate.mjs waiting
 *   node scripts/ui-catalog/boot-gate.mjs ready
 */
import {
  API as ENV_API,
  BASE as ENV_BASE,
  DEFAULT_VIEWPORT,
  createSession,
  launchBrowser,
  readManifest,
  saveManifest,
} from "./lib.mjs";

const MAIN = { base: ENV_BASE, api: ENV_API };
const GROUP = "bootgate";
const phase = process.argv[2] || "waiting";

const entries = readManifest(GROUP)?.entries ?? [];
const log = [];
let failed = 0;

function put(entry) {
  const index = entries.findIndex((e) => e.id === entry.id);
  if (index >= 0) entries[index] = entry;
  else entries.push(entry);
}

const browser = await launchBrowser();
const session = await createSession(browser, {
  group: GROUP,
  name: "启动状态",
  theme: "dark",
  viewport: DEFAULT_VIEWPORT,
  ...MAIN,
});

try {
  if (phase === "waiting") {
    // 后端故意不起来：先看"正在启动"，等它超时后看失败态
    await session.goto("#/", { waitFor: ".boot-note", settle: 600 });
    const starting = await session.page.locator(".boot-note").first().innerText();
    const startingOk = starting.includes("正在启动");
    log.push(`waiting-1: ${startingOk ? "PASS" : "FAIL"}｜${starting.replace(/\n/g, " / ")}`);
    if (!startingOk) failed += 1;
    put(await session.shot("bootgate-01-starting", "后端没起来时：说明在等什么，而不是白屏"));

    await session.page.waitForSelector(".boot-note.err", { timeout: 90_000 });
    const failedText = await session.page.locator(".boot-note.err").innerText();
    const failedOk =
      failedText.includes("后端没有应答") &&
      failedText.includes("QIO.log") &&
      failedText.includes("重试");
    log.push(`waiting-2: ${failedOk ? "PASS" : "FAIL"}｜${failedText.replace(/\n/g, " / ")}`);
    if (!failedOk) failed += 1;
    put(await session.shot("bootgate-02-failed", "等不到后端：给出原因、日志位置与重试"));
  } else {
    // 后端已经在跑：同一套代码必须自己渲染出主界面
    await session.goto("#/", { waitFor: ".conversation", settle: 800 });
    const note = session.page.locator(".boot-note");
    const noteGone = (await note.count()) === 0;
    log.push(`ready: ${noteGone ? "PASS" : "FAIL"}｜启动说明已消失，主界面已渲染`);
    if (!noteGone) failed += 1;
    put(await session.shot("bootgate-03-ready", "后端就绪：自动进入主界面"));
  }
} catch (err) {
  failed += 1;
  log.push(`${phase}: FAIL ${String(err?.message ?? err).slice(0, 200)}`);
} finally {
  await session.context.close();
  await browser.close();
}

saveManifest(GROUP, entries);
console.log(log.join("\n"));
console.log(`\n合计 ${failed} 条不通过`);
process.exit(failed ? 1 : 0);
