/**
 * WS4：「回到最新消息」按钮几何与滚动控制的真实界面实测（验证方写、Lead 执行）。
 *
 * 契约（`_JANK-RACE-CONTRACT.md` WS4）要测的：
 *   * 按钮**下缘距输入框上缘 8–12px**，水平对齐对话内容列中心；
 *   * 按钮中心 `elementFromPoint` 命中的就是按钮本身（没有被浮层/提示条盖住）；
 *   * 宽窗口 / 窄窗口 / 多行输入 / 候选卡出现与消失 / 缩放后**仍然成立**；
 *   * 最新消息完整可读；输入框与发送按钮可操作；
 *   * 上翻不自动回底；流式更新不重启已被用户中断的滚动。
 *
 * 真实 DOM 契约（读源码核对过）：按钮是 `.back-latest`，外层是 `.latest-anchor`
 * （`MessageStream.vue`，`position: fixed`；**没有用 Teleport** —— Lead 实测 Teleport 在挂载时
 * 目标还没进文档、`disabled` 为真、目标解析成 null 且永不恢复，按钮永远不出现）。
 *
 * 间距的**主指标是到输入区面板 `.composer` 上缘**：面板顶部那行 `.topicbar` 里有可交互的
 * 「取消」按钮，贴到 textarea 本体会让按钮落进那一行、`elementFromPoint` 命中 `div.topicbar`、
 * 按钮点不到（Lead 实测）。到 textarea 本体的距离作为**第二个参考数字**一起打印。
 *
 * 用法（需要隔离实例：前端 6206 + 后端 8841）：
 *   node scripts/ui-catalog/latest-button.mjs
 * 一键运行器：node "…\.verify-tmp\run-latest-button-check.mjs"
 */
import { mkdirSync, writeFileSync } from "node:fs";
import path from "node:path";

import { launchBrowser, createSession, sleep, SHOT_ROOT } from "./lib.mjs";

const BASE = process.env.QIO_BASE || "http://127.0.0.1:6206";
const API = process.env.QIO_API || "http://127.0.0.1:8841";
const GROUP = "latest-button";
const SHOT_DIR = path.join(SHOT_ROOT, GROUP);
const CHECKS_FILE = path.join(SHOT_ROOT, "latest-button-checks.json");

const results = [];
function record(id, title, outcome, detail = "") {
  results.push({ id, title, outcome, detail });
  console.log(`  [${outcome}] ${id} — ${title} :: ${detail}`);
}
async function check(id, title, fn) {
  try {
    record(id, title, "PASS", (await fn()) ?? "");
  } catch (e) {
    record(id, title, "FAIL", String(e?.message ?? e));
  }
}
function skip(id, title, why) {
  record(id, title, "SKIP", why);
}
function assert(cond, message) {
  if (!cond) throw new Error(message);
}

/**
 * 在页面里测量按钮 / 输入区面板 / textarea / 命中。
 *
 * **间距的主指标是「按钮下缘 → 输入区面板（`.composer`）上缘」**：
 * 面板顶部那一行 `.topicbar`（话题名 + Enter 提示 + 待落实接续的「取消」按钮）是可交互区域，
 * 把按钮贴到 textarea 本体会让它落在 `.topicbar` 那一行的范围内、被可交互控件挡住
 * （Lead 实测：贴 textarea 时 `elementFromPoint` 命中 `div.topicbar`，按钮点不到）。
 * 到 textarea 本体的距离仍然一起量，只作为**第二个参考数字**打印。
 */
