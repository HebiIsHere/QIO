/**
 * 分组 `conv-root`：对话页与消息流的**穷举**截图（root 负责的这一组）。
 *
 * 覆盖：基础阅读（长回复 / 表格 / 宽代码块 / 复制 / 回到最新）、Markdown 全元素、
 * 运行态（等待 / 生成 / 工具 / 独立任务 / 队列 / 继续条 / 工具创建 / 知识候选）、
 * 通知条家族、审批弹窗（四类 kind + 高/低风险 + 高级详情 + 失败 + 入口）、
 * 主题与视口、以及**空数据目录的首次启动**。
 *
 * 两个实例（都指向自己起的隔离实例，避免多路采集互相注入事件）：
 * - 主对话实例：QIO_BASE / QIO_API（本组约定 6204 / 8839，已播种基线数据）；
 * - 空实例：6203 / 8838（空数据目录，真正的第一次打开）。
 *
 * 用法：
 *   python scripts/ui-catalog/instance.py up --name convroot --backend-port 8839 --frontend-port 6204 --seed --clean
 *   python scripts/ui-catalog/instance.py up --name empty --backend-port 8838 --frontend-port 6203 --clean
 *   $env:QIO_BASE="http://127.0.0.1:6204"; $env:QIO_API="http://127.0.0.1:8839"
 *   node scripts/ui-catalog/conv-root.mjs
 */
import {
  API as ENV_API,
  BASE as ENV_BASE,
  createSession,
  launchBrowser,
  saveManifest,
  sleep,
} from "./lib.mjs";

const MAIN = { base: ENV_BASE, api: ENV_API };
const EMPTY = { base: "http://127.0.0.1:6203", api: "http://127.0.0.1:8838" };
const GROUP = "conv-root";

const all = [];
const failures = [];

/** 单张图失败不能带走整批：记下来继续跑 */
async function safe(id, title, fn) {
  try {
    await fn();
  } catch (err) {
    const message = String(err?.message ?? err).replace(/\s+/g, " ").slice(0, 240);
    failures.push({ id, title, error: message });
    console.log(`  [FAIL] ${id} ${title} :: ${message}`);
  }
}

function collect(session) {
  all.push(...session.entries.splice(0, session.entries.length));
}

/** 每段结束就落盘一次：中途失败也不会丢掉已拍的图 */
function flush() {
  saveManifest(GROUP, all);
}

const DOCK = "[data-planet-entry]";

const MARKDOWN_ALL = `## 二级标题
这是正文，带 **加粗**、*斜体*、\`行内代码\`，还有一个[站内链接](#/)。

### 列表
- 无序项一
- 无序项二

1. 有序一
2. 有序二

- [x] 已完成的任务项
- [ ] 未完成的任务项

> 引用：内容先出现，动画只是它的入场方式。

---

| 能力 | 状态 | 说明 |
| --- | --- | --- |
| 记忆 | 可用 | 片段封块 + 摘要 |
| 星球 | 可用 | 话题大陆 |

\`\`\`ts
const follow = isNearBottom(el) && !userScrolledUp;
\`\`\`
`;

const LONG_ANSWER = `## 这一轮做了什么
把「操作有没有被接住」这件事落到三个具体动作上：发送立刻有反馈、状态一直可辨、位置不丢。
**第一**，点击发送之后界面马上显示这条已经交出去了。**第二**，失败必须看得见，不能悄悄清空输入。
**第三**，正在读的位置、正在写的草稿都要保留。

### 几个说明
- 动画短而平稳，不排队挡住后续操作。
- 已经显示过的文字不再播第二次。
`;

const CAPS_WRITE = [
  "联网：是（限 api.example.com）",
  "读取文件：是",
  "写入文件：是",
  "启动进程：否",
  "使用凭据：无",
  "副作用：write",
];
const CAPS_READONLY = [
  "联网：否",
  "读取文件：是",
  "写入文件：否",
  "启动进程：否",
  "使用凭据：无",
  "副作用：read",
];

