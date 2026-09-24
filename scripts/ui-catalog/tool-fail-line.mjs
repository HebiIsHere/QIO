/**
 * 工具失败卡的一行结论：原因可读 + 窄窗口不溢出（2026-09-24 加固复核）。
 *
 * 为什么要单独拍：
 * 1. 以前失败原因超过 60 字就被折叠成「这次执行没有成功，展开可看原因」，
 *    而真实事故里的原因（`[WinError 3] … 'C:\Users\…\workspace'`）正好落在
 *    60–120 字之间 —— 用户看不到任何可读的原因。
 * 2. 放宽上限后，长路径（没有空格可断行）可能把卡片撑出横向溢出，所以要
 *    在窄窗口下真的量一次 `scrollWidth`，而不是只看截图顺眼。
 *
 * 用法（隔离实例，避免别人的事件串进来）：
 *   python scripts/ui-catalog/instance.py up --name toolfail --backend-port 8842 --frontend-port 6207
 *   $env:QIO_BASE="http://127.0.0.1:6207"; $env:QIO_API="http://127.0.0.1:8842"
 *   node scripts/ui-catalog/tool-fail-line.mjs
 *   python scripts/ui-catalog/instance.py down --name toolfail
 */
import {
  API as ENV_API,
  BASE as ENV_BASE,
  DEFAULT_VIEWPORT,
  NARROW_VIEWPORT,
  createSession,
  launchBrowser,
  readManifest,
  saveManifest,
  sleep,
} from "./lib.mjs";

const MAIN = { base: ENV_BASE, api: ENV_API };
const GROUP = "toolfail";

/** 真实事故里的原文（来自安装实例的 trace，长度 70 字上下） */
const WINERROR_REASON =
  "列目录失败：[WinError 3] 系统找不到指定的路径。: 'C:\\Users\\zxy\\AppData\\Roaming\\qio\\workspace'";
/** 超过 120 字的极端原因：必须截断，且仍然不横向溢出 */
const LONG_REASON = `抓取失败：连不上 https://example.com/${"very-long-path-segment/".repeat(12)}index.html（ConnectError: 没有更多说明）`;

const entries = readManifest(GROUP)?.entries ?? [];
const log = [];
let failed = 0;

function put(entry) {
  const index = entries.findIndex((e) => e.id === entry.id);
  if (index >= 0) entries[index] = entry;
  else entries.push(entry);
}

/** 一条失败的工具卡：先 TOOL_START 再 TOOL_END，和真实事件顺序一致 */
async function failCard(session, callId, reason) {
  await session.inject("TOOL_START", {
    call_id: callId,
    tool: "fs_list",
    turn_id: "toolfail_turn_1",
    presentation: { title: "列目录", status: "运行中", tool: "fs_list" },
    arguments: { path: "." },
  });
  await session.inject("TOOL_END", {
    call_id: callId,
    tool: "fs_list",
    ok: false,
    error: reason,
    content_preview: "",
    presentation: { title: "列目录", status: "失败", tool: "fs_list" },
    duration_ms: 4,
  });
  await sleep(600);
}

/** 量一行结论的真实可用宽度：横向溢出 = 卡片被撑破 */
async function overflow(page, selector) {
  return page.evaluate((sel) => {
    const el = document.querySelector(sel);
    if (!el) return null;
    return { scrollWidth: el.scrollWidth, clientWidth: el.clientWidth };
  }, selector);
}

async function runCase({ id, title, viewport, reason, expectFull }) {
  const session = await createSession(browser, {
    group: GROUP,
    name: "工具失败一行结论",
    theme: "dark",
    viewport,
    ...MAIN,
  });
  try {
    await session.goto("#/", { waitFor: ".conversation", settle: 1800 });
    await failCard(session, `toolfail_call_${id}`, reason);

    const line = session.page.locator(".tool-fail-line").first();
    if (!(await line.count())) throw new Error("页面上没有出现 .tool-fail-line");
    const text = (await line.innerText()).trim();
    const card = await overflow(session.page, ".tool-card");
    const lineBox = await overflow(session.page, ".tool-fail-line");

    const textOk = expectFull ? text === reason : text !== reason && text.includes("展开可看完整原因");
    const noOverflow =
      card && lineBox && card.scrollWidth <= card.clientWidth + 1 && lineBox.scrollWidth <= lineBox.clientWidth + 1;
    const ok = textOk && noOverflow;
    if (!ok) failed += 1;
    log.push(
      `${id}: ${ok ? "PASS" : "FAIL"} 文案${textOk ? "符合" : "不符"}｜溢出${noOverflow ? "无" : "有"}` +
        `\n      实际文案：${text}` +
        `\n      卡片 ${card?.scrollWidth}/${card?.clientWidth}，文案行 ${lineBox?.scrollWidth}/${lineBox?.clientWidth}`,
    );

    put(
      await session.shot(id, title, {
        note: `实测文案：${text}`,
        fullPage: false,
      }),
    );
    put(
      await session.shotEl(
        '.tool-card[data-state="failed"]',
        `${id}-crop`,
        `${title}（局部）`,
        { pad: 10 },
      ),
    );
  } catch (err) {
    failed += 1;
    log.push(`${id}: FAIL ${String(err?.message ?? err).slice(0, 200)}`);
  } finally {
    await session.context.close();
  }
}

const browser = await launchBrowser();
try {
  await runCase({
    id: "toolfail-01-reason-visible",
    title: "工具失败：真实原因（70 字）原样显示，不再折叠成笼统文案",
    viewport: DEFAULT_VIEWPORT,
    reason: WINERROR_REASON,
    expectFull: true,
  });
  await runCase({
    id: "toolfail-02-reason-narrow",
    title: "窄窗口（820px）：长原因折行，不把卡片撑出横向溢出",
    viewport: NARROW_VIEWPORT,
    reason: WINERROR_REASON,
    expectFull: true,
  });
  await runCase({
    id: "toolfail-03-reason-truncated",
    title: "超长原因（>120 字）：截断并提示展开，仍不横向溢出",
    viewport: NARROW_VIEWPORT,
    reason: LONG_REASON,
    expectFull: false,
  });
} finally {
  await browser.close();
}

saveManifest(GROUP, entries);
console.log(log.join("\n"));
console.log(`\n合计 ${failed} 条不通过`);
process.exit(failed ? 1 : 0);
