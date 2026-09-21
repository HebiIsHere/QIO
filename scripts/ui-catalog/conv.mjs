/**
 * conv 分组：对话页的穷举采集。
 *
 * 用主实例（前端 5199 / 后端 8734，已播种基线数据）：
 * 对话页的历史消息只能来自数据库（不能靠注入伪造用户消息），
 * 运行态、卡片、审批这些则用 `/api/events/test` 注入口造出来。
 *
 * 用法：node scripts/ui-catalog/conv.mjs
 * 产物：frontend/e2e-shots/ui-catalog/conv/*.png 与 manifest-conv.json
 */
import {
  createSession,
  injectEvent,
  launchBrowser,
  runGroup,
  sleep,
  apiRequest,
  readManifest,
} from "./lib.mjs";

const HINT_MAIN = "前端稳定化长对话与阅读连续性";
const HINT_EMPTY = "空白话题无片段无知识无实体";
const HINT_RICH = "超长标题";

const failures = [];
const findings = [];
const ids = { main: "", empty: "", rich: "" };

/** Markdown 全覆盖样本：每种元素都出现一次 */
const MARKDOWN_ALL = [
  "## 三种反馈各自的边界",
  "",
  "**结论**：能用文字说清的，就不要用动画；*动效只回答「发生了什么」*，不承担内容。",
  "",
  "- 反馈要立刻出现：`发送` 之后马上有回执",
  "- 状态要一直可辨：等待 / 生成 / 工具 / 审批",
  "- 变化尽量少：切页不重排已经读到的内容",
  "",
  "1. 先确认操作有没有被接住",
  "2. 再确认内容有没有被弄丢",
  "3. 最后才考虑动画是否优雅",
  "",
  "- [x] 发送之后立刻出现回执",
  "- [ ] 审批失败时必须显示失败",
  "- [ ] 复制失败时不能显示成功",
  "",
  "> 引用块用来检验左边界、缩进与行距是否与正文区分得开。",
  "",
  "链接示例：[视觉语言规范](https://example.com/spec) 与行内代码 `resolve(anchor)`。",
  "",
  "---",
  "",
  "| 状态 | 触发 | 界面表达 |",
  "| --- | --- | --- |",
  "| 等待 | TURN_START | 三圆点 + 正在处理 |",
  "| 生成 | ASSISTANT | 打字机正文 + 过程标记 |",
  "| 工具 | TOOL_START | 运行中的工具卡 |",
  "| 审批 | APPROVAL_REQUIRED | 等待你确认 |",
  "",
  "```ts",
  "export function label(activity: string): string {",
  String.raw`  return ACTIVITY_LABELS[activity] ?? "";`,
  "}",
  "```",
].join("\n");

async function scene(label, fn) {
  const t0 = Date.now();
  try {
    await fn();
    console.log(`\n[场景完成] ${label} · ${((Date.now() - t0) / 1000).toFixed(1)}s`);
  } catch (e) {
    const msg = String(e?.message ?? e).slice(0, 300);
    failures.push(`${label}: ${msg}`);
    console.log(`\n[场景失败] ${label}: ${msg}`);
  }
}

async function loadTopics() {
  const res = await apiRequest("GET", "/api/graph/topics");
  const list = res.topics ?? [];
  const pick = (hint) => list.find((t) => String(t.title ?? "").includes(hint))?.topic_id ?? "";
  ids.main = pick(HINT_MAIN);
  ids.empty = pick(HINT_EMPTY);
  ids.rich = pick(HINT_RICH);
  if (!ids.main) throw new Error("找不到基线主话题：主实例是否已经播种？");
  console.log(
    `话题：main=${ids.main} empty=${ids.empty || "(无)"} rich=${ids.rich || "(无)"}（共 ${list.length} 个）`,
  );
}

/** 切锚点（后端广播 ANCHOR，页面会重载历史） */
async function anchor(topicId, extra = {}) {
  await apiRequest("POST", "/api/anchor", { topic_id: topicId, ...extra });
  await sleep(500);
}

/**
 * 清空事件总线的回放缓冲。
 *
 * 后端 SSE 总线保留了最近 50 条事件，**新订阅者（新开的页面/新标签页）在没有游标时
 * 会把这些事件全部重放一遍**（见 `backend/src/agent/api/bus.py::_replay`，只过滤了
 * APPROVAL_REQUIRED）。于是上一次采集注入的 WARNING / TURN_END / TURN_QUEUE /
 * KNOWLEDGE_CANDIDATE 会在新页面里「复活」，把空状态拍成带错误条与候选卡的样子。
 * 每次开会话前先灌 55 条无副作用的 CAPABILITY 事件，把旧事件挤出缓冲。
 */
async function flushBus(rounds = 5, size = 11) {
  for (let r = 0; r < rounds; r += 1) {
    await Promise.all(
      Array.from({ length: size }, () => injectEvent("CAPABILITY", { adapter: "native" })),
    );
  }
}

async function newSession(browser, opts = {}) {
  await flushBus();
  const s = await createSession(browser, { group: "conv", ...opts });
  // 复制按钮要真的能写剪贴板，否则只能拍到「复制失败」
  await s.context.grantPermissions(["clipboard-read", "clipboard-write"]).catch(() => {});
  // 单张图失败不该拖垮整段采集：记下来继续，最后统一汇报
  const rawShot = s.shot.bind(s);
  const rawShotEl = s.shotEl.bind(s);
  s.shot = async (...args) => {
    try {
      return await rawShot(...args);
    } catch (e) {
      failures.push(`截图 ${args[0]}：${String(e?.message ?? e).slice(0, 160)}`);
      console.log(`  [跳过] ${args[0]}：${String(e?.message ?? e).slice(0, 120)}`);
      return null;
    }
  };
  s.shotEl = async (...args) => {
    try {
      return await rawShotEl(...args);
    } catch (e) {
      failures.push(`截图 ${args[1]}：${String(e?.message ?? e).slice(0, 160)}`);
      console.log(`  [跳过] ${args[1]}：${String(e?.message ?? e).slice(0, 120)}`);
      return null;
    }
  };
  return s;
}

/** 滚到消息流底部（后续注入的新内容才在视口里） */
async function bottom(s) {
  await s.page.locator(".stream").evaluate((el) => {
    el.scrollTop = el.scrollHeight;
  });
  await sleep(320);
}

