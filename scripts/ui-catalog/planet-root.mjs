/**
 * 分组 `planet-root`：星球入口球 + 全屏星球层（话题 / 知识 / 实体）。
 *
 * 用隔离实例 `planetroot`（8841/6206，已播种基线数据）。
 * 无头环境是软件渲染（swiftshader），WebGL 冷启动 1.5–3s：
 * 每次「打开星球」都要等场景真的画出来再截，否则会拍到空白球。
 *
 * 两个需要说明的状态：
 * - **WebGL 不可用降级**：另起一个禁掉 3D 的浏览器实例来触发（不是改代码）；
 * - **加载失败**：用 Playwright 拦截 `/api/planet/overview` 造出来。
 *
 * 用法：
 *   python scripts/ui-catalog/instance.py up --name planetroot --backend-port 8841 --frontend-port 6206 --seed --clean
 *   $env:QIO_BASE="http://127.0.0.1:6206"; $env:QIO_API="http://127.0.0.1:8841"
 *   node scripts/ui-catalog/planet-root.mjs
 */
import {
  API as ENV_API,
  BASE as ENV_BASE,
  chromium,
  createSession,
  launchBrowser,
  saveManifest,
  sleep,
} from "./lib.mjs";

const INSTANCE = { base: ENV_BASE, api: ENV_API };
const GROUP = "planet-root";
const ENTRY = "[data-planet-entry]";

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

/**
 * 打开星球并等场景真的画出来。
 * `planet-view` 挂上只是开始，three.js 冷启动期间画布是空的 —— 等 scale 变量
 * 落到 1（B 阶段结束）+ 再留一段渲染时间。
 */
async function openPlanet(s, { settleMs = 2600 } = {}) {
  await s.page.locator(ENTRY).click();
  await s.page.waitForSelector(".planet-view", { timeout: 20000 });
  await sleep(settleMs);
  await s.page.waitForFunction(
    () => {
      const stage = document.querySelector(".planet-stage");
      const k = stage?.getAttribute("data-stage-k");
      return k === undefined || Math.abs(Number(k) - 1) < 0.02;
    },
    null,
    { timeout: 20000 },
  ).catch(() => {});
  await sleep(700);
}

async function switchTab(s, tab) {
  await s.page.locator(`.tabs .tab-${tab}`).click();
  await sleep(900);
}