async function measure(page) {
  return await page.evaluate(() => {
    const rectOf = (el) => {
      const r = el.getBoundingClientRect();
      return {
        top: r.top,
        bottom: r.bottom,
        left: r.left,
        right: r.right,
        width: r.width,
        height: r.height,
        cx: r.left + r.width / 2,
        cy: r.top + r.height / 2,
      };
    };
    const button = document.querySelector(".back-latest");
    const anchor = document.querySelector(".latest-anchor");
    const panel = document.querySelector(".composer");
    const textarea = document.querySelector("textarea");
    if (!button) return { button: null, anchorPresent: !!anchor, panelPresent: !!panel };
    const br = rectOf(button);
    const pr = panel ? rectOf(panel) : null;
    const tr = textarea ? rectOf(textarea) : null;
    const hit = document.elementFromPoint(br.cx, br.cy);
    const column = document.querySelector(".stream");
    const anchorStyle = anchor ? getComputedStyle(anchor) : null;
    return {
      button: br,
      panel: pr,
      textarea: tr,
      // 主指标：到输入区面板上缘
      gapPanel: pr ? Math.round((pr.top - br.bottom) * 10) / 10 : null,
      // 参考：到 textarea 本体（面板顶部那行可交互，所以这个数字天然更大）
      gapTextarea: tr ? Math.round((tr.top - br.bottom) * 10) / 10 : null,
      hitIsButton: !!hit && (hit === button || button.contains(hit)),
      hitTag: hit ? `${hit.tagName.toLowerCase()}.${String(hit.className || "").split(" ")[0]}` : null,
      anchorPresent: !!anchor,
      anchorPosition: anchorStyle ? anchorStyle.position : null,
      buttonInAnchor: !!anchor && anchor.contains(button),
      buttonInViewport:
        br.top >= 0 && br.bottom <= window.innerHeight && br.left >= 0 && br.right <= window.innerWidth,
      columnCenter: column ? Math.round(rectOf(column).cx * 10) / 10 : null,
      buttonCenter: Math.round(br.cx * 10) / 10,
      viewport: { w: window.innerWidth, h: window.innerHeight },
      zoom: document.body.style.zoom || "1",
    };
  });
}

async function streamScrollTop(page) {
  return await page.evaluate(() => {
    const el = document.querySelector(".stream");
    return el ? Math.round(el.scrollTop) : -1;
  });
}
async function scrollUp(page, by = 1400) {
  await page.evaluate((delta) => {
    const el = document.querySelector(".stream");
    if (el) el.scrollTop = Math.max(0, el.scrollTop - delta);
  }, by);
  await sleep(500);
}

/** 锚点的内联样式（诊断用：位置是它自己算出来写进去的） */
async function anchorSnapshot(page) {
  return await page.evaluate(() => {
    const anchor = document.querySelector(".latest-anchor");
    const button = document.querySelector(".back-latest");
    const panel = document.querySelector(".composer");
    return {
      anchorStyle: anchor ? anchor.getAttribute("style") || "" : null,
      anchorBottom: anchor ? Math.round(anchor.getBoundingClientRect().bottom * 10) / 10 : null,
      panelTop: panel ? Math.round(panel.getBoundingClientRect().top * 10) / 10 : null,
      buttonBottom: button ? Math.round(button.getBoundingClientRect().bottom * 10) / 10 : null,
      zoom: document.body.style.zoom || "1",
      viewport: { w: window.innerWidth, h: window.innerHeight },
      scrollTop: document.querySelector(".stream")?.scrollTop ?? -1,
    };
  });
}

/**
 * 等锚点收敛：连续两次读到的内联 style 一致才继续。
 * 为什么需要：lb-05 改过输入框高度、lb-06 改过视口、lb-07 改 zoom 之后，锚点可能还在按新边界重算；
 * 不等它稳定就测量会量到中间值（Lead 复刻不出来的那个 25.1px 就是这么来的）。
 */
async function waitAnchorSettled(page, { tries = 20, interval = 100 } = {}) {
  let previous = null;
  for (let i = 0; i < tries; i += 1) {
    const now = await anchorSnapshot(page);
    if (previous && now.anchorStyle === previous.anchorStyle) return now;
    previous = now;
    await sleep(interval);
  }
  return await anchorSnapshot(page);
}

async function scrollToBottom(page) {
  await page.evaluate(() => {
    const el = document.querySelector(".stream");
    if (el) el.scrollTop = el.scrollHeight;
  });
  await sleep(500);
}
async function latestMessageVisible(page) {
  return await page.evaluate(() => {
    const host = document.querySelector(".stream");
    if (!host) return { ok: false, reason: "找不到 .stream" };
    const bubbles = [...host.querySelectorAll("[class*='msg'], [class*='message'], [class*='bubble']")];
    const last = bubbles[bubbles.length - 1];
    if (!last) return { ok: false, reason: "没有消息元素" };
    const r = last.getBoundingClientRect();
    const h = host.getBoundingClientRect();
    return {
      ok: r.bottom <= h.bottom + 1 && r.top >= h.top - 1,
      top: Math.round(r.top),
      bottom: Math.round(r.bottom),
      hostBottom: Math.round(h.bottom),
    };
  });
}

