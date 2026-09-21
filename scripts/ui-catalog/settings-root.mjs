/**
 * 分组 `settings-root`：设置页（7 分区）+ 凭据管理 + 确认层 + 调试页。
 *
 * 用隔离实例 `setroot`（8840/6205，已播种基线数据：4 张不同状态的凭据卡、
 * 知识/实体/空话题、长标题话题）。
 *
 * 调试页两种状态：
 * - 「无 trace」是本地真实状态（隔离数据目录里没有跑的轮次）；
 * - 「有 trace / 详情」用 Playwright 拦截 `/api/traces*` 返回**构造的数据**来渲染 ——
 *   本地没有任何真实 trace，而造一条真实 trace 需要真实模型密钥（项目约定禁止）。
 *   图册与索引里会标明这一点，不冒充真实数据。
 *
 * 用法：
 *   python scripts/ui-catalog/instance.py up --name setroot --backend-port 8840 --frontend-port 6205 --seed --clean
 *   $env:QIO_BASE="http://127.0.0.1:6205"; $env:QIO_API="http://127.0.0.1:8840"
 *   node scripts/ui-catalog/settings-root.mjs
 */
import {
  API as ENV_API,
  BASE as ENV_BASE,
  createSession,
  launchBrowser,
  saveManifest,
  sleep,
} from "./lib.mjs";

const INSTANCE = { base: ENV_BASE, api: ENV_API };
const GROUP = "settings-root";
const SECTIONS = ["外观", "对话与记忆", "模型与联网", "工具与权限", "数据与维护", "凭据", "高级"];

const all = [];
const failures = [];

async function safe(id, title, fn) {
  try {
    await fn();
  } catch (err) {
    const message = String(err?.message ?? err).replace(/\s+/g, " ").slice(0, 200);
    failures.push({ id, title, error: message });
    console.log(`  [FAIL] ${id} ${title} :: ${message}`);
  }
}

function flush() {
  saveManifest(GROUP, all);
}

/** 切到某个分区并把设置页自己的滚动容器拉回顶部 */
async function openSection(s, label) {
  await s.page.locator(".nav .tab", { hasText: label }).first().click();
  await sleep(320);
  await s.page.locator(".settings").evaluate((el) => {
    el.scrollTop = 0;
  });
  await sleep(220);
}

async function scrollSettings(s, top) {
  await s.page.locator(".settings").evaluate((el, t) => {
    el.scrollTop = t;
  }, top);
  await sleep(260);
}

/**
 * 关掉当前弹层。
 *
 * 注意：凭据弹窗**不响应 Esc**（只有点击遮罩或 × 才关），所以这里必须点 ×，
 * 不能只按 Esc —— 之前就是因为漏关弹窗，后面的点击全被遮罩挡住（13 项连环失败）。
 * 对 QConfirm layer 档，Esc 是有效的（它会自己收回）。
 */
async function closeModal(s) {
  const mask = s.page.locator(".modal-mask");
  if (await mask.count()) {
    const close = s.page.locator(".modal .close").first();
    if (await close.count()) await close.click({ timeout: 5000 }).catch(() => {});
    else await mask.first().click({ position: { x: 8, y: 8 }, timeout: 5000 }).catch(() => {});
    await mask.first().waitFor({ state: "detached", timeout: 6000 }).catch(() => {});
  }
  const layer = s.page.locator(".qio-confirm-scrim");
  if (await layer.count()) {
    await s.press("Escape");
    await layer.first().waitFor({ state: "detached", timeout: 6000 }).catch(() => {});
  }
  await sleep(300);
}

/**
 * 设置页所有分区都在 DOM 里（非当前分区是 `display:none`），所以「第几个 .qio-select」
 * 这种写法会选中隐藏分区里的元素 → 点击永远不可用。统一限定在当前可见分区里。
 */
const vis = (selector) => `.panel:visible ${selector}`;