async function main() {
  const browser = await launchBrowser();

  // ---------------------------------------------------------------- 入口球（在对话页上）
  const s1 = await createSession(browser, { group: GROUP, name: "星球入口", theme: "dark", ...INSTANCE });
  await s1.goto("#/", { waitFor: ".conversation", settle: 1800 });
  await safe("planet-01-entry-orb", "星球入口球（对话页右下）", async () => {
    await s1.shotEl(ENTRY, "planet-01-entry-orb", "星球入口球（对话页右下）", { pad: 20 });
  });
  await safe("planet-02-entry-orb-hover", "星球入口球：悬停", async () => {
    await s1.page.locator(ENTRY).hover();
    await sleep(700);
    await s1.shotEl(ENTRY, "planet-02-entry-orb-hover", "星球入口球：悬停", { pad: 20 });
  });

  // ---------------------------------------------------------------- 打开连续体逐帧（慢放）
  const sSlow = await createSession(browser, {
    group: GROUP,
    name: "打开连续体",
    theme: "dark",
    ...INSTANCE,
  });
  await safe("planet-03-open-frames", "打开连续体逐帧（铺底 → 长大 → 接管 → 浮层）", async () => {
    await sSlow.goto("#/", { extraQuery: "planetdemo=slow", waitFor: ".conversation", settle: 1600 });
    await sSlow.page.locator(ENTRY).click();
    await sSlow.page.waitForSelector(".planet-view", { timeout: 20000 });
    // 慢放模式下每个阶段约 3 倍时长：分几个时间点取帧
    await sleep(500);
    await sSlow.shot("planet-03a-open-curtain", "打开阶段 A：铺底 + 入口激活", { settle: 0 });
    await sleep(1200);
    await sSlow.shot("planet-03b-open-expanding", "打开阶段 B：体量展开中", { settle: 0 });
    await sleep(1600);
    await sSlow.shot("planet-03c-open-scene", "打开阶段 C：场景接管", { settle: 0 });
    await sleep(2200);
    await sSlow.shot("planet-03d-open-layers", "打开阶段 D：玻璃浮层进入", { settle: 0 });
  });
  all.push(...sSlow.entries.splice(0));
  flush();
  await sSlow.close({ save: false });

  // ---------------------------------------------------------------- 全屏星球与面板
  const s2 = await createSession(browser, { group: GROUP, name: "星球面板", theme: "dark", ...INSTANCE });
  await s2.goto("#/", { waitFor: ".conversation", settle: 1800 });
  await safe("planet-10-full", "全屏星球：overview + 右侧面板", async () => {
    await openPlanet(s2);
    await s2.shot("planet-10-full", "全屏星球：overview + 右侧面板");
  });
  await safe("planet-11-panel", "右侧面板：话题列表 + 详情", async () => {
    await s2.shotEl(".panel", "planet-11-panel", "右侧面板：话题列表 + 详情", { pad: 10 });
  });
  await safe("planet-12-panel-collapsed", "面板收起态", async () => {
    await s2.page.locator(".panel-toggle").click();
    await sleep(700);
    await s2.shot("planet-12-panel-collapsed", "面板收起态");
    await s2.page.locator(".panel-toggle").click();
    await sleep(700);
  });
  await safe("planet-13-search", "话题搜索：过滤中", async () => {
    await s2.page.locator(".panel-head input.qio-input").fill("审批");
    await sleep(700);
    await s2.shotEl(".panel", "planet-13-search", "话题搜索：过滤中", { pad: 10 });
  });
  await safe("planet-14-search-empty", "话题搜索：无结果", async () => {
    await s2.page.locator(".panel-head input.qio-input").fill("zzz不存在");
    await sleep(700);
    await s2.shotEl(".panel", "planet-14-search-empty", "话题搜索：无结果", { pad: 10 });
    await s2.page.locator(".panel-head input.qio-input").fill("");
    await sleep(600);
  });
  await safe("planet-15-topic-selected", "选中话题：详情 + 片段历史", async () => {
    await s2.page.locator(".topic-list li").first().click();
    await sleep(1200);
    await s2.shotEl(".panel", "planet-15-topic-selected", "选中话题：详情 + 片段历史", { pad: 10 });
  });
  await safe("planet-16-fragment-selected", "片段历史：选中一段（从这里继续可用）", async () => {
    const frag = s2.page.locator(".fragment-item").last();
    await frag.scrollIntoViewIfNeeded().catch(() => {});
    await frag.click();
    await sleep(700);
    await s2.shotEl(".detail-scroll", "planet-16-fragment-selected", "片段历史：选中一段", { pad: 8 });
  });
  await safe("planet-17-raw", "查看原文（只读历史）", async () => {
    const toggle = s2.page.locator(".raw-toggle").last();
    await toggle.scrollIntoViewIfNeeded().catch(() => {});
    await toggle.click();
    await sleep(1400);
    await s2.shotEl(".fragment-item >> nth=-1", "planet-17-raw", "查看原文（只读历史）", { pad: 8 });
  });
  await safe("planet-18-start-here", "从这里继续：写入起点", async () => {
    const btn = s2.page.locator(".start-btn").last();
    await btn.scrollIntoViewIfNeeded().catch(() => {});
    await btn.click();
    await sleep(1800);
    await s2.shot("planet-18-start-here", "从这里继续后的结果", {
      note: "成功则回到对话页并带上锚点；失败则面板保留并显示起点失败原因",
    });
  });
  all.push(...s2.entries.splice(0));
  flush();

  // 知识 / 实体面板（重开一次星球拿到干净状态）
  const s3 = await createSession(browser, { group: GROUP, name: "知识实体", theme: "dark", ...INSTANCE });
  await s3.goto("#/", { waitFor: ".conversation", settle: 1800 });
  await openPlanet(s3);
  await safe("planet-20-knowledge", "知识面板：各状态同屏", async () => {
    await switchTab(s3, "knowledge");
    await s3.shotEl(".panel", "planet-20-knowledge", "知识面板：各状态同屏", { pad: 10 });
  });
  await safe("planet-21-knowledge-edit", "知识面板：内联修正", async () => {
    const edit = s3.page.locator(".k-edit-open").first();
    await edit.scrollIntoViewIfNeeded().catch(() => {});
    await edit.click();
    await sleep(600);
    await s3.shotEl(".k-item >> nth=0", "planet-21-knowledge-edit", "知识面板：内联修正", { pad: 8 });
  });
  await safe("planet-22-knowledge-archive", "知识面板：归档确认（inline 档）", async () => {
    const archive = s3.page.locator(".k-archive").nth(1);
    await archive.scrollIntoViewIfNeeded().catch(() => {});
    await archive.click();
    await sleep(700);
    await s3.shotEl(".kpanel", "planet-22-knowledge-archive", "知识面板：归档确认", { pad: 10 });
  });
  await safe("planet-23-knowledge-create", "知识面板：新建表单", async () => {
    await s3.page.locator(".k-clear-filters").first().click({ timeout: 3000 }).catch(() => {});
    await s3.page.locator(".k-create-open").click();
    await sleep(700);
    await s3.shotEl(".kpanel", "planet-23-knowledge-create", "知识面板：新建表单", { pad: 10 });
  });
  await safe("planet-30-entities", "实体面板：实体卡列表", async () => {
    await switchTab(s3, "entity");
    await s3.shotEl(".panel", "planet-30-entities", "实体面板：实体卡列表", { pad: 10 });
  });
  await safe("planet-31-entity-detail", "实体详情：摘要 / 别名 / 属性 / 关系", async () => {
    await s3.page.locator(".e-item").first().click();
    await sleep(900);
    await s3.shotEl(".panel", "planet-31-entity-detail", "实体详情", { pad: 10 });
  });
  await safe("planet-32-entity-archive", "实体：归档确认", async () => {
    await s3.page.locator(".e-revoke").first().click();
    await sleep(700);
    await s3.shotEl(".panel", "planet-32-entity-archive", "实体：归档确认", { pad: 10 });
  });
  await safe("planet-33-manage-mode", "管理模式", async () => {
    await switchTab(s3, "topic");
    await s3.page.locator(".mode-btn").click();
    await sleep(800);
    await s3.shot("planet-33-manage-mode", "管理模式");
  });
  all.push(...s3.entries.splice(0));
  flush();
  await s3.close({ save: false });

  // ---------------------------------------------------------------- 开发者模式 HUD
  const sDev = await createSession(browser, {
    group: GROUP,
    name: "开发者模式",
    theme: "dark",
    developerMode: true,
    ...INSTANCE,
  });
  await safe("planet-40-hud", "开发者模式：FPS / 视角 HUD", async () => {
    await sDev.goto("#/", { waitFor: ".conversation", settle: 1800 });
    await openPlanet(sDev);
    await sDev.shot("planet-40-hud", "开发者模式：FPS / 视角 HUD");
    await sDev.shotEl(".hud", "planet-40b-hud-crop", "HUD（局部）", { pad: 10 });
  });
  all.push(...sDev.entries.splice(0));
  flush();
  await sDev.close({ save: false });

  // ---------------------------------------------------------------- 加载失败 / 收起中
  const sErr = await createSession(browser, { group: GROUP, name: "加载失败", theme: "dark", ...INSTANCE });
  await safe("planet-41-load-error", "首次数据加载失败 + 重试", async () => {
    await sErr.page.route("**/api/planet/overview*", (route) => route.abort());
    await sErr.goto("#/", { waitFor: ".conversation", settle: 1600 });
    await openPlanet(sErr, { settleMs: 3200 });
    await sErr.shot("planet-41-load-error", "首次数据加载失败 + 重试");
  });
  all.push(...sErr.entries.splice(0));
  flush();
  await sErr.close({ save: false });

  const sClose = await createSession(browser, { group: GROUP, name: "收起", theme: "dark", ...INSTANCE });
  await safe("planet-42-closing", "收起星球：收势 → 收拢", async () => {
    await sClose.goto("#/", { waitFor: ".conversation", settle: 1800 });
    await openPlanet(sClose);
    await sClose.page.locator(".close-btn").click();
    await sleep(260);
    await sClose.shot("planet-42a-closing", "收起阶段 A：浮层退场 + 收势", { settle: 0 });
    await sleep(700);
    await sClose.shot("planet-42b-collapsing", "收起阶段 B：体量收拢回入口", { settle: 0 });
    await sleep(1400);
    await sClose.shot("planet-42c-back", "收起完成：回到对话页", { settle: 0 });
  });
  all.push(...sClose.entries.splice(0));
  flush();
  await sClose.close({ save: false });

  // ---------------------------------------------------------------- 浅色主题
  const sLight = await createSession(browser, { group: GROUP, name: "星球浅色", theme: "light", ...INSTANCE });
  await safe("planet-50-light-full", "浅色主题：全屏星球", async () => {
    await sLight.goto("#/", { waitFor: ".conversation", settle: 1800 });
    await openPlanet(sLight);
    await sLight.shot("planet-50-light-full", "浅色主题：全屏星球");
  });
  await safe("planet-51-light-knowledge", "浅色主题：知识面板", async () => {
    await switchTab(sLight, "knowledge");
    await sLight.shotEl(".panel", "planet-51-light-knowledge", "浅色主题：知识面板", { pad: 10 });
  });
  all.push(...sLight.entries.splice(0));
  flush();
  await sLight.close({ save: false });

  // ---------------------------------------------------------------- 窄窗口
  const sNarrow = await createSession(browser, {
    group: GROUP,
    name: "星球窄窗口",
    theme: "dark",
    viewport: { width: 820, height: 900 },
    ...INSTANCE,
  });
  await safe("planet-60-narrow", "窄窗口：星球层与面板覆盖", async () => {
    await sNarrow.goto("#/", { waitFor: ".conversation", settle: 1800 });
    await openPlanet(sNarrow);
    await sNarrow.shot("planet-60-narrow", "窄窗口：星球层与面板覆盖");
  });
  all.push(...sNarrow.entries.splice(0));
  flush();
  await sNarrow.close({ save: false });
  await browser.close();

  // ---------------------------------------------------------------- WebGL 不可用（禁掉 3D 的浏览器）
  await safe("planet-70-webgl-fallback", "WebGL 不可用：降级说明 + 面板仍可用", async () => {
    const noGl = await chromium.launch({
      channel: "msedge",
      headless: true,
      args: ["--disable-3d-apis", "--disable-webgl", "--disable-gpu"],
    });
    const s = await createSession(noGl, { group: GROUP, name: "WebGL 降级", theme: "dark", ...INSTANCE });
    await s.goto("#/", { waitFor: ".conversation", settle: 1800 });
    await openPlanet(s, { settleMs: 2600 });
    await s.shot("planet-70-webgl-fallback", "WebGL 不可用：降级说明 + 面板仍可用");
    await s.shotEl(".webgl-fallback", "planet-70b-webgl-fallback-crop", "WebGL 降级提示（局部）", {
      pad: 10,
    });
    all.push(...s.entries.splice(0));
    await s.close({ save: false });
    await noGl.close();
  });
  flush();

  console.log(`\n合计 ${all.length} 张；失败 ${failures.length} 项。`);
  for (const f of failures) console.log(`  - ${f.id} ${f.title} :: ${f.error}`);
}

await main();
