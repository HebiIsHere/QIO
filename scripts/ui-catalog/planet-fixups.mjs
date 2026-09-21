/**
 * 补拍 `planet-root` 里没跑完的两张，并把磁盘上有图、清单里没登记的条目补登记。
 *
 * 上次的问题有两个：
 * 1. 浅色星球在软件渲染下把渲染器拖垮（浏览器直接没了）→ 这一次**每个状态各起一个
 *    浏览器**，一边崩了不影响另一边，视口也降到 1280×800；
 * 2. 打开星球的等待条件太弱（`.planet-view` 在「球态」时也已经在 DOM 里）→
 *    改成等 `.planet-view.layers`（浮层进入），等不到才退化成定时等待。
 */
import { readdirSync, statSync } from "node:fs";
import path from "node:path";
import {
  API as ENV_API,
  BASE as ENV_BASE,
  SHOT_ROOT,
  createSession,
  launchBrowser,
  readManifest,
  saveManifest,
  sleep,
} from "./lib.mjs";

const INSTANCE = { base: ENV_BASE, api: ENV_API };
const GROUP = "planet-root";
const ENTRY = "[data-planet-entry]";

const previous = readManifest(GROUP)?.entries ?? [];
const merged = [...previous];
const failures = [];

async function safe(id, title, fn) {
  try {
    await fn();
    console.log(`  [ok] ${id}`);
  } catch (err) {
    const message = String(err?.message ?? err).replace(/\s+/g, " ").slice(0, 200);
    failures.push({ id, title, error: message });
    console.log(`  [FAIL] ${id} :: ${message}`);
  }
}

function collect(s) {
  for (const entry of s.entries.splice(0, s.entries.length)) {
    const index = merged.findIndex((e) => e.id === entry.id);
    if (index >= 0) merged[index] = entry;
    else merged.push(entry);
  }
}

async function openPlanet(s, settleMs = 2400) {
  await s.page.locator(ENTRY).click({ timeout: 15000 });
  // 球态时 `.planet-view` 也在 DOM 里，所以等「浮层进入」这个状态类
  await s.page.waitForSelector(".planet-view.layers", { timeout: 30000 }).catch(() => {});
  await sleep(settleMs);
}

/**
 * 每一步都上硬超时：软件渲染下 WebGL 有可能把渲染器拖住，此时 Playwright 自己的
 * timeout 也未必会返回 —— 用 Promise.race 保证「要么出结果，要么如实失败」。
 */
function withTimeout(promise, ms, label) {
  return Promise.race([
    promise,
    new Promise((_, reject) => setTimeout(() => reject(new Error(`${label} 超时 ${ms}ms`)), ms)),
  ]);
}

// ---------------------------------------------------------------- 浅色主题知识面板
await safe("planet-51-light-knowledge", "浅色主题：知识面板", async () => {
  const browser = await launchBrowser();
  const s = await createSession(browser, {
    group: GROUP,
    name: "补拍",
    theme: "light",
    viewport: { width: 1280, height: 800 },
    ...INSTANCE,
  });
  s.page.setDefaultTimeout(20000);
  await withTimeout(s.goto("#/", { waitFor: ".conversation", settle: 1200 }), 40000, "打开对话页");
  await withTimeout(openPlanet(s, 2400), 60000, "打开星球");
  await withTimeout(
    s.page.locator(".tabs .tab-knowledge").click({ timeout: 15000 }),
    30000,
    "切到知识面板",
  );
  await sleep(1800);
  await withTimeout(
    s.shotEl(".panel", "planet-51-light-knowledge", "浅色主题：知识面板", { pad: 10 }),
    45000,
    "截图",
  );
  collect(s);
  await withTimeout(
    s.shot("planet-51b-light-planet", "浅色主题：知识面板打开时的整屏"),
    45000,
    "截图（整屏）",
  );
  collect(s);
  // 关闭也要有超时：渲染器被拖住时 context.close() 会一直等下去
  await withTimeout(s.close({ save: false }), 15000, "关闭会话").catch(() => {});
  await withTimeout(browser.close(), 15000, "关闭浏览器").catch(() => {});
});