async function toTop(s) {
  await s.page.locator(".stream").evaluate((el) => {
    el.scrollTop = 0;
  });
  await sleep(400);
}

async function openConv(s) {
  await s.goto("#/", { waitFor: ".conversation", settle: 700 });
  await sleep(600);
}

/**
 * 点一个元素：常规点击被遮挡时退回 `dispatchEvent`。
 *
 * 对话页底部的三个提示块（知识候选 `.candidate`、兼容模式 `.notice.fallback`、
 * 话题切换 `.topic-switch`）会被 **fixed 的输入区气泡**盖住（见汇报里的布局问题），
 * Playwright 的可点性检查会一直等下去。为了把状态本身拍出来，这里退回事件派发。
 */
async function safeClick(s, selector) {
  const loc = s.page.locator(selector).first();
  try {
    await loc.click({ timeout: 4000 });
    return "click";
  } catch {
    await loc.dispatchEvent("click");
    return "dispatch";
  }
}

/**
 * 把被输入区气泡遮住的卡片单独拍下来。
 *
 * 输入区是 fixed 的浮动气泡，截图里它必然压住卡片（这就是用户看到的画面），
 * 所以先拍一张真实观感，再**临时把输入区设为不可见**拍卡片本身 ——
 * manifest 的 note 会写明这一点，不冒充「用户看到的画面」。
 */
async function shotBehindComposer(s, selector, id, title) {
  await s.shot(id, `${title}（真实观感：被输入区气泡遮住）`);
  await shotCardOnly(s, selector, `${id}-card`, `${title}（卡片本身）`);
}

/** 临时隐藏输入区气泡，把被它盖住的卡片拍清楚（note 里写明这是「卡片本身」） */
async function shotCardOnly(s, selector, id, title, note = "") {
  await s.page.locator(".composer").evaluate((el) => {
    el.style.visibility = "hidden";
  });
  await sleep(250);
  await s.shotEl(selector, id, title, {
    pad: 14,
    note:
      note ||
      "输入区气泡遮住了这个提示块（见 REPORT-conv 的布局问题）；本图临时隐藏输入区以显示卡片本身，不是用户当前看到的画面",
  });
  await s.page.locator(".composer").evaluate((el) => {
    el.style.visibility = "";
  });
  await sleep(200);
}

// --------------------------------------------------------------------------
// 1. 空状态
// --------------------------------------------------------------------------
async function sceneEmpty(browser) {
  await anchor(ids.empty);

  const s = await newSession(browser, { theme: "dark", name: "空状态" });
  await openConv(s);
  await s.page.waitForSelector(".empty .greet", { timeout: 15000 });
  await sleep(2600); // 等入口球的真实星球渲染就位
  await s.shot("conv-01-empty-dark", "空状态（暗色）：问候语 + 引导条 + 入口球");
  await s.shotEl(".empty", "conv-02-empty-guide", "空状态引导块：问候 / 副标题 / 打开话题星球");
  await s.shotEl(".composer", "conv-03-composer-idle", "输入区：话题行 + 占位文案 + 发送按钮");
  await s.close();

  const l = await newSession(browser, { theme: "light", name: "空状态亮色" });
  await openConv(l);
  await l.page.waitForSelector(".empty .greet", { timeout: 15000 });
  await sleep(2600);
  await l.shot("conv-04-empty-light", "空状态（亮色）：同一状态的浅色主题");
  await l.close();
}

