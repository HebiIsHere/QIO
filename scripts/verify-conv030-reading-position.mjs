// 0.3.0 对话页阅读位置 / 回到最新 / 辅助入口行的**真实浏览器回归场景**。
//
// 为什么需要它：jsdom 没有布局，虚拟列表按 120px 估算再逐条实测真实高度，
// 「恢复同一段内容仍在相近视口位置」只能在真实浏览器里证明；本脚本不满足于
// scrollTop 数字相等，而是比较**目标段落相对消息流视口的偏移**。
//
// 跑法（需先起假厂商 + 后端 + 前端 dev server，见 scripts/verify-e2e.ps1 的前 3 步）：
//   node scripts/verify-conv030-reading-position.mjs
// 环境变量：QIO_E2E_BASE（默认 http://127.0.0.1:5199）、QIO_E2E_PROVIDER（默认 8798）、
//          QIO_E2E_API（默认 http://127.0.0.1:8734）、QIO_PLAYWRIGHT（覆盖 playwright 路径）、
//          QIO_CONV030_SHOTS（默认 docs/verification-shots-conv030）
//
// 结论口径：这是真实前端 + 真实虚拟列表 + 本机扮演的假厂商（真 SSE）的浏览器验证；
// 不证明真实厂商，也不证明 Windows 原生窗口外轮廓。

import { createRequire } from "node:module";
import { mkdirSync, writeFileSync } from "node:fs";
import { resolve, join } from "node:path";

const require = createRequire(import.meta.url);
const PW =
  process.env.QIO_PLAYWRIGHT ||
  "D:\\dsh\\npm-cache\\_npx\\e41f203b7505f1fb\\node_modules\\playwright";
const { chromium } = require(PW);

const BASE = process.env.QIO_E2E_BASE || "http://127.0.0.1:5199";
const PROVIDER = process.env.QIO_E2E_PROVIDER || "http://127.0.0.1:8798";
const API = process.env.QIO_E2E_API || "http://127.0.0.1:8734";
const OUT = resolve(process.env.QIO_CONV030_SHOTS || "docs/verification-shots-conv030");
mkdirSync(OUT, { recursive: true });

