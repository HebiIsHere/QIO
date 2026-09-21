/**
 * QIO「全部 UI」穷举截图：共享采集库。
 *
 * 为什么有它：UI 目录需要对上百个界面状态逐个截图，每个采集脚本都要
 * 「开浏览器 → 定主题/视口 → 打开页面 → 造状态 → 截图 → 记元数据」。
 * 这套动作集中在这里，各分组脚本只描述「状态怎么造」，产物格式统一，
 * 后面的拼图 / 图册 / 索引才能自动读同一份 manifest。
 *
 * 约定：
 * - 截图落在 `frontend/e2e-shots/ui-catalog/<group>/<id>.png`（该目录已被 .gitignore 忽略）；
 * - 元数据落在 `frontend/e2e-shots/ui-catalog/manifest-<group>.json`，
 *   每个分组一个文件，分组之间不互相覆盖；
 * - 一处状态 = 一张图 = manifest 里一条记录，标题用中文（给人看的）。
 *
 * 依赖：Playwright + 系统 Edge（与仓库既有 scripts/baseline/* 相同，不新增依赖）。
 * 环境变量：QIO_BASE / QIO_API 可指向隔离实例（见 scripts/ui-catalog/instance.py）。
 */
import { createRequire } from "node:module";
import { mkdirSync, readFileSync, writeFileSync, existsSync, statSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const require = createRequire(import.meta.url);

export const PLAYWRIGHT_PATH =
  process.env.QIO_PW ||
  "C:\\Users\\zxy\\.cache\\codex-runtimes\\codex-primary-runtime\\dependencies\\node\\node_modules\\playwright";
export const { chromium } = require(PLAYWRIGHT_PATH);

const HERE = path.dirname(fileURLToPath(import.meta.url));
/** 仓库里的 qio 目录 */
export const QIO_ROOT = path.resolve(HERE, "..", "..");
export const SHOT_ROOT =
  process.env.QIO_SHOT_ROOT || path.join(QIO_ROOT, "frontend", "e2e-shots", "ui-catalog");

/** 前端 dev server / 后端地址：默认主实例，可用环境变量指向隔离实例 */
export const BASE = process.env.QIO_BASE || "http://127.0.0.1:5199";
export const API = process.env.QIO_API || "http://127.0.0.1:8734";

export const DEFAULT_VIEWPORT = { width: 1440, height: 900 };
/** 窄窗口：命中 Composer / MessageStream 的 899px 断点 */
export const NARROW_VIEWPORT = { width: 820, height: 900 };
/** 超窄：命中设置页单列断点 */
export const TINY_VIEWPORT = { width: 620, height: 860 };

export const THEME_KEY = "qio-theme";
export const MOTION_KEY = "qio-motion";
export const DEV_MODE_KEY = "qio-developer-mode";

export const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

let freshSeq = Math.floor(Date.now() / 1000) % 100000;
/**
 * 带新参数的地址。IAB 与 Vite 对模块文件不重新校验，同一个 URL 可能继续跑旧模块；
 * 每个新页面都带一个递增的 fresh 参数，保证加载的是当前代码。
 */
export function freshUrl(hash = "#/", extraQuery = "", base = BASE) {
  freshSeq += 1;
  const q = extraQuery ? `&${extraQuery}` : "";
  return `${base}/?fresh=${freshSeq}${q}${hash}`;
}

export async function launchBrowser({ headless = true } = {}) {
  return chromium.launch({
    channel: "msedge",
    headless,
    // 软件渲染：无头环境没有真 GPU，星球（WebGL）要能出图
    args: [
      "--use-gl=angle",
      "--use-angle=swiftshader",
      "--enable-unsafe-swiftshader",
      "--force-color-profile=srgb",
      "--font-render-hinting=none",
    ],
  });
}

/** 后端测试事件注入口（开发模式才注册）：把界面推进到目标状态，不调用真实模型 */
export async function injectEvent(type, data = {}, api = API) {
  const resp = await fetch(`${api}/api/events/test?event_type=${encodeURIComponent(type)}`, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify(data),
  });
  if (!resp.ok) throw new Error(`inject ${type} -> ${resp.status} ${await resp.text()}`);
  return resp.json();
}