// --------------------------------------------------------------------------
// 2. 阅读态：长回复 / 表格 / 宽代码 / hover / 复制 / 键盘 / 未读
// --------------------------------------------------------------------------
async function sceneReading(browser) {
  await anchor(ids.main);

  const s = await newSession(browser, { theme: "dark", name: "阅读态" });
  await openConv(s);
  await s.shot("conv-05-history-dark", "正常多轮对话（暗色）：长回复 + 表格 + 宽代码");

  // 键盘 skip-link：第一个 Tab 停靠点，鼠标用户看不到它
  let focusedSkipLink = false;
  for (let i = 0; i < 4 && !focusedSkipLink; i += 1) {
    await s.page.keyboard.press("Tab");
    await sleep(150);
    focusedSkipLink = await s.page.evaluate(
      () => document.activeElement?.classList?.contains("skip-link") ?? false,
    );
  }
  await sleep(350);
  await s.shot(
    "conv-06-skip-link-focus",
    `键盘 skip-link 聚焦态：一步跳到输入框${focusedSkipLink ? "" : "（未拿到焦点，仅示意顶部区域）"}`,
    { clip: { x: 0, y: 0, width: 640, height: 84 } },
  );

  // 轮次分隔头 hover：索引与时间从淡到显形
  await s.page.locator(".turn").first().hover();
  await sleep(300);
  await s.shotEl(".turn-meta", "conv-07-turn-meta-hover", "轮次分隔头 hover：轮次索引与开始时间显形");

  // 长回复（整条消息，比视口高）
  const long = s.page.locator('.message.assistant:has-text("体验改进的四个抓手")').first();
  await long.scrollIntoViewIfNeeded();
  await sleep(300);
  await s.shotEl(
    '.message.assistant:has-text("体验改进的四个抓手")',
    "conv-08-long-answer",
    "长回复整条：虚拟滚动下的高度测量与排版",
  );

  // 表格 + 宽代码
  const table = s.page.locator('.message.assistant:has-text("对比数据如下")').first();
  await table.scrollIntoViewIfNeeded();
  await sleep(300);
  await s.shotEl(
    '.message.assistant:has-text("对比数据如下")',
    "conv-09-table-and-code",
    "表格与代码块同屏：表格不撑破气泡，宽代码自带横向滚动",
  );
  await s.shotEl(
    '.message.assistant:has-text("对比数据如下") .table-wrap',
    "conv-10-table",
    "表格局部：列宽与表头对齐",
  );

  const wideCode = s.page.locator('.code-block:has-text("assemble_context")').first();
  await wideCode.scrollIntoViewIfNeeded();
  await wideCode.hover();
  await sleep(300);
  await s.shotEl(
    '.code-block:has-text("assemble_context")',
    "conv-11-wide-code-hover",
    "宽代码块 hover：复制按钮出现，代码自身横向滚动",
  );
  await wideCode.locator(".code-copy").click();
  await sleep(250);
  await s.shotEl(
    '.code-block:has-text("assemble_context")',
    "conv-12-code-copied",
    "代码块复制成功：按钮变「已复制」",
  );

  // 消息 hover 出现复制按钮 + 复制成功
  const userMsg = s.page.locator('.message.user:has-text("帮我梳理一下")').first();
  await userMsg.scrollIntoViewIfNeeded();
  await userMsg.hover();
  await sleep(300);
  await s.shotEl(
    '.message.user:has-text("帮我梳理一下")',
    "conv-13-message-hover-copy",
    "消息 hover：复制按钮出现（未 hover 时不占注意力）",
  );
  await userMsg.locator(".copy-btn").click();
  await sleep(250);
  await s.shotEl(
    '.message.user:has-text("帮我梳理一下")',
    "conv-14-message-copied",
    "复制成功：只有真的写进剪贴板才显示「已复制」",
  );

  // 上翻阅读：停止跟随 + 未读计数 + 回到最新
  await toTop(s);
  await s.inject("TURN_START", { turn_id: "conv_unread_1", revision: 9001 });
  await s.inject("ASSISTANT", { content: "这条是在你上翻阅读时到达的新消息。" });
  await s.inject("TURN_END", {
    turn_id: "conv_unread_1",
    status: "completed",
    revision: 9002,
    final_content: "这条是在你上翻阅读时到达的新消息。",
  });
  await sleep(700);
  await s.shot("conv-15-unread-back-latest", "上翻阅读时新消息到达：只计数，不抢回滚动位置");
  await s.shotEl(".back-latest", "conv-16-back-latest", "「回到最新消息」入口 + 未读计数");
  await s.close();

  // 窄窗口：表格横向滚动不撑破气泡
  const n = await newSession(browser, {
    theme: "dark",
    viewport: { width: 820, height: 900 },
    name: "窄窗口表格",
  });
  await openConv(n);
  const narrowTable = n.page.locator('.message.assistant:has-text("对比数据如下")').first();
  await narrowTable.scrollIntoViewIfNeeded();
  await sleep(400);
  await n.shotEl(
    '.message.assistant:has-text("对比数据如下")',
    "conv-17-narrow-table-scroll",
    "窄窗口（820px）：表格在气泡内横向滚动，气泡不被撑破",
  );
  await n.close();

  // 开发者模式：token 明细
  const d = await newSession(browser, { theme: "dark", developerMode: true, name: "开发者模式" });
  await openConv(d);
  await bottom(d);
  await d.inject("TURN_START", { turn_id: "conv_dev_1", revision: 9101 });
  await d.inject("ASSISTANT", { content: "开发者模式下，每条消息会带本轮的 token 用量。" });
  await d.inject("TURN_END", {
    turn_id: "conv_dev_1",
    status: "completed",
    revision: 9102,
    final_content: "开发者模式下，每条消息会带本轮的 token 用量。",
  });
  await d.inject("USAGE", { turn_id: "conv_dev_1", tokens: 18432 });
  await sleep(700);
  await d.shotEl(
    '.message.assistant:has-text("每条消息会带本轮的 token 用量")',
    "conv-18-dev-token",
    "开发者模式：消息 metadata 里带本轮 token 用量",
  );
  await d.close();
}

// --------------------------------------------------------------------------
// 3. 运行态：等待 / 生成 / 工具 / 独立任务
// --------------------------------------------------------------------------
async function sceneRunning(browser) {
  const s = await newSession(browser, { theme: "dark", name: "运行态" });
  await openConv(s);
  await bottom(s);

  // 等待中：三圆点 + 正在处理
  await s.inject("TURN_START", { turn_id: "conv_rt_1", revision: 9201 });
  await sleep(500);
  await bottom(s);
  await s.shot("conv-20-waiting", "等待响应：三圆点 + 「正在处理」");
  await s.shotEl(".typing", "conv-21-waiting-row", "全局状态行局部：等待中");

  // 正在生成：打字机 + 过程标记
  await s.inject("ASSISTANT", { content: MARKDOWN_ALL });
  await sleep(700);
  await s.shot("conv-22-generating", "正在生成：打字机逐字展开 + 「过程」标记");
  await s.shotEl(".interim-tag", "conv-23-interim-tag", "「过程」标记：中间话不是最终答案", {
    pad: 22,
  });

  // 正在使用工具：运行中的工具卡
  await s.inject("TOOL_START", {
    call_id: "conv_call_1",
    tool: "web_fetch",
    turn_id: "conv_rt_1",
    presentation: {
      title: "抓取网页内容",
      status: "运行中",
      summary: "正在读取 https://example.com/spec 的正文",
      tool: "web_fetch",
    },
    arguments: { url: "https://example.com/spec" },
  });
  await sleep(600);
  await bottom(s); // 工具卡在流末尾：先贴底，否则整屏图里看不到它
  await s.shot("conv-24-tool-running", "正在使用工具：运行中的工具卡（不假装已完成）");
  await s.shotEl(
    '.tool-card[data-state="running"]',
    "conv-25-tool-card-running",
    "工具卡局部：运行中 + 中文展示名",
  );

  // 工具成功（折叠）
  await s.inject("TOOL_END", {
    call_id: "conv_call_1",
    tool: "web_fetch",
    ok: true,
    content_preview: "已读取正文 12,480 字，主题是「反馈要立刻出现」。",
    presentation: {
      title: "抓取网页内容",
      status: "已完成",
      summary: "已读取正文 12,480 字，主题是「反馈要立刻出现」。",
      tool: "web_fetch",
    },
    duration_ms: 1240,
  });
  await sleep(500);
  await s.shotEl(
    '.tool-card[data-state="ready"]',
    "conv-26-tool-ok",
    "工具卡成功（折叠）：已完成 + 耗时",
  );

  // 工具展开：完整输出
  const okCard = s.page.locator('.tool-card[data-state="ready"]').first();
  await okCard.locator(".tool-head").click();
  await sleep(500);
  await s.shotEl(
    '.tool-card[data-state="ready"]',
    "conv-27-tool-expanded",
    "工具卡展开：完整输出与耗时",
  );

  // 工具失败
  await s.inject("TOOL_START", {
    call_id: "conv_call_2",
    tool: "fs_write",
    turn_id: "conv_rt_1",
    presentation: { title: "写入文件", status: "运行中", tool: "fs_write" },
    arguments: { path: "D:\\notes\\spec.md" },
  });
  await s.inject("TOOL_END", {
    call_id: "conv_call_2",
    tool: "fs_write",
    ok: false,
    error: "沙箱拒绝：路径不在允许的工作区内",
    content_preview: "",
    presentation: { title: "写入文件", status: "失败", tool: "fs_write" },
    duration_ms: 320,
  });
  await sleep(500);
  await s.shotEl(
    '.tool-card[data-state="failed"]',
    "conv-28-tool-failed",
    "工具卡失败：卡面一行结论 + 耗时",
  );
  const failCard = s.page.locator('.tool-card[data-state="failed"]').first();
  await failCard.locator(".tool-head").click();
  await sleep(500);
  await s.shotEl(
    '.tool-card[data-state="failed"]',
    "conv-29-tool-failed-expanded",
    "工具卡失败展开：完整错误留在折叠详情里",
  );

  // 独立任务：running / done / failed
  await s.inject("SUBAGENT_STATUS", {
    task_id: "conv_task_1",
    status: "running",
    display_name: "整理接口清单",
    tool: "subagent",
    goal: "把 API 路由整理成一张按用途分组的清单",
  });
  await sleep(400);
  await s.shotEl(
    '.subagent-card[data-state="running"]',
    "conv-30-subagent-running",
    "独立任务进行中：与普通工具卡明确区分",
  );

  await s.inject("SUBAGENT_STATUS", {
    task_id: "conv_task_2",
    status: "done",
    display_name: "统计事件类型",
    tool: "subagent",
    goal: "统计事件协议里的类型数量",
    ok: true,
    content_preview: "事件协议里共有 18 种类型，最常触发的是 ASSISTANT。",
  });
  await sleep(400);
  await s.shotEl(
    '.subagent-card[data-state="ready"]',
    "conv-31-subagent-done",
    "独立任务已完成：结果一行可见",
  );

  await s.inject("SUBAGENT_STATUS", {
    task_id: "conv_task_3",
    status: "failed",
    display_name: "跑一遍回归",
    tool: "subagent",
    goal: "跑一遍前端回归测试",
    ok: false,
    error: "环境里没有可用的测试运行时",
  });
  await sleep(400);
  await s.shotEl(
    '.subagent-card[data-state="failed"]',
    "conv-32-subagent-failed",
    "独立任务失败：说清结果与原因",
  );

  // 正在整理独立任务的结果（notify）
  await s.inject("TURN_END", { turn_id: "conv_rt_1", status: "completed", revision: 9202 });
  await s.inject("TURN_START", { turn_id: "conv_rt_2", notify: true, revision: 9203 });
  await sleep(500);
  await s.shotEl(".typing", "conv-33-notify", "正在整理独立任务的结果（系统驱动的收尾轮）");
  await s.inject("TURN_END", { turn_id: "conv_rt_2", status: "completed", revision: 9204 });
  await s.close();
}