async function main() {
  mkdirSync(SHOT_DIR, { recursive: true });
  const headed = process.env.QIO_HEADED !== "0";
  const browser = await launchBrowser({ headless: !headed });
  const session = await createSession(browser, {
    group: GROUP,
    name: "回到最新消息按钮几何",
    theme: "dark",
    viewport: { width: 1440, height: 900 },
    base: BASE,
    api: API,
  });
  const page = session.page;
  try {
    await session.goto("#/", { waitFor: ".conversation", settle: 900 });

    for (let i = 0; i < 14; i += 1) {
      const turnId = `latest_btn_${i}`;
      const content = `第 ${i + 1} 段用来撑高对话的正文。`.repeat(14);
      await session.inject("TURN_START", { turn_id: turnId, revision: 8100 + i });
      await session.inject("ASSISTANT", { content });
      await session.inject("TURN_END", {
        turn_id: turnId,
        status: "completed",
        revision: 8200 + i,
        final_content: content,
      });
      await sleep(120);
    }
    await sleep(700);

    await check("lb-01-按钮只在上翻阅读时出现（固定定位浮层）", "跟随底部时不显示；上翻后出现、在视口内、外层 .latest-anchor 是 position:fixed", async () => {
      // 先确认「跟随底部时按设计不显示」（按钮是给上翻阅读用的）
      const followingCount = await page.evaluate(
        () => document.querySelectorAll(".back-latest").length,
      );
      assert(followingCount === 0, `跟随底部时按钮不该出现，实际有 ${followingCount} 个`);
      // 再上翻：这时才应该出现（lb-01 之前在这里直接等按钮 → 15s 超时，是脚本前置条件错）
      await scrollUp(page);
      await page.waitForSelector(".back-latest", { state: "visible", timeout: 15000 });
      await waitAnchorSettled(page);
      const m = await measure(page);
      assert(m.button, "找不到 .back-latest");
      assert(m.anchorPresent, "找不到外层 .latest-anchor");
      assert(m.buttonInAnchor, "按钮不在 .latest-anchor 里");
      assert(m.anchorPosition === "fixed", `.latest-anchor 的 position=${m.anchorPosition}（要求 fixed）`);
      assert(m.buttonInViewport, "按钮不在视口内");
      return `跟随底部时不显示 ✓；上翻后出现（锚点 position=fixed，按钮 ${Math.round(m.button.width)}×${Math.round(m.button.height)}，在视口内）`;
    });

    await check("lb-02-宽窗口间距 8–12px（到输入区面板上缘）", "1440×900：按钮下缘距输入区面板 .composer 上缘 8–12px", async () => {
      await scrollUp(page);
      await waitAnchorSettled(page);
      const m = await measure(page);
      assert(m.button && m.panel, "按钮或输入区面板找不到");
      assert(m.gapPanel >= 8 && m.gapPanel <= 12, `到面板上缘实测 ${m.gapPanel}px（要求 8–12）`);
      return (
        `到面板上缘 gap=${m.gapPanel}px（按钮 bottom=${Math.round(m.button.bottom)}，面板 top=${Math.round(m.panel.top)}）；` +
        `参考：到 textarea 本体 ${m.gapTextarea}px —— 面板顶部 .topicbar 那行有可交互的「取消」按钮，` +
        `按钮不能贴到 textarea，否则会落在那一行里被挡住`
      );
    });
    await session.shot("lb-01-wide", "宽窗口：按钮与输入区面板");

    await check("lb-03-命中测试", "按钮中心 elementFromPoint 命中的就是按钮本身", async () => {
      const m = await measure(page);
      assert(m.hitIsButton, `命中 ${m.hitTag}，不是按钮`);
      return `命中 ${m.hitTag}`;
    });

    await check("lb-04-水平对齐内容列中心", "按钮中心与对话内容列中心对齐（±12px）", async () => {
      const m = await measure(page);
      assert(m.columnCenter !== null, "找不到 .stream");
      const delta = Math.abs(m.columnCenter - m.buttonCenter);
      assert(delta <= 12, `偏差 ${delta}px（列中心 ${m.columnCenter}，按钮中心 ${m.buttonCenter}）`);
      return `偏差 ${Math.round(delta * 10) / 10}px`;
    });

    await check("lb-05-多行输入后仍成立", "输入框变成多行后，到面板的间距与命中仍成立", async () => {
      await page.locator("textarea").first().fill("这是一段很长的输入，用来把输入框撑成多行。".repeat(5));
      await sleep(500);
      await waitAnchorSettled(page);
      const m = await measure(page);
      assert(m.gapPanel >= 8 && m.gapPanel <= 12, `多行后到面板上缘 ${m.gapPanel}px`);
      assert(m.hitIsButton, `多行后被 ${m.hitTag} 盖住`);
      return `gap=${m.gapPanel}px（到 textarea ${m.gapTextarea}px），面板高 ${Math.round(m.panel.height)}px`;
    });
    await session.shot("lb-02-multiline", "多行输入后");
    await page.locator("textarea").first().fill("");
    await sleep(400);

    await check("lb-06-窄窗口仍成立", "820×900：到面板的间距与命中仍成立", async () => {
      await page.setViewportSize({ width: 820, height: 900 });
      await sleep(600);
      await scrollUp(page);
      await waitAnchorSettled(page);
      const m = await measure(page);
      assert(m.button && m.panel, "窄窗口里按钮或输入区面板找不到");
      assert(m.gapPanel >= 8 && m.gapPanel <= 12, `窄窗口到面板上缘 ${m.gapPanel}px`);
      assert(m.hitIsButton, `窄窗口被 ${m.hitTag} 盖住`);
      return `gap=${m.gapPanel}px（到 textarea ${m.gapTextarea}px），视口 ${m.viewport.w}×${m.viewport.h}`;
    });
    await session.shot("lb-03-narrow", "窄窗口");
    await page.setViewportSize({ width: 1440, height: 900 });
    await sleep(500);

    await check("lb-07-缩放后仍成立", "页面缩放到 80% / 125% 后到面板的间距仍成立", async () => {
      // 进 lb-07 前显式复位前序步骤留下的状态（lb-05 多行输入、lb-06 改过视口）
      await page.locator("textarea").first().fill("");
      await page.locator("textarea").first().blur().catch(() => {});
      await page.setViewportSize({ width: 1440, height: 900 });
      await page.evaluate(() => {
        document.body.style.zoom = "1";
      });
      await sleep(600);

      const out = [];
      for (const zoom of [0.8, 1.25]) {
        await page.evaluate((z) => {
          document.body.style.zoom = String(z);
        }, zoom);
        await sleep(600);
        await scrollUp(page);
        const settled = await waitAnchorSettled(page); // 等锚点按新缩放重算稳定
        const m = await measure(page);
        if (!(m.button && m.panel) || !(m.gapPanel >= 8 && m.gapPanel <= 12)) {
          throw new Error(
            `zoom=${zoom} 时间距不成立：gapPanel=${m.gapPanel}px；诊断：` +
              `anchor.style="${settled.anchorStyle}"，anchorBottom=${settled.anchorBottom}，` +
              `panelTop=${settled.panelTop}，buttonBottom=${settled.buttonBottom}，` +
              `zoom=${settled.zoom}，视口=${JSON.stringify(settled.viewport)}，scrollTop=${settled.scrollTop}，` +
              `到 textarea=${m.gapTextarea}px，命中=${m.hitTag}`,
          );
        }
        out.push(`zoom=${zoom}:${m.gapPanel}px`);
      }
      await page.evaluate(() => {
        document.body.style.zoom = "1";
      });
      await sleep(300);
      return out.join("，");
    });

    await check("lb-08-最新消息完整可读", "点按钮回到最新后，最后一条消息完整可见", async () => {
      // 先显式复位前序步骤留下的状态（lb-06 改过视口、lb-07 改过 zoom），
      // 并等按钮真的可见再点 —— 否则失败原因会混进「按钮没出现」这种无关因素。
      await page.setViewportSize({ width: 1440, height: 900 });
      await page.evaluate(() => {
        document.body.style.zoom = "1";
      });
      await sleep(500);
      await scrollUp(page);
      try {
        await page.waitForSelector(".back-latest", { state: "visible", timeout: 10000 });
      } catch (e) {
        const m = await measure(page);
        throw new Error(
          `上翻后按钮没有出现/不可见：${String(e?.message ?? e)}；` +
            `按钮=${JSON.stringify(m.button)}，scrollTop=${await streamScrollTop(page)}，` +
            `命中=${m.hitTag}，zoom=${m.zoom}，视口=${JSON.stringify(m.viewport)}`,
        );
      }
      try {
        await page.click(".back-latest", { timeout: 8000 });
      } catch (e) {
        const m = await measure(page);
        throw new Error(
          `点击失败：${String(e?.message ?? e)}；按钮=${JSON.stringify(m.button)}，` +
            `scrollTop=${await streamScrollTop(page)}，中心命中=${m.hitTag}，zoom=${m.zoom}`,
        );
      }
      await sleep(800);
      const v = await latestMessageVisible(page);
      assert(v.ok, `最后一条消息不完整：${JSON.stringify(v)}`);
      return `top=${v.top} bottom=${v.bottom} 容器底=${v.hostBottom}`;
    });

    await check("lb-09-上翻不自动回底", "用户上翻后等待 1.5 秒，位置没有被自动拉回底部", async () => {
      await scrollToBottom(page);
      await scrollUp(page);
      const after = await streamScrollTop(page);
      await sleep(1500);
      const settled = await streamScrollTop(page);
      assert(settled <= after + 5, `位置被拉回：${after} → ${settled}`);
      return `上翻后 ${after} → 等待后 ${settled}`;
    });

    await check("lb-10-流式更新不重启被打断的滚动", "上翻状态下注入流式内容，位置不被拉到最新", async () => {
      await scrollUp(page);
      const before = await streamScrollTop(page);
      await session.inject("TURN_START", { turn_id: "latest_btn_stream", revision: 9100 });
      await session.inject("ASSISTANT", { content: "流式内容。".repeat(120) });
      await sleep(900);
      const after = await streamScrollTop(page);
      assert(after <= before + 40, `流式更新把位置拉到 ${after}（上翻位置 ${before}）`);
      await session.inject("TURN_END", {
        turn_id: "latest_btn_stream",
        status: "completed",
        revision: 9200,
        final_content: "流式内容。".repeat(120),
      });
      return `上翻 ${before} → 流式后 ${after}`;
    });

    await check("lb-11-输入框与发送按钮可操作", "输入框可输入、发送按钮可用（不真的发送）", async () => {
      const box = page.locator("textarea").first();
      await box.fill("可操作性检查（不会真的发送）");
      assert((await box.inputValue()).includes("可操作性检查"), "输入没有进输入框");
      const send = page.locator("button[type='submit'], .send, [aria-label*='发送']").first();
      const enabled = await send.isEnabled().catch(() => false);
      await box.fill("");
      assert(enabled, "发送按钮不可用");
      return "输入框可写、发送按钮可用";
    });

    await session.shot("lb-04-final", "收尾状态");
  } finally {
    await session.close();
    await browser.close();
  }

  const failed = results.filter((r) => r.outcome === "FAIL").length;
  writeFileSync(
    CHECKS_FILE,
    JSON.stringify(
      { group: GROUP, updatedAt: new Date().toISOString(), total: results.length, failed, checks: results },
      null,
      2,
    ),
    "utf-8",
  );
  console.log(`\n本轮 ${results.length} 条，FAIL ${failed} 条 → ${CHECKS_FILE}`);
  if (failed > 0) process.exitCode = 1;
}

main().catch((e) => {
  console.error("[失败]", e);
  process.exitCode = 1;
});

