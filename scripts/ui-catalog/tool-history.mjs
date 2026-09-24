/**
 * 工具调用历史的实拍复核（2026-09-24）。
 *
 * 拍什么：刷新之后历史里还有没有工具卡、展开能不能看到参数与输出全文、
 * 输出被清理 / 没保存时文案对不对、长输出会不会把卡片撑破。
 *
 * 数据不是注入事件造的（历史记录只从数据库读），由同目录流程先往隔离实例的库里
 * 写两条真实记录（见 README 与本文件用法）。
 *
 * 用法：
 *   python scripts/ui-catalog/instance.py up --name toolhist --backend-port 8843 --frontend-port 6208 --clean
 *   $env:QIO_BASE="http://127.0.0.1:6208"; $env:QIO_API="http://127.0.0.1:8843"
 *   node scripts/ui-catalog/tool-history.mjs
 *   python scripts/ui-catalog/instance.py down --name toolhist
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
const GROUP = "toolhist";
/** 与写入脚本里的输出对得上的一句（用于确认展开的是全文而不是 400 字预览） */
const SENTINEL = "文件 25.txt";

const entries = readManifest(GROUP)?.entries ?? [];
const log = [];
let failed = 0;

function put(entry) {
  const index = entries.findIndex((e) => e.id === entry.id);
  if (index >= 0) entries[index] = entry;
  else entries.push(entry);
}

async function overflow(page, selector) {
  return page.evaluate((sel) => {
    const el = document.querySelector(sel);
    if (!el) return null;
    return { scrollWidth: el.scrollWidth, clientWidth: el.clientWidth };
  }, selector);
}

async function runCase({ id, title, viewport, expectMissingNote }) {
  const session = await createSession(browser, {
    group: GROUP,
    name: "工具调用历史",
    theme: "dark",
    viewport,
    ...MAIN,
  });
  try {
    // 关键：整页重新加载 —— 「刷新之后还看得到」正是这个功能的验收点
    await session.goto("#/", { waitFor: ".conversation", settle: 2200 });
    const cards = session.page.locator('.tool-card[data-state="failed"], .tool-card[data-state="ready"]');
    const count = await cards.count();
    if (count < 2) throw new Error(`历史里应有 2 张工具卡，实际 ${count} 张`);

    // 第一张：失败 + 长输出；第二张：没保存输出
    const first = cards.first();
    await first.locator(".tool-head").click();
    await sleep(900);
    const detail = await first.locator(".tool-detail").innerText();
    const labels = await first.locator(".sec-label").allInnerTexts();
    const notes = await first.locator(".tool-note").allInnerTexts();
    const second = cards.nth(1);
    await second.locator(".tool-head").click();
    await sleep(900);
    const secondNotes = await second.locator(".tool-note").allInnerTexts();

    const hasArgs = labels.includes("参数") && detail.includes('"path"');
    const hasFullOutput = detail.includes(SENTINEL);
    const redacted = detail.includes("***redacted***") && !detail.includes("sk-demo-should-be-redacted");
    const missingNoteOk = expectMissingNote
      ? secondNotes.join(" ").includes("未保存")
      : true;
    const card = await overflow(session.page, ".tool-card");
    const noOverflow = card && card.scrollWidth <= card.clientWidth + 1;
    const ok = hasArgs && hasFullOutput && redacted && missingNoteOk && noOverflow;
    if (!ok) failed += 1;
    log.push(
      `${id}: ${ok ? "PASS" : "FAIL"} 参数=${hasArgs} 全文=${hasFullOutput} 打码=${redacted} ` +
        `未保存提示=${missingNoteOk} 溢出=${noOverflow ? "无" : "有"}\n` +
        `      参数段=${labels.join("/")}｜卡片 ${card?.scrollWidth}/${card?.clientWidth}\n` +
        `      第一条提示：${notes.join(" | ") || "（无）"}\n` +
        `      第二条提示：${secondNotes.join(" | ") || "（无）"}`,
    );
    put(await session.shot(id, title, { note: `展开后标签：${labels.join(" / ")}` }));
    put(await session.shotEl(".tool-card", `${id}-crop`, `${title}（局部）`, { pad: 10 }));
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
    id: "toolhist-01-failed-long-output",
    title: "刷新后历史里仍有工具卡：展开看到参数、全文与打码后的密钥",
    viewport: DEFAULT_VIEWPORT,
    expectMissingNote: true,
  });
  await runCase({
    id: "toolhist-02-narrow",
    title: "窄窗口（820px）：长输出折行，卡片不横向溢出",
    viewport: NARROW_VIEWPORT,
    expectMissingNote: true,
  });
} finally {
  await browser.close();
}

saveManifest(GROUP, entries);
console.log(log.join("\n"));
console.log(`\n合计 ${failed} 条不通过`);
process.exit(failed ? 1 : 0);