// --------------------------------------------------------------------------
// 4. 队列 / 继续条
// --------------------------------------------------------------------------
async function sceneQueue(browser) {
  const s = await newSession(browser, { theme: "dark", name: "队列" });
  await openConv(s);
  await bottom(s);
  await s.inject("TURN_START", { turn_id: "conv_q_run", revision: 9301 });
  await s.inject("TURN_QUEUE", {
    revision: 9302,
    running: { turn_id: "conv_q_run", message: "帮我梳理一下这一轮要做的事。" },
    queued: [
      { turn_id: "conv_q_2", message: "顺便把表格也整理一份。" },
      { turn_id: "conv_q_3", message: "再给一段宽代码示例。" },
    ],
    cancelled: [{ turn_id: "conv_q_9", message: "这条我撤回。" }],
  });
  await sleep(600);
  await s.shot("conv-35-queue-chip", "队列折叠态：1 运行中 · 2 排队中 · 1 已取消");
  await s.shotEl(".queue", "conv-36-queue-chip-only", "队列气泡局部：状态计数 + 展开箭头");
  await s.page.locator(".queue .chip").click();
  await sleep(500);
  await s.shotEl(
    ".queue",
    "conv-37-queue-open",
    "队列展开：运行中可停止、排队项可取消、已取消只留记录",
  );
  await s.close();

  const c = await newSession(browser, { theme: "dark", name: "继续条" });
  await openConv(c);
  await bottom(c);
  await c.inject("APPROVAL_REQUIRED", {
    approval_id: "conv_continue_1",
    kind: "continue",
    payload: { used_iterations: 6, max_iterations: 6 },
  });
  await sleep(500);
  await c.shotEl(
    ".continue-bar",
    "conv-38-continue-bar",
    "继续条：已达迭代上限，继续 / 停止都在这里决定",
  );
  await c.page.route("**/api/approvals/*/respond", (route) =>
    route.fulfill({ status: 500, contentType: "application/json", body: '{"detail":"boom"}' }),
  );
  await c.page.locator(".continue-bar .qio-btn.primary").click();
  await sleep(700);
  await c.shotEl(".continue-bar", "conv-39-continue-error", "继续条提交失败：失败留在流程内，可重试");
  await c.close();
}

// --------------------------------------------------------------------------
// 5. 状态通知条
// --------------------------------------------------------------------------
async function noticeScene(browser, name, fn) {
  const s = await newSession(browser, { theme: "dark", name });
  await openConv(s);
  await fn(s);
  return s;
}