// ---------------------------------------------------------------- WebGL 不可用降级
await safe("planet-70-webgl-fallback", "WebGL 不可用：降级说明 + 面板仍可用", async () => {
  /**
   * 用「canvas.getContext("webgl*") 返回 null」触发降级：等价于一台没有 WebGL 的机器，
   * 产品代码一行没动（也比另起一个禁 GPU 的浏览器稳 —— 上次就是卡在那边）。
   */
  const browser = await launchBrowser();
  const s = await createSession(browser, { group: GROUP, name: "补拍", theme: "dark", ...INSTANCE });
  await s.context.addInitScript(() => {
    const original = HTMLCanvasElement.prototype.getContext;
    HTMLCanvasElement.prototype.getContext = function (type, ...rest) {
      if (String(type).toLowerCase().startsWith("webgl")) return null;
      return original.call(this, type, ...rest);
    };
  });
  s.page.setDefaultTimeout(20000);
  await withTimeout(s.goto("#/", { waitFor: ".conversation", settle: 1500 }), 40000, "打开对话页");
  await withTimeout(openPlanet(s, 1500), 45000, "打开星球");
  await withTimeout(
    s.shot("planet-70-webgl-fallback", "WebGL 不可用：降级说明 + 面板仍可用"),
    45000,
    "截图",
  );
  collect(s);
  await withTimeout(
    s.shotEl(".webgl-fallback", "planet-70b-webgl-fallback-crop", "WebGL 降级提示（局部）", {
      pad: 10,
    }),
    45000,
    "截图（局部）",
  );
  collect(s);
  await withTimeout(s.close({ save: false }), 15000, "关闭会话").catch(() => {});
  await withTimeout(browser.close(), 15000, "关闭浏览器").catch(() => {});
});

// ---------------------------------------------------------------- 补登记：磁盘上有图、清单里没有
/**
 * 上一次运行中途被杀，有几张图写进了目录但清单没来得及记（清单只在分段结束时落盘）。
 * 图是证据，不能因为清单漏了就当作不存在：这里按已知的 id→标题补上。
 */
const TITLES = {
  "planet-01-entry-orb": "星球入口球（对话页右下）",
  "planet-02-entry-orb-hover": "星球入口球：悬停",
  "planet-03a-open-curtain": "打开阶段 A：铺底 + 入口激活",
  "planet-03b-open-expanding": "打开阶段 B：体量展开中",
  "planet-03c-open-scene": "打开阶段 C：场景接管",
  "planet-03d-open-layers": "打开阶段 D：玻璃浮层进入",
  "planet-10-full": "全屏星球：overview + 右侧面板",
  "planet-11-panel": "右侧面板：话题列表 + 详情",
  "planet-12-panel-collapsed": "面板收起态",
  "planet-13-search": "话题搜索：过滤中",
  "planet-14-search-empty": "话题搜索：无结果",
  "planet-15-topic-selected": "选中话题：详情 + 片段历史",
  "planet-16-fragment-selected": "片段历史：选中一段",
  "planet-17-raw": "查看原文（只读历史）",
  "planet-18-start-here": "从这里继续后的结果",
  "planet-20-knowledge": "知识面板：各状态同屏",
  "planet-21-knowledge-edit": "知识面板：内联修正",
  "planet-22-knowledge-archive": "知识面板：归档确认",
  "planet-23-knowledge-create": "知识面板：新建表单",
  "planet-30-entities": "实体面板：实体卡列表",
  "planet-31-entity-detail": "实体详情：摘要 / 别名 / 属性 / 关系",
  "planet-32-entity-archive": "实体：归档确认",
  "planet-33-manage-mode": "管理模式",
  "planet-40-hud": "开发者模式：FPS / 视角 HUD",
  "planet-40b-hud-crop": "HUD（局部）",
  "planet-41-load-error": "首次数据加载失败 + 重试",
  "planet-42a-closing": "收起阶段 A：浮层退场 + 收势",
  "planet-42b-collapsing": "收起阶段 B：体量收拢回入口",
  "planet-42c-back": "收起完成：回到对话页",
  "planet-50-light-full": "浅色主题：全屏星球",
  "planet-60-narrow": "窄窗口：星球层与面板覆盖",
  "planet-51-light-knowledge": "浅色主题：知识面板",
  "planet-51b-light-planet": "浅色主题：知识面板打开时的整屏",
  "planet-70-webgl-fallback": "WebGL 不可用：降级说明 + 面板仍可用",
  "planet-70b-webgl-fallback-crop": "WebGL 降级提示（局部）",
};

const dir = path.join(SHOT_ROOT, GROUP);
let registered = 0;
for (const name of readdirSync(dir).filter((f) => f.endsWith(".png"))) {
  const id = name.replace(/\.png$/, "");
  if (merged.some((e) => e.id === id)) continue;
  const file = path.join(dir, name);
  merged.push({
    id,
    group: GROUP,
    title: TITLES[id] ?? `${id}（补登记）`,
    note: "上一次运行中途被杀，清单没记上这一条；图来自同一次真实采集",
    file,
    theme: id.includes("light") ? "light" : "dark",
    viewport: id.includes("narrow") ? { width: 820, height: 900 } : { width: 1440, height: 900 },
    url: INSTANCE.base,
    size: statSync(file).size,
  });
  registered += 1;
}

saveManifest(GROUP, merged);
console.log(`\n补登记 ${registered} 张；累计 ${merged.length} 条；失败 ${failures.length} 项。`);
for (const f of failures) console.log(`  - ${f.id} :: ${f.error}`);