/** 打开某张凭据卡的「管理」动作区；已经打开时不要再点（再点会收起，动作按钮就变成 inert） */
async function openManage(s, index) {
  const isOpen = await s.page.evaluate((i) => {
    const card = document.querySelectorAll(".cred-card")[i];
    return !!card?.querySelector(".manage")?.classList.contains("open");
  }, index);
  if (!isOpen) {
    await s.page.locator(".cred-card .btn-manage").nth(index).click({ timeout: 8000 });
    await sleep(400);
  }
}

const TRACE_LIST = [
  {
    turn_id: "turn_a1f9c3",
    status: "done",
    started_at: "2026-09-21T06:12:41.000Z",
    ended_at: "2026-09-21T06:12:47.000Z",
    duration_ms: 6120,
    initial_topic: "基线-前端稳定化长对话与阅读连续性",
    final_topic: "基线-前端稳定化长对话与阅读连续性",
    error: null,
  },
  {
    turn_id: "turn_77b2e0",
    status: "failed",
    started_at: "2026-09-21T06:20:03.000Z",
    ended_at: "2026-09-21T06:20:04.000Z",
    duration_ms: 940,
    initial_topic: "基线-审批失败后的重试路径",
    final_topic: null,
    error: "模型调用被拒绝（401）",
  },
  {
    turn_id: "turn_c4d81a",
    status: "cancelled",
    started_at: "2026-09-21T06:31:19.000Z",
    ended_at: "2026-09-21T06:31:26.000Z",
    duration_ms: 7010,
    initial_topic: "基线-任务排队与取消的状态归属",
    final_topic: "基线-任务排队与取消的状态归属",
    error: null,
  },
];

const TRACE_DETAIL = {
  ...TRACE_LIST[0],
  topic: {
    decision: "continue_current_topic",
    confidence: 0.86,
    candidates: [
      { topic: "基线-前端稳定化长对话与阅读连续性", score: 0.86 },
      { topic: "基线-审批失败后的重试路径", score: 0.11 },
    ],
  },
  injection: {
    fragments: ["早前讨论：虚拟滚动与跟随底部的判定口径"],
    knowledge: ["知识页默认筛选为已启用"],
    memory_budget: 4096,
  },
  model_calls: [
    { model: "gpt-4o-mini", tokens_in: 1840, tokens_out: 420, latency_ms: 2210 },
    { model: "gpt-4o-mini", tokens_in: 2410, tokens_out: 180, latency_ms: 1650 },
  ],
  tool_runs: [{ tool: "read_file", ok: true, duration_ms: 420, args: { path: "docs/status.md" } }],
  writes: { fragments: 0, knowledge: 1, entities: 2 },
  warnings: [],
  final_preview: "## 这一轮做了什么\n把「操作有没有被接住」落到三个动作上：发送立刻有反馈、状态一直可辨、位置不丢。",
};