async function sceneNotices(browser) {
  {
    const s = await noticeScene(browser, "错误条", async (s) => {
      await s.inject("ERROR", { message: "模型返回了无法解析的响应，这一轮没有完成" });
      await sleep(500);
      await s.shotEl(".notice.err", "conv-40-notice-error", "错误条：错误 + 查看详情 / 前往设置");
    });
    await s.close();
  }
  {
    const s = await noticeScene(browser, "警告条", async (s) => {
      await s.inject("WARNING", { message: "本轮没有联网检索：搜索服务暂时不可用" });
      await sleep(500);
      await s.shotEl(".notice.warn", "conv-41-notice-warning", "警告条：只需要知道，不需要查");
    });
    await s.close();
  }
  {
    const s = await noticeScene(browser, "已停止", async (s) => {
      await bottom(s);
      await s.inject("TURN_START", { turn_id: "conv_n_cancel", revision: 9401 });
      await s.inject("ASSISTANT", { content: "我先看了一下设置页的结构" });
      await s.inject("TURN_END", { turn_id: "conv_n_cancel", status: "cancelled", revision: 9402 });
      await sleep(600);
      await s.shotEl(".notice.quiet", "conv-42-notice-cancelled", "已停止：取消是正常结局，不按错误表示");
    });
    await s.close();
  }
  {
    const s = await noticeScene(browser, "模型不可用", async (s) => {
      await s.inject("TURN_START", { turn_id: "conv_n_unavail", revision: 9411 });
      await s.inject("TURN_END", { turn_id: "conv_n_unavail", status: "unavailable", revision: 9412 });
      await sleep(600);
      await s.shotEl(".notice.warn", "conv-43-notice-unavailable", "模型不可用：没有可用凭据时给出的去路");
    });
    await s.close();
  }
  {
    const s = await noticeScene(browser, "兼容模式", async (s) => {
      findings.push(
        "兼容模式提示条被输入区气泡遮住：`.notice.fallback` 排在 `.stream` 之后，" +
          "与 fixed 的 `.composer` 重叠（顶部那三条通知在 `.stream` 之前，所以没这个问题）",
      );
      await s.inject("FALLBACK", {
        message: "当前模型不支持原生工具调用，已使用兼容模式（功能可能受限）",
      });
      await sleep(800);
      await shotBehindComposer(s, ".notice.fallback", "conv-44-notice-fallback", "兼容模式提示");
    });
    await s.close();
  }
  {
    const s = await noticeScene(browser, "凭据不可用", async (s) => {
      await s.inject("CREDENTIAL_STATUS", { status: "unavailable" });
      await sleep(500);
      await s.shotEl(
        ".notice.warn",
        "conv-45-credential-unavailable",
        "凭据不可用提示（走警告条，不暴露 key_id）",
      );
    });
    await s.close();
  }
  {
    const s = await newSession(browser, { theme: "dark", name: "历史读取失败" });
    await s.page.route("**/api/session/context*", (route) =>
      route.fulfill({ status: 500, contentType: "application/json", body: '{"detail":"db locked"}' }),
    );
    await openConv(s);
    await sleep(500);
    await s.shotEl(
      ".notice.quiet",
      "conv-46-history-error",
      "历史读取失败：低干扰提示 + 重试（不清空已有内容）",
    );
    await s.page.unroute("**/api/session/context*");
    await s.page.locator(".notice.quiet .link").click();
    await sleep(1500);
    await s.shot("conv-47-history-retry-ok", "重试后：历史正常读回");
    await s.close();
  }
}

// --------------------------------------------------------------------------
// 6. 交互卡：话题切换 / 知识候选 / 工具创建
// --------------------------------------------------------------------------
/** 话题切换提示（含 busy） */
async function sceneTopicSwitch(browser) {
  findings.push(
    "话题切换提示条被输入区气泡遮住：`.topic-switch` 在布局里排在 fixed 的 `.composer` 之下，" +
      "1440×900 下 elementFromPoint 命中的是输入框（textarea），提示条看不见也点不到",
  );
  const s = await newSession(browser, { theme: "dark", name: "话题切换" });
  await openConv(s);
  await s.inject("TOPIC_SWITCH_SUGGESTED", {
    topic_id: ids.rich || "topic_other",
    topic_name: "知识页与实体页的可见性讨论",
    reason: "这段内容看起来属于另一个话题",
  });
  await sleep(900);
  await shotBehindComposer(s, ".topic-switch", "conv-50-topic-switch", "话题切换提示");

  await s.page.route("**/api/topic-switch/confirm", async (route) => {
    await sleep(8000);
    await route.abort();
  });
  await safeClick(s, ".topic-switch .qio-btn.primary");
  await sleep(700);
  await shotBehindComposer(s, ".topic-switch", "conv-51-topic-switch-busy", "话题切换进行中");
  await s.close();
}

/** 知识候选卡（默认 / 内联编辑 / 保存失败 / 保存中） */
async function sceneKnowledgeCandidate(browser) {
  findings.push(
    "知识候选卡被输入区气泡遮住：`.candidate` 反过来排在 `.stream` 之后，" +
      "与 fixed 的 `.composer`（860×124，y=761）重叠，用户看不到也点不到保存/修改/忽略",
  );
  const s = await newSession(browser, { theme: "dark", name: "知识候选" });
  await openConv(s);
  await bottom(s);
  await s.inject("TURN_START", { turn_id: "conv_kc_1", revision: 9501 });
  await s.inject("KNOWLEDGE_CANDIDATE", {
    knowledge_id: "conv_know_candidate_1",
    category: "user_profile",
    content: "用户更习惯用中文提出需求，回复也应以中文为主。",
    reason: "这条会长期影响回答的语言与措辞。",
  });
  await s.inject("TURN_END", {
    turn_id: "conv_kc_1",
    status: "completed",
    revision: 9502,
    final_content: "我把这条记下来了，等回答结束再请你确认。",
  });
  await sleep(900);
  await shotBehindComposer(s, ".candidate", "conv-52-candidate", "知识候选卡");

  await safeClick(s, ".candidate .edit-btn");
  await sleep(500);
  await shotCardOnly(
    s,
    ".candidate",
    "conv-53-candidate-edit",
    "知识候选内联编辑：不跳转到 Knowledge Panel",
  );
  await safeClick(s, ".candidate .actions .quiet");
  await sleep(400);

  await s.page.route("**/api/knowledge/*/verify", (route) =>
    route.fulfill({ status: 500, contentType: "application/json", body: '{"detail":"locked"}' }),
  );
  await safeClick(s, ".candidate .keep");
  await sleep(900);
  await shotCardOnly(s, ".candidate", "conv-54-candidate-fail", "知识候选保存失败：失败留在卡上，可重试");
  await s.page.unroute("**/api/knowledge/*/verify");

  await s.page.route("**/api/knowledge/*/verify", async (route) => {
    await sleep(8000);
    await route.abort();
  });
  await safeClick(s, ".candidate .keep");
  await sleep(600);
  await shotCardOnly(s, ".candidate", "conv-55-candidate-busy", "知识候选保存中：按钮进入「处理中…」");
  await s.close();
}

