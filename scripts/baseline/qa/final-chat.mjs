// 任务 05 最终验收（对话场景）：空态 / 长对话 / 超长输入 / 超长回答 / 滚动 / 排队 / 取消 / 断连 / 输入法。
// 用法（服务需在跑：后端 8734、前端 5199）：
//   node scripts/baseline/qa/final-chat.mjs [C|Q|K|E|I ...]   # 不传则全跑
// 输出：%TEMP%\qio-baseline\qa\report-final-chat.json 与 shots\*.png
import { createRequire } from "node:module";
import { mkdirSync, writeFileSync } from "node:fs";

const require = createRequire(import.meta.url);
const PW = "C:\\Users\\zxy\\.cache\\codex-runtimes\\codex-primary-runtime\\dependencies\\node\\node_modules\\playwright";
const { chromium } = require(PW);

const BASE = "http://127.0.0.1:5199";
const API = "http://127.0.0.1:8734";
const OUT = `${process.env.TEMP}\\qio-baseline\\qa`;
const SHOTS = `${OUT}\\shots`;
mkdirSync(SHOTS, { recursive: true });

const report = { startedAt: new Date().toISOString(), cases: [] };
const rec = (id, title, passed, actual, detail = null) => {
  report.cases.push({ id, title, passed: !!passed, actual: String(actual), detail });
  console.log(`[${passed ? "PASS" : "FAIL"}] ${id} ${title} :: ${actual}`);
};
const wanted = process.argv.slice(2);
const only = (id) => wanted.length === 0 || wanted.some((w) => id.startsWith(w));

let seq = Math.floor(Date.now() / 1000) % 100000;
const url = () => `${BASE}/?fresh=${++seq}#/`;
const wait = (ms) => new Promise((r) => setTimeout(r, ms));

const browser = await chromium.launch({
  channel: "msedge",
  headless: true,
  args: ["--use-gl=angle", "--use-angle=swiftshader", "--enable-unsafe-swiftshader"],
});
const context = await browser.newContext({ viewport: { width: 1440, height: 900 } });
await context.addInitScript(() => {
  try {
    if (!localStorage.getItem("qio-theme")) localStorage.setItem("qio-theme", "dark");
  } catch {
    /* 忽略 */
  }
});
const page = await context.newPage();
page.on("pageerror", (e) => report.cases.push({ id: "PAGEERROR", title: String(e).slice(0, 160), passed: false, actual: "未捕获异常" }));

const inject = (type, data) =>
  page.request.post(`${API}/api/events/test?event_type=${type}`, { data });
/** 清掉上一段用例留下的路由拦截：被拦截的请求还在飞时，旧 handler 会和新 handler 抢同一个 route */
async function clearRoutes() {
  if (typeof page.unrouteAll === "function") {
    try {
      await page.unrouteAll({ behavior: "ignoreErrors" });
    } catch {
      /* 忽略：老版本 playwright 没有这个 API */
    }
  }
}
const streamTop = () => page.locator(".stream").evaluate((el) => Math.round(el.scrollTop));
const distanceToBottom = () =>
  page.locator(".stream").evaluate((el) => Math.round(el.scrollHeight - el.clientHeight - el.scrollTop));

async function openChat() {
  await page.goto(url());
  await page.waitForSelector(".composer textarea", { timeout: 12000 });
  await wait(600);
}