/** 直接打后端 API（播种/查询用） */
export async function apiRequest(method, pathname, body, api = API) {
  const resp = await fetch(`${api}${pathname}`, {
    method,
    headers: body === undefined ? undefined : { "content-type": "application/json" },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  const text = await resp.text();
  if (!resp.ok) throw new Error(`${method} ${pathname} -> ${resp.status} ${text}`);
  return text ? JSON.parse(text) : null;
}

/**
 * 一个采集会话 = 一个浏览器上下文（固定主题 + 固定视口）。
 * 主题必须靠 addInitScript 预置 localStorage：无头浏览器默认浅色，
 * 不预置的话「暗色截图」会拍成浅色。
 */
export async function createSession(browser, options = {}) {
  const {
    group,
    name = "",
    theme = "dark",
    viewport = DEFAULT_VIEWPORT,
    motion = null,
    developerMode = false,
    storage = {},
    base = BASE,
    api = API,
    reducedMotion = null,
  } = options;
  if (!group) throw new Error("createSession 需要 group");

  const context = await browser.newContext({
    viewport,
    deviceScaleFactor: 1,
    ...(reducedMotion ? { reducedMotion } : {}),
  });
  await context.addInitScript(
    ({ theme, motion, developerMode, storage }) => {
      try {
        localStorage.setItem("qio-theme", theme);
        if (motion) localStorage.setItem("qio-motion", motion);
        if (developerMode) localStorage.setItem("qio-developer-mode", "1");
        else localStorage.removeItem("qio-developer-mode");
        for (const [k, v] of Object.entries(storage)) localStorage.setItem(k, String(v));
      } catch {
        /* localStorage 不可用时忽略（与产品内的兜底一致） */
      }
    },
    { theme, motion, developerMode, storage },
  );

  const page = await context.newPage();
  const consoleErrors = [];
  page.on("console", (m) => {
    if (m.type() === "error") consoleErrors.push(m.text().slice(0, 300));
  });
  page.on("pageerror", (e) => consoleErrors.push(`pageerror: ${String(e).slice(0, 300)}`));

  const entries = [];
  const groupDir = path.join(SHOT_ROOT, group);
  mkdirSync(groupDir, { recursive: true });

  const session = {
    page,
    context,
    group,
    theme,
    viewport,
    base,
    api,
    entries,
    consoleErrors,

    /**
     * 打开页面。
     * 简写：`goto("#/settings")`
     * 完整：`goto({ path: "/ui-catalog.html", hash: "#/" })`（path 用于多页入口，如原子组件画廊）
     */
    async goto(target = "#/", opts = {}) {
      const conf = typeof target === "string" ? { ...opts, hash: target } : { ...target };
      const { path: pathname = "/", hash = "#/", extraQuery = "", waitFor = null, settle = 400 } =
        conf;
      freshSeq += 1;
      const url = `${base}${pathname}?fresh=${freshSeq}${extraQuery ? `&${extraQuery}` : ""}${hash}`;
      await page.goto(url);
      if (waitFor) await page.waitForSelector(waitFor, { timeout: 20000 });
      await sleep(settle);
      return session;
    },

    async inject(type, data = {}) {
      await injectEvent(type, data, api);
      return session;
    },

    /**
     * 截一张图并记一条元数据。
     * opts.fullPage：整页（页面自身滚动时用）；内部滚动容器要用 shotEl。
     */
    async shot(id, title, opts = {}) {
      const {
        note = "",
        fullPage = false,
        clip = null,
        waitMs = 0,
        settle = 220,
        // 默认不动动画：星球那类「慢放逐帧」采集需要动画真的在跑
        animations = "allow",
      } = opts;
      if (waitMs) await sleep(waitMs);
      if (settle) await sleep(settle);
      const file = path.join(groupDir, `${id}.png`);
      await page.screenshot({ path: file, fullPage, animations, ...(clip ? { clip } : {}) });
      const entry = {
        id,
        group,
        title,
        note,
        file,
        theme,
        viewport: { width: viewport.width, height: viewport.height },
        url: page.url(),
      };
      const size = existsSync(file) ? statSync(file).size : 0;
      if (size < 1000) throw new Error(`截图疑似为空：${file} (${size} bytes)`);
      entries.push(entry);
      console.log(`  [shot] ${id} ${title} (${Math.round(size / 1024)}KB)`);
      return entry;
    },

    /** 对某个元素截图（卡片 / 面板 / 弹窗这类局部界面） */
    async shotEl(selector, id, title, opts = {}) {
      const { note = "", waitMs = 0, settle = 200, pad = 0, animations = "allow" } = opts;
      if (waitMs) await sleep(waitMs);
      if (settle) await sleep(settle);
      const loc = page.locator(selector).first();
      await loc.waitFor({ state: "visible", timeout: 15000 });
      // 元素在视口外时，clip 截图会拍到「空白区域」（Clipped area is either empty...）：
      // 先把目标滚进视口再算边界
      await loc.scrollIntoViewIfNeeded().catch(() => {});
      await sleep(180);
      const file = path.join(groupDir, `${id}.png`);
      if (pad) {
        const box = await loc.boundingBox();
        await page.screenshot({
          path: file,
          animations,
          clip: {
            x: Math.max(0, box.x - pad),
            y: Math.max(0, box.y - pad),
            width: box.width + pad * 2,
            height: box.height + pad * 2,
          },
        });
      } else {
        await loc.screenshot({ path: file, animations });
      }
      const entry = {
        id,
        group,
        title,
        note,
        file,
        theme,
        viewport: { width: viewport.width, height: viewport.height },
        url: page.url(),
      };
      const size = existsSync(file) ? statSync(file).size : 0;
      if (size < 800) throw new Error(`元素截图疑似为空：${file} (${size} bytes)`);
      entries.push(entry);
      console.log(`  [shot] ${id} ${title} (${Math.round(size / 1024)}KB)`);
      return entry;
    },

    /** 用键盘操作（Enter 提交、Esc 关闭这类语义） */
    async press(key) {
      await page.keyboard.press(key);
      await sleep(250);
      return session;
    },

    /** 只记一条元数据、不截图（同一张图说明多个状态时用） */
    note(id, title) {
      entries.push({
        id: `${id}#note`,
        group,
        title,
        note: "",
        file: null,
        theme,
        viewport: { width: viewport.width, height: viewport.height },
        url: page.url(),
      });
    },

    async close({ save = true } = {}) {
      if (save) saveManifest(group, entries);
      await context.close();
    },
  };
  return session;
}

export const MANIFEST_DIR = SHOT_ROOT;

export function manifestPath(group) {
  return path.join(MANIFEST_DIR, `manifest-${group}.json`);
}

/**
 * 写入该分组的 manifest（分组独占一个文件，互不覆盖）。
 * `fileName` 可覆盖文件名：同一目录下多路采集并存时用它避免互相覆盖
 * （例如 root 的对话组合集写 `manifest-conv-root.json`）。
 */
export function saveManifest(group, entries, { fileName } = {}) {
  mkdirSync(MANIFEST_DIR, { recursive: true });
  const target = path.join(MANIFEST_DIR, fileName ?? `manifest-${group}.json`);
  // 一个分组通常开多个采集会话，每个会话收工时各写一次 → 按 id 合并而不是整文件覆盖。
  // 同一 id 再次写入视为「重拍」，原地替换；不同 id 追加在后。
  let merged = [...entries];
  if (existsSync(target)) {
    try {
      const previous = JSON.parse(readFileSync(target, "utf-8"));
      merged = [...(previous?.entries ?? [])];
      for (const entry of entries) {
        const index = merged.findIndex((e) => e.id === entry.id);
        if (index >= 0) merged[index] = entry;
        else merged.push(entry);
      }
    } catch {
      merged = [...entries];
    }
  }
  const payload = {
    group,
    generatedAt: new Date().toISOString(),
    count: merged.length,
    entries: merged,
  };
  writeFileSync(target, JSON.stringify(payload, null, 2), "utf-8");
  console.log(`\n${group}: 本轮 ${entries.length} 条 / 累计 ${merged.length} 条 → ${target}`);
}

export function readManifest(group) {
  const p = manifestPath(group);
  if (!existsSync(p)) return null;
  return JSON.parse(readFileSync(p, "utf-8"));
}

/** 便捷入口：跑一个分组脚本时统一收敛异常，保证退出码可用 */
export async function runGroup(main) {
  try {
    await main();
  } catch (err) {
    console.error(`\n[失败] ${err?.stack ?? err}`);
    process.exitCode = 1;
  }
}