/** 工具创建卡：一条流程、一张卡 */
async function sceneToolCreation(browser) {
  const s = await newSession(browser, { theme: "dark", name: "工具创建" });
  await openConv(s);
  await bottom(s);
  const phases = [
    ["proposal", "提案", "我打算做一个「网页摘要」工具", "conv-56-tool-create-proposal", "工具创建·提案：要做什么先摊开"],
    ["building", "正在构建", "正在按提案写实现与注释", "conv-57-tool-create-building", "工具创建·构建：代码在开发工作区里生成"],
    ["testing", "正在测试", "正在跑边界用例", "conv-58-tool-create-testing", "工具创建·测试：这一步决定能不能提交给你"],
    ["testing_failed", "测试失败", "测试没有通过：先让工具把测试跑绿再提交", "conv-59-tool-create-test-failed", "工具创建·测试失败：原因写在卡面上"],
    ["waiting_approval", "等待你的确认", "测试通过，等你确认后再启用", "conv-60-tool-create-waiting", "工具创建·等待确认：授权前停下来"],
    ["registering", "正在启用", "正在注册到工具清单", "conv-61-tool-create-registering", "工具创建·启用：注册中"],
    ["ready", "已创建", "现在可以使用了", "conv-62-tool-create-ready", "工具创建·已创建：流程收束在同一张卡上"],
  ];
  for (const [phase, label, detail, id, title] of phases) {
    await s.inject("TOOL_CREATE_STATUS", {
      group_id: "conv_ws_1",
      phase,
      label,
      detail,
      ok: phase === "testing_failed" ? false : phase === "ready" ? true : undefined,
      tool_name: "网页摘要",
      turn_id: null,
    });
    await sleep(450);
    await s.shotEl(".create-card", id, title);
  }
  await safeClick(s, ".create-card .detail-toggle");
  await sleep(500);
  await s.shotEl(
    ".create-card",
    "conv-63-tool-create-ready-expanded",
    "工具创建详情展开：工具名 / 开发工作区 / 阶段",
  );

  await s.inject("TOOL_CREATE_STATUS", {
    group_id: "conv_ws_2",
    phase: "failed",
    label: "创建失败",
    detail: "测试没有通过：先让工具把测试跑绿再提交",
    ok: false,
    tool_name: "批量改文件名",
    turn_id: null,
  });
  await sleep(450);
  await s.shotEl(
    '.create-card[data-state="failed"]',
    "conv-64-tool-create-failed",
    "工具创建失败：说清结果 + 下一步",
  );
  await s.close();
}

// --------------------------------------------------------------------------
// 7. 审批
// --------------------------------------------------------------------------
const TOOL_CREATE_HIGH = {
  name: "网页摘要",
  tool_type: "function",
  description: "按你给的网址抓取正文，整理成三条要点",
  explanation: "你刚才要求「把这篇长文压缩一下」，需要联网读取原文",
  capabilities: ["联网：是", "读取文件：是", "写入文件：否", "副作用：read"],
  access: ["https://example.com/spec"],
  test_summary: "3 项检查通过",
  test_details: [
    { name: "边界用例", passed: true, detail: "空正文与超长正文都处理了" },
    { name: "失败路径", passed: true },
  ],
  policy_fingerprint: "pf_9f2ac1",
  risk: "medium",
};

async function approvalScene(browser, { id, kind, payload, name }) {
  const s = await newSession(browser, { theme: "dark", name });
  await openConv(s);
  await bottom(s);
  await s.inject("APPROVAL_REQUIRED", { approval_id: id, kind, payload });
  await sleep(800);
  return s;
}