const results = [];
function record(name, ok, detail) {
  results.push({ name, ok: Boolean(ok), detail: detail ?? null });
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

function longAnswer(group, paragraphs = 44) {
  const lines = ["## 长回答第 " + group + " 组", "", "这是一段用于真实虚拟列表测量变化的长回答。", ""];
  for (let i = 1; i <= paragraphs; i += 1) {
    lines.push(
      "第 " + i + " 段 · 长回答第" + group + "组：为了验证虚拟列表先估算再实测真实高度，" +
        "这一段故意写得足够长，包含中文、英文 mixed content、数字 " + (group * 100 + i) +
        "，以及一个可定位的标记 MARKER-G" + group + "-P" + i + "。",
    );
    lines.push("");
  }
  lines.push("长回答第 " + group + " 组结束。");
  return lines.join("\n");
}

/**
 * 等后端确认这一轮真的结束（虚拟列表只渲染视口附近的轮次，不能用 DOM 里的
 * turn-process 数量当轮次总数；那会把「列表只渲染了 6 条」误判成「第 7 轮没完成」）。
 */
async function waitTurnCompleted(expectedTurns) {
  const terminal = new Set(["completed", "failed", "cancelled", "incomplete", "unknown"]);
  for (let i = 0; i < 450; i += 1) {
    const resp = await fetch(API + "/api/session/context?limit=200");
    if (resp.ok) {
      const ctx = await resp.json();
      const facts = Array.isArray(ctx.turn_facts) ? ctx.turn_facts : [];
      const last = facts[facts.length - 1];
      if (facts.length >= expectedTurns && last && terminal.has(String(last.status))) {
        return last;
      }
    }
    await new Promise((r) => setTimeout(r, 200));
  }
  throw new Error("等待第 " + expectedTurns + " 轮结束超时");
}

async function waitDomText(page, text) {
  await page.waitForFunction(
    (needle) => (document.body.innerText || "").includes(needle),
    text,
    { timeout: 30000 },
  );
}

async function waitSendable(page) {
  const button = page.locator(".send-btn").first();
  await button.waitFor({ state: "visible", timeout: 20000 });
  for (let i = 0; i < 200; i += 1) {
    if (await button.isEnabled()) return;
    await page.waitForTimeout(100);
  }
  throw new Error("发送按钮一直不可用");
}

async function send(page, text) {
  const input = page.locator('textarea[aria-label^="输入消息"]');
  await input.waitFor({ state: "visible", timeout: 20000 });
  // 首屏刚挂载时输入框可能已经可见、但发送闸门还没就绪（历史/设置读取中）。
  // 填一次不成功就等一会再填，直到发送按钮真的可用；不靠固定 sleep 猜。
  const button = page.locator(".send-btn").first();
  for (let attempt = 0; attempt < 60; attempt += 1) {
    await input.fill(text);
    await page.waitForTimeout(200);
    if ((await input.inputValue()) !== text) {
      await page.waitForTimeout(400);
      continue;
    }
    if (await button.isEnabled()) {
      await button.click();
      await page.evaluate(() => document.activeElement && document.activeElement.blur());
      return;
    }
    await page.waitForTimeout(500);
  }
  throw new Error("发送按钮一直不可用");
}

async function shot(page, name) {
  const path = join(OUT, name);
  await page.screenshot({ path });
  return name;
}

function readState() {
  const stream = document.querySelector(".stream");
  if (!stream) return null;
  const back = document.querySelector('[data-test="back-to-latest"]');
  const count = back ? back.querySelector(".count") : null;
  return {
    scrollTop: stream.scrollTop,
    max: stream.scrollHeight - stream.clientHeight,
    scrollHeight: stream.scrollHeight,
    clientHeight: stream.clientHeight,
    backVisible: Boolean(back),
    backAria: back ? back.getAttribute("aria-label") : null,
    badge: count ? count.textContent : null,
    turns: document.querySelectorAll("[data-turn-key]").length,
  };
}

function offsetOfMarker(marker) {
  const stream = document.querySelector(".stream");
  if (!stream) return null;
  const walker = document.createTreeWalker(stream, NodeFilter.SHOW_TEXT);
  let node = walker.nextNode();
  while (node) {
    if ((node.data || "").includes(marker)) {
      const el = node.parentElement;
      if (el) {
        const sr = stream.getBoundingClientRect();
        const er = el.getBoundingClientRect();
        return {
          offset: er.top - sr.top,
          scrollTop: stream.scrollTop,
          max: stream.scrollHeight - stream.clientHeight,
          scrollHeight: stream.scrollHeight,
          text: (el.textContent || "").slice(0, 80),
        };
      }
    }
    node = walker.nextNode();
  }
  return null;
}

async function main() {
  const browser = await chromium.launch({ channel: "msedge", headless: true });
  const page = await browser.newPage({ viewport: { width: 1440, height: 900 } });
  try {
    await page.addInitScript(() => {
      try {
        localStorage.setItem("qio-theme", "dark");
      } catch {
        /* ignore */
      }
    });
    await page.goto(BASE, { waitUntil: "domcontentloaded", timeout: 30000 });
    await page.waitForSelector(".stream", { timeout: 30000 });
    await page.waitForSelector('textarea[aria-label^="输入消息"]', { timeout: 30000 });
    // 首次引导若还在，关掉（本轮不验证引导）
    const wizardClose = page.locator(".onboarding-close");
    if ((await wizardClose.count()) > 0) await wizardClose.first().click();

    // ---- 种一段真实长对话：8 轮 × 长回答（虚拟列表会先估算、再逐条实测） ----
    // 每轮除了主回答还会有一次辅助调用（非流式）：把默认回复也设成长回答，
    // 保证主回答一定拿到长文本，同时不依赖两次请求的先后顺序。
    for (let group = 1; group <= 8; group += 1) {
      await scriptProvider([], { chunks: [longAnswer(group)], chunk_delay_ms: 0 });
      await send(page, "请给出长回答第 " + group + " 组");
      await waitDomText(page, "长回答第 " + group + " 组结束。");
      await waitTurnCompleted(group);
      record("种子轮 " + group + " 拿到真实长回答", true, null);
      await page.waitForTimeout(150);
    }
    await page.waitForTimeout(1200);

    // ---- A. 离开前在底部：返回后主动贴底并继续跟随（不依赖新消息） ----
    const beforeA = await page.evaluate(readState);
    record("A1 离开前确实在底部（真实长对话）", beforeA && Math.abs(beforeA.scrollTop - beforeA.max) <= 2, beforeA);
    record("A2 虚拟列表已渲染真实轮次块", beforeA && beforeA.turns > 0, { turns: beforeA?.turns });
    await shot(page, "01-wide-dark-bottom.png");

    await page.evaluate(() => {
      location.hash = "#/settings";
    });
    await page.waitForTimeout(700);
    await page.evaluate(() => {
      location.hash = "#/";
    });
    await page.waitForSelector(".stream", { timeout: 15000 });
    await page.waitForTimeout(1500);
    const afterA = await page.evaluate(readState);
    record(
      "A3 设置返回后主动恢复到底部并保持跟随（没有新消息也贴底）",
      afterA && Math.abs(afterA.scrollTop - afterA.max) <= 2 && !afterA.backVisible,
      afterA,
    );

    // ---- B. 离开前在长回答内部：返回后同一段内容仍在相近视口位置 ----
    const marker = "MARKER-G5-P22";
    await page.evaluate((m) => {
      const stream = document.querySelector(".stream");
      const walker = document.createTreeWalker(stream, NodeFilter.SHOW_TEXT);
      let node = walker.nextNode();
      while (node) {
        if ((node.data || "").includes(m)) {
          const el = node.parentElement;
          const sr = stream.getBoundingClientRect();
          const er = el.getBoundingClientRect();
          stream.scrollTop += er.top - sr.top - 180;
          stream.dispatchEvent(new Event("scroll"));
          return;
        }
        node = walker.nextNode();
      }
      throw new Error("marker not found: " + m);
    }, marker);
    await page.waitForTimeout(600);
    const savedB = await page.evaluate(offsetOfMarker, marker);
    record("B1 已滚到长回答内部（标记段落可见）", savedB && Math.abs(savedB.offset - 180) <= 24, savedB);
    record("B2 保存时不在底部（回到最新入口应出现）", savedB && savedB.scrollTop < savedB.max - 200, {
      scrollTop: savedB?.scrollTop,
      max: savedB?.max,
    });
    await shot(page, "02-wide-dark-history-internal.png");

    // 离开到设置；离开期间后端真的完成一轮新消息（不是伪造 UI 消息）
    await page.evaluate(() => {
      location.hash = "#/settings";
    });
    await page.waitForTimeout(600);
    const newTurn = await page.evaluate(async (api) => {
      const ctx = await (await fetch(api + "/api/session/context?limit=1")).json();
      const resp = await fetch(api + "/api/turns", {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({ message: "离开期间来的新消息（浏览器回归）", topic_id: ctx.topic_id ?? null }),
      });
      return { status: resp.status, body: await resp.text() };
    }, API);
    record("B3 离开期间后端确实受理了新的一轮", newTurn && newTurn.status === 200, newTurn);
    // 等后端这一轮结束（假厂商立即返回）
    await page.waitForTimeout(2500);

    await page.evaluate(() => {
      location.hash = "#/";
    });
    await page.waitForSelector(".stream", { timeout: 15000 });
    // 恢复是有界的：轮询到入口出现或超时，不做固定等待
    await page
      .waitForSelector('[data-test="back-to-latest"]', { timeout: 8000 })
      .catch(() => null);
    await page.waitForTimeout(600);
    const restoredB = await page.evaluate(offsetOfMarker, marker);
    const stateB = await page.evaluate(readState);
    record(
      "B4 返回后同一段内容仍在相近视口位置（不是只比较 scrollTop）",
      restoredB && Math.abs(restoredB.offset - savedB.offset) <= 24,
      { savedOffset: savedB?.offset, restoredOffset: restoredB?.offset, savedScrollTop: savedB?.scrollTop, restoredScrollTop: restoredB?.scrollTop },
    );
    record(
      "B5 离开期间的新消息没有把阅读位置拉走（停止跟随）",
      stateB && stateB.scrollTop < stateB.max - 200 && stateB.backVisible,
      stateB,
    );
    record(
      "B6 新消息数量在回到最新入口上提示",
      stateB && stateB.backVisible && stateB.badge && Number(stateB.badge) >= 1,
      { badge: stateB?.badge, aria: stateB?.backAria },
    );
    await shot(page, "03-wide-dark-restored-new-messages.png");

    // ---- C. 辅助入口行以真实输入胶囊为基准居中；回到最新用强调色实底 ----
    const geometry = await page.evaluate(() => {
      const row = document.querySelector(".composer-entries .entry-row");
      const stack = document.querySelector(".composer-stack");
      const button = document.querySelector('[data-test="back-to-latest"]');
      if (!row || !stack || !button) return null;
      const rr = row.getBoundingClientRect();
      const sr = stack.getBoundingClientRect();
      const br = button.getBoundingClientRect();
      const style = getComputedStyle(button);
      return {
        rowCenter: rr.left + rr.width / 2,
        stackCenter: sr.left + sr.width / 2,
        buttonWidth: br.width,
        buttonHeight: br.height,
        background: style.backgroundColor,
        color: style.color,
        borderRadius: style.borderRadius,
        lastChild: row.lastElementChild === button,
      };
    });
    record(
      "C1 入口行以真实输入胶囊为基准居中（误差 ≤ 2px）",
      geometry && Math.abs(geometry.rowCenter - geometry.stackCenter) <= 2,
      geometry,
    );
    record(
      "C2 回到最新在入口行末尾、尺寸与其它入口一致（34px 圆形）",
      geometry && geometry.lastChild && Math.round(geometry.buttonWidth) === 34 && Math.round(geometry.buttonHeight) === 34,
      geometry,
    );
    record(
      "C3 强调色实底 + 白色箭头（颜色来自令牌）",
      geometry && geometry.background === "rgb(197, 27, 125)" && geometry.color === "rgb(255, 238, 248)",
      { background: geometry?.background, color: geometry?.color },
    );

    // 入口行参与正常布局：消息区在它上方结束，按钮不悬在正文里
    const layoutC = await page.evaluate(() => {
      const stage = document.querySelector(".message-stage");
      const row = document.querySelector(".composer-entries .entry-row");
      if (!stage || !row) return null;
      const sr = stage.getBoundingClientRect();
      const rr = row.getBoundingClientRect();
      return { stageBottom: sr.bottom, rowTop: rr.top, overlap: sr.bottom > rr.top + 1 };
    });
    record(
      "C4 入口行参与正常布局，消息区在其上方结束（不遮挡正文）",
      layoutC && !layoutC.overlap && layoutC.stageBottom <= layoutC.rowTop + 1,
      layoutC,
    );

    // 长输入：胶囊变高，入口行仍以胶囊为基准居中且不溢出
    const longInput = page.locator('textarea[aria-label^="输入消息"]');
    await longInput.fill("很长的输入内容，用来检查输入胶囊变高时入口行与按钮是否仍然稳定。".repeat(12));
    await page.waitForTimeout(400);
    const layoutLong = await page.evaluate(() => {
      const row = document.querySelector(".composer-entries .entry-row");
      const stack = document.querySelector(".composer-stack");
      const dock = document.querySelector(".composer-dock");
      if (!row || !stack || !dock) return null;
      const rr = row.getBoundingClientRect();
      const sr = stack.getBoundingClientRect();
      const dr = dock.getBoundingClientRect();
      return {
        rowCenter: rr.left + rr.width / 2,
        stackCenter: sr.left + sr.width / 2,
        overflow: rr.left < dr.left - 1 || rr.right > dr.right + 1,
      };
    });
    record(
      "C5 长输入时入口行仍以胶囊为中心且不溢出",
      layoutLong && !layoutLong.overflow && Math.abs(layoutLong.rowCenter - layoutLong.stackCenter) <= 2,
      layoutLong,
    );
    await longInput.fill("");
    await page.waitForTimeout(200);

    // 左侧任务栏收起 / 展开：入口行与胶囊中心不变
    const collapseButton = page.locator('button[aria-label="收起任务栏"]');
    if ((await collapseButton.count()) > 0) {
      await collapseButton.first().click();
      await page.waitForTimeout(500);
      const collapsed = await page.evaluate(() => {
        const row = document.querySelector(".composer-entries .entry-row");
        const stack = document.querySelector(".composer-stack");
        if (!row || !stack) return null;
        const rr = row.getBoundingClientRect();
        const sr = stack.getBoundingClientRect();
        return { rowCenter: rr.left + rr.width / 2, stackCenter: sr.left + sr.width / 2 };
      });
      record(
        "C6 左侧任务栏收起后入口行仍以胶囊为中心",
        collapsed && Math.abs(collapsed.rowCenter - collapsed.stackCenter) <= 2,
        collapsed,
      );
      const railEntry = page.locator("[data-rail-entry]");
      if ((await railEntry.count()) > 0) {
        await railEntry.first().click();
        await page.waitForTimeout(500);
      }
    } else {
      record("C6 左侧任务栏收起后入口行仍以胶囊为中心", false, { reason: "找不到收起任务栏按钮" });
    }

    // ---- D. 点击回到最新：到达底部、恢复跟随、入口隐藏；不取消接续选择 ----
    const chipBefore = await page.evaluate(
      () => document.querySelector(".topic-chip")?.textContent?.trim() ?? null,
    );
    await page.locator('[data-test="back-to-latest"]').click();
    await page.waitForTimeout(900);
    const afterD = await page.evaluate(readState);
    const chipAfter = await page.evaluate(
      () => document.querySelector(".topic-chip")?.textContent?.trim() ?? null,
    );
    record(
      "D1 点击回到最新后到底部并恢复跟随（入口隐藏）",
      afterD && Math.abs(afterD.scrollTop - afterD.max) <= 2 && !afterD.backVisible,
      afterD,
    );
    record(
      "D2 回到最新只改变阅读位置：接续选择提示原样保留",
      chipBefore === chipAfter && chipAfter !== null,
      { chipBefore, chipAfter },
    );

    // ---- E. 减少动态效果：立即到达底部，不依赖动画 ----
    await page.emulateMedia({ reducedMotion: "reduce" });
    await page.evaluate(() => {
      const stream = document.querySelector(".stream");
      stream.scrollTop = 120;
      stream.dispatchEvent(new Event("scroll"));
    });
    await page.waitForTimeout(300);
    const beforeE = await page.evaluate(readState);
    if (beforeE?.backVisible) {
      await page.locator('[data-test="back-to-latest"]').click();
      await page.waitForTimeout(80);
      const afterE = await page.evaluate(readState);
      record(
        "E1 减少动态效果下点击立即贴底（不播放平滑动画）",
        afterE && Math.abs(afterE.scrollTop - afterE.max) <= 2 && !afterE.backVisible,
        afterE,
      );
    } else {
      record("E1 减少动态效果下点击立即贴底（不播放平滑动画）", false, { reason: "回到最新入口未出现", beforeE });
    }
    await page.emulateMedia({ reducedMotion: "no-preference" });

    // ---- F. 浅色主题 / 最小支持窗口截图（真实布局，不是设计稿） ----
    await page.evaluate(() => document.documentElement.setAttribute("data-theme", "light"));
    await page.waitForTimeout(500);
    await shot(page, "04-wide-light-bottom.png");

    await page.setViewportSize({ width: 800, height: 600 });
    await page.waitForTimeout(800);
    // 窄窗口下先离开底部，检查入口行换行/不溢出，再截图
    await page.evaluate(() => {
      const stream = document.querySelector(".stream");
      stream.scrollTop = Math.max(0, stream.scrollTop - 500);
      stream.dispatchEvent(new Event("scroll"));
    });
    await page.waitForTimeout(500);
    const narrow = await page.evaluate(() => {
      const row = document.querySelector(".composer-entries .entry-row");
      const dock = document.querySelector(".composer-dock");
      const stack = document.querySelector(".composer-stack");
      if (!row || !dock || !stack) return null;
      const rr = row.getBoundingClientRect();
      const dr = dock.getBoundingClientRect();
      const sr = stack.getBoundingClientRect();
      return {
        rowLeft: rr.left,
        rowRight: rr.right,
        dockLeft: dr.left,
        dockRight: dr.right,
        rowCenter: rr.left + rr.width / 2,
        stackCenter: sr.left + sr.width / 2,
        overflow: rr.left < dr.left - 1 || rr.right > dr.right + 1,
      };
    });
    record(
      "F1 800×600 最小支持窗口：入口行不溢出、仍以胶囊为中心",
      narrow && !narrow.overflow && Math.abs(narrow.rowCenter - narrow.stackCenter) <= 2,
      narrow,
    );
    await shot(page, "05-narrow-light-history.png");

    await page.evaluate(() => document.documentElement.setAttribute("data-theme", "dark"));
    await page.waitForTimeout(300);
    await shot(page, "06-narrow-dark-history.png");

    const summary = {
      generatedAt: new Date().toISOString(),
      base: BASE,
      provider: PROVIDER,
      viewport: "1440x900 / 800x600",
      results,
      pass: results.filter((r) => r.ok).length,
      fail: results.filter((r) => !r.ok).length,
    };
    writeFileSync(join(OUT, "summary.json"), JSON.stringify(summary, null, 2), "utf8");
    console.log("SUMMARY pass=" + summary.pass + " fail=" + summary.fail);
    if (summary.fail > 0) process.exitCode = 1;
  } finally {
    await browser.close();
  }
}

main().catch((error) => {
  console.error("[FATAL]", error);
  process.exitCode = 1;
});
