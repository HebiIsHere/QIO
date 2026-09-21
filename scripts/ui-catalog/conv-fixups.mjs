/**
 * 补拍 `conv-root` 里失败的几张，并把结果合并回 manifest-conv-root.json。
 *
 * 每个补拍项都写清「为什么第一次没拍到」，避免把它变成「重试到拍出来为止」的玄学：
 * - conv-46：元素在视口外 → clip 拍成空白（shotEl 已修：先滚进视口）
 * - conv-52：知识候选卡的保存按钮要等编辑态真正渲染出来
 * - conv-61：加载历史会清掉加载途中收到的警告 → 必须等历史读完之后再注入
 * - conv-69：底部固定条在视口边缘 → 用元素自身截图，不用带 pad 的 clip
 * - conv-98：输入框有焦点时审批不自动弹（这是产品行为）→ 先让输入框失焦
 *
 * 用法（服务需在跑）：
 *   $env:QIO_BASE="http://127.0.0.1:6204"; $env:QIO_API="http://127.0.0.1:8839"
 *   node scripts/ui-catalog/conv-fixups.mjs
 */
import { readFileSync, writeFileSync } from "node:fs";
import {
  API as ENV_API,
  BASE as ENV_BASE,
  createSession,
  launchBrowser,
  saveManifest,
  sleep,
} from "./lib.mjs";

const MAIN = { base: ENV_BASE, api: ENV_API };
const GROUP = "conv-root";
const MANIFEST = new URL("../../frontend/e2e-shots/ui-catalog/manifest-conv-root.json", import.meta.url);
const CAPS_WRITE = [
  "联网：是（限 api.example.com）",
  "读取文件：是",
  "写入文件：是",
  "启动进程：否",
  "使用凭据：无",
  "副作用：write",
];

const browser = await launchBrowser();
const newEntries = [];
const failures = [];

async function attempt(id, title, fn) {
  try {
    await fn();
    console.log(`  [ok] ${id}`);
  } catch (err) {
    const message = String(err?.message ?? err).replace(/\s+/g, " ").slice(0, 200);
    failures.push({ id, title, error: message });
    console.log(`  [FAIL] ${id} :: ${message}`);
  }
}

// ---- conv-46：工具创建失败（元素在视口外，先滚进来）
await attempt("conv-46-tool-create-failed", "工具创建卡：失败", async () => {
  const s = await createSession(browser, { group: GROUP, name: "补拍", theme: "dark", ...MAIN });
  await s.goto("#/", { waitFor: ".conversation", settle: 1000 });
  await s.inject("TURN_START", { turn_id: "conv_fix_1", revision: 901 });
  await s.inject("TOOL_CREATE_STATUS", {
    group_id: "grp_demo_2",
    phase: "failed",
    tool_name: "csv_tool",
    detail: "测试没有通过：先让工具把测试跑绿再提交",
    ok: false,
    turn_id: "conv_fix_1",
  });
  await sleep(700);
  newEntries.push(await s.shotEl(".create-card >> nth=-1", "conv-46-tool-create-failed", "工具创建卡：失败", { pad: 8 }));
  await s.close({ save: false });
});