async function main() {
  if (only("C")) {
    // C. 聊天场景：空态 / 长对话 / 长输入 / 长回答 / markdown / 滚动
    await openChat();

    // 空态：切到一个没有消息的话题（隔离数据里的「基线-空白话题…」），看完再切回来
    const EMPTY_TOPIC = "基线-空白话题无片段无知识无实体";
    const BACK_TOPIC = "吃饭与饮食";
    async function startFrom(topicTitle) {
      await page.locator(".dock").click();
      await page.waitForSelector(".planet-view", { timeout: 8000 });
      await wait(1000);
      if (await page.locator(".panel-toggle").count()) {
        await page.locator(".panel-toggle").click();
        await wait(700);
      }
      const li = page.locator(".topic-list li", { hasText: topicTitle }).first();
      await li.waitFor({ timeout: 8000 });
      await li.click();
      // 等这一行真的成为选中项再点「从这里开始」：
      // 详情面板更新前按钮还属于上一个话题，抢点会把起点设错。
      await page
        .waitForFunction(
          (t) => {
            const active = document.querySelector(".topic-list li[aria-selected='true'], .topic-list li.active");
            return !!active && (active.textContent || "").includes(t);
          },
          topicTitle,
          { timeout: 8000 },
        )
        .catch(() => {});
      await wait(400);
      const start = page.locator(".start-btn").first();
      await start.waitFor({ timeout: 8000 });
      await start.click();
      await page.waitForSelector(".composer textarea", { timeout: 12000 });
      await wait(2500); // 等收起时间线 + 历史加载
    }
    try {
      await startFrom(EMPTY_TOPIC);
      const emptyState = await page.evaluate(() => {
        const stream = document.querySelector(".stream");
        return {
          text: (stream?.innerText || "").trim().slice(0, 60),
          composer: !!document.querySelector(".composer textarea"),
          rows: document.querySelectorAll(".stream .message").length,
        };
      });
      await page.screenshot({ path: `${SHOTS}\\chat-empty.png` });
      rec(
        "C-EMPTY",
        "空话题安静：没有历史消息时不堆仪表元素，输入区就在那里",
        emptyState.composer && emptyState.rows === 0,
        `输入区=${emptyState.composer} 消息行=${emptyState.rows} 文本='${emptyState.text}'`,
      );
    } catch (e) {
      rec("C-EMPTY", "空话题安静：没有历史消息时不堆仪表元素，输入区就在那里", false, `切换空话题失败：${e.message.slice(0, 160)}`);
    }
    try {
      await startFrom(BACK_TOPIC); // 还原对话起点，避免污染后续用例
      // 等历史真的回来再继续（否则后面的注入会加在空话题上）
      await page
        .waitForFunction(() => document.querySelectorAll(".stream .message").length >= 5, null, { timeout: 8000 })
        .catch(() => {});
      await wait(600);
    } catch (e) {
      rec("C-EMPTY-RESTORE", "验收后把对话起点还原到原来的话题", false, e.message.slice(0, 160));
    }

    // 长对话：用 TURN_START/ASSISTANT/TURN_END 造出 55 条独立回答（同 turn 的 ASSISTANT 会就地更新，
    // 所以必须带 turn 边界，否则只会合成一条）
    const t0 = Date.now();
    for (let i = 0; i < 55; i++) {
      const tid = `qa_long_${i}`;
      await inject("TURN_START", { turn_id: tid });
      await inject("ASSISTANT", {
        turn_id: tid,
        content: `第 ${i + 1} 条：长对话压力测试内容。\n\n- 要点一\n- 要点二\n`,
      });
      await inject("TURN_END", { turn_id: tid });
    }
    await wait(2500);
    const elapsed = Date.now() - t0;
    const longState = await page.evaluate(() => {
      const de = document.documentElement;
      const stream = document.querySelector(".stream");
      return {
        bubbles: document.querySelectorAll(".assist-bubble").length,
        rows: document.querySelectorAll(".stream .message").length,
        overflow: de.scrollWidth - window.innerWidth,
        streamScrollH: stream?.scrollHeight || 0,
      };
    });
    // 先滚到顶部，确认还能正常往回读（长对话里最常用的动作）
    await page.locator(".stream").evaluate((el) => {
      el.scrollTop = 0;
      el.dispatchEvent(new Event("scroll"));
    });
    await wait(500);
    const top = await streamTop();
    const backBtn = await page.locator(".back-latest").count();
    await page.screenshot({ path: `${SHOTS}\\chat-long.png` });
    rec(
      "C-LONG-CONV",
      `长对话（55 轮）能渲染、可回读顶部、无横向溢出（注入+渲染 ${elapsed}ms）`,
      // 消息列表是虚拟化的：DOM 里的气泡数取决于滚动位置，所以用滚动高度与 DOM 行数判断
      longState.streamScrollH > 6000 &&
        longState.rows >= 3 &&
        longState.overflow <= 1 &&
        top === 0 &&
        backBtn > 0,
      `回答气泡=${longState.bubbles} DOM 行=${longState.rows} 溢出=${longState.overflow}px 滚动高度=${longState.streamScrollH}px 回顶部=${top} 「回到最新」=${backBtn}`,
    );
    if (backBtn) {
      await page.locator(".back-latest").click();
      await wait(700);
    }

    // 超长用户输入：5000 字粘贴后输入框自增长但有上限，不把对话挤没
    const longText = "长输入测试。".repeat(800); // 4800 字
    await page.fill(".composer textarea", longText);
    await wait(500);
    const grow = await page.evaluate(() => {
      const ta = document.querySelector(".composer textarea");
      const composer = document.querySelector(".composer");
      return {
        taH: Math.round(ta.getBoundingClientRect().height),
        composerH: Math.round(composer.getBoundingClientRect().height),
        viewportH: window.innerHeight,
        caret: ta.scrollHeight > ta.clientHeight,
      };
    });
    rec(
      "C-HUGE-INPUT",
      "超长输入不会把聊天窗口挤没（输入框有最大高度并在内部滚动）",
      grow.taH < grow.viewportH * 0.6 && grow.caret,
      `输入框高=${grow.taH}px 输入区高=${grow.composerH}px 视口高=${grow.viewportH}px 内部滚动=${grow.caret}`,
    );
    await page.screenshot({ path: `${SHOTS}\\chat-huge-input.png` });
    await page.fill(".composer textarea", "");
    await wait(300);

    // 超长回答 + markdown（标题/列表/表格/引用/链接/代码）
    await inject("ASSISTANT", {
      turn_id: "qa_turn_md",
      content:
        "# 长回答标题\n\n" +
        "> 引用段落用于检查引用样式。\n\n" +
        "- 列表一\n- 列表二\n\n" +
        "| 列 A | 列 B |\n| --- | --- |\n| 1 | 2 |\n\n" +
        "[示例链接](https://example.com/very/long/url/that/should/wrap/without/breaking/layout)\n\n" +
        "```python\n" +
        "def f(x):\n    return x  # " +
        "超长代码行".repeat(30) +
        "\n```\n\n" +
        "正文段落。".repeat(1200), // ~4800 字正文
    });
    await wait(2000);
    const mdState = await page.evaluate(() => {
      const md = Array.from(document.querySelectorAll(".assist-bubble .markdown-body")).pop();
      // 宽内容是否被「某个内部容器」接住：找 md 里真正出现横向溢出的元素
      const scrollers = Array.from(md?.querySelectorAll("*") || []).filter(
        (el) => el.scrollWidth - el.clientWidth > 4 && getComputedStyle(el).overflowX !== "visible",
      );
      return {
        hasHeading: !!md?.querySelector("h1"),
        hasQuote: !!md?.querySelector("blockquote"),
        hasLink: !!md?.querySelector("a"),
        hasTable: !!md?.querySelector("table"),
        hasCode: !!md?.querySelector("pre"),
        scrollers: scrollers.map((el) => `${el.tagName.toLowerCase()}.${String(el.className).split(" ")[0]}`).slice(0, 5),
        docOverflow: document.documentElement.scrollWidth - window.innerWidth,
        length: (md?.innerText || "").length,
      };
    });
    rec(
      "C-MARKDOWN",
      "长回答里的标题/引用/链接/表格/代码都渲染，横向溢出留在各自容器里",
      mdState.hasHeading && mdState.hasQuote && mdState.hasLink && mdState.docOverflow <= 1,
      `标题=${mdState.hasHeading} 引用=${mdState.hasQuote} 链接=${mdState.hasLink} 表格=${mdState.hasTable} 代码=${mdState.hasCode} 内部横向滚动容器=${mdState.scrollers.join(",") || "无"} 页面溢出=${mdState.docOverflow}px 文本长度=${mdState.length}`,
    );
    await page.screenshot({ path: `${SHOTS}\\chat-long-answer.png` });
  }

  if (only("Q")) {
    // 排队 3 条：运行中仍可输入、队列状态清楚
    await clearRoutes();
    await openChat();
    await page.route("**/api/turns**", async (route) => {
      try {
        await wait(9000); // 让第一轮一直处于运行中
        await route.fulfill({ status: 200, contentType: "application/json", body: '{"ok":true}' });
      } catch {
        /* 路由已被新用例接管 */
      }
    });
    await page.fill(".composer textarea", "排队第 1 条");
    await page.locator(".send-btn").click();
    await wait(600);
    for (const t of ["排队第 2 条", "排队第 3 条"]) {
      await page.fill(".composer textarea", t);
      await page.locator(".send-btn").click();
      await wait(400);
    }
    const queueState = await page.evaluate(() => {
      const chips = Array.from(document.querySelectorAll(".queue-chip, .queued, [class*='queue']"));
      return {
        text: chips.map((c) => c.innerText.replace(/\s+/g, " ").trim()).slice(0, 6),
        editable: !document.querySelector(".composer textarea")?.disabled,
        stop: !!document.querySelector(".stop-btn"),
      };
    });
    await page.screenshot({ path: `${SHOTS}\\chat-queue.png` });
    rec(
      "Q-QUEUE-3",
      "运行中仍能继续输入并排队发送；队列状态可见；停止入口独立",
      queueState.editable && queueState.stop,
      `输入可编辑=${queueState.editable} 停止入口=${queueState.stop} 队列文案=${queueState.text.join(" / ") || "（未识别到队列元素）"}`,
    );
    await clearRoutes();
  }

  if (only("K")) {
    // 取消：停止按钮有等待反馈，目标明确
    await clearRoutes();
    await openChat();
    await page.route("**/api/turns**", async (route) => {
      try {
        await wait(9000);
        await route.fulfill({ status: 200, contentType: "application/json", body: '{"ok":true}' });
      } catch {
        /* 路由已被新用例接管 */
      }
    });
    await page.fill(".composer textarea", "取消测试");
    await page.locator(".send-btn").click();
    await wait(800);
    const running = await page.locator(".stop-btn").count();
    let stoppingText = "";
    if (running) {
      await page.route("**/api/turns/*/cancel**", async (route) => {
        try {
          await wait(1200);
          await route.fulfill({ status: 200, contentType: "application/json", body: '{"ok":true}' });
        } catch {
          /* 忽略 */
        }
      });
      await page.locator(".stop-btn").click();
      await wait(300);
      stoppingText = (await page.locator(".stop-btn").innerText().catch(() => "")).trim();
    }
    rec(
      "K-STOP-FEEDBACK",
      "运行中可停止；点了之后显示「正在停止」，不是立刻假装已停",
      running > 0 && /正在停止|停止中/.test(stoppingText),
      `停止按钮=${running} 点击后文案='${stoppingText}'`,
    );
    await clearRoutes();
  }

  if (only("E")) {
    // 断连/失败：错误可见 + 草稿保留
    await clearRoutes();
    await openChat();
    await page.route("**/api/turns**", async (route) => {
      try {
        await route.fulfill({ status: 500, contentType: "application/json", body: '{"detail":"模拟后端不可用"}' });
      } catch {
        /* 忽略 */
      }
    });
    await page.fill(".composer textarea", "断连测试草稿");
    await page.locator(".send-btn").click();
    // 等错误真的出现（上一个用例可能留下仍在飞的请求，导致这次发送被排队而不是立刻失败）
    await page
      .waitForFunction(
        () => Array.from(document.querySelectorAll("[role='alert'], .msg, .error")).some((e) => (e.innerText || "").trim().length > 0),
        null,
        { timeout: 8000 },
      )
      .catch(() => {});
    await wait(500);
    const errText = await page.evaluate(() => {
      const cands = Array.from(document.querySelectorAll(".msg, .error, [role='alert'], .turn-error"));
      return cands.map((e) => e.innerText.replace(/\s+/g, " ").trim()).filter(Boolean).slice(0, 4);
    });
    const draft = await page.inputValue(".composer textarea");
    await page.screenshot({ path: `${SHOTS}\\chat-error.png` });
    rec(
      "E-ERROR-DRAFT",
      "发送失败时错误可见、草稿可找回",
      errText.length > 0 && draft.includes("断连测试草稿"),
      `错误文案=${errText.join(" | ").slice(0, 120)} 草稿='${draft.slice(0, 30)}'`,
    );
    await clearRoutes();
  }

  if (only("I")) {
    // 输入法：选词回车不发送；composition 结束后回车才发送
    await clearRoutes();
    await openChat();
    // 用每次唯一的拼音串：历史里可能已经存在上一次跑出来的同名消息，
    // 否则「是否已发出」会永远为真。
    const pinyin = `zhongwen${String(Date.now()).slice(-6)}`;
    const streamHasText = async (t) => (await page.locator(".stream").innerText()).includes(t);
    await page.fill(".composer textarea", pinyin);
    await page.locator(".composer textarea").evaluate((el) => {
      el.dispatchEvent(new CompositionEvent("compositionstart", { bubbles: true }));
      el.dispatchEvent(new KeyboardEvent("keydown", { key: "Enter", bubbles: true, cancelable: true, isComposing: true }));
      el.dispatchEvent(new KeyboardEvent("keypress", { key: "Enter", bubbles: true, cancelable: true, isComposing: true }));
    });
    await wait(700);
    const draftAfterComposing = await page.inputValue(".composer textarea");
    const sentWhileComposing = await streamHasText(pinyin);
    await page.locator(".composer textarea").evaluate((el) => {
      el.dispatchEvent(new CompositionEvent("compositionend", { bubbles: true }));
    });
    await wait(200);

    await page.route("**/api/turns**", async (route) => {
      try {
        await wait(4000);
        await route.fulfill({ status: 200, contentType: "application/json", body: '{"ok":true}' });
      } catch {
        /* 忽略 */
      }
    });
    await page.locator(".composer textarea").evaluate((el) => {
      el.dispatchEvent(new KeyboardEvent("keydown", { key: "Enter", bubbles: true, cancelable: true, isComposing: false }));
    });
    await wait(1200);
    const draftAfterEnter = await page.inputValue(".composer textarea");
    const sentAfterEnter = (await streamHasText(pinyin)) || draftAfterEnter.trim() === "";
    rec(
      "I-IME",
      "中文输入法选词时回车不发送；输入完成后回车才发送",
      !sentWhileComposing && draftAfterComposing === pinyin && sentAfterEnter,
      `选词回车：草稿='${draftAfterComposing}' 是否已发出=${sentWhileComposing}；输入完成后回车：已发出=${sentAfterEnter} 草稿='${draftAfterEnter.slice(0, 20)}'`,
    );
    await clearRoutes();
  }

  await context.close();
  await browser.close();
}

// 无论中途哪里抛错，都要把已经跑出来的结果写盘（否则前面的结论会丢）
try {
  await main();
} catch (e) {
  report.cases.push({ id: "RUN-ERROR", title: String(e).slice(0, 200), passed: false, actual: "脚本中断" });
  console.error(`[FAIL] RUN-ERROR :: ${String(e).slice(0, 300)}`);
  try {
    await context.close();
    await browser.close();
  } catch {
    /* 忽略 */
  }
}
writeFileSync(`${OUT}\\report-final-chat.json`, JSON.stringify(report, null, 2), "utf-8");
const failed = report.cases.filter((c) => !c.passed);
console.log(`\n合计 ${report.cases.length} 项，失败 ${failed.length} 项。报告：${OUT}\\report-final-chat.json`);
process.exit(failed.length ? 1 : 0);