const APPROVALS = [
  {
    id: "conv-80-approval-tool-create-highrisk",
    title: "审批：工具创建（高风险，批准按钮降调）",
    theme: "dark",
    data: {
      approval_id: "conv_ap_1",
      kind: "tool_create",
      payload: {
        name: "fetch_doc",
        description: "抓取指定文档并写入工作区",
        explanation: "你要求自动归档资料，需要在联网抓取后落盘",
        capabilities: CAPS_WRITE,
        access: ["api.example.com", "workspace/archive/**.md"],
        policy_fingerprint: "fp_conv_1",
        test_summary: "3/3 检查通过",
        test_details: [{ name: "dry_run", passed: true, detail: "无异常" }],
      },
    },
  },
  {
    id: "conv-81-approval-tool-create-lowrisk",
    title: "审批：工具创建（低风险，只读语义 + 高级详情展开）",
    theme: "dark",
    expand: true,
    data: {
      approval_id: "conv_ap_2",
      kind: "tool_create",
      payload: {
        name: "report_only",
        description: "只读取统计信息并生成说明",
        capabilities: CAPS_READONLY,
        test_summary: "1/1 检查通过",
      },
    },
  },
  {
    id: "conv-82-approval-credential-grant",
    title: "审批：凭据授权",
    theme: "dark",
    data: {
      approval_id: "conv_ap_3",
      kind: "credential_grant",
      payload: {
        key_id: "key_baseline_main_openai",
        tool_name: "web_search",
        reason: "联网检索需要调用你配置的密钥",
        capabilities: ["联网：是", "使用凭据：main_openai", "副作用：read"],
      },
    },
  },
  {
    id: "conv-83-approval-knowledge",
    title: "审批：高影响知识确认",
    theme: "dark",
    data: {
      approval_id: "conv_ap_4",
      kind: "high_impact_knowledge",
      payload: {
        content: "用户长期目标：把 QIO 用作长期记忆助手。",
        reason: "这会长期影响回答方式",
        source_count: 3,
      },
    },
  },
  {
    id: "conv-84-approval-subagent-budget",
    title: "审批：子 agent 型工具（可改预算）",
    theme: "dark",
    data: {
      approval_id: "conv_ap_5",
      kind: "tool_create",
      payload: {
        name: "调研助手",
        tool_type: "subagent",
        description: "派生一个独立任务去调研并汇总",
        test_summary: "n/a (subagent)",
        capabilities: ["联网：是", "副作用：read"],
      },
    },
  },
  {
    id: "conv-85-approval-tool-create-light",
    title: "审批：工具创建（浅色主题）",
    theme: "light",
    data: {
      approval_id: "conv_ap_6",
      kind: "tool_create",
      payload: {
        name: "fetch_doc",
        description: "抓取指定文档并写入工作区",
        explanation: "你要求自动归档资料，需要在联网抓取后落盘",
        capabilities: CAPS_WRITE,
        test_summary: "3/3 检查通过",
      },
    },
  },
];