// ---- conv-51/52：知识候选 —— 修改态与保存失败
await attempt("conv-52-knowledge-failed", "知识候选：保存失败（保留可重试）", async () => {
  const s = await createSession(browser, { group: GROUP, name: "补拍", theme: "dark", ...MAIN });
  await s.goto("#/", { waitFor: ".conversation", settle: 1200 });
  await s.inject("TURN_START", { turn_id: "conv_fix_2", revision: 902 });
  await s.inject("KNOWLEDGE_CANDIDATE", {
    knowledge_id: "know_fixup_1",
    category: "user_profile",
    content: "用户偏好简洁、不啰嗦的解释，不要长篇大论。",
    reason: "这句话在本次对话里反复出现过",
  });
  await s.inject("TURN_END", {
    turn_id: "conv_fix_2",
    status: "completed",
    final_content: "记住了。",
    revision: 903,
  });
  // 后端事件总线会把最近的事件重放给新连接（产品行为），所以新页面里可能同时有
  // 之前注入过的候选卡：这里只看最新那一条
  const card = s.page.locator(".candidate").first();
  await card.waitFor({ state: "visible", timeout: 10000 });
  await sleep(500);
  /**
   * 这里必须 force：实测（1440×900、只有一张候选卡时）「保存 / 修改 / 忽略」
   * 被底部悬浮输入气泡盖住 —— 候选卡 y 750~900，输入气泡 761~885，
   * 按钮中心的 elementFromPoint 是 .composer 里的 textarea。这是**产品问题**，
   * 不是采集问题，所以留一张证据图，再用强制点击把后面的状态拍出来。
   */
  const geometry = await s.page.evaluate(() => {
    const cardEl = [...document.querySelectorAll(".candidate")].pop();
    const btn = cardEl?.querySelector(".edit-btn");
    const r = btn?.getBoundingClientRect();
    const c = document.querySelector(".composer")?.getBoundingClientRect();
    const at = r ? document.elementFromPoint(r.left + r.width / 2, r.top + r.height / 2) : null;
    return {
      btnBottom: r ? Math.round(r.bottom) : null,
      composerTop: c ? Math.round(c.y) : null,
      covering: at ? at.tagName : null,
    };
  });
  newEntries.push(
    await s.shot("conv-53-candidate-covered", "知识候选卡被输入气泡遮挡（采集发现的问题）", {
      note: `候选卡按钮 bottom=${geometry.btnBottom}px，输入气泡 top=${geometry.composerTop}px，按钮中心的命中元素=${geometry.covering}（应为 BUTTON）`,
    }),
  );
  // 用 DOM 事件直接派发：卡片被输入气泡盖住时，Playwright 的命中测试会一直不通过，
  // 而这里要看的是卡片自己的三种状态（点击路径的问题已经单独留证）
  await s.page.evaluate(() => {
    const el = document.querySelector(".candidate .edit-btn");
    if (el) el.click();
  });
  await s.page.waitForSelector(".candidate .save", { state: "visible", timeout: 10000 });
  await sleep(400);
  newEntries.push(
    await s.shotEl(".candidate >> nth=0", "conv-51-knowledge-editing", "知识候选：内联修改态", { pad: 8 }),
  );
  // 注入出来的 knowledge_id 后端没有对应记录 → 保存必然失败，正好拍到「失败可重试」
  await s.page.evaluate(() => {
    const el = document.querySelector(".candidate .save");
    if (el) el.click();
  });
  await s.page.waitForSelector(".candidate .err", { state: "visible", timeout: 10000 });
  await sleep(400);
  newEntries.push(
    await s.shotEl(".candidate >> nth=0", "conv-52-knowledge-failed", "知识候选：保存失败", { pad: 8 }),
  );
  await s.close({ save: false });
});

// ---- conv-61：警告条（等历史读完再注入，否则会被加载过程清掉）
await attempt("conv-61-warning", "警告条：非致命提示", async () => {
  const s = await createSession(browser, { group: GROUP, name: "补拍", theme: "dark", ...MAIN });
  await s.goto("#/", { waitFor: ".conversation", settle: 2200 });
  await s.inject("WARNING", { message: "搜索结果里有 2 条被截断，结论可能不完整。" });
  await s.page.waitForSelector(".notice.warn", { timeout: 8000 });
  await sleep(500);
  newEntries.push(await s.shot("conv-61-warning", "警告条：非致命提示"));
  newEntries.push(await s.shotEl(".notice.warn", "conv-61-warning-crop", "警告条（局部）", { pad: 8 }));
  await s.close({ save: false });
});

// ---- conv-69：推测切换条（底部固定条，用元素自身截图）
await attempt("conv-69-topic-switch", "推测切换：转到这里？", async () => {
  const s = await createSession(browser, { group: GROUP, name: "补拍", theme: "dark", ...MAIN });
  await s.goto("#/", { waitFor: ".conversation", settle: 2200 });
  await s.inject("TOPIC_SWITCH_SUGGESTED", {
    topic_id: "topic_other",
    topic_name: "对话深度与迭代预算",
    reason: "内容看起来属于另一个话题",
  });
  await s.page.waitForSelector(".topic-switch", { timeout: 8000 });
  await sleep(600);
  newEntries.push(await s.shot("conv-69-topic-switch", "推测切换：转到这里？"));
  // 底部固定条贴着视口边缘，元素级截图会拍到空白：只保留整屏那张（局部特写不补）
  await s.close({ save: false });
});