async function sceneApprovals(browser) {
  {
    const s = await approvalScene(browser, {
      id: "conv_appr_high",
      kind: "tool_create",
      payload: TOOL_CREATE_HIGH,
      name: "审批·高风险",
    });
    await s.shot("conv-70-approval-tool-high", "工具创建审批（高风险）：会联网时批准按钮降调");
    await s.shotEl(
      ".modal",
      "conv-71-approval-tool-high-card",
      "审批卡局部：做什么 / 会访问什么 / 会改变什么",
    );
    await s.close();
  }

  {
    const s = await approvalScene(browser, {
      id: "conv_appr_low",
      kind: "tool_create",
      payload: {
        name: "统计事件类型",
        tool_type: "function",
        description: "读一遍事件协议，统计每种类型的出现次数",
        capabilities: ["联网：否", "读取文件：是", "副作用：read"],
        test_summary: "2 项检查通过",
        policy_fingerprint: "pf_1c33dd",
      },
      name: "审批·低风险",
    });
    await s.shotEl(".modal", "conv-72-approval-tool-low", "工具创建审批（只读）：批准按钮是主操作色");
    await s.close();
  }

  {
    const s = await approvalScene(browser, {
      id: "conv_appr_subagent",
      kind: "tool_create",
      payload: {
        name: "回归巡检",
        tool_type: "subagent",
        description: "让一个独立任务把前端回归跑一遍并汇总结果",
        capabilities: ["启动进程：是", "读取文件：是", "副作用：read"],
        test_summary: "n/a (subagent)",
        subagent_budget: { max_iterations: 5, max_tokens: 100000, output_limit_chars: 2000 },
        policy_fingerprint: "pf_sub_7781",
      },
      name: "审批·子 agent 预算",
    });
    await s.shotEl(".modal", "conv-73-approval-subagent-budget", "子 agent 型工具：执行预算可改后批准");
    await s.close();
  }

  {
    const s = await approvalScene(browser, {
      id: "conv_appr_cred",
      kind: "credential_grant",
      payload: {
        key_id: "key_baseline_main_openai",
        tool_name: "联网检索",
        description: "这次检索要用你已配置的主钥",
        capabilities: ["使用凭据：key_baseline_main_openai", "联网：是", "副作用：read"],
      },
      name: "审批·凭据授权",
    });
    await s.shotEl(
      ".modal",
      "conv-74-approval-credential",
      "凭据授权审批：长期生效，说清会用哪一项凭据",
    );
    await s.close();
  }

  {
    const s = await approvalScene(browser, {
      id: "conv_appr_know",
      kind: "high_impact_knowledge",
      payload: {
        content: "用户偏好：解释要短，先给结论再给理由。",
        reason: "这条会长期影响回答的风格。",
        source_count: 3,
      },
      name: "审批·高影响知识",
    });
    await s.shotEl(".modal", "conv-75-approval-knowledge", "高影响知识确认：长期生效的一条偏好");
    await s.close();
  }

  {
    const s = await approvalScene(browser, {
      id: "conv_appr_adv",
      kind: "tool_create",
      payload: TOOL_CREATE_HIGH,
      name: "审批·高级详情",
    });
    await s.page.locator(".modal .adv summary").click();
    await sleep(400);
    // 弹窗在 <details> 展开后元素截图会卡在「等待字体加载」上，这里用整屏 + 关闭动画降级的截图
    await s.shot(
      "conv-76-approval-advanced",
      "审批高级详情展开：策略指纹 / 内部动作名 / 逐条测试结果",
      { animations: "disabled" },
    );
    await s.close();
  }

  {
    const s = await approvalScene(browser, {
      id: "conv_appr_fail",
      kind: "tool_create",
      payload: TOOL_CREATE_HIGH,
      name: "审批·提交失败",
    });
    await s.page.route("**/api/approvals/*/respond", (route) =>
      route.fulfill({ status: 500, contentType: "application/json", body: '{"detail":"backend down"}' }),
    );
    await s.page.locator(".modal .approve").click();
    await sleep(800);
    await s.shotEl(".modal", "conv-77-approval-failed", "审批提交失败：弹窗保留 + 「未做出任何授权」");
    await s.close();
  }

  {
    const s = await approvalScene(browser, {
      id: "conv_appr_stale",
      kind: "tool_create",
      payload: TOOL_CREATE_HIGH,
      name: "审批·失效",
    });
    await s.page.route("**/api/approvals/*/respond", (route) =>
      route.fulfill({ status: 404, contentType: "application/json", body: '{"detail":"not found"}' }),
    );
    await s.page.locator(".modal .approve").click();
    await sleep(800);
    await s.shotEl(
      ".modal",
      "conv-78-approval-stale",
      "审批已失效：只给一个「知道了」，不再提供批准/拒绝",
    );
    await s.close();
  }

  {
    const s = await newSession(browser, { theme: "dark", name: "审批入口" });
    await openConv(s);
    await bottom(s);
    await s.page.locator("#composer-input").click();
    await s.page.locator("#composer-input").type("我正在输入一条还没写完的消息");
    await s.inject("APPROVAL_REQUIRED", {
      approval_id: "conv_appr_entry",
      kind: "tool_create",
      payload: TOOL_CREATE_HIGH,
    });
    await sleep(800);
    await s.shot("conv-79-approval-entry", "审批入口条：正在输入时不抢焦点，只亮出待确认入口");
    await s.shotEl(".approval-entry", "conv-80-approval-entry-only", "审批入口条局部");
    await s.page.locator(".approval-entry").click();
    await sleep(700);
    await s.shotEl(".modal", "conv-81-approval-opened", "从入口主动打开：与直接弹窗是同一个对话框");
    await s.close();
  }

  // 审批窗口的「稍后处理」：Esc 收起但保留待办
  {
    const s = await approvalScene(browser, {
      id: "conv_appr_defer",
      kind: "tool_create",
      payload: TOOL_CREATE_HIGH,
      name: "审批·稍后处理",
    });
    await s.page.keyboard.press("Escape");
    await sleep(600);
    await s.shot("conv-82-approval-deferred", "审批「稍后处理」：窗口收起，待办不消失（入口条接手）");
    await s.shotEl(".typing", "conv-83-approval-activity", "全局状态行：等待你确认（窗口收起时才看得到）");
    await s.close();
  }
}

// --------------------------------------------------------------------------
// 8. 浮动组件与视口
// --------------------------------------------------------------------------
async function sceneFloating(browser) {
  const s = await newSession(browser, { theme: "dark", name: "浮动组件" });
  await openConv(s);
  await sleep(2600);
  await s.shotEl(".settings-float", "conv-85-settings-float", "设置悬浮入口（右上角贴角）");
  await s.page.locator(".settings-float").hover();
  await sleep(400);
  await s.shotEl(".settings-float", "conv-86-settings-float-hover", "设置入口 hover");
  await s.shotEl(".dock", "conv-87-planet-dock", "星球入口球：全屏星球的压缩态");

  const dock = s.page.locator(".dock");
  const box = await dock.boundingBox();
  if (box) {
    await s.page.mouse.move(box.x + box.width / 2, box.y + box.height / 2);
    await s.page.mouse.down();
    await s.page.mouse.move(box.x + box.width / 2 + 60, box.y + box.height / 2, { steps: 8 });
    await s.page.mouse.move(1440 - 8, box.y + box.height / 2, { steps: 8 });
    await s.page.mouse.up();
    await sleep(1200);
    await s.shot("conv-88-planet-dock-edge", "星球入口球拖动到贴边：靠边收起");
  }
  await s.close();

  const n = await newSession(browser, {
    theme: "dark",
    viewport: { width: 820, height: 900 },
    name: "窄窗口",
  });
  await openConv(n);
  await bottom(n);
  await n.shot("conv-89-narrow-layout", "窄窗口（820×900）：输入区整宽贴底，为悬浮球让出通道");
  await n.close();

  const t = await newSession(browser, {
    theme: "light",
    viewport: { width: 620, height: 860 },
    name: "超窄亮色",
  });
  await openConv(t);
  await bottom(t);
  await t.shot("conv-90-tiny-light", "超窄窗口（620×860，亮色）：极端视口下的对话页");
  await t.close();
}