async function main() {
  const browser = await launchBrowser();
  console.log(`对话实例 ${MAIN.base} / 空实例 ${EMPTY.base}`);

  // ---------------------------------------------------------------- 基础阅读
  const s1 = await createSession(browser, { group: GROUP, name: "对话页", theme: "dark", ...MAIN });
  await s1.context.grantPermissions(["clipboard-read", "clipboard-write"]);
  await safe("conv-01-default", "打开对话页（已有历史）", async () => {
    await s1.goto("#/", { waitFor: ".conversation", settle: 900 });
    await s1.shot("conv-01-default", "打开对话页（已有历史）", { note: "基线数据：主话题多轮对话" });
  });
  await safe("conv-02-message-hover", "消息 hover：复制按钮显形", async () => {
    await s1.page.locator(".message.user").first().hover();
    await s1.shotEl(".message.user", "conv-02-message-hover", "消息 hover：复制按钮显形");
  });
  await safe("conv-03-copy-ok", "复制成功反馈（已复制）", async () => {
    await s1.page.locator(".message.assistant .copy-btn").last().click();
    await s1.shotEl(".message.assistant", "conv-03-copy-ok", "复制成功反馈（已复制）", { settle: 120 });
  });
  await safe("conv-04-turn-meta-hover", "轮次分隔头 hover（索引显形）", async () => {
    await s1.page.locator(".turn").last().hover();
    await s1.shotEl(".turn", "conv-04-turn-meta-hover", "轮次分隔头 hover（索引显形）");
  });
  await safe("conv-05-reading-up", "上翻阅读：回到最新消息入口", async () => {
    await s1.page.locator(".stream").evaluate((el) => {
      el.scrollTop = Math.max(0, el.scrollTop - 1400);
    });
    await sleep(700);
    await s1.shot("conv-05-reading-up", "上翻阅读：回到最新消息入口");
  });
  await safe("conv-06-markdown-all", "Markdown 全元素渲染", async () => {
    await s1.goto("#/", { waitFor: ".conversation", settle: 900 });
    await s1.inject("TURN_START", { turn_id: "conv_md_1", revision: 101 });
    await s1.inject("ASSISTANT", { content: MARKDOWN_ALL });
    await sleep(1600);
    await s1.inject("TURN_END", {
      turn_id: "conv_md_1",
      status: "completed",
      final_content: MARKDOWN_ALL,
      revision: 102,
    });
    await sleep(900);
    await s1.shot("conv-06-markdown-all", "Markdown 全元素渲染");
  });
  await safe("conv-07-code-wide", "宽代码块横向滚动与复制按钮", async () => {
    await s1.page.locator(".message.assistant pre").last().scrollIntoViewIfNeeded();
    await sleep(400);
    await s1.shotEl(".message.assistant >> nth=-1", "conv-07-code-wide", "宽代码块（局部）");
  });
  await safe("conv-08-long-answer", "长回答与段落节奏", async () => {
    await s1.inject("TURN_START", { turn_id: "conv_long_1", revision: 103 });
    await s1.inject("ASSISTANT", { content: LONG_ANSWER });
    await sleep(1000);
    await s1.inject("TURN_END", {
      turn_id: "conv_long_1",
      status: "completed",
      final_content: LONG_ANSWER,
      revision: 104,
    });
    await sleep(900);
    await s1.shot("conv-08-long-answer", "长回答与段落节奏");
  });
  await safe("conv-09-skip-link", "键盘 skip-link 聚焦态", async () => {
    await s1.goto("#/", { waitFor: ".conversation", settle: 900 });
    await s1.page.locator("body").click({ position: { x: 5, y: 5 } });
    await s1.press("Tab");
    await s1.shot("conv-09-skip-link", "键盘 skip-link 聚焦态");
  });
  await safe("conv-10-settings-float", "设置悬浮入口", async () => {
    await s1.shotEl(".settings-float", "conv-10-settings-float", "设置悬浮入口", { pad: 16 });
  });
  await safe("conv-11-planet-dock", "星球入口球（压缩态）", async () => {
    await s1.shotEl(DOCK, "conv-11-planet-dock", "星球入口球（压缩态）", { pad: 18 });
  });
  collect(s1);
  flush();

  // ---------------------------------------------------------------- 运行态
  const s2 = await createSession(browser, { group: GROUP, name: "运行态", theme: "dark", ...MAIN });
  await s2.goto("#/", { waitFor: ".conversation", settle: 900 });
  await safe("conv-20-waiting", "等待中：三圆点 + 正在处理", async () => {
    await s2.inject("TURN_START", { turn_id: "conv_run_1", revision: 201 });
    await sleep(700);
    await s2.shot("conv-20-waiting", "等待中：三圆点 + 正在处理");
    await s2.shotEl(".typing", "conv-20b-waiting-crop", "等待指示器（局部）", { pad: 10 });
  });
  await safe("conv-21-generating", "正在生成：流式正文 + 过程标记", async () => {
    await s2.inject("ASSISTANT", { content: LONG_ANSWER });
    await sleep(1200);
    await s2.shot("conv-21-generating", "正在生成：流式正文 + 过程标记");
    // 生成中不额外显示状态行：流式正文本身就是进度
    await s2.shotEl(".message.assistant >> nth=-1", "conv-21b-generating-bubble", "流式正文（局部）", {
      pad: 10,
    });
  });
  await safe("conv-22-tool-running", "正在使用工具：运行中的工具卡", async () => {
    await s2.inject("TOOL_START", {
      call_id: "call_1",
      tool: "read_file",
      turn_id: "conv_run_1",
      arguments: { path: "docs/status.md" },
      presentation: { title: "读取文件", tool: "read_file" },
    });
    await sleep(700);
    await s2.shot("conv-22-tool-running", "正在使用工具：运行中的工具卡");
    await s2.shotEl(".tool-card >> nth=-1", "conv-22b-tool-running-crop", "工具卡：运行中");
  });
  await safe("conv-23-tool-ready", "工具完成：✓ 卡片 + 耗时", async () => {
    await s2.inject("TOOL_END", {
      call_id: "call_1",
      tool: "read_file",
      ok: true,
      duration_ms: 420,
      content_preview: "status.md: M11 已完成，第四阶段进行中。",
      presentation: { title: "读取文件", status: "已完成", summary: "status.md 前 40 行" },
    });
    await sleep(700);
    await s2.shotEl(".tool-card >> nth=-1", "conv-23-tool-ready", "工具卡：已完成", { pad: 8 });
  });
  await safe("conv-24-tool-expanded", "工具卡展开：完整输出", async () => {
    await s2.page.locator(".tool-card").last().locator(".tool-head").click();
    await sleep(500);
    await s2.shotEl(".tool-card >> nth=-1", "conv-24-tool-expanded", "工具卡展开：完整输出", { pad: 8 });
  });
  await safe("conv-25-tool-failed", "工具失败：✕ 标记 + 卡面结论", async () => {
    await s2.inject("TOOL_START", {
      call_id: "call_2",
      tool: "run_shell",
      turn_id: "conv_run_1",
      presentation: { title: "执行命令", tool: "run_shell" },
    });
    await sleep(400);
    await s2.inject("TOOL_END", {
      call_id: "call_2",
      tool: "run_shell",
      ok: false,
      error: "命令退出码 1：找不到目标目录",
      duration_ms: 180,
      content_preview: "",
      presentation: { title: "执行命令", status: "失败" },
    });
    await sleep(700);
    await s2.shotEl(".tool-card >> nth=-1", "conv-25-tool-failed", "工具卡：失败", { pad: 8 });
  });
  await safe("conv-26-subagent-running", "独立任务：进行中", async () => {
    await s2.inject("SUBAGENT_STATUS", {
      task_id: "task_1",
      status: "running",
      display_name: "调研星球的相机聚焦曲线",
      goal: "整理两套聚焦时长方案的差异",
      content_preview: "正在读取 planetContinuum…",
    });
    await sleep(700);
    await s2.shot("conv-26-subagent-running", "独立任务：进行中");
    await s2.shotEl(".subagent-card >> nth=-1", "conv-26b-subagent-running", "独立任务卡：进行中", {
      pad: 8,
    });
  });
  await safe("conv-27-subagent-done", "独立任务：已完成（含结果）", async () => {
    await s2.inject("SUBAGENT_STATUS", {
      task_id: "task_1",
      status: "done",
      display_name: "调研星球的相机聚焦曲线",
      goal: "整理两套聚焦时长方案的差异",
      ok: true,
      content_preview: "结论：2.25 距离下 420ms 最稳，短于 320ms 会读成跳切。",
    });
    await sleep(700);
    await s2.shotEl(".subagent-card >> nth=-1", "conv-27-subagent-done", "独立任务卡：已完成", { pad: 8 });
  });
  await safe("conv-28-subagent-failed", "独立任务：失败", async () => {
    await s2.inject("SUBAGENT_STATUS", {
      task_id: "task_2",
      status: "failed",
      display_name: "批量整理历史片段",
      goal: "给 12 个片段补摘要",
      ok: false,
      error: "其中一段没有可用的消息记录",
    });
    await sleep(700);
    await s2.shotEl(".subagent-card >> nth=-1", "conv-28-subagent-failed", "独立任务卡：失败", { pad: 8 });
  });
  await safe("conv-29-notify", "正在整理独立任务的结果（notify）", async () => {
    await s2.inject("TURN_START", { turn_id: "conv_run_2", notify: true, revision: 202 });
    await sleep(700);
    await s2.shotEl(".typing", "conv-29-notify", "系统轮的状态行（整理独立任务结果）", { pad: 10 });
  });
  await safe("conv-30-queue", "队列 chip：运行中 + 排队中", async () => {
    await s2.inject("TURN_QUEUE", {
      revision: 203,
      running: { turn_id: "conv_run_2", message: "帮我梳理一下这一轮前端稳定化要做的事。" },
      queued: [
        { turn_id: "conv_q_1", message: "把对比数据用表格给我。" },
        { turn_id: "conv_q_2", message: "再给一段很宽的代码示例。" },
      ],
      cancelled: [],
    });
    await sleep(700);
    await s2.shot("conv-30-queue", "队列 chip：运行中 + 排队中");
    await s2.shotEl(".queue", "conv-30b-queue-chip", "队列 chip（折叠）", { pad: 8 });
  });
  await safe("conv-31-queue-expanded", "队列展开：逐条取消", async () => {
    await s2.page.locator(".queue .chip").click();
    await sleep(500);
    await s2.shotEl(".queue", "conv-31-queue-expanded", "队列展开：逐条取消", { pad: 8 });
  });
  await safe("conv-32-queue-cancelled", "队列：已取消项", async () => {
    await s2.inject("TURN_QUEUE", {
      revision: 204,
      running: null,
      queued: [{ turn_id: "conv_q_2", message: "再给一段很宽的代码示例。" }],
      cancelled: [{ turn_id: "conv_q_1", message: "把对比数据用表格给我。" }],
    });
    await sleep(700);
    await s2.shotEl(".queue", "conv-32-queue-cancelled", "队列：已取消项", { pad: 8 });
  });
  await safe("conv-33-continue-bar", "迭代预算耗尽：继续 / 停止条", async () => {
    await s2.inject("APPROVAL_REQUIRED", {
      approval: {
        approval_id: "conv_continue_1",
        kind: "continue",
        payload: { used_iterations: 128, max_iterations: 128 },
      },
    });
    await sleep(700);
    await s2.shotEl(".continue-bar", "conv-33-continue-bar", "迭代预算耗尽：继续 / 停止条", { pad: 10 });
  });
  collect(s2);
  flush();

  // ---------------------------------------------------------------- 工具创建卡
  const s3 = await createSession(browser, { group: GROUP, name: "工具创建", theme: "dark", ...MAIN });
  await s3.goto("#/", { waitFor: ".conversation", settle: 900 });
  await s3.inject("TURN_START", { turn_id: "conv_tc_1", revision: 301 });
  const phases = [
    ["proposal", "提案", "conv-40-tool-create-proposal"],
    ["building", "正在构建", "conv-41-tool-create-building"],
    ["testing", "正在测试", "conv-42-tool-create-testing"],
    ["waiting_approval", "等待你的确认", "conv-43-tool-create-waiting"],
    ["ready", "已创建", "conv-44-tool-create-ready"],
  ];
  for (const [phase, label, id] of phases) {
    await safe(id, `工具创建卡：${label}`, async () => {
      await s3.inject("TOOL_CREATE_STATUS", {
        group_id: "grp_demo",
        phase,
        tool_name: "fetch_doc",
        label,
        detail: "抓取指定文档并写入工作区，方便后续引用。",
        ok: phase === "ready" ? true : undefined,
        turn_id: "conv_tc_1",
      });
      await sleep(600);
      await s3.shotEl(".create-card >> nth=-1", id, `工具创建卡：${label}`, { pad: 8 });
    });
  }
  await safe("conv-45-tool-create-expanded", "工具创建卡：展开详情", async () => {
    await s3.page.locator(".create-card").last().locator(".detail-toggle").click();
    await sleep(500);
    await s3.shotEl(".create-card >> nth=-1", "conv-45-tool-create-expanded", "工具创建卡：展开详情", {
      pad: 8,
    });
  });
  await safe("conv-46-tool-create-failed", "工具创建卡：失败", async () => {
    await s3.inject("TOOL_CREATE_STATUS", {
      group_id: "grp_demo_2",
      phase: "failed",
      tool_name: "csv_tool",
      detail: "测试没有通过：先让工具把测试跑绿再提交",
      ok: false,
      turn_id: "conv_tc_1",
    });
    await sleep(600);
    await s3.shotEl(".create-card >> nth=-1", "conv-46-tool-create-failed", "工具创建卡：失败", { pad: 8 });
  });
  collect(s3);
  flush();

  // ---------------------------------------------------------------- 知识候选卡
  const s4 = await createSession(browser, { group: GROUP, name: "知识候选", theme: "dark", ...MAIN });
  await s4.goto("#/", { waitFor: ".conversation", settle: 900 });
  await safe("conv-50-knowledge-candidate", "知识候选卡（回答完成后出现）", async () => {
    await s4.inject("TURN_START", { turn_id: "conv_kc_1", revision: 401 });
    await s4.inject("KNOWLEDGE_CANDIDATE", {
      knowledge_id: "know_demo_1",
      category: "user_profile",
      content: "用户偏好简洁、不啰嗦的解释，不要长篇大论。",
      reason: "这句话在本次对话里反复出现过",
    });
    await s4.inject("TURN_END", {
      turn_id: "conv_kc_1",
      status: "completed",
      final_content: "记住了。",
      revision: 402,
    });
    await sleep(1000);
    await s4.shotEl(".candidates", "conv-50-knowledge-candidate", "知识候选卡", { pad: 10 });
  });
  await safe("conv-51-knowledge-editing", "知识候选：内联修改态", async () => {
    await s4.page.locator(".candidate .edit-btn").click({ timeout: 8000, force: true });
    await sleep(500);
    await s4.shotEl(".candidate", "conv-51-knowledge-editing", "知识候选：内联修改态", { pad: 8 });
  });
  await safe("conv-52-knowledge-failed", "知识候选：保存失败（保留可重试）", async () => {
    // 这个 knowledge_id 来自测试注入，后端没有对应记录 → 保存必然失败，正好拍到失败态
    await s4.page.locator(".candidate .save").click({ timeout: 8000, force: true });
    await sleep(1500);
    await s4.shotEl(".candidate", "conv-52-knowledge-failed", "知识候选：保存失败", { pad: 8 });
  });
  collect(s4);
  flush();

  // ---------------------------------------------------------------- 通知条家族
  const notices = [
    {
      id: "conv-60-error",
      title: "错误条：本轮执行失败",
      inject: ["ERROR", { message: "这一轮执行失败：模型调用被拒绝（401）", turn_id: "conv_notice" }],
      sel: ".notice.err",
      start: true,
    },
    {
      id: "conv-61-warning",
      title: "警告条：非致命提示",
      inject: ["WARNING", { message: "搜索结果里有 2 条被截断，结论可能不完整。" }],
      sel: ".notice.warn",
    },
    {
      id: "conv-62-cancelled",
      title: "已停止条：取消是正常结局",
      inject: ["TURN_END", { turn_id: "conv_notice", status: "cancelled", revision: 502 }],
      sel: ".notice.quiet",
      start: true,
    },
    {
      id: "conv-63-unavailable",
      title: "模型不可用：TURN_END status=unavailable",
      inject: ["TURN_END", { turn_id: "conv_notice", status: "unavailable", revision: 503 }],
      sel: ".notice",
      start: true,
    },
    {
      id: "conv-64-fallback",
      title: "兼容模式条：模型能力降级",
      inject: ["FALLBACK", { message: "当前模型不支持原生工具调用，已使用兼容模式（功能可能受限）" }],
      sel: ".notice.fallback",
    },
    {
      id: "conv-65-cred-unavailable",
      title: "凭据提示：没有可用凭据",
      inject: ["CREDENTIAL_STATUS", { status: "unavailable" }],
      sel: ".notice.warn",
    },
    {
      id: "conv-66-cred-paused",
      title: "凭据提示：有一项已暂停",
      inject: ["CREDENTIAL_STATUS", { status: "paused" }],
      sel: ".notice.warn",
    },
    {
      id: "conv-67-cred-revoked",
      title: "凭据提示：有一项已失效",
      inject: ["CREDENTIAL_STATUS", { status: "revoked" }],
      sel: ".notice.warn",
    },
    {
      id: "conv-68-resync",
      title: "事件流抖动：正在同步最新状态",
      inject: ["RESYNC", { reason: "subscriber_backlog_overflow" }],
      sel: ".notice.warn",
    },
    {
      id: "conv-69-topic-switch",
      title: "推测切换：转到这里？",
      inject: [
        "TOPIC_SWITCH_SUGGESTED",
        { topic_id: "topic_other", topic_name: "对话深度与迭代预算", reason: "内容看起来属于另一个话题" },
      ],
      sel: ".topic-switch",
    },
  ];
  for (const n of notices) {
    await safe(n.id, n.title, async () => {
      const s = await createSession(browser, { group: GROUP, name: n.title, theme: "dark", ...MAIN });
      await s.goto("#/", { waitFor: ".conversation", settle: 1200 });
      if (n.start) {
        await s.inject("TURN_START", { turn_id: "conv_notice", revision: 501 });
        await sleep(350);
      }
      await s.inject(n.inject[0], n.inject[1]);
      await sleep(800);
      await s.shot(n.id, n.title);
      // 局部特写单独 try：构图问题不该把整条状态判为失败
      try {
        await s.shotEl(n.sel, `${n.id}-crop`, `${n.title}（局部）`, { pad: 8 });
      } catch (err) {
        failures.push({
          id: `${n.id}-crop`,
          title: `${n.title}（局部）`,
          error: String(err?.message ?? err).replace(/\s+/g, " ").slice(0, 160),
        });
      }
      collect(s);
      await s.close({ save: false });
    });
  }
  await safe("conv-70-anchor-historic", "锚点提示：从「XXX」继续", async () => {
    const s = await createSession(browser, { group: GROUP, name: "锚点", theme: "dark", ...MAIN });
    await s.goto("#/", { waitFor: ".conversation", settle: 1200 });
    await s.inject("ANCHOR", {
      topic_id: "topic_other",
      topic_name: "对话深度与迭代预算",
      fragment_id: "frag_old_1",
      fragment_title: "早前讨论：虚拟滚动与跟随底部的判定口径",
      historic: true,
    });
    await sleep(1100);
    await s.shotEl(".composer", "conv-70-anchor-historic", "输入区：从「…」继续", { pad: 10 });
    collect(s);
    await s.close({ save: false });
  });
  await safe("conv-71-history-error", "历史读取失败 + 重试", async () => {
    const s = await createSession(browser, { group: GROUP, name: "历史失败", theme: "dark", ...MAIN });
    await s.page.route("**/api/session/messages*", (route) => route.abort());
    await s.goto("#/", { waitFor: ".conversation", settle: 1200 });
    await s.shot("conv-71-history-error", "历史读取失败 + 重试");
    await s.shotEl(".notice.quiet", "conv-71b-history-error-crop", "历史读取失败（局部）", { pad: 8 });
    collect(s);
    await s.close({ save: false });
  });
  flush();

  // ---------------------------------------------------------------- 审批弹窗
  for (const a of APPROVALS) {
    await safe(a.id, a.title, async () => {
      const s = await createSession(browser, {
        group: GROUP,
        name: a.title,
        theme: a.theme,
        ...MAIN,
      });
      await s.goto("#/", { waitFor: ".conversation", settle: 1000 });
      await s.inject("APPROVAL_REQUIRED", { approval: a.data });
      await s.page.waitForSelector(".modal-mask .modal", { timeout: 10000 });
      await sleep(600);
      await s.shot(a.id, a.title);
      if (a.expand) {
        await s.page.locator(".modal details.adv > summary").click();
        await sleep(450);
        await s.shot(`${a.id}-advanced`, `${a.title}（高级详情展开）`);
      }
      collect(s);
      await s.close({ save: false });
    });
  }
  await safe("conv-86-approval-failed", "审批失败：未做出任何授权", async () => {
    const s = await createSession(browser, { group: GROUP, name: "审批失败", theme: "dark", ...MAIN });
    await s.goto("#/", { waitFor: ".conversation", settle: 1000 });
    await s.inject("APPROVAL_REQUIRED", { approval: APPROVALS[0].data });
    await s.page.waitForSelector(".modal-mask .modal", { timeout: 10000 });
    await s.page.locator(".modal .reject").click();
    await sleep(1400);
    await s.shot("conv-86-approval-failed", "审批失败：弹窗保留 + 未做出任何授权");
    await s.shotEl(".modal", "conv-86b-approval-failed-crop", "审批失败（局部）", { pad: 10 });
    collect(s);
    await s.close({ save: false });
  });
  await safe("conv-87-approval-entry", "审批入口（正在输入时不抢焦点）", async () => {
    const s = await createSession(browser, { group: GROUP, name: "审批入口", theme: "dark", ...MAIN });
    await s.goto("#/", { waitFor: ".conversation", settle: 1000 });
    await s.page.locator("#composer-input").click();
    await s.page.locator("#composer-input").type("我正在写一半的草稿", { delay: 10 });
    await s.inject("APPROVAL_REQUIRED", { approval: APPROVALS[0].data });
    await sleep(900);
    await s.shot("conv-87-approval-entry", "审批入口（正在输入时不抢焦点）");
    await s.shotEl(".approval-entry", "conv-87b-approval-entry-crop", "审批入口条", { pad: 12 });
    await s.page.locator(".approval-entry").click();
    await sleep(800);
    await s.shot("conv-88-approval-reopened", "从入口重新打开审批", { note: "草稿仍在输入框里" });
    collect(s);
    await s.close({ save: false });
  });
  flush();

  // ---------------------------------------------------------------- 浅色主题
  const sLight = await createSession(browser, { group: GROUP, name: "浅色主题", theme: "light", ...MAIN });
  await safe("conv-90-light-default", "浅色主题：对话页", async () => {
    await sLight.goto("#/", { waitFor: ".conversation", settle: 1200 });
    await sLight.shot("conv-90-light-default", "浅色主题：对话页");
  });
  await safe("conv-91-light-generating", "浅色主题：流式生成 + 工具卡", async () => {
    await sLight.inject("TURN_START", { turn_id: "conv_light_1", revision: 601 });
    await sLight.inject("ASSISTANT", { content: LONG_ANSWER });
    await sLight.inject("TOOL_START", {
      call_id: "call_light_1",
      tool: "read_file",
      turn_id: "conv_light_1",
      presentation: { title: "读取文件", tool: "read_file" },
    });
    await sleep(1000);
    await sLight.inject("TOOL_END", {
      call_id: "call_light_1",
      tool: "read_file",
      ok: true,
      duration_ms: 320,
      content_preview: "已完成",
      presentation: { title: "读取文件", status: "已完成" },
    });
    await sleep(700);
    await sLight.shot("conv-91-light-generating", "浅色主题：流式生成 + 工具卡");
  });
  await safe("conv-92-light-error", "浅色主题：错误条", async () => {
    await sLight.inject("ERROR", { message: "这一轮执行失败：模型调用被拒绝（401）" });
    await sleep(700);
    await sLight.shot("conv-92-light-error", "浅色主题：错误条");
  });
  collect(sLight);
  flush();

  // ---------------------------------------------------------------- 窄窗口
  const sNarrow = await createSession(browser, {
    group: GROUP,
    name: "窄窗口",
    theme: "dark",
    viewport: { width: 820, height: 900 },
    ...MAIN,
  });
  await safe("conv-95-narrow-default", "窄窗口：对话页", async () => {
    await sNarrow.goto("#/", { waitFor: ".conversation", settle: 1200 });
    await sNarrow.shot("conv-95-narrow-default", "窄窗口：对话页（820×900）");
  });
  await safe("conv-96-narrow-table", "窄窗口：表格横向滚动", async () => {
    await sNarrow.page.locator(".message.assistant table").last().scrollIntoViewIfNeeded();
    await sleep(500);
    await sNarrow.shot("conv-96-narrow-table", "窄窗口：表格横向滚动");
  });
  await safe("conv-97-narrow-composer-draft", "窄窗口：输入区随草稿增高", async () => {
    const draft =
      "帮我把这一轮的验证结论整理成三条：\n1. 发送立刻有反馈；\n2. 状态一直可辨；\n3. 阅读位置不丢。\n再补一句为什么动画要短。\n还要说明失败时草稿会回到输入框。";
    await sNarrow.page.fill("#composer-input", draft);
    await sleep(500);
    await sNarrow.shotEl(".composer", "conv-97-narrow-composer-draft", "窄窗口：输入区随草稿增高", {
      pad: 10,
    });
  });
  await safe("conv-98-narrow-approval", "窄窗口：审批弹窗", async () => {
    await sNarrow.inject("APPROVAL_REQUIRED", { approval: APPROVALS[0].data });
    await sNarrow.page.waitForSelector(".modal-mask .modal", { timeout: 10000 });
    await sleep(800);
    await sNarrow.shot("conv-98-narrow-approval", "窄窗口：审批弹窗");
  });
  collect(sNarrow);
  flush();

  const sTiny = await createSession(browser, {
    group: GROUP,
    name: "超窄窗口",
    theme: "dark",
    viewport: { width: 620, height: 860 },
    ...MAIN,
  });
  await safe("conv-99-tiny-default", "超窄窗口：对话页（620×860）", async () => {
    await sTiny.goto("#/", { waitFor: ".conversation", settle: 1200 });
    await sTiny.shot("conv-99-tiny-default", "超窄窗口：对话页（620×860）");
  });
  collect(sTiny);
  flush();

  // ---------------------------------------------------------------- 首次启动（空数据目录）
  const emptyReachable = await fetch(`${EMPTY.api}/api/health`)
    .then((r) => r.ok)
    .catch(() => false);
  if (!emptyReachable) {
    failures.push({
      id: "conv-100-first-run-empty",
      title: "首次启动：空对话页",
      error: `空实例未启动：${EMPTY.api} 不可达（先跑 instance.py up --name empty --backend-port 8838 --frontend-port 6203 --clean）`,
    });
  } else {
    const sEmpty = await createSession(browser, { group: GROUP, name: "首次启动", theme: "dark", ...EMPTY });
    await safe("conv-100-first-run-empty", "首次启动：空对话页", async () => {
      await sEmpty.goto("#/", { waitFor: ".conversation", settle: 1600 });
      await sEmpty.shot("conv-100-first-run-empty", "首次启动：空对话页", {
        note: "空数据目录：尚无话题、无消息",
      });
    });
    await safe("conv-101-first-run-empty-crop", "首次启动：空状态特写", async () => {
      await sEmpty.shotEl(".empty", "conv-101-first-run-empty-crop", "首次启动：空状态特写", { pad: 14 });
    });
    await safe("conv-102-first-run-send-fails", "首次启动：发送后没有可用凭据", async () => {
      await sEmpty.page.fill("#composer-input", "你好，先做个自我介绍。");
      await sEmpty.page.locator(".send-btn").click();
      await sleep(3000);
      await sEmpty.shot("conv-102-first-run-send-fails", "首次启动：发送后没有可用凭据", {
        note: "没有凭据时发送的真实结果：错误/警告条 + 草稿回到输入框",
      });
    });
    collect(sEmpty);
    await sEmpty.close({ save: false });
  }
  flush();

  await browser.close();

  console.log(`\n合计 ${all.length} 张；失败 ${failures.length} 项。`);
  for (const f of failures) console.log(`  - ${f.id} ${f.title} :: ${f.error}`);
  if (failures.length) {
    const { writeFileSync } = await import("node:fs");
    writeFileSync(
      new URL("./.conv-root-failures.json", import.meta.url),
      JSON.stringify(failures, null, 2),
      "utf-8",
    );
  }
}

await main();