// ---- conv-98：窄窗口审批（先让输入框失焦，否则审批只亮入口不弹窗）
await attempt("conv-98-narrow-approval", "窄窗口：审批弹窗", async () => {
  const s = await createSession(browser, {
    group: GROUP,
    name: "补拍",
    theme: "dark",
    viewport: { width: 820, height: 900 },
    ...MAIN,
  });
  await s.goto("#/", { waitFor: ".conversation", settle: 1400 });
  await s.page.evaluate(() => {
    const active = document.activeElement;
    if (active && typeof active.blur === "function") active.blur();
  });
  await s.inject("APPROVAL_REQUIRED", {
    approval: {
      approval_id: "conv_fix_ap",
      kind: "tool_create",
      payload: {
        name: "fetch_doc",
        description: "抓取指定文档并写入工作区",
        explanation: "你要求自动归档资料，需要在联网抓取后落盘",
        capabilities: CAPS_WRITE,
        test_summary: "3/3 检查通过",
      },
    },
  });
  await s.page.waitForSelector(".modal-mask .modal", { timeout: 10000 });
  await sleep(700);
  newEntries.push(await s.shot("conv-98-narrow-approval", "窄窗口：审批弹窗"));
  await s.close({ save: false });
});

// ---- conv-100/103：空数据目录的首次启动（星球懒加载会把首屏盖住一会儿，
//      既拍「正在打开星球…」这个真实瞬间，也拍它结束之后的干净的首次启动画面）
const EMPTY = { base: "http://127.0.0.1:6203", api: "http://127.0.0.1:8838" };
await attempt("conv-103-first-run-planet-boot", "首次启动：星球正在打开（懒加载占位）", async () => {
  const s = await createSession(browser, { group: GROUP, name: "补拍", theme: "dark", ...EMPTY });
  await s.page.goto(`${EMPTY.base}/?fresh=fixup-boot#/`);
  await s.page.waitForSelector(".planet-boot", { timeout: 15000 });
  newEntries.push(
    await s.shot("conv-103-first-run-planet-boot", "首次启动：星球正在打开（懒加载占位）", {
      note: "空数据目录首屏：对话页已经渲染，星球场景在后台懒加载",
    }),
  );
  await s.close({ save: false });
});

await attempt("conv-100-first-run-empty", "首次启动：空对话页", async () => {
  const s = await createSession(browser, { group: GROUP, name: "补拍", theme: "dark", ...EMPTY });
  await s.goto("#/", { waitFor: ".conversation", settle: 1500 });
  // 等星球懒加载的占位层退场，否则拍到的是被它盖住的中间帧
  await s.page.waitForFunction(() => !document.querySelector(".planet-boot"), null, { timeout: 20000 });
  await sleep(600);
  newEntries.push(
    await s.shot("conv-100-first-run-empty", "首次启动：空对话页", {
      note: "空数据目录：尚无话题、无消息",
    }),
  );
  newEntries.push(
    await s.shotEl(".empty", "conv-101-first-run-empty-crop", "首次启动：空状态特写", { pad: 14 }),
  );
  await s.close({ save: false });
});

await browser.close();

// 合并进已有 manifest：同 id 覆盖，其余保留
const existing = JSON.parse(readFileSync(MANIFEST, "utf-8"));
const byId = new Map(existing.entries.map((e) => [e.id, e]));
for (const entry of newEntries) byId.set(entry.id, entry);
const merged = [...byId.values()];
saveManifest(GROUP, merged);

console.log(`\n补拍成功 ${newEntries.length} 张；仍失败 ${failures.length} 项。`);
for (const f of failures) console.log(`  - ${f.id} :: ${f.error}`);
