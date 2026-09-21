/**
 * 最后两个星球状态：浅色主题的知识面板、WebGL 不可用降级。
 *
 * 这两张前几次都失败在「元素解析到了但点不动」（Playwright 的可点击性判定不通过）。
 * 所以这里改成：
 * 1. 先用 `elementFromPoint` 量一下「点下去到底命中了谁」——如果命中别的元素，
 *    那本身就是一条可报告的事实；
 * 2. 再用 DOM 事件（`el.click()`）触发，绕开命中测试把界面推进到目标状态。
 * 每步都有硬超时，最后强制退出，绝不挂住。
 */
import {
  API as ENV_API,
  BASE as ENV_BASE,
  chromium,
  createSession,
  saveManifest,
  readManifest,
  sleep,
} from "./lib.mjs";

const INSTANCE = { base: ENV_BASE, api: ENV_API };
const GROUP = "planet-root";
const merged = [...(readManifest(GROUP)?.entries ?? [])];
const notes = [];

const withTimeout = (p, ms, label) =>
  Promise.race([
    p,
    new Promise((_, reject) => setTimeout(() => reject(new Error(`${label} 超时 ${ms}ms`)), ms)),
  ]);

function collect(s) {
  for (const entry of s.entries.splice(0, s.entries.length)) {
    const index = merged.findIndex((e) => e.id === entry.id);
    if (index >= 0) merged[index] = entry;
    else merged.push(entry);
  }
}

/** 量「入口球中心点命中的是谁」，并用 DOM 事件点开星球 */
async function domOpenPlanet(s) {
  const diag = await s.page.evaluate(() => {
    const el = document.querySelector("[data-planet-entry]");
    if (!el) return { found: false };
    const r = el.getBoundingClientRect();
    const at = document.elementFromPoint(r.left + r.width / 2, r.top + r.height / 2);
    return {
      found: true,
      rect: { x: Math.round(r.x), y: Math.round(r.y), w: Math.round(r.width), h: Math.round(r.height) },
      topAtCenter: at ? `${at.tagName}.${String(at.className).slice(0, 40)}` : null,
      pointsToEntry: !!at && (at === el || el.contains(at)),
    };
  });
  notes.push(`入口球命中检测：${JSON.stringify(diag)}`);
  await s.page.evaluate(() => {
    const el = document.querySelector("[data-planet-entry]");
    if (el) el.click();
  });
  await sleep(2600);
  return diag;
}

async function runLightKnowledge() {
  const browser = await chromium.launch({
    channel: "msedge",
    headless: true,
    args: ["--use-gl=angle", "--use-angle=swiftshader", "--enable-unsafe-swiftshader"],
  });
  const s = await createSession(browser, {
    group: GROUP,
    name: "补拍",
    theme: "light",
    viewport: { width: 1280, height: 800 },
    ...INSTANCE,
  });
  s.page.setDefaultTimeout(20000);
  await withTimeout(s.goto("#/", { waitFor: ".conversation", settle: 1200 }), 40000, "打开对话页");
  await withTimeout(domOpenPlanet(s), 40000, "打开星球");
  await s.page.evaluate(() => document.querySelector(".tabs .tab-knowledge")?.click());
  await sleep(2000);
  await withTimeout(
    s.shot("planet-51b-light-planet", "浅色主题：星球层与知识面板"),
    40000,
    "截图（整屏）",
  );
  collect(s);
  await withTimeout(
    s.shotEl(".panel", "planet-51-light-knowledge", "浅色主题：知识面板", { pad: 10 }),
    40000,
    "截图（面板）",
  );
  collect(s);
  await withTimeout(s.close({ save: false }), 12000, "关闭会话").catch(() => {});
  await withTimeout(browser.close(), 12000, "关闭浏览器").catch(() => {});
}

async function runWebglFallback() {
  const browser = await chromium.launch({
    channel: "msedge",
    headless: true,
    args: ["--use-gl=angle", "--use-angle=swiftshader", "--enable-unsafe-swiftshader"],
  });
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
  await withTimeout(domOpenPlanet(s), 40000, "打开星球");
  await withTimeout(
    s.shot("planet-70-webgl-fallback", "WebGL 不可用：降级说明 + 面板仍可用"),
    40000,
    "截图",
  );
  collect(s);
  await withTimeout(
    s.shotEl(".webgl-fallback", "planet-70b-webgl-fallback-crop", "WebGL 降级提示（局部）", {
      pad: 10,
    }),
    40000,
    "截图（局部）",
  );
  collect(s);
  await withTimeout(s.close({ save: false }), 12000, "关闭会话").catch(() => {});
  await withTimeout(browser.close(), 12000, "关闭浏览器").catch(() => {});
}

try {
  await runLightKnowledge();
  console.log("浅色知识面板：完成");
} catch (err) {
  console.log(`浅色知识面板：失败 ${String(err?.message ?? err).slice(0, 160)}`);
}
try {
  await runWebglFallback();
  console.log("WebGL 降级：完成");
} catch (err) {
  console.log(`WebGL 降级：失败 ${String(err?.message ?? err).slice(0, 160)}`);
}

saveManifest(GROUP, merged);
console.log("诊断：");
for (const n of notes) console.log(`  - ${n}`);
process.exit(0);