// --------------------------------------------------------------------------
// 9. 历史锚点提示
// --------------------------------------------------------------------------
/** 亮色主题下的几个主状态（清单要求「全部主状态 × dark / light」） */
async function sceneLightVariants(browser) {
  {
    const s = await newSession(browser, { theme: "light", name: "亮色对话" });
    await openConv(s);
    await bottom(s);
    await s.shot("conv-91-conversation-light", "正常多轮对话（亮色）");
    await s.close();
  }
  {
    const s = await newSession(browser, { theme: "light", name: "亮色工具卡" });
    await openConv(s);
    await bottom(s);
    await s.inject("TURN_START", { turn_id: "conv_lt_1", revision: 9601 });
    await s.inject("TOOL_START", {
      call_id: "conv_lt_call",
      tool: "web_fetch",
      turn_id: "conv_lt_1",
      presentation: { title: "抓取网页内容", status: "运行中", tool: "web_fetch" },
    });
    await sleep(600);
    await s.shotEl(
      '.tool-card[data-state="running"]',
      "conv-92-tool-card-light",
      "工具卡运行中（亮色）",
    );
    await s.shot("conv-93-tool-running-light", "正在使用工具（亮色）：状态行 + 工具卡");
    await s.inject("TURN_END", { turn_id: "conv_lt_1", status: "completed", revision: 9602 });
    await s.close();
  }
  {
    const s = await newSession(browser, { theme: "light", name: "亮色错误条" });
    await openConv(s);
    await s.inject("ERROR", { message: "模型返回了无法解析的响应，这一轮没有完成" });
    await sleep(500);
    await s.shotEl(".notice.err", "conv-94-notice-error-light", "错误条（亮色）");
    await s.close();
  }
  {
    const s = await newSession(browser, { theme: "light", name: "亮色知识候选" });
    await openConv(s);
    await bottom(s);
    await s.inject("TURN_START", { turn_id: "conv_lt_kc", revision: 9611 });
    await s.inject("KNOWLEDGE_CANDIDATE", {
      knowledge_id: "conv_know_candidate_light",
      category: "goal",
      content: "本轮目标：把收集到的界面状态整理成可索引的图册。",
      reason: "这条会长期影响任务的组织方式。",
    });
    await s.inject("TURN_END", {
      turn_id: "conv_lt_kc",
      status: "completed",
      revision: 9612,
      final_content: "已经记下来了，等你确认。",
    });
    await sleep(900);
    await shotBehindComposer(s, ".candidate", "conv-97-candidate-light", "知识候选卡（亮色）");
    await s.close();
  }
  {
    const s = await newSession(browser, { theme: "light", name: "亮色审批" });
    await openConv(s);
    await bottom(s);
    await s.inject("APPROVAL_REQUIRED", {
      approval_id: "conv_appr_light",
      kind: "tool_create",
      payload: TOOL_CREATE_HIGH,
    });
    await sleep(800);
    await s.shotEl(".modal", "conv-98-approval-light", "工具创建审批（亮色）");
    await s.close();
  }
}

async function sceneAnchorHistoric(browser) {
  const detail = await apiRequest("GET", `/api/graph/topics/${encodeURIComponent(ids.main)}`);
  const frags = detail.fragments ?? [];
  const closed = frags.find((f) => f.closed_at) ?? frags[0];
  if (!closed) throw new Error("主话题没有可用片段，跳过锚点场景");
  const fragId = closed.fragment_id ?? closed.id;

  // 「从历史继续」有两个入口：产品里的「从这里继续」按钮走 continue_from_history。
  // 这条路径在「话题里已经有一个开放片段」时会 500（见汇报里的产品问题），
  // 所以先如实记录它，再用「进入这段历史位置」把同一个界面状态取出来。
  let historic = null;
  try {
    const res = await apiRequest("POST", "/api/anchor", {
      topic_id: ids.main,
      fragment_id: fragId,
      continue_from_history: true,
    });
    historic = Boolean(res.historic);
  } catch (e) {
    findings.push(
      `「从历史继续」接口失败：POST /api/anchor{continue_from_history:true} → ${String(e?.message ?? e)}` +
        "（话题已有开放片段时 fragments.topic_id 唯一索引冲突；旧片段本身是关闭的，属于正常数据形状）",
    );
    const fallback = await apiRequest("POST", "/api/anchor", {
      topic_id: ids.main,
      fragment_id: fragId,
    });
    historic = Boolean(fallback.historic);
  }
  console.log(`  [锚点] 历史位置 historic=${historic}`);
  await sleep(700);

  const s = await newSession(browser, { theme: "dark", name: "历史锚点" });
  await openConv(s);
  await s.shotEl(
    ".composer .topicbar",
    "conv-95-anchor-historic",
    `从历史位置继续：输入区上方显示「从「…」继续」${historic ? "" : "（锚点未标记为历史位置）"}`,
  );
  await s.shot("conv-96-anchor-historic-full", "从历史位置继续：整页状态");
  await s.close();

  await anchor(ids.main);
}

async function main() {
  await loadTopics();
  // 只重跑指定场景（其它场景的产物保留在 manifest 里，按 id 合并）：
  //   node scripts/ui-catalog/conv.mjs candidate
  const only = process.argv.slice(2).filter((a) => !a.startsWith("-"));
  const want = (name) => only.length === 0 || only.includes(name);
  findings.push(
    "新页面会重放事件总线里最近 50 条事件（只过滤了 APPROVAL_REQUIRED）：" +
      "新开的窗口/标签页会看到上一次会话留下的错误条、队列气泡、知识候选卡、兼容模式条，" +
      "空对话也能被拍出「1 排队中 · 已取消」这类残留状态（见采集时的 conv-01 首版）",
  );
  const browser = await launchBrowser();
  try {
    if (want("empty")) await scene("空状态", () => sceneEmpty(browser));
    if (want("reading")) await scene("阅读态", () => sceneReading(browser));
    if (want("running")) await scene("运行态", () => sceneRunning(browser));
    if (want("queue")) await scene("队列与继续条", () => sceneQueue(browser));
    if (want("notices")) await scene("状态通知条", () => sceneNotices(browser));
    if (want("topic-switch")) await scene("交互卡·话题切换", () => sceneTopicSwitch(browser));
    if (want("candidate")) await scene("交互卡·知识候选", () => sceneKnowledgeCandidate(browser));
    if (want("tool-creation")) await scene("交互卡·工具创建", () => sceneToolCreation(browser));
    if (want("approval")) await scene("审批", () => sceneApprovals(browser));
    if (want("floating")) await scene("浮动组件与视口", () => sceneFloating(browser));
    if (want("light")) await scene("亮色变体", () => sceneLightVariants(browser));
    if (want("anchor")) await scene("历史锚点", () => sceneAnchorHistoric(browser));
  } finally {
    await browser.close();
  }

  const manifest = readManifest("conv");
  console.log(`\n=== conv 采集结束：${manifest?.count ?? 0} 张 ===`);
  if (findings.length) {
    console.log(`产品问题 ${findings.length} 条：`);
    for (const f of findings) console.log(`  - ${f}`);
  }
  if (failures.length) {
    console.log(`失败场景 ${failures.length} 个：`);
    for (const f of failures) console.log(`  - ${f}`);
    process.exitCode = 1;
  }
}

await runGroup(main);