async function main() {
  const browser = await launchBrowser();

  // ---------------------------------------------------------------- 设置页（dark）
  const sDark = await createSession(browser, { group: GROUP, name: "设置页", theme: "dark", ...INSTANCE });
  await sDark.goto("#/settings", { waitFor: ".settings", settle: 900 });

  for (const label of SECTIONS) {
    const id = `set-${SECTIONS.indexOf(label) + 1}-${label}`;
    await safe(id, `设置页：${label}`, async () => {
      await openSection(sDark, label);
      await sDark.shot(`settings-dark-${SECTIONS.indexOf(label) + 1}-${label}`, `设置页 · ${label}`, {
        note: "整屏（设置页自己的滚动容器在顶部）",
      });
    });
  }
  // 凭据分区较长：补一张下半屏（四张卡 + 筛选）
  await safe("set-cred-lower", "设置页 · 凭据（下半屏：四张状态卡）", async () => {
    await openSection(sDark, "凭据");
    await scrollSettings(sDark, 640);
    await sDark.shot("settings-dark-cred-cards", "设置页 · 凭据（四张状态卡）", {
      note: "生效中 / 预算 90% 警告 / 已停用 / 已撤销",
    });
  });
  await safe("set-cred-expanded", "设置页 · 凭据卡展开（详情 + 管理动作）", async () => {
    await sDark.page.locator(".cred-card .btn-detail").first().click();
    await sleep(360);
    await openManage(sDark, 0);
    await sDark.shotEl(".cred-card", "settings-cred-expanded", "凭据卡：展开 + 管理动作", { pad: 10 });
  });
  await safe("set-cred-filter-disabled", "设置页 · 凭据筛选：已停用（空结果文案）", async () => {
    await sDark.page.locator(".filters .filter", { hasText: "已停用" }).click();
    await sleep(360);
    await sDark.shot("settings-dark-cred-filter-disabled", "设置页 · 凭据筛选：已停用");
  });
  await safe("set-cred-filter-all", "设置页 · 凭据筛选：全部", async () => {
    await sDark.page.locator(".filters .filter", { hasText: "全部" }).click();
    await sleep(360);
    await sDark.shot("settings-dark-cred-filter-all", "设置页 · 凭据筛选：全部（四种状态同屏）");
  });
  await safe("set-cred-confirm-revoke", "设置页 · 撤销凭据确认层（QConfirm layer）", async () => {
    await openManage(sDark, 1);
    await sDark.page.locator(".cred-card .btn-revoke").nth(1).click();
    await sDark.page.waitForSelector(".qio-confirm--layer", { timeout: 8000 });
    await sleep(400);
    await sDark.shot("settings-confirm-revoke", "设置页 · 撤销凭据确认层");
    await closeModal(sDark);
  });
  await safe("set-cred-confirm-delete", "设置页 · 删除凭据确认层（危险档）", async () => {
    await openManage(sDark, 1);
    await sDark.page.locator(".cred-card .btn-danger").nth(1).click();
    await sDark.page.waitForSelector(".qio-confirm--layer", { timeout: 8000 });
    await sleep(400);
    await sDark.shot("settings-confirm-delete", "设置页 · 删除凭据确认层");
    await closeModal(sDark);
  });
  await safe("set-cred-toast", "设置页 · 测试连接失败 toast", async () => {
    await openManage(sDark, 0);
    await sDark.page.locator(".cred-card .btn-test").first().click();
    await sleep(1600);
    await sDark.shot("settings-toast-test", "设置页 · 测试连接失败 toast");
  });
  await safe("set-cred-modal-create", "设置页 · 新建凭据弹窗", async () => {
    await openSection(sDark, "凭据");
    await sDark.page.locator(".new-cred").click();
    await sDark.page.waitForSelector(".modal-mask .modal", { timeout: 8000 });
    await sleep(500);
    await sDark.shot("settings-modal-create", "设置页 · 新建凭据弹窗");
    await closeModal(sDark);
  });
  await safe("set-cred-modal-meta", "设置页 · 编辑凭据信息弹窗", async () => {
    await closeModal(sDark);
    await openManage(sDark, 0);
    await sDark.page.locator(".cred-card .btn-meta").first().click();
    await sDark.page.waitForSelector(".modal-mask .modal", { timeout: 8000 });
    await sleep(500);
    await sDark.shot("settings-modal-meta", "设置页 · 编辑凭据信息弹窗");
    await closeModal(sDark);
  });
  await safe("set-cred-modal-rotate", "设置页 · 换钥弹窗", async () => {
    await closeModal(sDark);
    await openManage(sDark, 0);
    await sDark.page.locator(".cred-card .btn-rotate").first().click();
    await sDark.page.waitForSelector(".modal-mask .modal", { timeout: 8000 });
    await sleep(500);
    await sDark.shot("settings-modal-rotate", "设置页 · 换钥弹窗");
    await closeModal(sDark);
  });
  await safe("set-chat-custom", "设置页 · 对话与记忆：自定义轮数 QNumber", async () => {
    await openSection(sDark, "对话与记忆");
    await sDark.page.locator(vis(".qio-select")).first().click();
    await sleep(300);
    await sDark.page.locator(".qio-select-menu .opt", { hasText: "自定义" }).click();
    await sleep(500);
    await sDark.shot("settings-chat-custom", "设置页 · 对话与记忆：自定义轮数");
  });
  await safe("set-chat-invalid", "设置页 · 对话与记忆：越界校验提示", async () => {
    const input = sDark.page.locator(vis(".q-number-input")).first();
    await input.fill("31");
    await input.press("Enter");
    await sleep(700);
    await sDark.shot("settings-chat-invalid", "设置页 · 对话与记忆：越界校验提示");
  });
  await safe("set-model-bocha-edit", "设置页 · 模型与联网：替换博查 Key", async () => {
    await openSection(sDark, "模型与联网");
    await sDark.page.locator(vis(".qio-btn"), { hasText: "替换" }).first().click();
    await sleep(420);
    await sDark.shot("settings-model-bocha-edit", "设置页 · 模型与联网：替换博查 Key");
  });
  await safe("set-model-save", "设置页 · 模型与联网：保存后的反馈", async () => {
    await sDark.page.locator(vis(".qio-btn"), { hasText: "保存搜索配置" }).first().click();
    await sleep(900);
    await sDark.shot("settings-model-save", "设置页 · 模型与联网：保存后的反馈");
  });
  await safe("set-tools-select-open", "设置页 · 工具与权限：权限模式下拉打开", async () => {
    await openSection(sDark, "工具与权限");
    await sDark.page.locator(vis(".qio-select")).first().click();
    await sleep(360);
    await sDark.shot("settings-tools-select-open", "设置页 · 工具与权限：权限模式下拉打开");
    await sDark.press("Escape");
  });
  await safe("set-data-toggle", "设置页 · 数据与维护：开关与间隔", async () => {
    await openSection(sDark, "数据与维护");
    await sDark.page.locator(vis(".qio-switch")).first().click();
    await sleep(600);
    await sDark.shot("settings-data-toggle", "设置页 · 数据与维护：离线维护开关");
  });
  await safe("set-appearance-switch", "设置页 · 外观：切到浅色后的分区反馈", async () => {
    await openSection(sDark, "外观");
    await sDark.page.locator(".theme-opt", { hasText: "净白" }).click();
    await sleep(800);
    await sDark.shot("settings-appearance-light", "设置页 · 外观：切到净白（分区反馈）");
    await sDark.page.locator(".theme-opt", { hasText: "暗紫晶" }).click();
    await sleep(600);
  });
  await safe("set-window-hide", "设置页 · 外观：窗口行为与贴边隐藏开关", async () => {
    await scrollSettings(sDark, 900);
    await sDark.page.locator(vis(".qio-switch")).first().click();
    await sleep(500);
    await sDark.shot("settings-window-hide", "设置页 · 外观：窗口行为");
  });
  await safe("set-advanced-fold", "设置页 · 高级：SearXNG 折叠展开", async () => {
    await openSection(sDark, "高级");
    await sDark.page.locator(vis(".adv-fold > summary")).first().click();
    await sleep(420);
    await sDark.shot("settings-advanced-fold", "设置页 · 高级：SearXNG 折叠展开");
  });
  await safe("set-advanced-devmode", "设置页 · 高级：开发者模式开启", async () => {
    await sDark.page.locator(vis(".qio-switch")).first().click();
    await sleep(500);
    await sDark.shot("settings-advanced-devmode", "设置页 · 高级：开发者模式开启");
    await sDark.page.locator(vis(".qio-switch")).first().click();
    await sleep(400);
  });
  all.push(...sDark.entries.splice(0));
  flush();
  await sDark.close({ save: false });

  // ---------------------------------------------------------------- 设置页（light）
  const sLight = await createSession(browser, { group: GROUP, name: "设置页浅色", theme: "light", ...INSTANCE });
  await sLight.goto("#/settings", { waitFor: ".settings", settle: 900 });
  for (const label of SECTIONS) {
    const n = SECTIONS.indexOf(label) + 1;
    await safe(`set-light-${n}`, `设置页浅色：${label}`, async () => {
      await openSection(sLight, label);
      await sLight.shot(`settings-light-${n}-${label}`, `设置页浅色 · ${label}`);
    });
  }
  await safe("set-light-cred-cards", "设置页浅色 · 凭据（四张状态卡）", async () => {
    await openSection(sLight, "凭据");
    await sLight.page.locator(".filters .filter", { hasText: "全部" }).click();
    await scrollSettings(sLight, 560);
    await sLight.shot("settings-light-cred-cards", "设置页浅色 · 凭据（四张状态卡）");
  });
  all.push(...sLight.entries.splice(0));
  flush();
  await sLight.close({ save: false });

  // ---------------------------------------------------------------- 设置页（窄窗口）
  const sNarrow = await createSession(browser, {
    group: GROUP,
    name: "设置页窄窗口",
    theme: "dark",
    viewport: { width: 620, height: 860 },
    ...INSTANCE,
  });
  await safe("set-narrow", "设置页 · 窄窗口（620×860）", async () => {
    await sNarrow.goto("#/settings", { waitFor: ".settings", settle: 1000 });
    await sNarrow.shot("settings-narrow", "设置页 · 窄窗口（620×860）");
    await openSection(sNarrow, "凭据");
    await sNarrow.shot("settings-narrow-cred", "设置页 · 窄窗口：凭据分区");
  });
  all.push(...sNarrow.entries.splice(0));
  flush();
  await sNarrow.close({ save: false });

  // ---------------------------------------------------------------- 调试页
  const sDebug = await createSession(browser, { group: GROUP, name: "调试页", theme: "dark", ...INSTANCE });
  await safe("debug-empty", "调试页：还没有 trace", async () => {
    await sDebug.goto("#/debug", { waitFor: ".debug", settle: 900 });
    await sDebug.shot("debug-empty", "调试页：还没有 trace", {
      note: "隔离数据目录里没有跑过真实轮次 → 真实空状态",
    });
  });
  await sDebug.close({ save: false });

  const sDebugMock = await createSession(browser, {
    group: GROUP,
    name: "调试页（构造数据）",
    theme: "dark",
    ...INSTANCE,
  });
  await sDebugMock.page.route("**/api/traces*", async (route) => {
    const url = route.request().url();
    if (/\/api\/traces\/[^/?]+$/.test(url)) {
      await route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(TRACE_DETAIL) });
      return;
    }
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({ traces: TRACE_LIST, total: 23, limit: 20, offset: 0 }),
    });
  });
  await safe("debug-list", "调试页：Turn 列表（构造数据）", async () => {
    await sDebugMock.goto("#/debug", { waitFor: ".debug", settle: 1100 });
    await sDebugMock.shot("debug-list", "调试页：Turn 列表（构造数据）", {
      note: "本地没有真实 trace：这里用拦截 /api/traces 的构造数据渲染，仅用于展示界面本身",
    });
  });
  await safe("debug-detail", "调试页：选中一条 Turn 的决策链路（构造数据）", async () => {
    await sDebugMock.page.locator(".row").first().click();
    await sleep(600);
    await sDebugMock.shot("debug-detail", "调试页：Turn 决策链路（构造数据）");
  });
  await safe("debug-detail-open", "调试页：展开各项折叠块（构造数据）", async () => {
    for (const summary of ["上下文注入", "Model 调用", "工具调用", "记忆 / 知识写入"]) {
      const loc = sDebugMock.page.locator("summary", { hasText: summary }).first();
      if (await loc.count()) await loc.click({ timeout: 4000 }).catch(() => {});
      await sleep(180);
    }
    await sleep(400);
    await sDebugMock.shot("debug-detail-open", "调试页：展开各项折叠块（构造数据）");
  });
  await safe("debug-toggle-off", "调试页：记录开关", async () => {
    await sDebugMock.page.locator(".qio-btn", { hasText: /记录(已开启|已关闭)/ }).first().click();
    await sleep(700);
    await sDebugMock.shot("debug-toggle-off", "调试页：记录开关（切换后）");
  });
  all.push(...sDebugMock.entries.splice(0));
  flush();
  await sDebugMock.close({ save: false });

  await browser.close();
  console.log(`\n合计 ${all.length} 张；失败 ${failures.length} 项。`);
  for (const f of failures) console.log(`  - ${f.id} ${f.title} :: ${f.error}`);
}

await main();
